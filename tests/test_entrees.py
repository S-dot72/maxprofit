"""/entrees : T0, T1, T2 et T3 sur les mêmes signaux."""

from __future__ import annotations

import json
import re

from maxprofit.apprentissage.lecons import Exemple
from maxprofit.live import entrees as mod
from maxprofit.live.preinscriptions import ouverture_t

JOUR = 86400
PAIRES = ("A_otc", "B_otc", "C_otc", "D_otc")
T0 = 1_790_000_000 // JOUR * JOUR
SECS = (240, 600, 900)


def _signal(k: int, t0: int = T0) -> Exemple:
    """Le k-ième signal ZoneH1 :
    - entrée immédiate gagnante sauf k % 3 = 0 ;
    - à la bougie suivante (T1), elle perd EN PLUS quand k % 5 = 1 ;
    - confirmé sauf k % 4 = 0, et la confirmée perd en plus quand
      k % 7 = 0 ; la règle de l'heure relue à l'entrée l'écarte quand
      k % 50 = 3 ;
    - distance de l'entrée confirmée : 1,1 si k % 6 = 0, 1,4 si k % 6 = 1,
      0,8 sinon ;
    - k % 97 = 5 : égalité au signal, le signal sort du pool."""
    imm = {s: k % 3 != 0 for s in SECS}
    if k % 97 == 5:
        imm.pop(900)
    confirme = k % 4 != 0
    ctx = {"mouvement_heure": 0.0, "distance_niveau": 0.5,
           "suiv_distance_niveau": 0.9, "imm_mfe_15": 2.0,
           "suiv_mfe_15": 1.6, "imm_mae_15": 0.7, "suiv_mae_15": 1.0,
           "retournement_suivant": float(confirme),
           "attente_confirmation": 1.0 if confirme else 0.0,
           **{f"gagne_suivant_{s}": float(k % 3 != 0 and k % 5 != 1)
              for s in SECS}}
    ctx["gagne_suivant"] = ctx["gagne_suivant_900"]
    if confirme:
        g = float(k % 3 != 0 and k % 7 != 0)
        ctx.update({f"gagne_confirme_{s}": g for s in SECS})
        ctx.update({
            "gagne_confirme": g, "mfe_15": 1.4, "mae_15": 1.3,
            "e_mouvement_heure": -10.0 if k % 50 == 3 else 0.0,
            "e_distance_niveau": (1.1 if k % 6 == 0 else
                                  1.4 if k % 6 == 1 else 0.8)})
    return Exemple(ts_sec=t0 + k * 3600 + 60, pair=PAIRES[k % 4],
                   contexte=ctx, gagne=imm.get(900, False), issues=imm)


def _en_direct(e: Exemple) -> bool:
    return e.contexte.get("mouvement_heure", 0.0) >= -3.0


def _rapport(n: int = 300, t0: int = T0, **kw) -> dict:
    r = mod.entrees([_signal(k, t0) for k in range(n)], _en_direct, 1, **kw)
    return json.loads(json.dumps(r))


def _ks(n=300):
    return [k for k in range(n) if k % 97 != 5]


def test_le_pool_et_les_ordres_de_chaque_entree():
    r = _rapport()
    ks = _ks()
    assert r["n"] == len(ks) and r["exclus"] == 300 - len(ks)
    t2 = [k for k in ks if k % 4 and k % 50 != 3]
    assert r["t2_ordres"] == len(t2)
    assert r["ecartes_a_l_entree"] == sum(1 for k in ks
                                          if k % 4 and k % 50 == 3)
    v = {x["id"]: x for x in r["tableaux"]["900"]["variantes"]}
    assert v["T0"]["ordres"] == v["T1"]["ordres"] == len(ks)
    assert v["T2"]["ordres"] == len(t2)
    assert v["T2"]["annules"] == len(ks) - len(t2)
    assert v["T3-1.00"]["ordres"] == sum(1 for k in t2 if k % 6 > 1)
    assert v["T3-1.25"]["ordres"] == sum(1 for k in t2 if k % 6 != 1)
    assert v["T3-1.55"]["ordres"] == len(t2)
    # Le gain par opportunité : un signal sans ordre compte zéro.
    g0 = sum(1 for k in ks if k % 3)
    attendu = (0.92 * g0 - (len(ks) - g0)) / len(ks)
    assert abs(v["T0"]["gain_opp"] - round(attendu, 4)) < 1e-9


def test_la_decomposition_retard_selection_annulation():
    r = _rapport()
    e = r["tableaux"]["900"]["ecarts"]
    total = e["T1−T0"]["ecart"] + e["T2−T1"]["ecart"]
    assert abs(total - e["T2−T0"]["ecart"]) < 1e-3
    assert e["T1−T0"]["ecart"] < 0              # attendre coûte ici
    assert e["T3-1.55−T2"]["ecart"] == 0        # 1,55 n'annule rien
    tb = r["t0_t1"]["table"]
    ks = _ks()
    assert tb == [sum(1 for k in ks if k % 3 and k % 5 != 1),
                  sum(1 for k in ks if k % 3 and k % 5 == 1), 0,
                  sum(1 for k in ks if not k % 3)]
    assert r["prix"]["distances"]["T1"][1] == 0.9
    assert r["annulees"]["1.00"][0] == sum(
        1 for k in ks if k % 4 and k % 50 != 3 and k % 6 <= 1)


def test_le_seuil_choisi_et_ses_egalites():
    s = [10, 10, 5, 10, 5, 4, 2, 4, 2, 4, 2, 4, 2]       # T3 tous égaux
    assert mod.choisir_seuil(s) == 1.55
    s = [10, 10, 5, 10, 5, 4, 2, 2, 2, 4, 2, 4, 2]       # 1,00 meilleur
    assert mod.choisir_seuil(s) == 1.00


def test_les_decisions_et_le_texte():
    r = _rapport()
    v = {i: verdict for i, _h, verdict in mod.hypotheses(r)}
    assert {"T-retard", "T-selection", "T0-hist", "T3-hist", "R8-T0",
            "R8-T3"} <= set(v)
    assert v["T-retard"].startswith("✅")       # le retard coûte
    for verdict in v.values():
        assert verdict.split()[0] in ("✅", "❌", "≈")
    sortie = mod.texte(r)
    for titre in ("1. À 15 minutes", "2. Décomposition", "3. T0 contre T1",
                  "4. Prix d'entrée", "5. Stabilité", "6. Fenêtre",
                  "7. Décisions"):
        assert titre in sortie
    nu = re.sub(r"</?(b|pre)>", "", sortie)
    assert not re.search(r"[<>&]", nu)
    assert mod.texte(None).startswith("🚪")


def test_trop_peu_de_signaux_garde_le_registre():
    r = _rapport()
    maigre = mod.entrees([_signal(k) for k in range(20)], _en_direct, 1,
                         ancien=r)
    assert "note" in maigre and maigre["fenetre"] == r["fenetre"]
    assert mod.hypotheses(maigre) == []


def test_la_fenetre_fige_son_seuil_et_son_verdict(monkeypatch):
    monkeypatch.setattr(mod, "N_MIN_T", 50)
    ouv = ouverture_t()
    t0 = ouv - 6 * JOUR
    r = _rapport(t0=t0, fin=t0 + 300 * 3600)
    f = r["fenetre"]
    apres = [k for k in _ks() if t0 + k * 3600 + 60 >= ouv]
    assert f["n"] == len(apres) >= 50
    assert f["plan"]["fige_le"] and f["plan"]["seuil"] in (1.0, 1.25, 1.55)
    assert set(f["finals"]) == {"R8-T0", "R8-T3"}
    t0_verdict = f["verdicts"]["R8-T0"]
    assert t0_verdict["variante"] == "T0"
    # Rejoué plus tard, sur moins d'historique : tout est relu tel quel.
    exemples = [_signal(k, t0) for k in range(200, 300)]
    deux = mod.entrees(exemples, _en_direct, 1, ancien=r,
                       fin=t0 + 310 * 3600)
    assert deux["fenetre"]["plan"] == f["plan"]
    assert deux["fenetre"]["verdicts"]["R8-T0"] == t0_verdict
    assert deux["fenetre"]["n"] == f["n"]


def test_le_journal_du_protocole_compte_les_entrees():
    from maxprofit.apprentissage.indicateurs import Indicateurs
    from maxprofit.apprentissage.qualite import Qualite
    from maxprofit.live.protocole import protocole
    p = protocole(
        [], lambda e: True, 1, 900,
        qualite=Qualite(T0, T0 + 10 * JOUR, T0 + 5 * JOUR),
        indicateurs=Indicateurs(T0, T0 + 5 * JOUR), exclusions={},
        couverture=None, experiences={"coupure": None, "variables": []},
        ancien=None, version_regles="ZoneH1:abcd", version_experiences=11,
        debut=T0, fin=T0 + 10 * JOUR, gel=None, jours=10,
        entrees=_rapport())
    ids = {h[0] for h in p["hypotheses"]}
    assert {"R8-T0", "R8-T3", "T-retard"} <= ids
