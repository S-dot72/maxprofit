"""
`/graphiques` : le tirage (équilibré, un signal par zone, avant le 03/11),
les graphiques à l'aveugle sans aucune issue, les réponses avec les 15
minutes d'après, et la passe de bout en bout sur des bases inchangées.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from maxprofit.apprentissage.lecons import Exemple
from maxprofit.core.types import Candle
from maxprofit.live import graphiques as gr
from maxprofit.store.db import open_read_only, open_read_write
from maxprofit.store.market import MarketWriter

DEBUT = gr.FIN_TIRAGE - 4 * 86400
FIN = gr.FIN_TIRAGE + 2 * 86400
PAIRES = ("EURUSD_otc", "USDJPY_otc")


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def _exemple(paire, d, gagne, call=True, niveau=1.0):
    return Exemple(ts_sec=d, pair=paire, gagne=gagne,
                   contexte={"sens_call": float(call), "niveau": niveau})


def test_un_seul_signal_par_zone_et_rien_apres_le_3_novembre():
    t = DEBUT + 6 * 3600
    exemples = [_exemple("A", t, True), _exemple("A", t + 60, False),
                _exemple("A", t + 600, True),          # même zone (chaîne)
                _exemple("A", t + 3600, False),        # zone suivante
                _exemple("B", t + 60, True),
                _exemple("A", gr.FIN_TIRAGE + 60, True),
                Exemple(ts_sec=t + 7200, pair="A", gagne=True,
                        contexte={"sens_call": 1.0})]  # sans zone
    assert [(e.pair, e.ts_sec) for e in gr.candidats(exemples)] == [
        ("A", t), ("A", t + 3600), ("B", t + 60)]


def test_le_tirage_est_equilibre_jouable_et_reproductible():
    exemples = [_exemple("A", DEBUT + 3600 * i, i % 3 != 0)
                for i in range(200)]
    jouable = (lambda e: (e.ts_sec // 3600) % 4 != 0)
    tirage = gr.tirer(exemples, jouable, par_issue=20)
    assert sum(e.gagne for e in tirage) == 20
    assert sum(not e.gagne for e in tirage) == 20
    assert all(jouable(e) for e in tirage)
    assert tirage == gr.tirer(exemples, jouable, par_issue=20)


def _bougies(paire, debut, n, graine=1):
    alea = random.Random(graine)
    prix, sortie = 1.0, []
    for i in range(n):
        o = prix
        prix *= 1 + alea.gauss(0, 0.0004)
        sortie.append(Candle(paire, 60, debut + 60 * i, o, max(o, prix) +
                             0.0001, min(o, prix) - 0.0001, prix, 20, True))
    return sortie


def test_le_graphique_aveugle_ne_montre_rien_d_apres_l_entree():
    d = DEBUT + 10 * 3600
    bougies = _bougies("A", d - 301 * 60, 301 + gr.APRES)
    avant = [b for b in bougies if b.ts_sec < d]
    apres = [b for b in bougies if b.ts_sec >= d]
    e = _exemple("A", d, True, niveau=avant[-1].close)
    aveugle = gr.svg(1, e, avant, apres, False)
    reponse = gr.svg(1, e, avant, apres, True)
    assert "GAGNÉ" not in aveugle and "PERDU" not in aveugle
    assert "GAGNÉ" in reponse
    # Corps des bougies : contexte (300) + zoom (60), et 16 de plus deux
    # fois dans les réponses.
    corps = lambda s: s.count('fill="#1a9850"/>') + s.count('fill="#d73027"/>')
    assert corps(aveugle) == 300 + 60
    assert corps(reponse) == 300 + 60 + 2 * len(apres)
    assert "CALL (achat)" in aveugle


# --------------------------------------------------------------------------- #
# De bout en bout
# --------------------------------------------------------------------------- #

def _base(chemin: Path) -> None:
    conn = open_read_write(chemin)
    ecrivain = MarketWriter(conn)
    for k, paire in enumerate(PAIRES):
        ecrivain.upsert_candles(_bougies(paire, DEBUT, (FIN - DEBUT) // 60,
                                         graine=k + 3))
        conn.execute("INSERT INTO payouts (ts_sec, pair, payout_pct, is_open)"
                     " VALUES (?,?,?,1)", (DEBUT, paire, 92))
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def passe(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("graphiques")
    active = dossier / "active.db"
    _base(active)
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    exemples = [_exemple(p, DEBUT + 6 * 3600 + 1800 * i, i % 2 == 0,
                         call=i % 3 == 0, niveau=1.0)
                for p in PAIRES for i in range(60)]
    course = SimpleNamespace(rejeu_en_memoire=(exemples, None, 1, None))
    empreintes = [hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (active, releve)]
    a, r = open_read_only(active), open_read_only(releve)
    try:
        rapport, fichiers = gr.revue(r, a, course, respirer=lambda: None)
    finally:
        a.close()
        r.close()
    return active, releve, empreintes, course, rapport, fichiers


def test_les_bases_ne_changent_pas_et_les_fichiers(passe):
    active, releve, empreintes, _, rapport, fichiers = passe
    assert [hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (active, releve)] == empreintes
    assert "requêtes refusées : 0" in rapport
    assert [n for _, n, _ in fichiers] == [
        "graphiques_aveugles.html", "graphiques_reponses.html",
        "graphiques_bougies.csv", "graphiques_signaux.csv"]
    aveugle = next(c for c, n, _ in fichiers if n.endswith("aveugles.html"))
    assert b"GAGN" not in aveugle and b"PERDU" not in aveugle
    assert aveugle.count(b"<svg") == 80
    signaux = next(c for c, n, _ in fichiers if n == "graphiques_signaux.csv")
    lignes = signaux.decode("utf-8").splitlines()[1:]
    issues = [l.split(";")[-1] for l in lignes]
    assert issues.count("gain") == 40 and issues.count("perte") == 40
    assert "80 graphiques (40 gagnants, 40 perdants" in rapport


def test_le_service_et_la_commande(passe, tmp_path):
    from maxprofit.hosting import service
    from maxprofit.store import pool
    active, releve, empreintes, course, _, _ = passe
    texte, _ = service._graphiques(active, None, "")
    assert "Course indisponible" in texte
    texte, fichiers = service._graphiques(
        active, course, "", environ={pool.ENV_RELEVE: str(releve)},
        ouvrir_url=lambda url: open_read_only(url))
    assert "Revue des graphiques" in texte and len(fichiers) == 4

    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "graphiques" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))

    async def graphiques(argument):
        return "rapport", [(b"<html/>", "graphiques_aveugles.html", "")]

    bot._graphiques = graphiques

    async def deroule():
        await bot._traiter(_message("/graphiques", chat=INTRUS)["message"])
        assert documents == []
        await bot._traiter(_message("/graphiques")["message"])
        await bot._graphiques_en_cours

    asyncio.run(deroule())
    assert documents == ["graphiques_aveugles.html"]
