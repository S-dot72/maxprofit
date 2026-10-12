"""EXP-021 (stabilité dans le temps) et EXP-022 (le premier pas)."""

from __future__ import annotations

import re

from maxprofit.apprentissage.lecons import Exemple
from maxprofit.live.stabilite import (
    _rangs, intervalle_par_blocs, stabilite, texte, texte_alerte)

T0 = 1_790_000_000 // 86400 * 86400
JOUR = 86400


def _ex(t, gagne, paire="EURUSD_otc", **ctx):
    base = {"mouvement_heure": 0.0, "retournement_suivant": 1.0,
            "gagne_suivant": float(gagne), "attente_confirmation": 1.0,
            "gagne_confirme": float(gagne), "distance_niveau": 0.5,
            "e_distance_niveau": 1.3}
    base.update(ctx)
    return Exemple(ts_sec=t, pair=paire, contexte=base, gagne=gagne)


def _semaines(taux_par_semaine, par_semaine=40):
    exemples = []
    for s, taux in enumerate(taux_par_semaine):
        for i in range(par_semaine):
            t = T0 + s * 7 * JOUR + i * (7 * JOUR // par_semaine)
            exemples.append(_ex(t, (i % 10) < round(taux * 10),
                                paire=f"P{i % 4}_otc"))
    return exemples


def test_une_derniere_semaine_effondree_declenche_l_alerte():
    r = stabilite(_semaines([0.7, 0.7, 0.7, 0.2]), lambda e: True, 1)
    assert len(r["fenetres"]) == 4
    assert r["alerte"]["declenchee"]
    message = texte_alerte(r)
    assert "dégradation à examiner" in message
    assert "Aucune règle n'est modifiée" in message
    sortie = texte(r)
    assert "ALERTE" in sortie and "EXP-022" in sortie
    assert not re.search(r"<(?!/?b>)", sortie)


def test_une_semaine_ordinaire_ne_declenche_rien():
    r = stabilite(_semaines([0.7, 0.6, 0.7, 0.6]), lambda e: True, 1)
    assert not r["alerte"]["declenchee"]
    assert texte_alerte(r) is None


def test_l_intervalle_par_blocs_entoure_le_taux():
    exemples = _semaines([0.6, 0.6, 0.6, 0.6])
    bas, haut = intervalle_par_blocs(exemples)
    taux = sum(e.gagne for e in exemples) / len(exemples)
    assert bas < taux < haut


def test_une_paire_sans_assez_de_signaux_n_est_pas_jugee():
    exemples = _semaines([0.7, 0.7, 0.7, 0.7]) + [
        _ex(T0 + 3 * JOUR + 7, True, paire="RARE_otc")]
    r = stabilite(exemples, lambda e: True, 1)
    assert r["paires"]["RARE_otc"]["categorie"] == "données insuffisantes"
    assert r["paires"]["P0_otc"]["categorie"] == "stabilité à confirmer"


def test_le_plan_rejoue_donne_son_rang_a_chaque_entree():
    # Perte sur A ; B trop proche est sauté, le B suivant (pas 2) gagne ;
    # perte sur A, perte au pas 2 sur B (session perdue) ; C gagne.
    base = [_ex(T0, False, "A"), _ex(T0 + 300, True, "B"),
            _ex(T0 + 1000, True, "B"), _ex(T0 + 2000, False, "A"),
            _ex(T0 + 3000, False, "B"), _ex(T0 + 4000, True, "C")]
    r = _rangs(base, pas_max=2, echeance_sec=900)
    assert r["comptes"]["1"] == [3, 1]
    assert r["comptes"]["2"] == [2, 1]


def test_entree_immediate_et_confirmee_sur_les_memes_signaux():
    exemples = [_ex(T0 + i * 3600, i % 2 == 0, gagne_suivant=1.0)
                for i in range(30)]
    exemples += [_ex(T0 + i * 3600 + 60, True, retournement_suivant=0.0)
                 for i in range(10)]
    r = stabilite(exemples, lambda e: True, 1)
    er = r["entree_retardee"]
    assert er["confirmes"] == 30 and er["perdus"] == [10, 10]
    assert er["apparies_immediat"] == 15 and er["apparies_confirme"] == 30
    assert er["degradation_mediane"] == 0.8
