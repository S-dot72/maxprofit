"""
Six stratégies classiques sur l'OTC (recherche isolée) : le gel, les
indicateurs identiques à ceux du paquet `indicators`, chaque déclencheur,
les fenêtres continues, les épisodes, les issues aux ticks et leurs données
manquantes, la lecture du futur, l'isolation du module, une passe de bout en
bout en lecture seule, l'essai sans taux, et la commande.
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
from pathlib import Path

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core.types import Candle, Tick
from maxprofit.indicators.oscillators import rsi, stochastique_lente
from maxprofit.indicators.structure import bollinger_bandes
from maxprofit.live import classiques as cl
from maxprofit.live import engulfing as en
from maxprofit.store.db import open_read_only, open_read_write
from tests.test_engulfing import FIN_DONNEES, Espion, _base, _chemins

RACINE = Path(__file__).resolve().parents[1]
T0 = cl.DEBUT + 10 * 86400
PAIRE = "EURUSD_otc"


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def _marche(n: int, graine: int = 3, trou: int | None = None):
    """Une marche au hasard en bougies M1 (en.Bougie), avec un trou de
    10 minutes avant l'indice `trou` si demandé."""
    alea = random.Random(graine)
    prix, ts, sortie = 1.1000, T0, []
    for i in range(n):
        if trou is not None and i == trou:
            ts += 600
        o = prix
        c = round(o * math.exp(alea.gauss(0, 3e-4)), 5)
        h = round(max(o, c) * (1 + abs(alea.gauss(0, 1e-4))), 5)
        l = round(min(o, c) * (1 - abs(alea.gauss(0, 1e-4))), 5)
        sortie.append(en.Bougie(ts, o, h, l, c))
        prix, ts = c, ts + 60
    return sortie


def _candles(bougies):
    return [Candle(PAIRE, 60, b.ts_sec, b.open, b.high, b.low, b.close, 10,
                   True) for b in bougies]


def B(i: int, o: float, h: float, l: float, c: float) -> en.Bougie:
    return en.Bougie(T0 + 60 * i, o, h, l, c)


def test_le_gel_tient():
    doc = json.loads((RACINE / "docs" / "preinscription_classiques.json")
                     .read_text(encoding="utf-8"))
    assert doc == cl.PARAMETRES
    assert em.empreinte(cl.PARAMETRES) == cl.EMPREINTE
    autre = json.loads(json.dumps(cl.PARAMETRES))
    autre["execution"]["echeances_min"] = [1, 2]
    with pytest.raises(ValueError):
        em.exiger_le_gel(autre, cl.EMPREINTE)
    assert cl.FAMILLES == ("F1_rsi_sortie", "F2_bollinger_rsi",
                           "F3_stochastique_croisement", "F4_pin_bar",
                           "F5_chiffre_rond", "F6_cassure_canal")
    assert cl.ECHEANCES == (1, 2, 3, 5)
    assert cl.VARIANTES == ("brut", "prudent") and cl.H1_RECUL == 2


def test_les_indicateurs_egalent_ceux_du_paquet():
    bougies = _marche(160)
    s = cl.Serie(bougies)
    ref = _candles(bougies)
    r = cl.rsi_serie(s)
    sto = cl.stochastique_serie(s)
    for i in range(len(bougies)):
        fenetre = ref[:i + 1]
        assert r[i] == rsi(fenetre, 14), i
        attendu = stochastique_lente(fenetre, 14, 3, 3)
        assert sto[i] == (None if attendu is None
                          else (attendu.k, attendu.d)), i
        bandes = bollinger_bandes(fenetre, 20, 2.0)
        obtenu = cl.bollinger_en(s, i)
        assert obtenu == (None if bandes is None
                          else (bandes.basse, bandes.haute)), i


def test_une_fenetre_qui_traverse_un_trou_ne_donne_rien():
    bougies = _marche(120, trou=60)
    s = cl.Serie(bougies)
    r = cl.rsi_serie(s)
    sto = cl.stochastique_serie(s)
    assert all(r[i] is None for i in range(60, 74))
    assert r[74] is not None and r[59] is not None
    assert all(sto[i] is None for i in range(60, 77))
    assert cl.bollinger_en(s, 70) is None and cl.bollinger_en(s, 79)
    for famille, liste in cl.declencheurs(s).items():
        largeur = {"F1_rsi_sortie": 16, "F2_bollinger_rsi": 20,
                   "F3_stochastique_croisement": 19, "F4_pin_bar": 20,
                   "F5_chiffre_rond": 61, "F6_cassure_canal": 21}[famille]
        assert all(not (60 <= i < 60 + largeur - 1) for i, _ in liste), \
            famille


def _plat(n: int, prix: float = 1.1000, ecart: float = 0.0004):
    """n bougies consécutives qui oscillent dans un couloir étroit."""
    sortie = []
    for i in range(n):
        o = prix + (ecart if i % 2 else 0)
        c = prix + (0 if i % 2 else ecart)
        sortie.append(B(i, o, max(o, c) + 0.0001, min(o, c) - 0.0001, c))
    return sortie


def test_f1_sortie_de_zone_rsi():
    s = cl.Serie(_plat(20))
    rsi_ = [None] * 16 + [75.0, 69.0, 25.0, 31.0]
    assert cl.f1_rsi_sortie(s, rsi_) == [(17, cl.PUT), (19, cl.CALL)]
    for valeurs in ([70.0, 70.0], [30.0, 30.0], [69.0, 70.0], [31.0, 30.0]):
        assert cl.f1_rsi_sortie(s, [None] * 18 + valeurs) == [], \
            f"{valeurs} : rester sur le seuil ou y entrer n'est pas en sortir"


def test_f2_bollinger_et_rsi():
    bougies = _plat(20)
    o = bougies[-1].close
    bougies.append(B(20, o, 1.1031, o - 0.0001, 1.1030))   # au-dessus
    bougies.append(B(21, 1.1030, 1.1031, 1.0950, 1.0960))  # en dessous ?
    s = cl.Serie(bougies)
    rsi_ = [None] * 20 + [80.0, 50.0]
    assert cl.f2_bollinger_rsi(s, rsi_) == [(20, cl.PUT)]
    rsi_ = [None] * 20 + [60.0, 50.0]
    assert cl.f2_bollinger_rsi(s, rsi_) == [], "RSI pas extrême : rien"


def test_f3_croisement_stochastique_en_zone():
    s = cl.Serie(_plat(22))
    sto = [None] * 18 + [(85.0, 84.0), (83.0, 86.0), (15.0, 18.0),
                         (19.0, 17.0)]
    assert cl.f3_stochastique(s, sto) == [(19, cl.PUT), (21, cl.CALL)]
    sto = [None] * 18 + [(85.0, 79.0), (78.0, 80.0), None, None]
    assert cl.f3_stochastique(s, sto) == [], "%D sous 80 : hors zone"


def test_f4_pin_bar_a_l_extreme():
    bougies = _plat(19)
    bougies.append(B(19, 1.1000, 1.1030, 1.0999, 1.1002))   # mèche haute
    assert cl.f4_pin_bar(cl.Serie(bougies)) == [(19, cl.PUT)]
    bougies[-1] = B(19, 1.1002, 1.1003, 1.0970, 1.1000)     # mèche basse
    assert cl.f4_pin_bar(cl.Serie(bougies)) == [(19, cl.CALL)]
    bougies[-1] = B(19, 1.1000, 1.1030, 1.0999, 1.1015)     # corps trop gros
    assert cl.f4_pin_bar(cl.Serie(bougies)) == []
    bougies[5] = B(5, 1.1000, 1.1040, 1.0999, 1.1001)       # plus haut avant
    bougies[-1] = B(19, 1.1000, 1.1030, 1.0999, 1.1002)
    assert cl.f4_pin_bar(cl.Serie(bougies)) == []


def test_f5_premier_contact_d_un_chiffre_rond_rejete():
    assert cl.pas_rond(1.08) == 0.005 and cl.pas_rond(157.2) == 0.5
    assert cl.niveaux_touches(1.0849, 1.0851, 0.005) == [1.085]
    assert cl.niveaux_touches(1.085, 1.0852, 0.005) == [1.085]
    assert cl.niveaux_touches(1.0841, 1.0849, 0.005) == []
    bougies = _plat(61, prix=1.0830)          # sous 1,085, sans le toucher
    bougies.append(B(61, 1.0845, 1.0851, 1.0844, 1.0846))   # rejet par-dessous
    assert cl.f5_chiffre_rond(cl.Serie(bougies)) == [(61, cl.PUT)]
    bougies[61] = B(61, 1.0845, 1.0851, 1.0844, 1.0852)     # clôture au-delà
    assert cl.f5_chiffre_rond(cl.Serie(bougies)) == [], "cassure, pas rejet"
    bougies[61] = B(61, 1.0845, 1.0851, 1.0844, 1.0846)
    bougies[30] = B(30, 1.0846, 1.0850, 1.0845, 1.0847)     # déjà touché
    assert cl.f5_chiffre_rond(cl.Serie(bougies)) == [], "pas un premier contact"
    dessus = _plat(61, prix=1.0860)           # au-dessus de 1,085
    dessus.append(B(61, 1.0856, 1.0857, 1.0849, 1.0855))    # rejet par-dessus
    assert cl.f5_chiffre_rond(cl.Serie(dessus)) == [(61, cl.CALL)]
    dessus[61] = B(61, 1.0856, 1.0857, 1.0840, 1.0841)      # touche 1,085 et
    assert cl.f5_chiffre_rond(cl.Serie(dessus)) == []       # clôture dessous


def test_f6_cassure_du_canal():
    bougies = _plat(20)
    bougies.append(B(20, 1.1004, 1.1012, 1.1003, 1.1011))
    assert cl.f6_cassure_canal(cl.Serie(bougies)) == [(20, cl.CALL)]
    bougies[-1] = B(20, 1.1000, 1.1001, 1.0980, 1.0985)
    assert cl.f6_cassure_canal(cl.Serie(bougies)) == [(20, cl.PUT)]


def test_le_futur_ne_change_aucun_declenchement():
    bougies = _marche(400, graine=11)
    complet = cl.declencheurs(cl.Serie(bougies))
    for coupe in (150, 260, 399):
        partiel = cl.declencheurs(cl.Serie(bougies[:coupe]))
        for famille in cl.FAMILLES:
            assert partiel[famille] == [x for x in complet[famille]
                                        if x[0] < coupe], (famille, coupe)


def test_un_episode_dure_cinq_minutes():
    s = cl.Serie(_plat(30))
    bruts = [(i, cl.CALL) for i in range(0, 30)]
    gardes = cl.episodes(bruts, s)
    assert [i for i, _ in gardes] == [0, 5, 10, 15, 20, 25]


def _heures(n_heures: int, graine: int = 5, minutes_en_plus: int = 30):
    """Des heures complètes (60 bougies chacune) à partir d'une heure
    ronde, puis quelques minutes de l'heure en cours."""
    alea = random.Random(graine)
    debut = (T0 // 3600 + 1) * 3600
    prix, sortie = 1.1000, []
    for i in range(60 * n_heures + minutes_en_plus):
        o = prix
        c = round(o * math.exp(alea.gauss(0, 4e-4)), 5)
        sortie.append(en.Bougie(debut + 60 * i, o, max(o, c) + 0.00005,
                                min(o, c) - 0.00005, c))
        prix = c
    return sortie


def test_l1_la_tendance_h1_est_celle_de_zoneh1():
    from maxprofit.strategies.zone_h1 import ZoneH1
    bougies = _heures(5)
    lecons = cl.Lecons(cl.Serie(bougies), "EURUSD_otc")
    zone = ZoneH1()
    vus = set()
    for i in range(60 * 3, len(bougies)):
        attendu = zone._tendance_h1_haussiere(tuple(bougies[:i + 1]))
        obtenu = lecons.tendance(i)
        assert obtenu == (1 if attendu else -1), i
        vus.add(obtenu)
    assert vus == {1, -1}, "les deux tendances doivent apparaître"
    assert all(lecons.tendance(i) == 0 for i in range(0, 60 * 3)), \
        "moins de trois heures closes complètes : pas de tendance"
    trouee = bougies[:100] + bougies[101:]          # une minute manque
    lecons = cl.Lecons(cl.Serie(trouee), "EURUSD_otc")
    i = next(k for k, b in enumerate(trouee) if b.ts_sec >= bougies[0].ts_sec
             + 3 * 3600)
    assert lecons.tendance(i) == 0, "une heure incomplète : pas de tendance"


def test_l2_la_longue_meche_contre_le_trade():
    bougies = _plat(3)
    bougies.append(B(3, 1.1010, 1.1012, 1.0990, 1.1008))   # mèche basse 18/22
    bougies.append(B(4, 1.1000, 1.1022, 1.0998, 1.1004))   # mèche haute 18/24
    bougies.append(B(5, 1.1000, 1.1006, 1.0998, 1.1005))   # corps plein
    lecons = cl.Lecons(cl.Serie(bougies), "EURUSD_otc")
    assert lecons.meche_contre(3, cl.CALL) and not lecons.meche_contre(3, cl.PUT)
    assert lecons.meche_contre(4, cl.PUT) and not lecons.meche_contre(4, cl.CALL)
    assert not lecons.meche_contre(5, cl.CALL)
    assert not lecons.meche_contre(5, cl.PUT)


def test_l3_les_bougies_incluses_sur_cinquante():
    bougies = []
    for i in range(60):
        if i % 2:                               # incluse dans la précédente
            bougies.append(B(i, 1.1002, 1.1004, 1.1001, 1.1003))
        else:
            bougies.append(B(i, 1.1000, 1.1006, 1.0998, 1.1004))
    lecons = cl.Lecons(cl.Serie(bougies), "EURUSD_otc")
    assert lecons.incluses(48) is None, "49 bougies : fenêtre incomplète"
    assert lecons.incluses(49) == 25 and lecons.incluses(50) == 25
    assert lecons.incluses(59) == 25
    trouee = bougies[:30] + bougies[31:]
    lecons = cl.Lecons(cl.Serie(trouee), "EURUSD_otc")
    assert lecons.incluses(58) is None, "la fenêtre traverse un trou"


def test_l4_paires_usd_et_pin_bars_jamais_prudentes():
    bougies = _heures(6, graine=9)
    s = cl.Serie(bougies)
    hors_usd = cl.Lecons(s, "EURJPY_otc")
    usd = cl.Lecons(s, "EURUSD_otc")
    bruts = cl.declencheurs(s)
    tous = [x for liste in bruts.values() for x in liste]
    assert tous and not any(hors_usd.prudent(i, sens) for i, sens in tous)
    assert not any(usd.prudent(i, sens) for i, sens in bruts["F4_pin_bar"])
    for i, sens in tous:                 # les quatre leçons, toutes ensemble
        n = usd.incluses(i)
        attendu = (usd.tendance(i) == sens and not usd.meche_contre(i, sens)
                   and n is not None and n < cl.INCLUSES_MAX)
        assert usd.prudent(i, sens) == attendu, (i, sens)


def _flux_ticks(debut: int, prix, pas: int = 5, minutes: int = 12):
    """Des ticks toutes les `pas` secondes depuis `debut`, au prix
    `prix(t)`."""
    return [Tick(PAIRE, t * 1000, prix(t))
            for t in range(debut, debut + 60 * minutes, pas)]


def test_issues_aux_ticks_gain_perte_egalite():
    decision = T0 + 600
    ticks = _flux_ticks(decision - 60,
                        lambda t: 1.1000 + 0.0001 * ((t - decision) // 60))
    sig = cl.Signal(0, PAIRE, decision, cl.CALL)
    cl.issues_ticks(sig, en.Ticks(_chemins(ticks)))
    assert sig.issues == {1: 1, 2: 1, 3: 1, 5: 1}
    sig = cl.Signal(0, PAIRE, decision, cl.PUT)
    cl.issues_ticks(sig, en.Ticks(_chemins(ticks)))
    assert sig.issues == {1: -1, 2: -1, 3: -1, 5: -1}
    plat = _flux_ticks(decision - 60, lambda t: 1.1000)
    sig = cl.Signal(0, PAIRE, decision, cl.CALL)
    cl.issues_ticks(sig, en.Ticks(_chemins(plat)))
    assert sig.issues == {1: 0, 2: 0, 3: 0, 5: 0}, "égalité notée 0"


def test_entree_deux_secondes_apres_la_decision():
    decision = T0 + 600
    ticks = [Tick(PAIRE, (decision - 30) * 1000, 1.1000),
             Tick(PAIRE, (decision + 1) * 1000, 1.1010),   # connu à + 2 s
             Tick(PAIRE, (decision + 3) * 1000, 1.0990)]
    ticks += _flux_ticks(decision + 5, lambda t: 1.1005, minutes=7)
    sig = cl.Signal(0, PAIRE, decision, cl.CALL)
    cl.issues_ticks(sig, en.Ticks(_chemins(ticks)))
    assert sig.issues[1] == -1, "entrée à 1,1010, sortie à 1,1005"


def test_donnees_manquantes_et_flux_gele():
    decision = T0 + 600
    ticks = _flux_ticks(decision - 60, lambda t: 1.1000, minutes=8)
    sans = {decision + 180}                     # la quatrième minute manque
    sig = cl.Signal(0, PAIRE, decision, cl.CALL)
    cl.issues_ticks(sig, en.Ticks(_chemins(ticks, sans)))
    assert sig.statuts[1] == cl.ISSUE and sig.statuts[2] == cl.ISSUE
    assert sig.statuts[3] == cl.MANQUE and sig.statuts[5] == cl.MANQUE
    vieux = [Tick(PAIRE, (decision - 30) * 1000, 1.1000)]
    sig = cl.Signal(0, PAIRE, decision, cl.CALL)
    cl.issues_ticks(sig, en.Ticks(_chemins(vieux)))
    assert set(sig.statuts.values()) == {cl.MANQUE_ENTREE}
    # Des ticks toutes les 5 s jusqu'à + 115 s, puis un seul par minute, à
    # + 30 s : à + 3 min 2 s, le dernier tick connu a 32 s.
    gel = _flux_ticks(decision - 60, lambda t: 1.1000, minutes=3)
    gel += [Tick(PAIRE, (decision + 60 * m + 30) * 1000, 1.1)
            for m in range(2, 8)]
    sig = cl.Signal(0, PAIRE, decision, cl.CALL)
    cl.issues_ticks(sig, en.Ticks(_chemins(gel)))
    assert [sig.statuts[t] for t in cl.ECHEANCES] == [
        cl.ISSUE, cl.ISSUE, cl.GELE, cl.GELE]


def test_le_module_est_isole():
    source = (RACINE / "maxprofit" / "live" / "classiques.py").read_text(
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
                     "maxprofit.hosting"):
        assert not any(m == interdit or m.startswith(interdit + ".")
                       for m in importes), interdit
    for mot in ("INSERT", "UPDATE ", "DELETE", "upsert", "insert_"):
        assert mot not in source, mot


@pytest.fixture(scope="module")
def passe(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("classiques")
    active = dossier / "active.db"
    _base(active)
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    empreintes = [hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (active, releve)]
    a, r = open_read_only(active), open_read_only(releve)
    espion = Espion(r)
    try:
        rapport, fichiers = cl.etude(espion, a, maintenant=FIN_DONNEES + 3600,
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
    assert min(espion.bornes) >= cl.DEBUT - 86400
    assert [n for _, n, _ in fichiers] == [
        "classiques_cases.csv", "classiques_details.csv",
        "classiques_statuts.csv", "classiques_donnees.csv",
        "classiques_signaux.csv.gz"]


def test_passe_complete_cases_signaux_et_statuts(passe):
    *_, rapport, fichiers = passe
    cases = _lignes(fichiers, "classiques_cases.csv")
    assert len(cases) == 6 * 2 * 4 * 2
    assert {c["variante"] for c in cases} == {"brut", "prudent"}
    assert all(int(c["n"]) == 0 for c in cases
               if c["famille"] == "F4_pin_bar" and c["variante"] == "prudent")
    assert {c["sens_choisi"] for c in cases} <= {"conventionnel", "inverse"}
    for c in cases:
        n = int(c["n"])
        if n:
            g, l = int(c["gains_conv"]), int(c["pertes_conv"])
            attendu = (g if c["sens_choisi"] == "conventionnel" else l) / n
            assert abs(float(c["taux"]) - attendu) < 1e-6
    signaux = _lignes(fichiers, "classiques_signaux.csv.gz")
    assert signaux and {s["direction"] for s in signaux} == {"CALL", "PUT"}
    assert {s["famille"] for s in signaux} <= set(cl.FAMILLES)
    assert all(s["periode"] in ("exploration", "validation") for s in signaux)
    statuts = _lignes(fichiers, "classiques_statuts.csv")
    assert any(s["source"] == "déclenchements" for s in statuts)
    donnees = _lignes(fichiers, "classiques_donnees.csv")
    assert {d["paire"] for d in donnees} == {"EURUSD_otc", "GBPUSD_otc"}
    assert max(d["jour"] for d in donnees) < "2026-11-05"
    for famille in cl.FAMILLES:
        assert cl.LIBELLES[famille] in rapport
    assert "passeraient l'exploration par hasard" in rapport
    assert all(set(s["variantes"].split(",")) <= {"brut", "prudent"}
               and s["variantes"] for s in signaux)
    assert any("prudent" in s["variantes"] for s in signaux)
    par_variante = {}
    for s in signaux:
        for v in s["variantes"].split(","):
            par_variante.setdefault((s["famille"], v, s["paire"]), []).append(
                int(s["decision_ts"]))
    for decisions in par_variante.values():
        assert all(b - a >= cl.EPISODE_SEC for a, b in zip(decisions,
                                                           decisions[1:]))


def test_l_essai_ne_montre_aucun_taux(passe):
    active, releve, empreintes, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    espion = Espion(r)
    try:
        texte, fichiers = cl.etude(espion, a, maintenant=FIN_DONNEES + 3600,
                                   respirer=lambda: None, essai=True,
                                   version="abc1234")
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "%" not in texte
    assert texte.startswith("🧪 <b>Essai technique /classiques</b>")
    assert "Statistiques, rapport et fichiers : passés" in texte
    assert "Version déployée : abc1234" in texte
    assert max(espion.bornes) <= FIN_DONNEES
    assert cl.avancement() is None


def test_avant_la_fin_de_l_exploration_rien_a_valider(passe):
    active, releve, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    try:
        texte, fichiers = cl.etude(r, a, maintenant=cl.FIN_EXPLORATION - 60,
                                   respirer=lambda: None)
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "rien à valider" in texte


def test_les_cases_attendues_au_hasard():
    lignes = [{"periode": "exploration", "n": 1000},
              {"periode": "exploration", "n": 100},
              {"periode": "validation", "n": 1000}]
    z = (cl.POINT_MORT - 0.5) * 2 * math.sqrt(1000)
    attendu = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))
    assert abs(cl.attendues_au_hasard(lignes) - attendu) < 1e-12


def test_le_service_et_la_commande(monkeypatch, tmp_path):
    from maxprofit.hosting import service
    from maxprofit.store import pool
    vus = []

    def faux(lue, active, course=None, *, nom_lecture, essai=False,
             version=None):
        vus.append((nom_lecture, essai))
        return "🧪 Classiques", []

    monkeypatch.setattr(cl, "etude", faux)
    chemin = tmp_path / "active.db"
    open_read_write(chemin).close()
    texte, _ = service._classiques(chemin, None, "")
    assert "Aucune relève" in texte
    for argument in ("", "essai"):
        texte, _ = service._classiques(
            chemin, None, argument, environ={pool.ENV_RELEVE: str(chemin)},
            ouvrir_url=lambda url: open_read_only(url))
        assert texte == "🧪 Classiques"
    assert vus == [("relève", False), ("relève", True)]

    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "classiques" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))

    async def classiques(argument):
        await asyncio.sleep(0.2)
        return "rapport", [(b"a;b\n", "classiques_cases.csv", "")]

    bot._classiques = classiques
    bot._classiques_avancement = lambda: "paire EURUSD_otc ; 3/20 paires"
    bot.premier_avancement_sec = 0.05
    bot.intervalle_avancement_sec = 0.05
    monkeypatch.setattr("maxprofit.hosting.version.commit",
                        lambda: "abc1234")

    async def deroule():
        await bot._traiter(_message("/classiques", chat=INTRUS)["message"])
        assert documents == []
        await bot._traiter(_message("/classiques")["message"])
        await asyncio.sleep(0.01)
        await bot._traiter(_message("/classiques")["message"])
        await bot._classiques_en_cours
        await bot._classiques_veille
        assert bot._en_fond == {}

    asyncio.run(deroule())
    assert documents == ["classiques_cases.csv"]
    textes = [t for _, t in client.envoyes]
    assert any(t.startswith("🧪 Six stratégies classiques lancées sur l'OTC "
                            "(version abc1234") for t in textes)
    assert any(t.startswith("🧪 L'étude des stratégies classiques est déjà "
                            "en cours : paire") for t in textes)
    assert any(t.startswith("⏳ 🧪 Stratégies classiques : paire")
               for t in textes)


def test_l_avancement_suit_la_passe(passe):
    assert cl.avancement() is None
    import time as horloge
    maintenant = horloge.monotonic()
    cl._ETAT.update(debut=maintenant - 600, etape="paire EURUSD_otc",
                    faites=5, total=20, signaux=900,
                    debut_paires=maintenant - 300, rss_max=250.0)
    try:
        point = cl.avancement()
        assert "5/20 paires" in point and "900 signaux" in point
        assert "fin des paires dans ~15 min" in point
        assert "pic de l'étude 250 Mo" in point
    finally:
        cl._ETAT.clear()
