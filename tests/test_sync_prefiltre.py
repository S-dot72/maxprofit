"""
Correction B du transfert (29/10) : la synchronisation de la relève ne
rapatrie plus en entier les lignes de `plan_etat` (ni des autres petites
tables) qu'elle n'écrira pas.
- même résultat que la lecture complète, ligne pour ligne, et même bilan,
  sur des états tirés au hasard ;
- un changement réel (ligne nouvelle, état plus récent) est toujours copié ;
- une comparaison qui échoue retombe sur la lecture complète ;
- l'état inchangé n'est plus rapatrié.
"""

from __future__ import annotations

import random
import shutil

import pytest

from maxprofit.store import fusion
from maxprofit.store.db import open_read_write

COLONNES = ("campagne, maj_ts_sec, jour, solde, solde_ouverture, "
            "sessions_jouees, sessions_perdues_daffilee, jour_utc, reancrages, "
            "derniere_bougie, session_pas_joues, session_engagees, "
            "session_gain_vise")


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def _etat(conn, campagne: str, maj: int, solde: float, gros: int = 0):
    conn.execute(
        f"INSERT INTO plan_etat ({COLONNES}) VALUES "
        "(?, ?, 2, ?, 250, 0, 0, 0, ?, '{}', 0, '[]', 0) "
        "ON CONFLICT (campagne) DO UPDATE SET maj_ts_sec = excluded.maj_ts_sec, "
        "solde = excluded.solde, reancrages = excluded.reancrages",
        (campagne, maj, solde, "[" + "0," * gros + "0]"))


def _reglage(conn, cle: str, valeur: str, maj: int):
    conn.execute("INSERT INTO reglages (cle, valeur, maj_ts_sec) VALUES "
                 "(?, ?, ?) ON CONFLICT (cle) DO UPDATE SET "
                 "valeur = excluded.valeur, maj_ts_sec = excluded.maj_ts_sec",
                 (cle, valeur, maj))


def _contenu(conn, table: str):
    return sorted(tuple(r) for r in conn.execute(
        f"SELECT * FROM {table}").fetchall())


def _bilan(b):
    return (b.table, b.source, b.deja, b.a_inserer, b.a_remplacer)


def _trois_bases(tmp_path):
    source = open_read_write(tmp_path / "source.db")
    cible = open_read_write(tmp_path / "cible.db")
    return source, cible


@pytest.mark.parametrize("graine", range(20))
def test_meme_resultat_que_la_lecture_complete(tmp_path, graine):
    alea = random.Random(graine)
    source, cible = _trois_bases(tmp_path)
    try:
        for i in range(alea.randrange(1, 6)):
            campagne = f"plan-{i}"
            maj = alea.randrange(100, 200)
            _etat(source, campagne, maj, 250 + i, gros=alea.randrange(0, 50))
            sort = alea.random()
            if sort < 0.3:
                continue                                  # absente de la cible
            ecart = alea.choice((-20, 0, 0, 15))          # plus vieille, égale…
            _etat(cible, campagne, maj + ecart, 200 + i)
        for k in range(alea.randrange(0, 4)):
            _reglage(source, f"r{k}", "x", alea.randrange(10, 20))
            if alea.random() < 0.5:
                _reglage(cible, f"r{k}", "y", alea.randrange(10, 20))
        source.commit()
        cible.commit()
        cible.close()
        shutil.copy(tmp_path / "cible.db", tmp_path / "temoin.db")
        cible = open_read_write(tmp_path / "cible.db")
        temoin = open_read_write(tmp_path / "temoin.db")
        try:
            for table in ("plan_etat", "reglages", "operateurs"):
                b_complet = fusion._fusionner_table(source, temoin, table, True)
                b_filtre = fusion._fusionner_table(source, cible, table, True,
                                                   prefiltrer=True)
                assert _bilan(b_filtre) == _bilan(b_complet), table
                assert _contenu(cible, table) == _contenu(temoin, table), table
        finally:
            temoin.close()
    finally:
        source.close()
        cible.close()


class _Comptage:
    def __init__(self, conn):
        self.conn = conn
        self.sql: list[str] = []

    def execute(self, sql, params=()):
        self.sql.append(sql)
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()


def test_l_etat_inchange_n_est_plus_rapatrie_et_un_changement_l_est(
        tmp_path):
    source, cible = _trois_bases(tmp_path)
    try:
        for c in ("ancienne-1", "ancienne-2", "active"):
            _etat(source, c, 100, 250, gros=200)
            _etat(cible, c, 100, 250, gros=200)
        source.commit()
        cible.commit()
        espion = _Comptage(source)
        b = fusion._fusionner_table(espion, cible, "plan_etat", True,
                                    prefiltrer=True)
        assert (b.source, b.deja, b.a_inserer, b.a_remplacer) == (3, 3, 0, 0)
        assert not [s for s in espion.sql if "reancrages" in s], (
            "aucune ligne complète relue quand rien n'a changé")
        _etat(source, "active", 160, 261.81, gros=200)       # la course a écrit
        source.commit()
        espion.sql.clear()
        b = fusion._fusionner_table(espion, cible, "plan_etat", True,
                                    prefiltrer=True)
        assert (b.a_inserer, b.a_remplacer) == (0, 1)
        assert sum(1 for s in espion.sql if "reancrages" in s) == 1, (
            "seule la ligne changée est relue")
        assert cible.execute("SELECT solde FROM plan_etat WHERE campagne = "
                             "'active'").fetchone()[0] == pytest.approx(261.81)
    finally:
        source.close()
        cible.close()


def test_une_comparaison_ratee_retombe_sur_la_lecture_complete(
        tmp_path, monkeypatch):
    source, cible = _trois_bases(tmp_path)
    try:
        _etat(source, "active", 200, 261.81)
        _etat(cible, "active", 100, 250)
        source.commit()
        cible.commit()

        def panne(*a, **k):
            raise RuntimeError("comparaison impossible")
        monkeypatch.setattr(fusion, "_lignes_utiles", panne)
        b = fusion._fusionner_table(source, cible, "plan_etat", True,
                                    prefiltrer=True)
        assert b.a_remplacer == 1
        assert cible.execute("SELECT solde FROM plan_etat").fetchone()[0] == \
            pytest.approx(261.81), "la mise à jour n'a pas été perdue"
    finally:
        source.close()
        cible.close()


def test_trop_de_lignes_a_relire_retombe_sur_la_lecture_complete(
        tmp_path, monkeypatch):
    monkeypatch.setattr(fusion, "PREFILTRE_LIGNES_MAX", 2)
    source, cible = _trois_bases(tmp_path)
    try:
        for i in range(5):
            _etat(source, f"c{i}", 100, 250)
        source.commit()
        b = fusion._fusionner_table(source, cible, "plan_etat", True,
                                    prefiltrer=True)
        assert b.a_inserer == 5 and len(_contenu(cible, "plan_etat")) == 5
    finally:
        source.close()
        cible.close()


def test_la_synchronisation_de_la_releve_utilise_le_prefiltre(monkeypatch):
    from maxprofit.store import pool
    vus = []
    monkeypatch.setattr(pool, "_fusionner_table",
                        lambda s, c, t, a, **k: vus.append((t, k)) or
                        fusion.Bilan(t))
    monkeypatch.setattr(pool, "_tables", lambda conn: set(pool.PETITES))
    pool.synchroniser(object(), object(), journal=lambda *a: None)
    assert vus and all(k.get("prefiltrer") for _, k in vus)
