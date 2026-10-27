"""
Étape 3 — le protocole statistique : règle d'issue (croisée avec le rejeu),
causalité de la référence, mêmes signaux, bootstrap par blocs, Holm / BH,
verdicts, puissance, règle de poursuite, registre et gel.
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.apprentissage.historique import exemples_historiques
from maxprofit.core.types import Candle, Direction, Signal

PAIRE = "EURUSD_otc"
T0 = 1_792_000_020 // 60 * 60


def _bougie(i, o, c, complete=True):
    return Candle(pair=PAIRE, tf_sec=60, ts_sec=T0 + 60 * i, open=o,
                  high=max(o, c) + 0.0001, low=min(o, c) - 0.0001, close=c,
                  tick_count=10, complete=complete)


def _serie(mouvements, depart=1.1, premier=0):
    prix, sortie = depart, []
    for i, m in enumerate(mouvements, premier):
        sortie.append(_bougie(i, round(prix, 5), round(prix + m, 5)))
        prix = round(prix + m, 5)
    return sortie


class _ToujoursCall:
    p = SimpleNamespace(lookback=25, expiry_sec=900)

    def on_bar(self, vue):
        return Signal(pair=vue.pair, direction=Direction.CALL,
                      decided_at_ms=vue.now_ms, expiry_sec=900,
                      features={"niveau": 1.1, "touches": 2.0,
                                "entrees_deja_offertes": 1.0})


# --------------------------------------------------------------------------- #
# La règle d'issue
# --------------------------------------------------------------------------- #

def test_l_issue_est_la_cloture_de_la_bougie_qui_finit_a_d_plus_h():
    m = T0
    closes = {m: 1.1, m + 900: 1.2, m + 840: 0.9, m + 960: 0.8}
    assert em.issue(closes, m, True) == (em.GAIN, 1.2)
    assert em.issue(closes, m, False) == (em.PERTE, 1.2)
    assert em.issue({m: 1.1, m + 900: 1.1}, m, True) == (em.EGALITE, 1.1)
    assert em.issue({m: 1.1, m + 840: 1.2, m + 960: 1.2}, m, True) == (
        em.MANQUANTE, None), "une voisine ne remplace pas la bougie attendue"


def test_l_issue_refuse_un_instant_hors_grille_ou_sans_bougie_du_signal():
    with pytest.raises(ValueError):
        em.issue({T0: 1.1}, T0 + 30, True)
    with pytest.raises(ValueError):
        em.issue({}, T0, True)
    with pytest.raises(ValueError):
        em.issue({T0: 1.1}, T0, True, horizon_sec=90)


def test_la_regle_d_issue_est_celle_du_rejeu_signal_par_signal():
    """Mêmes issues que `exemples_historiques` sur chaque signal gardé, et
    les signaux écartés (égalité, bougie absente ou incomplète) ressortent
    avec la même issue."""
    alea = random.Random(5)
    mouvements = [alea.choice((-0.0001, 0.0, 0.0001)) for _ in range(300)]
    # Une bougie absente et une incomplète dans la plage des sorties.
    serie = [_bougie((b.ts_sec - T0) // 60, b.open, b.close,
                     complete=b.ts_sec != T0 + 60 * 230)
             for b in _serie(mouvements) if b.ts_sec != T0 + 60 * 200]
    sans_issue: list = []
    exemples = exemples_historiques({PAIRE: serie}, _ToujoursCall(),
                                    echeance_sec=900, sans_issue=sans_issue)
    closes = {b.ts_sec: b.close for b in serie if b.complete}
    assert exemples and sans_issue
    for e in exemples:
        resultat, _ = em.issue(closes, e.ts_sec - 60, True)
        assert resultat == (em.GAIN if e.gagne else em.PERTE)
    motifs = {r["issue"] for r in sans_issue}
    assert motifs == {em.EGALITE, em.MANQUANTE}
    for r in sans_issue:
        assert em.issue(closes, r["ts_sec"] - 60, r["call"])[0] == r["issue"]
        assert "distance_niveau" in r["contexte"]
    # Les sorties sur la bougie absente et sur l'incomplète sont manquantes.
    manquantes = {r["ts_sec"] - 60 + 900 for r in sans_issue
                  if r["issue"] == em.MANQUANTE}
    assert {T0 + 60 * 200, T0 + 60 * 230} <= manquantes


def test_les_egalites_sont_hors_metrique_ou_perdues_en_sensibilite():
    assert em.y_selon(em.GAIN) == 1 and em.y_selon(em.PERTE) == 0
    assert em.y_selon(em.EGALITE) is None
    assert em.y_selon(em.EGALITE, "perdues") == 0
    assert em.y_selon(em.MANQUANTE, "perdues") is None


def test_les_variables_de_reference_ne_lisent_rien_apres_la_decision():
    """Perturbation : on change toutes les bougies après la coupure. Les
    variables de la référence des signaux d'avant ne bougent pas — alors
    que leurs issues, elles, changent."""
    alea = random.Random(11)
    mouvements = [alea.choice((-0.0002, -0.0001, 0.0001, 0.0002))
                  for _ in range(260)]
    a = _serie(mouvements)
    coupure = 150
    b = a[:coupure] + _serie([-x for x in mouvements[coupure:]],
                             depart=a[coupure - 1].close, premier=coupure)
    ea = {e.ts_sec: e for e in exemples_historiques(
        {PAIRE: a}, _ToujoursCall(), echeance_sec=900)}
    eb = {e.ts_sec: e for e in exemples_historiques(
        {PAIRE: b}, _ToujoursCall(), echeance_sec=900)}
    avant = [t for t in ea if t <= T0 + 60 * coupure and t in eb]
    assert len(avant) > 50
    champs = [c for c in em.REFERENCE_NUMERIQUES if c != "payout"] + [
        "heure_utc"]
    for t in avant:
        for c in champs:
            assert ea[t].contexte.get(c) == eb[t].contexte.get(c), (t, c)
    assert any(ea[t].gagne != eb[t].gagne for t in avant), (
        "la perturbation n'a rien changé : le test ne prouverait rien")


def test_les_periodes_et_l_ouverture_de_la_confirmation():
    assert em.periode(em.FIN_EXPLORATION - 1) == em.EXPLORATION
    assert em.periode(em.FIN_EXPLORATION) == em.VALIDATION
    assert em.periode(em.FIN_VALIDATION) == em.QUARANTAINE
    assert em.periode(em.FIN_VALIDATION + 10 * 86400) == em.QUARANTAINE, (
        "sans gel, rien n'est en confirmation")
    gel = em.FIN_VALIDATION + 3 * 86400 + 5 * 3600
    ouverture = em.ouverture_confirmation(gel)
    assert ouverture == em.FIN_VALIDATION + 4 * 86400
    assert em.periode(ouverture - 1, gel) == em.QUARANTAINE
    assert em.periode(ouverture, gel) == em.CONFIRMATION
    assert em.ouverture_confirmation(em.FIN_EXPLORATION) == em.FIN_VALIDATION


# --------------------------------------------------------------------------- #
# Préparation, modèle, mêmes signaux
# --------------------------------------------------------------------------- #

def test_la_preparation_est_figee_sur_l_exploration():
    exploration = [{"x": 1.0, "paire": "A"}, {"x": 3.0, "paire": "B"},
                   {"x": None, "paire": "A"}]
    prep = em.Preparation.ajuster(exploration, ["x"], ["paire"])
    assert prep.medianes["x"] == 2.0
    assert prep.colonnes() == ["x", "x?manque", "paire=B"]
    # Une ligne de validation aux valeurs extrêmes est transformée avec les
    # paramètres de l'exploration, sans les modifier.
    v = prep.vecteur({"x": 100.0, "paire": "B"})
    assert v[0] == pytest.approx((100.0 - 2.0) / prep.ecarts["x"])
    assert v[1:] == [0.0, 1.0]
    assert prep.vecteur({"x": None, "paire": "Z"}) == [0.0, 1.0, 0.0]


def test_le_creneau_de_quatre_heures():
    assert em.creneau(0.0) == "0-4" and em.creneau(23.0) == "20-24"
    assert em.creneau(4.0) == "4-8"


def test_la_logistique_retrouve_le_sens_d_un_effet():
    alea = random.Random(3)
    x, y = [], []
    for _ in range(600):
        a, b = alea.gauss(0, 1), alea.gauss(0, 1)
        x.append([a, b])
        y.append(int(alea.random() < 1 / (1 + math.exp(-(0.8 * a)))))
    m = em.Logistique.ajuster(x, y)
    assert m.coefficients[1] > 0.5 and abs(m.coefficients[2]) < 0.2
    assert 0 < m.proba([3.0, 0.0]) < 1


def test_des_donnees_separables_restent_finies_grace_a_la_penalite():
    x = [[-1.0], [-0.5], [0.5], [1.0]]
    y = [0, 0, 1, 1]
    m = em.Logistique.ajuster(x, y)
    assert all(math.isfinite(c) for c in m.coefficients)


def test_une_comparaison_refuse_un_signal_double_ou_une_prediction_absente():
    ligne = em.LigneComparee("s1", ("A", 1), 1, 1, 0.5, 0.6)
    assert ligne.gain_log == pytest.approx(math.log(0.6) - math.log(0.5))
    with pytest.raises(ValueError):
        em.comparer([ligne, ligne])
    with pytest.raises(ValueError):
        em.LigneComparee("s2", ("A", 1), 1, 1, 0.5, None)
    with pytest.raises(ValueError):
        em.LigneComparee("s3", ("A", 1), 1, None, 0.5, 0.5)
    r = em.comparer([ligne, em.LigneComparee("s2", ("A", 1), 1, 0, 0.5,
                                             0.4)])
    assert r["n"] == 2 and r["gain_log"] > 0


# --------------------------------------------------------------------------- #
# Bootstrap, terciles, comparaisons multiples
# --------------------------------------------------------------------------- #

def test_le_bootstrap_est_reproductible_et_ignore_les_blocs_vides():
    alea = random.Random(1)
    blocs = {(p, j): [alea.gauss(0.0, 1.0) for _ in range(5)]
             for p in "ABC" for j in range(10)}
    blocs[("Z", 0)] = []
    a = em.bootstrap_par_blocs(blocs)
    b = em.bootstrap_par_blocs(blocs)
    assert a == b
    assert a["blocs"] == 30 and a["signaux"] == 150
    assert a["bas"] < a["estimation"] < a["haut"]
    assert 0.05 < a["p_unilaterale"] < 0.95


def test_le_bootstrap_voit_un_effet_net_sans_descendre_sous_sa_resolution():
    blocs = {i: [1.0, 1.2, 0.9] for i in range(40)}
    r = em.bootstrap_par_blocs(blocs, tirages=200)
    assert r["p_unilaterale"] == pytest.approx(1 / 201)
    assert r["bas"] > 0


def test_l_effet_du_tercile_et_les_bornes_figees():
    bornes = em.terciles([float(i) for i in range(90)])
    assert bornes == (30.0, 60.0)
    lignes = []
    for i in range(300):
        v = float(i % 90)
        y = 1 if (v >= 60 and i % 10 < 7) or (v < 60 and i % 2) else 0
        lignes.append({"v": v, "y": y, "jour": i // 30})
    r = em.effet_tercile(lignes, "v", bornes, 2,
                         bloc=lambda r: r["jour"], tirages=200)
    assert r["estimation"] == pytest.approx(0.7 - 0.5, abs=0.02)
    assert r["bas"] <= r["estimation"] <= r["haut"]
    assert em.terciles([None, 1.0]) is None


def test_holm_et_benjamini_hochberg():
    p = {"a": 0.001, "b": 0.02, "c": 0.021, "d": 0.5}
    assert em.holm(p) == {"a": True, "b": False, "c": False, "d": False}
    assert em.holm({"a": 0.01, "b": 0.016, "c": 0.024, "d": 0.04}) == {
        "a": True, "b": True, "c": True, "d": True}
    bh = em.benjamini_hochberg({"a": 0.01, "b": 0.04, "c": 0.06, "d": 0.5},
                               q=0.10)
    assert bh == {"a": True, "b": True, "c": True, "d": False}


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #

def _verdict(**kw):
    base = dict(rejet_holm=True,
                gain_log={"estimation": 0.01, "bas": 0.002, "haut": 0.02},
                tercile={"estimation": 0.05, "bas": 0.01, "haut": 0.09},
                tiers_positifs=3, signes_sensibilite={"a": 1, "b": 1},
                n_niveau_2=600)
    return em.verdict(**{**base, **kw})


def test_les_verdicts():
    assert _verdict()[0] == em.CONFIRMEE
    assert _verdict(n_niveau_2=499)[0] == em.INDETERMINEE
    assert _verdict(signes_sensibilite={"a": 1, "b": -1})[0] == \
        em.INDETERMINEE
    assert _verdict(tiers_positifs=1)[0] == em.INDETERMINEE
    assert _verdict(tercile={"estimation": 0.015, "bas": -0.03,
                             "haut": 0.06})[0] == em.INDETERMINEE
    assert _verdict(rejet_holm=False,
                    gain_log={"estimation": -0.01, "bas": -0.02,
                              "haut": -0.001},
                    signes_sensibilite={"a": -1, "b": -1})[0] == em.REJETEE
    assert _verdict(rejet_holm=False,
                    tercile={"estimation": 0.0, "bas": -0.01,
                             "haut": 0.019},
                    gain_log={"estimation": 0.0, "bas": -0.01,
                              "haut": 0.01})[0] == em.REJETEE
    v, motifs = _verdict(rejet_holm=False,
                         tercile={"estimation": 0.01, "bas": -0.08,
                                  "haut": 0.10},
                         gain_log={"estimation": 0.0, "bas": -0.01,
                                   "haut": 0.01})
    assert v == em.INDETERMINEE and "Holm non atteint" in motifs


# --------------------------------------------------------------------------- #
# Puissance et règle de poursuite
# --------------------------------------------------------------------------- #

def test_l_icc_est_nulle_sans_grappes_et_forte_avec():
    alea = random.Random(2)
    independants = {i: [int(alea.random() < 0.5) for _ in range(6)]
                    for i in range(200)}
    assert em.icc(independants) < 0.05
    grappes = {i: [i % 2] * 6 for i in range(200)}
    assert em.icc(grappes) > 0.9


def test_la_puissance_suit_la_formule_sans_grappes():
    blocs = {i: [i % 2] for i in range(1000)}          # p = 0,5, ρ = 0
    r = em.puissance(blocs, tailles=(500,), signaux_par_jour=100,
                     part_niveau_2=0.5)
    za = em._N.inv_cdf(1 - 0.0125)
    zb = em._N.inv_cdf(0.8)
    attendu = (za + zb) * math.sqrt(0.25 * 4.5 / 500)
    assert r["deff"] == pytest.approx(1.0)
    assert r["lignes"][0]["effet_min"] == pytest.approx(attendu)
    assert r["lignes"][0]["jours"] == pytest.approx(10.0)
    assert r["n_pour_confirmer"] == math.ceil(
        (za + zb) ** 2 * 0.25 * 4.5 / 0.02 ** 2)
    assert r["n_pour_confirmer"] > 20_000, (
        "un effet de deux points demande des dizaines de milliers de signaux")


def test_la_dependance_entre_signaux_augmente_les_tailles_requises():
    alea = random.Random(4)
    blocs = {}
    for i in range(300):
        base = 0.3 if i % 2 else 0.7
        blocs[i] = [int(alea.random() < base) for _ in range(4)]
    r = em.puissance(blocs)
    assert r["icc"] > 0 and r["deff"] > 1
    sans = em.puissance({i: [y] for i, v in blocs.items() for y in v[:1]})
    assert r["n_pour_confirmer"] > sans["n_pour_confirmer"] * 0.9


def test_la_depense_d_alpha_et_les_seuils_des_regards():
    assert em.depense_alpha(1.0) == pytest.approx(0.05)
    valeurs = [em.depense_alpha(t) for t in (0.1, 0.25, 0.5, 1.0)]
    assert valeurs == sorted(valeurs)
    seuils = em.seuils_des_regards()
    assert [s["N"] for s in seuils] == list(em.REGARDS)
    assert sum(s["nominal"] for s in seuils) == pytest.approx(0.05)
    assert seuils[0]["nominal"] < 1e-6, "aucune confirmation précoce facile"


# --------------------------------------------------------------------------- #
# Registre et gel
# --------------------------------------------------------------------------- #

def test_le_gel_refuse_des_parametres_modifies():
    params = {"hypotheses": [{"variable": "A1_dist_fin", "w": 300,
                              "tercile": 2}], "lambda": 1.0}
    empreinte = em.empreinte(params)
    em.exiger_le_gel(params, empreinte)
    with pytest.raises(ValueError):
        em.exiger_le_gel(params, None)
    with pytest.raises(ValueError):
        em.exiger_le_gel({**params, "lambda": 2.0}, empreinte)
    assert em.empreinte(dict(reversed(list(params.items())))) == empreinte


def test_le_registre_garde_tout_y_compris_l_abandonne():
    r = em.Registre()
    n1 = r.noter(phase="exploration", nature="exploratoire",
                 variable="B1_vol", w=60, statistique="gain_log", valeur=0.1)
    r.noter(phase="exploration", nature="sensibilite", variable="B1_vol",
            k=2)
    r.abandonner(n1, "fenêtre trop courte")
    assert r.compte() == 2 and r.compte("sensibilite") == 1
    assert r.lignes[0]["statut"] == "abandonné"
    texte = r.csv().decode()
    assert texte.splitlines()[0].startswith("n;phase;nature")
    assert "fenêtre trop courte" in texte
    with pytest.raises(ValueError):
        r.noter(phase="x", nature="inventee")
    with pytest.raises(ValueError):
        r.noter(phase="x", nature="controle", couleur="bleu")


def test_vingt_quatre_variantes_de_sensibilite_par_hypothese():
    variantes = em.variantes_de_sensibilite()
    assert len(variantes) == 24
    assert variantes[0] == {"traitement": "distincts", "k": 0,
                            "blocs": "paire_jour", "egalites": "exclues"}
