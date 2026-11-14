"""
Étude Engulfing (recherche isolée) : le gel, les motifs haussiers et
baissiers sur les corps, le retour au niveau d'entrée (bougies et ticks),
les signaux incomplets et les données manquantes, la lecture du futur,
les épisodes, l'isolation du module, et une passe de bout en bout en
lecture seule.
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
from maxprofit.live import engulfing as en
from maxprofit.store.chemin_ticks import decoder, encoder
from maxprofit.store.db import open_read_only, open_read_write
from maxprofit.store.market import MarketWriter

RACINE = Path(__file__).resolve().parents[1]
T0 = en.DEBUT + 10 * 86400 + 3600
PAIRE = "EURUSD_otc"


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def B(i: int, o: float, h: float, l: float, c: float) -> en.Bougie:
    return en.Bougie(T0 + 60 * i, o, h, l, c)


def _serie(*bougies):
    return list(bougies), {b.ts_sec: i for i, b in enumerate(bougies)}


C1 = B(0, 1.0010, 1.0012, 1.0000, 1.0002)        # rouge
C2 = B(1, 1.0002, 1.0016, 1.0001, 1.0014)        # verte, englobante


def test_le_gel_tient():
    doc = json.loads((RACINE / "docs" / "preinscription_engulfing.json")
                     .read_text(encoding="utf-8"))
    assert doc == en.PARAMETRES
    assert em.empreinte(en.PARAMETRES) == en.EMPREINTE
    autre = json.loads(json.dumps(en.PARAMETRES))
    autre["execution"]["attente_max_min"] = 5
    with pytest.raises(ValueError):
        em.exiger_le_gel(autre, en.EMPREINTE)
    assert en.POINT_MORT == pytest.approx(0.5208333, abs=1e-6)
    assert en.FLUX_MIN == 84
    assert en.ECHEANCES == (1, 2, 5, 10, 15) and en.DELAI_PRINCIPAL == 2
    assert list(en.NOMS_VARIANTES) == list(doc["variantes"])


def test_motifs_haussier_et_baissier_sur_les_corps():
    assert en.motif(C1, C2) == en.HAUSSIER
    assert en.sens_du_trade(en.HAUSSIER) == -1            # PUT
    d1 = B(0, 1.0002, 1.0012, 1.0000, 1.0010)             # verte
    d2 = B(1, 1.0010, 1.0011, 0.9996, 0.9998)             # rouge, englobante
    assert en.motif(d1, d2) == en.BAISSIER
    assert en.sens_du_trade(en.BAISSIER) == 1             # CALL
    # Les mèches de c2 couvrent c1, pas son corps : ce n'est pas un motif.
    assert en.motif(C1, B(1, 1.0003, 1.0030, 0.9980, 1.0008)) == 0
    # Même couleur, minutes non consécutives, corps égaux, ouverture de c2
    # au-dessus de la clôture de c1.
    assert en.motif(C1, B(1, 1.0012, 1.0013, 1.0000, 1.0001)) == 0
    assert en.motif(C1, en.Bougie(T0 + 120, 1.0002, 1.0016, 1.0001,
                                  1.0014)) == 0
    assert en.motif(B(0, 1.0010, 1.0011, 1.0001, 1.0002),
                    B(1, 1.0002, 1.0011, 1.0001, 1.0010)) == 0
    assert en.motif(C1, B(1, 1.0003, 1.0016, 1.0002, 1.0014)) == 0


def _signal(*apres, fin=T0 + 86400):
    bougies, index = _serie(C1, C2, *apres)
    s = en.caracteriser(bougies, 0, en.HAUSSIER, None)
    s.paire = PAIRE
    en.issues_m1(s, index, bougies, fin)
    en.scenario_a(s, index, bougies)
    return s


def test_entree_m1_au_retour_du_niveau_et_scenario_a_invalide():
    s = _signal(B(2, 1.0014, 1.0018, 1.0011, 1.0016),    # pas d'atteinte
                B(3, 1.0016, 1.0017, 1.0008, 1.0009),    # atteinte
                B(4, 1.0009, 1.0012, 1.0005, 1.0006))
    assert s.niveau == 1.0010 and s.statut_m1 == en.DECLENCHE
    assert s.touche_m1_ts == T0 + 180 and s.entree_m1 == 1.0010
    assert not s.entree_m1_ouverture
    assert s.issues_m1[1] == en.GAIN and s.issues_m1[2] == en.GAIN
    assert s.issues_m1[5] is None                        # sortie absente
    assert s.fin_episode == T0 + 180 + 60 + 15 * 60
    # Scénario A : entrée à l'ouverture de c1. À 1 min le PUT gagne
    # toujours (c1 est rouge), à 2 min il perd toujours (c2 clôture
    # au-dessus) : c'est le motif lui-même, lu après coup.
    assert s.issues_a[1] == en.GAIN and s.issues_a[2] == en.PERTE


def test_trou_fin_des_donnees_non_declenche_et_ouverture_au_dela():
    assert _signal(B(3, 1.0016, 1.0017, 1.0008, 1.0009)).statut_m1 == \
        "trou avant l'atteinte"
    assert _signal(fin=T0 + 150).statut_m1 == "fin des données"
    loin = [B(2 + k, 1.0015, 1.0020, 1.0012, 1.0016) for k in range(10)]
    s = _signal(*loin)
    assert s.statut_m1 == en.NON_DECLENCHE and not s.issues_m1
    assert s.fin_episode == s.fin_c2 + 600
    s = _signal(B(2, 1.0007, 1.0009, 1.0004, 1.0005))
    assert s.entree_m1_ouverture and s.entree_m1 == 1.0007


# --------------------------------------------------------------------------- #
# Les ticks
# --------------------------------------------------------------------------- #

def _chemins(ticks: list[Tick], sans: set[int] = frozenset()):
    par_minute: dict[int, list[Tick]] = {}
    for t in ticks:
        par_minute.setdefault(t.ts_ms // 60000 * 60, []).append(t)
    chemins = {}
    for m, liste in par_minute.items():
        if m in sans:
            continue
        c = encoder(liste)
        chemins[m] = (c.n_ticks, c.echelle, c.octets)
    return lambda jour: {m: v for m, v in chemins.items()
                         if m // 86400 == jour}


def _flux():
    """Un tick toutes les 5 s. Un tick PIÈGE sous le niveau AVANT la fin de
    c2 (à ignorer) ; l'atteinte à fin_c2 + 20 s ; un tick à 1.0003 une
    seconde plus tard (l'entrée à 2 s s'y fait) ; puis 1.0005."""
    fin_c2 = T0 + 120
    ticks = [Tick(PAIRE, (fin_c2 - 1) * 1000, 1.0009)]
    for k, p in enumerate((1.0014, 1.0013, 1.0012, 1.0011, 1.0010)):
        ticks.append(Tick(PAIRE, (fin_c2 + 5 * k) * 1000, p))
    ticks.append(Tick(PAIRE, (fin_c2 + 21) * 1000, 1.0003))
    for t in range(fin_c2 + 25, fin_c2 + 25 * 60, 5):
        ticks.append(Tick(PAIRE, t * 1000, 1.0005))
    return ticks


def test_atteinte_par_les_ticks_sans_lire_avant_la_fin_de_c2():
    s = _signal()
    s.med_etendue = 0.0003
    en.issues_ticks(s, en.Ticks(_chemins(_flux())))
    assert s.statut_ticks == en.DECLENCHE
    assert s.touche_ms == (T0 + 140) * 1000, \
        "le piège d'avant c2 est ignoré"
    assert s.attente_sec == 20
    # Entrée à 0 s : 1.0010 → 1.0005, le PUT gagne. Entrée à 2 s : 1.0003 →
    # 1.0005, il perd. La sensibilité au délai est réelle.
    assert all(s.issues_ticks[(0, t)] == en.GAIN for t in en.ECHEANCES)
    assert all(s.issues_ticks[(2, t)] == en.PERTE for t in en.ECHEANCES)
    assert s.trajectoire[60] == pytest.approx(-2 / 3)
    assert s.defavorable_d_abord and not s.favorable_d_abord


def test_ticks_manquants_et_flux_gele():
    s = _signal()
    en.issues_ticks(s, en.Ticks(_chemins(_flux(), sans={T0 + 120})))
    assert s.statut_ticks == en.SANS_TICKS and not s.issues_ticks
    s = _signal()
    en.issues_ticks(s, en.Ticks(_chemins(_flux(), sans={T0 + 600})))
    assert (0, 1) in s.issues_ticks and (0, 5) in s.issues_ticks
    assert s.statuts_sortie[(0, 10)] == "ticks manquants à la sortie"
    # Plus aucun tick pendant les 20 s qui précèdent la sortie à 1 min.
    flux = [t for t in _flux()
            if not (T0 + 180 < t.ts_ms / 1000 <= T0 + 200)]
    s = _signal()
    en.issues_ticks(s, en.Ticks(_chemins(flux)))
    assert s.statuts_sortie[(0, 1)] == "flux gelé à la sortie"


def test_le_decodage_en_tableaux_egale_celui_du_depot():
    alea = random.Random(3)
    m = T0 // 60 * 60
    ticks, prix = [], 1.08231
    for k in range(150):
        prix = round(prix + alea.choice((-1, 0, 1)) * 1e-5, 5)
        ticks.append(Tick(PAIRE, m * 1000 + alea.randrange(60000), prix))
    c = encoder(ticks)
    ts, px = en.ticks_de_chemin(c.minute_sec, c.n_ticks, c.echelle, c.octets)
    attendu = decoder(c)
    assert list(ts) == [t.ts_ms for t in attendu]
    assert list(px) == [t.price for t in attendu]


# --------------------------------------------------------------------------- #
# Aucune lecture du futur
# --------------------------------------------------------------------------- #

def _marche(n: int, graine: int = 5) -> list[en.Bougie]:
    alea = random.Random(graine)
    p, sortie = 1.1, []
    for i in range(n):
        # L'ouverture est le premier tick de la minute : elle diffère un peu
        # de la clôture précédente. Sans cet écart, deux corps partagent
        # chaque extrême et ZoneH1 ne trouve aucune pique UNIQUE.
        o = round(p * math.exp(alea.gauss(0, 5e-5)), 5)
        c = round(o * math.exp(alea.gauss(0, 3e-4)), 5)
        h = round(max(o, c) + abs(alea.gauss(0, 1e-4)), 5)
        l = round(min(o, c) - abs(alea.gauss(0, 1e-4)), 5)
        sortie.append(en.Bougie(T0 + 60 * i, o, h, l, c))
        p = c
    return sortie


def test_le_futur_ne_change_ni_le_motif_ni_les_variantes():
    bougies = _marche(900)
    zones = en.zones_de(bougies)
    vus = 0
    for i1 in range(320, 880):
        t = en.motif(bougies[i1], bougies[i1 + 1])
        if not t:
            continue
        avant = en.caracteriser(bougies, i1, t, zones)
        futur = list(bougies[:i1 + 2]) + [
            en.Bougie(b.ts_sec, b.open * 1.01, b.high * 1.01, b.low * 1.01,
                      b.close * 1.01) for b in bougies[i1 + 2:]]
        apres = en.caracteriser(futur, i1, t, en.zones_de(futur))
        for nom in ("zone", "retouches", "membres", "mouvement",
                    "compression", "corps_c2_rel", "c2_ce", "distance_zone"):
            assert getattr(avant, nom) == getattr(apres, nom), (i1, nom)
        vus += 1
    assert vus > 50
    assert any(en.caracteriser(bougies, i1, en.motif(bougies[i1],
                                                     bougies[i1 + 1]),
                               zones).zone is not None
               for i1 in range(320, 880)
               if en.motif(bougies[i1], bougies[i1 + 1])), \
        "au moins un contact de zone dans l'échantillon"


def test_un_seul_signal_par_episode_et_par_variante():
    def sig(c1_ts, membres, fin):
        s = en.Signal(paire=PAIRE, type_=1, c1_ts=c1_ts, niveau=1.0,
                      c1=(1, 1, 1, 1), c2=(1, 1, 1, 1))
        s.membres, s.fin_episode = membres, fin
        return s
    a, b, c, d = (sig(0, 0b11, 1000), sig(500, 0b01, 1500),
                  sig(600, 0b10, 1600), sig(1000, 0b11, 2000))
    en.episodes([a, b, c, d])
    assert [x.retenus for x in (a, b, c, d)] == [0b11, 0, 0, 0b11]


def test_le_module_est_isole():
    source = (RACINE / "maxprofit" / "live" / "engulfing.py") \
        .read_text(encoding="utf-8")
    modules = {n.module for n in ast.walk(ast.parse(source))
               if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Import) for a in n.names}
    interdits = ("maxprofit.execution", "maxprofit.collect", "maxprofit.plan",
                 "maxprofit.live.plan_demo", "maxprofit.hosting")
    assert not [m for m in modules if m.startswith(interdits)], modules
    for mot in ("INSERT", "UPDATE ", "DELETE", "upsert", "insert_"):
        assert mot not in source, mot


# --------------------------------------------------------------------------- #
# De bout en bout, en lecture seule
# --------------------------------------------------------------------------- #

DEBUT_DONNEES = en.FIN_EXPLORATION - 3 * 86400
FIN_DONNEES = en.FIN_EXPLORATION + 2 * 86400
APRES = 86400                     # un jour de plus, qui ne doit pas être lu


def _base(chemin: Path) -> None:
    conn = open_read_write(chemin)
    ecrivain = MarketWriter(conn)
    for k, paire_ in enumerate(("EURUSD_otc", "GBPUSD_otc")):
        alea = random.Random(11 + k)
        prix = 1.1 + 0.2 * k
        for m in range(DEBUT_DONNEES, FIN_DONNEES + APRES, 60):
            ticks = []
            for s in range(0, 60, 5):
                prix = round(prix * math.exp(alea.gauss(0, 6e-5)), 5)
                ticks.append(Tick(paire_, (m + s) * 1000, prix))
            px = [t.price for t in ticks]
            ecrivain.upsert_candles([Candle(paire_, 60, m, px[0], max(px),
                                            min(px), px[-1], len(px), True)])
            ecrivain.insert_tick_paths([encoder(ticks)])
        conn.execute("INSERT INTO payouts (ts_sec, pair, payout_pct, is_open)"
                     " VALUES (?,?,?,1)", (en.DEBUT - 60, paire_, 92))
    conn.commit()
    conn.close()


class Espion:
    """Note les bornes de temps de chaque lecture."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.bornes: list[int] = []

    def execute(self, sql, *args):
        if args and ("candles" in sql or "tick_paths" in sql):
            self.bornes += [int(v) for v in args[0] if isinstance(v, int)]
        return self.conn.execute(sql, *args)


@pytest.fixture(scope="module")
def passe(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("engulfing")
    active = dossier / "active.db"
    _base(active)
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    empreintes = [hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (active, releve)]
    a, r = open_read_only(active), open_read_only(releve)
    espion = Espion(r)
    try:
        rapport, fichiers = en.etude(espion, a, maintenant=FIN_DONNEES + 3600,
                                     respirer=lambda: None)
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
    assert "aucun ordre" in rapport
    assert espion.bornes and max(espion.bornes) <= FIN_DONNEES
    assert [n for _, n, _ in fichiers] == [
        "engulfing_cases.csv", "engulfing_details.csv",
        "engulfing_pertes.csv", "engulfing_statuts.csv",
        "engulfing_donnees.csv", "engulfing_signaux.csv.gz"]


def test_passe_complete_cases_signaux_et_statuts(passe):
    *_, rapport, fichiers = passe
    cases = _lignes(fichiers, "engulfing_cases.csv")
    assert len(cases) == 13 * 3 * 5 * 2
    principal = [c for c in cases if c["variante"] == "V00_tous"
                 and c["mode"] == "ticks" and c["delai_sec"] == "2"]
    assert all(int(c["n"]) > 0 for c in principal)
    assert all(c["verdict"] for c in principal
               if c["periode"] == "exploration")
    signaux = _lignes(fichiers, "engulfing_signaux.csv.gz")
    assert signaux and {s["direction"] for s in signaux} == {"CALL", "PUT"}
    assert all((s["type"] == "haussier") == (s["direction"] == "PUT")
               for s in signaux)
    statuts = _lignes(fichiers, "engulfing_statuts.csv")
    assert any(s["source"] == "motifs" for s in statuts)
    donnees = _lignes(fichiers, "engulfing_donnees.csv")
    assert {d["paire"] for d in donnees} == {"EURUSD_otc", "GBPUSD_otc"}
    assert max(d["jour"] for d in donnees) < "2026-11-05"
    pertes = _lignes(fichiers, "engulfing_pertes.csv")
    assert {p["nature"] for p in pertes} == {
        "connue à la décision", "après l'entrée (descriptif)"}
    assert all(p["notee"] == "—" for p in pertes
               if p["nature"] != "connue à la décision")
    assert "Scénario A, INVALIDE" in rapport


def test_avant_la_fin_de_l_exploration_rien_a_valider(passe):
    active, releve, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    try:
        texte, fichiers = en.etude(r, a, maintenant=en.FIN_EXPLORATION - 60,
                                   respirer=lambda: None)
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "rien à valider" in texte


def test_le_service_et_la_commande(monkeypatch, tmp_path):
    from maxprofit.hosting import service
    from maxprofit.store import pool
    vus = []

    def faux(lue, active, course=None, *, nom_lecture, avec_ticks):
        vus.append((nom_lecture, avec_ticks))
        return "🕯 Engulfing", []

    monkeypatch.setattr(en, "etude", faux)
    chemin = tmp_path / "active.db"
    open_read_write(chemin).close()
    texte, _ = service._engulfing(chemin, None, "")
    assert "Aucune relève" in texte
    for argument, ticks in (("", True), ("sans_ticks", False)):
        texte, _ = service._engulfing(
            chemin, None, argument, environ={pool.ENV_RELEVE: str(chemin)},
            ouvrir_url=lambda url: open_read_only(url))
        assert texte == "🕯 Engulfing"
    assert vus == [("relève", True), ("relève", False)]

    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "engulfing" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))

    async def engulfing(argument):
        return "rapport", [(b"a;b\n", "engulfing_cases.csv", "")]

    bot._engulfing = engulfing

    async def deroule():
        await bot._traiter(_message("/engulfing", chat=INTRUS)["message"])
        assert documents == []
        await bot._traiter(_message("/engulfing")["message"])
        await bot._engulfing_en_cours

    asyncio.run(deroule())
    assert documents == ["engulfing_cases.csv"]
