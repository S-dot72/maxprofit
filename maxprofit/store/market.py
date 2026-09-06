"""
Accès aux tables de marché : une porte en écriture, une porte en lecture.

`MarketWriter` est réservé à la couche Collecte. `MarketReader` s'ouvre sur un
descripteur en lecture seule et sert le backtest et les outils d'inspection.
Les deux classes existent séparément pour que la frontière du §0 soit visible
dans les imports : un fichier de `maxprofit/backtest/` qui importerait
`MarketWriter` se remarque à la relecture, et `tests/test_layering.py` le refuse.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Sequence

from maxprofit.core.errors import BotError
from maxprofit.core.types import Candle, PairInfo, Tick


class MarketWriter:
    """Écriture des tables de marché. Couche Collecte uniquement.

    Toutes les écritures sont idempotentes : une reconnexion renvoie
    systématiquement des ticks déjà reçus, et la clé primaire `(pair, ts_ms)`
    suffit à les absorber sans doublon ni perte.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- écritures groupées --------------------------------------------------

    def insert_ticks(self, ticks: Sequence[Tick]) -> int:
        if not ticks:
            return 0
        rows = [(t.pair, t.ts_ms, t.price) for t in ticks]
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO ticks (pair, ts_ms, price) VALUES (?,?,?)", rows
        )
        return cur.rowcount

    def upsert_candles(self, candles: Sequence[Candle]) -> int:
        """Une bougie en cours est réécrite à chaque flush jusqu'à sa clôture.

        `complete` ne peut que passer de 0 à 1, jamais l'inverse : une bougie
        observée entièrement puis réécrite par un tick tardif resterait
        complète. C'est pourquoi la clause DO UPDATE utilise `MAX` sur ce champ
        plutôt qu'une affectation directe.
        """
        if not candles:
            return 0
        rows = [
            (c.pair, c.tf_sec, c.ts_sec, c.open, c.high, c.low, c.close,
             c.tick_count, 1 if c.complete else 0)
            for c in candles
        ]
        cur = self.conn.executemany(
            """INSERT INTO candles
                   (pair, tf_sec, ts_sec, open, high, low, close, tick_count, complete)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(pair, tf_sec, ts_sec) DO UPDATE SET
                   high       = MAX(candles.high, excluded.high),
                   low        = MIN(candles.low,  excluded.low),
                   close      = excluded.close,
                   tick_count = MAX(candles.tick_count, excluded.tick_count),
                   complete   = MAX(candles.complete,   excluded.complete)""",
            rows,
        )
        return cur.rowcount

    def insert_payouts(self, ts_sec: int, pairs: Iterable[PairInfo]) -> int:
        """Relevé horodaté de TOUTES les paires, ouvertes ou non (§2.3)."""
        rows = [(ts_sec, p.name, p.payout_pct, 1 if p.is_open else 0) for p in pairs]
        if not rows:
            return 0
        cur = self.conn.executemany(
            "INSERT OR REPLACE INTO payouts (ts_sec, pair, payout_pct, is_open) "
            "VALUES (?,?,?,?)",
            rows,
        )
        return cur.rowcount

    def heartbeat(self, ts_sec: int, n_pairs: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO uptime (ts_sec, n_pairs) VALUES (?,?)",
            (ts_sec, n_pairs),
        )

    def counts(self) -> dict[str, int]:
        return _counts(self.conn)

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


class MarketReader:
    """Lecture des tables de marché. Le descripteur est en `mode=ro` : une
    écriture lève, elle ne corrompt pas."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def counts(self) -> dict[str, int]:
        return _counts(self.conn)

    def payout_at(self, pair: str, ts_sec: int) -> PairInfo | None:
        """Payout EN VIGUEUR à `ts_sec` : le relevé antérieur le plus proche.

        C'est la règle du §2.3, et elle est ici plutôt que dans le moteur de
        backtest pour qu'il n'y ait pas deux façons de la calculer. Retourne
        `None` si aucun relevé n'est antérieur — auquel cas le trade ne doit pas
        être généré du tout, jamais estimé avec le payout d'aujourd'hui.
        """
        row = self.conn.execute(
            """SELECT pair, payout_pct, is_open FROM payouts
               WHERE pair = ? AND ts_sec <= ?
               ORDER BY ts_sec DESC LIMIT 1""",
            (pair, ts_sec),
        ).fetchone()
        if row is None:
            return None
        return PairInfo(row["pair"], bool(row["is_open"]), int(row["payout_pct"]))

    def candles(self, pair: str, tf_sec: int, start_sec: int, end_sec: int) -> list[Candle]:
        """Bougies de `[start_sec, end_sec)`, ordonnées. Les bougies non
        exploitables ne sont PAS filtrées : c'est au moteur d'appliquer le §2.4
        et de compter les exclusions, un filtrage ici rendrait le taux
        d'exclusion inobservable."""
        if start_sec > end_sec:
            raise BotError(f"Fenêtre inversée : {start_sec} > {end_sec}")
        rows = self.conn.execute(
            """SELECT * FROM candles
               WHERE pair = ? AND tf_sec = ? AND ts_sec >= ? AND ts_sec < ?
               ORDER BY ts_sec""",
            (pair, tf_sec, start_sec, end_sec),
        ).fetchall()
        return [
            Candle(
                pair=r["pair"], tf_sec=r["tf_sec"], ts_sec=r["ts_sec"],
                open=r["open"], high=r["high"], low=r["low"], close=r["close"],
                tick_count=r["tick_count"], complete=bool(r["complete"]),
            )
            for r in rows
        ]

    def last_candle_ts_sec(self) -> int | None:
        row = self.conn.execute("SELECT MAX(ts_sec) AS m FROM candles").fetchone()
        return None if row["m"] is None else int(row["m"])

    def last_heartbeat_sec(self) -> int | None:
        row = self.conn.execute("SELECT MAX(ts_sec) AS m FROM uptime").fetchone()
        return None if row["m"] is None else int(row["m"])

    def close(self) -> None:
        self.conn.close()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        for table in ("ticks", "candles", "payouts", "uptime")
    }
