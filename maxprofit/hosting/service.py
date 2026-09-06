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
import threading
from pathlib import Path

from maxprofit.collect.collector import Collector, build_config
from maxprofit.core.config import charger_env_local
from maxprofit.collect.sources import PocketOptionSource, SimulatedSource
from maxprofit.core.errors import BotError
from maxprofit.hosting.health import start_http_server

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
    log.info("Base : %s", cfg.db)

    runner = await start_http_server(cfg.db)

    source = _fabriquer_source(args.source)
    collecteur = Collector(source, cfg)

    fin = asyncio.Event()
    boucle = asyncio.get_running_loop()

    def _tourner():
        try:
            collecteur.run()
        except Exception:
            log.exception("Le collecteur s'est arrêté sur une exception")
        finally:
            # Réveille le thread principal : le processus doit mourir avec le
            # collecteur. Rester en vie avec un serveur HTTP seul donnerait une
            # instance « verte » côté hébergeur qui n'enregistre plus rien.
            boucle.call_soon_threadsafe(fin.set)

    thread = threading.Thread(target=_tourner, name="collecteur", daemon=True)
    thread.start()

    def _arreter(*_):
        log.info("Signal reçu, arrêt du collecteur...")
        collecteur.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            boucle.add_signal_handler(sig, _arreter)
        except NotImplementedError:
            # Windows : add_signal_handler n'existe pas sur la boucle Proactor.
            signal.signal(sig, lambda *_: _arreter())

    if args.duration:
        boucle.call_later(args.duration, _arreter)

    await fin.wait()
    thread.join(timeout=30)
    await runner.cleanup()
    log.info("Processus terminé.")
    return 0


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
    ap.add_argument("--duration", type=int, default=0,
                    help="Arrêt automatique après N secondes (0 = illimité)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    charger_env_local()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
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
