#!/usr/bin/env python
r"""
Classe les paires par temps passé au payout MAXIMUM, pour choisir quoi collecter.

    .venv313\Scripts\python.exe outils\classer_paires.py
    .venv313\Scripts\python.exe outils\classer_paires.py --jours 14 --limite 40

La course n'entre que lorsque le broker paie son maximum : une paire qui y est
rarement ne rapportera presque rien, quelle que soit sa qualité. Les relevés de
payouts couvrent TOUTES les paires, collectées ou non, donc ce classement est
possible avant d'en ajouter une.

Pour les paires déjà collectées, l'outil donne aussi le rapport
tolérance/amplitude de la stratégie : il prédit le débit de signaux, et la
course écarte d'elle-même une paire hors de [0,05 ; 4,0].
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxprofit.core.config import charger_env_local                # noqa: E402
from maxprofit.core.payout import FLUX_POUR_LE_PLAFOND             # noqa: E402
from maxprofit.live.plan_demo import (                             # noqa: E402
    RAPPORT_TOLERANCE_MAX, RAPPORT_TOLERANCE_MIN)
from maxprofit.store.db import chemin_donnees, open_read_only      # noqa: E402
from maxprofit.strategies.zone_h1 import PARAMETRES_PRE_INSCRITS   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--jours", type=float, default=7.0)
    ap.add_argument("--limite", type=int, default=30)
    args = ap.parse_args()
    charger_env_local()

    conn = open_read_only(chemin_donnees())
    depuis = int(time.time() - args.jours * 86400)
    lignes = conn.execute(
        "SELECT pair, COUNT(*), SUM(CASE WHEN is_open = 1 AND payout_pct >= ? "
        "THEN 1 ELSE 0 END) FROM payouts WHERE ts_sec >= ? GROUP BY pair",
        (FLUX_POUR_LE_PLAFOND, depuis)).fetchall()
    amplitudes = {
        pair: med for pair, med in _amplitudes_medianes(conn, depuis).items()}

    classees = sorted(((int(n_max or 0) / n, pair, n) for pair, n, n_max
                       in lignes if n), reverse=True)
    print(f"Sur {args.jours:g} jours — part des relevés au plafond "
          f"(flux ≥ {FLUX_POUR_LE_PLAFOND} %)\n")
    print(f"{'paire':<16} {'au plafond':>10} {'relevés':>8}  "
          f"{'tolérance/amplitude':>20}")
    tol = PARAMETRES_PRE_INSCRITS.tolerance_pct
    for part, pair, n in classees[:args.limite]:
        amp = amplitudes.get(pair)
        if amp:
            rapport = tol / amp
            dans = RAPPORT_TOLERANCE_MIN <= rapport <= RAPPORT_TOLERANCE_MAX
            info = f"{rapport:6.2f} {'ok' if dans else 'HORS PLAGE'}"
        else:
            info = "non collectée"
        print(f"{pair:<16} {100 * part:9.1f}% {n:8d}  {info:>20}")
    return 0


def _amplitudes_medianes(conn, depuis: int) -> dict[str, float]:
    """Amplitude médiane d'une bougie, en % du prix, par paire collectée."""
    par_paire: dict[str, list[float]] = {}
    for pair, haut, bas, close in conn.execute(
            "SELECT pair, high, low, close FROM candles "
            "WHERE tf_sec = 60 AND ts_sec >= ?", (depuis,)).fetchall():
        if close:
            par_paire.setdefault(pair, []).append(
                100.0 * (haut - bas) / close)
    return {p: sorted(v)[len(v) // 2] for p, v in par_paire.items() if v}


if __name__ == "__main__":
    sys.exit(main())
