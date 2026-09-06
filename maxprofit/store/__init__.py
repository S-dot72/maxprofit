"""
Persistance — le seul paquet qui parle à SQLite.

Existe séparément de `collect` pour une raison de frontière (spec §0) : le
backtest doit lire les tables de marché sans pouvoir y écrire, et sans importer
la couche Collecte. Il obtient un `MarketReader` sur un descripteur ouvert en
`mode=ro` ; le collecteur obtient un `MarketWriter`. La règle « le backtest
n'écrit jamais dans les tables de marché » est ainsi portée par le système de
fichiers, pas par la vigilance du relecteur.

Les quatre garde-fous du §1 vivent ici :
  §1.1 chemin hors du code      -> maxprofit.core.config.db_path()
  §1.2 rien de destructeur      -> aucun DROP/DELETE ici ; voir reset_db.py
  §1.3 migrations en avant      -> db.apply_migrations + migrations.MIGRATIONS
  §1.4 sauvegardes              -> backup.sauvegarder_et_purger
"""

from maxprofit.store.backup import creer_sauvegarde, purger, sauvegarder_et_purger
from maxprofit.store.db import (
    SchemaError,
    apply_migrations,
    open_read_only,
    open_read_write,
    schema_version,
)
from maxprofit.store.market import MarketReader, MarketWriter
from maxprofit.store.research import (
    MIGRATIONS_RECHERCHE,
    Journal,
    commit_git_courant,
    compter_experiences,
    hash_jeu_de_donnees,
    open_recherche,
    open_recherche_lecture,
)
from maxprofit.store.migrations import MIGRATIONS, SCHEMA_VERSION, Migration

__all__ = [
    "MIGRATIONS",
    "MIGRATIONS_RECHERCHE",
    "SCHEMA_VERSION",
    "Journal",
    "MarketReader",
    "MarketWriter",
    "Migration",
    "SchemaError",
    "apply_migrations",
    "commit_git_courant",
    "compter_experiences",
    "hash_jeu_de_donnees",
    "creer_sauvegarde",
    "open_read_only",
    "open_recherche",
    "open_recherche_lecture",
    "open_read_write",
    "purger",
    "sauvegarder_et_purger",
    "schema_version",
]
