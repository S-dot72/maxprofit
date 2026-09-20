#!/usr/bin/env python
"""
Mesure l'exécution réelle sur compte DÉMO — phase 17 du protocole.

    .venv313\\Scripts\\python.exe outils\\mesurer_execution.py --ordres 50
    .venv313\\Scripts\\python.exe outils\\mesurer_execution.py --rapport

Le premier appel place des ordres ALÉATOIRES et journalise tout. Le second ne
touche à rien : il relit le journal et rend les cinq constats.

--- ⚠ Avant de lancer ------------------------------------------------------

Il faut un jeton de session DÉMO en local :

    .venv313\\Scripts\\python.exe outils\\capturer_ssid.py

Le jeton de production, sur Render, sert à la COLLECTE. Un second client sur
le même compte ne gêne pas, mais mieux vaut savoir lequel fait quoi.

Rien ne part si le jeton ne porte pas « isDemo: 1 ». Un champ absent refuse au
même titre qu'un champ à zéro.

--- Ce que ça coûte, et ce que ça ne prouve pas ---------------------------

Cinquante ordres à 1 $ engagent 50 $ de monnaie de démonstration. L'espérance
est NÉGATIVE par construction : des entrées aléatoires gagnent 50 % du temps
contre un seuil de 52,08 %. Voir le solde descendre est le comportement
attendu — ce n'est pas la mesure qui échoue.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxprofit.core.config import charger_env_local            # noqa: E402
from maxprofit.core.errors import BotError                     # noqa: E402
from maxprofit.collect.pocketoption import resoudre_ssid       # noqa: E402
from maxprofit.execution.courtier import CourtierDemo          # noqa: E402
from maxprofit.execution.garde import CompteRefuse, Plafonds   # noqa: E402
from maxprofit.execution.journal import JournalExecution       # noqa: E402
from maxprofit.execution.mesure import rapport                 # noqa: E402
from maxprofit.execution.sonde import (                        # noqa: E402
    Campagne, mesurer, sigma_sur_horizon)
from maxprofit.store.db import open_read_only                  # noqa: E402
from maxprofit.store.market import MarketReader                # noqa: E402

PAIRES = ("EURUSD_otc", "AUDUSD_otc", "GBPAUD_otc", "AUDCAD_otc")
JOURNAL = Path("execution.db")


def _echelle(expiration_sec: int) -> float:
    """L'écart-type du mouvement de prix sur l'échéance, depuis les données.

    Sans lui, un glissement se lit « 0,000012 » et ne veut rien dire. Avec lui,
    il se lit en points de taux de réussite et se compare aux 2,08 points qui
    séparent le hasard du seuil de rentabilité.
    """
    charger_env_local()
    lecteur = MarketReader(open_read_only(Path("lecture")))
    try:
        fin = lecteur.last_candle_ts_sec()
        if fin is None:
            raise BotError("Aucune bougie en base : pas d'échelle possible.")
        bougies = lecteur.candles("EURUSD_otc", 60, fin - 7 * 86400, fin + 60)
        closes = [b.close for b in bougies if b.complete]
        pas = max(1, expiration_sec // 60)
        return sigma_sur_horizon(closes, pas)
    finally:
        lecteur.close()


def _afficher(executions, sigma: float, expiration_sec: int) -> None:
    print()
    print("=" * 78)
    print("MESURE D'EXÉCUTION — ce que le backtest ne pouvait pas savoir")
    print("=" * 78)
    print(f"{len(executions)} ordre(s) journalisé(s). "
          f"Échelle du mouvement sur l'échéance : {sigma:.6f}")
    print(f"Le seuil à battre est 52,08 % : un coût de plus de 2,08 points "
          f"rend\ntout avantage inatteignable, quelle que soit la stratégie.\n")
    for constat in rapport(executions, sigma, expiration_sec):
        print(constat.resume())
        print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ordres", type=int, default=50,
                    help="Nombre d'ordres aléatoires à placer (défaut 50).")
    ap.add_argument("--mise", type=float, default=1.0,
                    help="Mise par ordre, en $ de démo (défaut 1).")
    ap.add_argument("--expiration", type=int, default=60,
                    help="Échéance en secondes (défaut 60).")
    ap.add_argument("--pause", type=float, default=5.0,
                    help="Pause entre deux ordres (défaut 5 s). Deux ordres "
                         "collés mesurent la file d'attente du broker.")
    ap.add_argument("--paires", default=",".join(PAIRES))
    ap.add_argument("--journal", type=Path, default=JOURNAL)
    ap.add_argument("--campagne", default="execution-v1")
    ap.add_argument("--rapport", action="store_true",
                    help="Ne place AUCUN ordre : relit le journal et conclut.")
    a = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with JournalExecution(a.journal, campagne=a.campagne) as journal:
        if a.rapport:
            executions = journal.toutes()
            if not executions:
                print("Journal vide : lancez d'abord une session de mesure.")
                return 1
            _afficher(executions, _echelle(a.expiration), a.expiration)
            return 0

        charger_env_local()
        ssid = resoudre_ssid(demo=True)
        if not ssid:
            print("Aucun jeton de session. Capturez-en un sur le compte "
                  "DÉMO :\n    .venv313\\Scripts\\python.exe "
                  "outils\\capturer_ssid.py")
            return 1

        campagne = Campagne(
            paires=tuple(p.strip() for p in a.paires.split(",") if p.strip()),
            expiration_sec=a.expiration, ordres=a.ordres, pause_sec=a.pause)
        plafonds = Plafonds(mise=a.mise, ordres_max=a.ordres)
        print(campagne.resume())
        print(plafonds.resume())
        print("Espérance NÉGATIVE par construction : des entrées aléatoires "
              "gagnent\n50 % du temps contre un seuil de 52,08 %. "
              "C'est l'exécution qu'on mesure,\npas un avantage.\n")

        try:
            with CourtierDemo(ssid, plafonds) as courtier:
                mesurer(courtier, journal, campagne)
        except CompteRefuse as refus:
            print(f"\nREFUSÉ — aucun ordre n'est parti.\n{refus}")
            return 2

        _afficher(journal.toutes(), _echelle(a.expiration), a.expiration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
