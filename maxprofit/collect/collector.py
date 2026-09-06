"""
Collecteur de données de marché.

Un seul rôle : enregistrer ce qui passe, fidèlement, sans jamais analyser ni
décider. Toute logique de stratégie ici serait une erreur d'architecture : elle
figerait vos règles dans les données collectées et rendrait le backtest circulaire.

Lancement :
    python -m maxprofit.collect.collector --source sim --db market_data.db
    python -m maxprofit.collect.collector --source po --min-payout 92

Arrêt propre : Ctrl+C (les tampons sont vidés avant la sortie).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List

from maxprofit.collect.sources import (
    MarketDataSource,
    PocketOptionSource,
    SimulatedSource,
)
from maxprofit.collect.storage import Storage
from maxprofit.core.types import Tick

log = logging.getLogger("collector")

TF_SEC = 60  # bougies M1


# --------------------------------------------------------------------------- #
# Agrégation en bougies
# --------------------------------------------------------------------------- #

@dataclass
class _Building:
    ts: int
    open: float
    high: float
    low: float
    close: float
    n: int = 1


class CandleAggregator:
    """Construit les bougies M1 à partir des ticks, en mémoire.

    Une bougie n'est marquée `complete` que si on l'a observée du début à la fin
    ET qu'on était connecté sans interruption pendant toute la minute. Une bougie
    incomplète reste en base mais le backtest devra l'ignorer : mieux vaut une
    donnée étiquetée douteuse qu'une donnée manquante silencieusement.
    """

    def __init__(self):
        self._cur: Dict[str, _Building] = {}
        self._closed: List[tuple] = []

    def add(self, t: Tick) -> None:
        bucket = (t.ts_ms // 1000) // TF_SEC * TF_SEC
        b = self._cur.get(t.pair)
        if b is None:
            self._cur[t.pair] = _Building(bucket, t.price, t.price, t.price, t.price)
            return
        if bucket > b.ts:
            self._closed.append(self._row(t.pair, b, complete=True))
            self._cur[t.pair] = _Building(bucket, t.price, t.price, t.price, t.price)
            return
        if bucket < b.ts:
            return  # tick en retard sur une bougie déjà fermée : on l'ignore
        b.high = max(b.high, t.price)
        b.low = min(b.low, t.price)
        b.close = t.price
        b.n += 1

    def _row(self, pair: str, b: _Building, complete: bool) -> tuple:
        return (pair, TF_SEC, b.ts, b.open, b.high, b.low, b.close, b.n,
                1 if complete else 0)

    def drain_closed(self) -> List[tuple]:
        rows, self._closed = self._closed, []
        return rows

    def drain_all(self) -> List[tuple]:
        """Sur arrêt ou déconnexion : on écrit aussi les bougies en cours,
        marquées incomplètes."""
        rows = self.drain_closed()
        rows += [self._row(p, b, complete=False) for p, b in self._cur.items()]
        self._cur.clear()
        return rows


# --------------------------------------------------------------------------- #
# Boucle principale
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    db: str = "market_data.db"
    min_payout: int = 92
    pairs_refresh_sec: int = 300     # relevé des payouts toutes les 5 min
    flush_sec: float = 2.0           # écriture disque groupée
    heartbeat_sec: int = 10          # trace de connexion, pour repérer les trous
    max_backoff_sec: int = 60


class Collector:
    def __init__(self, source: MarketDataSource, cfg: Config):
        self.source = source
        self.cfg = cfg
        self.store = Storage(cfg.db)
        self.agg = CandleAggregator()
        self.buf: List[tuple] = []
        self.subscribed: List[str] = []
        self.running = True
        self._t_flush = self._t_pairs = self._t_beat = 0.0

    def stop(self, *_):
        log.info("Arrêt demandé, vidage des tampons...")
        self.running = False

    # --- filtrage des paires ------------------------------------------------

    def refresh_pairs(self) -> None:
        pairs = self.source.list_pairs()
        self.store.insert_payouts(int(time.time()), pairs)  # historique complet

        eligible = sorted(
            p.name for p in pairs if p.is_open and p.payout_pct >= self.cfg.min_payout
        )
        if eligible != self.subscribed:
            added = set(eligible) - set(self.subscribed)
            removed = set(self.subscribed) - set(eligible)
            self.source.subscribe(eligible)
            self.subscribed = eligible
            log.info("Paires éligibles : %d (+%d / -%d)",
                     len(eligible), len(added), len(removed))

    # --- écriture -----------------------------------------------------------

    def flush(self) -> None:
        n_t = self.store.insert_ticks(self.buf)
        n_c = self.store.upsert_candles(self.agg.drain_closed())
        self.buf.clear()
        if n_t or n_c:
            log.debug("flush: %d ticks, %d bougies", n_t, n_c)

    # --- boucle -------------------------------------------------------------

    def run(self) -> None:
        backoff = 1
        while self.running:
            try:
                self.source.connect()
                self.refresh_pairs()
                backoff = 1
                self._consume()
            except KeyboardInterrupt:
                self.stop()
            except Exception as e:
                # Déconnexion : les bougies en cours deviennent incomplètes.
                log.warning("Connexion perdue (%s). Reconnexion dans %ds", e, backoff)
                self.store.upsert_candles(self.agg.drain_all())
                self.flush()
                time.sleep(backoff)
                backoff = min(backoff * 2, self.cfg.max_backoff_sec)

        self.store.upsert_candles(self.agg.drain_all())
        self.flush()
        log.info("Totaux en base : %s", self.store.counts())
        self.store.close()
        self.source.close()

    def _consume(self) -> None:
        for tick in self.source.stream():
            if not self.running:
                return
            self.buf.append((tick.pair, tick.ts_ms, tick.price))
            self.agg.add(tick)

            now = time.time()
            if now - self._t_flush >= self.cfg.flush_sec:
                self.flush()
                self._t_flush = now
            if now - self._t_beat >= self.cfg.heartbeat_sec:
                self.store.heartbeat(len(self.subscribed))
                self._t_beat = now
            if now - self._t_pairs >= self.cfg.pairs_refresh_sec:
                self.refresh_pairs()
                self._t_pairs = now


def main() -> int:
    ap = argparse.ArgumentParser(description="Collecteur de données Pocket Option")
    ap.add_argument("--source", choices=["sim", "po"], default="sim")
    ap.add_argument("--db", default="market_data.db")
    ap.add_argument("--min-payout", type=int, default=92)
    ap.add_argument("--duration", type=int, default=0,
                    help="Arrêt automatique après N secondes (0 = illimité)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    src = SimulatedSource() if a.source == "sim" else PocketOptionSource(demo=True)
    c = Collector(src, Config(db=a.db, min_payout=a.min_payout))

    signal.signal(signal.SIGINT, c.stop)
    signal.signal(signal.SIGTERM, c.stop)

    if a.duration:
        import threading
        threading.Timer(a.duration, c.stop).start()

    c.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
