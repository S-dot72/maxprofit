"""
PostgreSQL — le stockage durable, sans quota de synchronisation.

**Pourquoi.** Turso a coupé les lectures en plein milieu de la campagne. Les
causes réelles étaient chez nous et sont corrigées — payouts répétés à
l'identique, synchronisations qui ne poussaient rien, ticks bruts à 97,6 % du
volume — mais une base dont le fournisseur peut bloquer les lectures à la
frontière d'un quota ne convient pas à une collecte de quatorze jours. Neon
donne du PostgreSQL sans limite de lignes écrites, et du vrai SQL.

**Ce que cette couche prétend faire, et pas plus.** Elle donne au reste du code
un objet qui se comporte comme une `sqlite3.Connection` : `execute`, `commit`,
`rollback`, `close`, et un curseur qui répond à `fetchone`/`fetchall`. Rien
au-dessus ne sait quel moteur tourne. La traduction du SQL est isolée dans
`store/dialecte.py`, dont la surface est énumérée.

--- Deux différences de comportement qu'il faut connaître -------------------

**La connexion est en validation automatique.** Ce n'était pas le cas au
départ, et cela a coûté la sonde. psycopg ouvre une transaction à la PREMIÈRE
instruction, y compris un `SELECT`, et la garde ouverte jusqu'au `commit()`. La
sonde ne fait que lire : sa transaction restait donc ouverte indéfiniment, et
PostgreSQL a fini par couper — « terminating connection due to
idle-in-transaction timeout ». La sonde est passée au rouge pendant que la
collecte écrivait normalement.

En validation automatique, chaque instruction est autonome et rien ne traîne.
Les migrations, elles, ont besoin d'être atomiques : elles ouvrent un `BEGIN`
explicite, exactement comme sur `sqlite3`. Le régime est donc le même que celui
de SQLite, et `store/db.py` n'a pas de cas particulier à connaître.

**Une transaction avortée refuse tout.** Après une erreur SQL, PostgreSQL
rejette chaque instruction suivante avec « current transaction is aborted »
jusqu'au `rollback()`. Une écriture ratée — un tick malformé, une contrainte —
rendrait donc la connexion inutilisable pour tout le reste de la collecte. Le
curseur fait donc le `rollback()` lui-même avant de laisser remonter l'erreur :
l'appelant retrouve une connexion en état de marche, et voit la vraie cause.
"""

from __future__ import annotations

import logging
import os

from maxprofit.core.errors import BotError
from maxprofit.store import dialecte

log = logging.getLogger(__name__)

ENV_URL = "DATABASE_URL"

#: Délai d'établissement. Généreux : une base serverless peut être en veille et
#: demander quelques secondes pour se réveiller. Le confondre avec une panne
#: ferait boucler le collecteur sur son propre démarrage.
DELAI_CONNEXION_SEC = 30


class PostgresIndisponible(BotError):
    """PostgreSQL est demandé mais inutilisable."""


def configure() -> bool:
    """PostgreSQL est-il demandé ?

    L'URL seule en décide. Pas de repli silencieux vers un fichier local : ce
    repli donnerait exactement le comportement qu'on cherche à éviter — une
    collecte qui tourne et disparaît au redémarrage suivant.
    """
    url = os.environ.get(ENV_URL, "").strip()
    if not url:
        return False
    if not url.startswith(("postgresql://", "postgres://")):
        raise PostgresIndisponible(
            f"{ENV_URL} ne commence pas par postgresql:// — c'est l'URL de "
            f"connexion qu'il faut copier, pas le nom du projet."
        )
    return True


class _Curseur:
    """Enveloppe un curseur psycopg pour ressembler à celui de `sqlite3`."""

    def __init__(self, curseur):
        self._c = curseur

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    @property
    def rowcount(self) -> int:
        return self._c.rowcount

    def __iter__(self):
        return iter(self._c)


class _CurseurVide:
    """Réponse à une instruction ignorée — un `PRAGMA`, par exemple.

    Rendre `None` obligerait chaque appelant à savoir qu'une instruction peut
    ne pas s'exécuter. Un curseur vide se comporte comme un curseur.
    """

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    @property
    def rowcount(self) -> int:
        return 0

    def __iter__(self):
        return iter(())


class Connexion:
    """Ce que le reste du code croit être une `sqlite3.Connection`."""

    def __init__(self, brute):
        self._conn = brute

    def execute(self, sql: str, params=()):
        if dialecte.est_pragma(sql):
            # Aucun équivalent, et aucune conséquence : les PRAGMA du projet
            # règlent le journal et les délais d'attente de SQLite.
            return _CurseurVide()
        traduit = dialecte.vers_postgres(sql)
        curseur = self._conn.cursor()
        try:
            curseur.execute(traduit, tuple(params) if params else None)
        except Exception:
            # Sans ce rollback, PostgreSQL refuserait TOUTE instruction
            # suivante avec « current transaction is aborted » : une seule
            # écriture ratée condamnerait la connexion pour le reste de la
            # collecte, et le message utile serait remplacé par celui-là.
            try:
                self._conn.rollback()
            except Exception:                            # noqa: BLE001
                pass
            raise
        return _Curseur(curseur)

    @property
    def in_transaction(self) -> bool:
        """Une transaction explicite est-elle en cours ?

        Lu par `store/db._annuler` : tenter un `ROLLBACK` hors transaction
        masquerait l'erreur d'origine par un message sans rapport.
        """
        try:
            import psycopg

            return (self._conn.info.transaction_status
                    != psycopg.pq.TransactionStatus.IDLE)
        except Exception:                                # noqa: BLE001
            return False

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception as erreur:                      # noqa: BLE001
            log.debug("Fermeture sans effet : %s", erreur)


def ouvrir() -> Connexion:
    """Ouvre la connexion à PostgreSQL. Lève `PostgresIndisponible`."""
    url = os.environ.get(ENV_URL, "").strip()
    try:
        import psycopg
    except ImportError as erreur:
        raise PostgresIndisponible(
            f"Le pilote psycopg n'est pas installé ({erreur}). "
            f"pip install -r requirements.txt, ou retirez {ENV_URL} pour "
            f"revenir au stockage local."
        ) from None

    try:
        brute = psycopg.connect(url, connect_timeout=DELAI_CONNEXION_SEC,
                                autocommit=True)
    except Exception as erreur:                          # noqa: BLE001
        raise PostgresIndisponible(
            f"Connexion à PostgreSQL impossible : {erreur}. Vérifiez "
            f"{ENV_URL} — sur Neon, le mot de passe se régénère depuis "
            f"« Connection Details »."
        ) from None

    log.info("PostgreSQL ouvert (%s)", _sans_secret(url))
    return Connexion(brute)


def _sans_secret(url: str) -> str:
    """L'URL journalisable : hôte et base, jamais le mot de passe."""
    try:
        apres = url.split("://", 1)[1]
        hote = apres.split("@")[-1]
        return hote.split("?")[0]
    except (IndexError, AttributeError):
        return "url illisible"


def est_postgres(conn) -> bool:
    return isinstance(conn, Connexion)
