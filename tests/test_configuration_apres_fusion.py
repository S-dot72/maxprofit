"""
La configuration arrivée en base APRÈS le démarrage de la course.

Le 2026-10-18, le service a démarré sur la nouvelle base pendant que la
fusion la remplissait. `reglages`, copiée en dernier, n'y était pas encore :
la course est retombée sur l'environnement (`plan-demo-v2`, 250 $, jour 1)
et ne relisait sa configuration qu'à sa construction. `/etat` affichait le
mauvais plan sans rien dire de sa provenance.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from maxprofit.hosting.course import (
    MESSAGES_CONSIGNE_EXTERNE, SuperviseurCourse)
from maxprofit.hosting.pilotage import (
    alerte_sans_configuration, consigne_de_la_base)
from maxprofit.live.configuration import Configuration


def _attendre(condition, limite=5.0):
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        if condition():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------- #
# La règle
# --------------------------------------------------------------------------- #

def test_ce_que_la_base_demande_a_la_course():
    lancee = Configuration(campagne="plan-20261010-000517", lancee=True)
    arretee = Configuration(campagne="plan-20261010-000517", lancee=False)
    # Rien en base : la course suit l'environnement, on ne touche à rien.
    assert consigne_de_la_base(None, "plan-demo-v2") is None
    # La configuration arrivée après coup reprend la main.
    assert consigne_de_la_base(lancee, "plan-demo-v2") == "relancer"
    # Même campagne : un /capital ou un /risque attend le /demarrer.
    assert consigne_de_la_base(lancee, "plan-20261010-000517") is None
    assert consigne_de_la_base(arretee, "plan-demo-v2") == "arreter"
    # Une course qui ne dit pas sa campagne n'est pas relancée à l'aveugle.
    assert consigne_de_la_base(lancee, None) is None


# --------------------------------------------------------------------------- #
# Le superviseur
# --------------------------------------------------------------------------- #

class _Course:
    def __init__(self, campagne, origine):
        self.journal = SimpleNamespace(campagne=campagne)
        self.origine = origine
        self.tours = 0

    def tour(self):
        self.tours += 1
        time.sleep(0.01)
        return True

    def au_repos(self):
        return True

    def resume(self):
        return f"{self.journal.campagne} : {self.tours} tour(s)"


def test_la_configuration_arrivee_apres_coup_reprend_la_main():
    base = {"configuration": None}
    construites, alertes = [], []

    def fabriquer(_alerter=None):
        cfg = base["configuration"]
        construites.append(
            _Course(cfg.campagne, "configuration enregistrée") if cfg
            else _Course("plan-demo-v2", "variables d'environnement"))
        return construites[-1]

    def consigne(course):
        return consigne_de_la_base(base["configuration"],
                                   course.journal.campagne)

    s = SuperviseurCourse(fabriquer, pause_sec=0.01, alerter=alertes.append,
                          consigne_externe=consigne, verification_sec=0.02)
    s.demarrer()
    assert _attendre(lambda: construites and construites[0].tours > 3)
    assert s.origine() == "plan-demo-v2 (variables d'environnement)"
    # La fusion verse `reglages` pendant que la course tourne.
    base["configuration"] = Configuration(campagne="plan-20261010-000517",
                                          lancee=True)
    assert _attendre(lambda: len(construites) == 2)
    assert construites[1].journal.campagne == "plan-20261010-000517"
    assert MESSAGES_CONSIGNE_EXTERNE["relancer"] in alertes
    assert s.origine() == ("plan-20261010-000517 (configuration "
                           "enregistrée)")
    # Une fois sur la bonne campagne, plus rien ne bouge.
    time.sleep(0.2)
    assert len(construites) == 2
    s.arreter()


def test_une_base_illisible_ne_relance_rien():
    construites = []

    def fabriquer(_alerter=None):
        construites.append(_Course("plan-demo-v2", None))
        return construites[-1]

    def illisible(_course):
        raise OSError("failed to resolve host")

    s = SuperviseurCourse(fabriquer, pause_sec=0.01,
                          consigne_externe=illisible, verification_sec=0.01)
    s.demarrer()
    assert _attendre(lambda: construites and construites[0].tours > 10)
    assert len(construites) == 1 and s.tourne()
    assert s.origine() == "plan-demo-v2"
    s.arreter()


def test_la_base_qui_dit_arretee_arrete_la_course():
    construites, arretee = [], threading.Event()

    def fabriquer(_alerter=None):
        construites.append(_Course("plan-demo-v2", None))
        return construites[-1]

    def consigne(course):
        return consigne_de_la_base(
            Configuration(campagne="plan-x", lancee=False)
            if arretee.is_set() else None, course.journal.campagne)

    s = SuperviseurCourse(fabriquer, pause_sec=0.01,
                          consigne_externe=consigne, verification_sec=0.01)
    s.demarrer()
    assert _attendre(lambda: construites)
    arretee.set()
    assert _attendre(lambda: not s.tourne())
    assert "arrêtée" in s.resume()


# --------------------------------------------------------------------------- #
# L'alerte au repli sur l'environnement
# --------------------------------------------------------------------------- #

@pytest.fixture
def base(tmp_path, monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)
    from maxprofit.store.db import open_read_write
    conn = open_read_write(tmp_path / "m.db")
    yield conn
    conn.close()


def _etat(conn, campagne, maj, jour, solde):
    conn.execute(
        "INSERT INTO plan_etat (campagne, maj_ts_sec, jour, solde, "
        "solde_ouverture, sessions_jouees, sessions_perdues_daffilee, "
        "jour_utc, reancrages, derniere_bougie) VALUES "
        "(?, ?, ?, ?, ?, 0, 0, 0, '[]', '{}')",
        (campagne, maj, jour, solde, solde))
    conn.commit()


def test_le_repli_sur_l_environnement_est_signale(base):
    assert alerte_sans_configuration(base, "plan-demo-v2") is None
    _etat(base, "plan-demo-v2", 1_700_000_000, 3, 251.0)
    assert alerte_sans_configuration(base, "plan-demo-v2") is None
    # La campagne en cours, plus récente, n'a pas sa configuration.
    _etat(base, "plan-20261010-000517", 1_790_000_000, 8, 312.4)
    message = alerte_sans_configuration(base, "plan-demo-v2")
    assert "plan-20261010-000517" in message and "jour 8" in message
    assert "312.40 $" in message and "--tables reglages,plan_etat" in message
    assert "/demarrer" in message
