"""
Le registre : toutes les expériences, surtout celles qui échouent.

--- ⚠ Le problème qu'il résout, chiffré ------------------------------------

Tester 100 indicateurs × 20 seuils × 10 échéances × 9 paires, c'est 180 000
expériences. Sur du bruit pur, la meilleure affichera 56 % ou 57 %. Elle ne
vaut rien, et rien dans sa fiche ne le dira.

La correction du nombre de tests répond à ça — mais seulement si l'on corrige
par le nombre RÉEL d'expériences. Corriger par les 46 hypothèses du script du
jour, après en avoir essayé six cents la semaine d'avant, ne corrige rien du
tout : c'est une formule appliquée à un compte faux.

Le registre existe pour que ce compte soit vrai. Il est donc inutile s'il
n'enregistre que les succès.

--- La pré-inscription, et pourquoi elle est en deux temps -----------------

    id = registre.pre_enregistrer(...)     <- l'hypothèse, AVANT de la mesurer
    ... on mesure ...
    registre.conclure(id, resultat)        <- le résultat

Écrire l'hypothèse d'abord empêche la variante la plus commune du mensonge à
soi-même : regarder le résultat, puis formuler l'hypothèse qui lui va. Elle ne
se sent pas de l'intérieur — on croit sincèrement avoir eu l'idée avant.

Une expérience pré-inscrite et jamais conclue reste visible. Ce n'est pas un
défaut du registre, c'est une information : quelqu'un a lancé une mesure et ne
l'a pas rapportée.

--- Pourquoi une base à part -----------------------------------------------

`research.db` est locale et séparée de la base de marché. Les résultats de
recherche ne sont pas des données de marché : ils se rejouent, se jettent et
se recalculent, alors qu'un tick perdu l'est pour toujours. Les mêler
donnerait à la recherche un droit d'écriture sur la collecte, et ce droit
finirait par servir.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from maxprofit.core.errors import BotError
from maxprofit.research.metriques import Resultat, benjamini_hochberg

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiences (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    declaree_ts_sec   INTEGER NOT NULL,
    conclue_ts_sec    INTEGER,
    campagne          TEXT    NOT NULL,
    famille           TEXT    NOT NULL,
    nom               TEXT    NOT NULL,
    hypothese         TEXT    NOT NULL,
    univers           TEXT    NOT NULL,
    echeance_sec      INTEGER NOT NULL,
    periode_debut_sec INTEGER NOT NULL,
    periode_fin_sec   INTEGER NOT NULL,
    sur_scelle        INTEGER NOT NULL,
    parametres        TEXT    NOT NULL,
    signaux           INTEGER,
    gains             INTEGER,
    payout_moyen_pct  REAL,
    p_permutation     REAL,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_exp_campagne ON experiences(campagne);
"""


def _maintenant() -> int:
    return int(datetime.now(timezone.utc).timestamp())


@dataclass(frozen=True)
class Experience:
    """Une ligne du registre, telle qu'on la relit."""

    id: int
    campagne: str
    famille: str
    nom: str
    hypothese: str
    univers: str
    echeance_sec: int
    sur_scelle: bool
    parametres: dict[str, Any]
    resultat: Resultat | None
    p_permutation: float | None
    conclue: bool

    @property
    def p(self) -> float | None:
        return self.p_permutation


class Registre:
    """Le journal durable des expériences. Une base SQLite locale."""

    def __init__(self, chemin: Path | str, campagne: str):
        if not campagne or not campagne.strip():
            raise BotError(
                "Une campagne sans nom empêche de savoir sur quel ensemble "
                "d'expériences corriger. Nommez-la — par exemple "
                "« otc-m1-2026-09 ».")
        self.campagne = campagne.strip()
        self.conn = sqlite3.connect(str(chemin))
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- écriture -----------------------------------------------------------

    def pre_enregistrer(self, *, famille: str, nom: str, hypothese: str,
                        univers: str, echeance_sec: int,
                        periode_debut_sec: int, periode_fin_sec: int,
                        sur_scelle: bool = False,
                        parametres: dict[str, Any] | None = None) -> int:
        """Déclare une expérience AVANT de la mesurer. Rend son identifiant.

        `hypothese` est une phrase, pas une étiquette : « après trois bougies
        rouges en tendance haussière, la suivante monte » se relit dans six
        semaines ; « test_3 » non. Elle est obligatoire pour cette raison.
        """
        for champ, valeur in (("nom", nom), ("hypothese", hypothese),
                              ("famille", famille), ("univers", univers)):
            if not valeur or not str(valeur).strip():
                raise BotError(f"{champ} est obligatoire et ne peut être vide.")
        if len(hypothese.strip()) < 15:
            raise BotError(
                f"« {hypothese} » n'est pas une hypothèse, c'est une "
                f"étiquette. Écrivez la phrase que vous voulez relire dans six "
                f"semaines : ce que l'on attend, et dans quelles conditions.")
        if echeance_sec <= 0:
            raise BotError(f"echeance_sec doit être positive : {echeance_sec}")
        curseur = self.conn.execute(
            """INSERT INTO experiences
                   (declaree_ts_sec, campagne, famille, nom, hypothese,
                    univers, echeance_sec, periode_debut_sec, periode_fin_sec,
                    sur_scelle, parametres)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_maintenant(), self.campagne, famille.strip(), nom.strip(),
             hypothese.strip(), univers, echeance_sec, periode_debut_sec,
             periode_fin_sec, 1 if sur_scelle else 0,
             json.dumps(parametres or {}, sort_keys=True, ensure_ascii=False)),
        )
        self.conn.commit()
        return int(curseur.lastrowid)

    def conclure(self, experience_id: int, resultat: Resultat,
                 p_permutation: float | None = None,
                 notes: str = "") -> None:
        """Attache le résultat à une expérience déjà déclarée.

        Refuse de conclure deux fois. Une expérience reprise avec d'autres
        réglages est une expérience DIFFÉRENTE : la réécrire effacerait la
        première du compte, et c'est précisément le compte qui fait la valeur
        du registre.
        """
        ligne = self.conn.execute(
            "SELECT conclue_ts_sec FROM experiences WHERE id = ?",
            (experience_id,)).fetchone()
        if ligne is None:
            raise BotError(
                f"Expérience {experience_id} inconnue : conclure ce qui n'a "
                f"pas été déclaré contournerait la pré-inscription.")
        if ligne[0] is not None:
            raise BotError(
                f"Expérience {experience_id} déjà conclue. Une reprise avec "
                f"d'autres réglages est une expérience différente — "
                f"pré-enregistrez-la, sinon la première disparaît du compte "
                f"et la correction du nombre de tests devient fausse.")
        if p_permutation is not None and not (0 <= p_permutation <= 1):
            raise BotError(f"p_permutation hors [0,1] : {p_permutation}")
        self.conn.execute(
            """UPDATE experiences
               SET conclue_ts_sec = ?, signaux = ?, gains = ?,
                   payout_moyen_pct = ?, p_permutation = ?, notes = ?
               WHERE id = ?""",
            (_maintenant(), resultat.signaux, resultat.gains,
             resultat.payout_moyen_pct, p_permutation, notes, experience_id),
        )
        self.conn.commit()

    # --- lecture ------------------------------------------------------------

    def toutes(self, campagne: str | None = None) -> list[Experience]:
        """Toutes les expériences de la campagne, conclues OU NON."""
        cible = self.campagne if campagne is None else campagne
        lignes = self.conn.execute(
            """SELECT id, campagne, famille, nom, hypothese, univers,
                      echeance_sec, sur_scelle, parametres, signaux, gains,
                      payout_moyen_pct, p_permutation, conclue_ts_sec
               FROM experiences WHERE campagne = ? ORDER BY id""",
            (cible,)).fetchall()
        sortie = []
        for r in lignes:
            conclue = r[13] is not None
            resultat = (Resultat(signaux=int(r[9]), gains=int(r[10]),
                                 payout_moyen_pct=float(r[11]))
                        if conclue and r[9] is not None else None)
            sortie.append(Experience(
                id=int(r[0]), campagne=r[1], famille=r[2], nom=r[3],
                hypothese=r[4], univers=r[5], echeance_sec=int(r[6]),
                sur_scelle=bool(r[7]), parametres=json.loads(r[8]),
                resultat=resultat, p_permutation=r[12], conclue=conclue))
        return sortie

    def compte(self, campagne: str | None = None) -> dict[str, int]:
        """De quoi écrire la ligne qui manque à tous les rapports : combien
        d'expériences ont réellement eu lieu."""
        toutes = self.toutes(campagne)
        return {
            "declarees": len(toutes),
            "conclues": sum(1 for e in toutes if e.conclue),
            "abandonnees": sum(1 for e in toutes if not e.conclue),
            "avec_p": sum(1 for e in toutes if e.p is not None),
            "sur_scelle": sum(1 for e in toutes if e.sur_scelle),
        }

    def corriger(self, campagne: str | None = None,
                 alpha: float = 0.05) -> list[tuple[Experience, float]]:
        """Les p corrigées par TOUTES les expériences de la campagne.

        C'est la seule correction qui veuille dire quelque chose ici, et c'est
        pour elle que le registre existe. Les expériences sans p — une mesure
        descriptive, un rapport de couverture — ne sont pas corrigées mais
        restent dans le compte des `declarees` : elles n'ont pas cherché un
        avantage, donc elles n'ont pas pu en trouver un par hasard.
        """
        avec_p = [e for e in self.toutes(campagne) if e.p is not None]
        if not avec_p:
            return []
        corrigees = benjamini_hochberg([e.p for e in avec_p], alpha=alpha)
        return list(zip(avec_p, corrigees))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Registre":
        return self

    def __exit__(self, *_) -> None:
        self.close()
