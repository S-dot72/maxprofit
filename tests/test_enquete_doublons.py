"""Enquête sur la double réception des ticks (docs/enquete_doublons.md).

Deux familles de tests, et aucun ne modifie le collecteur :

- des tests de CARACTÉRISATION du code tel qu'il est : deux listeners qui
  écrivent dans le même tampon, ce que l'adaptateur en tire, ce que
  l'agrégateur en fait — le mécanisme supposé, rejoué sans réseau ;
- les politiques de déduplication comparées hors ligne, en mémoire, et leurs
  faux positifs inévitables.
"""

from __future__ import annotations

import hashlib
import itertools
import sqlite3
import time

import pytest

from maxprofit.apprentissage import doublons as dd
from maxprofit.collect.collector import CandleAggregator
from maxprofit.collect.pocketoption import PocketOptionSource
from maxprofit.core.types import Candle, Tick
from maxprofit.store.chemin_ticks import encoder
from maxprofit.store.db import open_read_write
from maxprofit.store.market import MarketWriter

P = "EURUSD_otc"


# --------------------------------------------------------------------------- #
# Le mécanisme, rejoué : un tampon global, deux listeners
# --------------------------------------------------------------------------- #

class _TamponGlobal:
    """`global_value.pairs` de la bibliothèque : UN dictionnaire par
    processus. Chaque socket y appende ce qu'il reçoit (ws/client.py, branche
    `updateStream`), et l'adaptateur le lit par `GetTicks`."""

    def __init__(self):
        self.pairs: dict = {}

    def recevoir(self, actif, instant, prix):
        self.pairs.setdefault(actif, {"ticks": [], "history": []})
        self.pairs[actif]["ticks"].append({"time": instant, "price": prix})

    def GetTicks(self, nom):
        entree = self.pairs.get(nom)
        return entree["ticks"] if entree else None

    def check_connect(self):
        return True


def _source(tampon):
    source = PocketOptionSource(demo=True)
    source._client = tampon
    source._souscrites = [P]
    source._decalage_sec = 0
    source._unite = "sec"
    return source


def _minute_courante() -> int:
    """Le début d'une minute récente : l'adaptateur compare l'horloge du
    broker à la nôtre, un instant figé vieillirait."""
    return int(time.time()) // 60 * 60 - 120


def _flux(debut: int, n: int = 6, pas: float = 0.5):
    """(instant en secondes, prix) d'une minute, prix tous différents."""
    return [(debut + 1 + k * pas, round(1.1 + k / 1000, 5)) for k in range(n)]


def _agreger(source):
    agg = CandleAggregator(garder_ticks=True)
    for tick in source._drainer(P):
        agg.add(tick)
    bougies = agg.drain_all()
    return bougies, agg.drain_ticks()


def test_un_seul_socket_chaque_tick_compte_une_fois():
    tampon = _TamponGlobal()
    debut = _minute_courante()
    for instant, prix in _flux(debut):
        tampon.recevoir(P, instant, prix)
    (bougie,), (chemin,) = _agreger(_source(tampon))
    assert bougie.tick_count == 6 and len(chemin) == 6


def test_deux_sockets_sur_un_tampon_chaque_tick_compte_deux_fois():
    """Caractérisation du code actuel : rien ne distingue les deux copies, ni
    dans le tampon ni après l'adaptateur. L'OHLC est le même, les comptes
    doublent."""
    tampon = _TamponGlobal()
    debut = _minute_courante()
    for instant, prix in _flux(debut):
        tampon.recevoir(P, instant, prix)            # socket du collecteur
        tampon.recevoir(P, instant, prix)            # socket du courtier
    assert set(map(len, map(dict.keys, tampon.pairs[P]["ticks"]))) == {2}, \
        "{time, price} : aucun champ ne dit de quel socket vient l'entrée"
    (bougie,), (chemin,) = _agreger(_source(tampon))
    assert bougie.tick_count == 12 and len(chemin) == 12
    assert (bougie.open, bougie.high, bougie.low, bougie.close) == (
        1.1, 1.105, 1.1, 1.105)


def test_un_socket_en_retard_change_la_cloture_a_la_frontiere():
    """Le second socket livre avec un retard plus grand que l'écart entre
    ticks : sa copie du dernier tick de la minute arrive APRÈS le premier
    tick de la minute suivante, et l'agrégateur l'écarte comme tardive. La
    clôture de la bougie est alors la copie arrivée la dernière — un tick
    plus ancien —, celle du chemin trié, le dernier tick : l'écart
    « ordre d'arrivée » de l'audit. Sans retard, rien."""
    debut = _minute_courante()
    flux = _flux(debut, n=4, pas=14) + [(debut + 61, 1.2)]
    tampon = _TamponGlobal()
    a_livrer = []                                    # copies du second socket
    for instant, prix in flux:
        tampon.recevoir(P, instant, prix)
        if a_livrer:
            tampon.recevoir(P, *a_livrer.pop(0))     # un tick de retard
        a_livrer.append((instant, prix))
    bougies, chemins = _agreger(_source(tampon))
    close = next(b for b in bougies if b.ts_sec == debut)
    chemin = next(c for c in chemins if c[0].ts_ms // 60_000 == debut // 60)
    assert close.close == 1.102, "la copie tardive du 3e tick"
    assert max(chemin, key=lambda t: t.ts_ms).price == 1.103
    assert close.tick_count == len(chemin) == 7, "copie du 4e tick écartée"


def test_une_reconnexion_ne_double_pas_a_elle_seule():
    """Après une coupure, le collecteur vide l'agrégateur (`_vider_tampons`)
    puis l'adaptateur relit TOUT le tampon (`_vus.clear()`) : les minutes
    sont reconstruites, chaque tick une fois."""
    tampon = _TamponGlobal()
    debut = _minute_courante()
    for instant, prix in _flux(debut, n=4):
        tampon.recevoir(P, instant, prix)
    source = _source(tampon)
    agg = CandleAggregator(garder_ticks=True)
    for tick in source._drainer(P):
        agg.add(tick)
    agg.drain_all()                                  # la coupure
    agg.drain_ticks()
    for instant, prix in _flux(debut + 2, n=2):      # reçus pendant la coupure
        tampon.recevoir(P, instant + 0.25, prix + 0.01)
    source._vus.clear()                              # le rétablissement
    for tick in source._drainer(P):
        agg.add(tick)
    (bougie,) = agg.drain_all()
    (chemin,) = agg.drain_ticks()
    assert bougie.tick_count == len(chemin) == 6
    assert len({(t.ts_ms, t.price) for t in chemin}) == 6


# --------------------------------------------------------------------------- #
# Les politiques, hors ligne
# --------------------------------------------------------------------------- #

def _t(ms, prix):
    return Tick(P, 1_792_800_000_000 + ms, prix)


def test_des_ticks_legitimes_au_meme_prix_sont_gardes():
    ticks = [_t(0, 1.1), _t(500, 1.1), _t(1000, 1.1)]
    assert dd.dedoublonner(ticks, "experimentale") == ticks


def test_le_meme_instant_a_un_autre_prix_est_garde():
    ticks = [_t(0, 1.1), _t(0, 1.2), _t(0, 1.1)]
    assert dd.dedoublonner(ticks, "experimentale") == ticks[:2]


def test_l_heuristique_ne_distingue_pas_une_coincidence_legitime():
    """Deux ticks LÉGITIMES au même instant et au même prix : l'heuristique
    en écarte un. C'est sa limite, et aucune donnée stockée ne la lève."""
    ticks = [_t(0, 1.1), _t(0, 1.1)]               # supposés légitimes
    assert len(dd.dedoublonner(ticks, "experimentale")) == 1


def test_l_ordre_d_arrivee_ne_change_pas_le_resultat():
    ticks = [_t(0, 1.1), _t(500, 1.2), _t(500, 1.2), _t(1000, 1.1),
             _t(0, 1.1)]
    resultats = set()
    for ordre in itertools.permutations(ticks):
        trie = sorted(ordre, key=lambda t: t.ts_ms)  # comme l'encodeur
        resultats.add(frozenset(dd.dedoublonner(trie, "experimentale")))
    assert len(resultats) == 1


def test_la_politique_prouvee_n_est_pas_testable():
    assert dd.dedoublonner([_t(0, 1.1)], "prouvee") is None
    r = dd.comparer([(P, 0, [_t(0, 1.1)], None)])
    assert r["politiques"]["prouvee"]["statut"] == "NOT TESTABLE"
    assert "identifiant" in r["politiques"]["prouvee"]["raison"]
    with pytest.raises(ValueError):
        dd.dedoublonner([], "inventee")


def _minute(debut_ms, n, double=False, manque=(), triple=()):
    ticks = []
    for k in range(n):
        t = _t(debut_ms + 500 * k, round(1.1 + (k % 3) / 1000, 5))
        ticks.append(t)
        if double and k not in manque:
            ticks.append(t)
        if k in triple:
            ticks.append(t)
    return ticks


def test_la_comparaison_dit_ce_qui_change_et_ce_qui_ne_change_pas():
    jour = 1_792_800_000 // 86400 * 86400
    minutes = []
    # Jour 1 : flux simple, dont une coïncidence au même instant et prix.
    for k in range(10):
        ticks = _minute((jour + 60 * k) * 1000 - 1_792_800_000_000, 100)
        if k == 0:
            ticks.append(ticks[10])
            ticks.sort(key=lambda t: t.ts_ms)
        minutes.append((P, jour + 60 * k, ticks, None))
    # Jour 3 : flux simple encore : la médiane de la paire reste « simple ».
    j3 = jour + 2 * 86400
    for k in range(10):
        ms = (j3 + 60 * k) * 1000 - 1_792_800_000_000
        minutes.append((P, j3 + 60 * k, _minute(ms, 100), None))
    # Jour 2 : flux doublé, un tick sans jumeau, un en trois exemplaires, et
    # une minute maigre (3 ticks distincts, 6 stockés).
    j2 = jour + 86400
    for k in range(10):
        ms = (j2 + 60 * k) * 1000 - 1_792_800_000_000
        ticks = (_minute(ms, 3, double=True) if k == 9 else
                 _minute(ms, 100, double=True, manque=(5,) if k == 0 else (),
                         triple=(7,) if k == 0 else ()))
        bougie = Candle(P, 60, j2 + 60 * k, ticks[0].price,
                        max(t.price for t in ticks),
                        min(t.price for t in ticks), ticks[-1].price,
                        len(ticks), True)
        minutes.append((P, j2 + 60 * k, ticks, bougie))
    r = dd.comparer(minutes)
    g = r["regimes"]
    assert g["simple"]["minutes"] == 20 and g["doublee"]["minutes"] == 10
    assert r["borne_faux_positifs"] == pytest.approx(1 / 2001)
    assert r["multiplicites"]["3"] == 1
    assert 0 < r["orphelins_doublees"] < 0.01
    assert r["ohlc_change"] == {"o": 0, "h": 0, "l": 0, "c": 0}
    a, e = r["politiques"]["aucune"], r["politiques"]["experimentale"]
    assert e["decroissants"] == 0, "l'ordre temporel ne bouge pas"
    assert a["tick_count_egal"] == 10 and e["tick_count_egal"] == 0, \
        "les bougies stockées comptent les doublons"
    assert a["minutes_pauvres"] == 0 and e["minutes_pauvres"] == 1
    assert r["pauvres_changent"] == 1
    assert e["ecarts_nuls"] < a["ecarts_nuls"]
    # EXP-001 signale le jour doublé pour débit anormal ; en ticks
    # distincts, plus rien.
    assert [x[1] for x in a["jours_debit_anormal"]] == [jour + 86400]
    assert e["jours_debit_anormal"] == []
    t = dd.texte(r)
    for etiquette in ("[Fait observé]", "[Borne]", "[Non testable]"):
        assert etiquette in t


def test_l_outil_lit_sans_ecrire(tmp_path):
    """L'outil compare en mémoire : la base n'est pas modifiée d'un octet."""
    from outils.doublons_ticks import comparer_en_base
    conn = open_read_write(tmp_path / "m.db")
    debut = 1_792_800_000 // 60 * 60
    agg = CandleAggregator(garder_ticks=True)
    for instant, prix in _flux(debut, n=10):
        tick = Tick(P, int(instant * 1000), prix)
        agg.add(tick)
        agg.add(tick)                            # deux sockets, comme avant
    MarketWriter(conn).upsert_candles(agg.drain_all())
    MarketWriter(conn).insert_tick_paths(
        [encoder(p) for p in agg.drain_ticks()])
    conn.commit()
    conn.close()
    chemin = tmp_path / "m.db"
    avant = hashlib.sha256(chemin.read_bytes()).hexdigest()
    lecture = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    r = comparer_en_base(lecture, debut, debut + 60, [P])
    lecture.close()
    assert hashlib.sha256(chemin.read_bytes()).hexdigest() == avant
    assert r["politiques"]["aucune"]["ticks"] == 20
    assert r["politiques"]["experimentale"]["ticks"] == 10
    assert r["politiques"]["aucune"]["tick_count_egal"] == 1
