"""
Le plan, joué en DÉMO sur les signaux de l'hypothèse pré-inscrite.

--- ⚠ Ce que cette course peut établir, et ce qu'elle ne peut pas ---------

Elle **ne peut pas** établir que l'hypothèse gagne. Il faudrait 2 071 signaux
pour trancher 57,9 % à 3 sigma ; dix jours en produiront ~180. Une course
bénéficiaire ne prouverait rien, et une course perdante non plus.

Elle **peut** établir que la chaîne tient : signal -> dimensionnement ->
ordre -> dénouement -> solde, avec les gardes qui se déclenchent quand il
faut. C'est la phase 18 du protocole, et c'est la seule chose qui manque
entre un backtest et de l'argent réel.

--- Un diagnostic qui arrive AVANT la fin ---------------------------------

La fréquence des sessions perdues est elle-même informative, et elle parle
bien plus vite que le solde :

    précision 57,9 %  ->  session perdue 7,5 %   ->  deux d'affilée ~10 jours
    précision 50,0 %  ->  session perdue 12,5 %  ->  deux d'affilée ~3,5 jours

Si le réancrage se déclenche dans les trois premiers jours, l'hypothèse est
probablement du bruit — et on le saura sans attendre la dixième journée.

--- Les règles, telles qu'elles ont été fixées ----------------------------

    capital 250 $, risque 1/7, échéance 15 min
    trois pas maximum : si le troisième est perdu, la session s'arrête
    deux sessions perdues d'affilée -> on se réancre sur le jour du plan
    le plus proche du solde réel, on ne court pas après le plan

--- ⚠ Pourquoi les données viennent de la BASE et non du broker -----------

Le collecteur écrit les bougies M1 closes dans la base ; ce module les relit.
Ouvrir un second flux de ticks pour les mêmes bougies ferait deux sources
d'une même vérité, qui divergeraient un jour. La connexion au broker ne sert
qu'à PLACER les ordres.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from maxprofit.core.errors import BotError
from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.types import Candle, Direction
from maxprofit.execution.courtier import CourtierDemo
from maxprofit.execution.journal import JournalExecution
from maxprofit.plan import (
    Arret,
    Echelle,
    EtatSession,
    Journee,
    PlanCapital,
    Session,
    jour_le_plus_proche,
)
from maxprofit.store.market import MarketReader
from maxprofit.strategies.zone_h1 import ZoneH1

log = logging.getLogger(__name__)

#: Le signal est daté à la clôture de la bougie. Au-delà de ce retard, on ne
#: le prend plus : la décision reposait sur un prix qui n'est plus le prix.
FRAICHEUR_MAX_SEC = 90


@dataclass
class Etat:
    """Ce qui avance pendant la course. Sérialisable pour la reprise."""

    plan: PlanCapital
    solde: float
    jour: int = 1
    journee: Journee | None = None
    session: Session | None = None
    sessions_perdues_daffilee: int = 0
    reancrages: list[tuple[int, int, float]] = field(default_factory=list)
    derniere_bougie: dict[str, int] = field(default_factory=dict)

    def ouvrir_la_journee(self) -> None:
        self.journee = Journee(plan=self.plan, solde=self.solde)


class CoursePlanDemo:
    """Fait tourner le plan sur les signaux de `ZoneH1`, en démo."""

    def __init__(self, lecteur: MarketReader, courtier: CourtierDemo,
                 journal: JournalExecution, plan: PlanCapital,
                 paires: tuple[str, ...], strategie: ZoneH1 | None = None):
        self.lecteur = lecteur
        self.courtier = courtier
        self.journal = journal
        self.paires = paires
        self.strategie = strategie or ZoneH1()
        self.etat = Etat(plan=plan, solde=plan.capital_initial)
        self.etat.ouvrir_la_journee()
        for p in paires:
            self.courtier.suivre(p)

    # --- le signal ---------------------------------------------------------

    def _bougies(self, paire: str, n: int) -> list[Candle]:
        fin = self.lecteur.last_candle_ts_sec()
        if fin is None:
            return []
        bougies = self.lecteur.candles(paire, 60, fin - n * 60, fin + 60)
        return [b for b in bougies if b.complete]

    def chercher_un_signal(self):
        """Le premier signal frais parmi les paires suivies, ou `None`.

        Les paires sont parcourues dans un ordre FIXE et non au hasard : un
        ordre aléatoire rendrait la course non reproductible, et deux courses
        sur les mêmes données donneraient des soldes différents.
        """
        maintenant = int(time.time())
        for paire in self.paires:
            bougies = self._bougies(paire, self.strategie.p.lookback)
            if len(bougies) < 2 * self.strategie.p.fenetre_pique + 2:
                continue
            derniere = bougies[-1]
            if derniere.ts_sec <= self.etat.derniere_bougie.get(paire, 0):
                continue          # déjà évaluée
            self.etat.derniere_bougie[paire] = derniere.ts_sec
            # La bougie close à ts_sec couvre [ts_sec, ts_sec+60[. Le signal
            # est donc daté de sa FIN, et c'est de là qu'on compte la
            # fraîcheur — pas de son début.
            if maintenant - (derniere.ts_sec + 60) > FRAICHEUR_MAX_SEC:
                continue
            vue = SequenceMarketView(paire, bougies)
            signal = self.strategie.on_bar(vue)
            if signal is not None:
                return signal
        return None

    # --- la session --------------------------------------------------------

    def _echelle(self) -> Echelle:
        """L'échelle du moment, dimensionnée sur le SOLDE COURANT.

        Sur le solde et non sur le capital initial : c'est ce qui fait qu'une
        perte réduit les mises suivantes au lieu de les laisser calibrées sur
        un capital qu'on n'a plus.
        """
        gain = self.etat.plan.gain_par_session_pct / 100 * self.etat.solde
        return Echelle(payout_pct=92, gain_vise=gain)

    def _reancrer(self) -> None:
        """Deux sessions perdues d'affilée : on repart du jour le plus proche.

        On ne rattrape pas, on se réancre. Sans cela, un compte tombé au
        niveau du jour 7 continue de viser les gains du jour 12 : les mises
        restent calibrées sur un capital qu'on n'a plus.
        """
        nouveau = jour_le_plus_proche(self.etat.plan, self.etat.solde)
        log.warning(
            "Deux sessions perdues d'affilée. Réancrage : jour %d -> jour %d "
            "(solde %.2f $).", self.etat.jour, nouveau, self.etat.solde)
        self.etat.reancrages.append(
            (self.etat.jour, nouveau, self.etat.solde))
        self.etat.jour = max(1, nouveau)
        self.etat.sessions_perdues_daffilee = 0

    def jouer_un_pas(self, signal) -> None:
        """Place l'ordre du pas courant et enregistre son dénouement."""
        if self.etat.session is None:
            self.etat.session = Session(echelle=self._echelle())
        session = self.etat.session
        mise = session.mise_courante()

        sens = "call" if signal.direction is Direction.UP else "put"
        execution = self.courtier.placer(
            signal.pair, sens, self.strategie.p.expiry_sec)
        if not execution.accepte:
            log.warning("Ordre refusé (%s) : le pas n'est pas joué.",
                        execution.refus)
            self.journal.ecrire(execution)
            return
        execution.mise = mise
        execution = self.courtier.denouer(execution)
        self.journal.ecrire(execution)

        if execution.resultat not in ("win", "loose", "draw"):
            log.error("Dénouement inconnu (%s) : session INTERROMPUE plutôt "
                      "que comptée au hasard.", execution.resultat)
            session.interrompre()
            self._cloturer_session()
            return

        gagne = execution.resultat == "win"
        etat = session.enregistrer(gagne)
        log.info("pas %d/%d  %s %s  mise %.2f $  -> %s",
                 session.pas_joues, session.echelle.pas_max, signal.pair,
                 sens, mise, execution.resultat)
        if etat.terminee:
            self._cloturer_session()

    def _cloturer_session(self) -> None:
        session = self.etat.session
        if session is None:
            return
        montant = session.montant
        self.etat.solde += montant
        self.etat.journee.enregistrer(montant)
        if session.etat is EtatSession.PERDUE:
            self.etat.sessions_perdues_daffilee += 1
            log.warning(
                "%s", session.message_protection(
                    self.etat.solde + session.engage,
                    self.etat.plan.pas_avant_liquidation()))
            if self.etat.sessions_perdues_daffilee >= 2:
                self._reancrer()
        elif session.etat is EtatSession.GAGNEE:
            self.etat.sessions_perdues_daffilee = 0
        self.etat.session = None
        log.info("session %s  montant %+.2f $  solde %.2f $",
                 session.etat, montant, self.etat.solde)

    # --- la boucle ---------------------------------------------------------

    def peut_ouvrir(self) -> Arret | None:
        return self.etat.journee.peut_ouvrir_une_session()

    def tour(self) -> bool:
        """Un passage : cherche un signal, joue un pas si c'est possible.

        Rend `True` si quelque chose a été joué. Une session ENTAMÉE a la
        priorité sur un nouveau signal : la martingale doit finir sa descente
        avant qu'on en ouvre une autre, sinon deux échelles courent en même
        temps et l'exposition n'est plus celle qu'on a calculée.
        """
        if self.etat.session is None:
            arret = self.peut_ouvrir()
            if arret is not None:
                return False
        signal = self.chercher_un_signal()
        if signal is None:
            return False
        self.jouer_un_pas(signal)
        return True

    def resume(self) -> str:
        j = self.etat.journee
        return (
            f"jour {self.etat.jour}/{self.etat.plan.jours}  "
            f"solde {self.etat.solde:.2f} $  "
            f"sessions {j.sessions_jouees}/{self.etat.plan.sessions_par_jour}  "
            f"journée {j.resultat_pct:+.2f} %  "
            f"réancrages {len(self.etat.reancrages)}")


def nouveau_jour(etat: Etat) -> None:
    """Passe à la journée suivante : nouvelle `Journee`, mêmes soldes."""
    etat.jour += 1
    etat.ouvrir_la_journee()


def jour_utc(ts_sec: int | None = None) -> int:
    ts = int(time.time()) if ts_sec is None else ts_sec
    return int(datetime.fromtimestamp(ts, timezone.utc).timestamp() // 86400)
