"""
Rapport de qualité des données.

À lancer régulièrement pendant la collecte. Une base qui grossit ne veut pas dire
une base exploitable : ce qui compte est le nombre de bougies COMPLÈTES, en
continu, sur des paires qui étaient réellement éligibles à ce moment-là.

    python -m maxprofit.collect.inspect_data --db market_data.db
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

from maxprofit.core.config import db_path
from maxprofit.store.db import open_read_only


def fmt(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None,
                    help="Par défaut : $TRADING_DB_PATH.")
    ap.add_argument("--min-payout", type=int, required=True)
    ap.add_argument("--gap-sec", type=int, default=60,
                    help="Trou de connexion signalé au-delà de N secondes")
    a = ap.parse_args()

    # Lecture SEULE : un outil d'inspection ne doit pas pouvoir modifier
    # le schéma ni les données sous les pieds du collecteur qui tourne.
    c = open_read_only(Path(a.db) if a.db else db_path())

    row = c.execute("SELECT MIN(ts_sec), MAX(ts_sec), COUNT(*) FROM candles").fetchone()
    if not row or row[0] is None:
        print("Base vide.")
        return
    t0, t1, n = row
    span_min = (t1 - t0) // 60 + 1
    print(f"Période   : {fmt(t0)} → {fmt(t1)} UTC  ({span_min/60/24:.1f} jours)")
    print(f"Bougies   : {n:,}   Ticks : "
          f"{c.execute('SELECT COUNT(*) FROM ticks').fetchone()[0]:,}")

    # --- trous de connexion --------------------------------------------------
    beats = [r[0] for r in c.execute("SELECT ts_sec FROM uptime ORDER BY ts_sec")]
    gaps = [(beats[i], beats[i + 1]) for i in range(len(beats) - 1)
            if beats[i + 1] - beats[i] > a.gap_sec]
    lost = sum(b - x for x, b in gaps)
    print(f"\nTrous     : {len(gaps)}  ({lost/60:.0f} min perdues, "
          f"{100*lost/max(1,(t1-t0)):.1f}% du temps)")
    for x, b in gaps[:5]:
        print(f"            {fmt(x)} → {fmt(b)}  ({(b-x)/60:.0f} min)")
    if len(gaps) > 5:
        print(f"            ... et {len(gaps)-5} autres")

    # --- qualité par paire ---------------------------------------------------
    print(f"\n{'Paire':<18}{'Bougies':>9}{'Complètes':>11}{'Ticks/bougie':>14}"
          f"{'Éligible':>10}")
    print("-" * 62)

    rows = c.execute("""
        SELECT pair, COUNT(*), SUM(complete), AVG(tick_count)
        FROM candles GROUP BY pair ORDER BY COUNT(*) DESC
    """).fetchall()

    for pair, total, complete, avg_ticks in rows:
        elig = c.execute(
            "SELECT AVG(payout_pct >= ? AND is_open) FROM payouts WHERE pair = ?",
            (a.min_payout, pair),
        ).fetchone()[0] or 0
        print(f"{pair:<18}{total:>9,}{100*(complete or 0)/total:>10.0f}%"
              f"{avg_ticks:>14.1f}{100*elig:>9.0f}%")

    # --- verdict -------------------------------------------------------------
    usable = c.execute(
        "SELECT COUNT(*) FROM candles WHERE complete = 1 AND tick_count >= 5"
    ).fetchone()[0]
    print(f"\nBougies exploitables (complètes, >=5 ticks) : {usable:,}")
    print("Repère : ~5 000 bougies M1 par paire donnent quelques centaines de")
    print("signaux au mieux. En dessous de ~400 trades simulés, un taux de")
    print("réussite mesuré ne permet pas de distinguer 52% de 58%.")

    c.close()


if __name__ == "__main__":
    main()
