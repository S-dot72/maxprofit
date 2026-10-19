#!/usr/bin/env python
r"""
Recopie les hypothèses pré-inscrites (`maxprofit/live/preinscriptions.py`)
dans le registre local des expériences, `research.db`.

    .venv313\Scripts\python.exe outils\preinscrire.py --campagnes
    .venv313\Scripts\python.exe outils\preinscrire.py --campagne otc-m1-2026-09
    .venv313\Scripts\python.exe outils\preinscrire.py --campagne otc-m1-2026-09 ^
        --conclure P4 --signaux 280 --gains 151

Idempotent : une hypothèse déjà inscrite dans la campagne (même nom) n'est
pas inscrite deux fois. Choisir la campagne qui tient déjà ZoneH1 (#58, #59)
garde le compte des expériences vrai : c'est sur lui que porte la correction
du nombre de tests.

`--conclure` attache le résultat de la fenêtre aveugle, lu dans /protocole,
une fois la fenêtre close. Le registre refuse de conclure deux fois.

Le registre est `RESEARCH_DB_PATH`, sinon `research.db` à côté de la base de
marché locale.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxprofit.core.config import (                                # noqa: E402
    charger_env_local, research_db_path)
from maxprofit.core.errors import BotError                         # noqa: E402
from maxprofit.live.preinscriptions import (                       # noqa: E402
    DECLAREES_LE, DECLAREES_R8, DECLAREES_T, FENETRE_JOURS_R8,
    FENETRE_JOURS_T, PREINSCRIPTIONS, PREINSCRIPTIONS_R8, PREINSCRIPTIONS_T,
    fenetre_pre_inscrite, ouverture_r8, ouverture_t)
from maxprofit.research.metriques import Resultat                  # noqa: E402
from maxprofit.research.registre import Registre                   # noqa: E402


def inscrire(registre: Registre) -> list[tuple[str, int, bool]]:
    """(ident, id du registre, nouvelle ?) pour chaque pré-inscription.
    Celles de la mission Reference 8 et des entrées T0 à T3 gardent LEUR
    fenêtre et leur date."""
    deja = {e.nom: e.id for e in registre.toutes()}
    sortie = []
    debut_r8 = ouverture_r8()
    lots = [(p, *fenetre_pre_inscrite(), DECLAREES_LE)
            for p in PREINSCRIPTIONS]
    lots += [(p, debut_r8, debut_r8 + FENETRE_JOURS_R8 * 86400,
              DECLAREES_R8) for p in PREINSCRIPTIONS_R8]
    debut_t = ouverture_t()
    lots += [(p, debut_t, debut_t + FENETRE_JOURS_T * 86400, DECLAREES_T)
             for p in PREINSCRIPTIONS_T]
    for p, debut, fin, declaree in lots:
        if p.nom in deja:
            sortie.append((p.ident, deja[p.nom], False))
            continue
        ident = registre.pre_enregistrer(
            famille=p.famille, nom=p.nom, hypothese=p.hypothese,
            univers=p.univers, echeance_sec=p.echeance_sec,
            periode_debut_sec=debut, periode_fin_sec=fin, sur_scelle=True,
            parametres={**dict(p.parametres), "ident": p.ident,
                        "critere": p.critere, "n_min": p.n_min,
                        "declaree_le": declaree})
        sortie.append((p.ident, ident, True))
    return sortie


def campagnes(chemin: Path) -> list[tuple[str, int]]:
    if not chemin.exists():
        return []
    conn = sqlite3.connect(str(chemin))
    try:
        return conn.execute(
            "SELECT campagne, COUNT(*) FROM experiences GROUP BY campagne "
            "ORDER BY campagne").fetchall()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--campagne", help="Campagne du registre où inscrire")
    ap.add_argument("--campagnes", action="store_true",
                    help="Lister les campagnes du registre et quitter")
    ap.add_argument("--conclure", help="Identifiant à conclure (P2-E, P4…)")
    ap.add_argument("--signaux", type=int)
    ap.add_argument("--gains", type=int)
    ap.add_argument("--payout", type=float, default=92.0)
    args = ap.parse_args()

    charger_env_local()
    chemin = research_db_path()
    if args.campagnes:
        lignes = campagnes(chemin)
        if not lignes:
            print(f"Registre vide ou absent : {chemin}")
        for nom, n in lignes:
            print(f"{nom} : {n} expérience(s)")
        return 0
    if not args.campagne:
        ap.error("--campagne est obligatoire (voir --campagnes)")

    with Registre(chemin, args.campagne) as registre:
        resultats = inscrire(registre)
        for ident, num, nouvelle in resultats:
            etat = "inscrite" if nouvelle else "déjà inscrite"
            print(f"{ident} : #{num} {etat}")
        if args.conclure:
            if args.signaux is None or args.gains is None:
                ap.error("--conclure demande --signaux et --gains")
            numeros = {ident: num for ident, num, _ in resultats}
            if args.conclure not in numeros:
                ap.error(f"{args.conclure} n'est pas pré-inscrite : "
                         f"{', '.join(numeros)}")
            registre.conclure(numeros[args.conclure], Resultat(
                signaux=args.signaux, gains=args.gains,
                payout_moyen_pct=args.payout),
                notes="fenêtre aveugle, résultat lu dans /protocole")
            print(f"{args.conclure} conclue : {args.gains}/{args.signaux}")
        compte = registre.compte()
        print(f"Campagne {args.campagne} : {compte['declarees']} déclarées, "
              f"{compte['conclues']} conclues, {compte['sur_scelle']} sur "
              f"période scellée — registre {chemin}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BotError as erreur:
        # Le registre refuse ce qui fausserait le compte (conclure deux
        # fois, une hypothèse vide) : le dire, sans trace de pile.
        print(f"Refusé : {erreur}")
        sys.exit(1)
