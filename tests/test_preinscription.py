"""
La pré-inscription (`docs/etape3_preinscription.json`) ne doit pas
diverger du code qui l'exécutera : variables, fenêtres, horizon, regards,
seuils, référence, règle de retrait. Et son empreinte doit être stable.
"""

from __future__ import annotations

import json
from pathlib import Path

from maxprofit.apprentissage import etude_micro as em
from maxprofit.apprentissage import microstructure_pre as mp

CHEMIN = Path(__file__).resolve().parents[1] / "docs" / \
    "etape3_preinscription.json"


def _doc() -> dict:
    return json.loads(CHEMIN.read_text(encoding="utf-8"))


def test_les_hypotheses_existent_dans_le_code():
    doc = _doc()
    hyp = doc["hypotheses"]
    assert 1 <= len(hyp) <= em.HYPOTHESES_MAX
    assert [h["id"] for h in hyp] == ["H1", "H2"]
    for h in hyp:
        assert h["variable"] in mp.VARIABLES
        assert h["fenetre_sec"] in mp.FENETRES_SEC
        assert h["sens"] in (1, -1) and h["tercile"] in (0, 1, 2)
        for champ in ("formule", "unite", "mecanisme", "manquante",
                      "sens_en_clair"):
            assert h[champ], (h["id"], champ)
    assert hyp[1]["oscillations"] and hyp[1]["deduplication"]


def test_les_regles_communes_sont_celles_du_code():
    doc = _doc()
    assert doc["regards"]["signaux_niveau_2"] == list(em.REGARDS)
    assert doc["regards"]["plafond"] == em.REGARDS[-1]
    assert doc["regards"]["alpha_famille"] == em.ALPHA_FAMILLE
    assert doc["retrait_quasi_constantes"]["version"] == em.VERSION_RETRAIT
    assert doc["population"]["niveau_disponibilite"] == 2
    assert set(doc["population"]["classes_admises"]) <= {mp.VALID, mp.PARTIAL}
    assert doc["ticks"]["troncature_principale_sec"] == 0
    assert doc["effet_pratique"]["seuil_points"] == 100 * em.EFFET_PRATIQUE
    sens = doc["sensibilites"]
    assert sens["troncature_sec"] == list(mp.TRONCATURES_SEC)
    assert set(sens["traitement"]) == set(mp.TRAITEMENTS)
    n = 1
    for valeurs in sens.values():
        n *= len(valeurs)
    assert n == len(em.variantes_de_sensibilite())
    variables = " ".join(doc["reference"]["variables"])
    for nom in em.REFERENCE_NUMERIQUES:
        assert nom in variables, nom
    assert "λ = 1" in doc["reference"]["modele"] and em.LAMBDA_L2 == 1.0
    assert "graine 7" in doc["critere_principal"]["incertitude"]
    assert em.GRAINE == 7 and em.TIRAGES == 1000


def test_le_brouillon_n_est_pas_un_gel_et_son_empreinte_est_stable():
    doc = _doc()
    assert doc["statut"] == "brouillon", (
        "le gel se fait par un commit qui note l'empreinte, après accord")
    a = em.empreinte(doc)
    assert len(a) == 64 and a == em.empreinte(_doc())
