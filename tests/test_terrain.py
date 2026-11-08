"""
`/terrain` : le gel, les séries (contiguïté, sauts aberrants), les couples
et les triangles, et une passe de bout en bout sur une base synthétique où
deux failles sont plantées (une mémoire à court terme, un triangle qui
revient) et doivent être trouvées, sans jamais lire après le 10/11.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import shutil
from pathlib import Path

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core.payout import FLUX_POUR_LE_PLAFOND
from maxprofit.core.types import Candle
from maxprofit.live import terrain as tr
from maxprofit.store.db import open_read_only, open_read_write
from maxprofit.store.market import MarketWriter

RACINE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def test_le_gel_tient():
    doc = json.loads((RACINE / "docs" / "preinscription_terrain.json")
                     .read_text(encoding="utf-8"))
    assert doc == tr.PARAMETRES
    assert em.empreinte(tr.PARAMETRES) == tr.EMPREINTE
    em.exiger_le_gel(tr.PARAMETRES, tr.EMPREINTE)
    autre = json.loads(json.dumps(tr.PARAMETRES))
    autre["point_mort"] = 0.5
    with pytest.raises(ValueError):
        em.exiger_le_gel(autre, tr.EMPREINTE)
    assert tr.FLUX_MIN == FLUX_POUR_LE_PLAFOND
    assert tr.POINT_MORT == pytest.approx(1 / 1.92, abs=1e-9)
    assert tr.HORIZONS == (1, 5, 15)
    assert tr.FIN_EXPLORATION == tr.DEBUT + 28 * 86400
    assert tr.FIN_VALIDATION == tr.FIN_EXPLORATION + 7 * 86400


def test_la_validation_s_arrete_au_9_novembre():
    assert tr.fin_de_validation(tr.FIN_VALIDATION + 5 * 86400) \
        == tr.FIN_VALIDATION
    assert tr.fin_de_validation(tr.FIN_EXPLORATION + 2 * 86400 + 3600) \
        == tr.FIN_EXPLORATION + 2 * 86400


def test_contiguite_et_sauts_aberrants():
    s = tr.Serie("EURUSD_otc", tr.DEBUT, 10)
    for i, x in enumerate([1.0, 1.001, 1.002, math.nan, 1.003, 1.003,
                           1.02, 1.021, 1.02, 1.019]):
        s.close[i] = x
    s.finir()
    assert s.etat(0, 2) == 0
    assert s.etat(2, 4) == tr.ABSENT                 # la bougie 3 manque
    assert s.etat(5, 7) == tr.SAUTE                  # +1,4 % de 5 à 6
    assert s.etat(6, 9) == 0                         # le saut est avant 6
    assert s.etat(8, 12) == tr.ABSENT                # hors bornes
    assert list(s.signes_futur[1][:3]) == [1, 1, tr.ABSENT]
    assert s.signes_futur[1][4] == 0                 # égalité
    assert s.signes_futur[1][5] == tr.SAUTE
    assert s.signes_passe[1][0] == tr.ABSENT
    assert s.signes_passe[1][9] == -1


def test_couples_et_triangles():
    assert tr.orientation_meneur("EURUSD_otc", "GBPUSD_otc") == 1
    assert tr.orientation_meneur("EURUSD_otc", "USDJPY_otc") == -1
    assert tr.orientation_meneur("USDJPY_otc", "USDCAD_otc") == 1
    assert tr.orientation_meneur("EURUSD_otc", "EURUSD_otc") == 0
    assert tr.orientation_meneur("EURUSD_otc", "GBPJPY_otc") == 0
    assert tr.orientation_meneur("EURUSD_otc", "BTCUSDT_otc") == 0
    trios = tr.triangles(["EURUSD_otc", "USDJPY_otc", "EURJPY_otc",
                          "GBPUSD_otc"])
    assert trios == [(("EUR", "JPY", "USD"),
                      ["EURJPY_otc", "USDJPY_otc", "EURUSD_otc"])]


# --------------------------------------------------------------------------- #
# De bout en bout
# --------------------------------------------------------------------------- #

APRES = 2 * 86400          # des bougies après le 10/11, qui ne doivent pas être lues


def _series(minutes: int):
    """EURUSD a de la mémoire (rendements AR(1) positifs), USDJPY est une
    marche au hasard, EURJPY = EURUSD × USDJPY × un écart qui revient."""
    alea = random.Random(11)
    eu, uj, ecart, r = [1.10], [150.0], 0.0, 0.0
    ej = [eu[0] * uj[0]]
    for _ in range(minutes - 1):
        r = 0.6 * r + alea.gauss(0, 1e-4)
        eu.append(eu[-1] * math.exp(r))
        uj.append(uj[-1] * math.exp(alea.gauss(0, 1e-4)))
        ecart = 0.3 * ecart + alea.gauss(0, 4e-4)
        ej.append(eu[-1] * uj[-1] * math.exp(ecart))
    return {"EURUSD_otc": eu, "USDJPY_otc": uj, "EURJPY_otc": ej}


def _base(chemin: Path) -> None:
    minutes = (tr.FIN_VALIDATION + APRES - tr.DEBUT) // 60
    conn = open_read_write(chemin)
    ecrivain = MarketWriter(conn)
    for paire, prix in _series(minutes).items():
        bougies, prec = [], prix[0]
        for i, x in enumerate(prix):
            ts = tr.DEBUT + 60 * i
            if ts >= tr.FIN_VALIDATION:
                x = prec * 1.5               # un saut énorme, à ne pas voir
            bougies.append(Candle(paire, 60, ts, prec, max(prec, x),
                                  min(prec, x), x, 20, True))
            prec = x
        ecrivain.upsert_candles(bougies)
        conn.execute("INSERT INTO payouts (ts_sec, pair, payout_pct, is_open)"
                     " VALUES (?,?,?,1)", (tr.DEBUT - 60, paire, 92))
    conn.commit()
    conn.close()


class Espion:
    """Note les bornes de temps de chaque lecture de bougies."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.bornes: list[int] = []

    def execute(self, sql, *args):
        if "FROM candles" in sql and args:
            self.bornes += [int(v) for v in args[0] if isinstance(v, int)]
        return self.conn.execute(sql, *args)


@pytest.fixture(scope="module")
def passe(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("terrain")
    active = dossier / "active.db"
    _base(active)
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    empreintes = [hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (active, releve)]
    a, r = open_read_only(active), open_read_only(releve)
    espion = Espion(r)
    try:
        rapport, fichiers = tr.terrain(
            espion, a, maintenant=tr.FIN_VALIDATION + 3600,
            respirer=lambda: None)
    finally:
        a.close()
        r.close()
    return active, releve, empreintes, espion, rapport, fichiers


def _cases(fichiers):
    contenu = next(c for c, n, _ in fichiers if n == "terrain_cases.csv")
    lignes = contenu.decode("utf-8").splitlines()
    tete = lignes[0].split(";")
    return [dict(zip(tete, l.split(";"))) for l in lignes[1:]]


def test_les_bases_ne_changent_pas_et_rien_apres_le_10(passe):
    active, releve, empreintes, espion, rapport, fichiers = passe
    assert [hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (active, releve)] == empreintes
    assert "requêtes refusées : 0" in rapport
    assert "Gel vérifié" in rapport
    assert [n for _, n, _ in fichiers] == ["terrain_cases.csv",
                                          "terrain_paires.csv"]
    assert espion.bornes and max(espion.bornes) <= tr.FIN_VALIDATION
    # Le saut de +50 % posé après le 10/11 n'a jamais été vu.
    assert "aberrant (> 1 % en une minute) : 0." in rapport


def test_les_failles_plantees_sont_trouvees(passe):
    *_, rapport, fichiers = passe
    cases = {(c["famille"], c["portee"], c["k"], c["horizon"]): c
             for c in _cases(fichiers)}
    memoire = cases[("F1", "EURUSD_otc", "1", "1")]
    assert memoire["sens"] == "suite"
    assert memoire["verdict"] == "découverte (BH 10 %)"
    parite = cases[("F4", "EUR-JPY-USD:EURJPY_otc", "0", "1")]
    assert parite["sens"] == "retour à la parité"
    assert parite["verdict"] == "découverte (BH 10 %)"
    # USDJPY est une marche au hasard : sa suite à 1 min ne passe pas.
    hasard = cases[("F1", "USDJPY_otc", "1", "1")]
    assert hasard["verdict"] != "découverte (BH 10 %)"
    assert "Pistes" in rapport and "Découvertes (BH 10 %)" in rapport
    paires = next(c for c, n, _ in fichiers if n == "terrain_paires.csv")
    lignes = {l.split(";")[0]: l.split(";")
              for l in paires.decode("utf-8").splitlines()[1:]}
    assert float(lignes["EURUSD_otc"][5]) > 0.4      # ac1 ≈ 0,6
    assert abs(float(lignes["USDJPY_otc"][5])) < 0.05


def test_avant_le_10_novembre_il_faut_le_demander(passe):
    active, releve, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    try:
        texte, fichiers = tr.terrain(r, a, maintenant=tr.FIN_VALIDATION
                                     - 86400, respirer=lambda: None)
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "/terrain partielle" in texte


def test_le_service_et_la_commande(monkeypatch, tmp_path):
    from maxprofit.hosting import service
    from maxprofit.store import pool
    vus = []

    def faux(lue, active, course=None, *, nom_lecture, partielle):
        vus.append((nom_lecture, partielle))
        return "🧭 Terrain", []

    monkeypatch.setattr(tr, "terrain", faux)
    chemin = tmp_path / "active.db"
    open_read_write(chemin).close()
    texte, _ = service._terrain(chemin, None, "")
    assert "Aucune relève" in texte
    texte, _ = service._terrain(
        chemin, None, "partielle", environ={pool.ENV_RELEVE: str(chemin)},
        ouvrir_url=lambda url: open_read_only(url))
    assert texte == "🧭 Terrain" and vus == [("relève", True)]

    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "terrain" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))

    async def terrain(argument):
        return "rapport", [(b"a;b\n", "terrain_cases.csv", "")]

    bot._terrain = terrain

    async def deroule():
        await bot._traiter(_message("/terrain", chat=INTRUS)["message"])
        assert documents == []
        await bot._traiter(_message("/terrain")["message"])
        await bot._terrain_en_cours

    asyncio.run(deroule())
    assert documents == ["terrain_cases.csv"]
