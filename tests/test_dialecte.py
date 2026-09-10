"""
La traduction SQLite -> PostgreSQL.

Surface volontairement petite : ce module ne traduit pas SQL en general, il
traduit LE dialecte que ce projet ecrit. Chaque regle est ici, et rien d'autre
ne doit y entrer sans qu'on ait d'abord essaye d'ecrire la SQL portable.
"""

from __future__ import annotations

import pytest

from maxprofit.store import dialecte


# --- marqueurs de parametres ----------------------------------------------

def test_les_marqueurs_sont_traduits():
    assert dialecte.marqueurs("INSERT INTO t VALUES (?,?,?)") == \
        "INSERT INTO t VALUES (%s,%s,%s)"


def test_un_point_d_interrogation_dans_une_chaine_est_epargne():
    """Le projet n'en ecrit aucune, mais un remplacement global est le genre de
    raccourci qui se retourne contre soi des que quelqu'un en ecrit une."""
    sql = "SELECT * FROM t WHERE nom = 'pourquoi ?' AND x = ?"
    assert dialecte.marqueurs(sql) == \
        "SELECT * FROM t WHERE nom = 'pourquoi ?' AND x = %s"


# --- le piege du REAL ------------------------------------------------------

def test_real_devient_double_precision():
    """PostgreSQL accepte REAL sans broncher, mais c'est du 32 bits : un prix
    y perdrait des decimales, sans une seule erreur."""
    assert "DOUBLE PRECISION" in dialecte.vers_postgres("price REAL NOT NULL")
    assert "REAL" not in dialecte.vers_postgres("price REAL NOT NULL")


# --- MAX/MIN : agregat ou pas ---------------------------------------------

def test_max_a_deux_arguments_devient_greatest():
    traduit = dialecte.vers_postgres("SET high = MAX(candles.high, excluded.high)")
    assert "GREATEST(candles.high, excluded.high)" in traduit


def test_min_a_deux_arguments_devient_least():
    traduit = dialecte.vers_postgres("SET low = MIN(candles.low, excluded.low)")
    assert "LEAST(candles.low, excluded.low)" in traduit


def test_l_agregat_a_un_argument_n_est_PAS_touche():
    """La regression qui casserait chaque requete d'agregation du projet :
    `MAX(ts_sec)` est un agregat, identique dans les deux moteurs."""
    sql = "SELECT pair, MAX(ts_sec) FROM payouts GROUP BY pair"
    assert dialecte.vers_postgres(sql) == sql
    assert "GREATEST" not in dialecte.vers_postgres(sql)


def test_max_imbrique_est_entierement_traduit():
    traduit = dialecte.vers_postgres("SELECT MAX(a, MAX(b, c))")
    assert "MAX(" not in traduit
    assert traduit.count("GREATEST") == 2


# --- constructions propres a SQLite ---------------------------------------

def test_without_rowid_disparait():
    traduit = dialecte.vers_postgres(
        "CREATE TABLE t (a TEXT, PRIMARY KEY (a)) WITHOUT ROWID")
    assert "WITHOUT ROWID" not in traduit
    assert "PRIMARY KEY (a)" in traduit


@pytest.mark.parametrize("sql", [
    "PRAGMA journal_mode=WAL",
    "  pragma foreign_keys = ON",
])
def test_un_pragma_est_reconnu_comme_ignorable(sql):
    assert dialecte.est_pragma(sql)


def test_une_instruction_ordinaire_n_est_pas_un_pragma():
    assert not dialecte.est_pragma("SELECT 1")
    assert not dialecte.est_pragma("INSERT INTO pragma_log VALUES (1)")


# --- ce qui doit rester intact --------------------------------------------

def test_le_sql_portable_traverse_sans_dommage():
    """Les ecritures ont ete reecrites en SQL standard plutot que traduites :
    ce module ne doit pas avoir a les connaitre."""
    sql = ("INSERT INTO ticks (pair, ts_ms, price) VALUES (?,?,?) "
           "ON CONFLICT DO NOTHING")
    traduit = dialecte.vers_postgres(sql)
    assert "ON CONFLICT DO NOTHING" in traduit
    assert traduit.count("%s") == 3
