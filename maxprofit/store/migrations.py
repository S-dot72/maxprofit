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
def _v3_operateurs(conn) -> None:
    """Qui a accès au bot — en BASE, plus dans un fichier.

    L'annuaire vivait dans un JSON posé à côté de la base. Sur un hébergement
    sans disque, ce fichier disparaît à chaque déploiement : le journal affichait
    « 0 opérateur(s) inscrit(s) », les alertes n'avaient plus de destinataire, et
    il fallait renvoyer `/start <code>` après chaque mise à jour. Une alerte
    qu'on ne reçoit plus est pire qu'une alerte absente : on croit être couvert.

    Le choix contraire avait été documenté et argumenté — deux répliques libSQL
    sur le même fichier auraient été un risque de corruption pour la seule
    commodité de ne pas retaper une commande. L'argument tombe avec PostgreSQL :
    une connexion de plus n'y coûte rien.
    """
    for instruction in _decouper(
        """
        CREATE TABLE IF NOT EXISTS operateurs (
            chat_id        TEXT PRIMARY KEY,
            role           TEXT    NOT NULL,
            nom            TEXT    NOT NULL,
            inscrit_ts_sec INTEGER NOT NULL
        );
        """
    ):
        conn.execute(instruction)


def _v4_chemins_de_ticks(conn) -> None:
    """Les ticks reviennent — une ligne par MINUTE, plus une par tick.

    Les ticks avaient été coupés, et la mesure justifiait le choix :

        620 003 ticks/jour, 8 680 038 lignes sur quatorze jours
        ~1 Go en PostgreSQL index compris, contre 0,5 Go de quota

    Ce qui a changé n'est pas le quota, c'est la question posée aux données.
    Tout ce qui a été cherché à la résolution d'une minute est revenu vide —
    46 hypothèses, 1 402 conjonctions sous test de permutation, un modèle en
    validation glissante. La minute est un RÉSUMÉ de soixante secondes d'un
    processus qui bat à la seconde : on jetait environ soixante fois
    l'information avant de conclure qu'il n'y avait rien dedans.

    Une minute de ticks delta-encodée et compressée tient dans quelques
    centaines d'octets (mesuré, et tenu par un test) :

        ~70 000 lignes et ~20 Mo sur quatorze jours, SANS PERTE

    La table `ticks` de la v1 reste en place et vide : une migration ne
    détruit rien. Elle coûte 24 ko.

    `n_ticks` et `echelle` sont des colonnes à part plutôt que des entêtes
    dans le bloc : ils se lisent alors sans décompresser, ce qui rend un
    inventaire de la couverture possible sans toucher aux octets.
    """
    for instruction in _decouper(
        """
        CREATE TABLE IF NOT EXISTS tick_paths (
            pair        TEXT    NOT NULL,
            minute_sec  INTEGER NOT NULL,  -- DÉBUT de minute, secondes UTC
            n_ticks     INTEGER NOT NULL,
            echelle     INTEGER NOT NULL,  -- puissance de 10 des prix entiers
            chemin      BLOB    NOT NULL,  -- écarts successifs, compressés
            PRIMARY KEY (pair, minute_sec)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS idx_tick_paths_minute
            ON tick_paths(minute_sec);
        """
    ):
        conn.execute(instruction)


def _v5_course_du_plan(conn) -> None:
    """Le journal des ordres et l'état de la course — en base, pas en fichier.

    La course du plan devait d'abord vivre dans un SQLite local sauvegardé
    avant chaque commit. Elle n'aurait pas tenu : sur le plan gratuit de
    Render le disque est effacé à CHAQUE redémarrage et après chaque mise en
    veille, pas seulement aux déploiements. Dix jours de course auraient
    disparu au premier réveil, sans message d'erreur — le désastre de la §1.1,
    une deuxième fois.

    `executions` porte ce qu'un backtest ne peut pas savoir : le payout
    réellement appliqué, le prix d'entrée retenu, la latence, la durée tenue.
    `brut` garde la charge utile du broker telle quelle — cette API n'est pas
    documentée, et une lecture fausse des noms de champs se répare alors sans
    replacer un seul ordre.

    `plan_etat` est une ligne UNIQUE par course (`CHECK (id = 1)` côté SQLite,
    et la clé primaire ailleurs) : l'état est un singleton, et deux lignes
    voudraient dire deux courses qui se marchent dessus.
    """
    for instruction in _decouper(
        """
        CREATE TABLE IF NOT EXISTS executions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            campagne           TEXT    NOT NULL,
            pair               TEXT    NOT NULL,
            sens               TEXT    NOT NULL,
            mise               REAL    NOT NULL,
            signal_ts_ms       INTEGER NOT NULL,
            clic_ts_ms         INTEGER NOT NULL,
            accepte_ts_ms      INTEGER,
            prix_attendu       REAL    NOT NULL,
            prix_entree        REAL,
            prix_sortie        REAL,
            payout_flux_pct    REAL    NOT NULL,
            payout_broker_pct  REAL,
            expiration_sec     INTEGER NOT NULL,
            ouverture_ts_ms    INTEGER,
            expiration_ts_ms   INTEGER,
            decalage_broker_ms INTEGER,
            accepte            INTEGER NOT NULL,
            refus              TEXT,
            resultat           TEXT,
            profit             REAL,
            order_id           TEXT,
            brut               TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_exec_campagne ON executions(campagne);

        CREATE TABLE IF NOT EXISTS plan_etat (
            campagne       TEXT PRIMARY KEY,
            maj_ts_sec     INTEGER NOT NULL,
            jour           INTEGER NOT NULL,
            solde          REAL    NOT NULL,
            solde_ouverture REAL   NOT NULL,
            sessions_jouees INTEGER NOT NULL,
            sessions_perdues_daffilee INTEGER NOT NULL,
            jour_utc       INTEGER NOT NULL,
            reancrages     TEXT    NOT NULL,
            derniere_bougie TEXT   NOT NULL,
            -- La session EN COURS, et c'est le point délicat. Sans elle, un
            -- redémarrage au milieu d'une martingale repartirait au pas 1 :
            -- les mises déjà engagées auraient quitté le compte sans que le
            -- plan les connaisse, et l'échelle se serait réarmée toute seule.
            -- `session_engagees` est la liste des mises réellement placées,
            -- celle dont dépend le coût d'une session interrompue.
            session_pas_joues INTEGER NOT NULL DEFAULT 0,
            session_engagees  TEXT    NOT NULL DEFAULT '[]',
            session_gain_vise REAL    NOT NULL DEFAULT 0
        );
        """
    ):
        conn.execute(instruction)


def _v6_dernier_trade(conn) -> None:
    """Le dernier pas joué : sur quel actif, et quand.

    La règle d'indépendance en dépend — un pas de martingale ne se joue pas
    sur le même actif que le précédent ni dans les minutes qui suivent. Sans
    persistance, un redémarrage rendrait le pas suivant immédiatement
    éligible, et l'on rejouerait exactement le pari corrélé que la règle
    existe pour empêcher.

    Deux colonnes ajoutées plutôt qu'une table : `plan_etat` porte déjà tout
    l'état de la course, et le découper n'apporterait qu'une jointure.
    """
    for instruction in _decouper(
        """
        ALTER TABLE plan_etat ADD COLUMN dernier_trade_pair TEXT;
        ALTER TABLE plan_etat ADD COLUMN dernier_trade_ts_sec INTEGER;
        """
    ):
        try:
            conn.execute(instruction)
        except Exception:                        # noqa: BLE001
            # `ADD COLUMN IF NOT EXISTS` n'existe pas en SQLite. Une colonne
            # déjà présente n'est pas une panne : la migration doit pouvoir
            # se rejouer sur une base qui l'a partiellement reçue.
            pass


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "tables de marché", _v1_tables_de_marche),
    Migration(2, "état des connexions au broker", _v2_etat_broker),
    Migration(3, "annuaire des opérateurs", _v3_operateurs),
    Migration(4, "chemins de ticks compressés", _v4_chemins_de_ticks),
    Migration(5, "course du plan : ordres et état", _v5_course_du_plan),
    Migration(6, "dernier pas joué, pour la règle d'indépendance",
              _v6_dernier_trade),
)

#: Version de schéma que ce code sait produire.
SCHEMA_VERSION: int = MIGRATIONS[-1].version
