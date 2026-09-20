"""
Le confinement de la course — le seul risque du montage à un seul processus.

Un service séparé coûterait une instance payante, alors la course partage le
processus du collecteur. Tout ce fichier teste une seule chose : **qu'une
panne de la course ne puisse pas atteindre la collecte.**

Le contrat est inhabituel et il faut le dire : ici, on avale TOUT, y compris
une erreur de programmation. Ailleurs dans ce projet, c'est interdit — une
`BotError` ou un `AttributeError` doit tuer le collecteur bruyamment plutôt
que de le laisser tourner à vide. Ici, l'inverse : laisser remonter tuerait
le processus qui collecte, et quatorze jours de série continue ne se
rattrapent pas alors que dix jours de course, si.
"""

from __future__ import annotations

import threading
import time

import pytest

from maxprofit.core.errors import BotError
from maxprofit.hosting.course import ECHECS_MAX, SuperviseurCourse


class CourseFactice:
    def __init__(self, lever=None, tours_avant=0):
        self.lever = lever
        self.tours_avant = tours_avant
        self.tours = 0

    def tour(self):
        self.tours += 1
        if self.lever is not None and self.tours > self.tours_avant:
            raise self.lever
        return True

    def resume(self):
        return f"{self.tours} tour(s)"


def _attendre(condition, limite=5.0):
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        if condition():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------- #
# Rien ne remonte — pas même ce qui serait fatal ailleurs
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("erreur", [
    BotError("bogue métier"),
    AttributeError("erreur de programmation"),
    ValueError("donnée invalide"),
    RuntimeError("panne inattendue"),
])
def test_aucune_exception_ne_sort_du_thread(erreur):
    """Y compris `AttributeError` : ailleurs elle est FATALE et doit l'être.
    Ici, la laisser remonter tuerait le processus qui collecte."""
    vu: list[BaseException] = []
    ancien = threading.excepthook
    threading.excepthook = lambda args: vu.append(args.exc_value)
    try:
        s = SuperviseurCourse(lambda: CourseFactice(lever=erreur), pause_sec=0.01)
        s.demarrer()
        assert _attendre(lambda: s.echecs_consecutifs >= 1)
        s.arreter()
    finally:
        threading.excepthook = ancien
    assert vu == [], f"une exception a quitté le thread : {vu}"


def test_une_fabrication_qui_echoue_est_contenue():
    """Le cas le plus probable en production : la base ou le broker ne
    répondent pas au moment de construire la course."""
    def fabriquer():
        raise ConnectionError("broker injoignable")

    s = SuperviseurCourse(fabriquer, pause_sec=0.01)
    s.demarrer()
    assert _attendre(lambda: s.echecs_consecutifs >= 1)
    assert "ConnectionError" in (s.derniere_erreur or "")
    s.arreter()


# --------------------------------------------------------------------------- #
# Elle renonce plutôt que de marteler
# --------------------------------------------------------------------------- #

def test_la_course_abandonne_apres_trop_d_echecs(monkeypatch):
    """Réessayer sans fin martèlerait le broker et fausserait la mesure
    d'exécution qu'on est précisément en train de faire."""
    monkeypatch.setattr("maxprofit.hosting.course.BACKOFF_SEC", 0.01)
    monkeypatch.setattr("maxprofit.hosting.course.BACKOFF_MAX_SEC", 0.01)

    def fabriquer():
        raise RuntimeError("toujours cassé")

    s = SuperviseurCourse(fabriquer, pause_sec=0.01)
    s.demarrer()
    assert _attendre(lambda: s.abandonnee, limite=8.0)
    assert s.echecs_consecutifs == ECHECS_MAX
    assert not s.active
    assert "ABANDONNÉE" in s.resume()


def test_une_alerte_qui_echoue_ne_casse_pas_le_confinement():
    """Le dernier maillon : si prévenir lève, on est de retour au point de
    départ — une exception qui traverse le thread."""
    def alerter(message):
        raise OSError("Telegram injoignable")

    s = SuperviseurCourse(lambda: CourseFactice(lever=BotError("x")),
                          alerter=alerter, pause_sec=0.01)
    s.demarrer()
    assert _attendre(lambda: s.echecs_consecutifs >= 1)
    s.arreter()


# --------------------------------------------------------------------------- #
# Ce que l'opérateur lit
# --------------------------------------------------------------------------- #

def test_le_resume_distingue_desactivee_de_en_panne():
    """« Désactivée » et « en panne » sont deux états différents, et les
    confondre ferait croire à une course qui tourne alors qu'elle est morte."""
    s = SuperviseurCourse(lambda: CourseFactice())
    assert "désactivée" in s.resume()

    s.demarrer()
    assert _attendre(lambda: "🟢" in s.resume())
    s.arreter()


def test_une_course_qui_tourne_affiche_son_avancement():
    s = SuperviseurCourse(lambda: CourseFactice(), pause_sec=0.01)
    s.demarrer()
    assert _attendre(lambda: "tour(s)" in s.resume())
    s.arreter()


def test_le_thread_est_daemon_et_ne_retarde_pas_l_arret():
    s = SuperviseurCourse(lambda: CourseFactice(), pause_sec=0.01)
    s.demarrer()
    assert s._thread is not None and s._thread.daemon
    s.arreter()


# --------------------------------------------------------------------------- #
# Le drapeau
# --------------------------------------------------------------------------- #

def test_le_defaut_est_INACTIF(monkeypatch):
    """Le code est déployé avant d'être utilisé, pour que son démarrage soit
    éprouvé en production sans qu'un seul ordre ne parte."""
    from maxprofit.hosting.course import course_activee

    monkeypatch.delenv("PLAN_DEMO", raising=False)
    assert course_activee() is False
    monkeypatch.setenv("PLAN_DEMO", "0")
    assert course_activee() is False
    monkeypatch.setenv("PLAN_DEMO", "")
    assert course_activee() is False
    monkeypatch.setenv("PLAN_DEMO", "1")
    assert course_activee() is True
