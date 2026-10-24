"""L'étape 0 des ticks (/ticks) : chaque contrôle, en lecture seule."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from maxprofit.apprentissage import audit_ticks as at
from maxprofit.apprentissage.audit_ticks import csv_anomalies
from maxprofit.collect.collector import CandleAggregator
from maxprofit.core.types import Tick
from maxprofit.live.ticks import (
    lire_battements, lire_ordres, nouvel_audit, texte)
from maxprofit.store.chemin_ticks import encoder
from maxprofit.store.db import open_read_write
from maxprofit.store.market import MarketReader, MarketWriter

T0 = 1_792_800_000 // 3600 * 3600          # un début d'heure
P = "EURUSD_otc"
HEURE_BROKER_MS = 2 * 3_600_000


def _flux(pair=P, debut=T0, minutes=60, pas_ms=450, base=1.08000,
          trous=(), seconde=False, gels=()):
    """Des ticks réguliers ; `trous` : minutes sans ticks ; `gels` :
    (début_ms, durée_ms) sans ticks ; `seconde` : horodatages arrondis à la
    seconde."""
    ticks, prix, ms = [], base, debut * 1000
    k = 0
    while ms < (debut + 60 * minutes) * 1000:
        minute = (ms // 1000 - debut) // 60
        gele = any(g <= ms < g + d for g, d in gels)
        if minute not in trous and not gele:
            k += 1
            prix = round(prix + (0.00001 if k % 3 else -0.00002), 5)
            ts = ms // 1000 * 1000 if seconde else ms + (k % 7)
            ticks.append(Tick(pair=pair, ts_ms=ts, price=prix))
        ms += pas_ms
    return ticks


def _base(tmp_path, flux, battements=True, nom="m.db"):
    """Une base comme le collecteur l'écrit : le VRAI agrégateur fabrique
    bougies et chemins à partir des mêmes ticks."""
    conn = open_read_write(tmp_path / nom)
    ecrivain = MarketWriter(conn)
    agg = CandleAggregator(garder_ticks=True)
    for t in flux:
        agg.add(t)
    ecrivain.upsert_candles(agg.drain_all())
    ecrivain.insert_tick_paths([encoder(p) for p in agg.drain_ticks()])
    if battements:
        for s in range(T0 - 60, T0 + 3 * 3600, 30):
            ecrivain.heartbeat(s, 1)
    conn.commit()
    return conn


def _auditer(conn, paires=(P,), signaux=None, minutes=60, ordres=True):
    lecteur = MarketReader(conn)
    audit = nouvel_audit(T0, T0 + 60 * minutes,
                         joue=lambda e: e.contexte.get("mouvement_heure", 0)
                         >= -3.3)
    audit.noter_battements(lire_battements(conn, audit.debut, audit.fin))
    for p in paires:
        bougies = lecteur.candles(p, 60, T0 - 3600, T0 + 60 * minutes)
        audit.ajouter_paire(p, bougies,
                            lecteur.tick_paths(p, audit.debut, audit.fin),
                            (signaux or {}).get(p, ()))
    if ordres:
        audit.comparer_ordres(lire_ordres(conn), lecteur.ticks)
    return audit, audit.resultat(commit="test")


def test_des_donnees_saines_passent(tmp_path):
    conn = _base(tmp_path, _flux())
    _a, r = _auditer(conn)
    s = r["statuts"]
    assert s["horodatages"] == "PASS"
    assert s["concordance"] == "PASS"
    assert s["trous"] == "PASS"
    h = r["controles"]["horodatages"]
    assert h["granularite_ms"] == 1 and h["hors_minute"] == 0
    k = r["controles"]["concordance"]
    assert k["classes"] == {"identique": 60}
    assert r["controles"]["trous"]["minutes_avec_ticks"] == 60
    assert not [x for x in r["anomalies"]["comptes"] if x[0] == "FAIL"]
    # Rien d'assez long pour un signal, rien comme ordre : indéterminés.
    assert s["signaux"] == "INDÉTERMINÉ" and s["prix"] == "INDÉTERMINÉ"
    json.dumps(r)                          # sauvable avec l'état


def test_des_horodatages_a_la_seconde_echouent(tmp_path):
    conn = _base(tmp_path, _flux(seconde=True))
    _a, r = _auditer(conn)
    assert r["controles"]["horodatages"]["granularite_ms"] == 1000
    assert r["statuts"]["horodatages"] == "FAIL"
    assert any("SECONDE" in x for x in r["limites"])
    assert r["controles"]["horodatages"]["doublons_instant"] > 0


def test_les_ecarts_de_concordance_explicables_et_les_autres(tmp_path):
    conn = _base(tmp_path, _flux())
    m = T0 + 600
    # Une bougie au haut faussé : inexplicable.
    conn.execute("UPDATE candles SET high = high + 0.001 WHERE pair = ? AND "
                 "ts_sec = ?", (P, m))
    # Une bougie restée incomplète : explicable.
    conn.execute("UPDATE candles SET complete = 0, tick_count = tick_count "
                 "+ 1 WHERE pair = ? AND ts_sec = ?", (P, m + 60))
    # Une minute coupée par un redémarrage : la bougie a vu toute la minute
    # (règle MAX), le chemin n'en garde que le début.
    coupe = [t for t in _flux() if (m + 120) * 1000 <= t.ts_ms
             < (m + 140) * 1000]
    conn.execute("DELETE FROM tick_paths WHERE pair = ? AND minute_sec = ?",
                 (P, m + 120))
    MarketWriter(conn).insert_tick_paths([encoder(coupe)])
    conn.commit()
    _a, r = _auditer(conn)
    k = r["controles"]["concordance"]
    assert k["classes"]["écart inexpliqué"] == 1
    assert k["classes"]["bougie incomplète"] == 1
    assert k["classes"]["minute coupée"] == 1
    assert k["champs_inexpliques"] == {"h": 1}
    assert r["statuts"]["concordance"] == "FAIL", "1 sur 60 > 0,1 %"
    premiere = r["anomalies"]["premieres"][0]
    assert premiere["gravite"] == "FAIL"
    assert premiere["nature"] == "concordance M1 inexpliquée"
    assert premiere["debut"] == m


def test_les_trous_selon_que_le_collecteur_battait_ou_non(tmp_path):
    trous = set(range(10, 45))                      # 35 min sans ticks
    conn = _base(tmp_path, _flux(trous=trous))
    _a, r = _auditer(conn)
    t = r["controles"]["trous"]
    assert t["manquantes_ecoutees"] == 35 and t["manquantes_muettes"] == 0
    assert r["statuts"]["trous"] == "FAIL"
    assert t["interruptions_longues"] == 1
    assert ["WARNING", "interruption longue", 1] in r["anomalies"]["comptes"]
    # Collecteur arrêté : les mêmes minutes manquent, sans faute de flux.
    muet = _base(tmp_path, _flux(trous=trous), battements=False, nom="n.db")
    _a, r = _auditer(muet)
    assert r["statuts"]["trous"] == "INDÉTERMINÉ"
    assert any("battement" in x for x in r["limites"])


def test_une_paire_sans_ticks_et_des_ticks_sans_battement(tmp_path):
    conn = _base(tmp_path, _flux(minutes=60), battements=False)
    ecrivain = MarketWriter(conn)
    for s in range(T0 - 60, T0 + 1800, 30):        # le collecteur s'arrête
        ecrivain.heartbeat(s, 1)
    conn.commit()
    _a, r = _auditer(conn, paires=(P, "GBPUSD_otc"))
    t = r["controles"]["trous"]
    assert t["paires_sans_ticks"] == ["GBPUSD_otc"]
    assert t["minutes_sans_battement"] == 29, "ticks de 00:31 à 01:00"
    assert t["statut"] == "PASS", "GBPUSD sortie du statut"
    natures = {n for _g, n, _v in r["anomalies"]["comptes"]}
    assert "paire sans aucun tick dans la fenêtre" in natures
    assert "ticks sans battement du collecteur" in natures
    assert any("GBPUSD" in x for x in r["limites"])


def test_les_gels_et_la_couverture_des_signaux(tmp_path):
    gels = (((T0 + 1500) * 1000, 15_000), ((T0 + 3000) * 1000, 40_000))
    conn = _base(tmp_path, _flux(minutes=120, gels=gels,
                                 trous={100}))
    signaux = {P: [
        SimpleNamespace(ts_sec=T0 + 1200, contexte={}),   # gel 15 s dedans
        SimpleNamespace(ts_sec=T0 + 4800, contexte={}),   # complète
        SimpleNamespace(ts_sec=T0 + 6000, contexte={}),   # minute 100 absente
        SimpleNamespace(ts_sec=T0 + 300, contexte={}),    # trop près du bord
        SimpleNamespace(ts_sec=T0 + 4860,
                        contexte={"mouvement_heure": -9}),  # non jouée
    ]}
    _a, r = _auditer(conn, signaux=signaux, minutes=120)
    s = r["controles"]["signaux"]
    assert s["signaux"] == [4, 2]
    assert s["joues"] == [3, 1]
    assert s["motifs"]["gel du flux de 10 s ou plus"] == 1
    assert s["motifs"]["minute(s) absente(s)"] == 1
    assert s["hors_fenetre"] == 1
    assert s["statut"] == "WARNING"
    natures = {(g, n) for g, n, _v in r["anomalies"]["comptes"]}
    assert ("INFO", "gel du flux") in natures, "15 s"
    assert ("WARNING", "gel du flux") in natures, "40 s"
    assert r["controles"]["horodatages"]["ecarts_ms"]["max"] >= 40_000


def _ordre(conn, prix, ouverture_s, decalage=HEURE_BROKER_MS + 350,
           brut="{}"):
    """Un ordre réel : les instants du broker avancent de deux heures."""
    o = (ouverture_s * 1000) + HEURE_BROKER_MS
    conn.execute(
        "INSERT INTO executions (campagne, pair, sens, mise, signal_ts_ms, "
        "clic_ts_ms, accepte_ts_ms, prix_attendu, prix_entree, prix_sortie, "
        "payout_flux_pct, expiration_sec, ouverture_ts_ms, expiration_ts_ms, "
        "decalage_broker_ms, accepte, brut) VALUES "
        "('c', ?, 'call', 1, ?, ?, ?, ?, ?, ?, 92, 60, ?, ?, ?, 1, ?)",
        (P, ouverture_s * 1000 - 2000, ouverture_s * 1000 - 1500,
         ouverture_s * 1000 - 500, prix, prix, prix, o, o + 60_000,
         decalage, brut))


def test_les_prix_du_broker_retrouves_dans_le_flux(tmp_path):
    flux = _flux(minutes=60)
    conn = _base(tmp_path, flux)
    par_instant = {t.ts_ms: t.price for t in flux}

    def prix_a(s):
        return par_instant[max(x for x in par_instant if x <= s * 1000)]

    for k in range(12):
        s = T0 + 120 + 240 * k
        _ordre(conn, prix_a(s), s)
    _ordre(conn, 1.2, T0 + 3000)                 # un prix qui n'existe pas
    conn.commit()
    _a, r = _auditer(conn)
    p = r["controles"]["prix"]
    assert p["ordres"] == 13
    assert p["ouverture"]["comparables"] == 13
    assert p["ouverture"]["trouves"][1] == 12
    assert p["synchronisation_ms"][1] == 350
    assert p["statut"] == "PASS", "12/13 > 90 %"
    assert ["WARNING", "prix d'ouverture absent du flux à ± 1 s", 1] in \
        r["anomalies"]["comptes"]


def test_lecture_seule_rien_n_est_ecrit(tmp_path):
    conn = _base(tmp_path, _flux())
    conn.close()
    chemin = tmp_path / "m.db"
    avant = hashlib.sha256(chemin.read_bytes()).hexdigest()
    lecture = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    _a, r = _auditer(lecture)
    with pytest.raises(sqlite3.OperationalError):
        lecture.execute("DELETE FROM candles")
    lecture.close()
    assert hashlib.sha256(chemin.read_bytes()).hexdigest() == avant
    assert all(q.lstrip().upper().startswith("SELECT")
               for q in at.REQUETES.values())


def test_l_outil_rend_les_memes_comptes(tmp_path, monkeypatch):
    from outils import audit_ticks as outil
    conn = _base(tmp_path, _flux())
    _a, r_bot = _auditer(conn)
    r_outil, _anomalies = outil.auditer(conn, T0, T0 + 3600, [P])
    for cle in ("horodatages", "concordance", "trous"):
        assert r_outil["statuts"][cle] == r_bot["statuts"][cle]
    assert (r_outil["reproductibilite"]["empreinte"]
            == r_bot["reproductibilite"]["empreinte"])


def test_le_rapport_et_la_fiche(tmp_path):
    conn = _base(tmp_path, _flux(trous=set(range(10, 45))))
    audit, r = _auditer(conn)
    t = texte(r, fichier=True)
    for nom in ("Horodatages", "Concordance M1", "Trous de collecte",
                "Couverture des signaux", "Prix d'exécution"):
        assert nom in t
    assert "❌ FAIL — Trous de collecte" in t
    assert "<code>SELECT pair, minute_sec" in t
    nu = re.sub(r"</?(b|pre|code)>", "", t)
    assert "<" not in nu and ">" not in nu and "&lt;" in nu
    fiche = csv_anomalies(audit.anomalies_triees()).decode()
    assert fiche.startswith("gravite;nature;paire;debut_utc")
    assert "interruption longue" in fiche
    assert "pas encore mesurée" in texte(None)
    assert "a échoué" in texte({"erreur": "boom"})


def test_un_chemin_illisible_est_une_anomalie_pas_une_panne(tmp_path):
    conn = _base(tmp_path, _flux())
    lecteur = MarketReader(conn)
    chemins = lecteur.tick_paths(P, T0, T0 + 3600)
    chemins[5] = replace(chemins[5], octets=b"pas du zlib")
    audit = nouvel_audit(T0, T0 + 3600)
    audit.ajouter_paire(P, lecteur.candles(P, 60, T0, T0 + 3600), chemins)
    r = audit.resultat()
    assert r["controles"]["horodatages"]["illisibles"] == 1
    assert r["statuts"]["horodatages"] == "FAIL"


def test_la_commande_est_au_menu():
    from maxprofit.hosting.telegram import COMMANDES
    assert "ticks" in [nom for nom, _ in COMMANDES]
