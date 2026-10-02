"""Les critères de validation : ils mesurent, ils ne décident pas."""

from __future__ import annotations

import math

from maxprofit.core.types import Candle
from maxprofit.strategies.criteres import AUCUN_OBSTACLE, criteres

T0 = 1_790_000_000 // 3600 * 3600


def _vagues(n=300, montee=0.0):
    """Des vagues de 60 min, éventuellement sur une pente."""
    serie = []
    for i in range(n):
        a = 1.1 + 0.003 * math.sin(2 * math.pi * i / 60) + montee * i
        b = 1.1 + 0.003 * math.sin(2 * math.pi * (i + 1) / 60) \
            + montee * (i + 1)
        serie.append(Candle(pair="X", tf_sec=60, ts_sec=T0 + 60 * i,
                            open=round(a, 5), high=round(max(a, b) + 2e-5, 5),
                            low=round(min(a, b) - 2e-5, 5), close=round(b, 5),
                            tick_count=10, complete=True))
    return serie


def test_la_structure_zigzag_suit_la_pente():
    hausse = _vagues(montee=0.00002)
    assert criteres(hausse, True, None)["zigzag_structure"] == 1.0
    assert criteres(hausse, False, None)["zigzag_structure"] == -1.0


def test_une_zone_sur_un_creux_du_zigzag_est_reconnue():
    serie = _vagues()
    creux = min(b.low for b in serie[-60:])
    assert criteres(serie, True, creux)["zigzag_pivot_zone"] == 1.0
    assert criteres(serie, True, creux + 0.002)["zigzag_pivot_zone"] == 0.0


def test_sans_niveau_casse_pas_d_obstacle():
    sortie = criteres(_vagues(), True, None)
    assert sortie["obstacle_inverse"] == AUCUN_OBSTACLE or \
        sortie["obstacle_inverse"] > 0


def test_les_criteres_exiges_se_lisent_et_refusent(monkeypatch):
    from maxprofit.live.plan_demo import criteres_exiges, criteres_refuses
    monkeypatch.delenv("CRITERES", raising=False)
    assert criteres_exiges() == ()
    monkeypatch.setenv("CRITERES", "structure_zigzag, inconnu,sans_obstacle")
    exiges = criteres_exiges()
    assert exiges == ("structure_zigzag", "sans_obstacle")
    assert criteres_refuses(exiges, {"zigzag_structure": -1.0}) \
        == "structure_zigzag"
    assert criteres_refuses(exiges, {"zigzag_structure": 1.0,
                                     "obstacle_inverse": 99.0}) is None


def test_le_laboratoire_juge_les_criteres_contre_la_variante_8():
    from maxprofit.apprentissage.lecons import Exemple
    from maxprofit.live.laboratoire import Mesure, laboratoire
    exemples = []
    for i in range(400):
        ctx = {"heure_utc": 1.0, "retournement_suivant": 1.0,
               "gagne_suivant": float(i % 3 != 0),
               "zigzag_structure": 1.0 if i % 2 else -1.0}
        if i % 2:
            ctx["gagne_suivant"] = float(i % 10 != 1)
        exemples.append(Exemple(T0 + 1800 * i, "A_otc", ctx, i % 2 == 0))
    r = laboratoire(exemples, None, lambda e: True)
    m = {d["nom"]: Mesure.from_dict(d) for d in r["mesures"]}
    assert m["8b. + structure ZigZag dans le sens du trade"].verdict \
        .startswith("✅")
