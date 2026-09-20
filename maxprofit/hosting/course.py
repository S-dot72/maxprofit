"""
La course du plan, dans un thread du service — et ses pannes CONTENUES.

--- ⚠ Pourquoi ce fichier est presque entièrement du confinement -----------

Un service séparé coûterait une instance payante. La course partage donc le
processus du collecteur, et c'est le seul vrai risque de ce montage : une
panne de la course ne doit JAMAIS arrêter la collecte. Quatorze jours de
série continue ne se rattrapent pas ; dix jours de course, si.

D'où trois règles, et elles vont toutes dans le même sens :

1. **Aucune exception ne remonte.** Le thread avale tout, sans exception —
   y compris `BotError`, y compris une erreur de programmation. Ailleurs dans
   ce projet, avaler une erreur de programmation est interdit ; ici, c'est
   l'inverse, parce que la remonter tuerait le processus qui collecte.

2. **La course s'arrête d'elle-même après trop d'échecs.** Réessayer sans fin
   une course cassée martèlerait le broker et fausserait la mesure
   d'exécution qu'on est en train de faire. `ECHECS_MAX` échecs consécutifs
   et elle rend les armes, définitivement, en le disant.

3. **Le thread est `daemon`.** Il ne peut pas retarder l'arrêt du service.

--- Ce que la course NE fait pas ------------------------------------------

Elle ne touche à aucune table de marché : sa lecture passe par un descripteur
en lecture seule, son écriture par les deux tables de la migration v5. Elle ne
peut donc pas abîmer ce que le collecteur enregistre, même en cas de bogue.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: Échecs consécutifs après lesquels la course renonce. Réessayer sans fin
#: martèlerait le broker et fausserait la mesure d'exécution en cours.
ECHECS_MAX = 5
#: Attente après un échec, doublée à chaque fois, plafonnée.
BACKOFF_SEC = 60
BACKOFF_MAX_SEC = 1800


class SuperviseurCourse:
    """Fait tourner la course du plan à côté de la collecte, sans l'exposer.

    `fabriquer` est une fonction sans argument qui rend un objet possédant
    `tour()` et `resume()`. Elle est passée plutôt qu'importée pour que ce
    module reste testable sans broker ni base.
    """

    def __init__(self, fabriquer, *, alerter=None, pause_sec: float = 20.0):
        self._fabriquer = fabriquer
        self._alerter = alerter
        self.pause_sec = pause_sec
        self._thread: threading.Thread | None = None
        self._arret = threading.Event()
        self.active = False
        self.demarrages = 0
        self.echecs_consecutifs = 0
        self.derniere_erreur: str | None = None
        self.abandonnee = False
        self._dernier_resume = "pas encore démarrée"

    # --- cycle de vie -------------------------------------------------------

    def demarrer(self) -> None:
        if self._thread is not None:
            return
        self.active = True
        self._thread = threading.Thread(
            target=self._boucle, name="course-plan", daemon=True)
        self._thread.start()
        log.info("Course du plan : thread démarré.")

    def arreter(self) -> None:
        self._arret.set()
        self.active = False

    # --- la boucle, et son confinement --------------------------------------

    def _boucle(self) -> None:
        attente = BACKOFF_SEC
        while not self._arret.is_set() and not self.abandonnee:
            try:
                self.demarrages += 1
                course = self._fabriquer()
                self.echecs_consecutifs = 0
                attente = BACKOFF_SEC
                while not self._arret.is_set():
                    if course.tour():
                        self._dernier_resume = course.resume()
                    else:
                        self._arret.wait(self.pause_sec)
                return
            except BaseException as erreur:      # noqa: BLE001
                # TOUT est avalé, y compris ce qui serait fatal ailleurs.
                # Laisser remonter tuerait le processus qui collecte, et la
                # collecte vaut plus que la course.
                self.echecs_consecutifs += 1
                self.derniere_erreur = f"{type(erreur).__name__}: {erreur}"
                log.exception("Course du plan en échec (%d/%d)",
                              self.echecs_consecutifs, ECHECS_MAX)
                self._prevenir(
                    f"Course du plan en échec "
                    f"({self.echecs_consecutifs}/{ECHECS_MAX}) : "
                    f"{self.derniere_erreur}")
                if self.echecs_consecutifs >= ECHECS_MAX:
                    self.abandonnee = True
                    self.active = False
                    log.error(
                        "Course du plan ABANDONNÉE après %d échecs. La "
                        "collecte continue.", ECHECS_MAX)
                    self._prevenir(
                        "Course du plan ABANDONNÉE. La collecte continue "
                        "normalement.")
                    return
                self._arret.wait(attente)
                attente = min(attente * 2, BACKOFF_MAX_SEC)

    def _prevenir(self, message: str) -> None:
        """Alerter ne doit pas pouvoir faire tomber le confinement."""
        if self._alerter is None:
            return
        try:
            self._alerter(message)
        except Exception:                        # noqa: BLE001
            log.debug("Alerte de course non envoyée", exc_info=True)

    # --- ce que /etat affiche -----------------------------------------------

    def resume(self) -> str:
        if self.abandonnee:
            return (f"🔴 course ABANDONNÉE après {ECHECS_MAX} échecs — "
                    f"{self.derniere_erreur}")
        if not self.active:
            return "⏸ course du plan désactivée (PLAN_DEMO=0)"
        if self.echecs_consecutifs:
            return (f"🟠 course en reprise ({self.echecs_consecutifs}/"
                    f"{ECHECS_MAX}) — {self.derniere_erreur}")
        return f"🟢 {self._dernier_resume}"


def course_activee() -> bool:
    """`PLAN_DEMO=1` et rien d'autre.

    Le défaut est INACTIF, et c'est voulu : le code est déployé avant d'être
    utilisé, pour que son démarrage et ses imports soient éprouvés en
    production sans qu'un seul ordre ne parte. Un défaut actif ferait trader
    le jour où quelqu'un déploie sans y penser.
    """
    import os
    return os.environ.get("PLAN_DEMO", "0").strip() == "1"
