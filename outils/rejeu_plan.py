#!/usr/bin/env python
r"""
Rejoue le PLAN sur les bougies collectées, pour plusieurs échéances.

    .venv313\Scripts\python.exe outils\rejeu_plan.py
    .venv313\Scripts\python.exe outils\rejeu_plan.py --echeances 60,120,180,300,900 --entrees 2,3

Répond à une question précise : combien de sessions tiennent dans 12 h, et à
quel taux de réussite, selon l'échéance des options ? La stratégie (ZoneH1,
zone vierge, garde de calibration), le filtre de payout maximum, la règle
d'indépendance, l'attente maximale et les trois pas de martingale sont ceux
du direct.

⚠ DEUX PÉRIODES, ET SEULE LA SECONDE COMPTE. Les données sont coupées en une
période d'ÉTALONNAGE (la plus ancienne) et une période de VALIDATION (les
`--validation-pct` % les plus récents). Choisir un réglage parce qu'il brille
sur l'étalonnage, puis le garder seulement s'il tient sur la validation : sinon
on choisit le hasard qui a le mieux réussi. Seuil de rentabilité à 92 % de
payout : 52,08 %.

Lit la base de `DATABASE_URL` (ou la base locale), en lecture seule.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxprofit.core.config import charger_env_local                # noqa: E402
from maxprofit.live.plan_demo import (                             # noqa: E402
    ATTENTE_MAX_PAS_SEC, DELAI_INDEPENDANCE_SEC, CoursePlanDemo)
from maxprofit.research.rejeu import (                             # noqa: E402
    FENETRE_SEC, IndexBougies, IndexPayouts, generer_signaux, rejouer)
from maxprofit.store.db import chemin_donnees, open_read_only      # noqa: E402
from maxprofit.store.market import MarketReader                    # noqa: E402
from maxprofit.strategies.zone_h1 import (                         # noqa: E402
    PARAMETRES_PRE_INSCRITS, ZoneH1)

SEUIL = 100 / 192


def _date(texte: str) -> int:
    return int(datetime.strptime(texte, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


def _liste(texte: str, conv=str) -> list:
    return [conv(x.strip()) for x in texte.split(",") if x.strip()]


def _paires_disponibles(conn, minimum: int) -> list[str]:
    lignes = conn.execute(
        "SELECT pair, COUNT(*) FROM candles WHERE tf_sec = 60 "
        "GROUP BY pair ORDER BY pair").fetchall()
    return [p for p, n in lignes if n >= minimum]


def _payouts(conn, paires, debut, fin):
    marques = ",".join("?" * len(paires))
    return conn.execute(
        f"SELECT ts_sec, pair, payout_pct, is_open FROM payouts "
        f"WHERE ts_sec BETWEEN ? AND ? AND pair IN ({marques})",
        (debut - 86400, fin, *paires)).fetchall()


def _fenetres_couvertes(bougies_par_paire, debut, fin, seuil=0.9) -> set[int]:
    """Les fenêtres de 12 h où la collecte a vu au moins 90 % des minutes :
    une fenêtre sans collecte ne produit aucune session, et la compter
    ferait passer une panne pour une limite de la stratégie."""
    minutes: set[int] = set()
    for serie in bougies_par_paire.values():
        minutes.update(b.ts_sec for b in serie)
    comptes: dict[int, int] = {}
    for m in minutes:
        k = (m - debut) // FENETRE_SEC
        comptes[k] = comptes.get(k, 0) + 1
    n = (fin - debut) // FENETRE_SEC
    return {k for k in range(n) if comptes.get(k, 0) >= seuil * FENETRE_SEC / 60}


def _pct(x) -> str:
    return "   —  " if x is None else f"{100 * x:5.1f}%"


def _ligne(nom, r, debut, fin, valides) -> str:
    fen = r.par_fenetre(debut, fin, valides)
    perdues = sum(i == "perdue" for _, i in r.sessions)
    closes = sum(i != "interrompue" for _, i in r.sessions)
    if fen:
        debit = (f"{statistics.mean(fen):5.1f} {statistics.median(fen):5.1f} "
                 f"{max(fen):4d} {100 * sum(c >= 18 for c in fen) / len(fen):5.0f}%")
    else:
        debit = "   —     —     —      — "
    return (f"{nom:<22} {len(r.trades):6d} {_pct(r.precision())} "
            f"{_pct(r.precision(1))} {_pct(r.precision(2))} "
            f"{_pct(r.precision(3))}  {debit} {len(fen):4d}  "
            f"{_pct(perdues / closes if closes else None)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--debut", help="AAAA-MM-JJ (défaut : première bougie)")
    ap.add_argument("--fin", help="AAAA-MM-JJ (défaut : dernière bougie)")
    ap.add_argument("--paires", default="",
                    help="Paires séparées par des virgules (défaut : toutes "
                         "celles qui ont assez de bougies)")
    ap.add_argument("--min-bougies", type=int, default=2000)
    ap.add_argument("--echeances", default="60,120,180,300,600,900",
                    help="Échéances en secondes, multiples de 60")
    ap.add_argument("--entrees", default=str(
        PARAMETRES_PRE_INSCRITS.entrees_max_par_zone),
        help="Valeurs de entrees_max_par_zone à comparer")
    ap.add_argument("--independance", type=int, default=DELAI_INDEPENDANCE_SEC)
    ap.add_argument("--validation-pct", type=float, default=30.0)
    args = ap.parse_args()

    charger_env_local()
    echeances = _liste(args.echeances, int)
    if any(e % 60 for e in echeances):
        ap.error("les échéances doivent être des multiples de 60 s")

    conn = open_read_only(chemin_donnees())
    lecteur = MarketReader(conn)
    paires = (_liste(args.paires) if args.paires
              else _paires_disponibles(conn, args.min_bougies))
    if not paires:
        print("Aucune paire n'a assez de bougies.")
        return 1
    bornes = conn.execute(
        "SELECT MIN(ts_sec), MAX(ts_sec) FROM candles WHERE tf_sec = 60"
    ).fetchone()
    debut = _date(args.debut) if args.debut else int(bornes[0])
    fin = _date(args.fin) if args.fin else int(bornes[1]) + 60
    print(f"Paires ({len(paires)}) : {', '.join(paires)}")
    print(f"Période : {datetime.fromtimestamp(debut, timezone.utc):%Y-%m-%d %H:%M}"
          f" -> {datetime.fromtimestamp(fin, timezone.utc):%Y-%m-%d %H:%M} UTC")

    t0 = time.monotonic()
    bougies = {p: lecteur.candles(p, 60, debut, fin) for p in paires}
    payouts = IndexPayouts(_payouts(conn, paires, debut, fin))
    index = IndexBougies(bougies)
    print(f"Chargé en {time.monotonic() - t0:.0f} s : "
          f"{sum(map(len, bougies.values()))} bougies.")

    coupure = fin - int((fin - debut) * args.validation_pct / 100)
    coupure = debut + (coupure - debut) // FENETRE_SEC * FENETRE_SEC
    valides = _fenetres_couvertes(bougies, debut, fin)
    parts = {
        "ÉTALONNAGE": (debut, coupure),
        "VALIDATION": (coupure, fin),
    }

    for entrees in _liste(args.entrees, int):
        strategie = ZoneH1(replace(PARAMETRES_PRE_INSCRITS,
                                   entrees_max_par_zone=entrees))
        garde = SimpleNamespace(strategie=strategie)

        def calibration(pair, fenetre, _g=garde):
            return CoursePlanDemo._dans_sa_plage_de_calibration(
                _g, pair, fenetre)

        t0 = time.monotonic()
        signaux = generer_signaux(
            bougies, strategie, calibration,
            progression=lambda p: print(f"  stratégie sur {p}…", end="\r"))
        print(f"\nentrees_max_par_zone={entrees} : {len(signaux)} signaux en "
              f"{time.monotonic() - t0:.0f} s")

        for nom, (a, b) in parts.items():
            sous = [s for s in signaux if a <= s.fin_sec < b]
            decale = {k - (a - debut) // FENETRE_SEC for k in valides
                      if a <= debut + k * FENETRE_SEC < b}
            print(f"\n  {nom} — {len(sous)} signaux, {len(decale)} fenêtres "
                  f"de 12 h couvertes")
            print(f"  {'échéance':<22} {'ordres':>6} {'préc.':>6} "
                  f"{'pas 1':>6} {'pas 2':>6} {'pas 3':>6}  "
                  f"{'moy':>5} {'méd':>5} {'max':>4} {'≥18':>6} "
                  f"{'fen.':>4}  {'perdues':>6}")
            for echeance in echeances:
                r = rejouer(sous, index, payouts, echeance_sec=echeance,
                            independance_sec=args.independance,
                            attente_max_sec=ATTENTE_MAX_PAS_SEC)
                repere = "  <- actuel" if (
                    echeance == PARAMETRES_PRE_INSCRITS.expiry_sec
                    and entrees == PARAMETRES_PRE_INSCRITS
                    .entrees_max_par_zone) else ""
                print("  " + _ligne(f"{echeance // 60} min", r, a, b, decale)
                      + repere)
    print(f"\nSessions : ouvertes par fenêtre de 12 h (moyenne, médiane, "
          f"maximum, part des fenêtres à 18 ou plus). Seuil de rentabilité "
          f"par ordre : {100 * SEUIL:.2f} %. Un réglage ne vaut que s'il tient "
          f"sur la VALIDATION.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
