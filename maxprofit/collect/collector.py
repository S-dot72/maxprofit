"""
Collecteur de données de marché.

Un seul rôle : enregistrer ce qui passe, fidèlement, sans jamais analyser ni
décider. Toute logique de stratégie ici serait une erreur d'architecture : elle
figerait vos règles dans les données collectées et rendrait le backtest
circulaire.

    export TRADING_DB_PATH=/home/vous/trading_data/market.db
    python -m maxprofit.collect.collector --source sim --min-payout 92

Arrêt propre : Ctrl+C (les tampons sont vidés avant la sortie).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from maxprofit.core.config import backups_dir, charger_env_local
from maxprofit.store.db import chemin_donnees, valider
from maxprofit.core.errors import BotError
from maxprofit.core.timebase import bucket_of_ms
from maxprofit.core.types import Candle, Tick
from maxprofit.collect.pocketoption import SessionExpiree, SourceIndisponible
from maxprofit.collect.sources import (
    MarketDataSource,
    PocketOptionSource,
    SimulatedSource,
)
from maxprofit.store import turso
from maxprofit.store.backup import sauvegarder_et_purger
from maxprofit.store.db import open_read_write
from maxprofit.store.market import MarketWriter

log = logging.getLogger("collector")

TF_SEC = 60  # bougies M1


# --------------------------------------------------------------------------- #
# Agrégation en bougies
# --------------------------------------------------------------------------- #

@dataclass
class _EnCours:
    ts_sec: int
    open: float
    high: float
    low: float
    close: float
    n: int = 1


class CandleAggregator:
    """Construit les bougies M1 à partir des ticks, en mémoire.

    Une bougie n'est marquée `complete` que si on l'a observée du début à la fin
    ET qu'on était connecté sans interruption pendant toute la minute. Une
    bougie incomplète reste en base mais le backtest devra l'ignorer : mieux
    vaut une donnée étiquetée douteuse qu'une donnée manquante silencieusement.
    """

    def __init__(self, tf_sec: int = TF_SEC):
        self.tf_sec = tf_sec
        self._cur: Dict[str, _EnCours] = {}
        self._closed: List[Candle] = []

    def add(self, tick: Tick) -> None:
        bucket = bucket_of_ms(tick.ts_ms, self.tf_sec)
        b = self._cur.get(tick.pair)
        if b is None:
            self._cur[tick.pair] = _EnCours(bucket, tick.price, tick.price,
                                            tick.price, tick.price)
            return
        if bucket > b.ts_sec:
            self._closed.append(self._candle(tick.pair, b, complete=True))
            self._cur[tick.pair] = _EnCours(bucket, tick.price, tick.price,
                                            tick.price, tick.price)
            return
        if bucket < b.ts_sec:
            return  # tick en retard sur une bougie déjà fermée : ignoré
        b.high = max(b.high, tick.price)
        b.low = min(b.low, tick.price)
        b.close = tick.price
        b.n += 1

    def _candle(self, pair: str, b: _EnCours, *, complete: bool) -> Candle:
        return Candle(
            pair=pair, tf_sec=self.tf_sec, ts_sec=b.ts_sec,
            open=b.open, high=b.high, low=b.low, close=b.close,
            tick_count=b.n, complete=complete,
        )

    def drain_closed(self) -> List[Candle]:
        rows, self._closed = self._closed, []
        return rows

    def drain_all(self) -> List[Candle]:
        """Sur arrêt ou déconnexion : on écrit aussi les bougies en cours,
        marquées incomplètes."""
        rows = self.drain_closed()
        rows += [self._candle(p, b, complete=False) for p, b in self._cur.items()]
        self._cur.clear()
        return rows


# --------------------------------------------------------------------------- #
# Boucle principale
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Config:
    """Aucune valeur par défaut sur `db` ni `min_payout` (spec §5) : ils
    touchent aux données et à l'argent, donc ils sont fournis explicitement ou
    le collecteur ne démarre pas. Les cadences ci-dessous sont opérationnelles
    et n'influencent aucun résultat, elles peuvent avoir un défaut."""

    db: Path
    min_payout: int
    pairs_refresh_sec: int = 300      # relevé des payouts toutes les 5 min
    flush_sec: float = 2.0            # écriture disque groupée
    heartbeat_sec: int = 10           # trace de connexion, pour repérer les trous
    backup_sec: int = 6 * 3600        # sauvegarde toutes les 6 h (§1.4)
    #: Synchronisation vers Turso. Sans effet en stockage local. C'est la
    #: fenêtre de perte maximale si le conteneur est tué brutalement : une
    #: minute de ticks, qui laissera un trou dans `uptime` et sera donc écartée
    #: par le backtest (§2.4) plutôt que raisonnée dessus.
    sync_sec: int = 60
    max_backoff_sec: int = 60
    #: Nombre maximal de paires SOUSCRITES simultanément.
    #:
    #: Mesuré, pas supposé. Le diagnostic a tourné 90 s sans faute sur 4 paires.
    #: En production, un abonnement à 32 paires d'un coup a fait fermer le socket
    #: par le broker au bout de 20 s, sans qu'un seul tick n'arrive — deux heures
    #: de collecte pour zéro ligne.
    #:
    #: Ne limite QUE l'abonnement. L'historique complet des payouts continue
    #: d'être enregistré pour toutes les paires (§2.3), donc le backtest peut
    #: toujours rejouer l'éligibilité telle qu'elle était.
    max_paires: int = 8

    def __post_init__(self) -> None:
        if not (0 <= self.min_payout <= 100):
            raise BotError(f"min_payout hors [0,100] : {self.min_payout}")


#: Exceptions qui ne sont PAS des pertes de connexion et qu'il ne sert à rien
#: de réessayer. Sans cette distinction, une erreur de programmation ou une base
#: corrompue prend l'apparence d'un broker instable : le collecteur tourne en
#: boucle avec backoff, le journal répète « connexion perdue », et l'on découvre
#: au bout de quatorze jours que rien n'a été enregistré.
FATALES = (sqlite3.ProgrammingError, sqlite3.IntegrityError, BotError,
           TypeError, AttributeError, NameError, ImportError)


class Collector:
    def __init__(self, source: MarketDataSource, cfg: Config):
        self.source = source
        self.cfg = cfg
        # La connexion N'EST PAS ouverte ici. Un objet sqlite3.Connection ne
        # peut être utilisé que dans le thread qui l'a créé, et ce collecteur
        # est démarré depuis un autre thread que celui qui l'instancie (voir
        # maxprofit/hosting/service.py). Elle est donc ouverte par run().
        self.conn: sqlite3.Connection | None = None
        self.store: MarketWriter | None = None
        self.agg = CandleAggregator()
        self.buf: List[Tick] = []
        self.subscribed: List[str] = []
        self.running = True
        # Les minuteurs partent à `now` et non à 0 : sinon le premier passage
        # dans la boucle rejoue immédiatement toutes les tâches périodiques,
        # dont un second relevé de payouts une seconde après le premier.
        maintenant = time.time()
        self._t_flush = self._t_beat = self._t_pairs = self._t_backup = maintenant
        self._t_sync = maintenant

    def stop(self, *_):
        log.info("Arrêt demandé, vidage des tampons...")
        self.running = False

    # --- filtrage des paires ------------------------------------------------

    def refresh_pairs(self) -> None:
        pairs = self.source.list_pairs()
        # Historique COMPLET : toutes les paires, ouvertes ou non. Le filtre
        # ci-dessous décide seulement à quoi s'abonner, il ne décide de rien
        # pour le backtest, qui rejouera l'éligibilité depuis cette table.
        self.store.insert_payouts(int(time.time()), pairs)

        # Les meilleurs payouts d'abord : si l'on doit se limiter, autant que
        # ce soit sur les paires qui rapportent le plus.
        candidates = sorted(
            (p for p in pairs
             if p.is_open and p.payout_pct >= self.cfg.min_payout),
            key=lambda p: (-p.payout_pct, p.name),
        )
        eligible = sorted(p.name for p in candidates[:self.cfg.max_paires])
        if len(candidates) > self.cfg.max_paires:
            log.info(
                "%d paires éligibles, abonnement limité aux %d meilleurs "
                "payouts. Les payouts de toutes restent enregistrés.",
                len(candidates), self.cfg.max_paires,
            )

        if eligible != self.subscribed:
            ajoutees = set(eligible) - set(self.subscribed)
            retirees = set(self.subscribed) - set(eligible)
            self.source.subscribe(eligible)
            self.subscribed = eligible
            log.info("Paires éligibles : %d (+%d / -%d)",
                     len(eligible), len(ajoutees), len(retirees))

    # --- écriture -----------------------------------------------------------

    def flush(self) -> None:
        n_t = self.store.insert_ticks(self.buf)
        n_c = self.store.upsert_candles(self.agg.drain_closed())
        self.buf.clear()
        # Valider explicitement : `libsql` tient une transaction implicite et
        # accumulerait sans fin sans jamais rien pousser vers Turso. En sqlite3
        # autocommit, c'est sans effet.
        valider(self.conn)
        if n_t or n_c:
            log.debug("flush: %d ticks, %d bougies", n_t, n_c)

    def backup(self) -> None:
        """§1.4. Une sauvegarde ratée ne doit pas tuer la collecte : perdre six
        heures de sauvegarde est réparable, perdre le collecteur ne l'est pas.
        L'échec est journalisé en ERROR pour rester visible."""
        try:
            sauvegarder_et_purger(self.conn, self.cfg.db, backups_dir(),
                                  now=datetime.now(timezone.utc))
        except Exception as erreur:
            log.error("Sauvegarde échouée : %s", erreur)

    # --- boucle -------------------------------------------------------------

    def _ouvrir(self) -> None:
        """Ouvre la base DANS le thread qui va s'en servir."""
        self.conn = open_read_write(self.cfg.db)
        self.store = MarketWriter(self.conn)

    def _base_repond(self) -> bool:
        """La connexion à la base est-elle encore vivante ?

        Une connexion réseau meurt aussi. Avec Turso, le flux Hrana vers le
        serveur peut expirer ou être fermé, et toute écriture échoue alors sur
        « stream not found ». Un fichier SQLite local, lui, ne tombe jamais —
        c'est pourquoi le collecteur ne le vérifiait pas.

        Sans ce contrôle, la boucle de reconnexion rouvrait le socket du broker
        indéfiniment tout en réutilisant une connexion de base morte : elle
        semblait travailler et n'écrivait plus une ligne.
        """
        if self.conn is None:
            return False
        try:
            self.conn.execute("SELECT 1").fetchone()
            return True
        except Exception as erreur:                      # noqa: BLE001
            log.warning("La connexion à la base ne répond plus : %s", erreur)
            return False

    def _rouvrir_base(self) -> None:
        """Referme et rouvre. Les ticks en tampon survivent : ils sont en
        mémoire, et seront écrits au premier vidage réussi."""
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:                                # noqa: BLE001
            pass
        log.info("Réouverture de la base.")
        self._ouvrir()

    def run(self) -> None:
        self._ouvrir()
        backoff = 1
        try:
            while self.running:
                try:
                    self.source.connect()
                    self.refresh_pairs()
                    self._t_pairs = time.time()
                    backoff = 1
                    self._consume()
                except KeyboardInterrupt:
                    self.stop()
                except SourceIndisponible as erreur:
                    if isinstance(erreur, SessionExpiree):
                        raise
                    if not self._base_repond():
                        self._rouvrir_base()
                    # Indisponibilité passagère du broker : exactement ce que
                    # le backoff sert à absorber. Sans ce cas explicite, elle
                    # tomberait dans FATALES — qui contient BotError, dont elle
                    # hérite — et tuerait la collecte à la première alerte.
                    log.warning("Source indisponible (%s). Reconnexion dans %ds",
                                erreur, backoff)
                    self._vider_tampons()
                    if not self.running:
                        break
                    time.sleep(backoff)
                    backoff = min(backoff * 2, self.cfg.max_backoff_sec)
                except SessionExpiree:
                    # Réessayer ne peut rien réparer : il faut un nouveau jeton,
                    # donc un humain. On laisse remonter pour que le superviseur
                    # alerte, plutôt que de boucler des jours sur un jeton mort
                    # en journalisant « connexion perdue » toutes les minutes.
                    log.error("Session expirée : arrêt en attente d'un nouveau "
                              "jeton.")
                    self._vider_tampons()
                    raise
                except FATALES:
                    # Réessayer ne peut rien réparer. On sauve ce qu'on a et on
                    # laisse remonter : mieux vaut un processus mort et visible
                    # qu'un processus vivant qui n'enregistre rien.
                    log.exception("Erreur non récupérable, arrêt du collecteur")
                    self._vider_tampons()
                    raise
                except Exception as erreur:
                    # Déconnexion : les bougies en cours deviennent incomplètes.
                    log.warning("Connexion perdue (%s). Reconnexion dans %ds",
                                erreur, backoff)
                    # La panne peut venir de la base autant que du broker — avec
                    # un stockage distant, une écriture échoue comme un socket.
                    # Vérifier AVANT de vider les tampons : les vider sur une
                    # connexion morte perdrait les ticks au lieu de les écrire.
                    if not self._base_repond():
                        self._rouvrir_base()
                    self._vider_tampons()
                    if not self.running:
                        break
                    time.sleep(backoff)
                    backoff = min(backoff * 2, self.cfg.max_backoff_sec)
        finally:
            self._vider_tampons()
            if self.store is not None:
                log.info("Totaux en base : %s", self.store.counts())
                self.store.close()
            self.source.close()

    def _vider_tampons(self) -> None:
        """Écrit tout ce qui est en mémoire. Appelée sur chaque sortie de la
        boucle, y compris en erreur : les ticks déjà reçus sont des données
        acquises, il n'y a aucune raison de les perdre parce que la suite s'est
        mal passée."""
        if self.store is None:
            return
        try:
            self.store.upsert_candles(self.agg.drain_all())
            self.flush()
            # Synchroniser APRÈS avoir vidé les tampons, et sur chaque sortie de
            # boucle : c'est la dernière occasion de pousser vers Turso avant
            # qu'un arrêt ne fasse disparaître la réplique locale.
            turso.synchroniser(self.conn)
        except Exception:
            log.exception("Impossible de vider les tampons")

    def _consume(self) -> None:
        for tick in self.source.stream():
            if not self.running:
                return
            self.buf.append(tick)
            self.agg.add(tick)

            now = time.time()
            if now - self._t_flush >= self.cfg.flush_sec:
                self.flush()
                self._t_flush = now
            if now - self._t_beat >= self.cfg.heartbeat_sec:
                self.store.heartbeat(int(now), len(self.subscribed))
                self._t_beat = now
            if now - self._t_pairs >= self.cfg.pairs_refresh_sec:
                self.refresh_pairs()
                self._t_pairs = now
            if now - self._t_backup >= self.cfg.backup_sec:
                self.backup()
                self._t_backup = now
            if now - self._t_sync >= self.cfg.sync_sec:
                turso.synchroniser(self.conn)
                self._t_sync = now


def build_config(args) -> Config:
    """Assemble la configuration. `--db` l'emporte sur `TRADING_DB_PATH` pour
    les tests et l'inspection ; en production, on ne passe pas `--db`."""
    return Config(
        db=Path(args.db) if args.db else chemin_donnees(),
        min_payout=args.min_payout,
        max_paires=args.max_paires,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collecteur de données de marché")
    ap.add_argument("--source", choices=["sim", "po"], default="sim")
    ap.add_argument("--db", default=None,
                    help="Chemin de la base. Par défaut : $TRADING_DB_PATH.")
    ap.add_argument("--min-payout", type=int, required=True,
                    help="Payout minimal pour s'abonner à une paire. "
                         "Obligatoire : aucune valeur par défaut sur ce qui "
                         "touche à l'argent (spec §5).")
    ap.add_argument("--max-paires", type=int,
                    default=int(os.environ.get("MAX_PAIRES", "8") or 8),
                    help="Nombre maximal de paires souscrites (défaut : 8, ou "
                         "$MAX_PAIRES). Au-delà d'une dizaine, le broker ferme "
                         "le socket sans envoyer de ticks.")
    ap.add_argument("--duration", type=int, default=0,
                    help="Arrêt automatique après N secondes (0 = illimité)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    charger_env_local()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    try:
        cfg = build_config(a)
    except BotError as erreur:
        log.error("%s", erreur)
        return 2

    src = SimulatedSource() if a.source == "sim" else PocketOptionSource(demo=True)
    c = Collector(src, cfg)

    signal.signal(signal.SIGINT, c.stop)
    signal.signal(signal.SIGTERM, c.stop)

    if a.duration:
        import threading
        threading.Timer(a.duration, c.stop).start()

    c.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
