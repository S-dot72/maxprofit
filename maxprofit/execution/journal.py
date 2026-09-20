"""
Le journal d'exécution — ce que le backtest ne peut pas savoir.

--- ⚠ Les quatre suppositions que tout mon backtest a faites ---------------

Elles n'ont jamais été vérifiées, et chacune peut coûter plus que l'avantage
qu'on cherche. Le seuil à battre est 52,08 % ; une supposition qui coûte deux
points de taux de réussite tue une stratégie à 54 % sans qu'aucun backtest ne
le montre.

    E1  le payout appliqué est celui lu dans le flux au moment du signal
    E2  le prix d'entrée est la clôture de la bougie de décision
    E3  le délai signal -> clic -> acceptation ne déplace pas le prix
    E4  l'expiration tombe à la seconde demandée

Ce journal enregistre de quoi les trancher. Il ne les tranche pas lui-même :
il écrit, et `sonde` mesure.

--- Le champ qui sauve tout : `brut` ---------------------------------------

La charge utile renvoyée par le broker est stockée telle quelle, en JSON, à
côté des champs extraits. Cette bibliothèque est du reverse-engineering d'un
WebSocket non documenté : mon interprétation des noms de champs est une
hypothèse, pas un fait.

Si elle est fausse, les colonnes extraites seront fausses — mais `brut`
contiendra la vérité, et l'on pourra tout recalculer sans replacer un seul
ordre. Sans lui, une erreur de lecture coûterait toute la campagne de mesure.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maxprofit.core.errors import BotError

SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    campagne           TEXT    NOT NULL,
    pair               TEXT    NOT NULL,
    sens               TEXT    NOT NULL,   -- call | put
    mise               REAL    NOT NULL,
    -- La chronologie, en millisecondes. C'est elle qui porte E3.
    signal_ts_ms       INTEGER NOT NULL,   -- la règle a dit « entre »
    clic_ts_ms         INTEGER NOT NULL,   -- appel de buy()
    accepte_ts_ms      INTEGER,            -- réponse du broker, NULL si refus
    -- Les prix. E2 compare les deux premiers.
    prix_attendu       REAL    NOT NULL,   -- dernier tick vu au signal
    prix_entree        REAL,               -- celui que le broker retient
    prix_sortie        REAL,
    -- Les payouts. E1 compare les deux.
    payout_flux_pct    REAL    NOT NULL,   -- lu dans le flux au signal
    payout_broker_pct  REAL,               -- appliqué par le broker
    -- Les expirations. E4 compare les deux.
    expiration_sec     INTEGER NOT NULL,   -- demandée
    expiration_ts_ms   INTEGER,            -- réellement constatée
    -- Le dénouement.
    accepte            INTEGER NOT NULL,   -- 0 = le broker a refusé l'ordre
    refus              TEXT,
    resultat           TEXT,               -- win | loose | draw | unknown
    profit             REAL,
    order_id           TEXT,
    -- La charge utile telle quelle : la seule protection contre une lecture
    -- fausse des noms de champs d'une API non documentée.
    brut               TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exec_campagne ON executions(campagne);
"""


@dataclass
class Execution:
    """Un ordre, de l'intention au dénouement. Mutable : il se remplit en
    plusieurs temps, et c'est justement ce découpage qu'on mesure."""

    pair: str
    sens: str
    mise: float
    signal_ts_ms: int
    prix_attendu: float
    payout_flux_pct: float
    expiration_sec: int

    clic_ts_ms: int | None = None
    accepte_ts_ms: int | None = None
    accepte: bool = False
    refus: str | None = None
    order_id: str | None = None
    prix_entree: float | None = None
    prix_sortie: float | None = None
    payout_broker_pct: float | None = None
    expiration_ts_ms: int | None = None
    resultat: str | None = None
    profit: float | None = None
    brut: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sens not in ("call", "put"):
            raise BotError(
                f"sens doit valoir « call » ou « put » : {self.sens!r}")
        if self.mise <= 0:
            raise BotError(f"mise doit être positive : {self.mise}")
        if self.prix_attendu <= 0:
            raise BotError(f"prix_attendu invalide : {self.prix_attendu}")
        if self.expiration_sec <= 0:
            raise BotError(
                f"expiration_sec doit être positive : {self.expiration_sec}")

    # --- les écarts, qui sont tout l'objet du journal -----------------------

    @property
    def latence_ms(self) -> int | None:
        """E3 : signal -> acceptation. Ce que le backtest suppose nul."""
        if self.clic_ts_ms is None or self.accepte_ts_ms is None:
            return None
        return self.accepte_ts_ms - self.signal_ts_ms

    @property
    def glissement(self) -> float | None:
        """E2 : prix retenu moins prix attendu, SIGNÉ dans le sens du pari.

        Signé et non absolu : un glissement qui va dans notre sens n'est pas
        un coût. Confondre les deux ferait passer une exécution neutre pour
        une exécution chère, et inversement.
        """
        if self.prix_entree is None:
            return None
        ecart = self.prix_entree - self.prix_attendu
        return -ecart if self.sens == "call" else ecart

    @property
    def glissement_pct(self) -> float | None:
        g = self.glissement
        return None if g is None else 100 * g / self.prix_attendu

    @property
    def ecart_payout_pct(self) -> float | None:
        """E1 : payout appliqué moins payout lu dans le flux."""
        if self.payout_broker_pct is None:
            return None
        return self.payout_broker_pct - self.payout_flux_pct

    @property
    def ecart_expiration_sec(self) -> float | None:
        """E4 : durée réellement tenue moins durée demandée."""
        if self.expiration_ts_ms is None or self.accepte_ts_ms is None:
            return None
        return (self.expiration_ts_ms - self.accepte_ts_ms) / 1000 \
            - self.expiration_sec


class JournalExecution:
    """Le journal durable. Une base SQLite locale, comme le registre."""

    def __init__(self, chemin: Path | str, campagne: str):
        if not campagne or not campagne.strip():
            raise BotError("Une campagne d'exécution sans nom ne se relit pas.")
        self.campagne = campagne.strip()
        self.conn = sqlite3.connect(str(chemin))
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def ecrire(self, ex: Execution) -> int:
        """Enregistre un ordre, abouti OU REFUSÉ.

        Les refus comptent : un broker qui rejette un ordre sur dix change le
        nombre de trades d'une stratégie, donc son résultat. Ne garder que les
        ordres acceptés donnerait un taux de refus de zéro par construction.
        """
        if ex.clic_ts_ms is None:
            raise BotError(
                "Exécution sans horodatage de clic : elle n'a rien à mesurer. "
                "Un ordre qu'on n'a jamais tenté de passer n'est pas un refus.")
        curseur = self.conn.execute(
            """INSERT INTO executions
                   (campagne, pair, sens, mise, signal_ts_ms, clic_ts_ms,
                    accepte_ts_ms, prix_attendu, prix_entree, prix_sortie,
                    payout_flux_pct, payout_broker_pct, expiration_sec,
                    expiration_ts_ms, accepte, refus, resultat, profit,
                    order_id, brut)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.campagne, ex.pair, ex.sens, ex.mise, ex.signal_ts_ms,
             ex.clic_ts_ms, ex.accepte_ts_ms, ex.prix_attendu, ex.prix_entree,
             ex.prix_sortie, ex.payout_flux_pct, ex.payout_broker_pct,
             ex.expiration_sec, ex.expiration_ts_ms, 1 if ex.accepte else 0,
             ex.refus, ex.resultat, ex.profit, ex.order_id,
             json.dumps(ex.brut, ensure_ascii=False, default=str)),
        )
        self.conn.commit()
        return int(curseur.lastrowid)

    def toutes(self) -> list[Execution]:
        lignes = self.conn.execute(
            """SELECT pair, sens, mise, signal_ts_ms, prix_attendu,
                      payout_flux_pct, expiration_sec, clic_ts_ms,
                      accepte_ts_ms, accepte, refus, order_id, prix_entree,
                      prix_sortie, payout_broker_pct, expiration_ts_ms,
                      resultat, profit, brut
               FROM executions WHERE campagne = ? ORDER BY id""",
            (self.campagne,)).fetchall()
        return [
            Execution(
                pair=r[0], sens=r[1], mise=r[2], signal_ts_ms=r[3],
                prix_attendu=r[4], payout_flux_pct=r[5], expiration_sec=r[6],
                clic_ts_ms=r[7], accepte_ts_ms=r[8], accepte=bool(r[9]),
                refus=r[10], order_id=r[11], prix_entree=r[12],
                prix_sortie=r[13], payout_broker_pct=r[14],
                expiration_ts_ms=r[15], resultat=r[16], profit=r[17],
                brut=json.loads(r[18]),
            )
            for r in lignes
        ]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "JournalExecution":
        return self

    def __exit__(self, *_) -> None:
        self.close()
