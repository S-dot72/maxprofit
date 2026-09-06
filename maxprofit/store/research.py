"""
La base de RECHERCHE : expériences, évaluations, contrefactuels.

**Pourquoi un second fichier de base.**

Le §0 dit que le backtest n'écrit jamais dans les tables de marché, et
`open_read_only` le garantit par le descripteur de fichier. Mais le §2.6 et le
§3.1 demandent au backtest d'écrire beaucoup : une ligne par expérience, une
ligne par bougie évaluée, une ligne par contrefactuel. Les deux exigences ne
tiennent pas dans un seul fichier ouvert en lecture seule.

D'où deux bases :

    market.db     collecte. Écrite par le collecteur seul. Irremplaçable :
                  quatorze jours de collecte ne se régénèrent pas.
    research.db   analyses. Écrite par le backtest. Entièrement RECONSTRUCTIBLE
                  en rejouant les backtests sur market.db.

La séparation n'est pas cosmétique : elle rend physiquement impossible qu'un
bug d'analyse corrompe les données de marché, et elle permet de supprimer
research.db sans réfléchir quand on veut repartir propre — ce qu'on ne doit
jamais faire avec l'autre.

**Immuabilité des expériences (§2.6).**

« Non modifiable, non supprimable. » Des déclencheurs SQLite refusent tout
UPDATE et tout DELETE sur `experiments`. Ce n'est pas de la paranoïa
administrative : le compteur d'expériences est ce qui permet de savoir qu'après
soixante variantes testées, il devient normal d'en trouver quelques-unes à 58 %
par pur hasard. Un compteur qu'on peut nettoyer ne compte plus rien — et la
tentation d'effacer « les essais ratés qui polluent » arrive précisément quand
le compteur commence à dire quelque chose de désagréable.

Les métriques vivent dans une table à part, insérée à la fin. Une expérience
sans résultat est donc une exécution qui a planté : c'est une information, pas
un défaut de conception.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence

from maxprofit.core.errors import BotError
from maxprofit.core.types import Candle, Evaluation
from maxprofit.store.db import open_read_only, open_read_write
from maxprofit.store.migrations import Migration


def _v1_recherche(conn) -> None:
    """Instructions exécutées une par une : les corps de déclencheurs
    contiennent des points-virgules, que le découpage naïf de `migrations.py`
    ne saurait pas traiter."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiments (
            id             INTEGER PRIMARY KEY,
            demarre_ts_sec INTEGER NOT NULL,
            strategie      TEXT    NOT NULL,
            params_json    TEXT    NOT NULL,
            config_json    TEXT    NOT NULL,
            commit_git     TEXT    NOT NULL,
            hash_donnees   TEXT    NOT NULL,
            note           TEXT
        )
    """)
    # §2.6 : non modifiable, non supprimable. Les déclencheurs sont la seule
    # façon de le garantir même contre un `sqlite3` ouvert à la main.
    # SQLite ne concatène PAS les littéraux de chaîne adjacents comme le fait
    # Python : chaque message tient sur une seule chaîne, sans accents pour
    # rester lisible depuis n'importe quel client sqlite3.
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS experiments_non_modifiables
        BEFORE UPDATE ON experiments
        BEGIN
            SELECT RAISE(ABORT, 'Une experience est immuable (spec 2.6) : enregistrez-en une nouvelle plutot que de modifier celle-ci.');
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS experiments_non_supprimables
        BEFORE DELETE ON experiments
        BEGIN
            SELECT RAISE(ABORT, 'Une experience est indelebile (spec 2.6) : le compteur d''hypotheses testees ne vaut que s''il est complet.');
        END
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiment_results (
            experiment_id  INTEGER PRIMARY KEY REFERENCES experiments(id),
            termine_ts_sec INTEGER NOT NULL,
            metriques_json TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evaluations (
            id                  INTEGER PRIMARY KEY,
            experiment_id       INTEGER NOT NULL REFERENCES experiments(id),
            ts_ms               INTEGER NOT NULL,
            pair                TEXT    NOT NULL,
            direction_envisagee TEXT,
            conditions_json     TEXT    NOT NULL,
            features_json       TEXT    NOT NULL,
            condition_bloquante TEXT,
            signal_emis         INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outcomes (
            evaluation_id  INTEGER PRIMARY KEY REFERENCES evaluations(id),
            resultat       TEXT    NOT NULL,
            pnl            REAL,
            payout_pct     INTEGER NOT NULL,
            entry_ts_ms    INTEGER,
            entry_price    REAL,
            settle_ts_ms   INTEGER,
            settle_price   REAL,
            motif_irresolu TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_exp ON evaluations(experiment_id, ts_ms)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_bloquante "
        "ON evaluations(experiment_id, condition_bloquante)")


MIGRATIONS_RECHERCHE: tuple[Migration, ...] = (
    Migration(1, "tables de recherche", _v1_recherche),
)


def open_recherche(path: Path | str) -> sqlite3.Connection:
    return open_read_write(path, migrations=MIGRATIONS_RECHERCHE)


def open_recherche_lecture(path: Path | str) -> sqlite3.Connection:
    return open_read_only(path)


# --------------------------------------------------------------------------- #
# Traçabilité (§2.6)
# --------------------------------------------------------------------------- #

def commit_git_courant(racine: Path | None = None) -> str:
    """Commit courant, suffixé `-sale` si l'arbre de travail est modifié.

    Le suffixe compte : un backtest lancé sur du code non commité n'est pas
    reproductible, et c'est le cas le plus fréquent pendant qu'on cherche.
    Mieux vaut l'enregistrer honnêtement que prétendre le contraire.
    """
    racine = racine or Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=racine, capture_output=True,
            text=True, timeout=10, check=True,
        ).stdout.strip()
        modifie = subprocess.run(
            ["git", "status", "--porcelain"], cwd=racine, capture_output=True,
            text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "inconnu"
    return f"{commit}-sale" if modifie else commit


def hash_jeu_de_donnees(candles: Sequence[Candle]) -> str:
    """Empreinte de la série exacte utilisée.

    Deux backtests aux métriques identiques mais aux empreintes différentes
    n'ont pas tourné sur les mêmes données — ce qui arrive dès qu'un jour de
    collecte s'ajoute, et qui rendrait leur comparaison trompeuse.
    """
    h = hashlib.sha256()
    for c in candles:
        h.update(f"{c.pair}|{c.tf_sec}|{c.ts_sec}|{c.open}|{c.high}|"
                 f"{c.low}|{c.close}|{c.tick_count}|{int(c.complete)}\n".encode())
    return h.hexdigest()[:16]


def _json(valeur: Any) -> str:
    if is_dataclass(valeur) and not isinstance(valeur, type):
        valeur = asdict(valeur)
    return json.dumps(valeur, default=str, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #

class Journal:
    """Écrit les expériences, les évaluations et leurs contrefactuels."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.experiment_id: int | None = None

    def demarrer(self, *, strategie: str, params: Any, config: Any,
                 commit_git: str, hash_donnees: str,
                 note: str | None = None) -> int:
        if self.experiment_id is not None:
            raise BotError("Une expérience est déjà en cours sur ce journal")
        cur = self.conn.execute(
            """INSERT INTO experiments
                   (demarre_ts_sec, strategie, params_json, config_json,
                    commit_git, hash_donnees, note)
               VALUES (?,?,?,?,?,?,?)""",
            (int(time.time()), strategie, _json(params), _json(config),
             commit_git, hash_donnees, note),
        )
        self.experiment_id = int(cur.lastrowid)
        return self.experiment_id

    def enregistrer(self, evaluation: Evaluation, *, contrefactuel=None,
                    payout_pct: int | None = None) -> int:
        """Une ligne par bougie évaluée, signal ou non (§3.1).

        `contrefactuel` est le trade qui AURAIT eu lieu. Il est enregistré
        exactement de la même façon qu'un trade réel : c'est le point de toute
        la manoeuvre. Sans lui, on ne peut comparer les bougies retenues qu'à
        elles-mêmes.
        """
        if self.experiment_id is None:
            raise BotError("Aucune expérience démarrée")

        cur = self.conn.execute(
            """INSERT INTO evaluations
                   (experiment_id, ts_ms, pair, direction_envisagee,
                    conditions_json, features_json, condition_bloquante,
                    signal_emis)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                self.experiment_id, evaluation.ts_ms, evaluation.pair,
                evaluation.direction_envisagee.value
                if evaluation.direction_envisagee else None,
                _json([
                    {"nom": c.nom, "validee": c.validee, "valeur": c.valeur}
                    for c in evaluation.conditions
                ]),
                _json(dict(evaluation.features)),
                evaluation.condition_bloquante,
                1 if evaluation.signal_emis else 0,
            ),
        )
        evaluation_id = int(cur.lastrowid)

        if contrefactuel is not None:
            self.conn.execute(
                """INSERT INTO outcomes
                       (evaluation_id, resultat, pnl, payout_pct, entry_ts_ms,
                        entry_price, settle_ts_ms, settle_price, motif_irresolu)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    evaluation_id, contrefactuel.resultat.value, contrefactuel.pnl,
                    payout_pct if payout_pct is not None else contrefactuel.payout_pct,
                    contrefactuel.entry_ts_ms, contrefactuel.entry_price,
                    contrefactuel.settle_ts_ms, contrefactuel.settle_price,
                    contrefactuel.motif_irresolu.value
                    if contrefactuel.motif_irresolu else None,
                ),
            )
        return evaluation_id

    def terminer(self, metriques: dict) -> None:
        if self.experiment_id is None:
            raise BotError("Aucune expérience démarrée")
        self.conn.execute(
            "INSERT INTO experiment_results (experiment_id, termine_ts_sec, "
            "metriques_json) VALUES (?,?,?)",
            (self.experiment_id, int(time.time()), _json(metriques)),
        )

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


def compter_experiences(conn: sqlite3.Connection) -> dict[str, int]:
    """Le compteur d'hypothèses du §2.6.

    « Après 60 variantes testées, il devient normal d'en trouver quelques-unes
    à 58 % de réussite par pur hasard. Le compteur d'expériences est ce qui
    vous permet de le savoir. Sans lui, vous confondrez chance et découverte. »
    """
    total = conn.execute("SELECT COUNT(*) AS n FROM experiments").fetchone()["n"]
    abouties = conn.execute(
        "SELECT COUNT(*) AS n FROM experiment_results").fetchone()["n"]
    return {"total": int(total), "abouties": int(abouties),
            "interrompues": int(total) - int(abouties)}
