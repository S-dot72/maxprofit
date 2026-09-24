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
from maxprofit.hosting.operateurs import Annuaire, DepotBase, chemin_annuaire
from maxprofit.hosting import version as version_deployee
from maxprofit.store import postgres
from maxprofit.collect.pocketoption import resoudre_ssid
from maxprofit.hosting.course import (
    SuperviseurCourse, course_activee, date_de_depart)
from maxprofit.hosting.superviseur import Superviseur
from maxprofit.live.plan_demo import (
    UNIVERS_PRE_INSCRIT, fabriquer_course)
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
    if postgres.configure():
        # Il n'y a pas d'emplacement à vérifier : rien n'est écrit sur le
        # disque. Sans cette sortie, le chemin symbolique « postgresql » était
        # résolu relativement au répertoire de travail et l'on avertissait que
        # la base vivait dans le code — un reproche sans objet, placé juste
        # au-dessus des lignes qu'il faut vraiment lire.
        return
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


#: Le superviseur de course, partagé avec `/etat`. Un dict plutôt qu'une
#: variable : il est rempli APRÈS la construction du bot, et le capturer par
#: fermeture avant qu'il existe donnerait un `None` figé — la ligne de la
#: course manquerait dans `/etat` sans que rien ne le signale.
_course: dict = {}

#: Les paires COLLECTÉES par défaut, quand `PAIRES_FIXES` n'est pas réglé.
#: L'univers TRADÉ par la course, lui, est `UNIVERS_PRE_INSCRIT` et ne se
#: règle pas — voir le commentaire là-bas. Ce sont
#: celles sur lesquelles l'hypothèse a été mesurée : en changer ferait tourner
#: la course sur un univers différent de celui qui a été pré-inscrit.
PAIRES_PAR_DEFAUT = ("EURUSD_otc", "AUDUSD_otc", "GBPAUD_otc", "AUDCAD_otc")


def univers_trade(cfg) -> tuple[str, ...]:
    """Les paires sur lesquelles la course prend des positions.

    Une fonction, et non une expression noyée dans l'appel à
    `fabriquer_course`, parce que cette décision s'est perdue deux fois :
    une fois en élargissant la collecte sans s'apercevoir qu'on élargissait le
    trading, une fois en figeant le trading sans s'apercevoir qu'on
    l'empêchait d'élargir. Une décision qui compte se teste.

    C'est TOUT CE QUE L'ON COLLECTE. Décidé le 2026-09-24 : quatre paires ne
    sont à 92 % que 2,5 en moyenne, et attendre la fin du test du plan pour
    élargir coûtait des semaines de débit. Le coût est consigné au registre
    sous #71 — voir aussi le commentaire à l'appel de `fabriquer_course`.
    """
    return tuple(cfg.paires_fixes or PAIRES_PAR_DEFAUT)


def _alerte_synchrone(bot):
    """Un pont thread -> boucle asyncio pour que la course puisse alerter.

    La course tourne dans un thread ; `bot.alerter` est une coroutine. Appeler
    l'une depuis l'autre sans passer par la boucle ne ferait RIEN et ne
    lèverait pas : la coroutine ne serait jamais attendue, et l'alerte
    disparaîtrait en silence — exactement le mode de panne que ce projet
    passe son temps à éliminer.
    """
    boucle = asyncio.get_running_loop()

    def alerter(message: str) -> None:
        asyncio.run_coroutine_threadsafe(bot.alerter(message), boucle)

    return alerter


async def _servir(args) -> int:
    cfg = build_config(args)
    _verifier_emplacement_base(cfg.db)
    # En premier, avant tout le reste : c'est la ligne qui dit si le journal
    # qu'on est en train de lire correspond au code qu'on vient de corriger.
    log.info("%s", version_deployee.resume())
    log.info("Base : %s", cfg.db)
    log.info("Paires souscrites au maximum : %d", cfg.max_paires)
    if cfg.paires_fixes:
        log.info("Paires épinglées (séries continues) : %s",
                 ", ".join(cfg.paires_fixes))
    if not postgres.configure():
        log.info("Synchronisation Turso : toutes les %d s", cfg.sync_sec)
    log.info("Ticks bruts : %s", "enregistrés" if cfg.stocker_ticks
             else "NON enregistrés (bougies M1 seules)")

    etat_collecte = EtatCollecte(cfg.db)

    async with aiohttp.ClientSession() as http:
        bot = _fabriquer_bot(http)
        superviseur = Superviseur(
            lambda: _fabriquer_source(args.source), cfg,
            alerter=(bot.alerter if bot else None),
        )
        if bot is not None:
            bot._etat = lambda: _resume(superviseur, etat_collecte,
                                        _course.get("sup"))
            bot._installer_jeton = superviseur.installer_jeton
            bot._paires = lambda: _paires(superviseur)
            bot._diagnostic = lambda: _diagnostic(superviseur)

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

        # La course du plan, dans un thread À PART et INACTIVE par défaut.
        #
        # Elle partage ce processus faute d'instance séparée, qui serait
        # payante. Tout le confinement est dans `SuperviseurCourse` : aucune
        # exception n'en sort, elle renonce après cinq échecs plutôt que de
        # marteler le broker, et son thread est `daemon`. La collecte vaut
        # plus que la course — quatorze jours de série continue ne se
        # rattrapent pas, dix jours de course si.
        course = SuperviseurCourse(
            lambda alerter: fabriquer_course(
                resoudre_ssid(demo=True),
                campagne=os.environ.get("PLAN_CAMPAGNE", "plan-demo-v1"),
                capital=float(os.environ.get("PLAN_CAPITAL", "250")),
                sessions_par_jour=int(os.environ.get("PLAN_SESSIONS", "18")),
                jours=int(os.environ.get("PLAN_JOURS", "30")),
                # ⚠ ON TRADE TOUT CE QUE L'ON COLLECTE. C'EST UNE DÉCISION,
                # PRISE LE 2026-09-24, ET ELLE A UN COÛT.
                #
                # Cette ligne a passé `UNIVERS_PRE_INSCRIT` pendant une
                # journée, pour que l'univers tradé reste exactement celui
                # déclaré au registre (#58, #59) — condition d'une validation
                # hors échantillon qui vaille quelque chose. L'utilisateur a
                # tranché autrement : quatre paires ne sont à 92 % que 2,5 en
                # moyenne, et attendre la fin du test pour élargir coûtait des
                # semaines de débit.
                #
                # Ce que cela coûte exactement, pour que personne ne le
                # découvre en relisant les résultats : le test au niveau du
                # PLAN — enchaînement des sessions, choix du pas de martingale,
                # séquence des gains — tourne désormais sur un univers de six
                # paires là où cinq en étaient déclarées zéro. Cette partie
                # n'est plus une validation hors échantillon de la
                # configuration déclarée.
                #
                # Ce qui SURVIT, et c'est ce qui justifie de ne pas tout
                # jeter : le journal enregistre la paire de chaque ordre. La
                # précision PAR PAIRE des quatre pré-inscrites reste donc
                # mesurable, non biaisée, et c'est elle que #58/#59 prédisent.
                # `UNIVERS_PRE_INSCRIT` reste en dur pour cette analyse.
                # Consigné au registre sous #71.
                paires=univers_trade(cfg),
                # Les paires dont NOTRE BASE a l'historique. Elles sont lues
                # localement au lieu d'être demandées au broker pour 27
                # secondes chacune — c'est ce qui rend l'élargissement de la
                # collecte utile au direct, sans toucher à l'univers tradé.
                paires_collectees=cfg.paires_fixes or PAIRES_PAR_DEFAUT,
                mode_univers=os.environ.get(
                    "PLAN_UNIVERS", "epinglees").strip() or "epinglees",
                alerter=alerter),
            alerter=(lambda m: None) if bot is None else _alerte_synchrone(bot),
            # Le départ est une DATE, pas un geste. Faire dépendre le
            # lancement d'une bascule manuelle le bon jour, c'est le manquer.
            debut_ts_sec=date_de_depart(),
        )
        _course["sup"] = course
        if course_activee():
            course.demarrer()
        else:
            log.info("Course du plan : INACTIVE (PLAN_DEMO=0). Le code est "
                     "déployé et éprouvé, aucun ordre ne part.")
        if course.en_attente():
            log.info("Course du plan ARMÉE : départ programmé, %s",
                     course.resume())

        # Le superviseur commande : quand il rend la main, le processus s'arrête.
        # Le bot n'est qu'un canal ; le laisser maintenir le processus en vie
        # donnerait une instance verte côté hébergeur qui n'enregistre plus rien.
        try:
            await taches[0]
        except Exception:
            log.exception("Le collecteur s'est arrêté sur une exception")
        finally:
            # La course d'abord : son thread est `daemon` et ne retarderait
            # rien, mais lui demander de s'arrêter lui laisse le temps de
            # sauver son état après le pas en cours.
            course.arreter()
            for tache in taches[1:]:
                tache.cancel()
            await asyncio.gather(*taches[1:], return_exceptions=True)
            await runner.cleanup()

    log.info("Processus terminé.")
    return 0


def _annuaire() -> Annuaire:
    """L'annuaire en base quand il y en a une, dans un fichier sinon.

    Le fichier vit sur le disque du conteneur, effacé à chaque déploiement : le
    journal affichait « 0 opérateur(s) inscrit(s) » après chaque mise à jour, et
    les alertes n'avaient plus de destinataire. On se croyait couvert.

    Le bot ouvre sa PROPRE connexion, distincte de celle du collecteur : un
    objet de connexion n'est pas partageable entre threads, et le bot tourne
    dans la boucle asyncio pendant que le collecteur écrit dans le sien.
    """
    if not postgres.configure():
        return Annuaire(chemin_annuaire())
    try:
        return Annuaire(depot=DepotBase(postgres.ouvrir()))
    except Exception as erreur:                          # noqa: BLE001
        # Un annuaire injoignable ne doit pas empêcher la collecte de démarrer :
        # elle, elle n'a besoin de personne. On perd la persistance des
        # inscriptions, et on le dit.
        log.error("Annuaire en base indisponible (%s) : repli sur le fichier, "
                  "les inscriptions ne survivront pas au déploiement.", erreur)
        return Annuaire(chemin_annuaire())


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

    annuaire = _annuaire()
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


async def _resume(superviseur: Superviseur, etat: EtatCollecte,
                  course=None) -> str:
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
        lignes.append(f"Bougies : {compteurs.get('candles', 0):,}")
        # La sous-minute a sa propre ligne, et elle dit ce qui est RÉGLÉ autant
        # que ce qui est écrit.
        #
        # Elle existe à cause d'un écart qui s'est produit : `STOCKER_TICKS=1`
        # poussé dans `render.yaml`, et la variable du tableau de bord — restée
        # à "0" — l'emporte, parce qu'un blueprint ne se resynchronise pas sur
        # un simple `git push`. Le collecteur tournait, les bougies arrivaient,
        # tout était vert, et pas un tick n'était enregistré. Un réglage qu'on
        # croit actif est pire qu'un réglage absent.
        minutes = compteurs.get("tick_paths", 0)
        if superviseur.cfg.stocker_ticks:
            lignes.append(
                f"Sous-minute : {minutes:,} minute(s) de ticks"
                + ("" if minutes else
                   " — ⚠ activé mais rien d'écrit pour l'instant"))
        else:
            lignes.append(
                "Sous-minute : ⚠ DÉSACTIVÉE (STOCKER_TICKS=0) — bougies M1 "
                "seules, aucune analyse sous la minute possible")
    couv = details.get("couverture")
    if couv and couv.get("fenetre_sec"):
        # Les 24 dernières heures D'ABORD : c'est la seule qui réagit à ce
        # qu'on vient de corriger. Le cumul est tiré vers le bas par des pannes
        # anciennes et ne remonte plus, quoi qu'on fasse.
        lignes.append(
            f"Couverture 24 h : {100 * couv['part']:.1f} % "
            f"({couv['interruptions']} interruption(s))")
        # Immédiatement parlant, là où la moyenne sur 24 h met 24 h à oublier
        # un trou. Sans lui, on lit « 66 % » pendant une journée entière alors
        # que tout va bien depuis une heure.
        if couv.get("en_cours"):
            lignes.append(
                f"Sans interruption depuis : "
                f"{_duree_h(couv.get('continue_depuis_sec', 0))}")
    tot = details.get("couverture_totale")
    if tot and tot.get("fenetre_sec"):
        lignes.append(
            f"Depuis le début : {100 * tot['part']:.1f} % "
            f"({tot['collecte_sec'] / 3600:.0f} h sur "
            f"{tot['fenetre_sec'] / 3600:.0f} h)")
    lignes.append(f"Démarrages du collecteur : {superviseur.demarrages}")
    lignes.append(
        f"Paires souscrites : {superviseur.paires_souscrites_nommees()}")
    if course is not None:
        lignes.append(f"Course du plan : {course.resume()}")
    lignes.append(f"<code>{version_deployee.resume()}</code>")
    return "\n".join(lignes)


def _duree_h(secondes: float) -> str:
    heures = secondes / 3600
    if heures < 1:
        return f"{secondes / 60:.0f} min"
    return f"{heures:.1f} h"


def _oui_non(valeur) -> str:
    """Le champ qui manquait le plus.

    Le catalogue des actifs est public : il arrive même sans compte valide, et
    c'est ce qui rendait la panne invisible. Le solde, lui, n'arrive qu'après
    authentification.
    """
    if valeur is None:
        return "inconnu"
    return "oui" if valeur else "NON — aucun tick ne sera diffusé"


def _verdict_point_d_acces(etat: dict) -> str:
    """Dire si la substitution a pris, plutôt que de laisser deviner.

    Un point d'accès demandé mais non appliqué se lisait exactement comme un
    point d'accès appliqué : `/diag` affichait le nom demandé et l'URL réelle,
    et il fallait connaître la table des adresses par cœur pour voir qu'elles
    ne concordaient pas.
    """
    demandee, reelle = etat.get("url_demandee"), etat.get("url")
    if not demandee:
        return "(point d'accès par défaut)"
    if not reelle:
        return "⏳ Connexion pas encore établie."
    if reelle == demandee:
        return "✅ Substitution appliquée."
    return (f"❌ <b>Substitution NON appliquée</b> — demandé "
            f"<code>{demandee}</code>. L'expérience n'a pas eu lieu.")


async def _diagnostic(superviseur: Superviseur) -> str:
    """Ce que la bibliothèque du broker a reçu, sans interprétation.

    Quand la collecte est connectée, abonnée et muette, une seule question
    tranche : le tampon de la bibliothèque se remplit-il ? S'il se remplit, la
    panne est chez nous, dans le drainage — et ça se répare. S'il reste vide, le
    broker n'envoie rien à cette adresse, et aucune correction de notre côté n'y
    changera quoi que ce soit.

    Cette commande existe parce qu'on a passé plusieurs jours à supposer, faute
    de pouvoir regarder à l'intérieur d'un processus qui tourne ailleurs.
    """
    source = getattr(superviseur.collecteur, "source", None)
    mesurer = getattr(source, "diagnostic", None)
    if mesurer is None:
        return ("<b>Diagnostic indisponible</b>\n\n"
                "La source active ne sait pas se mesurer (source simulée ?).")

    etat = mesurer()
    lignes = [
        "<b>Intérieur du client du broker</b>",
        "",
        f"Socket connecté : {etat.get('connecte')}",
        f"Compte authentifié : {_oui_non(etat.get('authentifie'))}",
        f"Point d'accès demandé : {etat.get('region')}",
        f"Réellement connecté à :\n<code>{etat.get('url') or 'url inconnue'}</code>",
        _verdict_point_d_acces(etat),
        f"Actifs connus de la bibliothèque : {etat.get('cles_bibliotheque')}",
        f"Paires souscrites : {len(etat.get('souscrites') or ())}",
        "",
    ]
    tampons = etat.get("tampons") or {}
    if not tampons:
        lignes.append("Aucun tampon : rien n'est souscrit.")
        return "\n".join(lignes)

    total = 0
    for nom, mesure in tampons.items():
        recus, lus = mesure["ticks"], mesure["lus"]
        total += recus
        lignes.append(f"<code>{nom}</code> — reçus {recus}, lus {lus}, "
                      f"historique {mesure['history']}")

    lignes.append("")
    if total == 0:
        lignes.append(
            "⚠️ <b>Zéro tick reçu par la bibliothèque elle-même.</b>\n"
            "Le problème est en amont de notre code : le broker accepte la "
            "connexion et l'abonnement, mais ne diffuse rien vers cette "
            "adresse."
        )
    else:
        lignes.append(
            "✅ La bibliothèque reçoit des ticks. S'ils n'arrivent pas en base, "
            "la panne est dans notre drainage — donc réparable."
        )
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
    # AVANT de construire les arguments, pas après. Plusieurs défauts sont lus
    # dans l'environnement au moment où `add_argument` s'exécute ; les charger
    # ensuite revenait à ignorer le `.env` en silence. Sur un hébergeur les
    # variables sont déjà dans l'environnement, donc rien ne se voyait — c'est
    # exactement le genre de bug qui n'apparaît que sur le poste de quelqu'un.
    charger_env_local()

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
    ap.add_argument("--paires", default="",
                    help="Paires à suivre en permanence, séparées par des "
                         "virgules (ou $PAIRES_FIXES).")
    ap.add_argument("--sans-ticks", action="store_true",
                    default=os.environ.get("STOCKER_TICKS", "1").strip() == "0",
                    help="N'écrit pas les ticks bruts (97,6 %% du volume). "
                         "Ou $STOCKER_TICKS=0.")
    ap.add_argument("--sync-sec", type=int,
                    default=int(os.environ.get("TURSO_SYNC_SEC", "0") or 0),
                    help="Intervalle de synchronisation vers Turso, en "
                         "secondes (défaut : 300, ou $TURSO_SYNC_SEC).")
    ap.add_argument("--duration", type=int, default=0,
                    help="Arrêt automatique après N secondes (0 = illimité)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

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
