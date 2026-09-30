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
        if actuel is None or not actuel.n or not actuel.echeances \
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
                    respirer=lambda: time.sleep(0.002))
        finally:
            try:
                conn.close()
            except Exception:                    # noqa: BLE001
                pass
        ancien = course.etat.apprentissage
        nouveau = apprendre(exemples)
        course.etat.apprentissage = nouveau
        log.info("Apprentissage : %d signaux rejoués sur %d jours en %.0f s, "
                 "%d leçon(s) active(s).", nouveau.n, jours,
                 time.monotonic() - debut_calcul, len(nouveau.regles))
        course._prevenir(_annonce(ancien, nouveau))


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
