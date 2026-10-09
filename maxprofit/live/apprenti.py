"""
Le fil qui réapprend : une fois par jour, la stratégie rejouée sur les
derniers jours collectés, et les leçons recalculées.

Une fois par JOUR, et pas après chaque perte (SPEC §3.4) : un bot qui
réapprend sur ses résultats de l'heure et agit aussitôt poursuit du bruit.

Il tourne à côté de la collecte et de la course, dans le même processus :
- sa propre connexion à la base (une connexion ne se partage pas entre fils) ;
- une paire à la fois, pour ne pas charger dix jours de douze paires en
  mémoire d'un coup ;
- une pause régulière, pour laisser la main aux autres fils.
Le résultat est posé sur l'état de la course d'une seule affectation : la
course lit l'ancien ou le nouveau, jamais un mélange.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from maxprofit.apprentissage.historique import exemples_historiques
from maxprofit.apprentissage.lecons import apprendre
from maxprofit.store.market import MarketReader

log = logging.getLogger(__name__)

PERIODE_SEC = 24 * 3600
#: Pause du rejeu toutes les 200 bougies. Elle valait 2 ms : le rejeu, qui
#: rejoue désormais plusieurs stratégies, occupait le processeur et la course
#: évaluait ses bougies jusqu'à une minute en retard. 100 ms laissent la main
#: à la course ; le rejeu, quotidien, prend simplement plus longtemps.
RESPIRATION_SEC = 0.1
#: Attente avant le premier rejeu d'une course sans apprentissage : laisser
#: la collecte et la course se connecter d'abord.
DELAI_INITIAL_SEC = 120


def jours_d_apprentissage() -> int:
    """Trente jours par défaut, soit tout l'historique collecté.

    Dix jours n'ont donné que 276 signaux : trop peu pour établir autre
    chose qu'un contexte perdant deux fois sur trois. Le coût est un rejeu
    trois fois plus long, une fois par jour.
    """
    try:
        return max(2, int(os.environ.get("JOURS_APPRENTISSAGE", "30")))
    except ValueError:
        return 30


class Apprenti:
    """Le fil de réapprentissage. Un seul par processus : voir `demarrer`."""

    _actif: "Apprenti | None" = None
    _verrou = threading.Lock()

    def __init__(self, course, ouvrir_la_base, paires):
        self.course = course
        self._ouvrir = ouvrir_la_base
        self.paires = tuple(paires)
        self._arret = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def demarrer(cls, course, ouvrir_la_base, paires) -> "Apprenti":
        """Démarre le fil, ou le rattache à la nouvelle course.

        Une course refabriquée après un échec en démarrerait sinon un de
        plus à chaque fois — autant de rejeux concurrents.
        """
        with cls._verrou:
            if cls._actif is not None and cls._actif._thread is not None \
                    and cls._actif._thread.is_alive():
                # Une course relancée sur un nouveau réglage repart d'une
                # campagne neuve, donc sans apprentissage : elle hérite de
                # celui de la précédente plutôt que d'attendre un rejeu.
                if course.etat.apprentissage is None:
                    course.etat.apprentissage = \
                        cls._actif.course.etat.apprentissage
                cls._actif.course = course
                cls._actif.paires = tuple(paires)
                return cls._actif
            apprenti = cls(course, ouvrir_la_base, paires)
            apprenti._thread = threading.Thread(
                target=apprenti._boucle, name="apprentissage", daemon=True)
            apprenti._thread.start()
            cls._actif = apprenti
            return apprenti

    def arreter(self) -> None:
        self._arret.set()

    def _prochaine_attente(self) -> float:
        actuel = self.course.etat.apprentissage
        # Un apprentissage d'avant la comparaison des échéances est refait
        # tout de suite : /echeances resterait sinon muet jusqu'à un jour.
        # Et d'avant le taux par paire : /lecons le montrerait vide.
        if actuel is None or not actuel.n or not actuel.echeances \
                or not actuel.par_paire or not actuel.simulations \
                or not actuel.laboratoire \
                or not (_avec_zone_confirmee(actuel.laboratoire)
                        or "note" in actuel.laboratoire) \
                or not _mesure_tout(actuel):
            return DELAI_INITIAL_SEC
        return max(DELAI_INITIAL_SEC,
                   actuel.cree_ts + PERIODE_SEC - time.time())

    def _boucle(self) -> None:
        while not self._arret.wait(self._prochaine_attente()):
            try:
                self.apprendre_une_fois()
            except Exception:                    # noqa: BLE001
                # Un rejeu raté n'arrête ni la course ni la collecte : les
                # leçons d'hier restent en place, on réessaiera demain.
                log.exception("Apprentissage en échec")
                self._arret.wait(3600)

    def apprendre_une_fois(self) -> None:
        course = self.course
        strategie = type(course.strategie)(course.strategie.p)
        tolerance = course.strategie.p.tolerance_pct
        from maxprofit.live.plan_demo import dans_la_plage_de_calibration

        jours = jours_d_apprentissage()
        fin = int(time.time())
        debut = fin - jours * 86400
        debut_calcul = time.monotonic()
        # Les stratégies candidates (zones H1, ZigZag, prise de liquidité,
        # supports inversés) ont toutes été jugées au laboratoire le
        # 2026-10-03 sans faire mieux que ZoneH1 : leurs rejeux, qui
        # occupaient le processeur de la course, sont retirés. Leur code
        # reste, et STRATEGIE peut encore les mettre en course.
        etendus = None
        conn = self._ouvrir()
        try:
            lecteur = MarketReader(conn)
            exemples = []
            for paire in self.paires:
                bougies = lecteur.candles(paire, 60, debut, fin)
                exemples += exemples_historiques(
                    {paire: bougies}, strategie,
                    echeance_sec=strategie.p.expiry_sec,
                    garde=lambda p, f: dans_la_plage_de_calibration(
                        tolerance, f),
                    respirer=lambda: time.sleep(RESPIRATION_SEC))
        finally:
            try:
                conn.close()
            except Exception:                    # noqa: BLE001
                pass
        ancien = course.etat.apprentissage
        # ⚠ L'APPRENTISSAGE PORTE SUR CE QUE LA COURSE JOUE.
        #
        # Le taux par paire décide des paires écartées « au taux perdant »,
        # les leçons des contextes écartés, l'autopsie de ce qui est
        # « défavorable ». Tout cela était mesuré sur ZoneH1 SANS
        # confirmation, et sur la bougie du SIGNAL, alors que la course joue
        # ZoneH1 PLUS la confirmation M1 et décide sur la bougie d'ENTRÉE :
        # - le 03/10, 41 % des bougies tombaient sur des paires écartées
        #   d'après des signaux que la course ne prend plus ;
        # - le 08/10, un CALL GBPUSD pris 5 amplitudes au-dessus de son
        #   support recevait « aucun contexte connu pour perdre » : au
        #   rejeu, la distance était mesurée au contact de la zone.
        # Les entrées confirmées, décrites à l'entrée, sont la population
        # jouée : c'est sur elle qu'on apprend.
        mode = getattr(course, "confirmation_m1", "0")
        fenetre = {"suivante": 1, "2": 2, "3": 3}.get(mode)
        if fenetre is not None:
            from maxprofit.live.laboratoire import confirmes
            nouveau = apprendre(confirmes(exemples, fenetre))
        else:
            nouveau = apprendre(exemples)
        nouveau.jours = jours
        nouveau.simulations = _simulations(
            exemples, strategie.p.expiry_sec, course.seuil_contre_heure,
            _echeances_courtes(exemples, course.seuil_contre_heure))
        from maxprofit.live.laboratoire import laboratoire
        seuil = course.seuil_contre_heure
        nouveau.laboratoire = laboratoire(
            exemples, etendus,
            (lambda e: True) if seuil is None else
            (lambda e: e.contexte.get("mouvement_heure", 0.0) >= -seuil),
            strategie.p.expiry_sec)
        nouveau.laboratoire["paires_jugees_sur"] = mode
        from maxprofit.live.experiences import experiences
        nouveau.laboratoire["experiences"] = experiences(
            exemples,
            (lambda e: True) if seuil is None else
            (lambda e: e.contexte.get("mouvement_heure", 0.0) >= -seuil),
            fenetre or 3, strategie.p.expiry_sec)
        course.etat.apprentissage = nouveau
        log.info("Apprentissage : %d signaux rejoués sur %d jours en %.0f s, "
                 "%d leçon(s) active(s).", nouveau.n, jours,
                 time.monotonic() - debut_calcul, len(nouveau.regles))
        course._prevenir(_annonce(ancien, nouveau))


def _avec_zone_confirmee(laboratoire) -> bool:
    """Faux pour un laboratoire calculé avant que les paires soient jugées
    sur ce que la course joue (confirmation M1 comprise) : il est alors
    refait au démarrage, pas dans vingt-quatre heures."""
    return "paires_jugees_sur" in laboratoire and "experiences" in laboratoire


def _echeances_courtes(exemples, seuil_contre_heure) -> dict:
    """Le plan à 3 et 5 min, demandé le 2026-10-03 : ZoneH1 et la
    confirmation M1, comme en direct (règle de l'heure en cours comprise),
    mêmes entrées, seule la sortie change. La confirmation à 15 min sert de
    repère."""
    from maxprofit.live.laboratoire import a_l_echeance, confirmes
    joues = [e for e in exemples if seuil_contre_heure is None
             or e.contexte.get("mouvement_heure", 0.0) >= -seuil_contre_heure]
    sortie = {"Confirmation M1 (15 min), comme en direct":
              (confirmes(joues), 900)}
    for sec in (180, 300):
        m = sec // 60
        sortie[f"ZoneH1 ({m} min), comme en direct"] = (
            a_l_echeance(joues, sec), sec)
        sortie[f"Confirmation M1 ({m} min), comme en direct"] = (
            confirmes(joues, echeance_sec=sec), sec)
    return sortie


def _simulations(exemples, echeance_sec, seuil_contre_heure, autres) -> dict:
    """Le plan simulé : la stratégie jouée (telle qu'en direct, puis sans la
    règle de l'heure en cours), et chacune des autres."""
    from maxprofit.live.simulation import comparer

    def bloc(liste, echeance, garder=lambda e: True):
        return {"signaux": sum(1 for e in liste if garder(e)),
                "resultats": [r.to_dict()
                              for r in comparer(liste, echeance, garder)]}

    sortie = {}
    if seuil_contre_heure is not None:
        sortie["ZoneH1 (15 min), comme en direct"] = bloc(
            exemples, echeance_sec,
            lambda e: e.contexte.get("mouvement_heure", 0.0)
            >= -seuil_contre_heure)
    sortie["ZoneH1 (15 min), sans la règle de l'heure"] = bloc(
        exemples, echeance_sec)
    for nom, (liste, echeance) in autres.items():
        sortie[nom] = bloc(liste, echeance)
    return sortie


def _mesure_tout(apprentissage) -> bool:
    """Faux si l'apprentissage date d'avant une caractéristique ajoutée
    depuis : il est alors refait au démarrage, pas dans vingt-quatre heures."""
    vues = {s.tranche.caracteristique for s in apprentissage.statistiques}
    return {"elan_30m", "mouvement_heure"} <= vues


def _annonce(ancien, nouveau) -> str:
    """Ce qui a changé depuis le dernier apprentissage, puis le résumé."""
    avant = {r.tranche.libelle() for r in (ancien.regles if ancien else ())}
    apres = {r.tranche.libelle() for r in nouveau.regles}
    changements = []
    if apres - avant:
        changements.append("🆕 " + " · ".join(sorted(apres - avant)))
    if avant - apres:
        changements.append("🗑 retirée(s), plus confirmée(s) : "
                           + " · ".join(sorted(avant - apres)))
    tete = "\n".join(changements)
    return (tete + "\n\n" if tete else "") + nouveau.resume()
