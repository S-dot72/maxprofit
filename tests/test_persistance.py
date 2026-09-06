"""
Critère d'acceptation du §1, mot pour mot :

« Écrire un test qui : crée une base, insère 100 lignes, simule un déploiement
(nouvelle version du code, migration ajoutée), redémarre, et vérifie que les
100 lignes sont toujours là et que la nouvelle colonne existe. Ce test tourne
en CI à chaque commit. »

C'est `test_survie_au_deploiement` ci-dessous. Le reste du fichier couvre les
quatre garde-fous du §1 un par un, parce qu'un seul test vert sur le cas
nominal ne dit rien du comportement le jour où quelque chose ne va pas — et
c'est ce jour-là qu'on perd les données.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from maxprofit.core.types import Candle, PairInfo, Tick
from maxprofit.store import backup
from maxprofit.store.db import (
    SchemaError,
    apply_migrations,
    open_read_only,
    open_read_write,
    schema_version,
)
from maxprofit.store.market import MarketReader, MarketWriter
from maxprofit.store.migrations import MIGRATIONS, SCHEMA_VERSION, Migration

T0_SEC = 1_704_067_200
T0_MS = T0_SEC * 1000


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "market.db"


def _cent_ticks(writer: MarketWriter) -> None:
    writer.insert_ticks(
        [Tick("EURUSD_otc", T0_MS + i * 250, 1.1 + i / 100_000) for i in range(100)]
    )


# --------------------------------------------------------------------------- #
# §1.3 — le critère d'acceptation
# --------------------------------------------------------------------------- #

def test_survie_au_deploiement(db):
    """Une base peuplée survit à un déploiement qui ajoute une migration."""
    # --- version du code déployée aujourd'hui --------------------------------
    conn = open_read_write(db)
    writer = MarketWriter(conn)
    _cent_ticks(writer)
    writer.close()
    assert db.is_file()

    # --- déploiement : le code repart avec une migration de plus -------------
    def _v2_ajoute_une_colonne(c):
        # ADD COLUMN : autorisé, non destructeur, instantané sur SQLite.
        c.execute("ALTER TABLE ticks ADD COLUMN source TEXT")

    migrations_v2 = MIGRATIONS + (Migration(2, "colonne source", _v2_ajoute_une_colonne),)

    # --- redémarrage ----------------------------------------------------------
    conn = open_read_write(db, migrations=migrations_v2)

    assert schema_version(conn) == 2, "la version de schéma n'a pas été avancée"

    lignes = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    assert lignes == 100, f"{100 - lignes} lignes perdues lors du déploiement"

    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(ticks)")}
    assert "source" in colonnes, "la nouvelle colonne n'a pas été créée"
    assert {"pair", "ts_ms", "price"} <= colonnes, "des colonnes ont disparu"

    # Les lignes préexistantes ont la nouvelle colonne à NULL, pas de valeur
    # inventée : le backtest doit pouvoir distinguer « inconnu » de « vide ».
    assert conn.execute("SELECT COUNT(*) FROM ticks WHERE source IS NULL").fetchone()[0] == 100
    conn.close()


def test_redemarrage_sans_migration_ne_change_rien(db):
    conn = open_read_write(db)
    _cent_ticks(MarketWriter(conn))
    conn.close()

    for _ in range(3):
        conn = open_read_write(db)
        assert conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 100
        assert schema_version(conn) == SCHEMA_VERSION
        conn.close()


def test_code_plus_vieux_que_la_base_refuse_de_demarrer(db):
    """Le cas du rollback de déploiement. Le vieux code ne connaît pas les
    colonnes ajoutées depuis : démarrer écrirait des lignes incomplètes."""
    def _v2(c):
        c.execute("ALTER TABLE ticks ADD COLUMN source TEXT")

    conn = open_read_write(db, migrations=MIGRATIONS + (Migration(2, "v2", _v2),))
    _cent_ticks(MarketWriter(conn))
    conn.close()

    with pytest.raises(SchemaError, match="plus VIEUX"):
        open_read_write(db, migrations=MIGRATIONS)

    # Et surtout : rien n'a été touché.
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 100
    conn.close()


def test_migration_qui_echoue_laisse_la_base_dans_son_etat_anterieur(db):
    """« Chacune dans sa transaction » : une migration à moitié appliquée
    serait pire qu'une migration échouée."""
    conn = open_read_write(db)
    _cent_ticks(MarketWriter(conn))
    conn.close()

    def _v2_casse(c):
        c.execute("ALTER TABLE ticks ADD COLUMN source TEXT")
        raise RuntimeError("panne au milieu de la migration")

    with pytest.raises(RuntimeError):
        open_read_write(db, migrations=MIGRATIONS + (Migration(2, "v2", _v2_casse),))

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1, (
        "la version a été avancée alors que la migration a échoué"
    )
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(ticks)")}
    assert "source" not in colonnes, "la colonne a survécu au ROLLBACK"
    assert conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 100
    conn.close()


def test_migrations_appliquees_une_par_une_dans_l_ordre(db):
    trace = []

    def faire(n):
        def _appliquer(c):
            trace.append(n)
            c.execute(f"CREATE TABLE IF NOT EXISTS t{n} (x INTEGER)")
        return _appliquer

    migrations = MIGRATIONS + tuple(
        Migration(n, f"v{n}", faire(n)) for n in (2, 3, 4)
    )
    conn = open_read_write(db, migrations=migrations)
    assert trace == [2, 3, 4]
    assert schema_version(conn) == 4
    conn.close()

    # Deuxième démarrage : plus rien à appliquer.
    trace.clear()
    conn = open_read_write(db, migrations=migrations)
    assert trace == []
    conn.close()


def test_numerotation_incoherente_refusee(db):
    conn = open_read_write(db)
    for mauvaises in (
        MIGRATIONS + (Migration(3, "trou", lambda c: None),),
        MIGRATIONS + (Migration(1, "doublon", lambda c: None),),
    ):
        with pytest.raises(SchemaError, match="mal numérotées"):
            apply_migrations(conn, mauvaises)
    conn.close()


# --------------------------------------------------------------------------- #
# §0 / §1.2 — la frontière en lecture seule
# --------------------------------------------------------------------------- #

def test_lecture_seule_refuse_toute_ecriture(db):
    conn = open_read_write(db)
    _cent_ticks(MarketWriter(conn))
    conn.close()

    ro = open_read_only(db)
    assert MarketReader(ro).counts()["ticks"] == 100
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO ticks (pair, ts_ms, price) VALUES ('X', 1, 1.0)")
    ro.close()


def test_lecture_seule_ne_cree_pas_la_base(db):
    """Une base absente ne doit pas être créée en silence par un outil de
    lecture : la question est de savoir pourquoi elle manque."""
    with pytest.raises(SchemaError, match="n'existe pas"):
        open_read_only(db)
    assert not db.exists()


def test_lecture_seule_ne_migre_pas(db):
    conn = open_read_write(db)
    conn.close()
    ro = open_read_only(db)
    assert schema_version(ro) == SCHEMA_VERSION
    ro.close()


def test_repertoire_inexistant_refuse(tmp_path):
    with pytest.raises(SchemaError, match="n'existe pas"):
        open_read_write(tmp_path / "faute_de_frappe" / "market.db")


# --------------------------------------------------------------------------- #
# Écritures idempotentes
# --------------------------------------------------------------------------- #

def test_ticks_idempotents(db):
    """Une reconnexion renvoie des ticks déjà reçus. Ils ne doivent produire ni
    doublon ni erreur."""
    conn = open_read_write(db)
    writer = MarketWriter(conn)
    _cent_ticks(writer)
    _cent_ticks(writer)
    assert writer.counts()["ticks"] == 100
    conn.close()


def test_une_bougie_complete_ne_redevient_pas_incomplete(db):
    """Un tick tardif réécrit une bougie déjà close. Elle a bien été observée
    en entier : son drapeau `complete` ne doit pas retomber à 0, sinon le
    backtest l'écarterait à tort et le taux d'exclusion du §2.4 mentirait."""
    conn = open_read_write(db)
    writer = MarketWriter(conn)
    base = dict(pair="EURUSD_otc", tf_sec=60, ts_sec=T0_SEC, open=1.0, low=0.9)

    writer.upsert_candles([Candle(**base, high=1.2, close=1.1, tick_count=30, complete=True)])
    writer.upsert_candles([Candle(**base, high=1.05, close=1.0, tick_count=2, complete=False)])

    row = conn.execute("SELECT * FROM candles").fetchone()
    assert row["complete"] == 1
    assert row["tick_count"] == 30
    assert row["high"] == pytest.approx(1.2), "le plus haut a été rétréci"
    assert row["low"] == pytest.approx(0.9)
    conn.close()


# --------------------------------------------------------------------------- #
# §2.3 — payout d'époque
# --------------------------------------------------------------------------- #

def test_payout_d_epoque(db):
    conn = open_read_write(db)
    writer = MarketWriter(conn)
    writer.insert_payouts(T0_SEC, [PairInfo("EURUSD_otc", True, 92)])
    writer.insert_payouts(T0_SEC + 3600, [PairInfo("EURUSD_otc", True, 78)])
    conn.close()

    reader = MarketReader(open_read_only(db))
    # Avant tout relevé : pas de payout. Le trade ne doit pas être généré,
    # surtout pas estimé avec celui d'aujourd'hui (§2.3).
    assert reader.payout_at("EURUSD_otc", T0_SEC - 1) is None
    assert reader.payout_at("EURUSD_otc", T0_SEC).payout_pct == 92
    assert reader.payout_at("EURUSD_otc", T0_SEC + 3599).payout_pct == 92
    assert reader.payout_at("EURUSD_otc", T0_SEC + 3600).payout_pct == 78
    assert reader.payout_at("INCONNUE_otc", T0_SEC) is None
    reader.close()


# --------------------------------------------------------------------------- #
# §1.4 — sauvegardes
# --------------------------------------------------------------------------- #

def test_sauvegarde_est_une_copie_lisible_et_complete(db, tmp_path):
    """« Une restauration doit être testée une fois, à vide, avant de faire
    confiance au dispositif. » Ce test est cette restauration, et il tourne à
    chaque commit plutôt qu'une seule fois."""
    conn = open_read_write(db)
    _cent_ticks(MarketWriter(conn))

    dossier = tmp_path / "backups"
    copie = backup.creer_sauvegarde(conn, dossier,
                                    now=datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc))
    conn.close()

    assert copie.name == "market_20240101_0600.db"
    restaure = sqlite3.connect(copie)
    assert restaure.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 100
    assert restaure.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    restaure.close()


def test_sauvegarde_pendant_que_la_base_est_ouverte(db, tmp_path):
    """VACUUM INTO doit produire un instantané cohérent sans arrêter le
    collecteur : la connexion d'écriture reste ouverte et utilisable après."""
    conn = open_read_write(db)
    writer = MarketWriter(conn)
    _cent_ticks(writer)

    backup.creer_sauvegarde(conn, tmp_path / "backups",
                            now=datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc))

    writer.insert_ticks([Tick("EURUSD_otc", T0_MS + 999_000, 1.5)])
    assert writer.counts()["ticks"] == 101
    conn.close()


def test_retention_7_quotidiennes_et_4_hebdomadaires(tmp_path):
    dossier = tmp_path / "backups"
    dossier.mkdir()
    depart = datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc)

    # 60 jours, 4 sauvegardes par jour (toutes les 6 h).
    fichiers = {}
    for jour in range(60):
        for heure in (0, 6, 12, 18):
            quand = depart + timedelta(days=jour, hours=heure)
            f = dossier / f"market_{quand.strftime('%Y%m%d_%H%M')}.db"
            f.write_bytes(b"x")
            fichiers[f] = quand

    garder = backup.sauvegardes_a_conserver(fichiers)
    supprimes = backup.purger(dossier, tmp_path / "market.db")

    restants = sorted(p.name for p in dossier.iterdir())
    assert len(restants) == len(garder)
    assert len(supprimes) == 240 - len(restants)
    # 7 jours + 4 semaines, les semaines récentes recoupant les jours récents.
    assert 7 <= len(restants) <= 11
    # La plus récente est toujours là.
    assert "market_20240229_1800.db" in restants


def test_purge_ne_touche_pas_aux_fichiers_inconnus(tmp_path):
    """Le répertoire de sauvegardes peut contenir une copie manuelle précieuse.
    Ce qui ne correspond pas exactement au motif est laissé en place."""
    dossier = tmp_path / "backups"
    dossier.mkdir()
    depart = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for jour in range(30):
        quand = depart + timedelta(days=jour)
        (dossier / f"market_{quand.strftime('%Y%m%d_%H%M')}.db").write_bytes(b"x")

    intrus = [
        dossier / "avant_le_gros_refactor.db",
        dossier / "market.db",
        dossier / "notes.txt",
        dossier / "market_20240101.db",
    ]
    for f in intrus:
        f.write_bytes(b"precieux")

    backup.purger(dossier, tmp_path / "market.db")
    for f in intrus:
        assert f.is_file(), f"{f.name} a été supprimé alors qu'il est inconnu"


def test_purge_refuse_de_supprimer_la_base_vive(tmp_path):
    """Cas tordu mais possible : quelqu'un place la base vive dans le
    répertoire de sauvegardes, avec un nom qui correspond au motif."""
    dossier = tmp_path / "backups"
    dossier.mkdir()
    vive = dossier / "market_20200101_0000.db"
    vive.write_bytes(b"la base vive")
    for jour in range(1, 40):
        quand = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=jour)
        (dossier / f"market_{quand.strftime('%Y%m%d_%H%M')}.db").write_bytes(b"x")

    backup.purger(dossier, vive)
    assert vive.is_file(), "la base vive a été purgée comme une sauvegarde"
