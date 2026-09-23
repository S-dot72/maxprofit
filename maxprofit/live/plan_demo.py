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

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from maxprofit.core.errors import BotError
from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.payout import PLAFOND_PCT, au_plafond
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
from maxprofit.store.db import valider
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
    #: De quoi distinguer « j'attends un signal » de « je suis cassé ».
    #:
    #: Sans ces compteurs, `/etat` affichait « pas encore démarrée » aussi
    #: bien pour une course qui évalue 240 bougies par heure sans rien trouver
    #: que pour une course qui ne lit plus la base du tout. Les deux
    #: ressemblaient à du vert.
    bougies_evaluees: int = 0
    signaux_trouves: int = 0
    #: Signaux ÉCARTÉS faute de payout maximal. Comptés à part, et affichés :
    #: sans ce compteur, une course qui refuse tout pour cause de payout
    #: ressemblerait trait pour trait à une course qui ne trouve rien.
    signaux_ecartes_payout: int = 0
    derniere_evaluation_ts: int = 0

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
            self.etat.bougies_evaluees += 1
            self.etat.derniere_evaluation_ts = maintenant
            # La bougie close à ts_sec couvre [ts_sec, ts_sec+60[. Le signal
            # est donc daté de sa FIN, et c'est de là qu'on compte la
            # fraîcheur — pas de son début.
            if maintenant - (derniere.ts_sec + 60) > FRAICHEUR_MAX_SEC:
                continue
            vue = SequenceMarketView(paire, bougies)
            signal = self.strategie.on_bar(vue)
            if signal is None:
                continue
            self.etat.signaux_trouves += 1
            if not self._payout_au_maximum(paire):
                self.etat.signaux_ecartes_payout += 1
                continue
            return signal
        return None

    def _payout_au_maximum(self, paire: str) -> bool:
        """N'entrer QUE lorsque le broker paie son maximum.

        ⚠ « Payout 92 % » se lit sur le FLUX à 84, pas à 92. Le broker
        applique `min(flux + 8, 92)` — mesuré sur 26 ordres réels et confirmé
        par l'affichage de la plateforme. Filtrer sur `flux >= 92` écarterait
        42 % d'occasions qui paient exactement la même chose.

        Une indisponibilité du payout vaut REFUS. Entrer sans savoir ce qu'on
        sera payé est précisément ce que ce filtre existe pour empêcher.
        """
        try:
            flux = self.courtier.payout(paire)
        except BotError as erreur:
            log.warning("Payout indisponible sur %s : %s", paire, erreur)
            return False
        if au_plafond(flux):
            return True
        log.info("Signal écarté sur %s : payout %d %% (appliqué %d %%), "
                 "le maximum de %d %% demande un flux >= %d %%.",
                 paire, flux, min(flux + 8, PLAFOND_PCT), PLAFOND_PCT,
                 PLAFOND_PCT - 8)
        return False

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

        sens = "call" if signal.direction is Direction.CALL else "put"
        execution = self.courtier.placer(
            signal.pair, sens, self.strategie.p.expiry_sec, mise=mise)
        if not execution.accepte:
            log.warning("Ordre refusé (%s) : le pas n'est pas joué.",
                        execution.refus)
            self.journal.ecrire(execution)
            return
        execution = self.courtier.denouer(execution)
        self.journal.ecrire(execution)

        if execution.resultat not in ("win", "loose", "draw"):
            log.error("Dénouement inconnu (%s) : mise %.2f $ notée comme "
                      "engagée, session INTERROMPUE plutôt que comptée au "
                      "hasard.", execution.resultat, mise)
            # La mise est PARTIE. Interrompre sans la noter la ferait
            # disparaître du solde : l'argent serait sorti du compte sans
            # laisser de trace dans le plan.
            session.engager_sans_resoudre(mise)
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
        # UNE seule voie de mise à jour du solde. `Journee` tient le sien ;
        # en incrémenter un second ici ferait deux vérités qui divergeraient
        # au premier arrondi, et le plan serait jugé sur le mauvais.
        self.etat.journee.enregistrer(
            session.etat is EtatSession.GAGNEE, montant)
        self.etat.solde = self.etat.journee.solde
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
        e = self.etat
        if e.bougies_evaluees == 0:
            return ("connectée, aucune bougie évaluée pour l'instant — "
                    "si ça dure, c'est la LECTURE de la base qui est en cause")
        age = int(time.time()) - e.derniere_evaluation_ts
        base = (f"jour {e.jour}/{e.plan.jours}  solde {e.solde:.2f} $  "
                f"sessions {j.sessions_jouees}/{e.plan.sessions_par_jour}  "
                f"journée {j.resultat_pct:+.2f} %  "
                f"réancrages {len(e.reancrages)}")
        # Les compteurs d'activité viennent APRÈS le plan mais ils sont le
        # seul moyen de dire qu'une course sans ordre est vivante.
        return (f"{base} | {e.bougies_evaluees} bougies évaluées, "
                f"{e.signaux_trouves} signal(aux) dont "
                f"{e.signaux_ecartes_payout} écarté(s) payout, "
                f"dernière lecture il y a {age} s")


def nouveau_jour(etat: Etat) -> None:
    """Passe à la journée suivante : nouvelle `Journee`, mêmes soldes."""
    etat.jour += 1
    etat.ouvrir_la_journee()


def jour_utc(ts_sec: int | None = None) -> int:
    ts = int(time.time()) if ts_sec is None else ts_sec
    return int(datetime.fromtimestamp(ts, timezone.utc).timestamp() // 86400)


# --------------------------------------------------------------------------- #
# La persistance — sans elle, un redémarrage efface dix jours
# --------------------------------------------------------------------------- #

def sauver_etat(conn, campagne: str, etat: Etat, jour_utc_courant: int) -> None:
    """Écrit l'état complet de la course, session en cours comprise.

    ⚠ Appelée après CHAQUE pas, pas à la fin. Le processus peut mourir à
    n'importe quel moment — l'hébergeur redéploie, met en veille, redémarre —
    et un état sauvé « de temps en temps » rejouerait des ordres déjà passés
    ou en oublierait.

    La session en cours est incluse, et c'est le point délicat. Sans elle, un
    redémarrage au milieu d'une martingale repartirait au pas 1 : les mises
    déjà engagées auraient quitté le compte sans que le plan les connaisse.
    """
    session = etat.session
    conn.execute(
        """INSERT INTO plan_etat
               (campagne, maj_ts_sec, jour, solde, solde_ouverture,
                sessions_jouees, sessions_perdues_daffilee, jour_utc,
                reancrages, derniere_bougie, session_pas_joues,
                session_engagees, session_gain_vise)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(campagne) DO UPDATE SET
               maj_ts_sec = excluded.maj_ts_sec,
               jour = excluded.jour,
               solde = excluded.solde,
               solde_ouverture = excluded.solde_ouverture,
               sessions_jouees = excluded.sessions_jouees,
               sessions_perdues_daffilee = excluded.sessions_perdues_daffilee,
               jour_utc = excluded.jour_utc,
               reancrages = excluded.reancrages,
               derniere_bougie = excluded.derniere_bougie,
               session_pas_joues = excluded.session_pas_joues,
               session_engagees = excluded.session_engagees,
               session_gain_vise = excluded.session_gain_vise""",
        (campagne, int(time.time()), etat.jour, etat.solde,
         etat.journee.solde_ouverture, etat.journee.sessions_jouees,
         etat.sessions_perdues_daffilee, jour_utc_courant,
         json.dumps(etat.reancrages), json.dumps(etat.derniere_bougie),
         session.pas_joues if session else 0,
         json.dumps(session.engagees if session else []),
         session.echelle.gain_vise if session else 0.0),
    )
    valider(conn)


def charger_etat(conn, campagne: str, plan: PlanCapital) -> tuple[Etat, int] | None:
    """Relit une course interrompue. `None` s'il n'y en a pas.

    Rend aussi le jour UTC de la dernière écriture : c'est lui qui dit si la
    journée doit repartir à zéro ou continuer. Le déduire de l'horloge seule
    ferait repartir une journée entamée avec ses compteurs remis à neuf.
    """
    ligne = conn.execute(
        """SELECT jour, solde, solde_ouverture, sessions_jouees,
                  sessions_perdues_daffilee, jour_utc, reancrages,
                  derniere_bougie, session_pas_joues, session_engagees,
                  session_gain_vise
           FROM plan_etat WHERE campagne = ?""", (campagne,)).fetchone()
    if ligne is None:
        return None
    etat = Etat(plan=plan, solde=float(ligne[1]), jour=int(ligne[0]))
    etat.journee = Journee(plan=plan, solde=float(ligne[2]))
    etat.journee.solde = float(ligne[1])
    etat.journee.sessions_jouees = int(ligne[3])
    etat.sessions_perdues_daffilee = int(ligne[4])
    etat.reancrages = [tuple(x) for x in json.loads(ligne[6])]
    etat.derniere_bougie = {k: int(v) for k, v
                            in json.loads(ligne[7]).items()}
    pas, engagees, gain = int(ligne[8]), json.loads(ligne[9]), float(ligne[10])
    if pas or engagees:
        # Une session était en cours. On la reconstruit telle quelle : même
        # échelle, mêmes mises déjà engagées, même profondeur atteinte.
        session = Session(echelle=Echelle(payout_pct=92, gain_vise=gain))
        session.pas_joues = pas
        session.engagees = [float(x) for x in engagees]
        etat.session = session
    return etat, int(ligne[5])


def fabriquer_course(ssid: str, *, campagne: str, capital: float,
                     sessions_par_jour: int, jours: int,
                     paires: tuple[str, ...], chemin_lecture="lecture",
                     chemin_ecriture="ecriture"):
    """Assemble une course prête à tourner, et reprend celle en cours s'il y en a.

    ⚠ `ssid` est PASSÉ et non résolu ici. Le résoudre demanderait d'importer
    `collect`, droit que `live` n'a pas et ne doit pas avoir : la couche qui
    décide et exécute n'a rien à faire dans celle qui collecte. C'est
    l'appelant — `hosting`, dont c'est le métier d'assembler — qui le fournit.

    Rend un objet à `tour()` / `resume()`, plus la fonction qui sauve son
    état. Le superviseur appelle les deux sans rien savoir du reste.
    """
    from pathlib import Path

    from maxprofit.execution.courtier import CourtierDemo
    from maxprofit.execution.garde import Plafonds
    from maxprofit.execution.journal import JournalExecution
    from maxprofit.plan import Echelle, Risque, solde_projete
    from maxprofit.store.db import open_read_only, open_read_write
    from maxprofit.store.market import MarketReader
    from maxprofit.strategies.zone_h1 import ZoneH1

    plan = PlanCapital.depuis_risque(
        capital_initial=capital, risque=Risque(1, 7), payout_pct=92,
        sessions_par_jour=sessions_par_jour, jours=jours,
        sessions_perdues_max=2)
    # Le plafond est le 3e pas AU CAPITAL VISÉ, pas au capital initial.
    #
    # Les mises sont dimensionnées sur le solde COURANT : elles grandissent
    # avec lui. Un plafond calculé sur les 250 $ de départ aurait refusé chaque
    # ordre dès que le solde aurait dépassé ce niveau — silencieusement, en
    # abandonnant la course au bout de cinq refus. C'est exactement ce qui est
    # arrivé, à une nuance près : le plafond absolu de 10 $ a bloqué dès le
    # premier ordre.
    vise = solde_projete(plan, plan.jours)
    pire = Echelle(payout_pct=92,
                   gain_vise=vise * plan.gain_par_session_pct / 100).mises()[-1]
    plafond = round(pire * 1.1, 2)
    plafonds = Plafonds(mise=plafond, mise_max_absolue=plafond,
                        ordres_max=2000, duree_max_sec=11 * 86400)
    log.info("Plafond de mise : %.2f $ (3e pas au capital visé de %.2f $)",
             plafond, vise)

    lecteur = MarketReader(open_read_only(Path(chemin_lecture)))
    ecriture = open_read_write(Path(chemin_ecriture))
    journal = JournalExecution(ecriture, campagne=campagne)
    courtier = CourtierDemo(ssid, plafonds)
    courtier.connecter()
    course = CoursePlanDemo(lecteur, courtier, journal, plan, paires, ZoneH1())

    repris = charger_etat(ecriture, campagne, plan)
    if repris is not None:
        course.etat, _ = repris
        log.info("Course REPRISE depuis la base : %s", course.resume())
    else:
        log.info("Nouvelle course : %s", course.resume())

    tour_nu = course.tour

    def tour_persistant() -> bool:
        # L'état est sauvé après CHAQUE pas. Le processus peut mourir à
        # n'importe quel moment — l'hébergeur redéploie, met en veille — et un
        # état sauvé par intermittence rejouerait des ordres déjà passés.
        joue = tour_nu()
        if joue:
            sauver_etat(ecriture, campagne, course.etat, jour_utc())
        return joue

    course.tour = tour_persistant
    return course
