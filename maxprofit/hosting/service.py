"""
Point d'entrée du processus hébergé.

    python -m maxprofit.hosting.service --source sim --min-payout 92

Ce qui tourne 24 h/24 aujourd'hui, c'est le COLLECTEUR — pas un bot Telegram.
L'étape 2 de la spec §4 demande quatorze jours de données continues avec moins
de 5 % de bougies écartées, et c'est le seul travail en cours qui ait besoin
d'un hébergement permanent. Le bot Telegram est l'étape 8, conditionnée par une
étape 7 concluante ; l'héberger maintenant reviendrait à payer un serveur pour
diffuser des signaux dont rien n'établit qu'ils valent mieux que le hasard.

Structure du processus :

    thread principal  ->  boucle asyncio  ->  serveur HTTP (sonde /health)
    thread secondaire ->  collecteur (boucle bloquante, sockets, sqlite)

Le collecteur est synchrone : il dort, il lit un générateur bloquant, il écrit
en base. Le réécrire en asynchrone pour le faire cohabiter avec aiohttp serait
une réécriture profonde d'un code qui marche, au bénéfice d'un serveur HTTP qui
sert trois requêtes par heure. Un thread coûte moins cher et isole mieux : une
exception dans le collecteur ne peut pas emporter le serveur, et la sonde reste
capable de signaler la panne — ce qui est précisément son rôle.

Chaque thread a SA connexion SQLite (le collecteur en écriture, la sonde en
lecture seule) : un objet `sqlite3.Connection` n'est pas partageable entre
threads, et le mode WAL rend la lecture concurrente sans verrou.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import aiohttp

from maxprofit.collect.collector import build_config
from maxprofit.collect.sources import PocketOptionSource, SimulatedSource
from maxprofit.core.config import charger_env_local
from maxprofit.core.errors import BotError
from maxprofit.hosting.health import EtatCollecte, start_http_server
from maxprofit.hosting.operateurs import Annuaire, chemin_annuaire
from maxprofit.hosting import version as version_deployee
from maxprofit.hosting.superviseur import Superviseur
from maxprofit.hosting.telegram import BotExploitation, ClientTelegram

ENV_TELEGRAM_JETON = "TELEGRAM_BOT_TOKEN"
ENV_TELEGRAM_CHAT = "TELEGRAM_CHAT_ID"     # facultatif, historique
ENV_CODE_ADMIN = "TELEGRAM_ACCESS_CODE"

log = logging.getLogger("hosting.service")


def _verifier_emplacement_base(db: Path) -> None:
    """Avertit si la base vit dans le répertoire de code (§1.1).

    Ce n'est pas une erreur fatale — en développement c'est parfois voulu —
    mais en production c'est le bug de conception que toute la §1 cherche à
    empêcher : le prochain déploiement remplace ce répertoire, et la collecte
    disparaît sans un message."""
    racine = Path(__file__).resolve().parents[2]
    try:
        db.resolve().relative_to(racine)
    except ValueError:
        return
    log.warning(
        "TRADING_DB_PATH (%s) est DANS le répertoire de code (%s). Un "
        "déploiement écrasera la base. Déplacez-la sur un volume persistant.",
        db, racine,
    )


def _fabriquer_source(nom: str):
    return SimulatedSource() if nom == "sim" else PocketOptionSource(demo=True)


async def _servir(args) -> int:
    cfg = build_config(args)
    _verifier_emplacement_base(cfg.db)
    # En premier, avant tout le reste : c'est la ligne qui dit si le journal
    # qu'on est en train de lire correspond au code qu'on vient de corriger.
    log.info("%s", version_deployee.resume())
    log.info("Base : %s", cfg.db)
    log.info("Paires souscrites au maximum : %d", cfg.max_paires)

    etat_collecte = EtatCollecte(cfg.db)

    async with aiohttp.ClientSession() as http:
        bot = _fabriquer_bot(http)
        superviseur = Superviseur(
            lambda: _fabriquer_source(args.source), cfg,
            alerter=(bot.alerter if bot else None),
        )
        if bot is not None:
            bot._etat = lambda: _resume(superviseur, etat_collecte)
            bot._installer_jeton = superviseur.installer_jeton
            bot._paires = lambda: _paires(superviseur)

        # Le serveur est démarré APRÈS le superviseur, pour lui passer
        # l'installateur à la construction : aiohttp déprécie la modification
        # d'une application déjà démarrée.
        #
        # Deux chemins pour un même geste : Telegram quand on a le jeton sous
        # la main, POST /session quand l'outil de capture l'envoie lui-même.
        runner = await start_http_server(
            cfg.db, installer=superviseur.installer_jeton)

        boucle = asyncio.get_running_loop()

        def _arreter(*_):
            log.info("Signal reçu, arrêt du collecteur...")
            superviseur.arreter()
            if bot is not None:
                bot.actif = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                boucle.add_signal_handler(sig, _arreter)
            except NotImplementedError:
                # Windows : add_signal_handler n'existe pas sur la boucle Proactor.
                signal.signal(sig, lambda *_: _arreter())

        if args.duration:
            boucle.call_later(args.duration, _arreter)

        taches = [asyncio.create_task(superviseur.boucler(), name="superviseur")]
        if bot is not None:
            await bot.alerter("🟢 <b>Collecte démarrée</b>")
            taches.append(asyncio.create_task(bot.boucler(), name="telegram"))

        # Le superviseur commande : quand il rend la main, le processus s'arrête.
        # Le bot n'est qu'un canal ; le laisser maintenir le processus en vie
        # donnerait une instance verte côté hébergeur qui n'enregistre plus rien.
        try:
            await taches[0]
        except Exception:
            log.exception("Le collecteur s'est arrêté sur une exception")
        finally:
            for tache in taches[1:]:
                tache.cancel()
            await asyncio.gather(*taches[1:], return_exceptions=True)
            await runner.cleanup()

    log.info("Processus terminé.")
    return 0


def _fabriquer_bot(http) -> BotExploitation | None:
    """`None` si Telegram n'est pas configuré : la collecte doit tourner sans.

    Les deux variables vont ensemble. Un jeton sans identifiant de conversation
    donnerait un bot sans liste blanche, donc pilotable par quiconque le
    découvre — on refuse plutôt que de démarrer à moitié.
    """
    jeton = os.environ.get(ENV_TELEGRAM_JETON, "").strip()
    if not jeton:
        log.info("Telegram non configuré : ni alertes ni renouvellement à "
                 "distance.")
        return None

    annuaire = Annuaire(chemin_annuaire())
    if len(annuaire) == 0 and not os.environ.get(ENV_CODE_ADMIN, "").strip():
        # Ni inscrit ni code : le bot répondrait à tout le monde « demandez un
        # code » sans que ce code existe. Personne ne recevrait jamais d'alerte,
        # et l'on ne s'en apercevrait qu'au moment d'en avoir besoin.
        raise BotError(
            f"{ENV_TELEGRAM_JETON} est défini, mais ni {ENV_CODE_ADMIN} ni "
            f"aucun opérateur inscrit. Personne ne pourrait s'inscrire ni "
            f"recevoir d'alerte. Définissez un code d'accès."
        )
    log.info("Telegram actif — %d opérateur(s) inscrit(s).", len(annuaire))
    return BotExploitation(
        ClientTelegram(jeton, http), annuaire,
        etat=None, installer_jeton=None,       # branchés juste après
    )


async def _resume(superviseur: Superviseur, etat: EtatCollecte) -> str:
    """Ce que raconte /etat. Aucune décision ici : on formate (§0)."""
    sain, details = etat.rapport()
    compteurs = details.get("compteurs") or {}
    lignes = [
        "<b>État de la collecte</b>",
        "",
        superviseur.resume(),
        "",
        f"Sonde : {'🟢' if sain else '🔴'} {details.get('status')}",
    ]
    age = details.get("age_battement_sec")
    if age is not None:
        lignes.append(f"Dernier battement : il y a {age} s")
    if compteurs:
        lignes.append(
            f"Ticks : {compteurs.get('ticks', 0):,} — "
            f"bougies : {compteurs.get('candles', 0):,}"
        )
    lignes.append(f"Démarrages du collecteur : {superviseur.demarrages}")
    lignes.append(f"Paires souscrites : {superviseur.paires_souscrites()}")
    lignes.append(f"<code>{version_deployee.resume()}</code>")
    return "\n".join(lignes)


async def _paires(superviseur: Superviseur) -> str:
    """Ce à quoi la collecte est réellement abonnée.

    Répond à la question qu'aucun compteur global ne tranche : le collecteur
    tourne peut-être, mais suit-il les bonnes paires ? Le week-end, seules les
    paires OTC cotent, et le seuil de payout peut n'en laisser aucune.
    """
    collecteur = superviseur.collecteur
    souscrites = list(getattr(collecteur, "subscribed", []) or [])
    if not souscrites:
        return ("<b>Aucune paire suivie</b>\n\n"
                "Soit la collecte n'a pas démarré, soit aucune paire n'atteint "
                "le payout minimal à cette heure. Le week-end, seules les "
                "paires OTC cotent.")
    lignes = [f"<b>{len(souscrites)} paire(s) suivie(s)</b>", ""]
    lignes += [f"• {nom}" for nom in souscrites[:30]]
    if len(souscrites) > 30:
        lignes.append(f"… et {len(souscrites) - 30} autres")
    return "\n".join(lignes)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Processus hébergé : collecteur + sonde HTTP",
    )
    ap.add_argument("--source", choices=["sim", "po"], default="sim")
    ap.add_argument("--db", default=None,
                    help="Chemin de la base. Par défaut : $TRADING_DB_PATH.")
    ap.add_argument("--min-payout", type=int,
                    default=_depuis_env("MIN_PAYOUT_PCT"),
                    help="Payout minimal. Obligatoire, via --min-payout ou "
                         "$MIN_PAYOUT_PCT : aucune valeur par défaut sur ce qui "
                         "touche à l'argent (spec §5).")
    ap.add_argument("--max-paires", type=int,
                    default=int(os.environ.get("MAX_PAIRES", "4") or 4),
                    help="Nombre maximal de paires souscrites (défaut : 8, ou "
                         "$MAX_PAIRES).")
    ap.add_argument("--duration", type=int, default=0,
                    help="Arrêt automatique après N secondes (0 = illimité)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    charger_env_local()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        # stdout et non stderr, le défaut de logging. Deux raisons : c'est la
        # convention des applications en conteneur, et sous PowerShell une
        # redirection `2>&1` sur un exécutable natif enveloppe CHAQUE ligne
        # dans un objet d'erreur, rendant le journal illisible et faisant
        # croire à une avalanche de pannes.
        stream=sys.stdout,
    )

    if args.min_payout is None:
        log.error("Payout minimal non fourni : passez --min-payout ou "
                  "définissez MIN_PAYOUT_PCT. Aucune valeur par défaut n'est "
                  "appliquée (spec §5).")
        return 2

    try:
        return asyncio.run(_servir(args))
    except BotError as erreur:
        log.error("%s", erreur)
        return 2
    except KeyboardInterrupt:
        return 0


def _depuis_env(nom: str) -> int | None:
    brut = os.environ.get(nom, "").strip()
    if not brut:
        return None
    try:
        return int(brut)
    except ValueError:
        return None


if __name__ == "__main__":
    sys.exit(main())
