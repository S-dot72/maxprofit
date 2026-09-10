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

#: Le numero de la prochaine migration, quel que soit l'etat du schema.
#: Ecrire 2 en dur obligeait a reprendre ces tests a chaque migration
#: ajoutee -- et un test qu'on retouche a chaque deploiement finit par
#: etre ajuste au resultat au lieu de le verifier.
V_SUIVANTE = SCHEMA_VERSION + 1

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

    migrations_v2 = MIGRATIONS + (Migration(V_SUIVANTE, "colonne source", _v2_ajoute_une_colonne),)

    # --- redémarrage ----------------------------------------------------------
    conn = open_read_write(db, migrations=migrations_v2)

    assert schema_version(conn) == V_SUIVANTE, "la version de schéma n'a pas été avancée"

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

    conn = open_read_write(db, migrations=MIGRATIONS + (Migration(V_SUIVANTE, "v2", _v2),))
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
        open_read_write(db, migrations=MIGRATIONS + (Migration(V_SUIVANTE, "v2", _v2_casse),))

    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT version FROM _schema_version WHERE id = 1").fetchone()[0] == SCHEMA_VERSION, (
        "la version a été avancée alors que la migration a échoué"
    )
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(ticks)")}
    assert "source" not in colonnes, "la colonne a survécu au ROLLBACK"
    assert conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 100
    conn.close()


def test_migrations_appliquees_une_par_une_dans_l_ordre(db):
    trace = []
    _TROIS = (V_SUIVANTE, V_SUIVANTE + 1, V_SUIVANTE + 2)

    def faire(n):
        def _appliquer(c):
            trace.append(n)
            c.execute(f"CREATE TABLE IF NOT EXISTS t{n} (x INTEGER)")
        return _appliquer

    migrations = MIGRATIONS + tuple(
        Migration(n, f"v{n}", faire(n)) for n in _TROIS
    )
    conn = open_read_write(db, migrations=migrations)
    assert trace == list(_TROIS)
    assert schema_version(conn) == _TROIS[-1]
    conn.close()

    # Deuxième démarrage : plus rien à appliquer.
    trace.clear()
    conn = open_read_write(db, migrations=migrations)
    assert trace == []
    conn.close()


def test_numerotation_incoherente_refusee(db):
    conn = open_read_write(db)
    for mauvaises in (
        MIGRATIONS + (Migration(V_SUIVANTE + 1, "trou", lambda c: None),),
        MIGRATIONS + (Migration(SCHEMA_VERSION, "doublon", lambda c: None),),
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
    # La version vit en table, pas dans le PRAGMA : voir store/version.py — un
    # PRAGMA silencieusement ignoré par un moteur compatible SQLite ferait
    # rejouer toutes les migrations.
    assert restaure.execute(
        "SELECT version FROM _schema_version WHERE id = 1").fetchone()[0] == SCHEMA_VERSION
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


def test_une_base_versionnee_par_pragma_est_reprise_sans_rejouer(tmp_path):
    """Reprise des bases existantes.

    Une base créée avant le passage à la table porte sa version dans
    `PRAGMA user_version`. À la première ouverture, ce numéro est recopié dans
    la table — aucune migration n'est rejouée, aucune donnée touchée. Rejouer
    une migration non idempotente sur une base peuplée détruirait la collecte.
    """
    db = tmp_path / "ancienne.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.executescript(
        "CREATE TABLE ticks (pair TEXT, ts_ms INTEGER, price REAL);"
        "INSERT INTO ticks VALUES ('X', 1704067200000, 1.1);"
        "PRAGMA user_version = 1;"
    )
    conn.close()

    ouverte = open_read_write(db)
    try:
        # La version heritee n'est pas perdue : les migrations deja
        # appliquees ne sont PAS rejouees. Seules celles d'apres le numero
        # herite tournent, et le schema arrive a jour.
        assert schema_version(ouverte) == SCHEMA_VERSION
        assert ouverte.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1
        # La table heritee n'a pas ete refaite au passage : elle garde ses trois
        # colonnes d'origine, sans celles que la migration 1 aurait creees.
        colonnes = {r[1] for r in ouverte.execute("PRAGMA table_info(ticks)")}
        assert colonnes == {"pair", "ts_ms", "price"}
    finally:
        ouverte.close()


def test_la_lecture_seule_n_ecrit_rien(tmp_path):
    """Régression : lire la version créait la table, ce qui faisait échouer
    toute ouverture en lecture seule."""
    db = tmp_path / "market.db"
    conn = open_read_write(db)
    conn.close()

    ro = open_read_only(db)
    try:
        assert schema_version(ro) == SCHEMA_VERSION
    finally:
        ro.close()


# --------------------------------------------------------------------------- #
# Ecriture groupee : une instruction par lot, pas une par ligne
# --------------------------------------------------------------------------- #
#
# `executemany` de `libsql` boucle en Python : chaque ligne est un aller-retour
# vers Turso. Mesure en production le 7 septembre : 183 payouts en 29 secondes,
# soit 158 ms par ligne. Pendant ces 29 secondes le collecteur ne drainait aucun
# tick, et l'operation se repete toutes les cinq minutes.

class ConnexionQuiCompte:
    """Enveloppe une vraie connexion et compte les `execute`."""

    def __init__(self, conn):
        self._conn = conn
        self.executions = 0

    def execute(self, sql, params=()):
        self.executions += 1
        return self._conn.execute(sql, params)

    def executemany(self, sql, rows):        # pragma: no cover
        raise AssertionError(
            "executemany fait un aller-retour reseau par ligne : interdit ici"
        )


def test_les_ecritures_groupees_ne_font_pas_une_requete_par_ligne(db):
    conn = open_read_write(db)
    espionne = ConnexionQuiCompte(conn)
    writer = MarketWriter(espionne)

    writer.insert_ticks(
        [Tick("EURUSD_otc", T0_MS + i * 250, 1.1 + i / 100_000) for i in range(300)]
    )
    assert espionne.executions <= 2, (
        f"{espionne.executions} requetes pour 300 ticks : sur une base "
        f"distante, c'est autant d'allers-retours reseau"
    )

    espionne.executions = 0
    writer.insert_payouts(
        T0_SEC, [PairInfo(f"P{i}_otc", True, 90) for i in range(183)])
    assert espionne.executions == 1, "183 payouts doivent tenir en une requete"

    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 300
    assert conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0] == 183
    conn.close()


def test_le_lot_reste_sous_la_limite_de_parametres(db):
    """Au-dela de 999 parametres lies, SQLite refuse l'instruction."""
    from maxprofit.store.market import MAX_PARAMS

    conn = open_read_write(db)
    writer = MarketWriter(conn)
    n = 5_000
    writer.insert_ticks(
        [Tick("EURUSD_otc", T0_MS + i * 250, 1.1) for i in range(n)])
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == n
    assert MAX_PARAMS <= 999
    conn.close()


def test_une_bougie_groupee_se_fusionne_toujours_correctement(db):
    """La clause ON CONFLICT doit survivre au passage en insertion multiple.

    C'est elle qui garantit qu'une bougie complete ne redevient jamais
    incomplete a cause d'un tick tardif.
    """
    conn = open_read_write(db)
    writer = MarketWriter(conn)
    base = dict(pair="EURUSD_otc", tf_sec=60, ts_sec=T0_SEC, open=1.0, low=0.9)
    writer.upsert_candles([
        Candle(**base, high=1.2, close=1.1, tick_count=30, complete=True),
        Candle(**base, high=1.05, close=1.0, tick_count=2, complete=False),
    ])
    conn.commit()
    ligne = conn.execute(
        "SELECT high, low, close, tick_count, complete FROM candles").fetchone()
    assert ligne[0] == 1.2, "le plus haut a ete perdu dans la fusion"
    assert ligne[3] == 30
    assert ligne[4] == 1, "une bougie complete est redevenue incomplete"
    conn.close()


# --------------------------------------------------------------------------- #
# Les payouts : 95 % de redondance mesuree
# --------------------------------------------------------------------------- #
#
# 407 907 lignes enregistrees en production, 20 168 porteuses d'information.
# 183 paires relevees toutes les cinq minutes, c'est 52 704 lignes par jour qui
# repetent la precedente -- et c'est ce qui a epuise le quota du stockage
# distant avant la fin de la campagne.

def test_un_payout_inchange_n_est_pas_reecrit(db):
    conn = open_read_write(db)
    writer = MarketWriter(conn)
    paires = [PairInfo("EURUSD_otc", True, 92), PairInfo("GBPUSD_otc", True, 88)]

    assert writer.insert_payouts(T0_SEC, paires) == 2
    for i in range(1, 20):
        assert writer.insert_payouts(T0_SEC + i * 300, paires) == 0

    assert conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0] == 2
    conn.close()


def test_un_changement_est_toujours_enregistre(db):
    conn = open_read_write(db)
    writer = MarketWriter(conn)

    writer.insert_payouts(T0_SEC, [PairInfo("EURUSD_otc", True, 92)])
    writer.insert_payouts(T0_SEC + 300, [PairInfo("EURUSD_otc", True, 92)])
    writer.insert_payouts(T0_SEC + 600, [PairInfo("EURUSD_otc", True, 78)])
    writer.insert_payouts(T0_SEC + 900, [PairInfo("EURUSD_otc", False, 78)])

    lignes = conn.execute(
        "SELECT ts_sec, payout_pct, is_open FROM payouts ORDER BY ts_sec"
    ).fetchall()
    assert [(l[1], l[2]) for l in lignes] == [(92, 1), (78, 1), (78, 0)]
    conn.close()


def test_la_regle_du_2_3_donne_le_meme_resultat_qu_avant(db):
    """Le point qui compte : la deduplication ne doit RIEN changer a la
    reponse de `payout_at`. Une valeur inchangee est deja representee par le
    dernier point de changement."""
    conn = open_read_write(db)
    writer = MarketWriter(conn)
    writer.insert_payouts(T0_SEC, [PairInfo("EURUSD_otc", True, 92)])
    for i in range(1, 10):
        writer.insert_payouts(T0_SEC + i * 300, [PairInfo("EURUSD_otc", True, 92)])
    writer.insert_payouts(T0_SEC + 3000, [PairInfo("EURUSD_otc", True, 70)])
    conn.commit()
    conn.close()

    ro = open_read_only(db)
    lecteur = MarketReader(ro)
    # Au milieu de la plage sans ligne : la valeur en vigueur est bien 92.
    assert lecteur.payout_at("EURUSD_otc", T0_SEC + 1500).payout_pct == 92
    assert lecteur.payout_at("EURUSD_otc", T0_SEC + 3600).payout_pct == 70
    # Avant tout releve : rien, et surtout pas la valeur d'aujourd'hui.
    assert lecteur.payout_at("EURUSD_otc", T0_SEC - 1) is None
    ro.close()


def test_un_redemarrage_ne_reecrit_pas_l_etat_courant(db):
    """Sur un hebergeur qui redemarre souvent, une deduplication qui ne
    survivrait pas au processus ne servirait a rien."""
    conn = open_read_write(db)
    paires = [PairInfo(f"P{i}_otc", True, 90) for i in range(183)]
    assert MarketWriter(conn).insert_payouts(T0_SEC, paires) == 183
    conn.commit()
    conn.close()

    conn = open_read_write(db)
    assert MarketWriter(conn).insert_payouts(T0_SEC + 300, paires) == 0
    assert conn.execute("SELECT COUNT(*) FROM payouts").fetchone()[0] == 183
    conn.close()


# --------------------------------------------------------------------------- #
# Le choix du moteur
# --------------------------------------------------------------------------- #

def test_postgres_l_emporte_sur_turso_et_sur_le_fichier(monkeypatch):
    """Trois modes de stockage, une seule regle de priorite.

    Sans ordre explicite, une variable Turso oubliee dans les reglages d'un
    hebergeur ferait repartir la collecte sur une base vide -- exactement le
    desastre silencieux que la §1.1 cherche a empecher.
    """
    from maxprofit.store import db as mod

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/base")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://ailleurs.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "jeton")

    ouverts = []
    monkeypatch.setattr(mod.postgres, "ouvrir", lambda: ouverts.append("pg") or _ConnFactice())
    monkeypatch.setattr(mod.turso, "ouvrir", lambda p: ouverts.append("turso"))

    assert mod.chemin_donnees() == Path("postgresql")
    mod.open_read_only("peu-importe")
    assert ouverts == ["pg"], "Turso a ete prefere a PostgreSQL"


def test_une_url_qui_n_est_pas_postgres_est_refusee(monkeypatch):
    """Pas de repli silencieux : une URL copiee du mauvais endroit doit se
    voir au demarrage, pas se traduire par une base locale qui repart vide."""
    from maxprofit.store import postgres

    monkeypatch.setenv("DATABASE_URL", "Maxprofit")
    with pytest.raises(postgres.PostgresIndisponible, match="postgresql://"):
        postgres.configure()


def test_sans_url_postgres_n_est_pas_demande(monkeypatch):
    from maxprofit.store import postgres

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert postgres.configure() is False


class _ConnFactice:
    def execute(self, *a, **k):
        raise AssertionError("aucune requete ne devait partir dans ce test")
