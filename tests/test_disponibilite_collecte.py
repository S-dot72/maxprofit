"""
Étape 3, niveau 2 : ce que le mécanisme de collecte garantit, éprouvé sur
le VRAI code (`CandleAggregator`, `Collector.flush`, `Collector.run`, la
base SQLite et la classification de l'audit), et non supposé.

Ce que ces tests établissent :
- l'ordre d'écriture : chemin, puis bougie, puis validation, puis réveil de
  la course ;
- les frontières : un tick appartient à la minute de son instant, et une
  minute ne se ferme qu'à l'arrivée d'un tick de la minute suivante ;
- un tick arrivé après la fermeture de sa minute est écarté du chemin ET de
  la bougie ;
- les copies d'un tick (deux sockets) et l'arrivée hors d'ordre gardent
  chemin et bougie sur le même ensemble de ticks ;
- une minute réécrite après une coupure n'est plus « identique » : la
  concordance la sort du niveau 2.

Ce qu'ils n'établissent pas : l'heure de réception réelle, qui n'est pas
stockée. Le contrôle sur les ordres réels (`/micro_essai`) la borne en
production.
"""

from __future__ import annotations

from typing import Iterator, List, Sequence

import pytest

from maxprofit.apprentissage.audit_ticks import AuditTicks, IDENTIQUE, ORDRE
from maxprofit.apprentissage.microstructure_pre import (
    CONCORDANCES_NIVEAU_2, niveau)
from maxprofit.collect import collector as module_collecteur
from maxprofit.collect.collector import CandleAggregator, Collector, Config
from maxprofit.collect.sources import MarketDataSource
from maxprofit.core.types import PairInfo, Tick
from maxprofit.store.chemin_ticks import decoder
from maxprofit.store.db import open_read_only
from maxprofit.store.market import MarketReader

PAIRE = "EURUSD_otc"
M0 = 1_792_540_800            # 21/10/2026 00:00 UTC, un début de minute
M0_MS = M0 * 1000


def _t(ms: int, prix: float, paire: str = PAIRE) -> Tick:
    return Tick(paire, M0_MS + ms, prix)


# --------------------------------------------------------------------------- #
# Ordre d'écriture dans `flush`
# --------------------------------------------------------------------------- #

class _Espion:
    def __init__(self, journal: list) -> None:
        self.journal = journal

    def insert_tick_paths(self, chemins) -> int:
        self.journal.append(("chemins", sorted((c.pair, c.minute_sec,
                                                c.n_ticks) for c in chemins)))
        return len(chemins)

    def upsert_candles(self, bougies) -> int:
        self.journal.append(("bougies", sorted((b.pair, b.ts_sec,
                                                b.tick_count)
                                               for b in bougies)))
        return len(bougies)


class _Reveil:
    def __init__(self, journal: list) -> None:
        self.journal = journal

    def set(self) -> None:
        self.journal.append(("réveil",))


def _collecteur_espionne(monkeypatch, tmp_path) -> tuple[Collector, list]:
    journal: list = []
    cfg = Config(db=tmp_path / "x.db", min_payout=92, flush_sec=0.0,
                 heartbeat_sec=0, max_backoff_sec=1)
    c = Collector(_SourceVide(), cfg)
    c.store = _Espion(journal)
    c.conn = object()
    monkeypatch.setattr(module_collecteur, "valider",
                        lambda conn: journal.append(("validation",)))
    monkeypatch.setattr(module_collecteur, "BOUGIES_ECRITES",
                        _Reveil(journal))
    return c, journal


class _SourceVide(MarketDataSource):
    def connect(self) -> None:
        pass

    def list_pairs(self) -> List[PairInfo]:
        return []

    def subscribe(self, pairs) -> None:
        pass

    def stream(self) -> Iterator[Tick]:
        return iter(())


def test_le_chemin_est_ecrit_avant_la_bougie_et_le_reveil_vient_en_dernier(
        monkeypatch, tmp_path):
    c, journal = _collecteur_espionne(monkeypatch, tmp_path)
    for ms, prix in ((100, 1.1), (30_000, 1.1002), (59_999, 1.1001),
                     (60_000, 1.1003)):
        c.agg.add(_t(ms, prix))
    c.flush()
    assert [e[0] for e in journal] == ["chemins", "bougies", "validation",
                                       "réveil"]
    assert journal[0][1] == [(PAIRE, M0, 3)]
    assert journal[1][1] == [(PAIRE, M0, 3)], (
        "la bougie et le chemin ne portent pas les mêmes ticks")


def test_aucune_minute_ouverte_n_est_ecrite_ni_ne_reveille_la_course(
        monkeypatch, tmp_path):
    """Une paire silencieuse : sa minute reste ouverte tant qu'aucun tick
    de la minute suivante n'arrive, même si l'horloge l'a dépassée."""
    c, journal = _collecteur_espionne(monkeypatch, tmp_path)
    c.agg.add(_t(100, 1.1))
    c.agg.add(_t(59_000, 1.1001))
    c.flush()
    assert ("réveil",) not in journal
    assert all(not e[1] for e in journal if e[0] in ("chemins", "bougies"))
    c.agg.add(_t(185_000, 1.1004))          # trois minutes plus tard
    journal.clear()
    c.flush()
    assert journal[0] == ("chemins", [(PAIRE, M0, 2)])
    assert journal[-1] == ("réveil",)


# --------------------------------------------------------------------------- #
# Frontières, retards, copies, ordre d'arrivée : l'agrégateur seul
# --------------------------------------------------------------------------- #

def _paquets(agg: CandleAggregator) -> dict[int, list[Tick]]:
    return {p[0].ts_ms // 60_000 * 60: p for p in agg.drain_ticks()}


def test_un_tick_pile_a_la_frontiere_appartient_a_la_minute_suivante():
    agg = CandleAggregator(garder_ticks=True)
    agg.add(_t(59_999, 1.1))
    agg.add(_t(60_000, 1.2))
    agg.add(_t(120_000, 1.3))
    (b0, b1) = agg.drain_closed()
    paquets = _paquets(agg)
    assert (b0.ts_sec, b0.tick_count, b0.close) == (M0, 1, 1.1)
    assert (b1.ts_sec, b1.tick_count, b1.open) == (M0 + 60, 1, 1.2)
    assert [t.ts_ms for t in paquets[M0]] == [M0_MS + 59_999]
    assert [t.ts_ms for t in paquets[M0 + 60]] == [M0_MS + 60_000]


def test_un_tick_en_retard_est_ecarte_du_chemin_comme_de_la_bougie():
    """Le cœur du niveau 2 : un tick horodaté dans une minute close a été
    REÇU avant sa fermeture, sinon il n'est ni dans le chemin ni dans la
    bougie."""
    agg = CandleAggregator(garder_ticks=True)
    agg.add(_t(1_000, 1.1))
    agg.add(_t(60_500, 1.2))                # ferme la minute M0
    agg.add(_t(59_000, 9.9))                # retardataire de M0 : écarté
    agg.add(_t(120_500, 1.3))
    b0 = agg.drain_closed()[0]
    paquets = _paquets(agg)
    assert b0.tick_count == 1 and b0.high == 1.1
    assert [t.price for t in paquets[M0]] == [1.1]
    assert all(t.price != 9.9 for p in paquets.values() for t in p)


def test_les_copies_d_un_tick_sont_dans_le_chemin_et_dans_le_compte():
    """Deux sockets écrivent le même tick : chemin et bougie restent sur le
    même ensemble (copies comprises), donc « identique »."""
    agg = CandleAggregator(garder_ticks=True)
    for ms, prix in ((1_000, 1.1), (1_000, 1.1), (2_000, 1.2), (2_000, 1.2)):
        agg.add(_t(ms, prix))
    agg.add(_t(60_000, 1.3))
    b0 = agg.drain_closed()[0]
    assert b0.tick_count == 4
    assert len(_paquets(agg)[M0]) == 4


# --------------------------------------------------------------------------- #
# Bout en bout : collecte réelle en SQLite, puis classification de l'audit
# --------------------------------------------------------------------------- #

class _SourceScriptee(MarketDataSource):
    """Joue des épisodes de ticks ; entre deux épisodes, lève une perte de
    connexion. Arrête le collecteur après le dernier."""

    def __init__(self, episodes: Sequence[Sequence[Tick]]) -> None:
        self.episodes = [list(e) for e in episodes]
        self.collecteur: Collector | None = None

    def connect(self) -> None:
        pass

    def list_pairs(self) -> List[PairInfo]:
        return [PairInfo(PAIRE, True, 92)]

    def subscribe(self, pairs) -> None:
        pass

    def stream(self) -> Iterator[Tick]:
        episode = self.episodes.pop(0)
        yield from episode
        if self.episodes:
            raise ConnectionError("socket fermé par le broker")
        assert self.collecteur is not None
        self.collecteur.stop()


def _collecter(tmp_path, episodes) -> dict[int, str | None]:
    source = _SourceScriptee(episodes)
    cfg = Config(db=tmp_path / "market.db", min_payout=92, flush_sec=0.0,
                 heartbeat_sec=0, max_backoff_sec=0)
    source.collecteur = c = Collector(source, cfg)
    c.run()
    lecteur = MarketReader(open_read_only(tmp_path / "market.db"))
    try:
        audit = AuditTicks(M0, M0 + 3600, decoder=decoder,
                           garder_classes=True)
        audit.ajouter_paire(PAIRE, lecteur.candles(PAIRE, 60, M0, M0 + 3600),
                            lecteur.tick_paths(PAIRE, M0, M0 + 3600))
    finally:
        lecteur.close()
    return audit.classes.get(PAIRE, {})


def _minute(m: int, prix: Sequence[float], pas_ms: int = 400,
            depart_ms: int = 200) -> list[Tick]:
    return [_t(m * 60_000 + depart_ms + i * pas_ms, p)
            for i, p in enumerate(prix)]


def test_une_collecte_sans_incident_est_identique_minute_par_minute(tmp_path):
    ticks = []
    for m in range(5):
        ticks += _minute(m, [round(1.1 + 0.0001 * ((i * 7 + m) % 11), 5)
                             for i in range(120)], pas_ms=480)
    classes = _collecter(tmp_path, [ticks])
    assert [classes[M0 + 60 * m] for m in range(4)] == [IDENTIQUE] * 4
    minutes = [M0 + 60 * m for m in range(4)]
    par_minute = {m: [1] for m in minutes}
    assert niveau(minutes, par_minute, classes) == 2


def test_l_arrivee_hors_d_ordre_reste_au_niveau_2(tmp_path):
    """L'encodeur trie par instant, l'agrégateur prend ouverture et clôture
    à l'arrivée : mêmes ticks, « ordre d'arrivée », niveau 2."""
    m0 = _minute(0, [1.1001, 1.1003, 1.1002, 1.1004])
    m0 = [m0[1], m0[0], m0[2], m0[3]]          # les deux premiers inversés
    ticks = m0 + _minute(1, [1.1005, 1.1006]) + _minute(2, [1.1007])
    classes = _collecter(tmp_path, [ticks])
    assert classes[M0] == ORDRE
    assert ORDRE in CONCORDANCES_NIVEAU_2


def test_une_minute_reecrite_apres_une_coupure_sort_du_niveau_2(tmp_path):
    """Coupure au milieu de la minute 1, reprise dans la même minute : la
    bougie et le chemin ne portent plus le même ensemble de ticks. La
    concordance le voit, et le niveau 2 écarte toute fenêtre qui la
    contient."""
    avant = _minute(0, [1.1001] * 5) + _minute(1, [1.1002, 1.1003, 1.1004])
    apres = (_minute(1, [1.1005, 1.1006], depart_ms=40_000)
             + _minute(2, [1.1007] * 3) + _minute(3, [1.1008]))
    classes = _collecter(tmp_path, [avant, apres])
    assert classes[M0] == IDENTIQUE
    assert classes[M0 + 60] not in CONCORDANCES_NIVEAU_2
    assert classes[M0 + 120] == IDENTIQUE
    minutes = [M0, M0 + 60, M0 + 120]
    assert niveau(minutes, {m: [1] for m in minutes}, classes) == 1


@pytest.mark.parametrize("decalage_ms", [0, 1, 59_999])
def test_aucun_tick_d_une_minute_ouverte_n_est_en_base_au_reveil(
        monkeypatch, tmp_path, decalage_ms):
    """Au moment où la course est réveillée, la base ne contient aucun tick
    de la minute en cours : rien d'après la fin de la bougie du signal."""
    c, journal = _collecteur_espionne(monkeypatch, tmp_path)
    c.agg.add(_t(1_000, 1.1))
    c.agg.add(_t(60_000 + decalage_ms, 1.2))
    c.flush()
    assert journal[0] == ("chemins", [(PAIRE, M0, 1)])
