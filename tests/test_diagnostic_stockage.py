"""/stockage : le diagnostic du stockage et des payouts, en lecture seule
(étape 2, partie A). Le SQL est portable : les tests tournent sur SQLite."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from maxprofit.core.types import Candle
from maxprofit.hosting import stockage as hs
from maxprofit.store import diagnostic_stockage as ds
from maxprofit.store.db import open_read_write
from maxprofit.store.market import MarketWriter

JOUR = 86400
T0 = 1_792_800_000 // JOUR * JOUR            # un minuit UTC
MAINTENANT = T0 + 3 * JOUR + 3600


def _base(tmp_path):
    conn = open_read_write(tmp_path / "m.db")
    releves = [
        # A : 90, 90, 90, 85, 85, 90 — trois répétées, un retour A → B → A.
        ("A_otc", T0 + 0, 90, 1), ("A_otc", T0 + 300, 90, 1),
        ("A_otc", T0 + 600, 90, 1), ("A_otc", T0 + 900, 85, 1),
        ("A_otc", T0 + 1200, 85, 1), ("A_otc", T0 + 1500, 90, 1),
        # B : l'ouverture change seule, puis un relevé 30 s après le
        # précédent (deux collecteurs ?), puis plus rien pendant deux jours.
        ("B_otc", T0 + 0, 92, 1), ("B_otc", T0 + 300, 92, 0),
        ("B_otc", T0 + 330, 92, 0), ("B_otc", T0 + 2 * JOUR + 600, 80, 0),
    ]
    conn.executemany("INSERT INTO payouts (pair, ts_sec, payout_pct, is_open)"
                     " VALUES (?, ?, ?, ?)", releves)
    ecrivain = MarketWriter(conn)
    bougies = [Candle("A_otc", 60, T0 + j * JOUR + 60 * k, 1.1, 1.2, 1.0,
                      1.1, 10, True)
               for j in range(3) for k in range(j + 1)]
    ecrivain.upsert_candles(bougies)
    conn.commit()
    conn.close()
    return tmp_path / "m.db"


def _lecture(chemin):
    return lambda: sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)


def test_les_repetitions_et_les_categories(tmp_path):
    chemin = _base(tmp_path)
    p = ds.payouts(_lecture(chemin)(), MAINTENANT)
    assert p["lignes"] == 10 and p["paires"] == 2 and p["premiers"] == 2
    assert p["repetes"] == 4, "A : 3 ; B : le relevé 30 s après"
    assert p["changements"] == {"payout": 3, "ouverture": 1, "les_deux": 0}
    k = p["categories"]
    assert k["1_repetes_horodatages_differents"] == 4
    assert k["2_repetes_meme_horodatage"] == 0
    assert k["3_identiques_non_consecutifs"] == 1, "A revient à 90"
    assert k["4b_releves_a_moins_de_60s"] == 1 and k["4c_dont_repetes"] == 1
    assert k["5_validite_indeterminable"] == 0
    assert p["par_paire"][0] == ["A_otc", 6, 3]


def test_les_scenarios_sont_comptes_pas_appliques(tmp_path):
    chemin = _base(tmp_path)
    avant = hashlib.sha256(chemin.read_bytes()).hexdigest()
    p = ds.payouts(_lecture(chemin)(), MAINTENANT)
    s = p["scenarios_lignes"]
    assert s["changements_seuls"] == 4
    # Toutes les lignes tiennent dans une heure par paire, sauf le dernier
    # relevé de B : aucune heure n'est faite que de répétitions.
    assert s["ancrage_par_heure"] == 4 and s["ancrage_par_jour"] == 4
    assert hashlib.sha256(chemin.read_bytes()).hexdigest() == avant


def test_la_reconstruction_garde_la_valeur_et_perd_la_date(tmp_path):
    chemin = _base(tmp_path)
    r = ds.payouts(_lecture(chemin)(), MAINTENANT)["reconstruction"]
    assert r["valeurs_differentes"] == 0
    assert r["paires"] == 2
    assert r["derniere_ligne_repetee"] == 0, \
        "les deux paires finissent sur un changement"
    assert sum(r["age_du_releve_en_vigueur"].values()) == 10


def test_les_consommateurs_qui_comptent_les_lignes(tmp_path):
    chemin = _base(tmp_path)
    p = ds.payouts(_lecture(chemin)(), MAINTENANT)
    paires = {x[0]: x for x in p["classement"]["paires"]}
    assert paires["A_otc"][1] == 6 and paires["A_otc"][3] == 3, \
        "6 relevés tels quels, 3 en changements seuls"
    assert p["rejeu"]["ecarts_de_plus_d_un_jour"] == {
        "tel_quel": 1, "changements_seuls": 1}


def test_la_croissance_et_les_delais():
    mesures = {"base": 400 * 2 ** 20,
               "tables": {"candles": {"total": 1000}}}
    scenarios = {"bas": 10 * 2 ** 20, "central": 20 * 2 ** 20,
                 "haut": 40 * 2 ** 20}
    d = ds.delais(mesures, scenarios, 512)
    assert d["reste_octets"] == 112 * 2 ** 20
    assert d["haut"] == pytest.approx(2.8) and d["bas"] == pytest.approx(11.2)
    assert ds.delais(mesures, scenarios, None) == {}, "limite inconnue"


def test_la_croissance_par_jour_de_donnees(tmp_path):
    chemin = _base(tmp_path)
    c = ds.croissance(_lecture(chemin)(), {"tables": {}}, MAINTENANT)
    assert list(c["tables"]["candles"]["par_jour"].values()) == [1, 2, 3]
    assert c["tables"]["payouts"]["par_jour"][str(T0)] == 9
    assert c["scenarios"] == {}, "SQLite : pas de taille par table"


def test_la_croissance_physique_mesuree_en_memoire():
    e = ds.Echantillons()
    assert e.croissance(T0)["fenetres"] == {}
    e.noter(T0, {"base": 100, "tables": {"t": {"total": 10}}})
    e.noter(T0 + 25 * 3600, {"base": 160, "tables": {"t": {"total": 40}}})
    f = e.croissance(T0 + 25 * 3600)["fenetres"]
    assert f["24"]["base"] == 60 and f["24"]["tables"]["t"] == 30
    assert "48" not in f


def test_le_rapport_est_en_lecture_seule_et_etiquete(tmp_path, monkeypatch):
    chemin = _base(tmp_path)
    avant = hashlib.sha256(chemin.read_bytes()).hexdigest()
    monkeypatch.delenv(hs.ENV_QUOTA, raising=False)
    d = hs.mesurer(chemin, _lecture(chemin), MAINTENANT)
    t = hs.texte(d)
    assert hashlib.sha256(chemin.read_bytes()).hexdigest() == avant
    for morceau in ("[Vérifié", "[Probable", "[Inconnu", "Scénarios",
                    "JAMAIS", "Reconstruction", "rejeu_plan", "512 Mo"):
        assert morceau in t, morceau
    lecture = _lecture(chemin)()
    with pytest.raises(sqlite3.OperationalError):
        lecture.execute("DELETE FROM payouts")


def test_le_texte_avec_les_tailles_de_postgresql():
    mo = 2 ** 20
    d = {
        "mesure_le": MAINTENANT, "duree_sec": 12.0,
        "tailles": {"moteur": "postgresql", "base": 416 * mo,
                    "toutes_bases": 424 * mo, "limite_mo": 512,
                    "source_limite": "neon.max_cluster_size",
                    "taille_neon": None, "tables": {
                        "payouts": {"table": 60 * mo, "index": 40 * mo,
                                    "toast": 0, "total": 102 * mo,
                                    "autres": 2 * mo,
                                    "lignes_estimees": 700000}}},
        "croissance": {"jours": [str(T0)], "octets_par_jour": [20 * mo],
                       "tables": {"payouts": {
                           "lignes": 700000, "octets_par_ligne": 150.0,
                           "par_jour": {str(T0): 15000},
                           "aujourd_hui": 600}},
                       "scenarios": {"bas": 20 * mo, "central": 20 * mo,
                                     "haut": 20 * mo}},
        "payouts": {"erreur": "interrompu"},
        "physique": {"depuis_sec": 0, "fenetres": {}},
    }
    t = hs.texte(d, {"base": 410 * mo, "limite_mo": 512})
    assert "neon.max_cluster_size" in t and "Relève : 410.0 Mo" in t
    assert "Jours avant saturation" in t and "4.8 j" in t
    assert "Mesure impossible" in t


def test_la_commande_est_au_menu():
    from maxprofit.hosting.telegram import COMMANDES
    assert "stockage" in [nom for nom, _ in COMMANDES]
