"""
Couche de stockage du collecteur.

Choix techniques :
- SQLite en mode WAL : un seul fichier, lectures possibles pendant l'écriture,
  résistant aux coupures. Suffisant jusqu'à plusieurs dizaines de millions de lignes.
- On stocke les TICKS bruts, pas seulement les bougies. Une option binaire 1 min
  se règle sur le prix exact à la seconde d'expiration : sans ticks, le backtest
  ne peut pas reproduire fidèlement le résultat d'un trade.
- On stocke aussi l'HISTORIQUE DES PAYOUTS. Sans lui, impossible de rejouer le
  filtre "seulement les paires à 92%+" tel qu'il était à l'instant T. C'est la
  source de biais la plus courante dans ce type de backtest.
- On stocke les périodes de connexion (heartbeat). Un trou dans les données doit
  être identifiable comme "bot déconnecté" et non comme "marché immobile".
"""

import sqlite3
import time
from typing import Iterable, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS ticks (
    pair    TEXT    NOT NULL,
    ts_ms   INTEGER NOT NULL,          -- horodatage SERVEUR en millisecondes
    price   REAL    NOT NULL,
    PRIMARY KEY (pair, ts_ms)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS candles (
    pair        TEXT    NOT NULL,
    tf_sec      INTEGER NOT NULL,      -- 60 pour la M1
    ts          INTEGER NOT NULL,      -- début de bougie, secondes UTC
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    tick_count  INTEGER NOT NULL,      -- < 5 ticks => bougie peu fiable
    complete    INTEGER NOT NULL,      -- 1 = minute entièrement observée
    PRIMARY KEY (pair, tf_sec, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS payouts (
    ts       INTEGER NOT NULL,         -- secondes UTC du relevé
    pair     TEXT    NOT NULL,
    payout   INTEGER NOT NULL,
    is_open  INTEGER NOT NULL,
    PRIMARY KEY (ts, pair)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS uptime (
    ts       INTEGER PRIMARY KEY,      -- battement de coeur, secondes UTC
    n_pairs  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ticks_ts   ON ticks(ts_ms);
CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles(ts);
"""


class Storage:
    def __init__(self, path: str = "market_data.db"):
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.executescript(SCHEMA)

    # --- écritures groupées -------------------------------------------------
    # INSERT OR IGNORE : les reconnexions renvoient souvent des ticks déjà reçus.
    # La clé primaire (pair, ts_ms) rend le collecteur idempotent.

    def insert_ticks(self, rows: Sequence[tuple]) -> int:
        if not rows:
            return 0
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO ticks (pair, ts_ms, price) VALUES (?,?,?)", rows
        )
        return cur.rowcount

    def upsert_candles(self, rows: Sequence[tuple]) -> int:
        if not rows:
            return 0
        cur = self.conn.executemany(
            """INSERT INTO candles (pair, tf_sec, ts, open, high, low, close, tick_count, complete)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(pair, tf_sec, ts) DO UPDATE SET
                   high=excluded.high, low=excluded.low, close=excluded.close,
                   tick_count=excluded.tick_count, complete=excluded.complete""",
            rows,
        )
        return cur.rowcount

    def insert_payouts(self, ts: int, pairs: Iterable) -> int:
        rows = [(ts, p.name, int(p.payout_pct), 1 if p.is_open else 0) for p in pairs]
        if not rows:
            return 0
        cur = self.conn.executemany(
            "INSERT OR REPLACE INTO payouts (ts, pair, payout, is_open) VALUES (?,?,?,?)",
            rows,
        )
        return cur.rowcount

    def heartbeat(self, n_pairs: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO uptime (ts, n_pairs) VALUES (?,?)",
            (int(time.time()), n_pairs),
        )

    # --- lecture ------------------------------------------------------------

    def counts(self) -> dict:
        q = lambda s: self.conn.execute(s).fetchone()[0]
        return {
            "ticks": q("SELECT COUNT(*) FROM ticks"),
            "candles": q("SELECT COUNT(*) FROM candles"),
            "payouts": q("SELECT COUNT(*) FROM payouts"),
            "heartbeats": q("SELECT COUNT(*) FROM uptime"),
        }

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
