"""
/verifier : la lecture des trades collés (formats variés, fuseaux, années),
leur rejeu sur des ticks connus (gain, perte, égalité, ticks manquants,
échéance à venir), le fuseau retrouvé grâce aux prix, le bilan et le
verdict, la conformité des résultats annoncés, l'échappement HTML, la lecture
seule, le service et la commande.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from maxprofit.core.types import Tick
from maxprofit.live import engulfing as en
from maxprofit.live import verification as vf
from maxprofit.store.chemin_ticks import encoder
from maxprofit.store.db import open_read_only, open_read_write
from maxprofit.store.market import MarketWriter

RACINE = Path(__file__).resolve().parents[1]
DEBUT = (en.FIN_EXPLORATION + 5 * 86400) // 3600 * 3600 + 12 * 3600
PAIRE = "EURUSD_otc"
MAINTENANT = DEBUT + 2 * 86400
UTC3 = timezone(timedelta(hours=3))


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def prix_de(t: int) -> float:
    """Monte de 0,0001 par minute, sauf un palier plat de la minute 20 à
    la minute 29."""
    k = (t - DEBUT) // 60
    if 20 <= k < 30:
        k = 20
    return round(1.10000 + 0.0001 * k, 5)


SANS = {DEBUT + 60 * 40}                 # la minute 40 n'a pas de ticks
TARDIVE = DEBUT + 60 * 45                # la minute 45 commence à + 40 s


@pytest.fixture()
def base(tmp_path) -> Path:
    chemin = tmp_path / "active.db"
    conn = open_read_write(chemin)
    w = MarketWriter(conn)
    for k in range(60):
        m = DEBUT + 60 * k
        if m in SANS:
            continue
        debut_s = 40 if m == TARDIVE else 0
        w.insert_tick_paths([encoder([Tick(PAIRE, (m + s) * 1000,
                                           prix_de(m + s))
                                      for s in range(debut_s, 60, 2)])])
    conn.execute("INSERT INTO payouts (ts_sec, pair, payout_pct, is_open) "
                 "VALUES (?,?,?,1)", (DEBUT - 60, PAIRE, 92))
    conn.commit()
    conn.close()
    return chemin


def local(minute: int, seconde: int = 30, fuseau=UTC3) -> datetime:
    return datetime.fromtimestamp(DEBUT + 60 * minute + seconde,
                                  timezone.utc).astimezone(fuseau)


def ligne(minute, sens="CALL", duree="1min", seconde=30, fuseau=UTC3,
          extra=""):
    t = local(minute, seconde, fuseau)
    return (f"EURUSD {sens} {t:%d/%m/%Y} {t:%H:%M:%S} {duree} {extra}"
            ).strip()


def test_les_fuseaux():
    assert vf.lire_fuseau("UTC+3")[1] == "UTC+3"
    assert vf.lire_fuseau("+5:30")[0].utcoffset(None) == timedelta(
        hours=5, minutes=30)
    assert vf.lire_fuseau("-5")[0].utcoffset(None) == timedelta(hours=-5)
    assert vf.lire_fuseau("GMT−4")[1] == "UTC−4"
    assert isinstance(vf.lire_fuseau("Europe/Paris")[0], ZoneInfo)
    for faux in ("UTC+15", "Mars/Olympus", "+3:20"):
        with pytest.raises(ValueError):
            vf.lire_fuseau(faux)


def test_les_formats_de_ligne():
    ici = datetime(2026, 11, 27, 20, 0)
    cas = {
        "EURUSD CALL 27/11 14:05:30 1min 1.08452 gagné":
            ("EURUSD_otc", vf.CALL, datetime(2026, 11, 27, 14, 5, 30), 60,
             1.08452, 1),
        "EUR/USD OTC put 2026-11-27 14:05 5m":
            ("EURUSD_otc", vf.PUT, datetime(2026, 11, 27, 14, 5), 300, None,
             None),
        "GBPJPY;VENTE;27/11/2026;14:12:05;30s;perdu":
            ("GBPJPY_otc", vf.PUT, datetime(2026, 11, 27, 14, 12, 5), 30,
             None, -1),
        "audcad-otc haut 27.11 14h05 M3 1,08452":
            ("AUDCAD_otc", vf.CALL, datetime(2026, 11, 27, 14, 5), 180,
             1.08452, None),
        "EURUSD ACHAT 27/11 14:05:00 14:06:00":
            ("EURUSD_otc", vf.CALL, datetime(2026, 11, 27, 14, 5), 60, None,
             None),
        "USDJPY BUY 2026-11-27T09:00:10 2 ✅":
            ("USDJPY_otc", vf.CALL, datetime(2026, 11, 27, 9, 0, 10), 120,
             None, 1),
    }
    for texte, attendu in cas.items():
        t, raison = vf.lire_trade(1, texte, ici, None)
        assert raison == "", (texte, raison)
        assert (t.paire, t.sens, t.local, t.duree_sec, t.prix,
                t.annonce) == attendu, texte
    assert vf.lire_trade(1, "EURUSD CALL 27/11 14:05:30", ici, None)[1] == \
        "manque l'échéance"
    assert "le sens" in vf.lire_trade(1, "EURUSD 27/11 14:05 1m", ici,
                                      None)[1]
    assert "hors limites" in vf.lire_trade(1, "EURUSD CALL 27/11 14:05 2s",
                                           ici, None)[1]
    futur, _ = vf.lire_trade(1, "EURUSD CALL 28/12 10:00 1m", ici, None)
    assert futur.local.year == 2025, "une date à venir est de l'an dernier"
    defaut = datetime(2026, 11, 20)
    t, _ = vf.lire_trade(1, "EURUSD CALL 10:00:05 1m", ici, defaut)
    assert t.local == datetime(2026, 11, 20, 10, 0, 5)


def test_la_lecture_du_message_en_entier():
    texte = ("/verifier@MaxBot\nfuseau UTC+3\ndate 27/11/2026\n"
             "EURUSD CALL 14:05:30 1min\n\nn'importe quoi\n"
             "fuseau Pluton/Nord\nGBPUSD PUT 14:06:00 2min")
    trades, erreurs, fuseau = vf.lire(texte, datetime(2026, 11, 27, 20))
    assert [t.paire for t in trades] == ["EURUSD_otc", "GBPUSD_otc"]
    assert fuseau[1] == "UTC+3"
    assert [e[0] for e in erreurs] == [6, 7]
    assert all(t.local.date() == datetime(2026, 11, 27).date()
               for t in trades)


def _verifier(base, texte):
    conn = open_read_only(base)
    try:
        return vf.verifier(conn, texte, maintenant=MAINTENANT,
                           fuseau_defaut=ZoneInfo("America/Port-au-Prince"))
    finally:
        conn.close()


def test_le_rejeu_gain_perte_egalite_et_ticks_manquants(base):
    texte = "\n".join([
        "/verifier", "fuseau UTC+3",
        ligne(5, "CALL"),                 # monte : gagné
        ligne(6, "PUT"),                  # monte : perdu
        ligne(22, "CALL"),                # palier plat : égalité
        ligne(40, "CALL", seconde=10),    # minute sans ticks
        ligne(39, "CALL", duree="1min", seconde=50),   # sortie sans ticks
        ligne(5, "CALL", duree="3min"),   # gagné aussi
        ligne(45, "CALL", seconde=12),    # dernier tick connu : 14 s avant
        ligne(25, "PUT", duree="5min"),   # 1 min : égalité ; 5 min : perdu
    ])
    rapport, fichiers = _verifier(base, texte)
    lignes = fichiers[0][0].decode("utf-8").splitlines()
    tete = lignes[0].split(";")
    par_ligne = [dict(zip(tete, l.split(";"))) for l in lignes[1:]]
    issues = [x["issue"] for x in par_ligne]
    assert issues == ["gagné", "perdu", "égalité", "", "", "gagné", "",
                      "perdu"]
    assert par_ligne[3]["statut"] == "pas de tick frais à l'ouverture"
    assert par_ligne[4]["statut"] == "pas de tick frais à l'échéance"
    assert par_ligne[6]["statut"] == "pas de tick frais à l'ouverture", \
        "un tick de 14 s n'est plus frais"
    assert par_ligne[0]["utc"] == en._utc(DEBUT + 5 * 60 + 30,
                                          "%Y-%m-%d %H:%M:%S")
    assert float(par_ligne[0]["prix_entree"]) == pytest.approx(prix_de(
        DEBUT + 5 * 60 + 30))
    assert "Jugés : 5</b>" in rapport and "gagnés 2, perdus 2, égalités 1" \
        in rapport
    assert "UTC+3 (donné)" in rapport
    assert "trop peu de trades pour conclure" in rapport


def test_le_fuseau_est_retrouve_grace_aux_prix(base):
    lignes = []
    for minute in (3, 7, 11, 15):
        t = DEBUT + 60 * minute + 30
        lignes.append(ligne(minute, "CALL", extra=f"{prix_de(t):.5f}"))
    rapport, _ = _verifier(base, "\n".join(lignes))
    assert "UTC+3, retrouvé grâce aux prix d'entrée (écart médian 0,0 pip " \
        "sur 4 trades)" in rapport
    assert "Jugés : 4</b>" in rapport and "gagnés 4" in rapport
    sans_prix, _ = _verifier(base, ligne(3, "CALL", fuseau=UTC3))
    assert "America/Port-au-Prince par défaut" in sans_prix


def test_les_resultats_annonces_et_l_echappement(base):
    texte = "\n".join([
        "fuseau UTC+3",
        ligne(5, "CALL", extra="gagné"),
        ligne(6, "PUT", extra="gagné"),        # annoncé gagné, en fait perdu
        ligne(7, "PUT", extra="perdu"),
        "EURUSD <b>CALL</b> 31/12",
    ])
    rapport, _ = _verifier(base, texte)
    assert "Résultat annoncé : 2 conformes sur 3" in rapport
    assert "historique arrangé" in rapport
    assert "(annoncé autrement)" in rapport
    assert "&lt;b&gt;CALL&lt;/b&gt;" in rapport and "<b>CALL</b>" not in \
        rapport


def test_l_echeance_a_venir_n_est_pas_jugee(base):
    conn = open_read_only(base)
    try:
        rapport, _ = vf.verifier(conn, "fuseau UTC+3\n" + ligne(5),
                                 maintenant=DEBUT + 5 * 60 + 60)
    finally:
        conn.close()
    assert "échéance trop récente ou à venir" in rapport


def test_le_bilan_et_le_verdict():
    assert vf.p_unilaterale(60, 100) == pytest.approx(
        sum(math.comb(100, k) for k in range(60, 101)) / 2 ** 100)
    assert vf.p_unilaterale(0, 0) is None

    def b(g, l, e=0):
        trades = []
        for k, issue in enumerate([1] * g + [-1] * l + [0] * e):
            t = vf.Trade(k, "", PAIRE, vf.CALL, datetime(2026, 1, 1), True,
                         60)
            t.issue = issue
            t.entree = (0, 1.0)
            trades.append(t)
        return vf.bilan(trades)
    assert "trop peu" in vf.verdict(b(15, 5))
    assert "nettement" in vf.verdict(b(330, 170))
    assert "dans ce que le hasard produit" in vf.verdict(b(56, 44))
    assert "pas au-dessus" in vf.verdict(b(50, 50))
    x = b(6, 3, 1)
    assert (x["n"], x["taux"], x["taux_decisifs"]) == (10, 0.6, 6 / 9)
    assert x["resultat_mises"] == pytest.approx(6 * 0.92 - 3)


def test_l_aide_sans_trade(base):
    rapport, fichiers = _verifier(base, "/verifier")
    assert rapport.startswith("🔎 <b>/verifier</b>") and fichiers == []


def test_le_module_est_isole_et_en_lecture_seule(base):
    source = (RACINE / "maxprofit" / "live" / "verification.py").read_text(
        encoding="utf-8")
    importes = set()
    for noeud in ast.walk(ast.parse(source)):
        if isinstance(noeud, ast.ImportFrom) and noeud.module:
            importes.add(noeud.module)
        elif isinstance(noeud, ast.Import):
            importes.update(a.name for a in noeud.names)
    for interdit in ("maxprofit.execution", "maxprofit.collect",
                     "maxprofit.plan", "maxprofit.hosting",
                     "maxprofit.strategies"):
        assert not any(m == interdit or m.startswith(interdit + ".")
                       for m in importes), interdit
    for mot in ("INSERT", "UPDATE ", "DELETE", "upsert", "insert_"):
        assert mot not in source, mot
    avant = hashlib.sha256(base.read_bytes()).hexdigest()
    _verifier(base, "fuseau UTC+3\n" + ligne(5))
    assert hashlib.sha256(base.read_bytes()).hexdigest() == avant


def test_le_service_et_la_commande(monkeypatch, base, tmp_path):
    from maxprofit.hosting import service
    texte, fichiers = service._verifier(base, None,
                                        "/verifier\nfuseau UTC+3\n"
                                        + ligne(5), environ={})
    assert "Vérification de 1 trade OTC" in texte and "gagnés 1" in texte
    assert [n for _, n, _ in fichiers] == ["verification.csv"]

    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "verifier" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))
    recus = []

    async def verifier(argument):
        recus.append(argument)
        await asyncio.sleep(0.05)
        return "🔎 rapport", [(b"a;b\n", "verification.csv", "")]

    bot._verifier = verifier

    async def deroule():
        await bot._traiter(_message("/verifier\nEURUSD CALL 27/11 14:05 1m",
                                    chat=INTRUS)["message"])
        assert documents == []
        await bot._traiter(_message("/verifier\nEURUSD CALL 27/11 14:05 1m")
                           ["message"])
        await asyncio.sleep(0.01)
        await bot._traiter(_message("/verifier\nGBPUSD PUT 27/11 14:05 1m")
                           ["message"])
        await bot._verifier_en_cours

    asyncio.run(deroule())
    assert documents == ["verification.csv"]
    assert recus == ["/verifier\nEURUSD CALL 27/11 14:05 1m"]
    textes = [t for _, t in client.envoyes]
    assert "🔎 rapport" in textes
    assert any("déjà en cours" in t for t in textes)
