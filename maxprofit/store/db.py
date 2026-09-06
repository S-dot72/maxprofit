"""
Ouverture de la base — le seul endroit du projet qui appelle `sqlite3.connect`.

Spec §1.3. Le déroulé au démarrage, dans cet ordre :

    version_en_base = PRAGMA user_version
    si version_en_base > SCHEMA_VERSION  -> ARRÊT (code plus vieux que la base)
    si version_en_base < SCHEMA_VERSION  -> appliquer les migrations manquantes,
                                            une par une, chacune dans sa transaction

Le premier cas mérite qu'on s'y arrête, parce qu'il est contre-intuitif : une
base plus récente que le code arrive lors d'un rollback de déploiement. Le
vieux code ne connaît pas les colonnes ajoutées depuis, il écrirait des lignes
incomplètes dans un schéma qu'il croit comprendre. Refuser de démarrer est la
seule réponse sûre.

L'accès en lecture seule n'est pas une politesse, c'est la frontière du §0 :
« Backtest — ne fait jamais : écrire dans les tables de marché. » SQLite ouvre
le fichier en `mode=ro`, donc une écriture depuis le backtest lève au lieu de
corrompre l'historique. La discipline ne protège rien ; le descripteur de
fichier, si.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from maxprofit.core.errors import BotError
from maxprofit.store.migrations import MIGRATIONS, SCHEMA_VERSION, Migration

log = logging.getLogger(__name__)


class SchemaError(BotError):
    """Le schéma en base et le code ne sont pas compatibles."""


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _configure(conn: sqlite3.Connection) -> None:
    # WAL : lectures possibles pendant l'écriture (l'inspection et les backups
    # tournent sans arrêter le collecteur), et résistance aux coupures.
    # Ne peut pas s'exécuter dans une transaction, donc avant toute migration.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> int:
    """Amène la base à la version du code. Retourne la version atteinte.

    Chaque migration tourne dans SA transaction : si la troisième échoue, les
    deux premières restent acquises et la version en base le reflète. Une
    migration partiellement appliquée serait pire qu'une migration échouée.
    """
    if not migrations:
        raise SchemaError("Aucune migration déclarée")

    cible = migrations[-1].version
    numeros = [m.version for m in migrations]
    # Exactement 1, 2, 3... N. Un trou compte autant qu'un doublon : avec
    # `[1, 3]`, une base restée en version 1 se croirait à jour dès qu'elle
    # atteint 3, et la migration 2 ajoutée plus tard ne s'appliquerait jamais.
    if numeros != list(range(1, len(numeros) + 1)):
        raise SchemaError(
            f"Migrations mal numérotées : {numeros}. Attendu "
            f"{list(range(1, len(numeros) + 1))} — sans trou, sans doublon, "
            f"dans l'ordre."
        )

    courante = _user_version(conn)

    if courante > cible:
        raise SchemaError(
            f"La base est en version {courante}, le code n'en connaît que "
            f"{cible}. Le code est plus VIEUX que la base — typiquement après "
            f"un rollback de déploiement. Démarrer écrirait des lignes "
            f"incomplètes dans un schéma mal compris. Redéployez la version du "
            f"code qui correspond, ou repartez d'une sauvegarde."
        )

    if courante == cible:
        return courante

    for migration in migrations:
        if migration.version <= courante:
            continue
        log.info("Migration %d : %s", migration.version, migration.label)
        conn.execute("BEGIN")
        try:
            migration.apply(conn)
            # PRAGMA n'accepte pas de paramètre lié : le numéro vient de notre
            # propre table de migrations, jamais d'une entrée externe.
            conn.execute(f"PRAGMA user_version = {int(migration.version)}")
            conn.execute("COMMIT")
        except Exception:
            # `in_transaction` est faux si la migration a émis un COMMIT
            # implicite (executescript, PRAGMA journal_mode...). Tenter un
            # ROLLBACK dans ce cas masquerait l'erreur d'origine par un
            # « no transaction is active » et rendrait la panne illisible.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            log.error("Migration %d échouée, base laissée en version %d",
                      migration.version, _user_version(conn))
            raise

    return _user_version(conn)


def open_read_write(
    path: Path | str,
    *,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> sqlite3.Connection:
    """Ouvre la base en écriture et applique les migrations manquantes.

    Le fichier est créé s'il n'existe pas — c'est le cas normal du tout premier
    démarrage. En revanche le RÉPERTOIRE doit exister : `core.config.db_path()`
    le vérifie, pour qu'une faute de frappe dans `TRADING_DB_PATH` produise une
    erreur et non une base vide (§1.1).
    """
    path = Path(path)
    if not path.parent.is_dir():
        raise SchemaError(
            f"Le répertoire {path.parent} n'existe pas. La base n'est pas créée "
            f"à la volée dans un chemin inconnu : ce serait masquer une faute de "
            f"frappe par une base vide."
        )
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _configure(conn)
    version = apply_migrations(conn, migrations)
    log.info("Base %s ouverte en écriture, schéma v%d", path, version)
    return conn


def open_read_only(path: Path | str) -> sqlite3.Connection:
    """Ouvre la base en LECTURE SEULE, sans migrer.

    Utilisée par le backtest et par les outils d'inspection. Deux propriétés
    importantes :

    - toute écriture lève `sqlite3.OperationalError`, y compris une écriture
      accidentelle dans une table de marché ;
    - aucune migration n'est appliquée. Un outil de lecture ne doit jamais
      modifier le schéma sous les pieds du collecteur qui tourne.
    """
    path = Path(path)
    if not path.is_file():
        raise SchemaError(
            f"{path} n'existe pas. Une base absente n'est pas créée en lecture "
            f"seule : la question est de savoir pourquoi elle manque."
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row

    version = _user_version(conn)
    if version > SCHEMA_VERSION:
        conn.close()
        raise SchemaError(
            f"La base est en version {version}, ce code n'en connaît que "
            f"{SCHEMA_VERSION}. Lire une base plus récente donnerait des "
            f"résultats silencieusement partiels."
        )
    if version < SCHEMA_VERSION:
        log.warning(
            "Base %s en schéma v%d alors que le code attend v%d. Lecture "
            "autorisée mais les colonnes récentes sont absentes : lancez un "
            "processus en écriture pour migrer.", path, version, SCHEMA_VERSION,
        )
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return _user_version(conn)
