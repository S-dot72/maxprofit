"""
Le stockage Turso, éprouvé sans compte ni réseau.

Le pilote `libsql` n'a pas de wheel Windows pour Python 3.14 : il ne peut pas
être installé sur le poste de développement, seulement dans l'image Docker.
Ces tests le remplacent par un double et vérifient ce qui, sinon, ne se
découvrirait qu'en production — c'est-à-dire après avoir cru collecter.

Ce qui compte ici :

**Le repli silencieux est interdit.** Une URL sans jeton doit LEVER. Retomber
sur le stockage local donnerait exactement le comportement qu'on fuit : une
collecte qui tourne, qui a l'air saine, et qui disparaît au redémarrage.

**Un échec de synchronisation ne doit pas être muet.** Les données restent
dans la réplique locale, donc la collecte peut continuer — mais une réplique
qui ne se synchronise plus est une collecte qui ne survivra pas au prochain
redémarrage, et rien d'autre ne le signalerait.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from maxprofit.store import turso
from maxprofit.store.turso import TursoIndisponible


@pytest.fixture(autouse=True)
def env_propre(monkeypatch):
    for cle in (turso.ENV_URL, turso.ENV_JETON, "TRADING_DB_PATH"):
        monkeypatch.delenv(cle, raising=False)


class FausseConnexion:
    def __init__(self, echoue=False):
        self.syncs = 0
        self.echoue = echoue

    def sync(self):
        self.syncs += 1
        if self.echoue:
            raise RuntimeError("réseau coupé")


def _faux_libsql(monkeypatch, conn=None, erreur=None):
    module = types.ModuleType("libsql")
    cree = conn if conn is not None else FausseConnexion()

    def connect(chemin, sync_url=None, auth_token=None):
        if erreur:
            raise erreur
        cree.chemin, cree.url, cree.jeton = chemin, sync_url, auth_token
        return cree

    module.connect = connect
    monkeypatch.setitem(sys.modules, "libsql", module)
    return cree


# --------------------------------------------------------------------------- #
# Pas de repli silencieux
# --------------------------------------------------------------------------- #

def test_sans_url_turso_n_est_pas_utilise():
    assert turso.configure() is False


def test_une_url_sans_jeton_leve(monkeypatch):
    """LE test de ce fichier.

    Retomber en silence sur le stockage local sur un hébergement sans disque
    persistant produirait une collecte qui tourne et disparaît au redémarrage,
    sans un message. Mieux vaut refuser de démarrer."""
    monkeypatch.setenv(turso.ENV_URL, "libsql://exemple.turso.io")
    with pytest.raises(TursoIndisponible, match="vont ensemble"):
        turso.configure()


def test_url_et_jeton_activent_turso(monkeypatch):
    monkeypatch.setenv(turso.ENV_URL, "libsql://exemple.turso.io")
    monkeypatch.setenv(turso.ENV_JETON, "jeton")
    assert turso.configure() is True


def test_le_pilote_absent_donne_un_message_utile(monkeypatch, tmp_path):
    monkeypatch.setenv(turso.ENV_URL, "libsql://exemple.turso.io")
    monkeypatch.setenv(turso.ENV_JETON, "jeton")
    monkeypatch.setitem(sys.modules, "libsql", None)
    with pytest.raises(TursoIndisponible, match="pilote libsql"):
        turso.ouvrir(tmp_path / "cache.db")


# --------------------------------------------------------------------------- #
# Ouverture
# --------------------------------------------------------------------------- #

def test_l_ouverture_passe_url_et_jeton_et_synchronise(monkeypatch, tmp_path):
    monkeypatch.setenv(turso.ENV_URL, "libsql://exemple.turso.io")
    monkeypatch.setenv(turso.ENV_JETON, "jeton-secret")
    faux = _faux_libsql(monkeypatch)

    conn = turso.ouvrir(tmp_path / "sous" / "cache.db")

    assert conn.url == "libsql://exemple.turso.io"
    assert conn.jeton == "jeton-secret"
    assert conn.syncs == 1, "l'état distant n'a pas été tiré à l'ouverture"
    assert (tmp_path / "sous").is_dir(), "le répertoire du cache n'a pas été créé"


def test_une_synchronisation_initiale_ratee_empeche_le_demarrage(monkeypatch, tmp_path):
    """Démarrer sur une réplique dont on ignore l'état ferait rejouer des
    migrations déjà appliquées et réécrire des données déjà collectées."""
    monkeypatch.setenv(turso.ENV_URL, "libsql://exemple.turso.io")
    monkeypatch.setenv(turso.ENV_JETON, "jeton")
    _faux_libsql(monkeypatch, conn=FausseConnexion(echoue=True))

    with pytest.raises(TursoIndisponible, match="Synchronisation initiale"):
        turso.ouvrir(tmp_path / "cache.db")


def test_une_connexion_refusee_indique_quoi_verifier(monkeypatch, tmp_path):
    monkeypatch.setenv(turso.ENV_URL, "libsql://exemple.turso.io")
    monkeypatch.setenv(turso.ENV_JETON, "perime")
    _faux_libsql(monkeypatch, erreur=RuntimeError("401 unauthorized"))

    with pytest.raises(TursoIndisponible, match="tokens create"):
        turso.ouvrir(tmp_path / "cache.db")


# --------------------------------------------------------------------------- #
# Synchronisation courante
# --------------------------------------------------------------------------- #

def test_un_echec_de_synchronisation_n_arrete_pas_la_collecte(caplog):
    """Les données restent dans la réplique locale : le réseau reviendra. Mais
    l'échec doit être visible, sinon une collecte condamnée passerait pour
    saine."""
    conn = FausseConnexion(echoue=True)
    with caplog.at_level("ERROR"):
        assert turso.synchroniser(conn) is False
    assert any("perdues si le conteneur redémarre" in m for m in caplog.messages)


def test_synchroniser_une_connexion_sqlite_ordinaire_ne_fait_rien():
    """En stockage local, la synchronisation n'a pas de sens et ne doit pas
    lever : le même code de collecteur sert les deux modes."""
    import sqlite3

    assert turso.synchroniser(sqlite3.connect(":memory:")) is False
    assert turso.est_replique(sqlite3.connect(":memory:")) is False


def test_est_replique_reconnait_une_connexion_synchronisable():
    assert turso.est_replique(FausseConnexion()) is True


# --------------------------------------------------------------------------- #
# Chemin du cache
# --------------------------------------------------------------------------- #

def test_le_cache_va_dans_un_temporaire_par_defaut():
    """C'est ce qui rend l'hébergement possible sans disque persistant : plus
    besoin d'un point de montage."""
    chemin = turso.chemin_cache()
    assert chemin.name.endswith(".db")
    assert chemin.is_absolute()


def test_trading_db_path_sert_de_cache_si_defini(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "ailleurs.db"))
    assert turso.chemin_cache() == tmp_path / "ailleurs.db"


def test_le_choix_du_chemin_depend_du_mode(monkeypatch, tmp_path):
    """En mode durable, les exigences du §1.1 s'appliquent. En mode réplique,
    le fichier n'est qu'un cache et elles n'ont plus lieu d'être."""
    from maxprofit.store.db import chemin_donnees

    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "market.db"))
    assert chemin_donnees() == tmp_path / "market.db"

    # Sans Turso, un répertoire inexistant est refusé.
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "absent" / "market.db"))
    with pytest.raises(Exception):
        chemin_donnees()

    # Avec Turso, il sera créé : ce n'est qu'un cache.
    monkeypatch.setenv(turso.ENV_URL, "libsql://exemple.turso.io")
    monkeypatch.setenv(turso.ENV_JETON, "jeton")
    assert chemin_donnees() == tmp_path / "absent" / "market.db"
