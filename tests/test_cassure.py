"""
Cassure ou retournement au toucher d'une zone (recherche isolée) : le gel,
les quantiles et les tiers, le chemin des zones identique à celui de
/niveaux, la confluence contre un comptage brut, les dix signes sur un
graphique fait à la main (et son miroir), la fenêtre exigée, l'absence de
lecture du futur, les touchers identiques aux trades de /niveaux, la
préparation des variables, un score qui retrouve un effet planté, les
verdicts, l'isolation du module, une passe de bout en bout en lecture
seule, l'essai sans taux, et la commande.
"""

from __future__ import annotations

import ast
import asyncio
import gzip
import hashlib
import json
import math
import random
import shutil
from collections import Counter
from pathlib import Path
from statistics import median

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.live import cassure as cs
from maxprofit.live import engulfing as en
from maxprofit.live import niveaux as nv
from maxprofit.store.db import open_read_only, open_read_write
from tests.test_engulfing import FIN_DONNEES, Espion, _base, _chemins
from tests.test_niveaux import _ticks_et_bougies

RACINE = Path(__file__).resolve().parents[1]
T0 = (cs.DEBUT + 10 * 86400) // 86400 * 86400
PAIRE = "EURUSD_otc"


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def B(i: int, o: float, h: float, l: float, c: float) -> en.Bougie:
    return en.Bougie(T0 + 60 * i, o, h, l, c)


def _marche(n: int, graine: int = 3):
    alea = random.Random(graine)
    prix, sortie = 1.1000, []
    for i in range(n):
        o = prix
        c = round(o * math.exp(alea.gauss(0, 3e-4)), 5)
        h = round(max(o, c) * (1 + abs(alea.gauss(0, 1e-4))), 5)
        l = round(min(o, c) * (1 - abs(alea.gauss(0, 1e-4))), 5)
        sortie.append(B(i, o, h, l, c))
        prix = c
    return sortie


def test_le_gel_tient():
    doc = json.loads((RACINE / "docs" / "preinscription_cassure.json")
                     .read_text(encoding="utf-8"))
    assert doc == cs.PARAMETRES
    assert em.empreinte(cs.PARAMETRES) == cs.EMPREINTE
    autre = json.loads(json.dumps(cs.PARAMETRES))
    autre["signes"].pop("confluence")
    with pytest.raises(ValueError):
        em.exiger_le_gel(autre, cs.EMPREINTE)
    assert cs.DEFINITIONS == ("R1_M1_w5_s0", "R1_M5_w5_s0", "R1_M15_w5_s0")
    assert set(cs.DEFINITIONS) <= set(nv.DEFINITIONS)
    assert cs.SIGNES == tuple(cs.PARAMETRES["signes"])
    assert len(cs.SIGNES) == 10 and len(cs.CROISEMENTS) == 45
    assert cs.ECHEANCES == nv.ECHEANCES and cs.DEBUT == nv.DEBUT
    assert cs.FIN_EXPLORATION == nv.FIN_EXPLORATION
    assert (cs.N_CONTINU, cs.N_JOUR, cs.N_M, cs.N_COURT, cs.DEMI) == (
        240, 720, 50, 30, 0.5)
    assert (cs.N_EXPLO_MIN, cs.N_VALID_MIN, cs.LAMBDA) == (300, 100, 1.0)
    assert len(cs.noms_des_variables("score_simple")) == 13
    assert len(cs.noms_des_variables("score_croise")) == 58


def test_les_quantiles_et_les_tiers():
    v = list(range(10))
    assert cs.quantile(v, 1, 3) == 3 and cs.quantile(v, 2, 3) == 6
    assert cs.quantile(v, 1, 100) == 0 and cs.quantile(v, 99, 100) == 9
    assert cs.quantile([5.0], 2, 3) == 5.0
    assert cs.quantile(list(range(300)), 1, 3) == 100, "calcul entier"
    with pytest.raises(ValueError):
        cs.quantile([], 1, 3)
    assert [cs.tiers_de(x, 2.0, 5.0) for x in (1.9, 2.0, 4.9, 5.0, 9)] == [
        0, 1, 1, 2, 2]
    assert [cs.tiers_de(x, 0.0, 0.0) for x in (-1, 0.0, 0.1)] == [0, 1, 2], \
        "b1 = b2 : le milieu est la valeur répétée"


def test_le_parcours_suit_celui_de_niveaux():
    bougies = _marche(3000, graine=8)
    bougies = bougies[:1200] + bougies[1203:]          # un trou de 3 minutes
    s1 = nv.SerieU(bougies)
    vus = Counter()
    for u in cs.UNITES:
        su = s1 if u == 1 else nv.SerieU(nv.regrouper(bougies, u), 60 * u)
        for niv in nv.niveaux_de(su, 5):
            i, statut, fin = cs.parcours(s1, niv)
            assert (i, statut) == nv.minute_du_toucher(s1, niv)
            vus[statut] += 1
            if statut == nv.TOUCHER:
                assert fin == s1.ts[i]
            elif statut == nv.SANS_TOUCHER:
                assert fin == niv.activation + 86400 - 1
            elif statut == nv.TROU:
                manquante = fin + 1
                assert manquante % 60 == 0 and manquante not in set(s1.ts)
                assert manquante - 60 in set(s1.ts), "la minute d'avant existe"
    assert vus[nv.TOUCHER] > 100 and vus[nv.TROU] >= 1
    absente = nv.Niveau(T0 + 60 * 1201, 9.9, nv.PUT, None, 0.001)
    assert cs.parcours(s1, absente) == (None, nv.TROU, T0 + 60 * 1201 - 1)


def test_la_confluence():
    zones = [(100, 400, 1.1000, nv.PUT, 1),      # active de 100 à 400
             (100, 400, 1.1005, nv.PUT, 2),      # trop loin (≥ 0,0004)
             (100, 400, 1.1001, nv.CALL, 3),     # autre camp
             (250, 300, 1.0999, nv.PUT, 4),      # active de 250 à 300
             (90, 95, 1.1000, nv.PUT, 5)]        # finie avant
    act = cs.Actives(zones)
    assert act.compter(200, 1.1000, nv.PUT, 0, 0.0004) == 1
    assert act.compter(260, 1.1000, nv.PUT, 0, 0.0004) == 2
    assert act.compter(260, 1.1000, nv.PUT, 1, 0.0004) == 1, "pas soi-même"
    assert act.compter(300, 1.1000, nv.PUT, 0, 0.0004) == 2, "fin incluse"
    assert act.compter(301, 1.1000, nv.PUT, 0, 0.0004) == 1
    assert act.compter(401, 1.1000, nv.PUT, 0, 0.0004) == 0
    with pytest.raises(ValueError):
        act.compter(10, 1.1, nv.PUT, 0, 0.1)


def _serie_a_la_main():
    """1 500 bougies M1 d'étendue 0,0002 ; la zone des vendeurs à 1,1010,
    activée à l'indice 1380, touchée à l'indice 1400. Chaque signe a une
    valeur connue d'avance."""
    x = [[1.1000, 1.1001, 1.0999, 1.1000] for _ in range(1500)]
    i, a = 1400, 1380
    for k in range(i - 240, i - 120):
        x[k] = [1.1000, 1.1005, 1.0995, 1.1000]        # étendue 0,0010
    x[i - 60] = [1.1000, 1.1001, 1.0995, 1.0996]        # élan
    x[1385] = [1.1000, 1.1001, 1.0990, 1.1000]          # éloignement
    for k in (1390, 1395):
        x[k] = [1.1000, 1.10095, 1.0999, 1.1000]        # approches manquées
    x[1395][3] = 1.1004                                 # clôture à i − 5
    x[a - 1] = [1.1000, 1.10095, 1.0999, 1.1000]        # avant l'activation :
    #                                                     ne compte pas
    x[i - 1] = [1.1000, 1.1001, 1.0999, 1.1001]         # dernière bougie
    bougies = [B(k, *v) for k, v in enumerate(x)]
    niv = nv.Niveau(T0 + 60 * a, 1.1010, nv.PUT, None, 0.0010)
    return bougies, i, niv


ATTENDUS = (3.0, 7.0, math.log(3), math.log(21), math.log(6), math.log(11),
            math.log(0.2), 0.5, math.log(3), 0.0020 / 0.00195)


def test_les_dix_signes_a_la_main():
    bougies, i, niv = _serie_a_la_main()
    demandes = []

    def confluence(demi):
        demandes.append(demi)
        return 2
    signes, statut = cs.mesurer(nv.SerieU(bougies), i, niv, confluence)
    assert statut == cs.MESURE
    assert demandes == [pytest.approx(0.0001)], "à moins de 0,5 m"
    for nom, obtenu, attendu in zip(cs.SIGNES, signes, ATTENDUS):
        assert obtenu == pytest.approx(attendu, rel=1e-6, abs=1e-9), nom


def test_le_miroir_donne_les_memes_signes():
    bougies, i, niv = _serie_a_la_main()
    miroir = [en.Bougie(b.ts_sec, 2.2 - b.open, 2.2 - b.low, 2.2 - b.high,
                        2.2 - b.close) for b in bougies]
    acheteurs = nv.Niveau(niv.activation, 2.2 - niv.prix, nv.CALL, None,
                          niv.ecart)
    signes, statut = cs.mesurer(nv.SerieU(miroir), i, acheteurs,
                                lambda demi: 2)
    assert statut == cs.MESURE
    for nom, obtenu, attendu in zip(cs.SIGNES, signes, ATTENDUS):
        assert obtenu == pytest.approx(attendu, rel=1e-6, abs=1e-9), nom


def test_la_fenetre_exigee():
    bougies, i, niv = _serie_a_la_main()
    troue = bougies[:i - 100] + bougies[i - 99:]
    assert cs.mesurer(nv.SerieU(troue), i - 1, niv, lambda d: 0) == (
        None, cs.FENETRE_INCOMPLETE)
    court = bougies[800:]
    assert cs.mesurer(nv.SerieU(court), i - 800, niv, lambda d: 0) == (
        None, cs.JOUR_INCOMPLET), "600 minutes seulement dans les 24 h"
    plat = [B(k, 1.1, 1.1, 1.1, 1.1) for k in range(1500)]
    assert cs.mesurer(nv.SerieU(plat), i, niv, lambda d: 0) == (
        None, cs.ETENDUE_NULLE)
    assert cs.mesurer(nv.SerieU(bougies), 200, niv, lambda d: 0)[1] == \
        cs.FENETRE_INCOMPLETE


def test_aucun_signe_ne_lit_le_futur():
    bougies = _marche(2600, graine=13)
    s1 = nv.SerieU(bougies)
    essais = 0
    for niv in nv.niveaux_de(s1, 5):
        i, statut, _ = cs.parcours(s1, niv)
        if statut != nv.TOUCHER or i < 1500 or i > 2500:
            continue
        complet = cs.mesurer(s1, i, niv, lambda d: 1)
        coupe = cs.mesurer(nv.SerieU(bougies[:i + 1]), i, niv, lambda d: 1)
        brouille = bougies[:i] + [B(k, 9.0, 9.5, 8.5, 9.1)
                                  for k in range(i, 2600)]
        autre = cs.mesurer(nv.SerieU(brouille), i, niv, lambda d: 1)
        assert complet == coupe == autre and complet[1] == cs.MESURE
        essais += 1
    assert essais > 20


def test_les_touchers_sont_les_trades_de_niveaux_et_la_confluence_brute():
    _, ticks, bougies = _ticks_et_bougies(2 * 1440 + 300, graine=31)
    fin = bougies[-1].ts_sec + 60
    touchers = cs.paire(PAIRE, bougies, fin, en.Ticks(_chemins(ticks)),
                        lambda t: 92.0, Counter(), lambda: None)
    signaux = nv.paire(PAIRE, bougies, fin, en.Ticks(_chemins(ticks)),
                       lambda t: 92.0, Counter(), lambda: None)
    voulus = set(cs.DEFINITIONS)
    attendus = [(nv.DEFINITIONS[s.definition], s.reference_ms, s.sens)
                for s in signaux if nv.DEFINITIONS[s.definition] in voulus]
    obtenus = [(cs.DEFINITIONS[t.definition], t.reference_ms, t.sens)
               for t in touchers]
    assert sorted(obtenus) == sorted(attendus)
    assert len(obtenus) > 200
    issues = {(nv.DEFINITIONS[s.definition], s.reference_ms): s.issues
              for s in signaux}
    mesures = [t for t in touchers if t.signes is not None]
    assert len(mesures) > 0.5 * len(touchers)
    for t in mesures:
        assert t.issues == issues[(cs.DEFINITIONS[t.definition],
                                   t.reference_ms)]
    # La confluence, recomptée à la main sur toutes les zones.
    s1 = nv.SerieU(bougies)
    zones = []
    for u in cs.UNITES:
        su = s1 if u == 1 else nv.SerieU(nv.regrouper(bougies, u), 60 * u)
        for niv in nv.niveaux_de(su, 5):
            _, _, borne = cs.parcours(s1, niv)
            zones.append((niv.activation, borne, niv.prix, niv.sens))
    non_nulles = 0
    for t in mesures:
        minute = s1.ts[t.indice]
        m = median(s1.h[k] - s1.l[k]
                   for k in range(t.indice - 50, t.indice))
        brut = sum(1 for z, (a, borne, prix, sens) in enumerate(zones)
                   if z != t.zone and sens == t.sens and a <= minute <= borne
                   and abs(prix - t.niveau.prix) < 0.5 * m)
        assert round(math.expm1(t.signes[8])) == brut
        non_nulles += brut > 0
    assert non_nulles > 10


def _base_synthetique(n: int, effet: float, graine: int = 5,
                      egalites: int = 0) -> cs.Touchers:
    """Des touchers en colonnes : le premier signe (vitesse) décide du
    retournement avec la probabilité `effet`, le reste au hasard."""
    alea = random.Random(graine)
    base = cs.Touchers()
    for r in range(n):
        signes = [alea.gauss(0, 1) for _ in cs.SIGNES]
        valide = r % 2 == 1
        if alea.random() < effet:
            issue = 1 if signes[0] > 0 else -1
        else:
            issue = 1 if alea.random() < 0.5 else -1
        if r < 2 * egalites:
            issue = 0
        for j, v in enumerate(signes):
            base.signes[j].append(v)
        for t in cs.ECHEANCES:
            base.issues[t].append(issue)
        cle = (f"P{r % 4}_otc", r // 400)
        if cle not in base.blocs:
            base.blocs[cle] = len(base.noms_blocs)
            base.noms_blocs.append(cle)
        base.bloc.append(base.blocs[cle])
        base.validation.append(valide)
        base.unite.append(r % 3)
        base.acheteur.append(r % 5 == 0)
    return base


def test_le_sens_choisi_et_les_tiers_des_cases():
    base = _base_synthetique(1200, 0.0)
    for r in range(len(base)):
        for j in range(len(cs.SIGNES)):
            base.signes[j][r] = 0.0 if j != 0 else float(r % 3)
        for t in cs.ECHEANCES:
            base.issues[t][r] = 1 if (r // 2) % 2 == 0 else -1
    explo = base.indices(cs.EXPLORATION)
    bornes = cs.bornes_des_tiers(base, explo)
    assert bornes[0] == (1.0, 2.0) and bornes[5] == (0.0, 0.0)
    tirages = {per: en.Tirages([]) for per in (cs.EXPLORATION,
                                                 cs.VALIDATION)}
    lignes = cs.cases_des_signes(base, bornes, tirages, lambda: None)
    assert len(lignes) == 10 * 3 * 4 * 2
    vitesse = [l for l in lignes if l["nom"] == "vitesse"
               and l["periode"] == cs.EXPLORATION and l["echeance_min"] == 1]
    assert [l["n"] for l in vitesse] == [200, 200, 200]
    for l in lignes:
        if l["n"] and l["retournements"] == l["cassures"]:
            assert l["sens_choisi"] == cs.RETOURNEMENT, \
                "à égalité, le sens de la règle"
    vides = [l for l in lignes if l["nom"] == "elan" and l["tiers"] == "bas"]
    assert vides and all(l["n"] == 0 and l["taux"] is None for l in vides)


def test_la_preparation_des_variables():
    base = _base_synthetique(1000, 0.0)
    for k in range(len(base)):
        base.signes[9][k] = 3.0                     # écart-type nul
    base.signes[1][0] = 1e6                         # une valeur aberrante
    explo = base.indices(cs.EXPLORATION)
    prep = cs.preparation(base, explo)
    bas, haut, moy, et = prep[1]
    triees = sorted(base.signes[1][r] for r in explo)
    assert (bas, haut) == (triees[len(triees) // 100],
                           triees[len(triees) * 99 // 100])
    assert haut < 1e6, "l'aberrante est bornée"
    assert prep[9][3] == 0.0
    x = cs.variables(base, 0, prep, "score_simple")
    assert len(x) == 13 and x[9] == 0.0
    assert x[1] == pytest.approx((haut - moy) / et)
    assert x[10:] == [0.0, 0.0, 1.0], "unité M1, zone d'acheteurs"
    xc = cs.variables(base, 0, prep, "score_croise")
    assert len(xc) == 58 and xc[13] == pytest.approx(x[0] * x[1])


def test_un_score_retrouve_un_effet_plante():
    base = _base_synthetique(4000, 0.8, egalites=40)
    explo = base.indices(cs.EXPLORATION)
    prep = cs.preparation(base, explo)
    scores = cs.ajuster_les_scores(base, explo, prep, lambda: None)
    assert [(s.nom, s.echeance) for s in scores] == [
        (n, t) for n in cs.SCORES for t in cs.ECHEANCES]
    decisifs = sum(1 for r in explo if base.issues[1][r] in (1, -1))
    assert decisifs < len(explo), "des égalités en exploration"
    for sc in scores:
        assert sc.n_ajustement == decisifs, "égalités exclues de l'ajustement"
        coefs = sc.modele.coefficients
        assert coefs[1] > 1.0 and all(abs(c) < 0.5 for c in coefs[2:11])
        confiances = sorted(
            abs(sc.modele.proba(cs.variables(base, r, prep, sc.nom)) - 0.5)
            for r in explo)
        assert sc.seuil == confiances[len(confiances) * 2 // 3]
    tirages = {per: en.Tirages(sorted({base.bloc[r] for r in range(len(base))
                                       if bool(base.validation[r])
                                       == (per == cs.VALIDATION)}))
               for per in (cs.EXPLORATION, cs.VALIDATION)}
    lignes, details = cs.cases_des_scores(base, scores, tirages)
    cs.verdicts(lignes)
    for l in lignes:
        if l["periode"] == cs.VALIDATION:
            assert 600 <= l["n"] <= 750, "le tiers le plus confiant"
            assert l["taux"] > 0.85
            assert l["verdict"] == "validée (BH 10 %)"
    # Les égalités comptent comme des pertes, quel que soit le sens prédit.
    sc = scores[0]
    egalites_jouees = sum(
        1 for r in range(len(base)) if base.validation[r]
        and base.issues[1][r] == 0
        and sc.decision(cs.variables(base, r, prep, sc.nom)) is not None)
    v = next(l for l in lignes if l["nom"] == sc.nom
             and l["echeance_min"] == 1 and l["periode"] == cs.VALIDATION)
    gagnes = sum(1 for r in range(len(base)) if base.validation[r]
                 and base.issues[1][r] != 0
                 and sc.decision(cs.variables(base, r, prep, sc.nom))
                 == base.issues[1][r])
    assert egalites_jouees > 0 and v["taux"] * v["n"] == pytest.approx(gagnes)
    assert sum(n for g, n in details[(sc.nom, 1)].values()) == 2 * v["n"]


def test_les_verdicts_et_benjamini_hochberg():
    X, V = cs.EXPLORATION, cs.VALIDATION

    def l(fam, nom, per, n, taux, p=None, tiers="bas"):
        return {"famille": fam, "nom": nom, "tiers": tiers,
                "echeance_min": 1, "periode": per, "n": n, "taux": taux,
                "p": p, "verdict": ""}
    lignes = [l("signe", "a", X, 299, 0.60), l("signe", "a", V, 500, 0.6, 0.01),
              l("signe", "b", X, 400, 0.52), l("signe", "b", V, 500, 0.6, 0.01),
              l("signe", "c", X, 400, 0.55), l("signe", "c", V, 99, 0.6, 0.01),
              l("signe", "d", X, 400, 0.55), l("signe", "d", V, 500, 0.58, 0.001),
              l("signe", "e", X, 400, 0.55), l("signe", "e", V, 500, 0.50, 0.6),
              l("score", "s", X, 9000, 0.60, tiers=""),
              l("score", "s", V, 9000, 0.53, 0.002, tiers=""),
              l("score", "t", X, 9000, 0.60, tiers=""),
              l("score", "t", V, 50, 0.70, 0.001, tiers="")]
    cs.verdicts(lignes)
    v = {(x["nom"], x["periode"]): x["verdict"] for x in lignes}
    assert v[("a", X)] == "trop peu en exploration"
    assert v[("b", X)] == "non retenue en exploration"
    assert v[("c", X)] == "retenue, trop peu en validation"
    assert v[("c", V)] == "trop peu en validation"
    assert v[("d", X)] == "retenue en exploration"
    assert v[("d", V)] == "validée (BH 10 %)"
    assert v[("e", V)] == "non reproduite"
    assert v[("s", V)] == "validée (BH 10 %)"
    assert v[("s", X)] == "ajusté ici (en échantillon)"
    assert v[("t", V)] == "trop peu en validation"


def test_les_cases_attendues_au_hasard():
    lignes = [{"famille": "signe", "periode": "exploration", "n": 1000},
              {"famille": "signe", "periode": "exploration", "n": 100},
              {"famille": "score", "periode": "exploration", "n": 5000},
              {"famille": "signe", "periode": "validation", "n": 1000}]
    z = (cs.POINT_MORT - 0.5) * 2 * math.sqrt(1000)
    attendu = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))
    assert abs(cs.attendues_au_hasard(lignes) - attendu) < 1e-12


def test_le_module_est_isole():
    source = (RACINE / "maxprofit" / "live" / "cassure.py").read_text(
        encoding="utf-8")
    arbre = ast.parse(source)
    importes = set()
    for noeud in ast.walk(arbre):
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
    dossier = tmp_path_factory.mktemp("cassure")
    active = dossier / "active.db"
    _base(active)
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    empreintes = [hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (active, releve)]
    a, r = open_read_only(active), open_read_only(releve)
    espion = Espion(r)
    try:
        rapport, fichiers = cs.etude(espion, a, maintenant=FIN_DONNEES + 3600,
                                     respirer=lambda: None, version="abc1234")
    finally:
        a.close()
        r.close()
    return active, releve, empreintes, espion, rapport, fichiers


def _lignes(fichiers, nom):
    contenu = next(c for c, n, _ in fichiers if n == nom)
    if nom.endswith(".gz"):
        contenu = gzip.decompress(contenu)
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
    assert min(espion.bornes) >= cs.DEBUT - 86400
    assert [n for _, n, _ in fichiers] == [
        "cassure_cases.csv", "cassure_details.csv", "cassure_scores.csv",
        "cassure_statuts.csv", "cassure_donnees.csv",
        "cassure_touchers.csv.gz"]


def test_passe_complete_cases_scores_touchers(passe):
    *_, rapport, fichiers = passe
    cases = _lignes(fichiers, "cassure_cases.csv")
    signes = [c for c in cases if c["famille"] == "signe"]
    scores = [c for c in cases if c["famille"] == "score"]
    assert len(signes) == 10 * 3 * 4 * 2 and len(scores) == 2 * 4 * 2
    assert {c["sens_choisi"] for c in signes} <= {"retournement", "cassure"}
    for c in signes:
        n = int(c["n"])
        if n:
            g = int(c["retournements" if c["sens_choisi"] == "retournement"
                      else "cassures"])
            assert abs(float(c["taux"]) - g / n) < 1e-6
    assert all(int(c["n"]) > 0 for c in scores)
    touchers = _lignes(fichiers, "cassure_touchers.csv.gz")
    assert touchers and {t["definition"] for t in touchers} == set(
        cs.DEFINITIONS)
    assert {t["periode"] for t in touchers} == {"exploration", "validation"}
    mesures = [t for t in touchers if t["mesure"] == cs.MESURE]
    assert mesures and all(t["vitesse"] != "" for t in mesures)
    par_def = {}
    for t in touchers:
        par_def.setdefault((t["definition"], t["paire"]), []).append(
            int(t["reference_ms"]))
    for refs in par_def.values():
        assert all(b - a >= nv.EPISODE_MS for a, b in zip(refs, refs[1:]))
    coefs = _lignes(fichiers, "cassure_scores.csv")
    assert any(c["element"] == "coef:vitesse" for c in coefs)
    assert any(c["element"] == "coef:vitesse×elan" for c in coefs)
    assert sum(c["element"] == "seuil_confiance" for c in coefs) == 8
    statuts = _lignes(fichiers, "cassure_statuts.csv")
    assert {"niveaux", "épisodes", "mesure", "ticks"} <= {
        s["source"] for s in statuts}
    for signe in cs.SIGNES:
        assert cs.LIBELLES[signe] in rapport
    assert "Score simple" in rapport and "Score croisé" in rapport
    assert "passeraient" not in rapport and "hasard en donnerait" in rapport
    assert "heure" not in rapport.replace("Élan de l'heure", "").lower(), \
        "aucune ventilation par heure"


def test_l_essai_ne_montre_aucun_taux(passe):
    active, releve, empreintes, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    espion = Espion(r)
    try:
        texte, fichiers = cs.etude(espion, a, maintenant=FIN_DONNEES + 3600,
                                   respirer=lambda: None, essai=True,
                                   version="abc1234")
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "%" not in texte
    assert texte.startswith("🧱 <b>Essai technique /cassure</b>")
    assert "Statistiques, scores (8 ajustés), rapport et fichiers : passés" \
        in texte
    assert "Version déployée : abc1234" in texte
    assert max(espion.bornes) <= FIN_DONNEES
    assert cs.avancement() is None


def test_avant_la_fin_de_l_exploration_rien_a_valider(passe):
    active, releve, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    try:
        texte, fichiers = cs.etude(r, a, maintenant=cs.FIN_EXPLORATION - 60,
                                   respirer=lambda: None)
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "rien à valider" in texte


def test_l_avancement_suit_la_passe(passe):
    assert cs.avancement() is None
    import time as horloge
    maintenant = horloge.monotonic()
    cs._ETAT.update(debut=maintenant - 600, etape="paire EURUSD_otc",
                    faites=5, total=20, signaux=900,
                    debut_paires=maintenant - 300, rss_max=250.0)
    try:
        point = cs.avancement()
        assert "5/20 paires" in point and "900 touchers" in point
        assert "fin des paires dans ~15 min" in point
        assert "pic de l'étude 250 Mo" in point
    finally:
        cs._ETAT.clear()


def test_le_service_et_la_commande(monkeypatch, tmp_path):
    from maxprofit.hosting import service
    from maxprofit.store import pool
    vus = []

    def faux(lue, active, course=None, *, nom_lecture, essai=False,
             version=None):
        vus.append((nom_lecture, essai))
        return "🧱 Cassure", []

    monkeypatch.setattr(cs, "etude", faux)
    chemin = tmp_path / "active.db"
    open_read_write(chemin).close()
    texte, _ = service._cassure(chemin, None, "")
    assert "Aucune relève" in texte
    for argument in ("", "essai"):
        texte, _ = service._cassure(
            chemin, None, argument, environ={pool.ENV_RELEVE: str(chemin)},
            ouvrir_url=lambda url: open_read_only(url))
        assert texte == "🧱 Cassure"
    assert vus == [("relève", False), ("relève", True)]

    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "cassure" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))

    async def cassure(argument):
        await asyncio.sleep(0.2)
        return "rapport", [(b"a;b\n", "cassure_cases.csv", "")]

    bot._cassure = cassure
    bot._cassure_avancement = lambda: "paire EURUSD_otc ; 3/20 paires"
    bot.premier_avancement_sec = 0.05
    bot.intervalle_avancement_sec = 0.05
    monkeypatch.setattr("maxprofit.hosting.version.commit",
                        lambda: "abc1234")

    async def deroule():
        await bot._traiter(_message("/cassure", chat=INTRUS)["message"])
        assert documents == []
        await bot._traiter(_message("/cassure")["message"])
        await asyncio.sleep(0.01)
        await bot._traiter(_message("/cassure")["message"])
        await bot._cassure_en_cours
        await bot._cassure_veille
        assert bot._en_fond == {}

    asyncio.run(deroule())
    assert documents == ["cassure_cases.csv"]
    textes = [t for _, t in client.envoyes]
    assert any(t.startswith("🧱 Cassure ou retournement au toucher d'une "
                            "zone : étude lancée (version abc1234")
               for t in textes)
    assert any(t.startswith("🧱 L'étude cassure ou retournement est déjà en "
                            "cours : paire") for t in textes)
    assert any(t.startswith("⏳ 🧱 Cassure : paire") for t in textes)
