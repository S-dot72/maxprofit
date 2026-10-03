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


def test_le_bilan_compare_les_ordres_gardes_et_ecartes_par_8e():
    import time as _t
    from maxprofit.execution.journal import Execution
    from maxprofit.live.plan_demo import texte_du_bilan

    def ordre(i, gagne, obstacle):
        return Execution(
            pair="EURUSD_otc", sens="call", mise=1.6,
            signal_ts_ms=1_789_000_000_000 + i, prix_attendu=1.1,
            payout_flux_pct=92.0, expiration_sec=900,
            clic_ts_ms=1_789_000_000_000 + i * 1000, accepte=True,
            accepte_ts_ms=1_789_000_000_000 + i * 1000, order_id=f"o{i}",
            resultat="win" if gagne else "loose",
            profit=1.47 if gagne else -1.6,
            brut={"contexte": {"pas": 1, "obstacle_inverse": obstacle}})

    ordres = [ordre(i, i % 4 != 0, 99.0) for i in range(8)] + \
             [ordre(10 + i, i % 2 == 0, 1.5) for i in range(4)]
    texte = texte_du_bilan(ordres, int(_t.time()))
    assert "8e ✅ gardés : 6/8" in texte
    assert "8e ⛔ qu'il aurait écartés : 2/4" in texte


def test_la_legende_dit_si_8e_aurait_garde_l_ordre():
    from maxprofit.core.types import Direction, Signal
    from maxprofit.live.plan_demo import texte_du_contexte
    s = Signal(pair="X", direction=Direction.CALL,
               decided_at_ms=1_789_000_020_000, expiry_sec=900)
    assert "8e ⛔ niveau cassé à 1.5" in texte_du_contexte(
        {"obstacle_inverse": 1.5}, s)
    assert "8e ✅ aucun niveau cassé devant" in texte_du_contexte(
        {"obstacle_inverse": 99.0}, s)


def _ordre(i, gagne):
    from maxprofit.execution.journal import Execution
    return Execution(
        pair="EURUSD_otc", sens="call", mise=1.6,
        signal_ts_ms=1_789_000_000_000 + i, prix_attendu=1.1,
        payout_flux_pct=92.0, expiration_sec=900,
        clic_ts_ms=1_789_000_000_000 + i * 1000, accepte=True,
        accepte_ts_ms=1_789_000_000_000 + i * 1000, order_id=f"o{i}",
        resultat="win" if gagne else "loose",
        profit=1.47 if gagne else -1.6, brut={"contexte": {"pas": 1}})


def test_le_bilan_compte_les_pertes_d_affilee():
    import re
    import time as _t
    from maxprofit.live.plan_demo import texte_du_bilan
    # G G P P P G P G : une série de 3, une de 1.
    issues = [True, True, False, False, False, True, False, True]
    texte = texte_du_bilan([_ordre(i, g) for i, g in enumerate(issues)],
                           int(_t.time()))
    assert "Juste après une perte : 2/4 perdus" in texte
    assert "Séries de 2 pertes ou plus : 1" in texte
    assert "Séries de 3 pertes ou plus : 1" in texte
    assert "Plus longue série : 3 perte(s)" in texte
    assert "trop peu d'ordres" in texte
    assert not re.search(r"<(?!/?b>)", texte)


def test_des_pertes_groupees_sont_dites_enchainees():
    import time as _t
    from maxprofit.live.plan_demo import texte_du_bilan
    # Des blocs de 4 pertes puis 6 gains : les pertes s'enchaînent.
    issues = [i % 10 >= 4 for i in range(100)]
    texte = texte_du_bilan([_ordre(i, g) for i, g in enumerate(issues)],
                           int(_t.time()))
    assert "S'ENCHAÎNENT" in texte
    # Alternées : aucune perte ne suit une perte.
    alternes = [i % 2 == 0 for i in range(100)]
    texte = texte_du_bilan([_ordre(i, g) for i, g in enumerate(alternes)],
                           int(_t.time()))
    assert "pas d'enchaînement" in texte
