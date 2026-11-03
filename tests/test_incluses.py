"""
`/incluses` : l'épreuve gelée de la piste H_IB (bougies incluses).
- les paramètres du code sont ceux du document gelé, à l'empreinte près ;
- aucun résultat de la confirmation n'est montré avant le premier regard,
  ni dans le rapport ni dans les fichiers ;
- un regard porte sur les décisions jusqu'au N-ième signal favorable et
  jouable, et s'arrête au premier verdict ;
- la passe de bout en bout laisse les bases inchangées.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core.types import Candle, Direction
from maxprofit.live import incluses as ib
from maxprofit.store.db import open_read_only, open_read_write
from maxprofit.store.market import MarketWriter

RACINE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def test_les_parametres_sont_ceux_du_gel():
    doc = json.loads((RACINE / "docs/preinscription_incluses.json")
                     .read_text(encoding="utf-8"))
    assert ib.PARAMETRES == doc
    assert em.empreinte(doc) == ib.EMPREINTE
    assert ib.EMPREINTE in (RACINE / "docs/preinscription_incluses.md") \
        .read_text(encoding="utf-8")
    em.exiger_le_gel(ib.PARAMETRES, ib.EMPREINTE)
    # La confirmation s'ouvre au premier minuit UTC qui suit le gel (03/11).
    assert ib.OUVERTURE == em.ouverture_confirmation(
        int(ib._instant("2026-11-03T10:40:00Z")))
    assert ib.QUARANTAINE == (em.FIN_VALIDATION, ib.OUVERTURE - 86400)
    assert ib.REGARDS == (300, 600, 900)


def test_les_groupes():
    assert ib.groupe(4) == ib.FAVORABLE
    assert ib.groupe(5) == ib.EXCLU
    assert ib.groupe(6) == ib.DEFAVORABLE


def _ligne(i, gagne, groupe=ib.FAVORABLE, jouable=True, d0=0):
    return {"paire": f"P{i % 8}", "decision": d0 + i * 600,
            "exclusion": "", "groupe": groupe, "jouable": jouable,
            "issue": em.GAIN if gagne else em.PERTE}


def test_le_taux_face_au_point_mort():
    alea = random.Random(1)
    lignes = [_ligne(i, alea.random() < 0.62) for i in range(800)]
    r = ib.taux_contre_seuil(lignes, ib.POINT_MORT)
    assert r["n"] == 800 and r["bas"] < r["ecart"] < r["haut"]
    assert r["ecart"] > 0.05 and r["p"] < 0.01


def _confirmation(n_fav, taux_fav, taux_def, graine=3):
    alea = random.Random(graine)
    lignes = []
    for i in range(3 * n_fav):
        g = (ib.FAVORABLE, ib.DEFAVORABLE, ib.EXCLU)[i % 3]
        t = taux_fav if g == ib.FAVORABLE else taux_def
        lignes.append(_ligne(i, alea.random() < t, g, d0=ib.OUVERTURE))
    return lignes


def test_aucun_regard_avant_le_premier():
    assert ib.regards_atteints(_confirmation(299, 0.7, 0.4)) == []


def test_un_regard_porte_sur_les_decisions_jusqu_au_n_ieme_favorable():
    lignes = _confirmation(450, 0.52, 0.52)
    regards = ib.regards_atteints(lignes)
    assert [n for n, _, _ in regards] == [300]
    n, coupure, r = regards[0]
    favorables = sorted(l["decision"] for l in lignes
                        if l["groupe"] == ib.FAVORABLE)
    assert coupure == favorables[299]
    assert r["C2"]["n"] == 300
    seuil = em.seuils_des_regards(ib.REGARDS)[0]["nominal"]
    assert r["alpha"] == pytest.approx(seuil)
    assert ib.verdict(n, r) == "poursuite jusqu'au regard suivant"


def test_un_effet_net_est_confirme_et_arrete_les_regards():
    regards = ib.regards_atteints(_confirmation(950, 0.66, 0.42))
    assert ib.verdict(*regards[-1][::2]) == "CONFIRMÉE"
    assert len(regards) < 3 or regards[-1][2]["confirmee"]


def test_un_effet_inverse_est_abandonne():
    regards = ib.regards_atteints(_confirmation(950, 0.40, 0.55))
    assert regards and ib.verdict(*regards[-1][::2]) == "ABANDONNÉE"
    assert len(regards) == 1


# --------------------------------------------------------------------------- #
# De bout en bout
# --------------------------------------------------------------------------- #

DEBUT = ib.QUARANTAINE[0] - 2 * 86400
MAINTENANT = ib.OUVERTURE + 2 * 86400
FIN = MAINTENANT + 86400
PAIRES = ("EURUSD_otc", "GBPUSD_otc")


class FausseStrategie:
    """Un signal toutes les 37 minutes, CALL et PUT en alternance."""

    def __init__(self, p) -> None:
        self.p = p

    def on_bar(self, vue):
        ts = vue.now_ms // 1000
        if ts % (37 * 60):
            return None
        call = (ts // (37 * 60)) % 2 == 0
        return SimpleNamespace(
            direction=Direction.CALL if call else Direction.PUT,
            features={})


def _base(chemin: Path) -> None:
    conn = open_read_write(chemin)
    ecrivain = MarketWriter(conn)
    alea = random.Random(5)
    for paire in PAIRES:
        prix, bougies = 1.0, []
        for ts in range(DEBUT, FIN, 60):
            # Des heures de tendance franche (aucune bougie incluse) et des
            # heures hésitantes, où une bougie sur trois reste dans la
            # précédente ; les fenêtres à cheval donnent les cas du milieu.
            o = prix
            comprime = (ts // 7200) % 2 == 1
            pas = alea.gauss(0, 0.0004)
            prix *= 1 + (pas if comprime else abs(pas) * (
                1 if (ts // 14400) % 2 else -1))
            haut, bas = max(o, prix) * 1.00002, min(o, prix) * 0.99998
            if bougies and comprime and alea.random() < 1 / 3:
                p = bougies[-1]
                haut, bas = p.high, p.low
                prix = min(max(prix, bas), haut)
                o = min(max(o, bas), haut)
            bougies.append(Candle(paire, 60, ts, o, haut, bas, prix, 20,
                                  True))
        ecrivain.upsert_candles(bougies)
        conn.execute("INSERT INTO payouts (ts_sec, pair, payout_pct, is_open)"
                     " VALUES (?,?,?,1)", (DEBUT, paire, 90))
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def passe(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("incluses")
    active = dossier / "active.db"
    _base(active)
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    course = SimpleNamespace(
        strategie=FausseStrategie(SimpleNamespace(
            lookback=60, tolerance_pct=0.02, expiry_sec=900)),
        paires_collectees=PAIRES)
    empreintes = [hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (active, releve)]
    a, r = open_read_only(active), open_read_only(releve)
    try:
        rapport, fichiers = ib.epreuves(r, a, course, maintenant=MAINTENANT,
                                        garde=lambda p, f: True,
                                        respirer=lambda: None)
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
    assert [n for _, n, _ in fichiers] == ["incluses_signaux.csv",
                                           "incluses_resultats.csv"]


def test_la_replication_est_calculee(passe):
    *_, rapport, fichiers = passe
    signaux = _csv(fichiers, "incluses_signaux.csv")
    quarantaine = [s for s in signaux if s["epreuve"] == "quarantaine"]
    assert quarantaine and all(
        ib.QUARANTAINE[0] <= int(s["decision"]) < ib.QUARANTAINE[1]
        for s in quarantaine)
    retenus = [s for s in quarantaine if not s["exclusion"]]
    assert {s["groupe"] for s in retenus} == {
        ib.FAVORABLE, ib.DEFAVORABLE, ib.EXCLU}
    assert {s["issue"] for s in retenus} <= {em.GAIN, em.PERTE, em.EGALITE,
                                             em.MANQUANTE}
    resultats = _csv(fichiers, "incluses_resultats.csv")
    assert resultats[0]["epreuve"] == "quarantaine"
    assert resultats[0]["verdict"] == "sans pouvoir de verdict"
    assert "<b>1. Réplication sur la quarantaine</b>" in rapport
    assert "C1, favorable contre défavorable" in rapport


def test_rien_de_la_confirmation_n_est_montre_avant_le_premier_regard(passe):
    *_, rapport, fichiers = passe
    signaux = _csv(fichiers, "incluses_signaux.csv")
    confirmation = [s for s in signaux if s["epreuve"] == "confirmation"]
    assert confirmation
    assert all(int(s["decision"]) >= ib.OUVERTURE for s in confirmation)
    assert {s["issue"] for s in confirmation if not s["exclusion"]} == {
        "masquée jusqu'au prochain regard"}
    assert len(_csv(fichiers, "incluses_resultats.csv")) == 1
    assert "premier regard à 300" in rapport
    assert "Aucun résultat n'est montré avant." in rapport


def test_un_gel_modifie_ferme_l_epreuve(monkeypatch):
    monkeypatch.setitem(ib.PARAMETRES, "version", 2)
    with pytest.raises(ValueError):
        ib.epreuves(None, None, SimpleNamespace())


def test_le_service_de_bout_en_bout_sur_la_releve(passe, monkeypatch):
    from maxprofit.hosting import service
    from maxprofit.store import pool
    active, releve, empreintes, course, _, _ = passe
    texte, _ = service._incluses(active, None, "")
    assert "Course indisponible" in texte
    texte, fichiers = service._incluses(
        active, course, "", environ={pool.ENV_RELEVE: str(releve)},
        ouvrir_url=lambda url: open_read_only(url))
    assert "H_IB" in texte and len(fichiers) == 2
    assert [hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (active, releve)] == empreintes


def test_la_commande_telegram_tourne_en_tache_de_fond(tmp_path):
    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "incluses" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))
    appels = []

    async def incluses(argument):
        appels.append(argument)
        return "rapport", [(b"a;b\n", "incluses_signaux.csv", "")]

    bot._incluses = incluses

    async def deroule():
        await bot._traiter(_message("/incluses", chat=INTRUS)["message"])
        assert appels == []
        await bot._traiter(_message("/incluses")["message"])
        assert "lancée" in client.envoyes[-1][1]
        await bot._incluses_en_cours

    asyncio.run(deroule())
    assert appels == [""]
    assert client.envoyes[-1] == (CHAT, "rapport")
    assert documents == ["incluses_signaux.csv"]
