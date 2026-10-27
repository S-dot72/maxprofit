"""
Étape 3 — les variables avant la décision : définitions calculées à la
main, frontières de fenêtre, anti-fuite par perturbation, qualité et
niveau de disponibilité.
"""

from __future__ import annotations

import math
import random

import pytest

from maxprofit.apprentissage.audit_ticks import IDENTIQUE, ORDRE
from maxprofit.apprentissage.microstructure_pre import (
    FENETRES_SEC, INVALID, PARTIAL, QUALITE, TRONCATURES_SEC, VALID, VARIABLES,
    Contexte, autotest_coupure, classe_de_fenetre, distincts, ligne_du_signal,
    niveau, pas_de_cotation, qualite, ticks_de_la_fenetre, variables)
from maxprofit.core.types import Tick

PAIRE = "EURUSD_otc"
D = 1_792_541_700             # une décision, multiple de 60
NIVEAU = 1.10000
CTX = Contexte(d_sec=D, s=1, niveau=NIVEAU, tolerance_pct=0.02,
               delta=0.00001)
MU = NIVEAU * 0.0002          # 0,00022


def _par_minute(ticks):
    sortie: dict[int, list[Tick]] = {}
    for t in ticks:
        sortie.setdefault(t.ts_ms // 60_000 * 60, []).append(t)
    return sortie


def _tick(dt_ms: int, prix: float) -> Tick:
    """Un tick à d + dt (dt négatif : avant la décision)."""
    return Tick(PAIRE, D * 1000 + dt_ms, prix)


def _flux(n: int = 900, graine: int = 3, fin_ms: int = -200):
    """Un flux régulier d'un tick par seconde sur les 15 minutes."""
    alea = random.Random(graine)
    prix, ticks = NIVEAU + 3 * MU, []
    for i in range(n):
        prix = round(prix + alea.choice((-1, 0, 1)) * 0.00002, 5)
        ticks.append(_tick(fin_ms - (n - 1 - i) * 1000, prix))
    return ticks


# --------------------------------------------------------------------------- #
# Fenêtres
# --------------------------------------------------------------------------- #

def test_la_fenetre_prend_d_moins_w_et_exclut_d():
    ticks = [_tick(-60_000, 1.1), _tick(-30_000, 1.2), _tick(-1, 1.3),
             _tick(0, 9.0), _tick(500, 9.0)]
    lus = ticks_de_la_fenetre(_par_minute(ticks), D, 60)
    assert [p for _, p in lus] == [1.1, 1.2, 1.3]


def test_la_troncature_retire_les_k_dernieres_secondes():
    ticks = [_tick(-5_001, 1.1), _tick(-5_000, 1.2), _tick(-1_999, 1.3)]
    pm = _par_minute(ticks)
    assert len(ticks_de_la_fenetre(pm, D, 60, k_sec=0)) == 3
    assert [p for _, p in ticks_de_la_fenetre(pm, D, 60, k_sec=2)] == [1.1,
                                                                       1.2]
    assert [p for _, p in ticks_de_la_fenetre(pm, D, 60, k_sec=5)] == [1.1]


def test_les_copies_ne_comptent_qu_une_fois_en_ticks_distincts():
    ticks = [_tick(-3_000, 1.1), _tick(-3_000, 1.1), _tick(-2_000, 1.1),
             _tick(-2_000, 1.2)]
    pm = _par_minute(ticks)
    assert len(ticks_de_la_fenetre(pm, D, 60, traitement="bruts")) == 4
    assert len(ticks_de_la_fenetre(pm, D, 60)) == 3
    assert len(distincts(ticks)) == 3


def test_un_traitement_inconnu_est_refuse():
    with pytest.raises(ValueError):
        ticks_de_la_fenetre({}, D, 60, traitement="moyenne")


def test_variables_refuse_un_tick_hors_de_la_fenetre():
    with pytest.raises(ValueError):
        variables([((D + 1) * 1000, 1.1)], CTX, 60)


def test_le_pas_de_cotation_vient_de_la_plus_grande_echelle():
    assert pas_de_cotation([3, 5, 4]) == pytest.approx(1e-5)
    assert pas_de_cotation([]) is None


# --------------------------------------------------------------------------- #
# Définitions, sur un cas calculé à la main
# --------------------------------------------------------------------------- #

def test_les_definitions_sur_un_cas_calcule_a_la_main():
    """Niveau 1,1 ; μ = 0,00022 ; δ = 0,00001 ; CALL ; fenêtre 60 s.
    Prix : 1,10044 (z=2), 1,10011 (z=0,5), 1,10000 (z=0), 1,09989 (z=−0,5),
    1,09967 (z=−1,5), 1,10022 (z=1)."""
    prix = [1.10044, 1.10011, 1.10000, 1.09989, 1.09967, 1.10022]
    instants = [-50_000, -40_000, -30_000, -20_000, -10_000, -5_000]
    ticks = [((D * 1000) + t, p) for t, p in zip(instants, prix)]
    v = variables(ticks, CTX, 60)
    z = [2.0, 0.5, 0.0, -0.5, -1.5, 1.0]
    assert v["A1_dist_fin"] == pytest.approx(z[-1])
    assert v["A2_dist_debut"] == pytest.approx(z[0])
    assert v["A3_vitesse"] == pytest.approx((1.0 - 2.0) / (45 / 60))
    assert v["A5_contacts"] == 2.0          # entrées au tick 2 et au tick 6
    # en zone : ticks 2, 3, 4 (10 s chacun) et 6 (5 s jusqu'à d)
    assert v["A6_temps_zone"] == pytest.approx(35.0)
    assert v["A7_penetration"] == pytest.approx(1.5)
    assert v["A8_depuis_contact"] == pytest.approx(5.0)
    assert v["A9_apres_contact"] == pytest.approx(0.0)
    dp = [-33, -11, -11, -22, 55]
    assert v["B1_vol"] == pytest.approx(math.sqrt(sum(x * x for x in dp)))
    assert v["B2_amplitude"] == pytest.approx(77)
    assert v["B3_nulles"] == 0.0
    assert v["B4_efficience"] == pytest.approx(22 / 132)
    assert v["C1_retournements"] == 1.0     # quatre baisses, une hausse
    assert v["C2_persistance"] == pytest.approx(5 / 2)
    assert v["C3_part_sens"] == pytest.approx(55 / 132)
    assert v["C4_amplitude_retournement"] is None   # un seul retournement
    assert v["D1_traversees"] == 2.0        # + → − puis − → +
    assert v["D2_sorties_retours"] == 1.0   # sortie au tick 5, retour au 6
    assert v["D3_temps_au_dela"] == pytest.approx(5.0)   # tick 5, 5 s


def test_un_put_retourne_le_signe_des_distances():
    ticks = [(D * 1000 - 10_000, 1.10044), (D * 1000 - 5_000, 1.10022)]
    put = Contexte(d_sec=D, s=-1, niveau=NIVEAU, tolerance_pct=0.02,
                   delta=0.00001)
    assert variables(ticks, put, 60)["A1_dist_fin"] == pytest.approx(-1.0)
    assert variables(ticks, CTX, 60)["A1_dist_fin"] == pytest.approx(1.0)


def test_les_retournements_multiples():
    prix = [1.1, 1.10003, 1.10001, 1.10004, 1.10002]   # +3 −2 +3 −2
    ticks = [(D * 1000 - 45_000 + i * 10_000, p) for i, p in enumerate(prix)]
    v = variables(ticks, CTX, 60)
    assert v["C1_retournements"] == 3.0
    assert v["C4_amplitude_retournement"] == pytest.approx((2 + 3) / 2)
    assert v["C5_delai_retournement"] == pytest.approx(10.0)


def test_une_fenetre_vide_ou_d_un_tick_rend_des_manquantes():
    assert all(x is None for x in variables([], CTX, 60).values())
    un = variables([(D * 1000 - 1_000, 1.1001)], CTX, 60)
    assert un["A1_dist_fin"] is not None
    for nom in ("A3_vitesse", "B1_vol", "B3_nulles", "C1_retournements",
                "C3_part_sens"):
        assert un[nom] is None, nom


def test_toutes_les_variables_sont_nommees_une_fois():
    assert len(VARIABLES) == len(set(VARIABLES)) == 23
    assert set(variables([], CTX, 60)) == set(VARIABLES)


# --------------------------------------------------------------------------- #
# Anti-fuite
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("k", TRONCATURES_SEC)
@pytest.mark.parametrize("w", FENETRES_SEC)
def test_rien_apres_la_coupure_ne_change_une_variable(w, k):
    """Perturbation : ticks ajoutés à d − k, juste après, à d et bien
    après, avec des prix extrêmes. Aucune variable ne bouge."""
    pm = _par_minute(_flux())
    assert autotest_coupure(pm, CTX, PAIRE, w_sec=w, k_sec=k)
    avant = variables(ticks_de_la_fenetre(pm, D, w, k_sec=k), CTX, w, k)
    pollue = _par_minute(_flux() + [
        _tick(-k * 1000, 2.0), _tick(-k * 1000 + 1, 0.5), _tick(0, 2.0),
        _tick(30_000, 0.5), _tick(900_000, 2.0)])
    apres = variables(ticks_de_la_fenetre(pollue, D, w, k_sec=k), CTX, w, k)
    assert avant == apres


def test_l_autotest_detecte_une_fenetre_qui_lirait_apres_la_coupure(
        monkeypatch):
    """Le contrôle doit pouvoir échouer : une fenêtre qui lit jusqu'à d + 1 s
    est prise en faute."""
    import maxprofit.apprentissage.microstructure_pre as mp
    vraie = mp.ticks_de_la_fenetre

    def fuyante(par_minute, d_sec, w_sec, *, k_sec=0, traitement="distincts"):
        return vraie(par_minute, d_sec + 1, w_sec, k_sec=k_sec,
                     traitement=traitement)

    monkeypatch.setattr(mp, "ticks_de_la_fenetre", fuyante)
    monkeypatch.setattr(mp, "variables",
                        lambda ticks, ctx, w, k=0: {"n": len(ticks)})
    assert not mp.autotest_coupure(_par_minute(_flux()), CTX, PAIRE)


# --------------------------------------------------------------------------- #
# Qualité et niveau
# --------------------------------------------------------------------------- #

def _concordances(classe=IDENTIQUE):
    return {D - 900 + 60 * i: classe for i in range(15)}


def test_un_flux_regulier_est_valid_et_de_niveau_2():
    q = qualite(_par_minute(_flux()), D, concordances=_concordances())
    assert q["classe"] == VALID
    assert q["E8_niveau"] == 2
    assert q["E1_couverture"] == 1.0
    assert q["E3_age_dernier_s"] == pytest.approx(0.2)
    assert set(QUALITE) <= set(q)


def test_un_gel_de_douze_secondes_fait_une_fenetre_partial():
    ticks = [t for t in _flux() if not -400_000 < t.ts_ms - D * 1000
             < -388_000]
    q = qualite(_par_minute(ticks), D, concordances=_concordances())
    assert q["E5_gels"] == 1
    assert q["classe"] == PARTIAL


def test_une_minute_absente_ou_mal_concordante_fait_le_niveau_1():
    flux = _flux()
    trou = [t for t in flux if (t.ts_ms // 60_000) * 60 != D - 300]
    q = qualite(_par_minute(trou), D, concordances=_concordances())
    assert q["E8_niveau"] == 1
    assert q["E1_couverture"] == pytest.approx(14 / 15)
    assert q["classe"] == INVALID           # écart de 61 s ≥ 30 s
    conc = _concordances()
    conc[D - 120] = "minute coupée"
    assert qualite(_par_minute(flux), D, concordances=conc)["E8_niveau"] == 1
    conc[D - 120] = ORDRE
    assert qualite(_par_minute(flux), D, concordances=conc)["E8_niveau"] == 2


def test_une_minute_fermee_chez_le_broker_empeche_valid():
    q = qualite(_par_minute(_flux()), D, concordances=_concordances(),
                fermees=[D - 600])
    assert q["E6_minutes_fermees"] == 1
    assert q["classe"] != VALID


def test_la_classe_suit_les_seuils_figes():
    base = {"E1_couverture": 1.0, "E2_ecart_max_s": 9.9,
            "E3_age_dernier_s": 5.0, "E6_minutes_fermees": 0}
    assert classe_de_fenetre(base) == VALID
    assert classe_de_fenetre({**base, "E3_age_dernier_s": 5.1}) == PARTIAL
    assert classe_de_fenetre({**base, "E1_couverture": 0.8,
                              "E2_ecart_max_s": 29.9}) == PARTIAL
    assert classe_de_fenetre({**base, "E1_couverture": 0.79}) == INVALID
    assert classe_de_fenetre({**base, "E2_ecart_max_s": 30}) == INVALID


def test_le_niveau_demande_toutes_les_minutes():
    minutes = [0, 60, 120]
    pm = {0: [1], 60: [1], 120: [1]}
    assert niveau(minutes, pm, {0: IDENTIQUE, 60: ORDRE,
                                120: IDENTIQUE}) == 2
    assert niveau(minutes, pm, {0: IDENTIQUE, 60: None,
                                120: IDENTIQUE}) == 1
    assert niveau(minutes, {0: [1], 120: [1]},
                  {m: IDENTIQUE for m in minutes}) == 1


def test_une_ligne_par_traitement_k_et_w():
    q, lignes = ligne_du_signal(_par_minute(_flux()), CTX,
                                concordances=_concordances())
    assert len(lignes) == 2 * len(TRONCATURES_SEC) * len(FENETRES_SEC)
    assert {(r["traitement"], r["k"], r["w"]) for r in lignes} == {
        (t, k, w) for t in ("distincts", "bruts") for k in TRONCATURES_SEC
        for w in FENETRES_SEC}
    assert q["classe"] == VALID
