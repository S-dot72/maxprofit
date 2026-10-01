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


def _ou(erreur: BaseException) -> str:
    """Où l'erreur s'est produite : fichier, ligne, et le code fautif.

    ⚠ SANS ÇA, UNE ERREUR AVALÉE EST INDIAGNOSTICABLE. Ce superviseur attrape
    TOUT — c'est une règle assumée, parce que laisser remonter tuerait la
    collecte, qui vaut plus que la course. Mais `/etat` n'affichait que le type
    et le message, et « TypeError: unsupported operand type(s) for -: 'str' and
    'str' » ne dit ni où ni sur quoi. Il a fallu inspecter la base, les types
    rendus par le pilote et trois modules pour ne rien trouver, faute de savoir
    quelle ligne accusait.

    On ne garde que le CADRE LE PLUS PROFOND, et seulement le code du projet :
    une pile entière dans un message Telegram est illisible, et les cadres de
    bibliothèque désignent presque toujours l'appelant.
    """
    trace = erreur.__traceback__
    dernier = profond = None
    while trace is not None:
        profond = trace
        nom = trace.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/maxprofit/" in nom:
            dernier = trace
        trace = trace.tb_next
    # À défaut d'un cadre du projet, le plus profond quel qu'il soit : une
    # erreur née dans une bibliothèque vaut mieux localisée là que nulle part.
    dernier = dernier or profond
    if dernier is None:
        return "origine inconnue"
    cadre = dernier.tb_frame
    chemin = cadre.f_code.co_filename.replace("\\", "/")
    # Le dépôt s'appelant `maxprofit` et le paquet aussi, préfixer sans
    # retirer donnerait « maxprofit/maxprofit/core/... ».
    reste = chemin.split("/maxprofit/")[-1] if "/maxprofit/" in chemin         else chemin.rsplit("/", 2)[-1]
    fichier = reste if reste.startswith("maxprofit/") else f"maxprofit/{reste}"
    ligne = ""
    try:
        import linecache
        ligne = linecache.getline(
            cadre.f_code.co_filename, dernier.tb_lineno).strip()
    except Exception:                        # noqa: BLE001
        pass
    return (f"{fichier}:{dernier.tb_lineno} dans "
            f"{cadre.f_code.co_name}()"
            + (f" — `{ligne[:120]}`" if ligne else ""))


class SuperviseurCourse:
    """Fait tourner la course du plan à côté de la collecte, sans l'exposer.

    `fabriquer(alerter)` rend un objet possédant `tour()` et `resume()`. Elle
    est passée plutôt qu'importée pour que ce module reste testable sans
    broker ni base.

    L'alerteur lui est transmis parce que c'est la COURSE qui sait quand une
    session se clôt ou qu'un réancrage se déclenche — le superviseur, lui, ne
    voit que des exceptions.
    """

    def __init__(self, fabriquer, *, alerter=None, pause_sec: float = 20.0,
                 debut_ts_sec: int | None = None,
                 reveil: threading.Event | None = None):
        self._fabriquer = fabriquer
        self._alerter = alerter
        #: Attente MAXIMALE entre deux passages sans ordre.
        self.pause_sec = pause_sec
        #: Levé quand des bougies closes arrivent en base. La course se
        #: réveille dessus : une pause fixe de vingt secondes faisait évaluer
        #: chaque bougie en moyenne dix secondes après sa clôture, jusqu'à
        #: vingt-deux, alors qu'elle est en base deux secondes après.
        self._reveil = reveil
        #: Instant avant lequel la course ATTEND, sans rien placer.
        #:
        #: Elle existe parce que la première version obligeait l'opérateur à
        #: venir basculer une variable le bon jour. Faire dépendre le départ
        #: d'un geste humain à une date précise, c'est le manquer — et une
        #: course lancée trop tôt passerait des ordres pendant que la collecte
        #: joue encore son critère de quatorze jours.
        self.debut_ts_sec = debut_ts_sec
        self._thread: threading.Thread | None = None
        self._arret = threading.Event()
        self.active = False
        self.demarrages = 0
        self.echecs_consecutifs = 0
        self.derniere_erreur: str | None = None
        self.abandonnee = False
        self._dernier_resume = "thread lancé, connexion au broker en cours"
        #: L'étape du démarrage en cours, et depuis quand (monotone).
        #:
        #: « connexion au broker en cours » couvrait tout, de l'ouverture de
        #: la base au rattrapage des ordres : impossible de dire ce qui
        #: traînait. La fabrique la nomme au fil de l'eau.
        self._etape: tuple[str, float] | None = None
        self._demarrage_ts: float | None = None
        #: La course elle-même, pour l'interroger PENDANT qu'elle travaille.
        #:
        #: Le résumé en cache ne se rafraîchissait qu'au retour de `tour()`,
        #: or `tour()` bloque un quart d'heure en attendant l'expiration d'une
        #: option. L'affichage restait donc figé pendant presque tout le temps
        #: où quelque chose se passait — des ordres partaient chez le broker et
        #: Telegram montrait encore « connexion en cours ».
        self._course = None
        #: « relancer » ou « arreter », demandé depuis Telegram et appliqué
        #: par le thread de la course dès qu'elle est AU REPOS : jamais au
        #: milieu d'une martingale ni pendant qu'un ordre vit.
        self._consigne: str | None = None

    # --- cycle de vie -------------------------------------------------------

    def demarrer(self) -> None:
        if self._thread is not None:
            return
        self.active = True
        self._thread = threading.Thread(
            target=self._boucle, name="course-plan", daemon=True)
        self._thread.start()
        log.info("Course du plan : thread démarré.")

    def tourne(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def relancer(self) -> str:
        """Reconstruit la course sur la configuration enregistrée.

        Démarre le thread s'il ne tourne pas ; sinon la relance attend que la
        course soit au repos. Rend ce qui va se passer, pour l'utilisateur.
        """
        if not self.tourne():
            self._thread = None
            self._arret.clear()
            self._consigne = None
            self.abandonnee = False
            self.echecs_consecutifs = 0
            self.debut_ts_sec = None
            self.demarrer()
            return "La course démarre."
        self._consigne = "relancer"
        self._reveiller()
        if self._course is None or _au_repos(self._course):
            return "La course repart avec ce réglage dans un instant."
        return ("Une session est en cours : la course repartira avec ce "
                "réglage dès qu'elle sera terminée.")

    def suspendre(self) -> str:
        """Arrête la course au prochain repos ; la collecte continue."""
        if not self.tourne():
            self.active = False
            return "La course est déjà arrêtée."
        self._consigne = "arreter"
        self._reveiller()
        if self._course is None or _au_repos(self._course):
            return "La course s'arrête dans un instant."
        return ("Une session est en cours : la course s'arrêtera dès qu'elle "
                "sera terminée, sans laisser de martingale à moitié jouée.")

    def _reveiller(self) -> None:
        if self._reveil is not None:
            self._reveil.set()

    def arreter(self) -> None:
        self._arret.set()
        self.active = False
        if self._reveil is not None:
            self._reveil.set()

    def _attendre_le_prochain_passage(self) -> None:
        if self._reveil is None:
            self._arret.wait(self.pause_sec)
            return
        self._reveil.wait(self.pause_sec)
        # Effacé APRÈS le réveil : des bougies écrites pendant le passage
        # suivant relèveront le drapeau, et ne seront pas manquées.
        self._reveil.clear()

    # --- la boucle, et son confinement --------------------------------------

    def en_attente(self) -> bool:
        return (self.debut_ts_sec is not None
                and time.time() < self.debut_ts_sec)

    def _patienter_jusqu_au_depart(self) -> None:
        """Attend l'heure dite, par petits pas pour rester interruptible."""
        if self.debut_ts_sec is None:
            return
        restant = self.debut_ts_sec - time.time()
        if restant <= 0:
            return
        log.info("Course du plan : départ programmé dans %.1f h.",
                 restant / 3600)
        while not self._arret.is_set() and time.time() < self.debut_ts_sec:
            self._arret.wait(min(60.0, self.debut_ts_sec - time.time()))
        if not self._arret.is_set():
            log.info("Course du plan : l'heure de départ est atteinte.")
            self._prevenir("🟢 Course du plan : démarrage programmé atteint.")

    def _boucle(self) -> None:
        self._patienter_jusqu_au_depart()
        attente = BACKOFF_SEC
        while not self._arret.is_set() and not self.abandonnee:
            try:
                self.demarrages += 1
                self._demarrage_ts = time.monotonic()
                self._etape = None
                # L'alerteur est transmis a la course : c'est elle qui sait
                # quand une session se clot ou qu'un reancrage se declenche.
                # La fabrique lit la configuration du moment : une relance
                # demandée avant ce point est donc déjà servie.
                if self._consigne == "relancer":
                    self._consigne = None
                course = self._fabriquer(self._alerter)
                self._course = course
                self._etape = None
                self.echecs_consecutifs = 0
                attente = BACKOFF_SEC
                consigne = None
                while not self._arret.is_set():
                    joue = course.tour()
                    # Le résumé est rafraîchi à CHAQUE passage, pas seulement
                    # quand un ordre part. Ne le mettre à jour qu'après un
                    # trade laissait `/etat` afficher « pas encore démarrée »
                    # pendant des heures sur une course parfaitement vivante
                    # qui attendait simplement un signal.
                    self._dernier_resume = course.resume()
                    if self._consigne is not None and _au_repos(course):
                        consigne, self._consigne = self._consigne, None
                        break
                    if not joue:
                        self._attendre_le_prochain_passage()
                if consigne is None:
                    return
                self._liberer_le_courtier()
                if consigne == "arreter":
                    self.active = False
                    self._dernier_resume = "course arrêtée depuis Telegram"
                    log.info("Course du plan : arrêtée à la demande.")
                    self._prevenir("⏹ <b>Course arrêtée</b>. La collecte "
                                   "continue ; /demarrer pour reprendre.")
                    return
                log.info("Course du plan : relance sur la nouvelle "
                         "configuration.")
                self._prevenir("🔄 <b>Course relancée</b> sur le nouveau "
                               "réglage.")
                continue
            except BaseException as erreur:      # noqa: BLE001
                # TOUT est avalé, y compris ce qui serait fatal ailleurs.
                # Laisser remonter tuerait le processus qui collecte, et la
                # collecte vaut plus que la course.
                self._liberer_le_courtier()
                self.echecs_consecutifs += 1
                self.derniere_erreur = (
                    f"{type(erreur).__name__}: {erreur} — {_ou(erreur)}")
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

    def noter_etape(self, etape: str) -> None:
        """Rappelée par la fabrique à chaque étape du démarrage."""
        self._etape = (etape, time.monotonic())

    def _resume_du_demarrage(self) -> str:
        if self._etape is None:
            return self._dernier_resume
        etape, depuis = self._etape
        maintenant = time.monotonic()
        total = maintenant - (self._demarrage_ts or depuis)
        return (f"démarrage — {etape} depuis {maintenant - depuis:.0f} s "
                f"(démarrage lancé il y a {total:.0f} s)")

    def _liberer_le_courtier(self) -> None:
        """Rend la connexion d'une course qu'on abandonne.

        ⚠ SANS CELA, CHAQUE ÉCHEC OUVRAIT UNE SESSION DE PLUS CHEZ LE BROKER.
        La course jetée gardait son client, que la bibliothèque reconnecte à
        vie, et la suivante en ouvrait un autre sur le même jeton : cinq échecs
        (le TypeError de 11 h 29), cinq sessions simultanées depuis la même IP.
        """
        course, self._course = self._course, None
        courtier = getattr(course, "courtier", None)
        if courtier is None:
            return
        try:
            courtier.fermer()
        except Exception:                        # noqa: BLE001
            log.debug("Fermeture du courtier en échec", exc_info=True)

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
            return ("⏸ course arrêtée — /configuration pour la régler, "
                    "/demarrer pour la lancer")
        if self.en_attente():
            restant = self.debut_ts_sec - time.time()
            return (f"⏳ course armée — départ dans "
                    f"{restant / 3600:.1f} h, aucun ordre d'ici là")
        if self.echecs_consecutifs:
            return (f"🟠 course en reprise ({self.echecs_consecutifs}/"
                    f"{ECHECS_MAX}) — {self.derniere_erreur}")
        # On interroge la course VIVANTE plutôt que le dernier résumé mis en
        # cache : `resume()` ne fait que lire des champs, et c'est le seul
        # moyen de voir ce qui se passe pendant qu'un ordre est en cours.
        if self._course is not None:
            try:
                return f"🟢 {self._course.resume()}"
            except Exception:                    # noqa: BLE001
                log.debug("Résumé de course illisible", exc_info=True)
        return f"🟢 {self._resume_du_demarrage()}"


def _au_repos(course) -> bool:
    """Une course qui ne sait pas le dire est réputée au repos."""
    try:
        return bool(getattr(course, "au_repos", lambda: True)())
    except Exception:                            # noqa: BLE001
        return False


def course_activee() -> bool:
    """`PLAN_DEMO=1` et rien d'autre.

    Le défaut est INACTIF, et c'est voulu : le code est déployé avant d'être
    utilisé, pour que son démarrage et ses imports soient éprouvés en
    production sans qu'un seul ordre ne parte. Un défaut actif ferait trader
    le jour où quelqu'un déploie sans y penser.
    """
    import os
    return os.environ.get("PLAN_DEMO", "0").strip() == "1"


def date_de_depart() -> int | None:
    """`PLAN_DEBUT` -> instant epoch, ou `None` pour « tout de suite ».

    Accepte `2026-09-22`, `2026-09-22T14:00`, ou un epoch en secondes. Une
    valeur illisible LÈVE plutôt que d'être ignorée : une date mal tapée
    silencieusement ignorée ferait partir la course le jour même, c'est-à-dire
    exactement ce qu'on cherchait à éviter en la réglant.
    """
    import os
    from datetime import datetime, timezone

    brut = os.environ.get("PLAN_DEBUT", "").strip()
    if not brut:
        return None
    if brut.isdigit():
        return int(brut)
    try:
        quand = datetime.fromisoformat(brut)
    except ValueError as erreur:
        raise ValueError(
            f"PLAN_DEBUT={brut!r} illisible. Attendu : 2026-09-22, "
            f"2026-09-22T14:00, ou un epoch en secondes. Ignorer cette "
            f"valeur ferait partir la course aujourd'hui."
        ) from erreur
    if quand.tzinfo is None:
        quand = quand.replace(tzinfo=timezone.utc)
    return int(quand.timestamp())
