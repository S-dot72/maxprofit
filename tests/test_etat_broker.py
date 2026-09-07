"""
L'attente entre deux tentatives doit survivre au processus.

C'est tout l'intérêt du dispositif : quand le broker refuse, le collecteur meurt
et l'hébergeur le relance. Une temporisation en mémoire repartirait de zéro à
chaque fois, et l'on rappellerait le broker toutes les quarante secondes,
indéfiniment — en empêchant justement la limitation de débit d'expirer.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from maxprofit.store import etat_broker
from maxprofit.store.db import open_read_write


@pytest.fixture
def conn(tmp_path: Path):
    c = open_read_write(tmp_path / "market.db")
    yield c
    c.close()


def _jusqu_au_silence(conn, raison: str = "refus") -> int:
    """Enchaine les echecs jusqu'a ce qu'une attente soit due.

    Ecrire le nombre en dur lierait ces tests a la forme exacte des paliers ;
    ils verifient le comportement, pas le tableau.
    """
    for n in range(1, len(etat_broker.PALIERS_SEC) + 1):
        etat_broker.noter_echec(conn, raison)
        if etat_broker.attente_requise(conn) > 0:
            return n
    raise AssertionError("aucun palier ne declenche d'attente")


def test_base_neuve_aucune_attente(conn):
    assert etat_broker.attente_requise(conn) == 0.0
    assert etat_broker.lire(conn) == (0, None, None)


def test_les_premiers_echecs_ne_coutent_rien(conn):
    """Une coupure de dix secondes ne doit pas creuser une minute de trou.

    La temporisation en memoire du collecteur (1 s, 2 s, 4 s) suffit pour un
    incident passager ; ce dispositif-ci ne vise que le refus durable.
    """
    for _ in range(3):
        etat_broker.noter_echec(conn, "coupure passagere")
        assert etat_broker.attente_requise(conn) == 0.0


def test_l_attente_croit_avec_les_echecs(conn):
    attentes = []
    for _ in range(len(etat_broker.PALIERS_SEC)):
        etat_broker.noter_echec(conn, "poignee de main expiree")
        # Arrondi a la seconde : l'attente decompte le temps deja ecoule, donc
        # deux paliers egaux different de quelques microsecondes.
        attentes.append(round(etat_broker.attente_requise(conn)))
    assert attentes == sorted(attentes), "l'attente doit etre croissante"
    assert attentes[-1] > 0
    # Le seuil est franchi une fois pour toutes, pas a chaque echec : on veut
    # une bascule nette entre « incident » et « refus », pas une pente douce.
    assert attentes[0] == 0.0


def test_l_attente_est_plafonnee(conn):
    for _ in range(50):
        etat_broker.noter_echec(conn, "refus")
    assert etat_broker.attente_requise(conn) <= max(etat_broker.PALIERS_SEC)


def test_un_succes_efface_l_ardoise(conn):
    _jusqu_au_silence(conn)
    assert etat_broker.attente_requise(conn) > 0
    etat_broker.noter_succes(conn)
    assert etat_broker.attente_requise(conn) == 0.0


def test_l_attente_decompte_le_temps_deja_ecoule(conn):
    """Un arrêt long paie la dette de lui-même.

    Sinon un conteneur redémarré après six heures se tairait encore une heure
    pour rien, alors que la limitation a expiré depuis longtemps.
    """
    _jusqu_au_silence(conn)
    du = etat_broker.attente_requise(conn)
    plus_tard = time.time() + du + 1
    assert etat_broker.attente_requise(conn, maintenant=plus_tard) == 0.0


def test_l_attente_survit_a_la_fermeture_de_la_connexion(tmp_path: Path):
    """Le cas qui compte : un processus neuf hérite de la dette du précédent."""
    base = tmp_path / "market.db"
    premier = open_read_write(base)
    attendus = _jusqu_au_silence(premier, "poignee de main expiree")
    premier.close()

    second = open_read_write(base)
    try:
        echecs, _, raison = etat_broker.lire(second)
        assert echecs == attendus
        assert "poignee" in raison
        assert etat_broker.attente_requise(second) > 0
    finally:
        second.close()


def test_raison_tronquee_mais_conservee(conn):
    etat_broker.noter_echec(conn, "x" * 5000)
    _, _, raison = etat_broker.lire(conn)
    assert 0 < len(raison) <= 500


def test_table_absente_ne_leve_pas(tmp_path: Path):
    """Une base plus ancienne que ce code ne doit pas empêcher la collecte.

    Ne rien savoir vaut mieux que ne pas démarrer : le pire qui puisse arriver
    est de rappeler le broker trop tôt une fois.
    """
    conn = open_read_write(tmp_path / "market.db")
    conn.execute("DROP TABLE etat_broker")
    conn.commit()
    assert etat_broker.lire(conn) == (0, None, None)
    assert etat_broker.attente_requise(conn) == 0.0
    etat_broker.noter_echec(conn, "peu importe")     # ne lève pas
    etat_broker.noter_succes(conn)                   # ne lève pas
    conn.close()
