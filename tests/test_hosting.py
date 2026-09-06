"""
La sonde HTTP, et surtout : la sonde ne ment pas.

Le seul test qui compte vraiment ici est `test_collecte_arretee_renvoie_503`.
Une sonde toujours verte laisse l'hébergeur maintenir en vie un collecteur mort
et le moniteur d'uptime se taire — on découvre la panne quatorze jours plus
tard, devant une base vide. Ce test est ce qui empêche quelqu'un de « simplifier »
`health_check` en un `return {'status': 'ok'}`.

Les tests asynchrones sont pilotés par `asyncio.run` plutôt que par un plugin
pytest : une dépendance de test en moins, et le comportement observé est celui
des vraies routes.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from maxprofit.core.types import Tick
from maxprofit.hosting import health
from maxprofit.hosting.health import EtatCollecte, build_app
from maxprofit.store.db import open_read_write
from maxprofit.store.market import MarketWriter

T0_MS = 1_704_067_200_000


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "market.db"


def _base_avec_battement(db: Path, age_sec: int, *, ticks: int = 5) -> None:
    """Crée une base dont le dernier battement de coeur date de `age_sec`."""
    conn = open_read_write(db)
    writer = MarketWriter(conn)
    writer.insert_ticks([Tick("EURUSD_otc", T0_MS + i * 250, 1.1) for i in range(ticks)])
    writer.heartbeat(int(time.time()) - age_sec, 6)
    writer.close()


def _get(db: Path, chemin: str) -> tuple[int, dict | str]:
    """Interroge une vraie route de l'application et retourne (statut, corps)."""
    async def _appeler():
        async with TestClient(TestServer(build_app(db))) as client:
            reponse = await client.get(chemin)
            if reponse.content_type == "application/json":
                return reponse.status, await reponse.json()
            return reponse.status, await reponse.text()

    return asyncio.run(_appeler())


# --------------------------------------------------------------------------- #
# Honnêteté de la sonde
# --------------------------------------------------------------------------- #

def test_collecte_vivante_renvoie_200(db):
    _base_avec_battement(db, age_sec=5)
    statut, corps = _get(db, "/health")
    assert statut == 200
    assert corps["status"] == "ok"
    assert corps["age_battement_sec"] < health.SEUIL_PANNE_SEC
    assert corps["compteurs"]["ticks"] == 5
    assert corps["schema_version"] >= 1


def test_collecte_arretee_renvoie_503(db):
    """LE test de ce fichier. Le collecteur n'a plus battu depuis longtemps :
    la sonde doit le dire, pas rassurer."""
    _base_avec_battement(db, age_sec=health.SEUIL_PANNE_SEC + 60)
    statut, corps = _get(db, "/health")
    assert statut == 503, "une collecte arrêtée est signalée comme saine"
    assert corps["status"] == "collecte_arretee"
    assert corps["age_battement_sec"] > health.SEUIL_PANNE_SEC


def test_base_absente_renvoie_503(db):
    """Au tout premier démarrage la base n'existe pas encore. C'est un état
    transitoire légitime, mais ce n'est pas « en bonne santé »."""
    statut, corps = _get(db, "/health")
    assert statut == 503
    assert corps["status"] == "demarrage"


def test_base_sans_battement_renvoie_503(db):
    conn = open_read_write(db)
    conn.close()
    statut, corps = _get(db, "/health")
    assert statut == 503
    assert corps["status"] == "aucun_battement"


def test_la_racine_est_la_meme_sonde_que_health(db):
    _base_avec_battement(db, age_sec=5)
    assert _get(db, "/")[0] == _get(db, "/health")[0] == 200


def test_ping_reste_vert_meme_si_la_collecte_est_morte(db):
    """`/ping` est la vivacité du processus, `/health` la santé de la collecte.

    Les séparer évite une boucle de redémarrages : si l'hébergeur est branché
    sur `/health` et que la collecte tombe pour une raison que le redémarrage
    ne corrige pas, il redémarre indéfiniment sans rien réparer.
    """
    _base_avec_battement(db, age_sec=health.SEUIL_PANNE_SEC + 600)
    assert _get(db, "/ping") == (200, "pong")
    assert _get(db, "/health")[0] == 503


def test_la_sonde_ne_peut_pas_ecrire_dans_la_base(db):
    """La sonde observe ; elle ne modifie pas ce qu'elle observe."""
    _base_avec_battement(db, age_sec=5)
    etat = EtatCollecte(db)
    etat.rapport()
    with pytest.raises(Exception):
        etat._reader.conn.execute(
            "INSERT INTO uptime (ts_sec, n_pairs) VALUES (1, 1)"
        )


def test_la_sonde_ne_leve_jamais(db, monkeypatch):
    """Une sonde qui plante prive l'hébergeur de la seule information qu'il
    sait lire. Quoi qu'il arrive en base, elle répond."""
    _base_avec_battement(db, age_sec=5)
    etat = EtatCollecte(db)
    etat.rapport()  # ouvre le lecteur

    import sqlite3

    def _casse(*_args, **_kwargs):
        raise sqlite3.OperationalError("disque en feu")

    monkeypatch.setattr(etat._reader, "last_heartbeat_sec", _casse)
    sain, details = etat.rapport()
    assert sain is False
    assert details["status"] == "erreur"


# --------------------------------------------------------------------------- #
# Contrat d'hébergement
# --------------------------------------------------------------------------- #

def test_le_port_vient_de_la_variable_PORT(monkeypatch, db):
    """L'hébergeur injecte `$PORT` et vérifie que le processus écoute dessus.
    Un port codé en dur passe en local et échoue au déploiement."""
    monkeypatch.setenv("PORT", "12345")

    async def _demarrer():
        runner = await health.start_http_server(db)
        try:
            adresses = [s.name for s in runner.sites]
            return adresses
        finally:
            await runner.cleanup()

    adresses = asyncio.run(_demarrer())
    assert any("12345" in str(a) for a in adresses), adresses


def test_ecoute_sur_toutes_les_interfaces(db):
    """Dans un conteneur, l'hébergeur teste le port depuis l'extérieur du
    namespace réseau : un bind sur 127.0.0.1 y est invisible."""
    async def _demarrer():
        runner = await health.start_http_server(db, port=0)
        try:
            return [str(s.name) for s in runner.sites]
        finally:
            await runner.cleanup()

    adresses = asyncio.run(_demarrer())
    assert any("0.0.0.0" in a for a in adresses), adresses
