"""
Version de schéma — en TABLE, pas en `PRAGMA user_version`.

`PRAGMA user_version` est la façon idiomatique de versionner un schéma SQLite,
et c'est ce que la spec §1.3 nomme explicitement. Elle a un défaut décisif ici :
rien ne garantit qu'un moteur compatible SQLite l'expose. Sur une réplique
embarquée libSQL — le mode qui rend l'hébergement possible sans disque
persistant — les PRAGMA passent par une couche de synchronisation, et un PRAGMA
silencieusement ignoré serait catastrophique : la version resterait à 0, les
migrations se rejoueraient à chaque démarrage, et une migration non idempotente
détruirait des données.

On stocke donc la version dans une TABLE ordinaire. Elle survit à tout ce qui
sait exécuter du SQL, et se lit avec les mêmes outils que le reste.

**Reprise des bases existantes.** Une base créée avant ce changement porte sa
version dans `PRAGMA user_version` et n'a pas la table. À la première ouverture,
on lit le PRAGMA et on l'inscrit dans la table. Aucune migration n'est rejouée,
aucune donnée n'est touchée — c'est le même numéro, écrit ailleurs. Le PRAGMA
est ensuite ignoré : une seule source de vérité, sinon les deux divergent.
"""

from __future__ import annotations

TABLE = "_schema_version"


def _lire_pragma(conn) -> int:
    """Version héritée. Retourne 0 si le moteur ne connaît pas ce PRAGMA."""
    try:
        ligne = conn.execute("PRAGMA user_version").fetchone()
    except Exception:                                    # noqa: BLE001
        return 0
    if not ligne:
        return 0
    try:
        return int(ligne[0])
    except (TypeError, ValueError):
        return 0


def initialiser(conn) -> None:
    """Crée la table si besoin, en reprenant la version héritée du PRAGMA.

    Idempotent : appelée à chaque ouverture.
    """
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        f"  id INTEGER PRIMARY KEY CHECK (id = 1),"
        f"  version INTEGER NOT NULL"
        f")"
    )
    ligne = conn.execute(f"SELECT version FROM {TABLE} WHERE id = 1").fetchone()
    if ligne is not None:
        return
    # Table neuve : soit la base est vierge (PRAGMA à 0), soit elle vient d'une
    # version antérieure de ce code, qui versionnait par PRAGMA. Les deux cas se
    # traitent pareil, et aucune migration n'est rejouée.
    conn.execute(f"INSERT INTO {TABLE} (id, version) VALUES (1, ?)",
                 (_lire_pragma(conn),))


def lire(conn) -> int:
    """Lit la version SANS rien écrire.

    Cette fonction est appelée sur des connexions ouvertes en lecture seule —
    celle du backtest, celle de la sonde HTTP. Y créer la table ferait échouer
    toute ouverture en lecture seule d'une base ancienne, ce qui est exactement
    l'inverse du service rendu.

    Table absente : on retombe sur le PRAGMA hérité. Une base d'avant ce
    changement se lit donc correctement sans être modifiée.
    """
    try:
        ligne = conn.execute(f"SELECT version FROM {TABLE} WHERE id = 1").fetchone()
    except Exception:                                    # noqa: BLE001 - table absente
        return _lire_pragma(conn)
    return int(ligne[0]) if ligne else _lire_pragma(conn)


def ecrire(conn, version: int) -> None:
    conn.execute(f"UPDATE {TABLE} SET version = ? WHERE id = 1", (int(version),))
