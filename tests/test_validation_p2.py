"""Validation de P2 (EXP-020 C et D) : causalité, groupes, critères, volume."""

from __future__ import annotations

import json
import random

import pytest

from maxprofit.apprentissage.historique import _confirmation_m1
from maxprofit.apprentissage.lecons import Exemple
from maxprofit.core.types import Candle
from maxprofit.live import validation_p2 as v
from maxprofit.live.preinscriptions import ouverture

JOUR = 86400
OUVERTURE = ouverture()
REFERENCE = {"version_regles": "ZoneH1:abcd1234", "confirmation_m1": 1,
             "regle_heure": 3.3, "echeance_sec": 900,
             "paires": ["A_otc", "B_otc", "C_otc", "D_otc"]}


def _entree(t: int, paire: str, gagne: bool, part: float | None,
            espace: float, douteuse: bool = False) -> Exemple:
    ctx = {"espace_obstacle": espace,
           "donnees_douteuses": float(douteuse)}
    if part is not None:
        ctx["part_parcourue"] = part
    return Exemple(ts_sec=t, pair=paire, contexte=ctx, gagne=gagne)


def _base(debut: int, jours: int, par_jour: int = 8,
          tard_perd: bool = True) -> list[Exemple]:
    """`par_jour` entrées par jour sur quatre paires. Une sur cinq « en
    retard » (C = 0,8, D = 0,5), réparties sur les quatre paires ; elles
    perdent si `tard_perd`, les autres gagnent deux fois sur trois."""
    sortie = []
    for k in range(jours * par_jour):
        t = debut + k * (JOUR // par_jour) + 60
        tard = k % 5 == 0
        gagne = (not tard_perd if tard else k % 3 != 0)
        part = None if k % 9 == 5 else (0.8 if tard else 0.2)
        sortie.append(_entree(t, f"{'ABCD'[k % 4]}_otc", gagne, part,
                              0.5 if tard else 3.0))
    return sortie


def _valider(base, ancien=None, *, fin, gain=None, reference=REFERENCE):
    debut = OUVERTURE
    jours_collecte = max(0.0, (fin - debut) / JOUR)
    etat = v.validation(base, ancien, fin=fin, jours_collecte=jours_collecte,
                        reference=reference, jours_avant=20.0, gain=gain)
    return json.loads(json.dumps(etat))


# --------------------------------------------------------------------------- #
# Phase 1 — C et D ne lisent pas l'avenir
# --------------------------------------------------------------------------- #

def _marche(n: int, graine: int) -> list[Candle]:
    rng = random.Random(graine)
    prix, sortie = 1.1, []
    for i in range(n):
        o = prix
        prix *= 1 + rng.gauss(0, 0.0007)
        sortie.append(Candle(
            pair="X_otc", tf_sec=60, ts_sec=OUVERTURE + 60 * i, open=o,
            high=max(o, prix) * 1.0001, low=min(o, prix) * 0.9999,
            close=prix, tick_count=30, complete=True))
    return sortie


@pytest.mark.parametrize("graine", [1, 2, 3])
def test_c_et_d_ne_dependent_pas_de_ce_qui_suit_l_entree(graine):
    """Les valeurs mesurées À L'ENTRÉE (préfixe « e_ ») sont identiques
    quand on remplace toutes les bougies qui suivent l'entrée : elles ne
    lisent que le passé, pivots du ZigZag compris."""
    serie = _marche(400, graine)
    autre = _marche(400, graine + 100)
    trouvees = 0
    for i in range(150, 360):
        call = serie[i + 1].close > serie[i + 1].open
        if not (call and serie[i + 1].close > serie[i].high):
            continue
        niveau = min(c.low for c in serie[i - 30:i])
        closes = {c.ts_sec: c.close for c in serie}
        avant = _confirmation_m1(serie, i, True, closes, 900,
                                 {"niveau": niveau})
        j = i + 1
        futur = [Candle(pair=c.pair, tf_sec=60, ts_sec=serie[k].ts_sec,
                        open=c.open, high=c.high, low=c.low, close=c.close,
                        tick_count=c.tick_count, complete=True)
                 for k, c in zip(range(j + 1, len(serie)), autre[j + 1:])]
        modifiee = serie[:j + 1] + futur
        closes_m = {c.ts_sec: c.close for c in modifiee}
        apres = _confirmation_m1(modifiee, i, True, closes_m, 900,
                                 {"niveau": niveau})
        cles = [k for k in avant if k.startswith("e_")]
        assert "e_espace_obstacle" in cles
        assert {k: avant[k] for k in cles} == {k: apres[k] for k in cles}
        trouvees += 1
    assert trouvees >= 5


# --------------------------------------------------------------------------- #
# Phase 5 — le volume
# --------------------------------------------------------------------------- #

def test_le_volume_requis():
    assert 1130 <= v.volume_requis(0.55, 0.6, 3) <= 1140
    assert 400 <= v.volume_requis(0.55, 0.6, 5) <= 415
    assert 95 <= v.volume_requis(0.55, 0.6, 10) <= 110
    # Des signaux corrélés en demandent davantage, à proportion.
    assert v.volume_requis(0.55, 0.6, 5, 1.5) > 1.45 * v.volume_requis(
        0.55, 0.6, 5)
    assert v.volume_requis(0.55, 1.0, 5) is None


def test_le_gain_minimal_vient_de_l_environnement(monkeypatch):
    monkeypatch.delenv(v.ENV_GAIN, raising=False)
    assert v.gain_minimal() is None
    monkeypatch.setenv(v.ENV_GAIN, "5")
    assert v.gain_minimal() == 5.0
    monkeypatch.setenv(v.ENV_GAIN, "abc")
    assert v.gain_minimal() is None


# --------------------------------------------------------------------------- #
# Phases 2 à 6 — les quatre groupes et le verdict
# --------------------------------------------------------------------------- #

def test_avant_l_ouverture_rien_n_est_mesure():
    etat = _valider(_base(OUVERTURE - 20 * JOUR, 10),
                    fin=OUVERTURE - JOUR)
    assert etat["note"] == "fenêtre pas encore ouverte"


def test_les_quatre_groupes_sur_les_memes_signaux():
    base = _base(OUVERTURE - 20 * JOUR, 20) + _base(OUVERTURE, 5)
    etat = _valider(base, fin=OUVERTURE + 5 * JOUR)
    g = etat["groupes"]
    assert g["A"]["n"] == 40 and etat["etape"] == "A"
    # Huit « en retard » : D les écarte toutes ; C garde celle qui n'a pas
    # de valeur de C (k = 5) — une valeur manquante n'exclut pas.
    assert g["P2-D"]["n"] == 32
    assert g["P2-C"]["n"] == 33
    assert g["P2-E"]["n"] == 32
    assert g["P2-E"]["ecart"] > 0 and g["A"]["ev"] < g["P2-E"]["ev"]
    assert g["P2-D"]["part_conservee"] == 0.8
    assert g["A"]["par_jour"] == 8.0
    assert len(etat["registre"]) == 40
    assert etat["revue"]["c_manquant"] > 0
    # Le plan vient des données d'AVANT la fenêtre, et du débit.
    plan = etat["gel"]["plan"]
    assert plan["n"] == 160 and plan["debit_calendaire"] == 8.0
    assert set(plan["volumes"]) == {"P2-C", "P2-D", "P2-E"}


def test_le_registre_cumule_au_dela_du_rejeu():
    premier = _valider(_base(OUVERTURE, 5), fin=OUVERTURE + 5 * JOUR)
    # Le rejeu suivant ne voit plus les deux premiers jours.
    second = _valider(_base(OUVERTURE + 2 * JOUR, 5), premier,
                      fin=OUVERTURE + 7 * JOUR)
    assert len(second["registre"]) == 56 == second["groupes"]["A"]["n"]


def test_etape_b_puis_verdict_fige():
    base = _base(OUVERTURE - 20 * JOUR, 20) + _base(OUVERTURE, 100)
    a = _valider(base[:160 + 120], fin=OUVERTURE + 15 * JOUR)
    assert a["etape"] == "B à paramétrer"
    b = _valider(base[:160 + 120], a, fin=OUVERTURE + 15 * JOUR, gain=3)
    gel = b["gel"]
    assert gel["gain"] == 3 and gel["cloture"]
    cible = gel["cible"]
    assert 120 < cible <= 800 and b["etape"] == "B"
    assert gel["cloture"] == OUVERTURE + -(-cible // 8) * JOUR
    # Un autre gain plus tard ne change rien : il est gelé.
    c = _valider(base[:160 + 120], b, fin=OUVERTURE + 16 * JOUR, gain=10)
    assert c["gel"]["cible"] == cible
    assert any("gain minimal" in x for x in c["alertes"])
    fin = OUVERTURE + 100 * JOUR
    d = _valider(base, c, fin=fin, gain=10)
    assert d["etape"] == "final" and d["final"]
    for ident in ("P2-C", "P2-D", "P2-E"):
        assert d["groupes"][ident]["conclusion"]
    # Les « en retard » perdent tout : D et C + D gagnent nettement.
    assert d["groupes"]["P2-D"]["conclusion"].startswith("✅")
    # Figé : une autre vérité ensuite ne change rien.
    e = _valider(_base(OUVERTURE, 3, tard_perd=False), d, fin=fin + JOUR)
    assert e == d


def test_une_reference_modifiee_est_signalee():
    base = _base(OUVERTURE, 3)
    a = _valider(base, fin=OUVERTURE + 3 * JOUR)
    autre = {**REFERENCE, "paires": ["A_otc"]}
    b = _valider(base, a, fin=OUVERTURE + 4 * JOUR, reference=autre)
    assert b["alertes"] == ["paires modifié pendant le test"]


def test_les_trois_issues():
    bon = {"ev": 0.1, "ecart": 0.08, "ic_ecart": [0.01, 0.15],
           "sans_deux_paires": 0.05, "sans_meilleur_jour": 0.06,
           "part_conservee": 0.7}
    assert v.conclusion(bon, True).startswith("✅")
    assert v.conclusion(bon, False).startswith("≈")
    assert v.conclusion({**bon, "ic_ecart": [-0.02, 0.15]},
                        True).startswith("≈")
    assert v.conclusion({**bon, "sans_deux_paires": -0.01},
                        True).startswith("❌")
    assert v.conclusion({**bon, "part_conservee": 0.4}, True).startswith("❌")
    assert v.conclusion({**bon, "ev": -0.05}, True).startswith("❌")
    assert v.conclusion({**bon, "ecart": -0.02, "ic_ecart": [-0.1, 0.0]},
                        True).startswith("❌")


def test_un_gain_qui_tient_a_une_paire_ne_tient_pas():
    """Les « en retard » ne perdent que sur la paire A, et gagnent
    ailleurs : le gain global vient d'une paire, et disparaît sans elle."""
    base = []
    for k in range(400):
        t = OUVERTURE + k * (JOUR // 8) + 60
        paire = f"{'ABCD'[k % 4]}_otc"
        tard = k % 8 == 0 or k % 20 in (1, 2, 3)
        gagne = (paire != "A_otc") if tard else k % 3 != 0
        base.append(_entree(t, paire, gagne, 0.8 if tard else 0.2,
                            0.5 if tard else 3.0))
    etat = _valider(base, fin=OUVERTURE + 51 * JOUR)
    g = etat["groupes"]["P2-D"]
    assert g["ecart"] > 0
    assert g["sans_deux_paires"] <= 0 and "A_otc" in g["paires_retirees"]


def test_le_texte_est_du_html_telegram_valide():
    import re
    base = _base(OUVERTURE - 20 * JOUR, 20) + _base(OUVERTURE, 5)
    etat = _valider(base, fin=OUVERTURE + 5 * JOUR)
    lignes = v.texte(etat, lambda ts: "14/10")
    sortie = "\n".join(lignes)
    assert "Étape A" in sortie and "<pre>" in sortie
    assert "Volume requis" in sortie
    nu = re.sub(r"</?(b|code|pre)>", "", sortie)
    assert not re.search(r"[<>&]", nu)
    assert [h[0] for h in v.hypotheses(etat)] == ["P2-C", "P2-D", "P2-E"]
