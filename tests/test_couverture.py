"""
Correction A du transfert (29/10) : la sonde `/health` ne relit plus toute
la table des battements.
- la couverture calculée par le serveur rend EXACTEMENT ce que rendait
  l'ancien calcul en Python, sur des séries tirées au hasard et sur les cas
  limites ;
- elle ne rapatrie qu'une ligne d'agrégats ;
- les compteurs de la sonde sont gardés 30 minutes, leur âge est publié, et
  le battement reste relu à chaque sonde.
"""

from __future__ import annotations

import random

import pytest

from maxprofit.hosting import health
from maxprofit.store.db import open_read_write
from maxprofit.store.market import MarketReader, MarketWriter

T0 = 1_792_454_400


def _ancien_calcul(battements, fenetre_sec=None, trou_max_sec=90,
                   maintenant=0) -> dict:
    """L'algorithme d'avant le 29/10, recopié tel quel : la référence."""
    battements = sorted(battements)
    vide = {"fenetre_sec": 0, "collecte_sec": 0, "part": 0.0,
            "interruptions": 0, "plus_long_trou_sec": 0}
    if not battements:
        return vide
    debut = (maintenant - fenetre_sec if fenetre_sec else battements[0])
    dans = [t for t in battements if t >= debut]
    fenetre = maintenant - debut
    if fenetre <= 0 or not dans:
        return vide
    trous = [(b - a) for a, b in zip(dans, dans[1:]) if b - a > trou_max_sec]
    if dans[0] - debut > trou_max_sec:
        trous.append(dans[0] - debut)
    if maintenant - dans[-1] > trou_max_sec:
        trous.append(maintenant - dans[-1])
    collecte = max(0, fenetre - sum(trous))
    continue_sec = maintenant - dans[-1] if maintenant > dans[-1] else 0
    if maintenant - dans[-1] <= trou_max_sec:
        derniere_reprise = dans[0]
        for a, b in zip(dans, dans[1:]):
            if b - a > trou_max_sec:
                derniere_reprise = b
        continue_sec = maintenant - derniere_reprise
    return {"fenetre_sec": fenetre, "collecte_sec": collecte,
            "part": collecte / fenetre, "interruptions": len(trous),
            "plus_long_trou_sec": max(trous) if trous else 0,
            "continue_depuis_sec": continue_sec,
            "en_cours": maintenant - dans[-1] <= trou_max_sec}


def _base(tmp_path, battements):
    conn = open_read_write(tmp_path / "b.db")
    ecrivain = MarketWriter(conn)
    for s in battements:
        ecrivain.heartbeat(s, 1)
    conn.commit()
    return conn


def _serie(alea: random.Random) -> list[int]:
    """Des battements toutes les 30 s, avec des trous de toutes tailles."""
    t, sortie = T0 + alea.randrange(0, 600), []
    for _ in range(alea.randrange(0, 400)):
        sortie.append(t)
        t += alea.choice((30, 30, 30, 30, 31, 60, 95, 300, 3600, 20_000))
    return sortie


@pytest.mark.parametrize("graine", range(25))
def test_la_couverture_du_serveur_est_celle_de_l_ancien_calcul(tmp_path,
                                                                graine):
    alea = random.Random(graine)
    battements = _serie(alea)
    conn = _base(tmp_path, battements)
    try:
        lecteur = MarketReader(conn)
        fin = (battements[-1] if battements else T0)
        for maintenant in (fin, fin + 45, fin + 200, fin + 90_000,
                           T0 + 10, T0 - 100):
            for fenetre in (None, 0, 3600, 24 * 3600, 600):
                for trou in (90, 40):
                    attendu = _ancien_calcul(battements, fenetre, trou,
                                             maintenant)
                    obtenu = lecteur.couverture(fenetre_sec=fenetre,
                                                trou_max_sec=trou,
                                                maintenant=maintenant)
                    assert obtenu == attendu, (maintenant, fenetre, trou)
    finally:
        conn.close()


def test_les_cas_limites(tmp_path):
    conn = _base(tmp_path, [T0])
    try:
        lecteur = MarketReader(conn)
        for maintenant in (T0 - 1, T0, T0 + 1, T0 + 91, T0 + 10_000):
            for fenetre in (None, 60, 24 * 3600):
                assert lecteur.couverture(
                    fenetre_sec=fenetre, maintenant=maintenant) == \
                    _ancien_calcul([T0], fenetre, 90, maintenant)
    finally:
        conn.close()
    vide = _base(tmp_path / "..", [])
    try:
        assert MarketReader(vide).couverture(maintenant=T0) == \
            _ancien_calcul([], None, 90, T0)
    finally:
        vide.close()


class _Comptage:
    """Une connexion qui compte les lignes rendues à l'appelant."""

    def __init__(self, conn):
        self.conn = conn
        self.lignes = 0

    def execute(self, sql, params=()):
        curseur = self.conn.execute(sql, params)
        compte = self

        class _C:
            def fetchone(self_):
                ligne = curseur.fetchone()
                compte.lignes += ligne is not None
                return ligne

            def fetchall(self_):
                lignes = curseur.fetchall()
                compte.lignes += len(lignes)
                return lignes
        return _C()


def test_la_sonde_ne_rapatrie_plus_qu_une_ligne_par_couverture(tmp_path):
    battements = list(range(T0, T0 + 3 * 86400, 30))      # 8 640 battements
    conn = _base(tmp_path, battements)
    try:
        compte = _Comptage(conn)
        MarketReader(compte).couverture(fenetre_sec=24 * 3600,
                                        maintenant=battements[-1] + 10)
        MarketReader(compte).couverture(maintenant=battements[-1] + 10)
        assert compte.lignes == 2, "une ligne d'agrégats par couverture"
    finally:
        conn.close()


class _LecteurFactice:
    def __init__(self):
        self.appels = {"counts": 0, "tailles": 0, "battement": 0}
        self.conn = object()

    def last_heartbeat_sec(self):
        self.appels["battement"] += 1
        return self.maintenant - 5

    def counts(self):
        self.appels["counts"] += 1
        return {"uptime": self.appels["counts"]}

    def tailles(self):
        self.appels["tailles"] += 1
        return {"total": 1}

    def couverture(self, fenetre_sec=None, **_):
        return {"fenetre_sec": fenetre_sec or 0}


def test_les_compteurs_de_la_sonde_sont_gardes_trente_minutes(monkeypatch,
                                                              tmp_path):
    etat = health.EtatCollecte(tmp_path / "x.db")
    lecteur = _LecteurFactice()
    etat._lecteur = lambda: lecteur
    monkeypatch.setattr(health, "schema_version", lambda conn: 9)
    horloge = {"t": T0}
    monkeypatch.setattr(health.time, "time", lambda: horloge["t"])

    def sonder():
        lecteur.maintenant = horloge["t"]
        return etat.rapport()

    sain, d = sonder()
    assert sain and d["compteurs"] == {"uptime": 1}
    assert d["compteurs_age_sec"] == 0
    horloge["t"] += health.CACHE_COMPTEURS_SEC - 1
    sain, d = sonder()
    assert d["compteurs"] == {"uptime": 1}, "encore frais : pas recompté"
    assert d["compteurs_age_sec"] == health.CACHE_COMPTEURS_SEC - 1
    assert lecteur.appels["battement"] == 2, "le battement est relu à chaque fois"
    horloge["t"] += 1
    sain, d = sonder()
    assert d["compteurs"] == {"uptime": 2} and d["compteurs_age_sec"] == 0
    assert lecteur.appels == {"counts": 2, "tailles": 2, "battement": 3}
    horloge["t"] -= 100                       # horloge qui recule : recalcul
    sonder()
    assert lecteur.appels["counts"] == 3
