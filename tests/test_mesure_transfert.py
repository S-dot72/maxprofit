"""
La mesure passive du transfert (`store.mesure_transfert`, `/transfert`) :
- elle ne change aucun résultat, et une panne de comptage ne gêne jamais une
  lecture ;
- elle estime les octets du format texte de PostgreSQL ;
- elle attribue chaque lecture à la fonction du projet qui l'a lancée ;
- elle compte les journées relues.
"""

from __future__ import annotations

import pytest

from maxprofit.store import mesure_transfert as mt
from maxprofit.store.postgres import Connexion

M0 = 1_792_454_400          # 20/10/2026 00:00 UTC


class _CurseurFactice:
    def __init__(self, lignes):
        self._lignes = list(lignes)
        self.rowcount = len(self._lignes)

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self._lignes.pop(0) if self._lignes else None

    def fetchall(self):
        lignes, self._lignes = self._lignes, []
        return lignes

    def __iter__(self):
        return iter(self.fetchall())


class _ConnexionFactice:
    """Un psycopg qui rend des lignes préparées, dans l'ordre."""

    def __init__(self, *reponses):
        self.reponses = list(reponses)
        self.closed = False

    def cursor(self):
        return _CurseurFactice(self.reponses.pop(0) if self.reponses else [])

    def rollback(self):
        pass


@pytest.fixture
def mesure(monkeypatch):
    m = mt.Mesure()
    monkeypatch.setattr(mt, "MESURE", m)
    return m


def _chemins(n: int, debut: int = M0, octets: bytes = b"\x01" * 400):
    return [("EURUSD_otc", debut + 60 * i, 120, 5, octets) for i in range(n)]


def test_les_octets_suivent_le_format_texte_de_postgresql():
    assert mt.octets_valeur(None) == 0
    assert mt.octets_valeur(b"\x00" * 400) == 802, "BYTEA en hexadécimal"
    assert mt.octets_valeur("EURUSD_otc") == 10
    assert mt.octets_valeur("é") == 2
    assert mt.octets_valeur(1792454400) == 10
    assert mt.octets_valeur(1.08231) == 7
    assert mt.octets_valeur(True) == 1
    assert mt.octets_ligne(("a", 1)) == 7 + 2 * 4 + 1 + 1


def test_les_bornes_d_une_lecture():
    assert mt.bornes(("EURUSD_otc", M0, M0 + 900)) == (
        (M0, M0 + 900), "EURUSD_otc")
    assert mt.bornes((M0 * 1000, M0 * 1000 + 5000)) == ((M0, M0 + 5), None)
    assert mt.bornes(("EURUSD_otc", 60, M0)) is None
    assert mt.bornes(()) is None


def test_une_lecture_de_chemins_est_attribuee_et_ses_journees_comptees(
        mesure):
    from maxprofit.store.market import MarketReader
    conn = Connexion(_ConnexionFactice(_chemins(3), _chemins(3)))
    lecteur = MarketReader(conn)
    premiers = lecteur.tick_paths("EURUSD_otc", M0, M0 + 180)
    seconds = lecteur.tick_paths("EURUSD_otc", M0, M0 + 180)
    assert [c.minute_sec for c in premiers] == [M0, M0 + 60, M0 + 120]
    assert premiers == seconds, "la mesure ne change aucun résultat"
    m = mesure.instantane()
    ((cle, (req, lignes, octets)),) = m["par_cle"].items()
    base, conso, operation, table = cle
    assert (operation, table, req, lignes) == ("market.tick_paths",
                                               "tick_paths", 2, 6)
    assert conso == "hors du projet"           # appelé depuis un test
    attendu = 2 * 3 * (7 + 5 * 4 + len("EURUSD_otc") + 10 + 3 + 1 + 802)
    assert octets == attendu
    ((_, relues),) = m["relues"].items()
    assert relues["paires_jours"] == 1 and relues["relectures"] == 1
    assert relues["octets_relus"] == pytest.approx(attendu / 2)
    ((_, portees),) = m["portees"].items()
    assert portees == {"fenêtre < 1 h": attendu}


def test_le_consommateur_est_la_fonction_du_projet(mesure):
    from maxprofit.live.etude3 import premiere_minute_de_ticks
    conn = Connexion(_ConnexionFactice([(M0,)]))
    assert premiere_minute_de_ticks(conn) == M0
    ((cle, (req, lignes, _)),) = mesure.instantane()["par_cle"].items()
    assert cle[1] == "live/etude3.py:premiere_minute_de_ticks"
    assert cle[2:] == ("SELECT tick_paths", "tick_paths")
    assert (req, lignes) == (1, 1)


def test_les_ecritures_ne_sont_pas_comptees(mesure):
    conn = Connexion(_ConnexionFactice([]))
    conn.execute("INSERT INTO uptime (ts_sec, n_pairs) VALUES (?, ?)",
                 (M0, 1))
    assert mesure.instantane()["par_cle"] == {}


def test_une_panne_de_comptage_ne_gene_pas_la_lecture(mesure, monkeypatch):
    def panne(_):
        raise RuntimeError("compteur cassé")
    monkeypatch.setattr(mt, "octets_ligne", panne)
    conn = Connexion(_ConnexionFactice(_chemins(2)))
    lignes = conn.execute("SELECT * FROM tick_paths").fetchall()
    assert len(lignes) == 2
    assert not mesure.actif and "compteur cassé" in mesure.erreur
    assert len(Connexion(_ConnexionFactice(_chemins(2))).execute(
        "SELECT * FROM tick_paths").fetchall()) == 2


def test_l_iteration_est_comptee_comme_les_autres(mesure):
    conn = Connexion(_ConnexionFactice(_chemins(4)))
    assert sum(1 for _ in conn.execute("SELECT * FROM tick_paths")) == 4
    ((_, (req, lignes, _)),) = mesure.instantane()["par_cle"].items()
    assert (req, lignes) == (1, 4)


def test_la_base_est_nommee_sans_son_adresse(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://a")
    monkeypatch.setenv("DATABASE_URL_RELEVE", "postgresql://b")
    assert mt.nom_de_base("postgresql://a") == "active"
    assert mt.nom_de_base("postgresql://b") == "relève"
    assert mt.nom_de_base("postgresql://c") == "autre"


def test_le_rapport_separe_mesure_et_extrapolation(mesure, monkeypatch):
    from maxprofit.hosting import transfert
    from maxprofit.store.market import MarketReader
    MarketReader(Connexion(_ConnexionFactice(_chemins(5)))).tick_paths(
        "EURUSD_otc", M0, M0 + 300)
    monkeypatch.setattr(transfert, "SERVEUR", [
        {"ts": 0, "lignes": {"tick_paths": 100}, "remise": "x"},
        {"ts": 3600, "lignes": {"tick_paths": 160}, "remise": "x"}])
    texte, fiches = transfert.rapport(None, mesure.instantane())
    for morceau in ("1. Total mesuré", "2. Par consommateur",
                    "3. Journées relues", "4. Portée", "5. Par table",
                    "6. Côté serveur", "7. Extrapolation", "(pas une mesure)",
                    "Incertitudes", "market.tick_paths", "tick_paths 60 lignes"):
        assert morceau in texte, morceau
    assert fiches[0][1] == "transfert.csv"
    assert fiches[0][0].decode().startswith("base;consommateur;operation")
    monkeypatch.setattr(transfert, "SERVEUR", [
        {"ts": 0, "lignes": {}, "remise": "x"},
        {"ts": 60, "lignes": {}, "remise": "y"}])
    assert "remises à zéro" in transfert.rapport(None,
                                                  mesure.instantane())[0]
