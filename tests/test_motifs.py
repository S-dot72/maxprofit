"""
Recherche automatique de motifs (recherche isolée) : le gel, les quantiles,
les fenêtres d'étendues triées au fil de l'eau, les 18 variables et les
états des 22 familles sur un graphique fait à la main, l'absence de lecture
du futur, les issues aux ticks contre un recalcul brut, les comptes en
tableau plat contre un comptage direct, les poids bayésiens, deux scores qui
retrouvent un effet planté, les verdicts, l'isolation du module, une passe
de bout en bout en lecture seule, l'essai sans taux, et la commande.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import math
import random
import shutil
from bisect import bisect_right
from collections import Counter
from pathlib import Path
from statistics import median

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core.types import Tick
from maxprofit.live import engulfing as en
from maxprofit.live import motifs as mf
from maxprofit.live import niveaux as nv
from maxprofit.store.db import open_read_only, open_read_write
from tests.test_engulfing import FIN_DONNEES, Espion, _base, _chemins

RACINE = Path(__file__).resolve().parents[1]
T0 = (mf.DEBUT + 10 * 86400) // 86400 * 86400


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def B(i, o, h, l, c):
    return en.Bougie(T0 + 60 * i, o, h, l, c)


def _marche(n, graine=3):
    alea = random.Random(graine)
    prix, sortie = 1.1, []
    for i in range(n):
        o = prix
        c = round(o * math.exp(alea.gauss(0, 3e-4)), 5)
        h = round(max(o, c) * (1 + abs(alea.gauss(0, 1e-4))), 5)
        l = round(min(o, c) * (1 - abs(alea.gauss(0, 1e-4))), 5)
        sortie.append(B(i, o, h, l, c))
        prix = c
    return sortie


def test_le_gel_tient():
    doc = json.loads((RACINE / "docs" / "preinscription_motifs.json")
                     .read_text(encoding="utf-8"))
    assert doc == mf.PARAMETRES
    assert em.empreinte(mf.PARAMETRES) == mf.EMPREINTE
    autre = json.loads(json.dumps(mf.PARAMETRES))
    autre["familles"]["meches"] = "autre"
    with pytest.raises(ValueError):
        em.exiger_le_gel(autre, mf.EMPREINTE)
    assert len(mf.FAMILLES) == 22 and mf.N_ETATS_TOTAL == 511
    assert len(mf.VARIABLES) == 18 and mf.ECHEANCES == (1, 2, 3, 5)
    assert set(mf.FAMILLES_BAYES) <= set(mf.FAMILLES)
    assert len(mf.FAMILLES_BAYES) == 9
    assert sum(len(f) for _, _, f in mf.GROUPES) == 22
    assert (mf.N_FENETRE, mf.N_M, mf.N_COURT, mf.SERIE_MAX) == (240, 50, 30,
                                                                8)
    assert mf.DEBUT == nv.DEBUT and mf.FIN_EXPLORATION == nv.FIN_EXPLORATION


def test_les_quantiles_et_les_classes():
    assert mf.quantile(list(range(10)), 1, 10) == 1
    assert mf.quantile(list(range(10)), 9, 10) == 9
    assert mf.quantile([4.0], 2, 3) == 4.0
    assert [mf.classe(x, [0.2, 0.5]) for x in (0.1, 0.2, 0.49, 0.5, 9)] == [
        0, 1, 1, 2, 2]
    assert mf.classe(0.0, [0.0, 0.0, 0.0, 1.0]) == 3, "bornes égales"


def test_les_fenetres_donnent_les_medianes_exactes():
    alea = random.Random(2)
    fen = mf.Fenetres()
    suite: list[float] = []
    for k in range(1200):
        if k in (300, 777):
            fen.vider()
            suite = []
        x = round(alea.random(), 3)                 # des égalités exprès
        fen.ajouter(x)
        suite.append(x)
        if len(suite) >= 240:
            for n in (240, 50, 30):
                assert fen.mediane(n) == median(suite[-n:]), (k, n)


def _serie_a_la_main():
    x = [[1.1000, 1.1002, 1.0998, 1.1000] for _ in range(300)]
    for k in range(60, 180):
        x[k] = [1.1000, 1.1005, 1.0995, 1.1000]           # étendue 0,0010
    i = 299
    x[i - 60] = [1.1000, 1.1002, 1.0990, 1.0990]          # rendement_60
    x[i - 30] = [1.1000, 1.1003, 1.0998, 1.1003]          # le plus haut
    x[i - 15] = [1.1000, 1.1002, 1.0996, 1.0996]          # le plus bas
    x[i - 5] = [1.10005, 1.1002, 1.0998, 1.1000]          # R
    x[i - 4] = [1.1000, 1.1002, 1.0998, 1.10005]          # V
    x[i - 3] = [1.1000, 1.1002, 1.0998, 1.10005]          # V
    x[i - 2] = [1.1000, 1.1002, 1.0998, 1.1001]           # V
    x[i - 1] = [1.1001, 1.1002, 1.0998, 1.1000]           # R
    x[i] = [1.0999, 1.1002, 1.0994, 1.1001]               # V, longue mèche basse
    return [B(k, *v) for k, v in enumerate(x)], i


def _medianes(bougies, i):
    e = [b.high - b.low for b in bougies[i - 239:i + 1]]
    return median(e[-50:]), median(e[-30:]), median(e)


def test_les_18_variables_a_la_main():
    bougies, i = _serie_a_la_main()
    med = _medianes(bougies, i)
    m = med[0]
    assert m == pytest.approx(0.0004)
    variables, code = mf.mesurer(nv.SerieU(bougies), i, med)
    v = dict(zip(mf.VARIABLES, variables))
    assert v["rendement_1"] == pytest.approx(0.0001 / m)
    assert v["rendement_3"] == pytest.approx(0.00005 / m)
    assert v["rendement_15"] == pytest.approx(0.0005 / m)
    assert v["rendement_30"] == pytest.approx(-0.0002 / m)
    assert v["rendement_60"] == pytest.approx(0.0011 / m)
    assert v["position_20"] == pytest.approx(0.0007 / 0.0008)
    assert v["position_60"] == pytest.approx(0.0007 / 0.0009)
    assert v["position_240"] == pytest.approx((1.1001 - 1.0990) / 0.0015)
    assert v["volatilite"] == pytest.approx(math.log(med[1] / med[2]))
    assert (v["corps_0"], v["corps_1"], v["corps_2"]) == pytest.approx(
        (0.5, -0.25, 0.25))
    assert (v["meche_haute"], v["meche_basse"]) == pytest.approx(
        (0.125, 0.625))
    assert v["serie"] == 1.0
    assert (v["depuis_haut"], v["depuis_bas"]) == (30.0, 0.0)
    assert code == 1 + 2 * 3 + 1 * 9 + 1 * 27 + 1 * 81 + 2 * 243


def test_les_etats_des_familles():
    bougies, i = _serie_a_la_main()
    variables, code = mf.mesurer(nv.SerieU(bougies), i,
                                 _medianes(bougies, i))
    base = mf.Decisions(["EURUSD_otc"], T0, T0 + 86400)
    base.ajouter(variables, code, {}, 0, False, False)
    bornes = {"taille": [0.2, 0.4], "volatilite": [-1.0, 0.0]}
    for n in mf.NS_RENDEMENT:
        bornes[f"rendement_{n}"] = [-2, -1, -0.5, -0.1, 0, 0.1, 0.5, 1, 2]
    attendu = {
        "couleurs_1": "V", "couleurs_3": "VRV", "couleurs_6": "RVVVRV",
        "couleur_taille_1": "V+", "couleur_taille_3": "Vo Ro V+",
        "meches": "V haute <0,2 basse ≥0,5",
        "rendement_1": "décile 7", "rendement_30": "décile 4",
        "position_20": "quintile 5", "extreme_60": "bas il y a 0 min",
        "serie": "V × 1",
    }
    for famille, nom in attendu.items():
        s = mf.etat(famille, base, 0, bornes)
        assert 0 <= s < mf.N_ETATS[famille], famille
        assert mf.libelle_etat(famille, s, bornes) == nom, famille
    assert mf.etat("couleurs_3", base, 0, bornes) == 0b101
    sans_corps = mf.Decisions(["EURUSD_otc"], T0, T0 + 86400)
    sans_corps.ajouter(variables, 0, {}, 0, False, False)  # toutes D
    for famille in ("couleurs_1", "couleur_taille_2", "meches", "volatilite"):
        assert mf.etat(famille, sans_corps, 0, bornes) == -1, famille
    noms = {mf.libelle_etat(f, s, bornes) for f in mf.FAMILLES
            for s in range(mf.N_ETATS[f])}
    assert len(noms) > 300, "des noms distincts par famille"


def test_une_serie_rouge():
    bougies, i = _serie_a_la_main()
    for k in (i - 2, i - 1, i):
        bougies[k] = B(k, 1.1001, 1.1002, 1.0998, 1.1000)    # R
    variables, code = mf.mesurer(nv.SerieU(bougies), i,
                                 _medianes(bougies, i))
    assert variables[mf.IDX["serie"]] == -3.0
    base = mf.Decisions(["EURUSD_otc"], T0, T0 + 86400)
    base.ajouter(variables, code, {}, 0, False, False)
    s = mf.etat("serie", base, 0, {})
    assert mf.libelle_etat("serie", s, {}) == "R × 3"


def test_aucune_mesure_ne_lit_le_futur():
    bougies = _marche(900, graine=5)
    for i in (300, 517, 800):
        med = _medianes(bougies, i)
        complet = mf.mesurer(nv.SerieU(bougies), i, med)
        coupe = mf.mesurer(nv.SerieU(bougies[:i + 1]), i, med)
        brouille = bougies[:i + 1] + [B(k, 9.0, 9.5, 8.5, 9.1)
                                      for k in range(i + 1, 900)]
        autre = mf.mesurer(nv.SerieU(brouille), i, med)
        assert complet == coupe == autre


def _ticks_irreguliers(minutes: int, graine: int = 9):
    """Des ticks aux secondes 0, 1, 4, 9… (le tick d'entrée à + 2 s est
    celui de la seconde 1) ; une minute sur 97 ne commence qu'à + 40 s, et
    son entrée ou sa sortie n'a alors qu'un tick de 13 s : périmé."""
    alea = random.Random(graine)
    prix, ticks, bougies = 1.1, [], []
    for k in range(minutes):
        m = T0 + 60 * k
        secondes = ((40, 44, 49, 53, 58) if k % 97 == 50
                    else (0, 1, 4, 9, 16, 25, 36, 49))
        px = []
        for s in secondes:
            prix = round(prix * math.exp(alea.gauss(0, 6e-5)), 5)
            ticks.append(Tick("EURUSD_otc", (m + s) * 1000, prix))
            px.append(prix)
        bougies.append(en.Bougie(m, px[0], max(px), min(px), px[-1]))
    return ticks, bougies


def test_les_issues_aux_ticks_contre_un_recalcul_brut():
    ticks, bougies = _ticks_irreguliers(1440 + 400)
    fin = bougies[-1].ts_sec + 60
    base = mf.Decisions(["EURUSD_otc"], bougies[0].ts_sec // 86400 * 86400,
                        fin + 86400)
    n = mf.paire("EURUSD_otc", bougies, fin, en.Ticks(_chemins(ticks)),
                 lambda t: 92.0, base, lambda: None)
    temps = [t.ts_ms for t in ticks]
    attendus, gardes = [], []
    # La dernière minute n'est pas une décision : la minute de son entrée
    # n'a pas de ticks (un tick d'une minute antérieure ne la remplace pas).
    for i in range(239, len(bougies) - 1):
        entree_ms = (bougies[i].ts_sec + 60) * 1000 + 2000
        k = bisect_right(temps, entree_ms) - 1
        if entree_ms - temps[k] > 10_000:
            continue                                  # entrée périmée
        p_e = ticks[k].price
        issues = []
        for t in mf.ECHEANCES:
            sortie_ms = entree_ms + 60_000 * t
            k = bisect_right(temps, sortie_ms) - 1
            if sortie_ms > fin * 1000 or sortie_ms - temps[k] > 10_000:
                issues.append(mf.ABSENTE)
                continue
            p_x = ticks[k].price
            issues.append(1 if p_x > p_e else (-1 if p_x < p_e else 0))
        attendus.append(issues)
        gardes.append(i)
    assert len(gardes) < len(bougies) - 240, "des entrées périmées écartées"
    assert any(mf.ABSENTE in x[:3] for x in attendus), "des sorties périmées"
    assert n == len(attendus) == len(base)
    obtenus = [[base.issues[t][r] for t in mf.ECHEANCES]
               for r in range(len(base))]
    assert obtenus == attendus
    assert {o for ligne in obtenus for o in ligne} >= {1, -1}
    assert sum(base.echantillon) == pytest.approx(n / 3, abs=2)
    # La décision de 23 h 59 entre le lendemain : bloc du lendemain.
    r = gardes.index(1439)
    assert gardes[r - 1] == 1438 and (base.bloc[r - 1], base.bloc[r]) == (
        0, 1)


def _decisions_synthetiques(n, effet=0.0, graine=4):
    """Des décisions en colonnes : avec la probabilité `effet`, la hausse
    suit le signe de rendement_5 ; sinon pile ou face."""
    alea = random.Random(graine)
    base = mf.Decisions(["P0_otc", "P1_otc"], T0, mf.FIN_EXPLORATION
                        + 10 * 86400)
    jours = base.n_jours
    for r in range(n):
        v = [alea.gauss(0, 1) for _ in range(6)]
        v += [alea.random() for _ in range(3)]
        v += [alea.gauss(0, 0.3)] + [alea.gauss(0, 0.5) for _ in range(3)]
        v += [alea.random() * 0.5, alea.random() * 0.5]
        v += [float(alea.choice([-3, -2, -1, 1, 2, 3])),
              float(alea.randrange(60)), float(alea.randrange(60))]
        code = sum(alea.choice([1, 2]) * 3 ** j for j in range(6))
        if alea.random() < effet:
            issue = 1 if v[2] > 0 else -1
        else:
            issue = alea.choice([1, -1])
        jour = r % jours
        bloc = (r % 2) * jours + jour
        validation = base.bloc_en_validation(bloc)
        base.ajouter(v, code, {t: issue for t in mf.ECHEANCES}, bloc,
                     validation, r % 3 == 0)
    return base


def test_les_comptes_contre_un_comptage_direct():
    base = _decisions_synthetiques(3000)
    explo = base.indices(mf.EXPLORATION)
    bornes = mf.bornes_de(base, explo)
    etats = mf.etats_de(base, bornes, lambda: None)
    comptes = mf.Comptes(base.n_blocs)
    comptes.remplir(base, etats, lambda: None)
    direct = Counter()
    for r in range(len(base)):
        for t in mf.ECHEANCES:
            o = base.issues[t][r]
            for f in ("couleurs_2", "rendement_5", "extreme_60"):
                s = etats[f][r]
                if s >= 0:
                    direct[(f, s, t, base.bloc[r], o)] += 1
    for (f, s, t, b, o), n in direct.items():
        par_bloc = comptes.par_bloc(comptes.case(f, s, t), [b])
        assert par_bloc[b][0 if o > 0 else 1] == n
    total = sum(sum(comptes.total(comptes.case("rendement_5", s, 1),
                                  range(base.n_blocs)))
                for s in range(10))
    assert total == len(base)


def test_les_poids_bayesiens():
    base = _decisions_synthetiques(2000)
    blocs = [b for b in range(base.n_blocs)
             if not base.bloc_en_validation(b)]
    explo = base.indices(mf.EXPLORATION)
    bornes = mf.bornes_de(base, explo)
    etats = mf.etats_de(base, bornes, lambda: None)
    comptes = mf.Comptes(base.n_blocs)
    comptes.remplir(base, etats, lambda: None)
    constante, poids = mf.poids_bayesiens(comptes, blocs, 2)
    H = sum(1 for r in explo if base.issues[2][r] == 1)
    Bn = sum(1 for r in explo if base.issues[2][r] == -1)
    assert constante == pytest.approx(math.log((H + 1) / (Bn + 1)))
    s = etats["serie"][explo[0]]
    h = sum(1 for r in explo if etats["serie"][r] == s
            and base.issues[2][r] == 1)
    b = sum(1 for r in explo if etats["serie"][r] == s
            and base.issues[2][r] == -1)
    assert poids["serie"][s] == pytest.approx(
        math.log((h + 1) / (b + 1)) - constante)


def test_les_deux_scores_retrouvent_un_effet_plante():
    base = _decisions_synthetiques(9000, effet=0.6)
    resultats = mf.analyser(base, lambda: None)
    sc = resultats["scores"]
    assert set(sc.logit) == set(mf.ECHEANCES)
    for t, modele in sc.logit.items():
        coefs = dict(zip(("interception",) + mf.VARIABLES,
                         modele.coefficients))
        assert coefs["rendement_5"] > 0.5
        assert all(abs(coefs[v]) < 0.2 for v in mf.VARIABLES
                   if v != "rendement_5")
    valides = [l for l in resultats["lignes"] if l["type"] == "score"
               and l["periode"] == mf.VALIDATION]
    assert len(valides) == 8
    n_valid = len(base.indices(mf.VALIDATION))
    for l in valides:
        assert l["taux"] > 0.7 and l["verdict"] == "validée (BH 10 %)", l
        assert abs(l["n"] - n_valid / 3) < 0.15 * n_valid / 3, \
            "le tiers le plus confiant"
    rend = [l for l in resultats["lignes"] if l["type"] == "famille"
            and l["famille"] == "rendement_5" and l["verdict"]
            == "validée (BH 10 %)"]
    assert rend, "les déciles extrêmes de rendement_5 sont validés"


def test_les_verdicts_et_benjamini_hochberg():
    X, V = mf.EXPLORATION, mf.VALIDATION

    def l(type_, fam, per, n, taux, p=None, etat="e"):
        return {"type": type_, "famille": fam, "etat": etat,
                "echeance_min": 1, "periode": per, "n": n, "taux": taux,
                "p": p, "verdict": ""}
    lignes = [l("famille", "a", X, 299, 0.6), l("famille", "a", V, 500, 0.6, 0.01),
              l("famille", "b", X, 400, 0.52), l("famille", "b", V, 500, 0.6, 0.01),
              l("famille", "c", X, 400, 0.55), l("famille", "c", V, 99, 0.6, 0.01),
              l("famille", "d", X, 400, 0.55), l("famille", "d", V, 500, 0.58, 0.001),
              l("famille", "e", X, 400, 0.55), l("famille", "e", V, 500, 0.5, 0.6),
              l("score", "s", X, 9000, 0.6, etat=""),
              l("score", "s", V, 9000, 0.53, 0.002, etat="")]
    mf.verdicts(lignes)
    v = {(x["famille"], x["periode"]): x["verdict"] for x in lignes}
    assert v[("a", X)] == "trop peu en exploration"
    assert v[("b", X)] == "non retenue en exploration"
    assert v[("c", V)] == "trop peu en validation"
    assert v[("d", V)] == "validée (BH 10 %)"
    assert v[("e", V)] == "non reproduite"
    assert v[("s", V)] == "validée (BH 10 %)"


def test_le_module_est_isole():
    source = (RACINE / "maxprofit" / "live" / "motifs.py").read_text(
        encoding="utf-8")
    importes = set()
    for noeud in ast.walk(ast.parse(source)):
        if isinstance(noeud, ast.ImportFrom) and noeud.module:
            importes.add(noeud.module)
        elif isinstance(noeud, ast.Import):
            importes.update(a.name for a in noeud.names)
    for interdit in ("maxprofit.execution", "maxprofit.collect",
                     "maxprofit.plan", "maxprofit.live.plan_demo",
                     "maxprofit.hosting", "maxprofit.strategies"):
        assert not any(m == interdit or m.startswith(interdit + ".")
                       for m in importes), interdit
    for mot in ("INSERT", "UPDATE ", "DELETE", "upsert", "insert_"):
        assert mot not in source, mot


@pytest.fixture(scope="module")
def passe(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("motifs")
    active = dossier / "active.db"
    _base(active)
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    empreintes = [hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (active, releve)]
    a, r = open_read_only(active), open_read_only(releve)
    espion = Espion(r)
    try:
        rapport, fichiers = mf.etude(espion, a, maintenant=FIN_DONNEES + 3600,
                                     respirer=lambda: None, version="abc1234")
    finally:
        a.close()
        r.close()
    return active, releve, empreintes, espion, rapport, fichiers


def _lignes(fichiers, nom):
    contenu = next(c for c, n, _ in fichiers if n == nom)
    lignes = contenu.decode("utf-8").splitlines()
    tete = lignes[0].split(";")
    return [dict(zip(tete, l.split(";"))) for l in lignes[1:]]


def test_passe_en_lecture_seule_sans_rien_lire_apres_la_fin(passe):
    active, releve, empreintes, espion, rapport, fichiers = passe
    assert [hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (active, releve)] == empreintes
    assert "requêtes refusées : 0" in rapport and "Gel vérifié" in rapport
    assert "aucun ordre" in rapport and "Version : abc1234" in rapport
    assert espion.bornes and max(espion.bornes) <= FIN_DONNEES
    assert [n for _, n, _ in fichiers] == [
        "motifs_cases.csv", "motifs_details.csv", "motifs_parametres.csv",
        "motifs_statuts.csv", "motifs_donnees.csv"]


def test_passe_complete_cases_et_rapport(passe):
    *_, rapport, fichiers = passe
    cases = _lignes(fichiers, "motifs_cases.csv")
    familles = [c for c in cases if c["type"] == "famille"]
    scores = [c for c in cases if c["type"] == "score"]
    assert len(familles) == 2044 * 2 and len(scores) == 8 * 2
    for c in familles:
        n = int(c["n"])
        if n:
            g = int(c["hausses" if c["sens_choisi"] == "hausse"
                      else "baisses"])
            assert abs(float(c["taux"]) - g / n) < 1e-6
    assert {c["sens_choisi"] for c in familles} <= {"hausse", "baisse"}
    parametres = _lignes(fichiers, "motifs_parametres.csv")
    assert sum(p["cle"] == "seuil_confiance" for p in parametres) == 8
    assert any(p["cle"] == "coef:rendement_5" for p in parametres)
    statuts = _lignes(fichiers, "motifs_statuts.csv")
    assert {"décisions", "ticks"} <= {s["source"] for s in statuts}
    for _, libelle, _ in mf.GROUPES:
        assert libelle in rapport
    assert "Score bayésien" in rapport and "Score logistique" in rapport
    assert "22 h" not in rapport and "par heure" not in rapport



def test_l_essai_ne_montre_aucun_taux(passe):
    active, releve, empreintes, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    try:
        texte, fichiers = mf.etude(r, a, maintenant=FIN_DONNEES + 3600,
                                   respirer=lambda: None, essai=True,
                                   version="abc1234")
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "%" not in texte
    assert texte.startswith("🔬 <b>Essai technique /motifs</b>")
    assert "scores (8 ajustés), rapport et fichiers : passés" in texte
    assert mf.avancement() is None


def test_avant_la_fin_de_l_exploration_rien_a_valider(passe):
    active, releve, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    try:
        texte, fichiers = mf.etude(r, a, maintenant=mf.FIN_EXPLORATION - 60,
                                   respirer=lambda: None)
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "rien à valider" in texte


def test_l_avancement_suit_la_passe(passe):
    assert mf.avancement() is None
    import time as horloge
    maintenant = horloge.monotonic()
    mf._ETAT.update(debut=maintenant - 600, etape="paire EURUSD_otc",
                    faites=5, total=20, signaux=90000,
                    debut_paires=maintenant - 300, rss_max=250.0)
    try:
        point = mf.avancement()
        assert "5/20 paires" in point and "90000 décisions" in point
        assert "fin des paires dans ~15 min" in point
    finally:
        mf._ETAT.clear()


def test_le_service_et_la_commande(monkeypatch, tmp_path):
    from maxprofit.hosting import service
    from maxprofit.store import pool
    vus = []

    def faux(lue, active, course=None, *, nom_lecture, essai=False,
             version=None):
        vus.append((nom_lecture, essai))
        return "🔬 Motifs", []

    monkeypatch.setattr(mf, "etude", faux)
    chemin = tmp_path / "active.db"
    open_read_write(chemin).close()
    texte, _ = service._motifs(chemin, None, "")
    assert "Aucune relève" in texte
    for argument in ("", "essai"):
        texte, _ = service._motifs(
            chemin, None, argument, environ={pool.ENV_RELEVE: str(chemin)},
            ouvrir_url=lambda url: open_read_only(url))
        assert texte == "🔬 Motifs"
    assert vus == [("relève", False), ("relève", True)]

    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "motifs" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))

    async def motifs(argument):
        await asyncio.sleep(0.2)
        return "rapport", [(b"a;b\n", "motifs_cases.csv", "")]

    bot._motifs = motifs
    bot._motifs_avancement = lambda: "paire EURUSD_otc ; 3/20 paires"
    bot.premier_avancement_sec = 0.05
    bot.intervalle_avancement_sec = 0.05
    monkeypatch.setattr("maxprofit.hosting.version.commit",
                        lambda: "abc1234")

    async def deroule():
        await bot._traiter(_message("/motifs", chat=INTRUS)["message"])
        assert documents == []
        await bot._traiter(_message("/motifs")["message"])
        await asyncio.sleep(0.01)
        await bot._traiter(_message("/motifs")["message"])
        await bot._motifs_en_cours
        await bot._motifs_veille
        assert bot._en_fond == {}

    asyncio.run(deroule())
    assert documents == ["motifs_cases.csv"]
    textes = [t for _, t in client.envoyes]
    assert any(t.startswith("🔬 Recherche automatique de motifs lancée "
                            "(version abc1234") for t in textes)
    assert any(t.startswith("🔬 La recherche de motifs est déjà en cours : "
                            "paire") for t in textes)
    assert any(t.startswith("⏳ 🔬 Motifs : paire") for t in textes)
