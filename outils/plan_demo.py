#!/usr/bin/env python
"""
Joue le plan en DÉMO sur les signaux de l'hypothèse pré-inscrite.

    .venv313\\Scripts\\python.exe outils\\plan_demo.py --apercu
    .venv313\\Scripts\\python.exe outils\\plan_demo.py

`--apercu` ne place AUCUN ordre : il affiche le plan, ses chiffres et le débit
de signaux attendu. À lancer d'abord.

--- (!) Ce que cette course prouve, et ce qu'elle ne prouve pas -------------

Elle ne peut pas établir que l'hypothèse gagne : il faudrait 2 071 signaux
pour trancher 57,9 % à 3 sigma, et dix jours en produiront ~180. Elle établit
que la chaîne tient — signal, dimensionnement, ordre, dénouement, solde — et
que les gardes se déclenchent quand il faut.

Le diagnostic qui arrive AVANT la fin : si le réancrage (deux sessions
perdues d'affilée) se déclenche dans les trois premiers jours, l'hypothèse est
probablement du bruit. À 57,9 % il n'arrive que tous les dix jours environ.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxprofit.collect.pocketoption import resoudre_ssid           # noqa: E402
from maxprofit.core.config import charger_env_local                # noqa: E402
from maxprofit.execution.courtier import CourtierDemo              # noqa: E402
from maxprofit.execution.garde import CompteRefuse, Plafonds       # noqa: E402
from maxprofit.execution.journal import JournalExecution           # noqa: E402
from maxprofit.live.plan_demo import (                             # noqa: E402
    CoursePlanDemo, charger_etat, nouveau_jour, sauver_etat)
from maxprofit.plan import PlanCapital, Risque, projeter           # noqa: E402
from maxprofit.store.db import open_read_only, open_read_write     # noqa: E402
from maxprofit.store.market import MarketReader                    # noqa: E402
from maxprofit.strategies.zone_h1 import ZoneH1                    # noqa: E402

PAIRES = ("EURUSD_otc", "AUDUSD_otc", "GBPAUD_otc", "AUDCAD_otc")
#: Débit mesuré de l'hypothèse : 317 signaux en 12 jours sur 4 paires.
SIGNAUX_PAR_JOUR = 317 / 12


def construire(capital: float, sessions: int, jours: int) -> PlanCapital:
    return PlanCapital.depuis_risque(
        capital_initial=capital, risque=Risque(1, 7), payout_pct=92,
        sessions_par_jour=sessions, jours=jours,
        # DEUX et non le défaut 3 : c'est la règle fixée. Deux sessions
        # perdues d'affilée ferment la journée ET déclenchent le réancrage.
        sessions_perdues_max=2)


def apercu(plan: PlanCapital) -> None:
    lignes = projeter(plan)
    print("=" * 74)
    print("LE PLAN, TEL QU'IL EST RÉGLÉ")
    print("=" * 74)
    print(f"  capital           {plan.capital_initial:.2f} $")
    print(f"  risque            1/7")
    print(f"  gain par session  {plan.gain_par_session_pct:.3f} %")
    print(f"  sessions par jour {plan.sessions_par_jour}")
    print(f"  ratio journalier  {plan.ratio_journalier_pct():.2f} %")
    print(f"  jour {plan.jours:<13} {lignes[-1].solde:.2f} $ visés")
    print()
    print("--- le débit de signaux, et ce qu'il autorise ---")
    # Une session consomme 1 à 3 signaux : 1 + q + q² avec q = 1 - précision.
    q = 1 - 0.579
    par_session = 1 + q + q * q
    plafond = SIGNAUX_PAR_JOUR / par_session
    print(f"  signaux mesurés          {SIGNAUX_PAR_JOUR:.1f} / jour")
    print(f"  signaux par session      {par_session:.2f} en moyenne")
    print(f"  sessions atteignables    {plafond:.1f} / jour")
    if plan.sessions_par_jour > plafond:
        print(f"  (!) {plan.sessions_par_jour} sessions/jour demandées : le débit "
              f"ne suit pas.\n    Les journées se termineront sur "
              f"SESSIONS_EPUISEES faute de signaux,\n    pas faute d'avoir "
              f"atteint l'objectif.")
    print()
    print("--- ce qu'une journée risque ---")
    exposition = plan.capital_initial * plan.gain_par_session_pct / 100
    from maxprofit.plan import Echelle
    e = Echelle(payout_pct=92, gain_vise=exposition)
    print(f"  exposition d'une session {e.exposition():.2f} $ "
          f"({e.part_du_capital(plan.capital_initial):.2f} % du capital)")
    print(f"  si toutes les sessions du jour étaient perdues : "
          f"{100 * e.exposition() * plan.sessions_par_jour / plan.capital_initial:.0f} %")
    print(f"  seuil de rentabilité     {e.seuil_de_rentabilite_pct():.2f} %")
    print()
    print("--- le diagnostic qui arrive avant la fin ---")
    for nom, precision in (("hypothèse vraie", 0.579), ("bruit", 0.500)):
        perte = (1 - precision) ** 3
        sessions_avant = 1 / (perte * perte) if perte else float("inf")
        jours_avant = sessions_avant / max(1, plan.sessions_par_jour)
        print(f"  {nom:<16} session perdue {100*perte:.1f} %  ->  "
              f"deux d'affilée après {jours_avant:.1f} jour(s)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capital", type=float, default=250.0)
    ap.add_argument("--sessions", type=int, default=18)
    ap.add_argument("--jours", type=int, default=30)
    ap.add_argument("--paires", default=",".join(PAIRES))
    ap.add_argument("--journal", type=Path, default=Path("execution.db"))
    ap.add_argument("--campagne", default="plan-demo-v1")
    ap.add_argument("--apercu", action="store_true",
                    help="N'ouvre rien et ne place rien : affiche le plan.")
    ap.add_argument("--pause", type=float, default=20.0,
                    help="Secondes entre deux inspections du flux.")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    plan = construire(a.capital, a.sessions, a.jours)
    apercu(plan)
    if a.apercu:
        return 0

    charger_env_local()
    ssid = resoudre_ssid(demo=True)
    if not ssid:
        print("Aucun jeton de session démo.")
        return 1

    paires = tuple(p.strip() for p in a.paires.split(",") if p.strip())
    # Le plafond de mise est celui du 3e pas, majoré : au-delà, quelque chose
    # a dérapé dans le dimensionnement et il vaut mieux refuser.
    from maxprofit.plan import Echelle
    pire = Echelle(payout_pct=92,
                   gain_vise=a.capital * plan.gain_par_session_pct / 100).mises()[-1]
    plafonds = Plafonds(mise=round(pire * 1.5, 2), ordres_max=2000,
                        duree_max_sec=11 * 86400)

    journal_log = logging.getLogger("plan_demo")
    # Deux connexions, et c'est voulu. La lecture du marché passe par un
    # descripteur en LECTURE SEULE : la course ne doit pas pouvoir écrire dans
    # les tables de marché, même par accident. Son propre journal passe par
    # l'autre.
    lecteur = MarketReader(open_read_only(Path("lecture")))
    ecriture = open_read_write(Path("ecriture"))
    try:
        with JournalExecution(ecriture, campagne=a.campagne) as journal, \
                CourtierDemo(ssid, plafonds) as courtier:
            course = CoursePlanDemo(lecteur, courtier, journal, plan,
                                    paires, ZoneH1())
            repris = charger_etat(ecriture, a.campagne, plan)
            if repris is not None:
                course.etat, jour_courant = repris
                journal_log.info("Course REPRISE : %s", course.resume())
            else:
                jour_courant = int(time.time()) // 86400
                journal_log.info("Nouvelle course : %s", course.resume())
            print("\nCourse démarrée. Ctrl+C pour arrêter.\n")
            while course.etat.jour <= plan.jours:
                if int(time.time()) // 86400 != jour_courant:
                    jour_courant = int(time.time()) // 86400
                    nouveau_jour(course.etat)
                    journal_log.info("=== %s", course.resume())
                    sauver_etat(ecriture, a.campagne, course.etat, jour_courant)
                if course.tour():
                    # Après CHAQUE pas, et pas de temps en temps : le
                    # processus peut mourir à n'importe quel moment, et un
                    # état sauvé par intermittence rejouerait des ordres déjà
                    # passés ou en oublierait.
                    sauver_etat(ecriture, a.campagne, course.etat, jour_courant)
                else:
                    time.sleep(a.pause)
    except CompteRefuse as refus:
        print(f"\nREFUSÉ — aucun ordre n'est parti.\n{refus}")
        return 2
    except KeyboardInterrupt:
        print("\nArrêt demandé.")
    finally:
        lecteur.close()
        ecriture.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
