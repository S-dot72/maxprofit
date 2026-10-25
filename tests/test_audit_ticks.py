"""L'étape 0 des ticks (/ticks) : chaque contrôle, en lecture seule
(cahier des charges v1.0 ; méthode : docs/etape0_ticks.md)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from maxprofit.apprentissage import audit_ticks as at
from maxprofit.apprentissage import microstructure as ms
from maxprofit.collect.collector import CandleAggregator
from maxprofit.core.types import Tick
from maxprofit.live.ticks import (
    fichiers, lire_battements, lire_ordres, nouvel_audit, texte)
from maxprofit.store.chemin_ticks import decoder, encoder
from maxprofit.store.db import open_read_write
from maxprofit.store.market import MarketReader, MarketWriter

T0 = 1_792_800_000 // 3600 * 3600          # un début d'heure (00:00 UTC)
P = "EURUSD_otc"
HEURE_BROKER_MS = 2 * 3_600_000
NT = "NOT TESTABLE"


def _flux(pair=P, debut=T0, minutes=60, pas_ms=450, base=1.08000,
          trous=(), seconde=False, gels=()):
    """Des ticks réguliers ; `trous` : minutes sans ticks ; `gels` :
    (début_ms, durée_ms) sans ticks ; `seconde` : horodatages arrondis à la
    seconde."""
    ticks, prix, ms_ = [], base, debut * 1000
    k = 0
    while ms_ < (debut + 60 * minutes) * 1000:
        minute = (ms_ // 1000 - debut) // 60
        gele = any(g <= ms_ < g + d for g, d in gels)
        if minute not in trous and not gele:
            k += 1
            prix = round(prix + (0.00001 if k % 3 else -0.00002), 5)
            ts = ms_ // 1000 * 1000 if seconde else ms_ + (k % 7)
            ticks.append(Tick(pair=pair, ts_ms=ts, price=prix))
        ms_ += pas_ms
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


def _auditer(conn, paires=(P,), signaux=None, minutes=60, ordres=True,
             audit=None):
    lecteur = MarketReader(conn)
    audit = audit or nouvel_audit(
        T0, T0 + 60 * minutes,
        joue=lambda e: e.contexte.get("mouvement_heure", 0) >= -3.3)
    audit.noter_battements(lire_battements(conn, audit.debut, audit.fin))
    for p in paires:
        bougies = lecteur.candles(p, 60, T0 - 3600, T0 + 60 * minutes)
        audit.ajouter_paire(p, bougies,
                            lecteur.tick_paths(p, audit.debut, audit.fin),
                            (signaux or {}).get(p, ()),
                            relire=lecteur.ticks)
    if ordres:
        audit.comparer_ordres(lire_ordres(conn), lecteur.ticks)
    return audit, audit.resultat(commit="test")


def _comptes(r):
    return {(g, n): v for g, n, _c, v in r["anomalies"]["comptes"]}


# --------------------------------------------------------------------------- #
# Phase B : qualité
# --------------------------------------------------------------------------- #

def test_des_donnees_saines_passent(tmp_path):
    conn = _base(tmp_path, _flux())
    _a, r = _auditer(conn)
    s = r["statuts"]
    assert s["horodatages"] == "PASS"
    assert s["compression"] == "PASS"
    assert s["concordance"] == "PASS"
    assert s["trous"] == "PASS"
    assert s["ticks_tardifs"] == NT and s["heure_reception"] == NT
    # Rien d'assez long pour un signal, aucun ordre : non testables.
    assert s["signaux"] == NT and s["prix"] == NT
    h, z = r["controles"]["horodatages"], r["controles"]["compression"]
    assert h["granularite_ms"] == 1 and h["hors_minute"] == 0
    assert z["reencodees"] == [6, 0], "une minute sur dix"
    assert z["relectures"][0] == 1 and z["relectures"][2] == 0
    assert r["controles"]["concordance"]["classes"] == {"identique": 60}
    assert r["paires"][P]["couverture"] == 1.0
    assert r["jours"][str(T0)]["attendues"] == 60, "jour du bord : partiel"
    assert not [k for k in _comptes(r) if k[0] == "FAIL"]
    assert any("Latence du flux" in x for x in r["inventaire"]["impossibles"])
    json.dumps(r)                          # sauvable avec l'état


def test_des_horodatages_a_la_seconde_echouent(tmp_path):
    conn = _base(tmp_path, _flux(minutes=60, seconde=True))
    _a, r = _auditer(conn)
    assert r["controles"]["horodatages"]["granularite_ms"] == 1000
    assert r["statuts"]["horodatages"] == "FAIL"
    assert any("SECONDE" in x for x in r["limites"])
    assert r["controles"]["horodatages"]["doublons_instant"] > 0


def test_la_compression_est_demontree_ou_mise_en_defaut(tmp_path):
    conn = _base(tmp_path, _flux())
    lecteur = MarketReader(conn)
    # Un encodeur qui ne rend pas les mêmes octets : réencodage différent.
    faux = nouvel_audit(T0, T0 + 3600)
    faux.encoder = lambda ticks: encoder([replace(ticks[0], price=2.0)]
                                         + list(ticks[1:]))
    faux.ajouter_paire(P, lecteur.candles(P, 60, T0, T0 + 3600),
                       lecteur.tick_paths(P, T0, T0 + 3600))
    r = faux.resultat()
    assert r["controles"]["compression"]["reencodees"] == [6, 6]
    assert r["statuts"]["compression"] == "FAIL"
    # Une relecture qui perd un tick : relecture différente.
    perte = nouvel_audit(T0, T0 + 3600)
    perte.ajouter_paire(P, lecteur.candles(P, 60, T0, T0 + 3600),
                        lecteur.tick_paths(P, T0, T0 + 3600),
                        relire=lambda p, a, b: lecteur.ticks(p, a, b)[1:])
    r = perte.resultat()
    assert r["controles"]["compression"]["relectures"][2] == 1
    assert ("FAIL", "relecture différente du décodage") in _comptes(r)


def test_les_ecarts_de_concordance_explicables_et_les_autres(tmp_path):
    conn = _base(tmp_path, _flux())
    m = T0 + 600
    conn.execute("UPDATE candles SET high = high + 0.001 WHERE pair = ? AND "
                 "ts_sec = ?", (P, m))                       # inexplicable
    conn.execute("UPDATE candles SET complete = 0, tick_count = tick_count "
                 "+ 1 WHERE pair = ? AND ts_sec = ?", (P, m + 60))
    coupe = [t for t in _flux() if (m + 120) * 1000 <= t.ts_ms
             < (m + 140) * 1000]
    conn.execute("DELETE FROM tick_paths WHERE pair = ? AND minute_sec = ?",
                 (P, m + 120))
    MarketWriter(conn).insert_tick_paths([encoder(coupe)])  # minute coupée
    conn.commit()
    _a, r = _auditer(conn)
    k = r["controles"]["concordance"]
    assert k["classes"]["écart inexpliqué"] == 1
    assert k["classes"]["bougie incomplète"] == 1
    assert k["classes"]["minute coupée"] == 1
    assert k["champs_inexpliques"] == {"h": 1}
    assert k["exemples"]["écart inexpliqué"] == [[P, m]]
    assert r["statuts"]["concordance"] == "FAIL", "1 sur 60 > 0,1 %"
    premiere = r["anomalies"]["premieres"][0]
    assert premiere["gravite"] == "FAIL" and premiere["id"] == "A00001"
    assert premiere["controle"] == "concordance"
    assert premiere["nature"] == "concordance M1 inexpliquée"
    assert premiere["debut"] == m and premiere["duree_sec"] == 60
    assert premiere["critere"] and premiere["impact"] and \
        premiere["confiance"] == "élevée"


def test_un_decalage_de_minute_est_reconnu(tmp_path):
    conn = _base(tmp_path, _flux())
    lignes = conn.execute(
        "SELECT ts_sec, open, high, low, close, tick_count FROM candles "
        "WHERE pair = ? ORDER BY ts_sec", (P,)).fetchall()
    # Les bougies de 00:20 à 00:29 décalées d'une minute vers le futur.
    for ts, o, h, l, c, n in lignes:
        if T0 + 1200 <= ts < T0 + 1800:
            conn.execute("UPDATE candles SET open=?, high=?, low=?, close=?, "
                         "tick_count=? WHERE pair=? AND ts_sec=?",
                         (o, h, l, c, n, P, ts + 60))
    conn.commit()
    _a, r = _auditer(conn)
    k = r["controles"]["concordance"]
    assert k["classes"]["décalage de minute"] >= 9
    assert ("FAIL", "décalage de minute") in _comptes(r)


def test_les_trous_selon_que_le_collecteur_battait_ou_non(tmp_path):
    trous = set(range(10, 45))                      # 35 min sans ticks
    conn = _base(tmp_path, _flux(trous=trous))
    _a, r = _auditer(conn)
    t = r["controles"]["trous"]
    assert t["manquantes_ecoutees"] == 35 and t["manquantes_muettes"] == 0
    assert r["statuts"]["trous"] == "FAIL"
    assert t["interruptions_longues"] == 1 and t["interruptions_min"] == 35
    assert t["principales_interruptions"][0][1] == T0 + 600
    assert _comptes(r)[("WARNING", "interruption longue")] == 1
    assert r["paires"][P]["interruptions"] == [1, 35]
    muet = _base(tmp_path, _flux(trous=trous), battements=False, nom="n.db")
    _a, r = _auditer(muet)
    assert r["statuts"]["trous"] == NT
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
    natures = {n for _g, n in _comptes(r)}
    assert "paire sans aucun tick dans la fenêtre" in natures
    assert "ticks sans battement du collecteur" in natures
    assert any("GBPUSD" in x for x in r["limites"])


# --------------------------------------------------------------------------- #
# Phase D : signaux
# --------------------------------------------------------------------------- #

def _signal(ts, **ctx):
    return SimpleNamespace(ts_sec=ts, contexte=ctx, issues={})


def test_chaque_signal_a_un_statut_et_un_code(tmp_path):
    gels = (((T0 + 1500) * 1000, 15_000),)
    conn = _base(tmp_path, _flux(minutes=180, gels=gels, trous={100}))
    signaux = {P: [
        _signal(T0 + 1200),                         # gel de 15 s dedans
        _signal(T0 + 4800),                         # complète
        _signal(T0 + 6000),                         # minute 100 absente
        _signal(T0 + 300),                          # trop près du bord
        _signal(T0 + 4860, mouvement_heure=-9),     # complète, non jouée
    ], "GBPUSD_otc": [_signal(T0 + 4800)]}           # aucune minute
    audit, r = _auditer(conn, paires=(P, "GBPUSD_otc"), signaux=signaux,
                        minutes=180)
    s = r["controles"]["signaux"]
    assert s["signaux"] == [5, 2]
    assert s["joues"] == [4, 1]
    assert s["codes"]["FEED_FREEZE"] == 1
    assert s["codes"]["INTERNAL_GAP"] == 1
    assert s["codes"]["NO_TICKS"] == 1
    assert s["hors_fenetre"] == 1
    assert s["statut"] == "FAIL", "2/5 < 50 %"
    fiche = next(f for f in audit.fiches_signaux if f["ts"] == T0 + 6000)
    assert fiche["id"] == f"{P}|{T0 + 6000}"
    assert fiche["minutes"] == 29 and fiche["code"] == "INTERNAL_GAP"
    assert fiche["debut"] == T0 + 6000 - 900
    csv = at.csv_signaux(audit.fiches_signaux).decode()
    assert csv.startswith("id;paire;decision_utc")
    assert "gagne" not in csv, "l'issue n'entre pas dans le statut"


def test_une_minute_illisible_est_une_anomalie_pas_une_panne(tmp_path):
    conn = _base(tmp_path, _flux(minutes=120))
    lecteur = MarketReader(conn)
    chemins = lecteur.tick_paths(P, T0, T0 + 7200)
    chemins[50] = replace(chemins[50], octets=b"pas du zlib")
    audit = nouvel_audit(T0, T0 + 7200)
    audit.ajouter_paire(P, lecteur.candles(P, 60, T0, T0 + 7200), chemins,
                        [_signal(T0 + 3000)])
    r = audit.resultat()
    assert r["controles"]["compression"]["illisibles"] == 1
    assert r["statuts"]["compression"] == "FAIL"
    assert r["controles"]["signaux"]["codes"]["DECOMPRESSION_ERROR"] == 1


def test_une_fenetre_sans_battement_a_un_horodatage_incertain(tmp_path):
    conn = _base(tmp_path, _flux(minutes=120), battements=False)
    for s in range(T0 - 60, T0 + 2400, 30):         # battements jusqu'à 00:40
        MarketWriter(conn).heartbeat(s, 1)
    conn.commit()
    _a, r = _auditer(conn, signaux={P: [_signal(T0 + 3000)]}, minutes=120)
    assert r["controles"]["signaux"]["codes"]["TIMESTAMP_UNCERTAIN"] == 1


# --------------------------------------------------------------------------- #
# Prix d'exécution
# --------------------------------------------------------------------------- #

def _ordre(conn, prix, ouverture_s, sens="call", accepte=1,
           decalage=HEURE_BROKER_MS + 350, brut="{}"):
    """Un ordre réel : les instants du broker avancent de deux heures."""
    o = (ouverture_s * 1000) + HEURE_BROKER_MS
    conn.execute(
        "INSERT INTO executions (campagne, pair, sens, mise, signal_ts_ms, "
        "clic_ts_ms, accepte_ts_ms, prix_attendu, prix_entree, prix_sortie, "
        "payout_flux_pct, expiration_sec, ouverture_ts_ms, expiration_ts_ms, "
        "decalage_broker_ms, accepte, brut) VALUES "
        "('c', ?, ?, 1, ?, ?, ?, ?, ?, ?, 92, 60, ?, ?, ?, ?, ?)",
        (P, sens, ouverture_s * 1000 - 2000, ouverture_s * 1000 - 1500,
         ouverture_s * 1000 - 500, prix, prix, prix, o, o + 60_000,
         decalage, accepte, brut))


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
    _ordre(conn, prix_a(T0 + 3100), T0 + 3100, accepte=0)      # refusé
    conn.commit()
    audit, r = _auditer(conn)
    p = r["controles"]["prix"]
    assert p["ordres"] == 14
    assert p["exclus"] == {"refusé par le broker": 1}
    assert p["ouverture"]["comparables"] == 13
    assert p["ouverture"]["trouves"][1] == 12
    assert p["synchronisation_ms"][1] == 350
    assert p["statut"] == "PASS", "12/13 > 90 %"
    assert p["niveau"] == "descriptif", "13 < 30 ordres"
    assert _comptes(r)[("WARNING",
                        "prix d'ouverture absent du flux à ± 1 s")] == 1
    d = p["delais_ms"]
    assert d["signal_clic"][1] == 500 and d["clic_acceptation"][1] == 1000
    assert d["acceptation_ouverture"][1] == 150, "500 − 350 de décalage"
    assert "ordres_ticks.csv" in [nom for _c, nom, _l in fichiers(audit)]


def test_la_convention_de_signe_des_ecarts(tmp_path):
    """Prix obtenu − référence ; coût = l'écart pour un CALL, son opposé pour
    un PUT : payer plus cher un CALL est défavorable, vendre plus cher un
    PUT est favorable."""
    flux = _flux(minutes=60)
    conn = _base(tmp_path, flux)
    lecteur = MarketReader(conn)
    s = T0 + 600
    ref = [t for t in lecteur.ticks(P, s - 10, s + 1)
           if t.ts_ms <= s * 1000 + 350 - 2000][-1].price
    _ordre(conn, round(ref + 0.00003, 5), s, sens="call")
    _ordre(conn, round(ref + 0.00003, 5), s, sens="put")
    conn.commit()
    audit, _r = _auditer(conn)
    call, put = audit.fiches_ordres
    assert call["ecarts"]["signal"] == {"ecart": 3, "cout": 3}
    assert put["ecarts"]["signal"] == {"ecart": 3, "cout": -3}


# --------------------------------------------------------------------------- #
# Phase F : exploratoire
# --------------------------------------------------------------------------- #

def test_une_trajectoire_au_tick_est_decrite_sans_seuil():
    d = 1_000_000_000
    entree, niveau, amp = 1.0000, 0.9995, 0.0005
    chemin = [(d + 60_000, 1.0010), (d + 300_000, 0.9990),
              (d + 420_000, 1.0002), (d + 880_000, 0.9998)]
    t = ms.trajectoire(chemin, d, entree, True, amp, niveau)
    assert t["issue_240"] is True and t["issue_600"] is True
    assert t["issue_900"] is False
    assert t["mfe"] == 2.0 and t["mae"] == 2.0
    assert t["t_mfe"] == 60 and t["t_mae"] == 300
    assert t["a_ete_gagnant"] is True
    assert t["dernier_passage"] == 880, "passé perdant à la fin"
    assert t["franchi"] is True and t["n_franchissements"] == 1
    assert t["profondeur"] == 1.0 and t["retour"] is True
    assert t["delai_retour"] == 120 and t["temps_au_dela"] == 120
    assert t["pos_60"] == 0.0 and t["pos_120"] == 2.0
    assert abs(ms.arcsinus_fin(60) - 0.165) < 0.002


def test_les_comparaisons_exploratoires_ont_leurs_effectifs():
    fiches = []
    for i in range(40):
        gagne = i % 2 == 0
        fiches.append({
            "paire": f"P{i % 8}", "jour": i % 5,
            "issue_240": gagne, "issue_600": not gagne, "issue_900": gagne,
            "issues_bougies": {"240": gagne, "600": not gagne,
                               "900": gagne},
            "mfe": 2.0 if gagne else 0.5, "mae": 0.5, "t_mfe": 600.0,
            "t_mae": 60.0, "a_ete_gagnant": True,
            "dernier_passage": 870.0 if not gagne else 100.0,
            **{f"pos_{s}": 0.0 for s in ms.POSITIONS_SEC},
            "franchi": not gagne, "profondeur": 1.0 if not gagne else None,
            "temps_au_dela": 30.0 if not gagne else None,
            "retour": False if not gagne else None, "delai_retour": None,
            "n_franchissements": int(not gagne)})
    temoins = [{"paire": "T", "jour": i % 6, "issue_900": False,
                "dernier_passage": 500.0} for i in range(30)]
    x = ms.resumer(fiches, temoins)
    assert x["accord"]["900"] == [40, 40]
    assert x["echeances"]["communs"] == 40
    assert x["echeances"]["240"] == [40, 20]
    assert x["echeances"]["600-900"]["ecart_ev"] == 0.0
    assert x["fin_echeance"]["60"]["signal"] == 1.0
    assert x["fin_echeance"]["60"]["temoin"] == 0.0
    f = x["franchissements"]
    assert f["perdants"]["franchis"] == 20 and f["gagnants"]["franchis"] == 0
    assert f["franchi_perdants_moins_gagnants"]["ecart"] == 1.0
    assert x["trajectoires"]["mfe_gagnants_moins_perdants"]["ecart"] == 1.5


def test_les_signaux_complets_sont_suivis_au_tick(tmp_path):
    conn = _base(tmp_path, _flux(minutes=120))
    sig = SimpleNamespace(ts_sec=T0 + 3000, contexte={
        "sens_call": 1.0, "niveau": 1.0799, "prix_signal": None},
        issues={240: True, 600: False, 900: True})
    audit, r = _auditer(conn, signaux={P: [sig]}, minutes=120)
    x = r["exploration"]
    assert x["signaux"] == 1 and x["temoins"] > 0
    t = audit.trajectoires[0]
    assert t["id"] == f"{P}|{T0 + 3000}"
    assert set(x["accord"]) == {"240", "600", "900"}


# --------------------------------------------------------------------------- #
# Lecture seule, reproductibilité, rapport
# --------------------------------------------------------------------------- #

def test_lecture_seule_rien_n_est_ecrit(tmp_path):
    conn = _base(tmp_path, _flux(minutes=120))
    _ordre(conn, 1.08, T0 + 600)
    conn.commit()
    conn.close()
    chemin = tmp_path / "m.db"
    avant = hashlib.sha256(chemin.read_bytes()).hexdigest()
    lecture = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    _a, r = _auditer(lecture, signaux={P: [_signal(T0 + 3000)]},
                     minutes=120)
    with pytest.raises(sqlite3.OperationalError):
        lecture.execute("DELETE FROM candles")
    lecture.close()
    assert hashlib.sha256(chemin.read_bytes()).hexdigest() == avant
    assert all(q.lstrip().upper().startswith("SELECT")
               for q in at.REQUETES.values())


def test_l_outil_rend_les_memes_comptes(tmp_path):
    from outils import audit_ticks as outil
    conn = _base(tmp_path, _flux())
    _a, r_bot = _auditer(conn)
    r_outil, audit = outil.auditer(conn, T0, T0 + 3600, [P])
    for cle in ("horodatages", "compression", "concordance", "trous"):
        assert r_outil["statuts"][cle] == r_bot["statuts"][cle]
    assert (r_outil["reproductibilite"]["empreinte"]
            == r_bot["reproductibilite"]["empreinte"])
    assert len(fichiers(audit)) == 3


def test_le_rapport_et_les_fiches(tmp_path):
    conn = _base(tmp_path, _flux(minutes=120, trous=set(range(70, 105))))
    sig = SimpleNamespace(ts_sec=T0 + 2400, contexte={
        "sens_call": 0.0, "niveau": 1.0801}, issues={900: False})
    audit, r = _auditer(conn, signaux={P: [sig]}, minutes=120)
    t = texte(r, fichier=True)
    for nom in ("Horodatages et continuité", "Compression et relecture",
                "Concordance M1", "Trous de collecte",
                "Couverture des signaux ZoneH1", "Prix d'exécution",
                "Ticks tardifs", "Heure de réception", "Inventaire des "
                "analyses", "Exploratoire", "[Fait observé]",
                "[Conclusion non établie]", "loi de l'arcsinus"):
        assert nom in t, nom
    assert "❌ FAIL — Trous de collecte" in t
    assert "❔ NOT TESTABLE — Ticks tardifs" in t
    assert "<code>SELECT pair, minute_sec" in t
    nu = re.sub(r"</?(b|pre|code)>", "", t)
    assert "<" not in nu and ">" not in nu and "&lt;" in nu
    noms = [nom for _c, nom, _l in fichiers(audit)]
    assert noms == ["anomalies_ticks.csv", "signaux_ticks.csv",
                    "ordres_ticks.csv"]
    registre = fichiers(audit)[0][0].decode()
    assert registre.startswith("id;gravite;controle;nature;paire;debut_utc")
    assert "interruption longue" in registre
    assert "pas encore mesurée" in texte(None)
    assert "a échoué" in texte({"erreur": "boom"})
    assert "version précédente" in texte({"controles": {"trous": {}}})


def test_la_commande_est_au_menu():
    from maxprofit.hosting.telegram import COMMANDES
    assert "ticks" in [nom for nom, _ in COMMANDES]


def test_le_decodeur_est_celui_du_stockage():
    audit = nouvel_audit(T0, T0 + 60)
    assert audit.decoder is decoder and audit.encoder is encoder
