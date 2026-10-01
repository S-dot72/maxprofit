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
import time
import uuid
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
    -- Les expirations. ⚠ Les deux horodatages ci-dessous sont dans l'HORLOGE
    -- DU BROKER, qui avance de deux heures sur l'UTC (mesuré, et documenté
    -- dans collect/pocketoption.py). Les comparer à un horodatage local donne
    -- « +7202 s d'écart » là où le contrat a duré 60 s exactement. C'est
    -- arrivé au tout premier ordre passé.
    expiration_sec     INTEGER NOT NULL,   -- demandée
    ouverture_ts_ms    INTEGER,            -- openTimestamp, horloge broker
    expiration_ts_ms   INTEGER,            -- closeTimestamp, horloge broker
    -- De combien l'horloge du broker avance sur la nôtre, au moment de
    -- l'ordre. C'est ce qui rend les deux mondes comparables.
    decalage_broker_ms INTEGER,
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
    ouverture_ts_ms: int | None = None
    expiration_ts_ms: int | None = None
    decalage_broker_ms: int | None = None
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
        """E4 : durée réellement tenue moins durée demandée.

        ⚠ Les DEUX bornes viennent de l'horloge du broker. La première version
        comparait `closeTimestamp` (horloge broker) à notre horodatage
        d'acceptation (horloge locale) et annonçait **+7202 s d'écart** sur un
        contrat qui avait duré 60 s exactement : l'horloge du broker avance de
        deux heures, ce que le collecteur documente depuis longtemps et que
        j'avais oublié ici.

        Une différence entre deux instants de la MÊME horloge est immunisée
        contre son décalage, quel qu'il soit et même s'il change.
        """
        if self.expiration_ts_ms is None or self.ouverture_ts_ms is None:
            return None
        return (self.expiration_ts_ms - self.ouverture_ts_ms) / 1000 \
            - self.expiration_sec

    @property
    def attente_ouverture_sec(self) -> float | None:
        """Entre « le broker accepte » et « l'option commence ».

        Découverte au premier ordre : le contrat s'est ouvert **2,3 s** après
        l'acceptation. Ce n'est ni de la latence réseau ni du glissement, c'est
        un troisième délai que je n'avais pas prévu — et sur une échéance de
        30 s, il vaut 8 % du contrat.

        Seule mesure qui traverse les deux horloges, d'où `decalage_broker_ms`.
        """
        if (self.ouverture_ts_ms is None or self.accepte_ts_ms is None
                or self.decalage_broker_ms is None):
            return None
        return (self.ouverture_ts_ms - self.decalage_broker_ms
                - self.accepte_ts_ms) / 1000


#: Le motif d'une ligne réservée et jamais complétée : un ordre PEUT-ÊTRE
#: parti, dont on ne sait rien.
RESERVE = "réservé : envoi en cours"
PREFIXE_RESERVATION = "reserve-"


class JournalExecution:
    """Le journal durable. Une base SQLite locale, comme le registre."""

    def __init__(self, cible: "Path | str | object", campagne: str):
        """`cible` : un CHEMIN (SQLite local) ou une CONNEXION déjà migrée.

        Les deux existent pour une raison, pas par commodité :

        - un chemin sert aux tests et aux outils locaux ; la table est créée
          à la volée et le fichier se jette ;
        - une connexion sert à la course réelle, qui écrit dans la base
          PostgreSQL de production. Là, la table vient de la migration v5 :
          la créer ici la ferait exister en deux endroits, et les deux
          finiraient par diverger.

        Le disque de l'hébergeur étant éphémère, un chemin en production
        perdrait la course au premier redémarrage. Le choix n'est donc pas
        « SQLite ou PostgreSQL » mais « jetable ou durable ».
        """
        if not campagne or not campagne.strip():
            raise BotError("Une campagne d'exécution sans nom ne se relit pas.")
        self.campagne = campagne.strip()
        if isinstance(cible, (str, Path)):
            self.conn = sqlite3.connect(str(cible))
            self.conn.executescript(SCHEMA)
            self._ajouter_les_colonnes_manquantes()
            self.conn.commit()
            self._proprietaire = True
        else:
            self.conn = cible
            self._proprietaire = False

    def _ajouter_les_colonnes_manquantes(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` n'ajoute rien à une table qui existe.

        Un journal ouvert avant l'ajout de `ouverture_ts_ms` resterait donc
        sans la colonne, et l'INSERT échouerait — ou pire, on effacerait le
        fichier pour « repartir propre », en jetant des ordres réellement
        passés. Même règle que les migrations de la base de marché : on ajoute,
        on ne détruit jamais.
        """
        presentes = {ligne[1] for ligne in
                     self.conn.execute("PRAGMA table_info(executions)")}
        for nom, type_sql in (("ouverture_ts_ms", "INTEGER"),
                              ("decalage_broker_ms", "INTEGER")):
            if nom not in presentes:
                self.conn.execute(
                    f"ALTER TABLE executions ADD COLUMN {nom} {type_sql}")

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
                    ouverture_ts_ms, expiration_ts_ms, decalage_broker_ms,
                    accepte, refus, resultat, profit, order_id, brut)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.campagne, ex.pair, ex.sens, ex.mise, ex.signal_ts_ms,
             ex.clic_ts_ms, ex.accepte_ts_ms, ex.prix_attendu, ex.prix_entree,
             ex.prix_sortie, ex.payout_flux_pct, ex.payout_broker_pct,
             ex.expiration_sec, ex.ouverture_ts_ms, ex.expiration_ts_ms,
             ex.decalage_broker_ms, 1 if ex.accepte else 0,
             ex.refus, ex.resultat, ex.profit, ex.order_id,
             json.dumps(ex.brut, ensure_ascii=False, default=str)),
        )
        self.conn.commit()
        # `lastrowid` est une notion SQLite : psycopg rend None. On ne s'en
        # sert que pour tracer, jamais pour décider — rendre 0 plutôt que
        # lever garde le journal utilisable sur les deux moteurs.
        return int(getattr(curseur, "lastrowid", 0) or 0)

    def reserver(self, pair: str, sens: str, mise: float,
                 expiration_sec: int) -> str:
        """Inscrit l'ordre AVANT qu'il ne parte. Rend le jeton de la ligne.

        ⚠ AUCUN ORDRE NE PART SANS SA LIGNE. L'ordre était écrit APRÈS son
        départ : quand l'écriture a échoué (clé dupliquée, 2026-09-28), deux
        pas de 3,31 $ ont couru chez le broker sans trace, et le second est
        parti une minute après le premier, avant son dénouement — la course,
        relancée, ignorait le premier. Désormais, si cette ligne ne s'écrit
        pas, l'ordre n'est pas envoyé ; et une réservation dont l'issue est
        inconnue bloque tout nouvel ordre.
        """
        jeton = f"{PREFIXE_RESERVATION}{uuid.uuid4().hex}"
        maintenant = int(time.time() * 1000)
        self.conn.execute(
            """INSERT INTO executions
                   (campagne, pair, sens, mise, signal_ts_ms, clic_ts_ms,
                    prix_attendu, payout_flux_pct, expiration_sec, accepte,
                    refus, order_id, brut)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.campagne, pair, sens, mise, maintenant, maintenant, 0.0,
             0.0, expiration_sec, 0, RESERVE, jeton, "{}"))
        self.conn.commit()
        return jeton

    def completer(self, jeton: str, ex: Execution) -> None:
        """Remplace la réservation par l'ordre tel qu'il est parti."""
        self.conn.execute(
            """UPDATE executions
               SET pair = ?, sens = ?, mise = ?, signal_ts_ms = ?,
                   clic_ts_ms = ?, accepte_ts_ms = ?, prix_attendu = ?,
                   prix_entree = ?, prix_sortie = ?, payout_flux_pct = ?,
                   payout_broker_pct = ?, expiration_sec = ?,
                   ouverture_ts_ms = ?, expiration_ts_ms = ?,
                   decalage_broker_ms = ?, accepte = ?, refus = ?,
                   resultat = ?, profit = ?, order_id = ?, brut = ?
               WHERE campagne = ? AND order_id = ?""",
            (ex.pair, ex.sens, ex.mise, ex.signal_ts_ms, ex.clic_ts_ms,
             ex.accepte_ts_ms, ex.prix_attendu, ex.prix_entree,
             ex.prix_sortie, ex.payout_flux_pct, ex.payout_broker_pct,
             ex.expiration_sec, ex.ouverture_ts_ms, ex.expiration_ts_ms,
             ex.decalage_broker_ms, 1 if ex.accepte else 0, ex.refus,
             ex.resultat, ex.profit, ex.order_id or jeton,
             json.dumps(ex.brut, ensure_ascii=False, default=str),
             self.campagne, jeton))
        self.conn.commit()

    def reservations(self) -> list[tuple[str, int, str, str, float]]:
        """(jeton, instant, paire, sens, mise) des ordres PEUT-ÊTRE partis :
        réservés, jamais complétés."""
        return [(str(r[0]), int(r[1]), str(r[2]), str(r[3]), float(r[4]))
                for r in self.conn.execute(
                    "SELECT order_id, clic_ts_ms, pair, sens, mise "
                    "FROM executions WHERE campagne = ? AND refus = ?",
                    (self.campagne, RESERVE)).fetchall()]

    def annuler_reservation(self, jeton: str, motif: str) -> None:
        """L'ordre n'est pas parti : la ligne le dit, et ne bloque plus."""
        self.conn.execute(
            "UPDATE executions SET refus = ? WHERE campagne = ? "
            "AND order_id = ?", (motif, self.campagne, jeton))
        self.conn.commit()

    def order_ids(self) -> set[str]:
        """Les identifiants broker déjà au journal, toutes campagnes."""
        return {str(r[0]) for r in self.conn.execute(
            "SELECT order_id FROM executions WHERE order_id IS NOT NULL"
        ).fetchall()}

    def mettre_a_jour(self, ex: Execution) -> None:
        """Complète un ordre DÉJÀ écrit, une fois son sort connu.

        L'ordre est journalisé dès qu'il part, avant même son dénouement :
        entre les deux il s'écoule un quart d'heure, et tout peut arriver —
        un redéploiement, une mise en veille, une coupure. Un ordre exécuté
        chez le broker et absent de nos livres est la pire des situations :
        le solde réel et le solde du plan divergent en silence.
        """
        if ex.order_id is None:
            raise BotError("Mise à jour sans order_id : rien à retrouver.")
        self.conn.execute(
            """UPDATE executions
               SET accepte_ts_ms = ?, prix_entree = ?, prix_sortie = ?,
                   payout_broker_pct = ?, ouverture_ts_ms = ?,
                   expiration_ts_ms = ?, decalage_broker_ms = ?,
                   resultat = ?, profit = ?, brut = ?
               WHERE campagne = ? AND order_id = ?""",
            (ex.accepte_ts_ms, ex.prix_entree, ex.prix_sortie,
             ex.payout_broker_pct, ex.ouverture_ts_ms, ex.expiration_ts_ms,
             ex.decalage_broker_ms, ex.resultat, ex.profit,
             json.dumps(ex.brut, ensure_ascii=False, default=str),
             self.campagne, ex.order_id),
        )
        self.conn.commit()

    def en_vol(self) -> list[Execution]:
        """Les ordres ACCEPTÉS dont on ne connaît pas encore le sort.

        C'est ce qu'on relit au démarrage : un ordre parti juste avant un
        redéploiement s'est dénoué chez le broker pendant qu'on était mort.
        Sans cette relecture, il resterait hors des comptes pour toujours.
        """
        # Filtré en SQL : la course l'interroge à CHAQUE passage, et relire
        # tout le journal (réponses brutes du broker comprises) coûterait de
        # plus en plus cher à mesure que la campagne avance.
        return self._lire("AND accepte = 1 AND resultat IS NULL")

    def inconnus(self) -> list[Execution]:
        """Les ordres acceptés dont l'issue a été notée INCONNUE.

        Leur mise est comptée perdue. Le broker peut pourtant connaître leur
        issue : un ordre placé par une autre instance, que `check_win` ne
        connaissait pas, a ainsi été noté « unknown » alors qu'il avait gagné.
        """
        return self._lire("AND accepte = 1 AND resultat IS NOT NULL "
                          "AND resultat NOT IN ('win', 'loose', 'draw')")

    def profits_du_plan(self) -> float:
        """Ce que les ordres de la campagne ont rapporté, en une ligne.

        Un ordre REFUSÉ ne compte pas : rien n'est sorti du compte.

        ⚠ Un ordre PLACÉ mais pas encore dénoué — ou dénoué en « unknown » —
        compte pour MOINS SA MISE. L'argent est sorti du compte au clic et ne
        revient qu'à l'échéance ; ne rien compter afficherait, pendant les
        quinze minutes de l'option, un solde que le compte n'a pas. Un
        gagnant rend la mise au dénouement avec son gain ; un « unknown » la
        laisse retirée, la seule lecture qui ne promette rien.
        """
        ligne = self.conn.execute(
            """SELECT COALESCE(SUM(CASE
                          WHEN resultat IN ('win', 'loose', 'draw')
                          THEN COALESCE(profit, 0)
                          ELSE -mise END), 0)
               FROM executions WHERE campagne = ? AND accepte = 1""",
            (self.campagne,)).fetchone()
        return float(ligne[0])

    def toutes(self, autres_campagnes: tuple[str, ...] = ()) -> list[Execution]:
        """Les ordres de la campagne, et ceux d'`autres_campagnes` avant eux.

        Chaque lancement depuis Telegram ouvre une campagne neuve — c'est ce
        qui remet le solde du plan au capital choisi. Les taux de réussite
        par paire, eux, ne doivent pas repartir de zéro à chaque réglage :
        on relit donc aussi les campagnes précédentes de la même stratégie.
        """
        return self._lire(autres_campagnes=autres_campagnes)

    def _lire(self, condition: str = "",
              autres_campagnes: tuple[str, ...] = ()) -> list[Execution]:
        noms = (self.campagne,
                *dict.fromkeys(c for c in autres_campagnes
                               if c and c != self.campagne))
        marques = ", ".join("?" * len(noms))
        # `prix_attendu > 0` écarte les réservations jamais complétées : ce ne
        # sont pas des ordres, seulement la trace d'un envoi tenté.
        lignes = self.conn.execute(
            f"""SELECT pair, sens, mise, signal_ts_ms, prix_attendu,
                      payout_flux_pct, expiration_sec, clic_ts_ms,
                      accepte_ts_ms, accepte, refus, order_id, prix_entree,
                      prix_sortie, payout_broker_pct, expiration_ts_ms,
                      resultat, profit, brut, ouverture_ts_ms,
                      decalage_broker_ms
               FROM executions WHERE campagne IN ({marques})
               AND prix_attendu > 0
               {condition}
               ORDER BY id""",
            noms).fetchall()
        return [
            Execution(
                pair=r[0], sens=r[1], mise=r[2], signal_ts_ms=r[3],
                prix_attendu=r[4], payout_flux_pct=r[5], expiration_sec=r[6],
                clic_ts_ms=r[7], accepte_ts_ms=r[8], accepte=bool(r[9]),
                refus=r[10], order_id=r[11], prix_entree=r[12],
                prix_sortie=r[13], payout_broker_pct=r[14],
                expiration_ts_ms=r[15], resultat=r[16], profit=r[17],
                brut=json.loads(r[18]), ouverture_ts_ms=r[19],
                decalage_broker_ms=r[20],
            )
            for r in lignes
        ]

    def close(self) -> None:
        # On ne ferme que ce qu'on a ouvert. Fermer une connexion prêtée
        # couperait la base sous les pieds de l'appelant — ici, le collecteur
        # de la course, qui s'en sert encore.
        if self._proprietaire:
            self.conn.close()

    def __enter__(self) -> "JournalExecution":
        return self

    def __exit__(self, *_) -> None:
        self.close()
