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
from maxprofit.store import postgres, turso
from maxprofit.store import version as version_schema
from maxprofit.store.migrations import MIGRATIONS, SCHEMA_VERSION, Migration

log = logging.getLogger(__name__)


class SchemaError(BotError):
    """Le schéma en base et le code ne sont pas compatibles."""


def _user_version(conn) -> int:
    """Version de schéma, lue dans une table et non dans un PRAGMA.

    Voir `store/version.py` : un PRAGMA silencieusement ignoré par un moteur
    compatible SQLite ferait rejouer toutes les migrations à chaque démarrage.
    """
    return version_schema.lire(conn)


def _debuter(conn) -> None:
    """Ouvre une transaction, quel que soit le moteur.

    Deux régimes incompatibles, et les confondre coûte un déploiement :

    `sqlite3` est ouvert ici en `isolation_level=None`, c'est-à-dire en
    validation automatique : rien n'est en cours, et un `BEGIN` explicite est
    nécessaire pour grouper une migration.

    `libsql` ouvre au contraire une transaction IMPLICITE dès la connexion. Y
    envoyer un `BEGIN` lève « connection has reached an invalid state, started
    with Txn » — l'erreur qui a fait échouer le premier démarrage sur Turso.
    """
    if _valide_implicitement(conn):
        return
    conn.execute("BEGIN")


def _valider(conn) -> None:
    if _valide_implicitement(conn):
        conn.commit()
    else:
        conn.execute("COMMIT")


def _annuler(conn) -> None:
    if _valide_implicitement(conn):
        conn.rollback()
        return
    # `in_transaction` est faux si l'instruction a émis un COMMIT implicite.
    # Tenter un ROLLBACK dans ce cas masquerait l'erreur d'origine par un
    # « no transaction is active » et rendrait la panne illisible.
    if getattr(conn, "in_transaction", False):
        conn.execute("ROLLBACK")


def _valide_implicitement(conn) -> bool:
    """La connexion tient-elle déjà une transaction ouverte ?

    Reconnu à la nature de la connexion — pas à un drapeau passé par
    l'appelant, qui se désynchroniserait.

    `sqlite3` ouvert en `isolation_level=None` valide chaque instruction ;
    libSQL et psycopg ouvrent une transaction et attendent un `commit()`. Un
    `BEGIN` explicite sur ces deux-là échoue, d'où cette distinction plutôt
    qu'un régime unique.
    """
    return turso.est_replique(conn) or postgres.est_postgres(conn)


def valider(conn) -> None:
    """Rend les écritures durables. Publique : le collecteur l'appelle après
    chaque vidage de tampon.

    Sans elle, `libsql` accumulerait indéfiniment dans sa transaction implicite
    et rien ne serait jamais poussé vers Turso. En `sqlite3` autocommit, c'est
    sans effet — le même code sert les deux modes.
    """
    try:
        conn.commit()
    except Exception as erreur:                          # noqa: BLE001
        log.debug("commit sans effet : %s", erreur)


def _configure(conn) -> None:
    """Réglages du moteur, appliqués au mieux.

    WAL permet de lire pendant l'écriture — l'inspection et les sauvegardes
    tournent sans arrêter le collecteur — et rend la base résistante aux
    coupures. Ces PRAGMA ne peuvent pas s'exécuter dans une transaction, donc
    avant toute migration.

    Un moteur compatible SQLite peut refuser ou ignorer certains d'entre eux :
    une réplique embarquée gère elle-même sa durabilité et n'a pas à recevoir
    d'ordre sur son journal. On n'échoue donc pas là-dessus — ce sont des
    optimisations, pas des garanties de correction. Ce qui, lui, ne doit jamais
    être supposé, c'est la version de schéma : elle est en table.
    """
    for reglage in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL",
                    "PRAGMA foreign_keys=ON", "PRAGMA busy_timeout=30000"):
        try:
            conn.execute(reglage)
        except Exception as erreur:                      # noqa: BLE001
            log.debug("%s refusé par le moteur : %s", reglage, erreur)


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

    # La table de version est créée ici, sur le chemin d'ÉCRITURE seulement :
    # `lire` ne doit rien écrire, puisqu'on l'appelle aussi sur des connexions
    # en lecture seule.
    version_schema.initialiser(conn)
    valider(conn)      # la table de version doit exister pour de bon
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
        _debuter(conn)
        try:
            migration.apply(conn)
            version_schema.ecrire(conn, migration.version)
            _valider(conn)
        except Exception:
            _annuler(conn)
            log.error("Migration %d échouée, base laissée en version %d",
                      migration.version, _user_version(conn))
            raise

    return _user_version(conn)


def _refuser_un_disque_ephemere() -> None:
    """Dans un conteneur sans stockage distant, ARRÊTER au lieu de collecter.

    C'est le désastre exact que toute la §1 cherche à empêcher, et il vient de
    se produire : `DATABASE_URL` n'était pas renseigné côté hébergeur, le code
    est retombé sur un fichier dans `/tmp`, et la collecte a tourné en affichant
    une sonde verte et des compteurs qui montaient. Ils repartaient de zéro à
    chaque redémarrage, et il a fallu comparer deux captures d'écran pour s'en
    apercevoir.

    Un fichier local est parfaitement légitime sur un poste de travail — d'où la
    détection de conteneur plutôt qu'une interdiction générale.
    """
    from maxprofit.core.config import _dans_un_conteneur

    if postgres.configure() or turso.configure():
        return
    if not _dans_un_conteneur():
        return
    raise SchemaError(
        f"Aucun stockage durable configuré, et ce processus tourne dans un "
        f"conteneur : le disque y est effacé à chaque déploiement, à chaque "
        f"redémarrage et après chaque mise en veille.\n"
        f"La collecte semblerait fonctionner — sonde verte, compteurs qui "
        f"montent — et repartirait de zéro sans un message. Définissez "
        f"{postgres.ENV_URL}."
    )


def _signaler_reglages_ignores() -> None:
    """Dire tout haut qu'un réglage de stockage ne sert à rien.

    Trois modes coexistent, et l'ordre de priorité est dans le code. Mais une
    variable Turso laissée dans les réglages d'un hébergeur donne l'impression
    qu'elle agit — on la voit, elle est là, et l'on cherche ensuite pourquoi
    « le code essaie toujours de se connecter à Turso ». Il ne le fait pas ;
    c'est le silence qui laissait croire le contraire.
    """
    import os

    if not os.environ.get(postgres.ENV_URL, "").strip():
        return
    restes = [nom for nom in (turso.ENV_URL, turso.ENV_JETON, "TRADING_DB_PATH")
              if os.environ.get(nom, "").strip()]
    if restes:
        log.warning(
            "%s est défini : %s sont IGNORÉS. Retirez-les des réglages pour "
            "que ce qui est affiché corresponde à ce qui s'exécute.",
            postgres.ENV_URL, ", ".join(restes),
        )


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

    _signaler_reglages_ignores()
    _refuser_un_disque_ephemere()
    if postgres.configure():
        # Aucun fichier local : la base est distante, point. C'est ce qui rend
        # l'hébergement sans disque possible sans le détour d'une réplique — et
        # sans le quota de synchronisations qui va avec.
        conn = postgres.ouvrir()
    elif turso.configure():
        # Le fichier local n'est plus qu'un CACHE : la vérité est chez Turso,
        # et la connexion la tire à l'ouverture. Son répertoire peut donc être
        # créé — ce qui serait interdit pour une base durable (§1.1), mais qui
        # est exactement ce qu'on veut pour un cache sur disque éphémère.
        conn = turso.ouvrir(path)
    else:
        if not path.parent.is_dir():
            raise SchemaError(
                f"Le répertoire {path.parent} n'existe pas. La base n'est pas "
                f"créée à la volée dans un chemin inconnu : ce serait masquer "
                f"une faute de frappe par une base vide."
            )
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row

    _configure(conn)
    version = apply_migrations(conn, migrations)
    if turso.est_replique(conn):
        # Les migrations viennent d'écrire : sans cette synchronisation, un
        # conteneur qui redémarre aussitôt repartirait d'un schéma antérieur.
        turso.synchroniser(conn)
    log.info("Base %s ouverte en écriture, schéma v%d%s", path, version,
             " (réplique Turso)" if turso.est_replique(conn) else "")
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
    if postgres.configure():
        # PostgreSQL n'a pas de descripteur en lecture seule à opposer au
        # backtest. La frontière du §0 y est tenue autrement : `MarketReader`
        # n'expose aucune écriture, et `test_layering` refuse qu'un fichier de
        # `maxprofit/backtest/` importe `MarketWriter`. C'est une garantie plus
        # faible qu'un `mode=ro`, et il faut le dire plutôt que le taire.
        return postgres.ouvrir()

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


def chemin_donnees() -> Path:
    """Où écrire, selon le mode de stockage.

    PostgreSQL configuré : aucun fichier n'est ouvert, et le chemin retourné
    ne sert qu'aux journaux. Exiger `TRADING_DB_PATH` ici ferait échouer un
    déploiement pour un réglage sans objet.

    Turso configuré : une réplique locale, qui n'est qu'un cache et peut donc
    vivre n'importe où — y compris sur le disque éphémère d'un conteneur.
    Sinon : le chemin durable du §1.1, avec toutes ses exigences.
    """
    from maxprofit.core.config import db_path

    if postgres.configure():
        return Path("postgresql")

    return turso.chemin_cache() if turso.configure() else db_path()
