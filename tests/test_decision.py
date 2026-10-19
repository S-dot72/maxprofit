"""/decision : volumes, dates estimées et critères de production."""

from __future__ import annotations

import json
import re

from maxprofit.live import decision as mod
from maxprofit.live.preinscriptions import ouverture_r8, ouverture_t

JOUR = 86400


def _labo(ev_m0=None, verdict_m1="≈ indéterminée — 40 signaux de M0 sur 101",
          n=40, jours_collecte=2.0, ev_m1=None):
    ev_m0 = ev_m0 or {"n": n, "g": 24, "ev": 0.152, "ic": [-0.1, 0.4],
                      "sans_deux_paires": 0.1, "sans_meilleur_jour": 0.08}
    return {
        "reference8": {
            "n": 259, "jours": 13.5,
            "fenetre": {
                "n": n, "jours_collecte": jours_collecte,
                "modeles": {
                    "M1": {"n": int(n * 0.7), "cible": 101, "ev": 0.25,
                           "verdict": verdict_m1},
                    "M3": {"n": int(n * 0.4), "cible": 310, "ev": 0.3,
                           "verdict": "≈ indéterminée — 40 sur 310"}},
                "echeance": {"n": n, "verdict": "≈ indéterminée"},
                "ev_fenetre": {"M0": ev_m0, "M1": ev_m1 or ev_m0}}},
        "entrees": {"n": 669, "jours": 13.7,
                    "fenetre": {"n": 0, "jours_collecte": 0.0,
                                "verdicts": {"R8-T0": {"verdict": "≈ …"},
                                             "R8-T3": {"verdict": "≈ …"}}}},
        "protocole": {"exp004": {"validation_p2": {
            "gel": {"cible": 160, "plan": {"debit_calendaire": 17.8}},
            "groupes": {"A": {"n": 91}}, "etape": "B"}}},
    }


def test_les_questions_et_leurs_dates():
    maintenant = ouverture_r8() + 2.5 * JOUR
    d = json.loads(json.dumps(mod.decision(_labo(), maintenant)))
    q = {x["id"]: x for x in d["questions"]}
    assert {"R8-M1", "R8-M3", "R8-H", "R8-T0", "R8-T3", "P2"} <= set(q)
    # 40 entrées en 2 jours de collecte : 20 par jour ; M1 attend 101.
    assert d["rythme_r8"] == 20.0
    attendu = int(maintenant + (101 - 40) / 20 * JOUR)
    assert q["R8-M1"]["eta"] == attendu
    # La fenêtre T ne s'ouvre qu'au 20/10 : la date part de là au plus tôt.
    assert q["R8-T0"]["eta"] >= ouverture_t()
    assert d["collecte_fenetre"] == round(2.0 / 2.5, 3)


def test_la_production_attend_le_volume():
    d = mod.decision(_labo(), ouverture_r8() + 2.5 * JOUR)
    p = d["production"]
    assert p["variante"] == "M0" and "T2" in p["entree"]
    assert p["criteres"]["volume"] is False
    assert p["statut"].startswith("⏳") and p["eta"] is not None


def test_production_possible_quand_les_quatre_criteres_sont_remplis():
    ev = {"n": 320, "g": 195, "ev": 0.17, "ic": [0.03, 0.31],
          "sans_deux_paires": 0.11, "sans_meilleur_jour": 0.12}
    d = mod.decision(_labo(ev_m0=ev, n=320, jours_collecte=9.0),
                     ouverture_r8() + 10 * JOUR)
    assert all(d["production"]["criteres"].values())
    assert d["production"]["statut"].startswith("✅")


def test_pas_de_production_quand_l_ev_n_est_pas_etablie():
    ev = {"n": 320, "g": 175, "ev": 0.05, "ic": [-0.06, 0.16],
          "sans_deux_paires": 0.01, "sans_meilleur_jour": 0.02}
    d = mod.decision(_labo(ev_m0=ev, n=320, jours_collecte=9.0),
                     ouverture_r8() + 10 * JOUR)
    assert d["production"]["statut"].startswith("❌")


def test_un_modele_soutenu_devient_la_variante_jouee():
    ev1 = {"n": 230, "g": 150, "ev": 0.25, "ic": [0.1, 0.4],
           "sans_deux_paires": 0.2, "sans_meilleur_jour": 0.2}
    d = mod.decision(_labo(verdict_m1="✅ soutenue — les cinq critères",
                           ev_m1=ev1, n=330, jours_collecte=9.0),
                     ouverture_r8() + 10 * JOUR)
    p = d["production"]
    assert p["variante"] == "M1" and p["ev"]["n"] == 230
    assert p["criteres"]["volume"] is False      # 230 sur 300


def test_le_texte():
    sortie = mod.texte(mod.decision(_labo(), ouverture_r8() + 2.5 * JOUR))
    for titre in ("1. Production", "2. Les questions en cours",
                  "3. Ce qui raccourcit le délai"):
        assert titre in sortie
    nu = re.sub(r"</?b>", "", sortie)
    assert not re.search(r"[<>&]", nu)
    assert mod.texte(mod.decision({})).startswith("🎯")
    assert mod.texte(None).startswith("🎯")
