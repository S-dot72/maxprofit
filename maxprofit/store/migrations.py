"""
Migrations versionnées, en avant seulement (spec §1.3).

Deux règles gouvernent ce fichier, et elles sont plus importantes que son
contenu :

**Une migration est immuable.** Une fois qu'elle a tourné en production, on ne
la modifie plus — on en ajoute une nouvelle. Modifier une migration déjà
appliquée crée deux bases différentes portant le même numéro de schéma, et rien
ne le signale : la divergence se découvre le jour où une requête renvoie des
colonnes absentes chez l'un et présentes chez l'autre.

**Une migration ne détruit rien.** Sont autorisés : `CREATE TABLE IF NOT
EXISTS`, `ALTER TABLE ... ADD COLUMN`, `CREATE INDEX IF NOT EXISTS`, et la copie
table → nouvelle table → renommage. Jamais de suppression de colonne, jamais de
recréation « pour repartir propre ». Une colonne devenue inutile est laissée en
place : son coût est nul, celui de trois semaines de collecte perdues ne l'est
pas.

Conventions de nommage des colonnes portant un instant : suffixe `_sec` ou
`_ms`, en UTC, sans exception (spec §5). Ces noms sont fixés ici parce qu'aucune
donnée n'existe encore en production — les corriger plus tard imposerait une
migration de copie de table sur plusieurs millions de lignes.
"""

from __future__ import annotations

from typing import Callable, NamedTuple


class Migration(NamedTuple):
    """Une étape de schéma. `version` est le numéro que portera
    `PRAGMA user_version` une fois l'étape appliquée."""

    version: int
    label: str
    apply: Callable[[object], None]


def _decouper(script: str) -> list[str]:
    """Découpe un script SQL en instructions. Aucune des instructions de ce
    module ne contient de point-virgule littéral, ce découpage naïf suffit et
    reste lisible ; une migration qui en aurait besoin devra s'écrire en
    appels `conn.execute` explicites."""
    return [i.strip() for i in script.split(";") if i.strip()]


def _v1_tables_de_marche(conn) -> None:
    """Tables de marché : ce que le collecteur enregistre, et rien d'autre.

    Les tables de la boucle d'apprentissage (`evaluations`, `outcomes`,
    `experiments`, `model_registry`) ne sont volontairement PAS créées ici. Une
    table créée « en prévision » fige un format avant qu'on sache ce qu'on y
    mettra, et une colonne née d'une supposition coûte une migration.
    """
    # `executescript` est volontairement évité : il émet un COMMIT implicite
    # avant de s'exécuter, ce qui viderait la transaction ouverte par le
    # moteur de migration et rendrait le ROLLBACK impossible en cas de panne
    # au milieu. Chaque instruction est donc exécutée séparément, dans la
    # transaction du moteur.
    for instruction in _decouper(
        """
        CREATE TABLE IF NOT EXISTS ticks (
            pair    TEXT    NOT NULL,
            ts_ms   INTEGER NOT NULL,      -- horloge SERVEUR, millisecondes UTC
            price   REAL    NOT NULL,
            PRIMARY KEY (pair, ts_ms)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS candles (
            pair        TEXT    NOT NULL,
            tf_sec      INTEGER NOT NULL,  -- 60 pour la M1
            ts_sec      INTEGER NOT NULL,  -- DÉBUT de bougie, secondes UTC
            open        REAL    NOT NULL,
            high        REAL    NOT NULL,
            low         REAL    NOT NULL,
            close       REAL    NOT NULL,
            tick_count  INTEGER NOT NULL,  -- < 5 => bougie peu fiable (§2.4)
            complete    INTEGER NOT NULL,  -- 1 = minute entièrement observée
            PRIMARY KEY (pair, tf_sec, ts_sec)
        ) WITHOUT ROWID;

        -- Historique COMPLET des payouts, paires fermées incluses. Sans lui,
        -- impossible de rejouer l'éligibilité telle qu'elle était à l'instant T
        -- et le backtest utilise le payout d'aujourd'hui pour un trade d'il y a
        -- deux semaines : biais silencieux qui gonfle les résultats (§2.3).
        CREATE TABLE IF NOT EXISTS payouts (
            ts_sec      INTEGER NOT NULL,
            pair        TEXT    NOT NULL,
            payout_pct  INTEGER NOT NULL,
            is_open     INTEGER NOT NULL,
            PRIMARY KEY (ts_sec, pair)
        ) WITHOUT ROWID;

        -- Battements de coeur. Un trou dans les données doit être identifiable
        -- comme « bot déconnecté » et non comme « marché immobile ».
        CREATE TABLE IF NOT EXISTS uptime (
            ts_sec   INTEGER PRIMARY KEY,
            n_pairs  INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ticks_ts   ON ticks(ts_ms);
        CREATE INDEX IF NOT EXISTS idx_candles_ts ON candles(ts_sec);
        CREATE INDEX IF NOT EXISTS idx_payouts_pair ON payouts(pair, ts_sec);
        """
    ):
        conn.execute(instruction)


def _v2_etat_broker(conn) -> None:
    """Mémoire des tentatives de connexion au broker, à travers les
    redémarrages.

    Le processus meurt quand le broker est injoignable — c'est la seule façon
    d'arrêter le thread de la bibliothèque, qui compose toutes les dix secondes.
    Mais l'hébergeur relance aussitôt, et l'on rappelle le broker quarante
    secondes plus tard. Ce cycle EMPÊCHE une limitation de débit d'expirer :
    on se maintient soi-même en pénitence.

    Un compteur en mémoire ne servirait à rien, puisqu'il meurt avec le
    processus. Il faut donc une trace durable, et ce n'est pas de la donnée de
    marché — mais c'est la seule base qui survive à un redéploiement.
    """
    for instruction in _decouper(
        """
        CREATE TABLE IF NOT EXISTS etat_broker (
            id                    INTEGER PRIMARY KEY CHECK (id = 1),
            echecs_consecutifs    INTEGER NOT NULL DEFAULT 0,
            dernier_echec_ts_sec  INTEGER,
            dernier_succes_ts_sec INTEGER,
            derniere_raison       TEXT
        );

        INSERT INTO etat_broker (id, echecs_consecutifs)
        VALUES (1, 0) ON CONFLICT DO NOTHING;
        """
    ):
        conn.execute(instruction)


#: Liste ordonnée et immuable. Ajouter une migration = ajouter une ligne à la
#: fin, jamais modifier une ligne existante.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "tables de marché", _v1_tables_de_marche),
    Migration(2, "état des connexions au broker", _v2_etat_broker),
)

#: Version de schéma que ce code sait produire.
SCHEMA_VERSION: int = MIGRATIONS[-1].version
