"""Entrée tardive, fatigue de zone, et expériences contrôlées autour de la
référence jouée."""

from __future__ import annotations

import re

from maxprofit.apprentissage.contexte import contexte
from maxprofit.apprentissage.lecons import Apprentissage, Exemple, autopsie
from maxprofit.core.types import Candle
from maxprofit.live.experiences import experiences, situer, texte
from maxprofit.live.laboratoire import confirmes

T0 = 1_790_000_000 // 60 * 60


def _b(i, o, c, haut=None, bas=None, pair="EURUSD_otc"):
    return Candle(pair=pair, tf_sec=60, ts_sec=T0 + 60 * i, open=o,
                  high=haut if haut is not None else max(o, c),
                  low=bas if bas is not None else min(o, c), close=c,
                  tick_count=10, complete=True)


def test_la_fatigue_de_zone_se_mesure_a_l_entree():
    # 30 bougies plates autour de 1,1000, puis un rebond timide sur le
    # support 1,0995 et une grande bougie verte d'entrée.
    bougies = [_b(i, 1.1000, 1.1000 + (0.0001 if i % 2 else -0.0001))
               for i in range(30)]
    bougies.append(_b(30, 1.1000, 1.1012))
    ctx = contexte(bougies, True, {"niveau": 1.0995})
    assert ctx["distance_niveau"] > 5, "entrée loin au-dessus du support"
    # Les 15 bougies AVANT l'entrée ne sont pas montées au-delà de 1,1001.
    assert 3 < ctx["rebond_15"] < 6
    assert abs(ctx["favorables_15"] - 8 / 15) < 1e-3


def _ex(i, gagne, **ctx):
    base = {"mouvement_heure": 0.0, "retournement_suivant": 1.0,
            "gagne_suivant": float(gagne), "attente_confirmation": 1.0,
            "gagne_confirme": float(gagne)}
    base.update(ctx)
    return Exemple(ts_sec=T0 + i * 1800, pair=f"P{i % 3}_otc",
                   contexte=base, gagne=gagne)


def test_l_entree_confirmee_remplace_le_contexte_du_signal():
    e = _ex(0, True, distance_niveau=0.2, e_distance_niveau=4.8,
            gagne_confirme_60=0.0, gagne_confirme_900=1.0)
    (entree,) = confirmes([e], 3)
    assert entree.contexte["distance_niveau"] == 4.8
    assert entree.issues == {60: False, 900: True}
    assert entree.ts_sec == e.ts_sec + 60


def test_le_rejeu_mesure_la_distance_sur_la_bougie_qui_confirme():
    from maxprofit.apprentissage.historique import _confirmation_m1
    serie = [_b(i, 1.1000, 1.1000 + (0.0001 if i % 2 else -0.0001))
             for i in range(30)]
    serie += [_b(30, 1.1001, 1.0999, haut=1.1002, bas=1.0996),
              _b(31, 1.0999, 1.1012)]
    serie += [_b(i, 1.1012, 1.1015) for i in range(32, 50)]
    closes = {b.ts_sec: b.close for b in serie}
    sortie = _confirmation_m1(serie, 30, True, closes, 900,
                              {"niveau": 1.0995})
    assert sortie["attente_confirmation"] == 1.0
    assert sortie["e_distance_niveau"] > 5
    assert sortie["gagne_confirme_60"] == 1.0


def _reference(n=300, effet=True):
    """Les entrées lointaines perdent si `effet`, sinon tout est au hasard."""
    exemples = []
    for i in range(n):
        distance = (i % 3) * 2.0 + 0.5 + (i % 7) * 0.01   # ~0,5 / 2,5 / 4,5
        if effet:
            gagne = (i % 10 < 4) if distance > 4 else (i % 10 < 7)
        else:
            gagne = i % 10 < 6
        exemples.append(_ex(i, gagne, e_distance_niveau=distance,
                            attente_confirmation=float(1 + i % 2),
                            gagne_confirme_60=float(i % 2 == 0)))
    return exemples


def test_le_tiers_des_entrees_tardives_est_designe_et_juge():
    r = experiences(_reference(), lambda e: True, fenetre=3)
    v = next(x for x in r["variables"] if x["cle"] == "distance_niveau")
    assert v["pire"] == 2, "le tiers le plus lointain"
    assert v["verdict"].startswith("✅")
    assert set(r["delai"]) == {"1", "2"}
    assert r["echeances"]["60"][1] + r["echeances"]["60"][3] == 150
    sortie = texte(r)
    assert "entrée tardive" in sortie
    assert not re.search(r"<(?!/?b>)", sortie)


def test_sans_effet_rien_n_est_dit_meilleur():
    r = experiences(_reference(effet=False), lambda e: True, fenetre=3)
    v = next(x for x in r["variables"] if x["cle"] == "distance_niveau")
    assert not v["verdict"].startswith("✅")


def test_l_autopsie_nomme_l_entree_tardive_au_lieu_de_la_variance():
    r = experiences(_reference(), lambda e: True, fenetre=3)
    a = Apprentissage(n=300, taux=0.6, laboratoire={"experiences": r})
    ctx = {"distance_niveau": 5.2, "rebond_15": 1.1, "favorables_15": 0.27,
           "mouvement_heure": 2.8, "elan_30m": -1.9}
    sortie = autopsie([("GBPUSD_otc", "call", ctx)], a)
    assert "à l'entrée : distance à la zone +5.2 ampl." in sortie
    assert "DÉFAVORABLE MESURÉ" in sortie
    assert "c'est la variance" not in sortie
    proche = autopsie([("GBPUSD_otc", "call", {**ctx,
                                               "distance_niveau": 0.4})], a)
    assert "c'est la variance" in proche
    assert situer({"distance_niveau": 5.2}, r)[0][4] is True


def test_la_legende_montre_l_entree():
    from maxprofit.core.types import Direction, Signal
    from maxprofit.live.plan_demo import texte_du_contexte
    s = Signal(pair="X", direction=Direction.CALL,
               decided_at_ms=1_789_000_020_000, expiry_sec=900)
    sortie = texte_du_contexte({"distance_niveau": 5.2, "rebond_15": 1.1,
                                "favorables_15": 4 / 15}, s)
    assert "entrée à +5.2 ampl. de la zone" in sortie
    assert "4/15 bougies dans le sens du trade" in sortie


def test_le_filtre_d_entree_tardive_est_coupe_par_defaut(monkeypatch):
    from maxprofit.live.plan_demo import distance_entree_max
    monkeypatch.delenv("DISTANCE_ENTREE_MAX", raising=False)
    assert distance_entree_max() is None
    monkeypatch.setenv("DISTANCE_ENTREE_MAX", "3.5")
    assert distance_entree_max() == 3.5
