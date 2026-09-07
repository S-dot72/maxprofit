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


#: Nombre maximal de parametres lies par instruction. SQLite en accepte
#: bien plus depuis la 3.32, mais rester sous l'ancienne limite de 999 evite
#: d'avoir a interroger le moteur, et decouper en deux requetes au lieu d'une
#: ne coute rien face a ce qu'on economise.
MAX_PARAMS = 900


def _en_lots(rows: Sequence[tuple], par_ligne: int) -> Iterable[Sequence[tuple]]:
    taille = max(1, MAX_PARAMS // par_ligne)
    for debut in range(0, len(rows), taille):
        yield rows[debut:debut + taille]


def _inserer_en_lot(conn, avant: str, apres: str, rows: Sequence[tuple]) -> int:
    """Une seule instruction par lot, au lieu d'une par ligne.

    `executemany` de `libsql` boucle en Python : chaque ligne est un
    aller-retour reseau vers Turso. Mesure en production, le 7 septembre : 183
    payouts ecrits en 29 secondes, soit 158 ms par ligne -- exactement une
    latence reseau. Pendant ces 29 secondes le collecteur ne drainait aucun
    tick, et l'operation se repete toutes les cinq minutes.

    Sur un fichier SQLite local la difference est negligeable ; sur une base
    distante elle decide si la collecte est possible ou non.
    """
    if not rows:
        return 0
    par_ligne = len(rows[0])
    marque = "(" + ",".join("?" * par_ligne) + ")"
    total = 0
    for lot in _en_lots(rows, par_ligne):
        sql = f"{avant} VALUES {','.join([marque] * len(lot))} {apres}".strip()
        params = [valeur for ligne in lot for valeur in ligne]
        cur = conn.execute(sql, params)
        # `rowcount` vaut -1 sur certains pilotes pour une insertion multiple.
        # Le nombre de lignes soumises est alors la meilleure reponse : il sert
        # a journaliser, jamais a decider.
        total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(lot)
    return total


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
        return _inserer_en_lot(
            self.conn, "INSERT OR IGNORE INTO ticks (pair, ts_ms, price)", "", rows)

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
        return _inserer_en_lot(
            self.conn,
            """INSERT INTO candles
                   (pair, tf_sec, ts_sec, open, high, low, close, tick_count,
                    complete)""",
            """ON CONFLICT(pair, tf_sec, ts_sec) DO UPDATE SET
                   high       = MAX(candles.high, excluded.high),
                   low        = MIN(candles.low,  excluded.low),
                   close      = excluded.close,
                   tick_count = MAX(candles.tick_count, excluded.tick_count),
                   complete   = MAX(candles.complete,   excluded.complete)""",
            rows,
        )

    def insert_payouts(self, ts_sec: int, pairs: Iterable[PairInfo]) -> int:
        """Relevé horodaté de TOUTES les paires, ouvertes ou non (§2.3)."""
        rows = [(ts_sec, p.name, p.payout_pct, 1 if p.is_open else 0) for p in pairs]
        if not rows:
            return 0
        return _inserer_en_lot(
            self.conn,
            "INSERT OR REPLACE INTO payouts (ts_sec, pair, payout_pct, is_open)",
            "", rows)

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
        # Accès POSITIONNEL, et colonnes énumérées dans le SELECT. Le pilote
        # libSQL ne garantit pas l'accès par nom que donne `sqlite3.Row`, et un
        # `SELECT *` rendrait la position dépendante de l'ordre des colonnes
        # dans le schéma — donc d'une future migration.
        return PairInfo(row[0], bool(row[2]), int(row[1]))

    def candles(self, pair: str, tf_sec: int, start_sec: int, end_sec: int) -> list[Candle]:
        """Bougies de `[start_sec, end_sec)`, ordonnées. Les bougies non
        exploitables ne sont PAS filtrées : c'est au moteur d'appliquer le §2.4
        et de compter les exclusions, un filtrage ici rendrait le taux
        d'exclusion inobservable."""
        if start_sec > end_sec:
            raise BotError(f"Fenêtre inversée : {start_sec} > {end_sec}")
        rows = self.conn.execute(
            """SELECT pair, tf_sec, ts_sec, open, high, low, close,
                      tick_count, complete
               FROM candles
               WHERE pair = ? AND tf_sec = ? AND ts_sec >= ? AND ts_sec < ?
               ORDER BY ts_sec""",
            (pair, tf_sec, start_sec, end_sec),
        ).fetchall()
        return [
            Candle(
                pair=r[0], tf_sec=r[1], ts_sec=r[2],
                open=r[3], high=r[4], low=r[5], close=r[6],
                tick_count=r[7], complete=bool(r[8]),
            )
            for r in rows
        ]

    def last_candle_ts_sec(self) -> int | None:
        row = self.conn.execute("SELECT MAX(ts_sec) FROM candles").fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def last_heartbeat_sec(self) -> int | None:
        row = self.conn.execute("SELECT MAX(ts_sec) FROM uptime").fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def close(self) -> None:
        self.conn.close()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("ticks", "candles", "payouts", "uptime")
    }
