"""Les pré-inscriptions P2 et P4 : registre, filtres et test aveugle."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

from maxprofit.apprentissage.indicateurs import Indicateurs
from maxprofit.apprentissage.lecons import Exemple
from maxprofit.apprentissage.qualite import Qualite
from maxprofit.live.preinscriptions import (
    EXCLUSION_C, EXCLUSION_D, PREINSCRIPTIONS, fenetre_pre_inscrite, juger,
    motifs_de_rejet, retenue)
from maxprofit.live.protocole import protocole, texte
from maxprofit.research.registre import Registre

JOUR = 86400
DEBUT_P, FIN_P = fenetre_pre_inscrite()


def _outil():
    chemin = Path(__file__).resolve().parents[1] / "outils" / "preinscrire.py"
    spec = importlib.util.spec_from_file_location("preinscrire", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entree(t: int, gagne: bool, part: float, espace: float) -> Exemple:
    ctx = {"mouvement_heure": 0.0, "retournement_suivant": 1.0,
           "gagne_suivant": float(gagne), "attente_confirmation": 1.0,
           "gagne_confirme": float(gagne), "e_part_parcourue": part,
           "e_espace_obstacle": espace}
    return Exemple(ts_sec=t, pair=("A_otc", "B_otc")[t % 2], contexte=ctx,
                   gagne=gagne)


def test_la_fenetre_pre_inscrite_et_les_identifiants():
    assert DEBUT_P == 1_791_936_000                    # 2026-10-14 0 h UTC
    assert FIN_P - DEBUT_P == 14 * JOUR
    assert [p.ident for p in PREINSCRIPTIONS] == ["P2-E", "P2-C", "P2-D",
                                                  "P4"]
    assert all(len(p.hypothese) > 100 for p in PREINSCRIPTIONS)


def test_les_exclusions_et_une_valeur_manquante():
    """Amendement du 14/10 : des EXCLUSIONS, comme dans /experiences ; une
    valeur manquante ne déclenche rien."""
    def e(**ctx):
        return Exemple(ts_sec=0, pair="A_otc", contexte=ctx, gagne=True)
    assert retenue(e(part_parcourue=0.3), [EXCLUSION_C])
    assert not retenue(e(part_parcourue=0.46), [EXCLUSION_C])
    assert retenue(e(), [EXCLUSION_C])                    # pas de C : gardée
    assert retenue(e(espace_obstacle=1.95), [EXCLUSION_D])
    assert not retenue(e(espace_obstacle=1.94), [EXCLUSION_D])
    assert retenue(e(espace_obstacle=99.0), [EXCLUSION_D])  # champ libre
    assert retenue(e(part_parcourue=0.3), [EXCLUSION_C, EXCLUSION_D])
    assert motifs_de_rejet({"part_parcourue": 0.5, "espace_obstacle": 1.0},
                           [EXCLUSION_C, EXCLUSION_D]) == ["C ≥ 0,46",
                                                           "D sous 1,95"]


def test_le_verdict_suit_le_critere_ecrit():
    assert juger(20, 15, (0.6, 0.9), 30).startswith("indécise")
    assert juger(100, 52, (0.42, 0.62), 30).startswith("❌")
    assert juger(100, 60, (0.5, 0.7), 30, [100, 62]).startswith("❌")
    assert juger(100, 60, (0.5, 0.7), 30, [100, 55]).startswith("≈")
    assert juger(400, 240, (0.55, 0.65), 100).startswith("✅")


def test_l_outil_inscrit_une_fois_et_conclut(tmp_path):
    outil = _outil()
    with Registre(tmp_path / "research.db", "otc-test") as registre:
        premiere = outil.inscrire(registre)
        seconde = outil.inscrire(registre)
        assert [n for _i, _id, n in premiere] == [True] * 10
        assert [n for _i, _id, n in seconde] == [False] * 10
        toutes = registre.toutes()
        assert len(toutes) == 10 and all(e.sur_scelle for e in toutes)
        p4 = next(e for e in toutes if e.parametres["ident"] == "P4")
        assert p4.parametres["n_min"] == 100
        assert p4.echeance_sec == 120 and not p4.conclue
        # La mission Reference 8 garde SA fenêtre et sa date.
        m1 = next(e for e in toutes if e.parametres["ident"] == "R8-M1")
        assert m1.parametres["declaree_le"] == "2026-10-18"
        assert m1.parametres["exclusions"] == [["taille_bougie", ">=", 1.31]]
        assert p4.parametres["declaree_le"] == "2026-10-13"
    assert outil.campagnes(tmp_path / "research.db") == [("otc-test", 10)]


def _rapport(fin, exemples, serie, ancien=None):
    vide_q = Qualite(DEBUT_P - 10 * JOUR, fin, DEBUT_P)
    vide_i = Indicateurs(DEBUT_P - 10 * JOUR, DEBUT_P)
    r = protocole(
        exemples, lambda e: True, 1, 900, qualite=vide_q,
        indicateurs=vide_i, exclusions={}, couverture=None,
        experiences={"coupure": None, "variables": []}, ancien=ancien,
        version_regles="ZoneH1:abcd1234", version_experiences=7,
        debut=DEBUT_P - 10 * JOUR, fin=fin, gel=None, jours=24, serie=serie)
    return json.loads(json.dumps(r))


def test_le_test_aveugle_des_pre_inscriptions():
    exemples, serie = [], []
    for i in range(240):
        t = DEBUT_P - 10 * JOUR + i * 3 * 3600 + 7
        # Un tiers « en retard » (part 0,8) perd ; les autres gagnent.
        tard = i % 3 == 0
        exemples.append(_entree(t, not tard, 0.8 if tard else 0.2,
                                0.5 if tard else 3.0))
        serie.append(Exemple(ts_sec=t + 60, pair=("A_otc", "B_otc")[i % 2],
                             contexte={}, gagne=i % 5 != 0))
    avant = _rapport(DEBUT_P - JOUR, exemples, serie)
    pre = avant["exp004"]["preinscriptions"]
    assert "resultats" not in pre and pre["ids"] == ["P4"]
    assert "pas encore ouverte" in texte(avant)

    pendant = _rapport(DEBUT_P + 7 * JOUR, exemples, serie, ancien=avant)
    res = {x["id"]: x for x in pendant["exp004"]["preinscriptions"]
           ["resultats"]}
    # P2 a sa validation propre depuis l'amendement : seule P4 est ici.
    assert set(res) == {"P4"}
    assert res["P4"]["reference"] is None
    assert res["P4"]["verdict"].startswith(("✅", "≈", "indécise"))
    groupes = pendant["exp004"]["validation_p2"]["groupes"]
    assert groupes["P2-E"]["n"] == groupes["P2-E"]["g"] > 30
    assert groupes["A"]["n"] > groupes["P2-E"]["n"]
    assert "P2-E" in pendant["journal"] and "P4" in pendant["journal"]
    # Telegram (HTML) refuse un « < », un « > » ou un « & » hors balise.
    nu = re.sub(r"</?(b|code|pre)>", "", texte(pendant))
    assert not re.search(r"[<>&]", nu)

    fini = _rapport(FIN_P + JOUR, exemples, serie, ancien=pendant)
    final = fini["exp004"]["preinscriptions"]
    assert final["final"] and final["fige_le"] == FIN_P + JOUR
    # Figé : le rejeu suivant, sur d'autres données, n'y change rien.
    apres = _rapport(FIN_P + 2 * JOUR, exemples[:10], serie[:10],
                     ancien=fini)
    assert apres["exp004"]["preinscriptions"] == final
    assert "résultat FINAL" in texte(apres)
