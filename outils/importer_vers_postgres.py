#!/usr/bin/env python
r"""
Reprendre une base SQLite dans PostgreSQL.

    .venv313\Scripts\python.exe outils\importer_vers_postgres.py FICHIER.db

Sert une fois, au changement de stockage. Trois choix méritent d'être dits.

**Les payouts sont compressés en points de changement.** 95,1 % des lignes
répétaient la précédente. `payout_at` prend « le relevé antérieur le plus
proche » : une valeur inchangée est déjà représentée par son dernier point de
changement, donc la compression ne perd rien de ce que le §2.3 exige. Elle
divise le volume par vingt.

**Les ticks sont facultatifs.** Ils représentent 97,6 % du volume écrit en
régime normal et le backtest du §2 travaille sur les bougies M1, dont le
`tick_count` est déjà calculé. `--avec-ticks` les reprend quand même.

**Rien n'est effacé.** L'import est idempotent : les écritures sont en
`ON CONFLICT`, donc relancer la commande après une coupure reprend sans
doublon ni perte.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _interpreteur import exiger  # noqa: E402

exiger("psycopg", "outils\importer_vers_postgres.py")

from maxprofit.core.config import charger_env_local          # noqa: E402
from maxprofit.core.types import Candle, PairInfo, Tick      # noqa: E402
from maxprofit.store.db import open_read_write               # noqa: E402
from maxprofit.store.market import (                        # noqa: E402
    MarketWriter,
    _inserer_en_lot,
)

LOT = 5_000


def _lots(curseur, taille=LOT):
    while True:
        lignes = curseur.fetchmany(taille)
        if not lignes:
            return
        yield lignes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Importe une base SQLite dans PostgreSQL.")
    ap.add_argument("source", help="Fichier SQLite à reprendre")
    ap.add_argument("--avec-ticks", action="store_true",
                    help="Reprend aussi les ticks bruts (97,6 %% du volume).")
    a = ap.parse_args(argv)
    charger_env_local()

    source = Path(a.source)
    if not source.is_file():
        print(f"Introuvable : {source}", file=sys.stderr)
        return 2

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dest = open_read_write("postgres")
    writer = MarketWriter(dest)

    # --- payouts : uniquement les points de changement ----------------------
    dernier: dict[str, tuple[int, int]] = {}
    retenus = lus = 0
    cur = src.execute(
        "SELECT ts_sec, pair, payout_pct, is_open FROM payouts ORDER BY ts_sec")
    for lignes in _lots(cur):
        par_instant: dict[int, list[PairInfo]] = {}
        for ts, pair, payout, ouvert in lignes:
            lus += 1
            etat = (int(payout), int(ouvert))
            if dernier.get(pair) == etat:
                continue
            dernier[pair] = etat
            par_instant.setdefault(int(ts), []).append(
                PairInfo(str(pair), bool(ouvert), int(payout)))
        for ts, paires in sorted(par_instant.items()):
            # Le writer déduplique aussi ; on lui présente déjà des changements
            # pour ne pas dépendre de l'ordre dans lequel il amorce son cache.
            writer._dernier_payout.pop(paires[0].name, None)
            retenus += _ecrire_payouts(writer, ts, paires)
        dest.commit()
    print(f"payouts  : {retenus:>7} retenus sur {lus} lus "
          f"({100 * (1 - retenus / max(lus, 1)):.1f} % de redondance)")

    # --- bougies ------------------------------------------------------------
    n = 0
    cur = src.execute(
        """SELECT pair, tf_sec, ts_sec, open, high, low, close, tick_count,
                  complete FROM candles ORDER BY ts_sec""")
    for lignes in _lots(cur):
        n += writer.upsert_candles([
            Candle(pair=r[0], tf_sec=r[1], ts_sec=r[2], open=r[3], high=r[4],
                   low=r[5], close=r[6], tick_count=r[7], complete=bool(r[8]))
            for r in lignes
        ])
        dest.commit()
    print(f"bougies  : {n:>7}")

    # --- battements ---------------------------------------------------------
    # En lot, pas un par un : `heartbeat()` fait un aller-retour reseau par
    # ligne, ce qui convient a un battement toutes les trente secondes mais
    # demande douze minutes pour reprendre 4 848 lignes d'historique.
    n = 0
    cur = src.execute("SELECT ts_sec, n_pairs FROM uptime ORDER BY ts_sec")
    for lignes in _lots(cur):
        n += _inserer_en_lot(
            dest, "INSERT INTO uptime (ts_sec, n_pairs)",
            "ON CONFLICT (ts_sec) DO UPDATE SET n_pairs = excluded.n_pairs",
            [(int(ts), int(paires)) for ts, paires in lignes])
        dest.commit()
    print(f"battements: {n:>6}")

    # --- ticks --------------------------------------------------------------
    if a.avec_ticks:
        n = 0
        cur = src.execute("SELECT pair, ts_ms, price FROM ticks ORDER BY ts_ms")
        for lignes in _lots(cur):
            n += writer.insert_ticks(
                [Tick(pair=r[0], ts_ms=r[1], price=r[2]) for r in lignes])
            dest.commit()
        print(f"ticks    : {n:>7}")
    else:
        total = src.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
        print(f"ticks    : {total:>7} laissés de côté (--avec-ticks pour les reprendre)")

    dest.commit()
    print()
    print("compteurs en base :", writer.counts())
    dest.close()
    src.close()
    return 0


def _ecrire_payouts(writer, ts_sec, paires) -> int:
    return writer.insert_payouts(ts_sec, paires)


if __name__ == "__main__":
    sys.exit(main())
