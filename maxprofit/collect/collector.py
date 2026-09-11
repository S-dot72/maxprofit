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
from maxprofit.collect.pocketoption import (
    BrokerInjoignable,
    RedemarrageRequis,
    SessionExpiree,
    SourceIndisponible,
)
from maxprofit.collect.sources import (
    MarketDataSource,
    PocketOptionSource,
    SimulatedSource,
)
from maxprofit.store import etat_broker, turso
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
    #: Battement de cœur. 30 s et non 10 : à 10 s c'était 8 640 lignes par
    #: jour, plus que les bougies et les payouts réunis, pour une granularité
    #: que rien n'exploite — les bougies sont à la minute, et un trou plus court
    #: qu'une bougie ne change pas le verdict du §2.4.
    heartbeat_sec: int = 30
    backup_sec: int = 6 * 3600        # sauvegarde toutes les 6 h (§1.4)
    #: Synchronisation vers Turso. Sans effet en stockage local.
    #:
    #: Ce n'est PAS une fenêtre de perte, contrairement à ce que ce commentaire
    #: affirmait. Mesuré le 2026-09-09 : `sync()` tire les changements distants,
    #: il ne pousse rien — les écritures partent au `commit()`. Pour le seul
    #: écrivain de la base, les synchronisations périodiques n'apprennent
    #: strictement rien.
    #:
    #: Six heures, donc : une prudence résiduelle au cas où une seconde
    #: instance aurait tourné, et non un mécanisme de durabilité. À 60 s, le
    #: quota du plan gratuit était consommé à 77 % avant que la collecte n'ait
    #: commencé.
    #: Mesuré : à 60 s, le quota de synchronisations du plan gratuit Turso
    #: était consommé à 77 % avant même que la collecte n'ait commencé. Une
    #: campagne de quatorze jours en demande 20 000 à ce rythme.
    #:
    #: À 300 s, la fenêtre de perte passe de une à cinq minutes en cas d'arrêt
    #: brutal du conteneur. Ce n'est pas une perte silencieuse : elle laisse un
    #: trou dans `uptime`, et le backtest écarte les fenêtres qui le chevauchent
    #: (§2.4). Cinq minutes écartées valent mieux qu'une collecte interrompue au
    #: dixième jour faute de quota.
    sync_sec: int = 6 * 3600
    max_backoff_sec: int = 60
    #: Au-dela de combien de secondes sans un seul tick on le dit dans le
    #: journal. Deux minutes : assez pour ne pas crier sur une paire calme,
    #: assez peu pour ne pas decouvrir le silence deux heures plus tard.
    silence_alerte_sec: int = 120
    #: Écrire ou non les ticks bruts.
    #:
    #: Mesuré : à 4 paires et 2 ticks/s, les ticks sont **97,6 % du volume
    #: écrit** — 9,9 millions de lignes sur quatorze jours contre 238 000 pour
    #: tout le reste. C'est ce qui épuise un plan gratuit, chez n'importe quel
    #: fournisseur.
    #:
    #: Les couper ne coûte rien au §2 : le backtest travaille sur les bougies
    #: M1, et `tick_count` — le critère de qualité du §2.4 — est porté par la
    #: bougie elle-même, calculée depuis les ticks avant écriture. Ce qu'on perd
    #: est l'analyse sous la minute et la possibilité de ré-agréger sur un autre
    #: pas de temps.
    #:
    #: Vrai par défaut : ne pas jeter des données en silence. Sur un stockage
    #: distant à quota, mettre `STOCKER_TICKS=0`.
    stocker_ticks: bool = True
    #: Paires SUIVIES en permanence, au lieu des meilleurs payouts du moment.
    #:
    #: Mesuré sur la collecte réelle : suivre le classement des payouts a donné
    #: 18 paires hachées en tranches de quelques heures, au lieu de 4 séries
    #: continues. Le classement tourne, l'abonnement suit, et l'on ne peut plus
    #: mesurer quoi que ce soit — une autocorrélation sur un morceau de deux
    #: heures ne veut rien dire, et le §2 demande quatorze jours CONTINUS.
    #:
    #: Épingler coûte des payouts moins bons ; c'est le prix de la continuité,
    #: et l'historique complet des payouts reste enregistré pour toutes les
    #: paires, donc le backtest rejoue l'éligibilité telle qu'elle était (§2.3).
    #:
    #: Vide = comportement d'origine, les meilleurs payouts.
    paires_fixes: tuple[str, ...] = ()
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
    #:
    #: Le défaut est 4 parce que c'est le SEUL nombre qu'on ait observé en
    #: train de livrer des ticks : le diagnostic du 5 septembre, quatre paires,
    #: ~2 ticks/s chacune. À 8, le broker a fermé le socket deux secondes après
    #: l'abonnement ; à 32, vingt secondes. On remontera quand des ticks seront
    #: confirmés, pas avant — mieux vaut quatre paires collectées que huit
    #: paires silencieuses.
    max_paires: int = 4

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
        #: Lisibles depuis un autre thread (le bot Telegram), pour dire pourquoi
        #: rien ne se passe. Deux entiers publiés par le thread qui les calcule
        #: et seulement lus ailleurs : pas de verrou nécessaire, et une lecture
        #: légèrement en retard ne trompe personne.
        self.echecs_broker = 0
        self.pause_jusqu_a_sec = 0.0
        self._t_dernier_tick = maintenant

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

        if self.cfg.paires_fixes:
            eligible = self._paires_epinglees(pairs)
        else:
            # Les meilleurs payouts d'abord : si l'on doit se limiter, autant
            # que ce soit sur les paires qui rapportent le plus.
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

    def _paires_epinglees(self, pairs) -> List[str]:
        """Les paires demandées, sans tenir compte du classement des payouts.

        Le payout minimal n'est PAS appliqué ici. Il sert à choisir où l'on
        mettrait de l'argent ; épingler, c'est décider où l'on veut une série
        continue. Un payout qui descend sous le seuil pendant deux heures ne
        doit pas trouer l'historique — le backtest rejouera l'éligibilité
        depuis la table des payouts, qui, elle, enregistre tout (§2.3).

        Un nom inconnu du catalogue est signalé fort : une faute de frappe
        collecterait silencieusement moins de paires que demandé, et l'on s'en
        apercevrait au moment d'analyser.
        """
        connues = {p.name: p for p in pairs}
        retenues, inconnues, fermees = [], [], []
        for nom in self.cfg.paires_fixes:
            info = connues.get(nom)
            if info is None:
                inconnues.append(nom)
            elif not info.is_open:
                fermees.append(nom)
            else:
                retenues.append(nom)

        if inconnues:
            log.error(
                "PAIRES_FIXES contient %d nom(s) inconnu(s) du broker : %s. "
                "Vérifiez l'orthographe — ces paires ne seront jamais "
                "collectées.", len(inconnues), ", ".join(inconnues))
        if fermees:
            log.info("Épinglées mais fermées pour l'instant : %s",
                     ", ".join(fermees))
        if not retenues:
            log.warning(
                "Aucune paire épinglée n'est ouverte : rien à suivre pour "
                "l'instant. La collecte reste connectée et reprendra à "
                "l'ouverture.")
        return sorted(retenues)

    # --- écriture -----------------------------------------------------------

    def flush(self) -> None:
        n_t = self.store.insert_ticks(self.buf) if self.cfg.stocker_ticks else 0
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

    def _imputer_au_broker(self, erreur: Exception, base_vivante: bool) -> None:
        """N'inscrire un refus que s'il est imputable au broker.

        Une écriture qui échoue parce que le flux vers Turso a expiré remonte
        ici sous la même forme qu'un socket fermé par le courtier. Les compter
        pareil mettrait la collecte en pénitence pendant des heures pour une
        panne de stockage — en cessant d'appeler le seul acteur qui n'y est
        pour rien.
        """
        if not base_vivante:
            log.info("Panne imputée à la base, pas au broker : %s", erreur)
            return
        etat_broker.noter_echec(self.conn, str(erreur))

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

    def _patienter_avant_broker(self) -> None:
        """Purger la dette d'attente accumulée par les échecs précédents.

        AVANT `source.connect()`, donc avant que le client du broker n'existe :
        une fois construit, son thread compose toutes les dix secondes et rien
        ne l'arrête. Attendre après coup n'empêcherait aucun appel.

        L'attente survit aux redémarrages parce qu'elle est calculée depuis la
        base. C'est le seul moyen : le processus meurt à chaque échec, et un
        compteur en mémoire repartirait toujours de zéro.
        """
        attente = etat_broker.attente_requise(self.conn)
        echecs, _, raison = etat_broker.lire(self.conn)
        self.echecs_broker = echecs
        if attente <= 0:
            self.pause_jusqu_a_sec = 0.0
            return
        self.pause_jusqu_a_sec = time.time() + attente
        log.warning(
            "%d échec(s) de connexion consécutif(s) : silence de %d s avant de "
            "rappeler le broker. Dernière raison : %s",
            echecs, int(attente), raison,
        )
        fin = self.pause_jusqu_a_sec
        while self.running and time.time() < fin:
            time.sleep(min(1.0, fin - time.time()))
        self.pause_jusqu_a_sec = 0.0

    def run(self) -> None:
        self._ouvrir()
        backoff = 1
        try:
            while self.running:
                try:
                    self._patienter_avant_broker()
                    if not self.running:
                        break
                    self.source.connect()
                    # Le crédit est rendu ici, pas à la fin de la session : une
                    # poignée de main réussie prouve que le broker nous accepte,
                    # et c'est la seule chose que l'attente cherchait à obtenir.
                    etat_broker.noter_succes(self.conn)
                    self.echecs_broker = 0
                    # La liste des abonnements vit sur le SERVEUR, et un socket
                    # neuf n'en a aucun. Garder `subscribed` ferait croire a
                    # refresh_pairs() qu'il n'y a rien a faire : on resterait
                    # connecte, souscrit a rien, et muet -- sans une seule
                    # erreur pour le dire. C'est exactement ce qui donnait
                    # « sonde verte, battement frais, 0 tick » en production.
                    self.subscribed = []
                    self.refresh_pairs()
                    self._t_pairs = time.time()
                    backoff = 1
                    self._consume()
                except KeyboardInterrupt:
                    self.stop()
                except SourceIndisponible as erreur:
                    if isinstance(erreur, SessionExpiree):
                        raise
                    base_vivante = self._base_repond()
                    if not base_vivante:
                        self._rouvrir_base()
                    # Indisponibilité passagère du broker : exactement ce que
                    # le backoff sert à absorber. Sans ce cas explicite, elle
                    # tomberait dans FATALES — qui contient BotError, dont elle
                    # hérite — et tuerait la collecte à la première alerte.
                    self._imputer_au_broker(erreur, base_vivante)
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
                except (BrokerInjoignable, RedemarrageRequis) as erreur:
                    # Ces deux-là tuent le processus, et l'hébergeur le relance
                    # dans la minute. C'est précisément le cycle qui empêchait
                    # une limitation de débit d'expirer : on inscrit l'échec en
                    # base pour que le processus SUIVANT sache se taire.
                    etat_broker.noter_echec(self.conn, str(erreur))
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
                    base_vivante = self._base_repond()
                    if not base_vivante:
                        self._rouvrir_base()
                    self._imputer_au_broker(erreur, base_vivante)
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
            # Pas de `synchroniser()` ici. Cette fonction est appelée à CHAQUE
            # sortie de boucle, donc à chaque reconnexion — c'était le premier
            # consommateur de quota, pour un geste qui ne pousse aucune donnée.
            # Le `flush()` ci-dessous valide, et la validation est ce qui envoie
            # les écritures au serveur.
            self.flush()
        except Exception:
            log.exception("Impossible de vider les tampons")

    def _consume(self) -> None:
        """Draine le flux. `None` signifie « rien pour l'instant », pas la fin.

        Sans ce cas, tout le travail periodique etait suspendu a l'arrivee d'un
        tick : un marche calme, ou un abonnement que le broker n'alimente pas,
        et plus rien ne tournait. Pas de battement de coeur, donc une sonde
        stale ; pas de `sync`, donc rien ne partait vers Turso ; et surtout
        aucune requete sur la connexion libSQL, dont le flux Hrana finissait par
        etre jete par le serveur (« stream not found ») au bout d'une vingtaine
        de secondes d'inactivite.
        """
        for tick in self.source.stream():
            if not self.running:
                return
            if tick is not None:
                # Le tick alimente TOUJOURS l'agrégateur : c'est lui qui produit
                # la bougie et son `tick_count`. Seule son écriture individuelle
                # est optionnelle.
                if self.cfg.stocker_ticks:
                    self.buf.append(tick)
                self.agg.add(tick)
                self._t_dernier_tick = time.time()
            self._taches_periodiques()

    def _taches_periodiques(self) -> None:
        """Ce qui doit tourner a l'heure, tick ou pas."""
        now = time.time()
        if now - self._t_flush >= self.cfg.flush_sec:
            self.flush()
            self._t_flush = now
        if now - self._t_beat >= self.cfg.heartbeat_sec:
            self.store.heartbeat(int(now), len(self.subscribed))
            self._t_beat = now
            self._signaler_silence(now)
        if now - self._t_pairs >= self.cfg.pairs_refresh_sec:
            self.refresh_pairs()
            self._t_pairs = now
        if now - self._t_backup >= self.cfg.backup_sec:
            self.backup()
            self._t_backup = now
        if now - self._t_sync >= self.cfg.sync_sec:
            turso.synchroniser(self.conn)
            self._t_sync = now

    def _signaler_silence(self, now: float) -> None:
        """Dire qu'on est abonne et muet.

        C'est l'etat le plus couteux du systeme : tout a l'air normal — le
        processus vit, la connexion tient, le journal est calme — et l'on
        decouvre au bout de deux heures que rien n'a ete enregistre. Il faut
        que ca se voie dans le journal, pas seulement dans un compteur.
        """
        if not self.subscribed:
            return
        silence = now - self._t_dernier_tick
        if silence < self.cfg.silence_alerte_sec:
            return
        log.warning(
            "Abonne a %d paire(s) mais aucun tick depuis %d s : on se "
            "reabonne.",
            len(self.subscribed), int(silence),
        )
        self._t_dernier_tick = now      # une reaction par periode, pas par tour
        # Se plaindre ne suffit pas. La bibliotheque peut rouvrir son socket
        # toute seule, sans que rien ne leve ici : le serveur a alors oublie
        # nos abonnements et personne ne s'en apercoit. Repartir de zero force
        # refresh_pairs() a les renvoyer.
        self.subscribed = []
        self.refresh_pairs()
        self._t_pairs = now


def _min_payout_env() -> int | None:
    """`MIN_PAYOUT_PCT`, ou rien. Jamais de valeur inventee.

    Exister evite d'avoir a repeter le reglage sur la ligne de commande d'un
    script de relance, alors qu'il est deja dans le `.env` que lit le service.
    Une valeur illisible vaut absence : on refusera de demarrer avec un message,
    plutot que de collecter des paires choisies au hasard.
    """
    brut = os.environ.get("MIN_PAYOUT_PCT", "").strip()
    if not brut:
        return None
    try:
        return int(brut)
    except ValueError:
        log.warning("MIN_PAYOUT_PCT illisible (%r) : ignore.", brut)
        return None


def build_config(args) -> Config:
    """Assemble la configuration. `--db` l'emporte sur `TRADING_DB_PATH` pour
    les tests et l'inspection ; en production, on ne passe pas `--db`."""
    reglages = dict(
        db=Path(args.db) if args.db else chemin_donnees(),
        min_payout=args.min_payout,
        max_paires=args.max_paires,
    )
    # 0 ou absent = on garde le défaut de Config, plutôt que d'écrire un zéro
    # qui ferait synchroniser à chaque tour de boucle.
    sync = getattr(args, "sync_sec", 0)
    if sync:
        reglages["sync_sec"] = sync
    if getattr(args, "sans_ticks", False):
        reglages["stocker_ticks"] = False
    fixes = _paires_fixes_env(getattr(args, "paires", ""))
    if fixes:
        reglages["paires_fixes"] = fixes
    return Config(**reglages)


def _paires_fixes_env(brut: str) -> tuple[str, ...]:
    """Liste séparée par des virgules, depuis l'argument ou `PAIRES_FIXES`.

    Les doublons sont retirés en conservant l'ordre donné : ce sont les
    premières qui comptent si la liste dépasse `max_paires`.
    """
    texte = (brut or "").strip() or os.environ.get("PAIRES_FIXES", "").strip()
    vues, sortie = set(), []
    for nom in texte.split(","):
        nom = nom.strip()
        if nom and nom not in vues:
            vues.add(nom)
            sortie.append(nom)
    return tuple(sortie)


def main(argv: list[str] | None = None) -> int:
    # AVANT de construire les arguments, pas apres. Plusieurs defauts sont
    # lus dans l'environnement au moment ou `add_argument` s'execute : les
    # charger ensuite revenait a ignorer le `.env` en silence. Sur un
    # hebergeur, les variables sont deja dans l'environnement et rien ne se
    # voyait ; en local, MIN_PAYOUT_PCT etait present dans le fichier et le
    # collecteur refusait quand meme de demarrer.
    charger_env_local()

    ap = argparse.ArgumentParser(description="Collecteur de données de marché")
    ap.add_argument("--source", choices=["sim", "po"], default="sim")
    ap.add_argument("--db", default=None,
                    help="Chemin de la base. Par défaut : $TRADING_DB_PATH.")
    ap.add_argument("--min-payout", type=int,
                    default=_min_payout_env(),
                    help="Payout minimal pour s'abonner à une paire. "
                         "Obligatoire : aucune valeur par défaut sur ce qui "
                         "touche à l'argent (spec §5).")
    ap.add_argument("--max-paires", type=int,
                    default=int(os.environ.get("MAX_PAIRES", "4") or 4),
                    help="Nombre maximal de paires souscrites (défaut : 4, ou "
                         "$MAX_PAIRES). 4 est le seul nombre observé en train "
                         "de livrer des ticks ; au-delà, le broker ferme le "
                         "socket sans rien envoyer.")
    ap.add_argument("--paires", default="",
                    help="Paires à suivre en permanence, séparées par des "
                         "virgules (ou $PAIRES_FIXES). Remplace le classement "
                         "par payout : c'est ce qui donne des séries CONTINUES, "
                         "seules exploitables pour mesurer quoi que ce soit.")
    ap.add_argument("--sans-ticks", action="store_true",
                    default=os.environ.get("STOCKER_TICKS", "1").strip() == "0",
                    help="N'écrit pas les ticks bruts (97,6 %% du volume). Les "
                         "bougies M1 et leur tick_count restent complets : le "
                         "backtest du §2 n'y perd rien. Ou $STOCKER_TICKS=0.")
    ap.add_argument("--sync-sec", type=int,
                    default=int(os.environ.get("TURSO_SYNC_SEC", "0") or 0),
                    help="Intervalle de synchronisation vers Turso, en "
                         "secondes (défaut : 300, ou $TURSO_SYNC_SEC). "
                         "L'augmenter économise le quota, au prix d'une "
                         "fenêtre de perte plus large en cas d'arrêt brutal.")
    ap.add_argument("--duration", type=int, default=0,
                    help="Arrêt automatique après N secondes (0 = illimité)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if a.min_payout is None:
        # Refuser, pas choisir a sa place. La meme regle que dans le service,
        # et le meme message : ce reglage decide sur quoi on mise, il est
        # fourni explicitement ou le collecteur ne demarre pas (spec 5).
        log.error("Payout minimal non fourni : passez --min-payout ou "
                  "definissez MIN_PAYOUT_PCT. Aucune valeur par defaut n'est "
                  "appliquee (spec 5).")
        return 2

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

    try:
        c.run()
    except BotError as erreur:
        # Code 2 = configuration : `collecter.ps1` s'arrête au lieu de relancer.
        # Un pilote manquant ou un jeton invalide ne se répare pas en
        # réessayant, et trois traces identiques noient le message utile.
        log.error("%s", erreur)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
