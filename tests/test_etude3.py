"""
Étape 3 — `/micro_essai` de bout en bout sur une base SQLite écrite par le
vrai agrégateur : rapport, fiches, contrôle sur les ordres réels, puissance
depuis l'exploration, et base inchangée (lecture seule prouvée).
"""

from __future__ import annotations

import hashlib
import random
from types import SimpleNamespace

import pytest

from maxprofit.apprentissage import microstructure_pre as mp
from maxprofit.collect.collector import CandleAggregator
from maxprofit.core.types import Direction, PairInfo, Signal, Tick
from maxprofit.live import etude3
from maxprofit.store.chemin_ticks import encoder
from maxprofit.store.db import open_read_only, open_read_write
from maxprofit.store.diagnostic_stockage import lecture_seule
from maxprofit.store.market import MarketWriter

P = "EURUSD_otc"
JOUR = 1_792_454_400          # 20/10/2026 00:00 UTC, dans l'exploration
HEURE_BROKER_MS = 2 * 3_600_000


def _flux(debut: int, fin: int, pas_ms: int = 2000) -> list[Tick]:
    alea = random.Random(9)
    prix, ticks = 1.08000, []
    for ms in range(debut * 1000, fin * 1000, pas_ms):
        prix = round(prix + alea.choice((-2, -1, 0, 1, 2)) * 0.00001, 5)
        ticks.append(Tick(P, ms + 137, prix))
    return ticks


class _Strategie:
    """Un signal CALL toutes les 97 minutes, niveau juste sous le prix."""

    def __init__(self, p) -> None:
        self.p = p

    def on_bar(self, vue):
        derniere = vue.candles(1)[-1]
        if (derniere.ts_sec // 60) % 97:
            return None
        return Signal(pair=vue.pair, direction=Direction.CALL,
                      decided_at_ms=vue.now_ms, expiry_sec=900,
                      features={"niveau": round(derniere.close - 0.00005, 5),
                                "touches": 2.0,
                                "entrees_deja_offertes": 0.0})


def _course():
    p = SimpleNamespace(lookback=30, expiry_sec=900, tolerance_pct=0.02)
    return SimpleNamespace(strategie=_Strategie(p), paires_collectees=(P,),
                           rejeu_en_memoire=None)


def _base(tmp_path):
    chemin = tmp_path / "m.db"
    conn = open_read_write(chemin)
    ecrivain = MarketWriter(conn)
    flux = _flux(JOUR - 6 * 3600, JOUR + 86400 + 1800)
    agg = CandleAggregator(garder_ticks=True)
    for t in flux:
        agg.add(t)
    ecrivain.upsert_candles(agg.drain_all())
    ecrivain.insert_tick_paths([encoder(p) for p in agg.drain_ticks()])
    ecrivain.insert_payouts(JOUR - 7 * 3600, [PairInfo(P, True, 92)])
    par_instant = [(t.ts_ms, t.price) for t in flux]

    def dernier_avant(ms):
        return max((x for x in par_instant if x[0] <= ms))[1]

    # Trois ordres : deux comparables (dont un au prix absent du flux), un
    # rapatrié du broker (exclu).
    for k, (decalage_ms, prix, brut) in enumerate((
            (1500, None, "{}"), (2400, 1.5, "{}"),
            (1800, None, '{"rapatrie_du_broker": true}'))):
        instant = JOUR + 3600 * (k + 2)
        local = instant * 1000 + decalage_ms - 350
        attendu = prix or dernier_avant(instant * 1000 + decalage_ms)
        conn.execute(
            "INSERT INTO executions (campagne, pair, sens, mise, "
            "signal_ts_ms, clic_ts_ms, accepte_ts_ms, prix_attendu, "
            "prix_entree, prix_sortie, payout_flux_pct, expiration_sec, "
            "ouverture_ts_ms, expiration_ts_ms, decalage_broker_ms, accepte, "
            "brut) VALUES ('c', ?, 'call', 1, ?, ?, ?, ?, ?, ?, 92, 900, ?, "
            "?, ?, 1, ?)",
            (P, local, local + 3, local + 100, attendu, attendu, attendu,
             local + 200 + HEURE_BROKER_MS + 350,
             local + 900_200 + HEURE_BROKER_MS + 350,
             HEURE_BROKER_MS + 350, brut))
    conn.commit()
    conn.close()
    return chemin


def _empreinte(chemin) -> str:
    return hashlib.sha256(chemin.read_bytes()).hexdigest()


def test_l_essai_de_bout_en_bout_ne_touche_pas_la_base(tmp_path):
    chemin = _base(tmp_path)
    avant = _empreinte(chemin)
    conn = open_read_only(chemin)
    lecture_seule(conn)
    try:
        rapport, fiches = etude3.essai(conn, _course(), "")
    finally:
        conn.close()
    assert _empreinte(chemin) == avant, "l'essai a écrit dans la base"
    for section in ("1. Coût", "2. Couverture", "3. Anti-fuite",
                    "4. Disponibilité", "5. Puissance"):
        assert section in rapport, section
    assert "✅" in rapport, "l'anti-fuite doit passer sur chaque fenêtre"
    assert "2 ordres" in rapport, "le rapatrié est exclu"
    # Ticks toutes les 2 s : le dernier tick peut dater de plus d'une
    # seconde ; il est retrouvé comme « dernier tick » et à ± 2 s.
    assert "même instant : 1/2" in rapport and "± 2 s : 1/2" in rapport
    assert "N = 500" in rapport and "+2 points" in rapport
    noms = [nom for _, nom, _ in fiches]
    assert noms == ["micro_essai.csv", "ordres_disponibilite.csv"]
    entete, *lignes = fiches[0][0].decode().splitlines()
    colonnes = entete.split(";")
    assert "gagne" not in colonnes and "issue" not in colonnes, (
        "l'échantillon ne doit porter aucune issue")
    assert set(mp.VARIABLES) <= set(colonnes)
    assert lignes and len(lignes) % (2 * 3 * 4) == 0


def test_l_echantillon_compte_la_qualite_et_le_niveau_2(tmp_path):
    chemin = _base(tmp_path)
    conn = open_read_only(chemin)
    try:
        ech = etude3.echantillon(conn, _course(), P, JOUR)
    finally:
        conn.close()
    assert ech["minutes"]["presentes"] == 1440 + 15
    assert ech["fiches"], "des signaux dans la journée"
    assert ech["classes"] == {mp.VALID: len(ech["fiches"])}
    assert ech["niveau_2"] == len(ech["fiches"])
    assert ech["autotests"][0] == ech["autotests"][1] > 0
    assert set(ech["temps"]) == {"lecture", "decodage", "concordance",
                                 "signaux", "variables"}


def test_les_ordres_reels_face_au_flux(tmp_path):
    chemin = _base(tmp_path)
    conn = open_read_only(chemin)
    try:
        from maxprofit.live.ticks import lire_ordres
        from maxprofit.store.market import MarketReader
        r = etude3.ordres_reels(lire_ordres(conn), MarketReader(conn).ticks)
    finally:
        conn.close()
    assert r["n"] == 2
    assert r["retard_ms"] == (1500, 1950, 2400)
    premier, second = r["fiches"]
    assert premier["trouve_0ms"] and premier["trouve_2000ms"]
    assert not second["trouve_5000ms"], "un prix absent du flux est vu"
    assert premier["ticks_depuis_frontiere"] == 1


def test_la_puissance_ne_lit_que_l_exploration():
    from maxprofit.apprentissage import etude_micro as em
    from maxprofit.apprentissage.lecons import Exemple
    exemples = [Exemple(ts_sec=em.FIN_EXPLORATION - 3600 * k, pair=P,
                        contexte={}, gagne=bool(k % 2)) for k in range(1, 200)]
    apres = [Exemple(ts_sec=em.FIN_EXPLORATION + 60 * k, pair=P, contexte={},
                     gagne=True) for k in range(500)]
    r = etude3.puissance_exploration(exemples + apres, 0.5)
    assert r["paire_jour"]["n"] == 199
    assert 0.45 < r["paire_jour"]["p"] < 0.55, "rien d'après le 21/10"
    assert etude3.puissance_exploration(apres, 0.5) is None


@pytest.mark.parametrize("argument, attendu", [
    ("", (P, JOUR)),
    ("2026-10-19", (P, JOUR - 86400)),
    ("EURUSD_otc 2026-10-20", (P, JOUR)),
])
def test_les_arguments(argument, attendu):
    assert etude3.analyser(argument, (P,)) == attendu


@pytest.mark.parametrize("argument", ["2026-10-21", "2026-10-25",
                                      "GBPUSD_otc", "2026-13-01"])
def test_les_arguments_refuses(argument):
    with pytest.raises(ValueError):
        etude3.analyser(argument, (P,))
