"""Le potentiel de zone, l'expiration dynamique et l'entrée au repli."""

from __future__ import annotations

import re

import pytest

from maxprofit.apprentissage import potentiel as pz
from maxprofit.apprentissage.lecons import Exemple
from maxprofit.core.types import Candle
from maxprofit.live import potentiel as rapport

T0 = 1_791_936_000


def _b(i: int, o: float, h: float, b: float, c: float) -> Candle:
    return Candle(pair="X_otc", tf_sec=60, ts_sec=T0 + 60 * i, open=o,
                  high=h, low=b, close=c, tick_count=20, complete=True)


# --------------------------------------------------------------------------- #
# Les figures de chandeliers
# --------------------------------------------------------------------------- #

def test_les_figures():
    rouge = _b(0, 1.10, 1.105, 1.095, 1.098)
    assert pz.motif(rouge, _b(1, 1.097, 1.106, 1.096, 1.104)) == 1
    verte = _b(0, 1.098, 1.105, 1.095, 1.102)
    assert pz.motif(verte, _b(1, 1.103, 1.104, 1.094, 1.096)) == 2
    # Marteau : petit corps en haut, longue mèche basse.
    assert pz.motif(None, _b(1, 1.100, 1.1012, 1.096, 1.101)) == 3
    # Étoile filante : l'inverse.
    assert pz.motif(None, _b(1, 1.100, 1.104, 1.0988, 1.099)) == 4
    assert pz.motif(None, _b(1, 1.100, 1.102, 1.098, 1.1001)) == 5
    assert pz.motif(None, _b(1, 1.100, 1.103, 1.099, 1.102)) == 0
    assert pz.favorable(1, True) and pz.favorable(3, True)
    assert pz.favorable(2, False) and pz.favorable(4, False)
    assert not pz.favorable(5, True) and not pz.favorable(1, False)


# --------------------------------------------------------------------------- #
# La règle d'expiration et les catégories
# --------------------------------------------------------------------------- #

def test_horizon_au_plus():
    assert pz.horizon_au_plus(30) == 15
    assert pz.horizon_au_plus(15) == 15
    assert pz.horizon_au_plus(7.5) == 5
    assert pz.horizon_au_plus(3.2) == 3
    assert pz.horizon_au_plus(0.4) == 1


@pytest.mark.parametrize("duree, part, espace, percees, attendu", [
    (20, 0.2, 3.0, 0, (15, 0)),             # propice à 15 min
    (3.2, 0.2, 3.0, 0, (3, 1)),              # rebonds courts
    (20, 0.6, 3.0, 0, (5, 2)),               # entrée tardive
    (20, 0.2, 1.0, 0, (5, 4)),               # obstacle proche
    (20, 0.2, 3.0, 2, (5, 8)),               # zone instable
    (20, 0.6, 1.0, 3, (5, 14)),
    (4, 0.6, 3.0, 0, (4, 3)),                # déjà sous 5 : on garde 4
    (20, None, None, None, (15, 0)),         # rien ne raccourcit
    (None, 0.2, 3.0, 0, (None, 0)),
])
def test_la_regle_d_expiration(duree, part, espace, percees, attendu):
    assert pz.expiration(duree, part, espace, percees) == attendu


def test_les_categories():
    assert pz.categorie(None, 3.0, 0, 10) == 4
    assert pz.categorie(5.0, 3.0, 2, 10) == 3
    assert pz.categorie(1.5, 3.0, 0, 10) == 0
    assert pz.categorie(5.0, 0.5, 0, 10) == 0
    assert pz.categorie(5.0, 3.0, 0, 10) == 2
    assert pz.categorie(3.0, 3.0, 0, 10) == 1


# --------------------------------------------------------------------------- #
# Les mesures à l'entrée
# --------------------------------------------------------------------------- #

def _chute_puis_rebond(n: int = 200) -> list[Candle]:
    """Une baisse régulière qui clôture au plus bas, une mèche qui perce
    sous 1,0800 puis se referme, et une bougie verte qui casse le haut
    de la précédente (la confirmation)."""
    bougies, prix = [], 1.1000
    for i in range(n - 1):
        o = prix
        prix -= 0.0001
        # La percée tombe dans l'heure, mais hors de la fenêtre du
        # Stochastic : celui-ci voit une chute qui clôture au plus bas.
        bas = prix - (0.0006 if i == n - 30 else 0.00002)
        bougies.append(_b(i, o, o + 0.00002, bas, prix))
    signal = bougies[-1]
    bougies.append(_b(n - 1, signal.close, signal.high + 0.0002,
                      signal.close - 0.00001, signal.high + 0.00015))
    return bougies


def test_les_mesures_a_l_entree():
    serie = _chute_puis_rebond()
    i, j = len(serie) - 2, len(serie) - 1
    niveau = serie[i].close + 0.00005
    m = pz.potentiel_a_l_entree(serie, i, j, True, niveau, part=0.2,
                                espace=3.0)
    # Une chute qui clôture au plus bas : survente, sous la ligne de 10.
    assert m["stoch_k_signal"] <= 10 and m["stoch_extreme"] == 1.0
    # EMA 20 sous EMA 50 : tendance baissière, CONTRE un achat.
    assert m["tendance_ema"] == -1.0
    assert m["percees_zone"] >= 1.0
    assert m["amplitude_15"] > 0 and m["categorie_potentiel"] in range(5)
    assert m["motif_entree"] in (0.0, 1.0, 3.0)
    # Le même signal, vu en vente : la tendance va dans son sens.
    vente = pz.potentiel_a_l_entree(serie, i, j, False, None)
    assert vente["tendance_ema"] == 1.0 and "percees_zone" not in vente


def test_une_fenetre_trouee_ne_mesure_rien():
    serie = _chute_puis_rebond(40)
    serie = serie[:20] + serie[25:]                 # cinq minutes absentes
    m = pz.potentiel_a_l_entree(serie, len(serie) - 2, len(serie) - 1,
                                True, None)
    assert "tendance_ema" not in m              # 150 minutes contiguës


def test_l_entree_au_repli():
    serie = [_b(0, 1.1, 1.1005, 1.0995, 1.1),          # signal, clôture 1,1
             _b(1, 1.1, 1.1012, 1.0999, 1.101),        # confirmation
             _b(2, 1.101, 1.1011, 1.0995, 1.0999),     # 1 min après : 1,0999
             *[_b(k, 1.1, 1.1003, 1.0999, 1.1002) for k in range(3, 20)]]
    closes = {c.ts_sec: c.close for c in serie}
    r = pz.entree_au_repli(serie, 0, 1, True, 1.0990, closes, 900)
    assert r["attente_repli"] == 1.0 and r["gagne_repli"] == 1.0
    # La zone cassée avant le retour : on renonce.
    casse = serie[:2] + [_b(2, 1.101, 1.1011, 1.0980, 1.0985)] + serie[3:]
    r = pz.entree_au_repli(casse, 0, 1, True, 1.0990,
                           {c.ts_sec: c.close for c in casse}, 900)
    assert r == {"attente_repli": 0.0}


# --------------------------------------------------------------------------- #
# Le rapport
# --------------------------------------------------------------------------- #

def _entree(k: int) -> Exemple:
    """Une entrée confirmée synthétique : les « en retard » perdent."""
    tard = k % 4 == 0
    gagne = not tard and k % 3 != 0
    ctx = {"mouvement_heure": 0.0, "retournement_suivant": 1.0,
           "gagne_suivant": float(gagne), "attente_confirmation": 1.0,
           "gagne_confirme": float(gagne), "sens_call": float(k % 2),
           "gagne_confirme_300": float(k % 2 == 0),
           "gagne_confirme_600": float(gagne),
           "e_part_parcourue": 0.8 if tard else 0.2,
           "e_espace_obstacle": 0.5 if tard else 3.0,
           "e_taille_bougie": 1.5 if k % 5 == 0 else 1.0,
           "e_duree_rebond": 12.0, "e_expiration_dynamique":
               300.0 if tard else 900.0,
           "e_propice_15": 0.0 if tard else 1.0,
           "e_raisons_expiration": 6.0 if tard else 0.0,
           "e_categorie_potentiel": 3.0 if tard else 2.0,
           "e_tendance_ema": 1.0 if k % 2 else -1.0,
           "e_stoch_extreme": 1.0 if k % 7 == 0 else 0.0,
           "e_stoch_k_signal": 5.0 if k % 7 == 0 else 50.0,
           "e_motif_favorable": 1.0 if k % 3 == 1 else 0.0,
           "e_motif_signal": 3.0 if k % 3 == 1 else 0.0,
           "mfe_15": 1.5, "mae_15": 0.8, "t_mfe": 6.0,
           "attente_repli": 2.0 if k % 2 else 0.0,
           "gagne_repli": float(k % 4 != 1)}
    return Exemple(ts_sec=T0 + k * 3600, pair="X_otc", contexte=ctx,
                   gagne=gagne)


def test_le_rapport_compare_les_memes_entrees():
    exemples = [_entree(k) for k in range(240)]
    r = rapport.potentiel(exemples, lambda e: True, 1)
    assert r["n"] == 240
    ids = [x["id"] for x in r["expirations"]]
    assert ids == ["15", "5", "RYT", "DYN", "P15", "P15/5"]
    p15 = next(x for x in r["expirations"] if x["id"] == "P15")
    assert p15["compte"][0] + p15["compte"][2] == 180   # les non tardives
    assert r["repartition"] == {"5": 60, "15": 180}
    assert r["raisons"] == {"entrée tardive": 60, "obstacle proche": 60}
    filtres = {x["id"]: x for x in r["filtres"]}
    assert filtres["A"]["verdict"] is None
    n = {k: x["compte"][0] + x["compte"][2] for k, x in filtres.items()}
    assert n["A"] == 240 and n["+C"] == n["+D"] == n["+C+D"] == 180
    assert n["+B+C"] < n["+C"]
    indic = {x["id"]: x for x in r["indicateurs"]}
    assert indic["EMA"]["achats"][0] == 120 and indic["EMA"]["ventes"][0] == 0
    assert r["descriptif"]["stoch_sous_10"] == 35
    assert r["repli"]["servies"] == 120
    cats = {x["categorie"] for x in r["categories"]}
    assert cats == {"élevé", "instable"}
    journal = rapport.hypotheses(r)
    assert any(h[0] == "EXP DYN" for h in journal)
    assert any(h[0] == "ZONE instable" for h in journal)
    assert any(h[0] == "REPLI" for h in journal)
    sortie = rapport.texte(r)
    for titre in ("1. Expiration", "2. Potentiel", "3. Entrée tardive",
                  "4. EMA", "5. Entrée au repli"):
        assert titre in sortie
    nu = re.sub(r"</?b>", "", sortie)
    assert not re.search(r"[<>&]", nu)


def test_trop_peu_d_entrees():
    r = rapport.potentiel([_entree(k) for k in range(10)], lambda e: True, 1)
    assert "note" in r and rapport.hypotheses(r) == []
    assert rapport.texte(None).startswith("🎯")
