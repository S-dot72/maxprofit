"""
La configuration choisie sur Telegram : plan ou trading seul, démo ou réel.

Ce qu'elle doit garantir :
- le plan est EXACTEMENT celui que le moteur calculait (risque N/M) ;
- un risque élevé est signalé, et lancer demande une confirmation ;
- un réglage impossible (mise sous 1 $) est dit, et refusé au lancement ;
- une relance attend que la course soit au repos ;
- le trading seul joue 1 % du solde, sans martingale, et s'arrête à +2 % ou
  −3 % jusqu'au lendemain UTC.
"""

from __future__ import annotations

import itertools
import re
import threading
import time

import pytest

from maxprofit.core.errors import BotError
from maxprofit.core.types import Direction, Signal
from maxprofit.execution.journal import JournalExecution
from maxprofit.hosting.course import SuperviseurCourse
from maxprofit.hosting.pilotage import Pilote, gouverne
from maxprofit.hosting.telegram import _commande
from maxprofit.live.configuration import Configuration, charger, sauver
from maxprofit.live.plan_demo import CoursePlanDemo
from maxprofit.plan import Arret
from maxprofit.store import reglages
from maxprofit.store.db import open_read_write

from test_plan_demo import T0_MS, CourtierFactice, LecteurFactice


# --------------------------------------------------------------------------- #
# Le calcul
# --------------------------------------------------------------------------- #

def test_le_plan_1_sur_7_est_celui_du_moteur():
    c = Configuration()
    assert c.erreur() is None
    assert c.mises() == pytest.approx((1.59, 3.31), abs=0.01)
    assert c.capital_a_30_jours() == pytest.approx(701.72, abs=0.01)
    assert c.niveau_de_risque() == "normal"
    assert "À atteindre en 30 jours : 701.72 $" in c.resume()


def test_un_risque_eleve_est_signale_avec_ses_chiffres():
    c = Configuration(gagnants=2, trades=5)
    assert c.erreur() is None
    assert c.niveau_de_risque() == "élevé"
    texte = c.resume()
    assert "⚠️" in texte and "17.4 %" in texte


def test_une_mise_sous_le_minimum_du_broker_est_refusee_avec_le_capital_utile():
    c = Configuration(gagnants=3, trades=10)
    assert "480 $" in c.erreur()
    assert "⛔" in c.resume()
    assert Configuration(capital=150).erreur() is not None
    assert Configuration(capital=160).erreur() is None


def test_le_trading_seul_demande_100_dollars():
    assert Configuration(mode="trading", capital=90).erreur() is not None
    assert Configuration(mode="trading", capital=100).erreur() is None


def test_le_trading_seul_joue_un_pour_cent_sans_martingale():
    c = Configuration(mode="trading", capital=300)
    plan = c.plan_de_course()
    assert c.pas_de_course() == 1
    assert plan.echelle(300).mises() == pytest.approx((3.0,))
    assert plan.objectif_journalier_pct == 2.0
    assert plan.perte_journaliere_max_pct == 3.0


def test_le_plafond_de_mise_laisse_le_plan_grandir():
    vise, plafond = Configuration().plafond_de_mise()
    assert vise == pytest.approx(701.72, abs=0.01)
    assert plafond > 3.31 * vise / 250


def test_aller_retour_dict():
    c = Configuration(mode="trading", compte="reel", capital=321.5,
                      campagnes_precedentes=["a", "b"])
    assert Configuration.from_dict(c.to_dict()) == c
    assert Configuration.from_dict(None) == Configuration()


@pytest.mark.parametrize("cfg", [
    Configuration(), Configuration(gagnants=2, trades=5),
    Configuration(gagnants=3, trades=10), Configuration(mode="trading"),
    Configuration(compte="reel")])
def test_le_resume_ne_casse_pas_le_html_de_telegram(cfg):
    """Un « < » nu fait refuser tout le message par Telegram."""
    assert not re.search(r"<(?!/?b>)", cfg.resume())


@pytest.mark.parametrize("reglage, saisie, champ, attendu", [
    ("risque", "1/7", ("gagnants", "trades"), (1, 7)),
    ("risque", "2 sur 5", ("gagnants", "trades"), (2, 5)),
    ("risque", "3 10", ("gagnants", "trades"), (3, 10)),
    ("capital", "300,50 $", ("capital",), (300.5,)),
    ("sessions", "4", ("sessions_par_jour",), (4,)),
    ("mode", "trading seul", ("mode",), ("trading",)),
    ("compte", "Réel", ("compte",), ("reel",)),
])
def test_les_saisies_sont_lues(reglage, saisie, champ, attendu):
    c = Configuration().modifiee(reglage, saisie)
    assert tuple(getattr(c, n) for n in champ) == attendu


@pytest.mark.parametrize("reglage, saisie", [
    ("risque", "7/1"), ("risque", "abc"), ("capital", "beaucoup"),
    ("capital", "5"), ("sessions", "0"), ("mode", "turbo"),
    ("compte", "autre")])
def test_les_saisies_illisibles_sont_refusees(reglage, saisie):
    with pytest.raises(BotError):
        Configuration().modifiee(reglage, saisie)


def test_les_reglages_survivent_en_base(tmp_path):
    conn = open_read_write(tmp_path / "b.db")
    assert charger(conn) is None
    sauver(conn, Configuration(capital=400))
    sauver(conn, Configuration(capital=500, mode="trading"))
    relue = charger(conn)
    assert (relue.capital, relue.mode) == (500, "trading")
    assert reglages.lire(conn, "inconnue") is None


# --------------------------------------------------------------------------- #
# Les commandes
# --------------------------------------------------------------------------- #

class SuperviseurFactice:
    def __init__(self, course=None):
        self._course = course
        self.relances = 0
        self.suspensions = 0
        self.vivant = False

    def tourne(self):
        return self.vivant

    def relancer(self):
        self.relances += 1
        self.vivant = True
        return "La course démarre."

    def suspendre(self):
        self.suspensions += 1
        self.vivant = False
        return "La course s'arrête dans un instant."


@pytest.fixture
def pilote(tmp_path):
    chemin = tmp_path / "p.db"
    sup = SuperviseurFactice()
    p = Pilote(lambda: open_read_write(chemin), lambda: sup, "plan-demo-v2")
    p.sup = sup
    return p


def test_une_modification_est_enregistree_et_reaffiche_la_projection(pilote):
    reponse = pilote("risque", "2/5")
    assert "Enregistré" in reponse and "⚠️" in reponse
    assert (pilote.charger().gagnants, pilote.charger().trades) == (2, 5)
    assert pilote.sup.relances == 0, "modifier ne lance rien"


def test_une_saisie_fausse_ne_change_rien(pilote):
    assert pilote("risque", "abc").startswith("⛔")
    assert pilote.charger() is None


def test_demarrer_ouvre_une_campagne_et_garde_les_precedentes(pilote):
    reponse = pilote("demarrer")
    assert "Lancé" in reponse
    cfg = pilote.charger()
    assert cfg.lancee and cfg.campagne.startswith("plan-")
    assert cfg.campagnes_precedentes == ["plan-demo-v2"]
    assert cfg.capital_lance == cfg.capital
    assert gouverne(cfg)
    assert pilote.sup.relances == 1
    assert "Déjà lancé" in pilote("demarrer")
    assert pilote.sup.relances == 1


def test_un_risque_eleve_demande_confirmation(pilote):
    pilote("risque", "2/5")
    assert "/demarrer confirmer" in pilote("demarrer")
    assert pilote.sup.relances == 0
    assert "Lancé" in pilote("demarrer", "confirmer")


def test_un_reglage_impossible_n_est_pas_lance(pilote):
    pilote("risque", "3/10")
    assert "refusé" in pilote("demarrer")
    assert pilote.sup.relances == 0


def test_le_compte_reel_n_est_pas_encore_lance(pilote):
    pilote("compte", "reel")
    assert "réel pas encore branché" in pilote("demarrer")
    assert pilote.sup.relances == 0


def test_arreter_est_retenu_meme_sans_configuration(pilote):
    assert "Arrêt demandé" in pilote("arreter")
    cfg = pilote.charger()
    assert not cfg.lancee and gouverne(cfg)
    assert pilote.sup.suspensions == 1


def test_une_relance_en_cours_de_plan_repart_du_solde_atteint(pilote):
    pilote("demarrer")
    cfg = pilote.charger()

    class Course:
        class journal:
            campagne = cfg.campagne

        class etat:
            solde = 262.40

    pilote.sup._course = Course()
    reponse = pilote("sessions", "4")
    assert "262.40 $" in reponse
    pilote("demarrer")
    relancee = pilote.charger()
    assert relancee.capital == pytest.approx(262.40)
    assert relancee.campagnes_precedentes == ["plan-demo-v2", cfg.campagne]


def test_un_capital_retouche_l_emporte_sur_le_solde(pilote):
    pilote("demarrer")
    pilote("capital", "400")
    pilote.sup._course = None
    pilote("demarrer")
    assert pilote.charger().capital == 400


@pytest.mark.parametrize("texte, attendu", [
    ("/capital 300", "capital"), ("/Démarrer", "demarrer"),
    ("/risque@MonBot 1/7", "risque"), ("bonjour", "")])
def test_la_commande_est_reconnue(texte, attendu):
    assert _commande(texte) == attendu


# --------------------------------------------------------------------------- #
# La relance, au repos seulement
# --------------------------------------------------------------------------- #

class CourseAuRepos:
    def __init__(self, repos):
        self.repos = repos
        self.tours = 0

    def tour(self):
        self.tours += 1
        time.sleep(0.01)
        return True

    def au_repos(self):
        return self.repos.is_set()

    def resume(self):
        return "factice"


def _attendre(condition, limite=5.0):
    fin = time.monotonic() + limite
    while time.monotonic() < fin:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_la_relance_attend_le_repos_puis_reconstruit():
    repos = threading.Event()
    construites = []

    def fabriquer(_alerter=None):
        construites.append(CourseAuRepos(repos))
        return construites[-1]

    s = SuperviseurCourse(fabriquer, pause_sec=0.01)
    s.demarrer()
    assert _attendre(lambda: construites and construites[0].tours > 2)
    assert "session est en cours" in s.relancer()
    time.sleep(0.1)
    assert len(construites) == 1, "pas de relance au milieu d'une session"
    repos.set()
    assert _attendre(lambda: len(construites) == 2)
    s.arreter()


def test_suspendre_arrete_au_repos_et_relancer_redemarre():
    repos = threading.Event()
    repos.set()
    construites = []

    def fabriquer(_alerter=None):
        construites.append(CourseAuRepos(repos))
        return construites[-1]

    s = SuperviseurCourse(fabriquer, pause_sec=0.01)
    s.demarrer()
    assert _attendre(lambda: construites)
    s.suspendre()
    assert _attendre(lambda: not s.tourne())
    assert "course arrêtée" in s.resume()
    assert s.relancer() == "La course démarre."
    assert _attendre(lambda: len(construites) == 2)
    s.arreter()


# --------------------------------------------------------------------------- #
# Le trading seul dans la course
# --------------------------------------------------------------------------- #

_numero = itertools.count()


def _course_trading(tmp_path, resultats, capital=250.0):
    cfg = Configuration(mode="trading", capital=capital)
    journal = JournalExecution(tmp_path / "t.db",
                               campagne=f"trading-{next(_numero)}")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(resultats), journal,
                       cfg.plan_de_course(), ("EURUSD_otc", "AUDCAD_otc"))
    c.pas_max = cfg.pas_de_course()
    c.mode_trading = True
    c.etat.sans_cible = True
    c.etat.caler_la_cible()
    compteur = itertools.count()

    def signal():
        paire = ("EURUSD_otc", "AUDCAD_otc")[next(compteur) % 2]
        return Signal(pair=paire, direction=Direction.CALL,
                      decided_at_ms=T0_MS, expiry_sec=900, reason="script")

    c.chercher_un_signal = signal
    return c


@pytest.fixture(autouse=True)
def _sans_delai(monkeypatch):
    monkeypatch.setattr("maxprofit.live.plan_demo.DELAI_INDEPENDANCE_SEC", 0)


def test_trading_seul_sans_martingale_et_arret_a_l_objectif(tmp_path):
    c = _course_trading(tmp_path, ["loose", "win", "win", "win", "win"])
    c.passer_le_jour_si_besoin(100)
    for _ in range(6):
        c.tour()
    mises = c.courtier.mises_recues
    assert mises[0] == pytest.approx(2.50)
    assert mises[1] < mises[0], "après une perte la mise BAISSE : 1 % du solde"
    assert c.etat.journee.arret is Arret.OBJECTIF_ATTEINT
    assert c.etat.journee.resultat_pct >= 2.0
    joues = len(mises)
    c.tour()
    assert len(c.courtier.mises_recues) == joues, "en pause jusqu'à demain"
    assert c.etat.journee.cible is None, "pas de planning en trading seul"


def test_trading_seul_arret_sur_pertes_puis_reprise_le_lendemain(tmp_path):
    c = _course_trading(tmp_path, ["loose"] * 3 + ["win"])
    c.passer_le_jour_si_besoin(100)
    for _ in range(5):
        c.tour()
    assert len(c.courtier.mises_recues) == 3
    assert c.etat.journee.arret is not None
    assert not c.passer_le_jour_si_besoin(100), "même jour UTC : on attend"
    assert c.passer_le_jour_si_besoin(101)
    assert c.etat.journee.arret is None and c.etat.jour == 2
    c.tour()
    assert len(c.courtier.mises_recues) == 4


def test_au_repos_seulement_hors_session(tmp_path):
    c = _course_trading(tmp_path, [])
    assert c.au_repos()
    c.etat.trade_en_cours = ("EURUSD_otc", "call", 2.5, 0)
    assert not c.au_repos()


# --------------------------------------------------------------------------- #
# Le journal relu sur plusieurs campagnes
# --------------------------------------------------------------------------- #

def test_les_taux_par_paire_relisent_les_campagnes_precedentes(tmp_path):
    ancienne = _course_trading(tmp_path, ["win", "loose"])
    ancienne.passer_le_jour_si_besoin(100)
    ancienne.tour()
    ancienne.tour()
    neuve = JournalExecution(tmp_path / "t.db", campagne="neuve")
    assert neuve.toutes() == []
    assert len(neuve.toutes((ancienne.journal.campagne,))) == 2


def test_la_journee_utc_survit_au_redemarrage(tmp_path):
    from maxprofit.live.plan_demo import charger_etat, sauver_etat
    c = _course_trading(tmp_path, [])
    c.passer_le_jour_si_besoin(123)
    conn = open_read_write(tmp_path / "etat.db")
    sauver_etat(conn, "x", c.etat, 123)
    etat, _ = charger_etat(conn, "x", c.etat.plan)
    assert etat.journee_utc == 123


def test_pas_regle_le_nombre_de_pas_de_martingale():
    """Le nombre de pas existait dans la configuration (2 par défaut) sans
    qu'aucune commande ne permette de le changer."""
    from maxprofit.live.configuration import Configuration
    base = Configuration(capital=200, gagnants=1, trades=6)
    for n, nb_mises in ((1, 1), (2, 2), (3, 3)):
        c = base.modifiee("pas", str(n))
        assert c.pas_max == n
        assert len(c.mises()) == nb_mises, "une mise par pas"


def test_plus_de_pas_coute_plus_cher_par_session_perdue():
    from maxprofit.live.configuration import Configuration
    base = Configuration(capital=200, gagnants=1, trades=6)
    pertes = [base.modifiee("pas", str(n)).perte_session_pct()
              for n in (1, 2, 3)]
    assert pertes == sorted(pertes) and pertes[0] < pertes[-1]


def test_pas_refuse_une_valeur_illisible_ou_hors_bornes():
    import pytest

    from maxprofit.core.errors import BotError
    from maxprofit.live.configuration import Configuration
    for faux in ("zero", "0", "11", "-2"):
        with pytest.raises(BotError):
            Configuration().modifiee("pas", faux)


def test_le_nombre_de_pas_fait_partie_de_la_SIGNATURE():
    """Changer le nombre de pas doit être vu comme un nouveau réglage, sinon
    `/demarrer` répondrait « déjà lancé avec ce réglage » et la course
    garderait l'ancien."""
    from maxprofit.hosting.pilotage import signature
    from maxprofit.live.configuration import Configuration
    a = Configuration(pas_max=2)
    assert signature(a) != signature(a.modifiee("pas", "3"))


def test_pas_est_une_commande_de_pilotage():
    from maxprofit.hosting.pilotage import REGLAGES
    from maxprofit.hosting.telegram import COMMANDES, COMMANDES_DE_PILOTAGE
    assert "pas" in REGLAGES
    assert "pas" in COMMANDES_DE_PILOTAGE
    assert "pas" in {nom for nom, _ in COMMANDES}, "visible dans le menu"
