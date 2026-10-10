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


def test_la_serie_de_bougies_est_lue_dans_le_sens_du_trade():
    bougies = [_b(i, 1.1000, 1.0999) for i in range(20)]       # rouges
    bougies += [_b(20 + k, 1.1000 + 0.0001 * k, 1.1001 + 0.0001 * k)
                for k in range(5)]                               # 5 vertes
    achat = contexte(bougies, True)
    vente = contexte(bougies, False)
    assert achat["serie_sens"] == 5.0
    assert vente["serie_sens"] == -5.0
    assert achat["amplitude_serie"] > 0 > vente["amplitude_serie"]
    assert achat["alternances_10"] == 1.0, "une seule bascule rouge → verte"


def test_la_persistance_compte_ce_qui_suit_une_serie():
    from maxprofit.apprentissage.persistance import Persistance
    from maxprofit.live.experiences import texte_persistance
    # Motif répété : 3 vertes puis 1 rouge. Après 1 ou 2 vertes, la suivante
    # est verte ; après 3, elle est rouge.
    bougies, prix = [], 1.1
    for i in range(400):
        verte = i % 4 != 3
        o, c = prix, prix + (0.0002 if verte else -0.0001)
        bougies.append(_b(i, o, c))
        prix = c
    p = Persistance(T0 + 280 * 60, echeances=(60,))
    p.ajouter(bougies)
    r = p.resultat()
    deux = r["verte"]["2"]["60"]
    assert deux[1] == deux[0] and deux[3] == deux[2], "toujours continuée"
    trois = r["verte"]["3"]["60"]
    assert trois[1] == 0 and trois[0] > 0, "toujours retournée"
    sortie = texte_persistance(r)
    assert "✅" in sortie and "🔁" in sortie
    assert not re.search(r"<(?!/?b>)", sortie)


def test_un_trou_de_collecte_interrompt_la_serie():
    from maxprofit.apprentissage.persistance import Persistance
    bougies = [_b(0, 1.1, 1.1001), _b(1, 1.1001, 1.1002),
               _b(5, 1.1002, 1.1003), _b(6, 1.1003, 1.1004)]
    p = Persistance(T0 + 10_000, echeances=(60,))
    p.ajouter(bougies)
    r = p.resultat()
    assert "3" not in r["verte"], "la série repart à 1 après le trou"


def test_la_sequence_est_jugee_a_chaque_echeance():
    exemples = _reference()
    for i, e in enumerate(exemples):
        e.contexte["e_serie_sens"] = float(1 + i % 5)
    r = experiences(exemples, lambda e: True, fenetre=3)
    v = next(x for x in r["variables"] if x["cle"] == "serie_sens")
    assert v["par_echeance"] and "60" in v["par_echeance"][0]
    assert "⏱" in texte(r)


def test_une_case_de_persistance_sur_un_seul_cas_recent_n_est_pas_marquee():
    from maxprofit.live.experiences import texte_persistance
    r = {"coupure": T0, "n_max": 8, "verte": {"7": {"120": [317, 169, 1, 1]}},
         "rouge": {}}
    assert "Au-delà du seuil" not in texte_persistance(r)
    r["verte"]["7"]["120"] = [317, 169, 40, 25]
    assert "Au-delà du seuil" in texte_persistance(r)


def test_le_diagnostic_dit_quand_le_rejeu_ne_voit_pas_le_recent():
    from maxprofit.live.experiences import texte_donnees
    persistance = {"coupure": T0 + 20 * 86400, "fin": T0 + 30 * 86400,
                   "donnees": {"EURUSD_otc": [40000, 39000, T0,
                                              T0 + 21 * 86400, 1440],
                               "GBPUSD_otc": [30000, 29000, T0,
                                              T0 + 12 * 86400, 0]}}
    sortie = texte_donnees(persistance)
    assert "68000 bougies closes" in sortie
    assert "il ne voit pas les données récentes" in sortie
    assert "GBPUSD" in sortie and "EURUSD" not in sortie.split("6 h")[-1]
    assert not re.search(r"<(?!/?b>)", sortie)
