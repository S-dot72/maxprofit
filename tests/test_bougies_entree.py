"""
`/bougies` : la tendance M1 (structure des sommets et creux, définition de
l'utilisateur du 03/11), les figures et le rapport de force des 50 bougies
avant l'entrée, le criblage exploration → validation, et la passe de bout
en bout sur deux bases SQLite (relève et active) inchangées.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.apprentissage.lecons import Exemple
from maxprofit.core.types import Candle
from maxprofit.live import bougies_entree as be
from maxprofit.store.db import open_read_only, open_read_write
from maxprofit.store.market import MarketWriter


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def _bougies(prix, debut=1_790_000_040 // 60 * 60, paire="EURUSD_otc",
             meche=0.00002):
    """Une bougie par minute : ouverture = clôture précédente."""
    sortie, precedent = [], prix[0]
    for i, p in enumerate(prix):
        o, c = precedent, p
        sortie.append(Candle(paire, 60, debut + 60 * i, o, max(o, c) + meche,
                             min(o, c) - meche, c, 20, True))
        precedent = p
    return sortie


def _vagues(bas, hauts, jambe=20):
    """Un chemin de prix qui alterne creux et sommets donnés."""
    points = []
    for b, h in zip(bas, hauts):
        points += [b, h]
    prix = []
    for a, z in zip(points, points[1:]):
        prix += [a + (z - a) * t / jambe for t in range(jambe)]
    return prix + [points[-1]]


def _tendance(bas, hauts):
    bougies = _bougies(_vagues(bas, hauts))
    return be.tendance_structure(bougies[-be.FENETRE_TENDANCE:])[0]


# --------------------------------------------------------------------------- #
# La tendance
# --------------------------------------------------------------------------- #

def test_la_tendance_par_la_structure():
    n = 8
    montants = _tendance([1.000 + 0.002 * i for i in range(n)],
                         [1.006 + 0.002 * i for i in range(n)])
    descendants = _tendance([1.020 - 0.002 * i for i in range(n)],
                            [1.026 - 0.002 * i for i in range(n)])
    # Sommets montants mais creux descendants : ni l'un ni l'autre.
    elargie = _tendance([1.010 - 0.001 * i for i in range(n)],
                        [1.016 + 0.001 * i for i in range(n)])
    plat = be.tendance_structure(_bougies([1.0] * 300))[0]
    assert (montants, descendants, elargie, plat) == (
        be.HAUSSIERE, be.BAISSIERE, be.RANGE, be.INDETERMINEE)


def test_l_orientation_par_rapport_au_trade():
    assert be.orientation(be.HAUSSIERE, True) == be.AVEC
    assert be.orientation(be.HAUSSIERE, False) == be.CONTRE
    assert be.orientation(be.BAISSIERE, False) == be.AVEC
    assert be.orientation(be.RANGE, True) == be.RANGE


def test_la_tendance_ema_est_une_sensibilite():
    montee = _bougies([1.0 + 0.0001 * i for i in range(300)])
    assert be.tendance_ema(montee) == be.HAUSSIERE
    assert be.tendance_ema(_bougies([1.0] * 300)) == be.RANGE


# --------------------------------------------------------------------------- #
# Les figures et les mesures
# --------------------------------------------------------------------------- #

def _b(o, h, l, c, ts=1_790_000_040):
    return Candle("X", 60, ts, o, h, l, c, 10, True)


def test_les_figures_sont_orientees_dans_le_sens_du_trade():
    marteau = _b(1.0010, 1.0012, 1.0000, 1.0012)
    assert "marteau_fav" in be.figures(None, marteau, True)
    assert "marteau_adv" in be.figures(None, marteau, False)
    assert "rejet_fav" in be.figures(None, marteau, True)
    rouge = _b(1.0010, 1.0011, 1.0004, 1.0005)
    englobante = _b(1.0003, 1.0013, 1.0002, 1.0012)
    assert "englobante_fav" in be.figures(rouge, englobante, True)
    assert "englobante_adv" in be.figures(rouge, englobante, False)
    assert "doji" in be.figures(None, _b(1.0, 1.001, 0.999, 1.0), True)
    assert "marubozu_fav" in be.figures(None, _b(1.0, 1.001, 1.0, 1.001),
                                        True)
    assert "incluse" in be.figures(englobante, _b(1.0005, 1.0010, 1.0004,
                                                  1.0008), True)
    assert be.figures(None, _b(1.0, 1.0, 1.0, 1.0), True) == {"doji"}


def test_les_mesures_du_rapport_de_force():
    verts = _bougies([1.0 + 0.0001 * i for i in range(60)], meche=0.000005)
    call = be.mesures(verts, 59, True)
    put = be.mesures(verts, 59, False)
    assert call["part_fav_50"] == 1.0 and put["part_fav_50"] == 0.0
    assert call["pression_corps_10"] > 0 > put["pression_corps_10"]
    assert call["serie"] == 50 and put["serie"] == -50, "bornée à la fenêtre"
    assert call["marubozu_fav_5"] == 5 and put["marubozu_adv_5"] == 5
    assert set(call) == set(be.noms_des_variables())
    # Une fenêtre trouée ne se mesure pas.
    trouee = verts[:30] + verts[31:]
    assert be.mesures(trouee, 58, True) is None
    assert be.mesures(verts, 20, True) is None


# --------------------------------------------------------------------------- #
# Les comparaisons
# --------------------------------------------------------------------------- #

def test_le_groupe_compare_est_fixe_sur_l_exploration():
    regle = be.regle_de_groupe([float(i) for i in range(90)])
    assert regle["forme"] == "tercile haut / tercile bas"
    assert be.appliquer(regle, 80.0) == 1 and be.appliquer(regle, 5.0) == 0
    assert be.appliquer(regle, 45.0) is None
    rare = be.regle_de_groupe([0.0] * 80 + [1.0] * 10)
    assert rare["forme"] == "> 0 / ≤ 0"
    assert be.appliquer(rare, 2.0) == 1 and be.appliquer(rare, 0.0) == 0


def _lignes(n, effet, graine, paires=10, jours=7, debut=0):
    alea = random.Random(graine)
    sortie = []
    for i in range(n):
        x = alea.random()
        gagne = alea.random() < 0.5 + (effet if x > 0.5 else -effet)
        sortie.append({"paire": f"P{i % paires}",
                       "decision": debut + (i % jours) * 86400 + i,
                       "issue": em.GAIN if gagne else em.PERTE,
                       "signal": x, "bruit": alea.random()})
    return sortie


def test_le_criblage_trouve_un_vrai_effet_et_pas_le_bruit():
    explo = _lignes(1500, 0.12, 1)
    valid = _lignes(1500, 0.12, 2, debut=10 * 86400)
    tests = {t["variable"]: t for t in be.criblage(
        explo, valid, ["signal", "bruit"], lambda: None)}
    assert tests["signal"]["sens"] == 1
    assert tests["signal"]["verdict"] == "découverte (BH 10 %)"
    assert tests["signal"]["p"] < 0.001
    assert not tests["bruit"]["verdict"].startswith("découverte")


def test_l_ecart_et_son_intervalle():
    lignes = _lignes(2000, 0.10, 3)
    r = be.ecart(lignes, lambda x: int(x["signal"] > 0.5))
    assert r["n1"] + r["n0"] == 2000
    assert r["bas"] < r["ecart"] < r["haut"]
    assert r["ecart"] > 0.1 and r["p"] < 0.001
    inverse = be.ecart(lignes, lambda x: int(x["signal"] > 0.5), sens=-1)
    assert inverse["ecart"] == pytest.approx(-r["ecart"])


# --------------------------------------------------------------------------- #
# De bout en bout
# --------------------------------------------------------------------------- #

DEBUT = em.FIN_EXPLORATION - 2 * 86400            # 19/10
FIN = em.FIN_VALIDATION + 86400                    # 29/10
PAIRES = ("EURUSD_otc", "USDJPY_otc")


def _base(chemin: Path):
    conn = open_read_write(chemin)
    ecrivain = MarketWriter(conn)
    alea = random.Random(11)
    for paire in PAIRES:
        prix, bougies = 1.0, []
        for ts in range(DEBUT, FIN, 60):
            o = prix
            prix *= 1 + alea.gauss(0, 0.0004)
            bougies.append(Candle(paire, 60, ts, o, max(o, prix) * 1.0001,
                                  min(o, prix) * 0.9999, prix, 20, True))
        ecrivain.upsert_candles(bougies)
        conn.execute("INSERT INTO payouts (ts_sec, pair, payout_pct, is_open)"
                     " VALUES (?,?,?,1)", (DEBUT, paire, 90))
    conn.commit()
    conn.close()


def _exemples(chemin: Path) -> list[Exemple]:
    """Un signal toutes les 47 minutes par paire, confirmé (k = 1) sauf un
    sur cinq ; plus un signal après le 28/10, qui ne doit pas être lu."""
    from maxprofit.store.market import MarketReader
    conn = open_read_only(chemin)
    lecteur = MarketReader(conn)
    sortie = []
    for paire in PAIRES:
        closes = {c.ts_sec: c.close
                  for c in lecteur.candles(paire, 60, DEBUT, FIN)}
        for n, d in enumerate(range(DEBUT + 6 * 3600, em.FIN_VALIDATION,
                                    47 * 60)):
            call = n % 2 == 0
            entree = d                                 # début de la bougie k = 1
            sortie_prix = closes.get(entree + 900)
            gagne = (sortie_prix > closes[entree]) == call
            confirme = n % 5 != 0
            ctx = {"sens_call": float(call),
                   "attente_confirmation": 1.0 if confirme else 0.0}
            if confirme:
                ctx["gagne_confirme"] = float(gagne)
            sortie.append(Exemple(ts_sec=d, pair=paire, contexte=ctx,
                                  gagne=gagne))
        sortie.append(Exemple(ts_sec=em.FIN_VALIDATION + 3600, pair=paire,
                              contexte={"sens_call": 1.0,
                                        "attente_confirmation": 1.0},
                              gagne=True))
    conn.close()
    return sortie


@pytest.fixture(scope="module")
def passe(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("bougies")
    active = dossier / "active.db"
    _base(active)
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    course = SimpleNamespace(rejeu_en_memoire=(_exemples(active), None, 1,
                                               None),
                             confirmation_m1="suivante")
    empreintes = [hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (active, releve)]
    a, r = open_read_only(active), open_read_only(releve)
    try:
        rapport, fichiers = be.etude(r, a, course, respirer=lambda: None)
    finally:
        a.close()
        r.close()
    return active, releve, empreintes, course, rapport, fichiers


def _csv(fichiers, nom):
    contenu = next(c for c, n, _ in fichiers if n == nom)
    lignes = contenu.decode("utf-8").splitlines()
    entete = lignes[0].split(";")
    return [dict(zip(entete, l.split(";"))) for l in lignes[1:]]


def test_les_bases_ne_changent_pas(passe):
    active, releve, empreintes, _, rapport, fichiers = passe
    assert [hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (active, releve)] == empreintes
    assert "requêtes refusées : 0" in rapport
    assert "Rien n'a été écrit en base." in rapport
    assert "0 divergence(s)" in rapport
    assert [n for _, n, _ in fichiers] == [
        "bougies_signaux.csv", "bougies_tests.csv", "bougies_tendance.csv"]


def test_la_population_et_ses_exclusions(passe):
    *_, rapport, fichiers = passe
    signaux = _csv(fichiers, "bougies_signaux.csv")
    assert all(int(s["decision"]) < em.FIN_VALIDATION for s in signaux)
    non_confirmes = [s for s in signaux if s["exclusion"] ==
                     "non confirmé en M1 au mode actuel"]
    par_paire = len(range(DEBUT + 6 * 3600, em.FIN_VALIDATION, 47 * 60))
    assert len(signaux) == len(PAIRES) * par_paire
    assert len(non_confirmes) == len(PAIRES) * len(range(0, par_paire, 5))
    retenus = [s for s in signaux if not s["exclusion"]]
    assert retenus and {s["periode"] for s in retenus} == {
        em.EXPLORATION, em.VALIDATION}
    assert {s["orientation"] for s in retenus} <= set(be.ORIENTATIONS)
    assert all(s["issue"] in (em.GAIN, em.PERTE) for s in retenus)
    assert all(s["jouable"] == "True" for s in retenus)
    # L'issue recalculée est celle du rejeu.
    assert all(s["issue"] == s["issue_rejeu"] for s in retenus)
    n = len([s for s in retenus if s["issue_rejeu"]])
    assert f"{n}/{n} identiques" in rapport
    assert "e_pression_corps_10" in signaux[0] and "t_serie" in signaux[0]


def test_le_rapport_et_les_tests(passe):
    *_, rapport, fichiers = passe
    assert "<b>3. P1, fixée à l'avance : avec la tendance &gt; contre</b>" \
        in rapport
    assert "<b>4. Le reste, choisi sur l'exploration" in rapport
    tests = _csv(fichiers, "bougies_tests.csv")
    p1 = [t for t in tests if t["variable"] == "P1_orientation"]
    assert {t["nature"] for t in p1} == {
        "principale (exploration)", "principale (validation)",
        "principale (ensemble)", "principale (ensemble_ema)",
        "principale (jouables)"}
    exploratoires = [t for t in tests if t["nature"] == "exploratoire"]
    assert len(exploratoires) == 2 * len(be.noms_des_variables()) + 1
    tendance = _csv(fichiers, "bougies_tendance.csv")
    assert {t["definition"] for t in tendance} == {"structure", "EMA 20/50"}


def test_sans_rejeu_et_sans_course(tmp_path):
    texte, fichiers = be.etude(None, None, SimpleNamespace(
        rejeu_en_memoire=None))
    assert texte == be.ATTENDRE_LE_REJEU and fichiers == []
    from maxprofit.hosting.service import _bougies
    texte, _ = _bougies(tmp_path / "x.db", None, "")
    assert "Course indisponible" in texte


def test_le_service_de_bout_en_bout_sur_la_releve(passe):
    from maxprofit.hosting import service
    from maxprofit.store import pool
    active, releve, empreintes, course, _, _ = passe
    texte, fichiers = service._bougies(
        active, course, "", environ={pool.ENV_RELEVE: str(releve)},
        ouvrir_url=lambda url: open_read_only(url))
    assert "Les bougies avant l'entrée" in texte and len(fichiers) == 3
    assert [hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (active, releve)] == empreintes


def test_la_commande_telegram_tourne_en_tache_de_fond(tmp_path):
    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "bougies" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))
    appels = []

    async def bougies(argument):
        appels.append(argument)
        return "rapport", [(b"a;b\n", "bougies_signaux.csv", "")]

    bot._bougies = bougies

    async def deroule():
        await bot._traiter(_message("/bougies", chat=INTRUS)["message"])
        assert appels == []
        await bot._traiter(_message("/bougies active")["message"])
        assert "lancée" in client.envoyes[-1][1]
        await bot._bougies_en_cours

    asyncio.run(deroule())
    assert appels == ["active"]
    assert client.envoyes[-1] == (CHAT, "rapport")
    assert documents == ["bougies_signaux.csv"]


def test_une_copie_divergente_est_relue_sur_l_active(passe, tmp_path):
    active, releve, _, course, _, fichiers = passe
    abimee = tmp_path / "releve_abimee.db"
    shutil.copy(releve, abimee)
    conn = open_read_write(abimee)
    jour = em.FIN_EXPLORATION + 86400
    conn.execute("UPDATE candles SET close = close * 1.01 WHERE pair = ? AND "
                 "ts_sec >= ? AND ts_sec < ?", ("EURUSD_otc", jour,
                                                jour + 3600))
    conn.commit()
    conn.close()
    a, r = open_read_only(active), open_read_only(abimee)
    try:
        rapport, autres = be.etude(r, a, course, respirer=lambda: None)
    finally:
        a.close()
        r.close()
    assert "1 divergence(s), lues sur l'active (1 paires-jours" in rapport
    assert _csv(autres, "bougies_signaux.csv") == _csv(
        fichiers, "bougies_signaux.csv")


def test_en_mode_0_la_touche_n_est_pas_testee_deux_fois(passe):
    active, releve, _, course, _, _ = passe
    mode_0 = SimpleNamespace(rejeu_en_memoire=course.rejeu_en_memoire,
                             confirmation_m1="0")
    a, r = open_read_only(active), open_read_only(releve)
    try:
        rapport, fichiers = be.etude(r, a, mode_0, respirer=lambda: None)
    finally:
        a.close()
        r.close()
    tests = [t for t in _csv(fichiers, "bougies_tests.csv")
             if t["nature"] == "exploratoire"]
    assert len(tests) == len(be.noms_des_variables()) + 1
    assert not any(t["variable"].startswith("t_") for t in tests)
    assert "la touche étant l'entrée en mode 0" in rapport
    assert "Issue : entrée immédiate" in rapport
