"""Neon coupe les connexions inactives : la connexion se rouvre seule."""

from __future__ import annotations

import psycopg
import pytest

from maxprofit.store.postgres import Connexion


class _Brute:
    def __init__(self, coupee=False):
        self.closed = False
        self.broken = False
        self.coupee = coupee
        self.vues: list[str] = []

    def cursor(self):
        brute = self

        class _C:
            rowcount = 0

            def execute(self, sql, params):
                if brute.coupee:
                    brute.broken = True
                    raise psycopg.OperationalError(
                        "consuming input failed: server closed the "
                        "connection unexpectedly")
                brute.vues.append(sql)

            def fetchone(self):
                return (1,)

        return _C()

    def rollback(self):
        pass

    def commit(self):
        pass

    def close(self):
        self.closed = True


def test_une_connexion_coupee_est_rouverte_et_l_instruction_rejouee():
    morte, neuve = _Brute(coupee=True), _Brute()
    conn = Connexion(morte, rouvrir=lambda: neuve)
    assert conn.execute("SELECT 1").fetchone() == (1,)
    assert neuve.vues == ["SELECT 1"]
    conn.execute("SELECT 2")
    assert neuve.vues[-1] == "SELECT 2", "la connexion neuve est gardée"


def test_au_milieu_d_un_begin_l_erreur_remonte_mais_la_connexion_revit():
    premiere = _Brute()
    neuve = _Brute()
    conn = Connexion(premiere, rouvrir=lambda: neuve)
    conn.execute("BEGIN")
    premiere.coupee = True
    with pytest.raises(psycopg.OperationalError):
        conn.execute("INSERT INTO t VALUES (1)")
    assert neuve.vues == [], "rien n'est rejoué hors de sa transaction"
    conn.execute("SELECT 1")
    assert neuve.vues == ["SELECT 1"]


def test_une_erreur_sql_ordinaire_n_est_jamais_rejouee():
    class _Erreur(_Brute):
        def cursor(self):
            class _C:
                def execute(self, sql, params):
                    raise psycopg.errors.UndefinedTable("t")
            return _C()

    appels = []
    conn = Connexion(_Erreur(), rouvrir=lambda: appels.append(1) or _Brute())
    with pytest.raises(psycopg.errors.UndefinedTable):
        conn.execute("SELECT * FROM t")
    assert appels == []
