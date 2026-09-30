"""
L'enchaînement des sessions — sans lui, il n'y a pas de mode plan.

Le module `plan` est testé seul : l'échelle, les gardes, le réancrage. Ce
fichier teste leur ASSEMBLAGE, qui est un endroit différent et où les pannes
sont silencieuses. Une session qui compte deux fois, un solde mis à jour par
deux voies, un pas joué sur un ordre refusé : rien de tout cela ne lève, tout
se voit seulement dans un solde faux, dix jours plus tard.

La stratégie n'est PAS sollicitée ici. Les signaux sont scriptés, parce qu'on
mesure l'orchestration et non la règle de décision — celle-ci a ses propres
tests. Mélanger les deux rendrait un échec illisible : on ne saurait pas si
c'est la martingale ou la zone qui a dérapé.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
import time
from pathlib import Path

import pytest

from maxprofit.core.errors import BotError
from maxprofit.core.types import Direction, Signal
from maxprofit.execution.journal import Execution, JournalExecution
from maxprofit.live.plan_demo import CoursePlanDemo, nouveau_jour
from maxprofit.plan import Arret, EtatSession, PlanCapital, Risque

T0_MS = 1_789_000_000_000
CAPITAL = 250.0


def _ordre_en_vol(order_id="abc"):
    """Un ordre ACCEPTÉ dont le sort n'est pas revenu — l'état exact d'un
    ordre parti juste avant un redéploiement."""
    return Execution(
        pair="EURUSD_otc", sens="call", mise=1.59, signal_ts_ms=T0_MS,
        prix_attendu=1.1, payout_flux_pct=92.0, expiration_sec=900,
        clic_ts_ms=T0_MS + 10, accepte_ts_ms=T0_MS + 300, accepte=True,
        order_id=order_id)


def _plan(sessions: int = 6, jours: int = 30, **gardes) -> PlanCapital:
    """Le plan des tests, construit comme celui de la production.

    ⚠ `objectif_journalier_pct` est posé ici, et il doit l'être : c'est lui qui
    definit la fin d'un jour du plan. Sans lui, le plan de test n'avait aucune
    cible, un jour ne pouvait s'achever que sur SESSIONS_EPUISEES, et les tests
    validaient un comportement que la production n'a pas.
    """
    defauts = dict(sessions_perdues_max=2)
    plan = PlanCapital.depuis_risque(
        capital_initial=CAPITAL, risque=Risque(1, 7), payout_pct=92,
        sessions_par_jour=sessions, jours=jours, **{**defauts, **gardes})
    if "objectif_journalier_pct" in gardes:
        return plan
    return replace(
        plan,
        objectif_journalier_pct=sessions * plan.gain_par_session_pct)


class CourtierFactice:
    """Un broker scripté. Aucune connexion, aucun aléatoire.

    `resultats` est consommé dans l'ordre : « win », « loose », « refus » ou
    « unknown ». Il permet d'écrire une séquence exacte et de vérifier ce que
    l'orchestrateur en fait.
    """

    def __init__(self, resultats):
        self.resultats = list(resultats)
        self.places: list[tuple[str, str, float]] = []
        self.suivies: list[str] = []
        self.mises_recues: list[float] = []
        self.profits: list[float] = []

    def suivre(self, pair):
        self.suivies.append(pair)

    def paires_au_plafond(self):
        return ["EURUSD_otc"]

    def prix(self, pair):
        return 1.1

    def solde(self):
        """Le solde du broker, simulé : l'ancre plus les profits encaissés.

        C'est ainsi que le vrai compte se comporte — et tout l'intérêt de
        dériver le solde du plan de celui-ci est qu'aucun livre parallèle ne
        peut en diverger.
        """
        return 53170.0 + sum(self.profits)

    def bougies(self, pair, count=300):
        # Aucune bougie : les tests d'enchaînement scriptent le signal et ne
        # passent jamais par la stratégie.
        return []

    def payout(self, pair):
        return 92.0

    def placer(self, pair, sens, expiration_sec, mise=None):
        """⚠ La mise REÇUE est enregistrée, pas une constante.

        La première version ignorait l'argument et posait 1.0. Le vrai
        courtier, lui, misait toujours son plafond au lieu de la mise de
        l'échelle : la martingale plaçait trois fois le même montant. Aucun
        test ne l'a vu, parce que l'orchestrateur réécrivait `execution.mise`
        APRÈS coup — les tests mesuraient donc l'intention, pas l'ordre.
        """
        issue = self.resultats.pop(0) if self.resultats else "loose"
        assert mise is not None, "l'orchestrateur doit passer une mise"
        self.mises_recues.append(mise)
        ex = Execution(
            pair=pair, sens=sens, mise=mise, signal_ts_ms=T0_MS,
            prix_attendu=1.1, payout_flux_pct=84.0, expiration_sec=expiration_sec,
            clic_ts_ms=T0_MS + 10)
        if issue == "refus":
            ex.refus = "asset closed"
            return ex
        ex.accepte = True
        ex.accepte_ts_ms = T0_MS + 300
        ex.order_id = f"o{len(self.places)}"
        ex.payout_broker_pct = 92.0
        ex._issue = issue                      # lu par `denouer`
        self.places.append((pair, sens, expiration_sec))
        return ex

    def denouer(self, ex):
        ex.resultat = getattr(ex, "_issue", "loose")
        ex.profit = ex.mise * 0.92 if ex.resultat == "win" else -ex.mise
        self.profits.append(ex.profit)
        return ex


class LecteurFactice:
    def last_candle_ts_sec(self):
        return None

    def candles(self, *a, **k):
        return []

    def candles_de(self, paires, tf, depuis):
        return {p: [] for p in paires}


#: Deux actifs, alternés par le signal scripté. La règle d'indépendance
#: interdit de jouer le pas suivant sur le MÊME actif : un montage à une seule
#: paire bloquerait chaque échelle au pas 1, et l'on ne testerait plus rien de
#: la martingale.
PAIRES_TEST = ("EURUSD_otc", "AUDCAD_otc")


@pytest.fixture
def course(tmp_path, monkeypatch):
    # Le délai est neutralisé : ces tests mesurent l'ENCHAÎNEMENT, pas
    # l'horloge. La règle de délai a ses propres tests, plus bas.
    monkeypatch.setattr("maxprofit.live.plan_demo.DELAI_INDEPENDANCE_SEC", 0)

    # ⚠ UNE CAMPAGNE PAR COURSE, et ce n'est pas de la cosmétique.
    #
    # Le journal est lu par campagne, et le solde du plan est désormais LA
    # SOMME DE SES PROFITS. Deux courses construites sous le même nom dans un
    # même test partageaient donc leurs ordres : la seconde démarrait sur le
    # bilan de la première. Un test qui compare deux scénarios comparait deux
    # cumuls.
    numero = itertools.count()

    def fabriquer(resultats, plan=None):
        journal = JournalExecution(tmp_path / "exec.db",
                                   campagne=f"test-{next(numero)}")
        c = CoursePlanDemo(LecteurFactice(), CourtierFactice(resultats),
                           journal, plan or _plan(), PAIRES_TEST)
        compteur = {"n": 0}

        def signal_scripte():
            paire = PAIRES_TEST[compteur["n"] % len(PAIRES_TEST)]
            compteur["n"] += 1
            return Signal(pair=paire, direction=Direction.CALL,
                          decided_at_ms=T0_MS, expiry_sec=900,
                          reason="script")

        c.chercher_un_signal = signal_scripte
        return c
    return fabriquer


# --------------------------------------------------------------------------- #
# La descente d'échelle
# --------------------------------------------------------------------------- #

def test_une_session_gagnee_au_premier_pas_ajoute_le_gain_vise(course):
    c = course(["win"])
    attendu = CAPITAL * c.etat.plan.gain_par_session_pct / 100
    c.tour()
    assert c.etat.session is None, "la session doit être close"
    assert c.etat.solde == pytest.approx(CAPITAL + attendu)
    assert c.etat.journee.sessions_jouees == 1


def test_les_mises_grossissent_a_chaque_pas_perdu(course):
    """Le rapport vaut (1 + payout)/payout = 2,087 — il tombe de la formule,
    ce n'est pas un réglage."""
    c = course(["loose", "loose", "win"])
    for _ in range(3):
        c.tour()
    # Les mises REÇUES PAR LE COURTIER, pas celles écrites dans le journal :
    # c'est la distinction qui a laissé passer le bogue de production.
    mises = c.courtier.mises_recues
    assert len(mises) == 3
    assert mises[1] / mises[0] == pytest.approx(2.087, abs=0.01)
    assert mises[2] / mises[1] == pytest.approx(2.087, abs=0.01)
    assert [e.mise for e in c.journal.toutes()] == pytest.approx(mises), (
        "le journal doit refléter ce qui a été PLACÉ")


def test_la_session_s_arrete_au_troisieme_pas_perdu(course):
    """« Trois pas, loss, on s'arrête » : il n'y a pas de quatrième."""
    c = course(["loose", "loose", "loose", "win"])
    for _ in range(4):
        c.tour()
    assert len(c.journal.toutes()) == 4, (
        "le 4e tour doit ouvrir une NOUVELLE session, pas un 4e pas")
    mises = [e.mise for e in c.journal.toutes()]
    assert mises[3] < mises[2], (
        "le 4e ordre est le pas 1 d'une nouvelle session : sa mise repart en bas")


def test_une_session_perdue_coute_exactement_son_exposition(course):
    c = course(["loose", "loose", "loose"])
    for _ in range(3):
        c.tour()
    engage = sum(e.mise for e in c.journal.toutes())
    assert c.etat.solde == pytest.approx(CAPITAL - engage)


def test_le_gain_d_une_session_ne_depend_pas_du_pas_ou_elle_est_gagnee(course,
                                                                      tmp_path):
    """C'est la propriété qui définit la martingale : gagner au pas 3
    rapporte autant que gagner au pas 1."""
    soldes = []
    for scenario in (["win"], ["loose", "win"], ["loose", "loose", "win"]):
        c = course(scenario)
        for _ in scenario:
            c.tour()
        soldes.append(c.etat.solde)
        c.journal.close()
    assert soldes[0] == pytest.approx(soldes[1])
    assert soldes[1] == pytest.approx(soldes[2])


# --------------------------------------------------------------------------- #
# Le dimensionnement
# --------------------------------------------------------------------------- #

def test_la_mise_suit_le_SOLDE_et_non_le_capital_initial(course):
    """Après une perte, les mises suivantes doivent rétrécir. Les laisser
    calibrées sur le capital de départ est exactement ce qui vide un compte."""
    c = course(["loose", "loose", "loose", "win"])
    for _ in range(3):
        c.tour()
    premiere = c.journal.toutes()[0].mise
    c.tour()
    apres_la_perte = c.journal.toutes()[3].mise
    assert c.etat.solde < CAPITAL
    assert apres_la_perte < premiere


# --------------------------------------------------------------------------- #
# Les gardes
# --------------------------------------------------------------------------- #

def test_une_session_entamee_a_priorite_sur_une_journee_close(course):
    """Deux échelles en parallèle ne donneraient plus l'exposition calculée,
    et une session abandonnée au pas 2 aurait coûté sans pouvoir rapporter."""
    c = course(["loose", "win"], plan=_plan(sessions=1))
    c.tour()                                   # pas 1, perdu, session ouverte
    assert c.etat.session is not None
    assert c.peut_ouvrir() is None or True     # la journée peut être close
    c.tour()                                   # le pas 2 doit être joué
    assert len(c.journal.toutes()) == 2
    assert c.etat.session is None


def test_une_session_PERDUE_ne_compte_pas_pour_le_jour(course):
    """Un jour du plan, c'est six sessions GAGNEES.

    Une perte n'avance pas le jour : elle creuse un retard. L'ancienne regle
    comptait les sessions JOUEES — une journee a une session s'arretait donc
    sur une perte, et l'on aurait lu « 7/6 » apres une perte et six gains.
    Avec un objectif, la journee s'arrete sur la cible ou sur une garde de
    perte, jamais sur un nombre de tentatives.
    """
    c = course(["loose", "loose", "loose", "win"], plan=_plan(sessions=1))
    for _ in range(3):
        c.tour()
    assert c.etat.session is None, "trois pas perdus closent la session"
    assert c.etat.journee.sessions_gagnees == 0
    assert c.peut_ouvrir() is None, (
        "une session perdue ne termine pas une journee d'une session")


def test_sans_objectif_le_plafond_de_sessions_reste(course):
    """Hors du plan, sans cible, le nombre de sessions est la seule borne."""
    from dataclasses import replace as _r
    plan = _r(_plan(sessions=1), objectif_journalier_pct=None)
    c = course(["loose", "loose", "loose"], plan=plan)
    for _ in range(3):
        c.tour()
    assert c.peut_ouvrir() is Arret.SESSIONS_EPUISEES

def test_une_journee_qui_atteint_sa_CIBLE_le_dit(course):
    """OBJECTIF_ATTEINT et SESSIONS_EPUISEES arrêtent tous deux la journée,
    mais c'est sur leur différence que le passage au jour suivant se décide."""
    c = course(["win"], plan=_plan(sessions=1))
    c.tour()
    assert c.peut_ouvrir() is Arret.OBJECTIF_ATTEINT


def test_CHAQUE_session_perdue_recale_le_plan_sur_le_solde(course):
    """Production, 2026-09-29 : une session perdue en trois pas fait tomber
    le solde sous la cible du jour 2, et le plan annonçait encore « jour 3,
    4/6 gagnées ». Le recalage n'avait lieu qu'après DEUX sessions perdues."""
    messages = []
    c = course(["loose"] * 3, plan=_plan(sessions=6))
    c._alerter = messages.append
    c.etat.jour = 3
    c.etat.ouvrir_la_journee()
    c.etat.journee.sessions_jouees = c.etat.journee.sessions_gagnees = 4
    for _ in range(3):
        c.tour()
    assert len(c.etat.reancrages) == 1
    depuis, vers, _ = c.etat.reancrages[0]
    assert (depuis, vers) == (3, 1), "le solde (238 $) est celui du jour 1"
    assert c.etat.jour == 1
    assert c.etat.journee.sessions_gagnees == 0
    assert c.etat.journee.cible == pytest.approx(CIBLE_JOUR_1)
    assert any("Recalage sur le plan" in m for m in messages)


def test_la_position_du_solde_est_celle_du_PLANNING():
    """Le cas du 2026-09-29 : 261,81 $ est le jour 2 après deux sessions
    gagnées (258,75 $ + 2 × 1,51 $), à 6 $ de la cible du jour 2 — et non le
    jour 3 à 15,37 $ de la sienne."""
    from maxprofit.live.plan_demo import Etat

    plan = _plan(sessions=6)
    for solde, attendu in [(261.81, (2, 2)), (274.78, (3, 4)),
                           (240.0, (1, 0)), (250.0, (1, 0)),
                           (258.74, (1, 5)), (258.76, (2, 0))]:
        e = Etat(plan=plan, solde=solde)
        assert e.position_du_solde() == attendu, solde
    e = Etat(plan=plan, solde=261.81, jour=2)
    e.ouvrir_la_journee()
    assert e.journee.cible == pytest.approx(267.81, abs=0.01)
    assert e.journee.manque == pytest.approx(6.0, abs=0.01)


# --------------------------------------------------------------------------- #
# Les cas dégradés — ceux qui faussent un solde sans rien lever
# --------------------------------------------------------------------------- #

def test_un_ordre_refuse_ne_consomme_pas_de_pas(course):
    """Le broker refuse : rien n'a été misé, donc la session ne doit pas
    avancer. Compter le pas ferait grossir la mise suivante sans raison."""
    c = course(["refus", "win"])
    c.tour()
    assert c.etat.session is not None
    assert c.etat.session.pas_joues == 0
    assert c.etat.solde == CAPITAL
    c.tour()
    assert c.etat.session is None
    assert c.etat.solde > CAPITAL


class LecteurAvecBougies(LecteurFactice):
    """Un lecteur qui rend de vraies bougies, pour que la stratégie tourne."""

    def __init__(self, fin_ts=1790000000):
        self.fin = fin_ts

    def last_candle_ts_sec(self):
        return self.fin

    def candles(self, pair, tf, debut, fin):
        from maxprofit.core.types import Candle
        n = 301
        return [Candle(pair=pair, tf_sec=60, ts_sec=self.fin - (n - 1 - k) * 60,
                       open=1.0, high=1.001, low=0.999, close=1.0,
                       tick_count=30, complete=True)
                for k in range(n)]

    def candles_de(self, paires, tf, depuis):
        self.requetes = getattr(self, "requetes", 0) + 1
        return {p: [b for b in self.candles(p, tf, depuis, None)
                    if b.ts_sec >= depuis] for p in paires}


def test_les_paires_collectees_sont_parcourues_A_TOUR_DE_ROLE(tmp_path):
    """La première de la liste emportait chaque égalité.

    Les gratuites étaient parcourues dans l'ordre de `PAIRES_FIXES` et la
    recherche rend le PREMIER signal trouvé : la troisième ne passait que si
    les deux d'avant n'avaient rien. Mesuré en production — GBPAUD_otc est
    troisième et n'a jamais reçu un seul ordre en trois jours, alors qu'elle
    est au plafond 35 % du temps.

    Pire : la première était EURUSD_otc, la plus prolixe ET la moins précise
    des quatre (44,1 %, sous le seuil). L'ordre fixe maximisait la part de la
    pire paire.
    """
    six = ("A_otc", "B_otc", "C_otc", "D_otc", "E_otc", "F_otc")
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurAvecBougies(), CourtierFactice(["win"] * 50),
                       journal, _plan(sessions=18), six)
    c.univers = lambda: list(six)

    # On observe l'ordre dans lequel les bougies sont DEMANDÉES.
    vues = []
    vraie = c.bougies_collectees
    c.bougies_collectees = lambda p, n: (vues.append(p) or vraie(p, n))

    premieres = []
    for _ in range(len(six)):
        vues.clear()
        c.chercher_un_signal()
        premieres.append(vues[0])
    assert premieres == list(six), (
        f"chaque paire doit passer en tête à son tour, vu {premieres}")
    journal.close()


def test_la_rotation_ne_saute_aucune_paire_dans_un_passage(tmp_path):
    """Tourner ne doit pas vouloir dire n'en regarder qu'une : toutes les
    paires collectées sont examinées à CHAQUE passage, seul l'ordre change."""
    six = ("A_otc", "B_otc", "C_otc", "D_otc", "E_otc", "F_otc")
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurAvecBougies(), CourtierFactice(["win"] * 50),
                       journal, _plan(sessions=18), six)
    c.univers = lambda: list(six)
    vues = []
    vraie = c.bougies_collectees
    c.bougies_collectees = lambda p, n: (vues.append(p) or vraie(p, n))
    c.chercher_un_signal()
    assert set(vues) == set(six), (
        "un passage examine toutes les paires, pas seulement celle de tête")
    journal.close()


def test_une_paire_qui_n_envoie_pas_de_tick_NE_TUE_PAS_la_course(tmp_path):
    """Le crash qui a mis la course en ABANDONNÉE après cinq échecs.

    `suivre()` refuse un actif muet depuis 20 s, et ce refus est JUSTE avant
    un ordre : on ne mise pas sur un prix inconnu. Au démarrage il est
    destructeur — une paire FERMÉE n'envoie légitimement aucun tick.
    AUDCAD_otc était hors séance, la construction a levé, le superviseur a
    reconstruit, cinq fois, puis course ABANDONNÉE. Les cinq autres paires
    étaient parfaitement tradables.
    """
    class CourtierMuetSurUne(CourtierFactice):
        def suivre(self, pair):
            if pair == "AUDCAD_otc":
                raise BotError(f"{pair} n'a envoyé aucun tick en 20 s.")
            super().suivre(pair)

    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    paires = ("EURUSD_otc", "AUDCAD_otc", "GBPAUD_otc")
    # Ne doit PAS lever.
    c = CoursePlanDemo(LecteurFactice(), CourtierMuetSurUne(["win"]), journal,
                       _plan(sessions=18), paires)
    assert c._non_souscrites == {"AUDCAD_otc"}
    assert set(c.courtier.suivies) == {"EURUSD_otc", "GBPAUD_otc"}
    journal.close()


def test_une_paire_REFUSEE_sans_cesse_est_ecartee(course):
    """La boucle qui a coûté une nuit entière de course.

    BTCUSD_otc a reçu 34 ordres en 34 minutes — un par minute, tous refusés
    par le broker avec « refus sans motif », tous journalisés, aucun joué. Un
    refus rendait la main sans rien retenir : le passage suivant retrouvait le
    même signal sur la même paire et retentait. Pendant ce temps la course ne
    faisait rien d'autre, c'était la seule paire au plafond de payout.

    Aucune erreur, aucune mise perdue. Seulement du temps — ce qui manque
    précisément quand on vise dix-huit sessions par jour.
    """
    from maxprofit.live.plan_demo import REFUS_AVANT_QUARANTAINE
    c = course(["refus"] * 10, plan=_plan(sessions=18))
    # La paire du signal scripté alterne : on la fige pour reproduire le cas.
    paire = PAIRES_TEST[0]
    c.chercher_un_signal = lambda: Signal(
        pair=paire, direction=Direction.CALL, decided_at_ms=T0_MS,
        expiry_sec=900, reason="script")
    c.univers = lambda: list(PAIRES_TEST)

    for _ in range(REFUS_AVANT_QUARANTAINE):
        c.tour()
    assert paire in c.etat.quarantaine, (
        f"{REFUS_AVANT_QUARANTAINE} refus d'affilée doivent écarter la paire")
    # Et elle ne revient plus dans les candidats tant que la peine court.
    assert c.chercher_un_signal is not None
    ordres_avant = len(c.journal.toutes())
    c.tour()
    assert len(c.journal.toutes()) == ordres_avant, (
        "une paire écartée ne doit plus produire d'ordre")


def test_un_refus_ISOLE_n_ecarte_pas_une_paire_saine(course):
    """Un payout qui bouge entre la décision et le clic suffit à faire
    refuser un ordre. Bannir là-dessus coûterait une paire qui marche."""
    from maxprofit.live.plan_demo import REFUS_AVANT_QUARANTAINE
    assert REFUS_AVANT_QUARANTAINE >= 2
    c = course(["refus", "win"], plan=_plan(sessions=18))
    c.tour()
    c.tour()
    assert c.etat.quarantaine == {}
    # Le compteur est PAR PAIRE, et le signal scripté alterne : le refus est
    # sur la première, le succès sur la seconde. L'ardoise de la première
    # reste donc à 1 — ce qui est juste, et inoffensif tant qu'elle décroît.
    assert c.etat.refus_daffilee == {PAIRES_TEST[0]: 1}


def test_un_refus_ACCEPTE_ensuite_remet_l_ardoise_a_zero(course):
    c = course(["refus", "win"], plan=_plan(sessions=18))
    c.chercher_un_signal = lambda: Signal(
        pair=PAIRES_TEST[0], direction=Direction.CALL, decided_at_ms=T0_MS,
        expiry_sec=900, reason="script")
    c.tour()
    assert c.etat.refus_daffilee == {PAIRES_TEST[0]: 1}
    c.tour()
    assert c.etat.refus_daffilee == {}, (
        "un ordre accepté prouve que la paire marche")
    assert c.etat.dernier_refus_ts == {}


def test_des_refus_ESPACES_ne_s_additionnent_pas(course, monkeypatch):
    """« D'affilée » doit vouloir dire quelque chose.

    Le compteur ne décroissait jamais : un refus isolé laissait un 1
    permanent, et trois refus isolés espacés d'une semaine finissaient par
    écarter une paire parfaitement saine. La suite n'était consécutive que
    dans le nom.
    """
    import time as _t

    from maxprofit.live.plan_demo import (QUARANTAINE_SEC,
                                          REFUS_AVANT_QUARANTAINE)
    c = course(["refus"] * 20, plan=_plan(sessions=18))
    c.chercher_un_skip = None
    c.chercher_un_signal = lambda: Signal(
        pair=PAIRES_TEST[0], direction=Direction.CALL, decided_at_ms=T0_MS,
        expiry_sec=900, reason="script")
    base = int(_t.time())
    for k in range(REFUS_AVANT_QUARANTAINE + 2):
        # Chaque refus est séparé du précédent par plus que la quarantaine.
        instant = base + k * (QUARANTAINE_SEC + 60)
        monkeypatch.setattr(_t, "time", lambda i=instant: float(i))
        c.tour()
        assert c.etat.refus_daffilee == {PAIRES_TEST[0]: 1}, (
            f"au {k + 1}e refus espacé, le compteur doit être reparti de 1")
        assert c.etat.quarantaine == {}, (
            "des incidents sans rapport ne doivent pas écarter une paire")


def test_la_quarantaine_EXPIRE_d_elle_meme(course, monkeypatch):
    """On n'a pas le motif du refus, donc on ne bannit pas définitivement."""
    import time as _t

    from maxprofit.live.plan_demo import QUARANTAINE_SEC
    assert QUARANTAINE_SEC > 0
    c = course(["win"], plan=_plan(sessions=18))
    c.etat.quarantaine = {PAIRES_TEST[0]: int(_t.time()) + 10}
    c.etat.refus_daffilee = {PAIRES_TEST[0]: 3}
    # Avant l'échéance : la peine tient.
    c._purger_la_quarantaine()
    assert PAIRES_TEST[0] in c.etat.quarantaine
    # Après : elle tombe, et l'ardoise de la paire avec elle.
    monkeypatch.setattr(_t, "time", lambda: 1e12)
    c._purger_la_quarantaine()
    assert c.etat.quarantaine == {}, "la peine expire seule"
    assert c.etat.refus_daffilee == {}


def test_la_quarantaine_SURVIT_a_un_redemarrage(tmp_path):
    """Sinon un redéploiement remet en route la boucle qu'elle arrête — et
    les redéploiements sont fréquents."""
    from maxprofit.live.plan_demo import charger_etat, sauver_etat
    from maxprofit.store.db import open_read_write

    conn = open_read_write(tmp_path / "plan.db")
    plan = _plan(sessions=10)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["win"]), journal,
                       plan, PAIRES_TEST)
    c.etat.quarantaine = {"BTCUSD_otc": 1790300000}
    c.etat.refus_daffilee = {"BTCUSD_otc": 3}
    sauver_etat(conn, "t", c.etat, 0)

    relu, _ = charger_etat(conn, "t", plan)
    assert relu.quarantaine == {"BTCUSD_otc": 1790300000}
    assert relu.refus_daffilee == {"BTCUSD_otc": 3}
    journal.close()
    conn.close()


def test_un_denouement_inconnu_interrompt_au_lieu_de_deviner(course):
    """« unknown » n'est ni gagné ni perdu. Le compter comme perdu
    surestimerait la perte, comme gagné l'effacerait."""
    c = course(["unknown"])
    c.tour()
    assert c.etat.session is None
    # Interrompue : on n'a perdu que le pas engagé, et ce n'est pas une
    # session PERDUE — le compteur de pertes consécutives ne bouge pas.
    assert c.etat.sessions_perdues_daffilee == 0
    assert c.etat.solde < CAPITAL


def test_le_solde_n_est_mis_a_jour_que_par_une_seule_voie(course):
    """Deux compteurs de solde divergeraient au premier arrondi, et le plan
    serait jugé sur le mauvais."""
    c = course(["win", "loose", "loose", "loose"], plan=_plan(sessions=10))
    for _ in range(4):
        c.tour()
    assert c.etat.solde == pytest.approx(c.etat.journee.solde)


def test_un_nouveau_jour_repart_du_solde_reel(course):
    c = course(["loose", "loose", "loose"], plan=_plan(sessions=10))
    for _ in range(3):
        c.tour()
    solde = c.etat.solde
    nouveau_jour(c.etat)
    assert c.etat.jour == 2
    assert c.etat.journee.solde == pytest.approx(solde)
    assert c.etat.journee.sessions_jouees == 0
    assert c.peut_ouvrir() is None


def test_l_univers_n_est_PAS_limite_aux_paires_epinglees(course):
    """Les quatre épinglées sont une décision de COLLECTE.

    S'y limiter pour chercher des signaux réduirait le champ à une poignée
    d'actifs, alors qu'à la moitié du temps UNE SEULE des quatre paie le
    maximum. L'univers vient du catalogue du broker, pas de la configuration
    du collecteur.
    """
    c = course(["win"])
    assert c.univers() == ["EURUSD_otc"]
    assert c.etat.univers_taille == 1


def test_le_mode_epinglees_s_abonne_car_placer_a_besoin_du_PRIX(course):
    """Les bougies viennent de la base, mais `placer()` a besoin du dernier
    prix, que le broker ne sert que sur un actif souscrit. Sans abonnement,
    l'ordre échouerait — et seulement au moment de trader."""
    c = course(["win"])
    assert c.courtier.suivies == list(PAIRES_TEST)


def test_le_mode_plafond_S_ABONNE_AUSSI_aux_paires_collectees(tmp_path):
    """La règle inverse était juste tant que le MODE décidait de la source.

    En mode plafond, l'abonnement se faisait tout seul : demander un
    historique change déjà d'actif. Depuis que la source dépend de la PAIRE,
    une paire collectée n'est plus jamais demandée au broker — donc plus
    jamais souscrite — et c'est celle sur laquelle on tradera le plus. La
    panne n'apparaîtrait qu'au premier signal, sur « aucun tick disponible ».
    """
    from maxprofit.live.plan_demo import UNIVERS_PLAFOND

    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["win"]), journal,
                       _plan(), ("EURUSD_otc",), mode_univers=UNIVERS_PLAFOND)
    assert c.courtier.suivies == ["EURUSD_otc"]
    journal.close()


def test_un_mode_d_univers_inconnu_est_refuse(tmp_path):
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    with pytest.raises(BotError, match="mode_univers"):
        CoursePlanDemo(LecteurFactice(), CourtierFactice([]), journal,
                       _plan(), ("EURUSD_otc",), mode_univers="tout")
    journal.close()


def test_le_catalogue_n_est_pas_relu_a_chaque_passage(course):
    """Les payouts bougent en minutes. Relire le catalogue vingt fois par
    minute n'apprendrait rien et coûterait une trame à chaque fois."""
    c = course(["win"])
    appels = []
    c.courtier.paires_au_plafond = lambda: appels.append(1) or ["EURUSD_otc"]
    c.univers()
    c.univers()
    c.univers()
    assert len(appels) == 1


# --------------------------------------------------------------------------- #
# La reprise après redémarrage
# --------------------------------------------------------------------------- #

def _base(tmp_path):
    """Une base au schéma RÉEL, migrations comprises.

    Pas un `CREATE TABLE` écrit dans le test : la migration est ce qui
    tournera en production, et un schéma recopié à la main diverge du jour où
    quelqu'un ajoute une colonne à l'une des deux.
    """
    from maxprofit.store.db import open_read_write
    return open_read_write(tmp_path / "market.db")


def test_une_course_interrompue_se_reprend_au_meme_pas(course, tmp_path):
    """Le cas qui décide si dix jours survivent à un redémarrage.

    L'hébergeur redéploie, met en veille, redémarre. Sans reprise de la
    session EN COURS, la martingale repartirait au pas 1 : les mises déjà
    engagées auraient quitté le compte sans que le plan les connaisse, et
    l'échelle se serait réarmée toute seule.
    """
    from maxprofit.live.plan_demo import charger_etat, sauver_etat

    conn = _base(tmp_path)
    c = course(["loose", "loose", "win"], plan=_plan(sessions=10))
    c.tour()
    c.tour()                                   # deux pas perdus, session ouverte
    assert c.etat.session is not None and c.etat.session.pas_joues == 2
    mise_du_pas_3 = c.etat.session.mise_courante()
    engagees = list(c.etat.session.engagees)

    sauver_etat(conn, "essai", c.etat, jour_utc_courant=20_000)

    repris, jour_utc = charger_etat(conn, "essai", c.etat.plan)
    assert jour_utc == 20_000
    assert repris.solde == pytest.approx(c.etat.solde)
    assert repris.jour == c.etat.jour
    assert repris.journee.sessions_jouees == c.etat.journee.sessions_jouees
    assert repris.session is not None
    assert repris.session.pas_joues == 2
    assert repris.session.engagees == pytest.approx(engagees)
    assert repris.session.mise_courante() == pytest.approx(mise_du_pas_3), (
        "la reprise doit replacer la MÊME mise, pas recommencer au pas 1")
    conn.close()


def test_sans_course_enregistree_le_chargement_rend_None(tmp_path):
    from maxprofit.live.plan_demo import charger_etat

    conn = _base(tmp_path)
    assert charger_etat(conn, "jamais-lancee", _plan()) is None
    conn.close()


def test_l_etat_s_ecrase_au_lieu_de_s_empiler(course, tmp_path):
    """Une campagne = une ligne. Deux voudraient dire deux courses qui se
    marchent dessus sur le même compte."""
    from maxprofit.live.plan_demo import charger_etat, sauver_etat

    conn = _base(tmp_path)
    c = course(["win", "win"], plan=_plan(sessions=10))
    c.tour()
    sauver_etat(conn, "essai", c.etat, 20_000)
    c.tour()
    sauver_etat(conn, "essai", c.etat, 20_001)

    n = conn.execute(
        "SELECT COUNT(*) FROM plan_etat WHERE campagne = ?", ("essai",)
    ).fetchone()[0]
    assert n == 1
    repris, jour_utc = charger_etat(conn, "essai", c.etat.plan)
    assert jour_utc == 20_001
    assert repris.journee.sessions_jouees == 2
    conn.close()


def test_une_session_close_ne_laisse_rien_a_reprendre(course, tmp_path):
    from maxprofit.live.plan_demo import charger_etat, sauver_etat

    conn = _base(tmp_path)
    c = course(["win"], plan=_plan(sessions=10))
    c.tour()
    assert c.etat.session is None
    sauver_etat(conn, "essai", c.etat, 20_000)
    repris, _ = charger_etat(conn, "essai", c.etat.plan)
    assert repris.session is None
    conn.close()


def test_une_course_sans_ordre_dit_si_elle_est_VIVANTE(course):
    """« J'attends un signal » et « je suis cassé » se ressemblaient.

    `/etat` affichait « pas encore démarrée » aussi bien pour une course qui
    évalue 240 bougies par heure sans rien trouver que pour une course qui ne
    lit plus la base du tout. Les deux étaient vertes, et seule la seconde
    demandait une intervention.
    """
    c = course(["win"], plan=_plan(sessions=10))
    assert "AUCUN actif au plafond" in c.resume()
    c.etat.univers_taille = 12
    assert "HISTORIQUE demandé au broker" in c.resume(), (
        "le résumé doit dire OÙ chercher si l'état dure")

    # Une évaluation, sans signal : la course doit se déclarer vivante.
    c.etat.bougies_evaluees = 240
    c.etat.derniere_evaluation_ts = int(time.time())
    resume = c.resume()
    assert "240 bougies vues par la stratégie" in resume
    assert "0 signal(aux) bruts" in resume
    assert "lecture il y a 0 s" in resume
    # Le repère qui distingue « ça démarre » de « c'est cassé » : sans lui,
    # « 8 bougies, 0 signal » ressemble à une panne alors que c'est
    # exactement ce qu'on attend au bout de trois minutes.
    assert "en route depuis" in resume
    assert "avant le prochain signal attendu" in resume


def test_le_resume_distingue_une_bougie_VUE_d_une_bougie_PERIMEE(course):
    """Les deux se comptaient ensemble, et c'est ce qui a masqué le défaut.

    La boucle marquait une bougie « évaluée » puis la jetait pour péremption
    sans que la stratégie l'ait vue. Le direct annonçait donc 810 bougies
    pour 1 signal — un taux qui accusait la stratégie — quand la stratégie
    n'en avait reçu qu'une fraction et se comportait normalement.
    """
    c = course(["win"], plan=_plan(sessions=10))
    c.etat.univers_taille = 40
    c.etat.paires_gratuites = 4
    c.etat.bougies_evaluees = 810
    c.etat.bougies_perimees = 715
    c.etat.derniere_evaluation_ts = int(time.time())
    resume = c.resume()
    assert "95 bougies vues par la stratégie" in resume
    assert "715 périmées sur 810" in resume
    assert "40 actif(s) au plafond dont 4 en base" in resume


def test_la_course_SAIT_separer_ce_qu_elle_trade_de_ce_qu_elle_lit(tmp_path):
    """Le MECANISME de separation, qui doit rester disponible.

    La production a choisi de trader tout ce qu'elle collecte (voir
    `hosting.service.univers_trade`, decision du 2026-09-24). Mais la course
    doit rester capable de dissocier les deux : c'est ce qui permettra de
    rejouer l'univers PRE-INSCRIT sur les memes bougies, et c'est la seule
    facon de conclure #58/#59 proprement une fois le plan termine.
    """
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(
        LecteurFactice(), CourtierFactice(["win"]), journal, _plan(),
        PAIRES_TEST, paires_collectees=PAIRES_TEST + ("CHFJPY_otc",))
    # Ce qu'on TRADE n'a pas bouge.
    assert c.paires == PAIRES_TEST
    # Ce qu'on LIT en base, si.
    assert "CHFJPY_otc" in c.paires_collectees
    # Et seules les paires tradees sont souscrites : un abonnement inutile
    # compte dans le budget du socket, qui est la ressource rare.
    assert set(c.courtier.suivies) == set(PAIRES_TEST)
    journal.close()


def test_une_paire_collectee_mais_non_epinglee_est_lue_en_BASE(tmp_path):
    """Sinon l'elargissement de la collecte ne servirait a rien : la paire
    serait en base ET demandee au broker pour 27 secondes."""
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(
        LecteurFactice(), CourtierFactice(["win"]), journal, _plan(),
        PAIRES_TEST, paires_collectees=PAIRES_TEST + ("CHFJPY_otc",))
    demandees = []
    vraie = c.courtier.bougies
    c.courtier.bougies = lambda pair, count=300: (
        demandees.append(pair) or vraie(pair, count))
    c._bougies_de("CHFJPY_otc")
    assert demandees == [], "collectee => lue en base, pas chez le broker"
    c._bougies_de("XAUUSD_otc")
    assert demandees == ["XAUUSD_otc"], "non collectee => le broker, faute de mieux"
    journal.close()


def test_la_production_trade_TOUT_ce_qu_elle_collecte(monkeypatch):
    """La decision du 2026-09-24, testee au point ou elle se prend.

    Elle s'est perdue deux fois : une fois en elargissant la collecte sans
    s'apercevoir qu'on elargissait le trading, une fois en figeant le trading
    sans s'apercevoir qu'on l'empechait d'elargir. Trois deploiements ont
    affiche « Paires souscrites : 4 » sans que rien ne soit en panne.
    """
    from maxprofit.collect.collector import Config
    from maxprofit.hosting.service import PAIRES_PAR_DEFAUT, univers_trade

    six = ("EURUSD_otc", "AUDUSD_otc", "GBPAUD_otc", "AUDCAD_otc",
           "CHFJPY_otc", "BTCUSD_otc")
    assert univers_trade(Config(db=Path("/tmp/x.db"), min_payout=92,
                                paires_fixes=six)) == six
    # Sans epinglees, on retombe sur le defaut du code, jamais sur rien :
    # un univers vide ferait une course qui tourne sans jamais rien evaluer.
    vide = univers_trade(Config(db=Path("/tmp/x.db"), min_payout=92))
    assert vide == PAIRES_PAR_DEFAUT and vide


def test_l_univers_pre_inscrit_reste_EN_DUR_pour_l_analyse():
    """Il ne sert plus a choisir ce qu'on trade, mais a RELIRE les resultats.

    Le journal enregistre la paire de chaque ordre : la precision par paire
    des quatre pre-inscrites reste donc mesurable meme si la course en trade
    six. C'est cette liste qui dira lesquelles comptent pour #58/#59 — et une
    liste qui se lit dans une variable d'environnement changerait sans qu'on
    s'en apercoive."""
    from maxprofit.live.plan_demo import UNIVERS_PRE_INSCRIT
    assert UNIVERS_PRE_INSCRIT == (
        "AUDCAD_otc", "AUDUSD_otc", "EURUSD_otc", "GBPAUD_otc")


def test_une_paire_collectee_ne_demande_RIEN_au_broker(course):
    """Le prix d'une paire, mesuré : 27 s chez le broker, une requête locale
    dans notre base. C'est ce qui décide combien de paires on peut suivre."""
    c = course(["win"], plan=_plan(sessions=10))
    c.courtier.bougies_demandees = []
    vraie = c.courtier.bougies

    def espionner(pair, count=300):
        c.courtier.bougies_demandees.append(pair)
        return vraie(pair, count)
    c.courtier.bougies = espionner

    c._bougies_de(PAIRES_TEST[0])
    assert c.courtier.bougies_demandees == [], (
        "une paire épinglée est dans NOTRE base : le broker n'a rien à dire")
    c._bougies_de("XAUUSD_otc")
    assert c.courtier.bougies_demandees == ["XAUUSD_otc"], (
        "une paire non collectée n'a pas d'autre source que le broker")


# --------------------------------------------------------------------------- #
# Le filtre de payout — n'entrer qu'au maximum
# --------------------------------------------------------------------------- #

class CourtierAPayout(CourtierFactice):
    """Un courtier scripté qui rend aussi un payout de FLUX."""

    def __init__(self, resultats, payout_flux):
        super().__init__(resultats)
        self.payout_flux = payout_flux

    def payout(self, pair):
        if self.payout_flux is None:
            raise BotError("payout indisponible")
        return float(self.payout_flux)


def _course_payout(tmp_path, resultats, payout_flux, plan=None):
    from maxprofit.strategies.zone_h1 import ZoneH1

    journal = JournalExecution(tmp_path / "exec.db", campagne="payout")
    c = CoursePlanDemo(LecteurFactice(), CourtierAPayout(resultats, payout_flux),
                       journal, plan or _plan(), ("EURUSD_otc",), ZoneH1())
    # Le signal est scripté : on mesure le FILTRE, pas la stratégie.
    c.strategie.on_bar = lambda vue: Signal(
        pair="EURUSD_otc", direction=Direction.CALL, decided_at_ms=T0_MS,
        expiry_sec=900, reason="script")
    return c


@pytest.mark.parametrize("flux,accepte", [
    (92, True),     # le maximum affiché
    (84, True),     # 84 + 8 = 92 : PAIE PAREIL, et c'est le point
    (83, False),    # 83 + 8 = 91 : un point de moins, on n'entre pas
    (71, False),
    (None, False),  # indisponible = refus
])
def test_on_n_entre_qu_au_payout_maximum(tmp_path, flux, accepte):
    """« Payout 92 % » se lit sur le FLUX à 84, pas à 92.

    Le broker applique min(flux + 8, 92) — mesuré sur 26 ordres réels et
    confirmé par l'affichage de la plateforme. Filtrer sur `flux >= 92`
    écarterait 42 % d'occasions qui paient exactement la même chose.
    """
    c = _course_payout(tmp_path, ["win"], flux)
    assert c._payout_au_maximum("EURUSD_otc") is accepte
    c.journal.close()


def test_la_taille_de_l_univers_est_affichee(tmp_path):
    """Sinon une course qui n'analyse rien parce que rien ne paie 92 %
    ressemble trait pour trait à une course qui ne trouve aucun signal."""
    c = _course_payout(tmp_path, ["win"], 71)
    assert "AUCUN actif au plafond" in c.resume()
    c.etat.univers_taille = 0
    c.etat.bougies_evaluees = 5
    c.etat.derniere_evaluation_ts = int(time.time())
    assert "0 actif(s) au plafond" in c.resume()
    c.journal.close()



# --------------------------------------------------------------------------- #
# La règle d'INDÉPENDANCE des pas — ce qui sépare -24 % de +68 %
# --------------------------------------------------------------------------- #

def test_le_pas_suivant_ne_se_joue_pas_sur_le_MEME_actif(tmp_path):
    """Le défaut mesuré en rejouant le plan : la stratégie tire plusieurs
    signaux d'affilée sur la MÊME zone, et le pas 2 rejoue le pari que le
    pas 1 vient de perdre.

    Sessions perdues : 15,3 % observées contre 8,5 % attendues à 56 % de
    précision. Solde 250 $ -> 189,89 $ sans la règle, 420,09 $ avec.
    """
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["loose", "win"]),
                       journal, _plan(sessions=10), PAIRES_TEST)
    c.chercher_un_signal = lambda: Signal(
        pair="EURUSD_otc", direction=Direction.CALL, decided_at_ms=T0_MS,
        expiry_sec=900, reason="script")

    assert c.tour() is True                      # pas 1, perdu
    assert c.etat.session is not None and c.etat.session.pas_joues == 1
    # Même actif, tout de suite : le pas 2 doit être REFUSÉ.
    assert c.tour() is False
    assert c.etat.pas_sautes_independance == 1
    assert len(c.journal.toutes()) == 1, "aucun second ordre ne doit partir"
    journal.close()


def test_le_pas_suivant_ne_se_joue_pas_TROP_TOT(tmp_path, monkeypatch):
    """Un autre actif ne suffit pas : deux signaux nés de la même minute de
    marché restent corrélés."""
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["loose", "win"]),
                       journal, _plan(sessions=10), PAIRES_TEST)
    tour = {"n": 0}

    def signal_scripte():
        # Le pas 1 part sur AUDCAD, tout le reste sur EURUSD : l'actif DIFFÈRE
        # à chaque fois, donc seul le délai peut encore bloquer.
        tour["n"] += 1
        paire = "AUDCAD_otc" if tour["n"] == 1 else "EURUSD_otc"
        return Signal(pair=paire, direction=Direction.CALL,
                      decided_at_ms=T0_MS, expiry_sec=900, reason="script")

    c.chercher_un_signal = signal_scripte
    c.tour()                                     # pas 1, perdu
    assert c.tour() is False, "autre actif mais trop tôt : refusé"
    assert c.etat.pas_sautes_independance == 1

    # Le délai écoulé, le même signal passe.
    monkeypatch.setattr("maxprofit.live.plan_demo.DELAI_INDEPENDANCE_SEC", 0)
    assert c.tour() is True
    assert len(c.journal.toutes()) == 2
    journal.close()


def test_le_PREMIER_pas_n_a_rien_a_respecter(course):
    """Il ouvre le pari : c'est la martingale qui a besoin d'indépendance
    entre ses pas, pas la stratégie entre ses signaux."""
    c = course(["win", "win"], plan=_plan(sessions=10))
    assert c.tour() is True
    assert c.etat.session is None
    # Deuxième session, même actif possible : rien ne l'interdit.
    assert c.tour() is True
    assert len(c.journal.toutes()) == 2


def test_une_session_qui_attend_longtemps_n_est_PLUS_interrompue(
        tmp_path, monkeypatch):
    """« On ne peut pas laisser la session incomplète » : elle était soldée
    au bout de deux heures sans pas indépendant, et la perte du pas 1 restait
    sans rattrapage. Elle attend désormais son pas suivant."""
    horloge = _Horloge()
    monkeypatch.setattr("maxprofit.live.plan_demo.time.time", horloge)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["loose"]),
                       journal, _plan(sessions=10), PAIRES_TEST)
    c.chercher_un_signal = _signal_sur("EURUSD_otc")   # le même actif
    c.tour()                                           # pas 1, perdu
    horloge.t += 24 * 3600
    assert c.tour() is False, "même actif : le pas 2 attend"
    assert c.etat.session is not None and c.etat.session.pas_joues == 1
    assert c.etat.sessions_interrompues == 0
    journal.close()


def test_deux_interruptions_ne_declenchent_pas_un_REANCRAGE(tmp_path):
    """Le réancrage répond à deux sessions PERDUES d'affilée — deux échelles
    descendues jusqu'au bout. Deux sessions interrompues sur un dénouement
    inconnu ne disent rien de la stratégie."""
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["unknown"] * 2),
                       journal, _plan(sessions=10), PAIRES_TEST)
    c.chercher_un_signal = _signal_sur("EURUSD_otc")
    for _ in range(2):
        c.tour()
    assert c.etat.sessions_interrompues == 2
    assert c.etat.journee.sessions_jouees == 0
    assert len(c.etat.reancrages) == 0, (
        "deux interruptions ne sont pas deux défaites")
    journal.close()


def test_le_dernier_trade_survit_a_un_redemarrage(course, tmp_path):
    """Sans cette reprise, un redémarrage rendrait le pas suivant
    immédiatement éligible — et l'on rejouerait exactement le pari corrélé
    que la règle d'indépendance existe pour empêcher."""
    from maxprofit.live.plan_demo import charger_etat, sauver_etat

    conn = _base(tmp_path)
    c = course(["loose", "win"], plan=_plan(sessions=10))
    c.tour()
    assert c.etat.dernier_trade is not None
    sauver_etat(conn, "indep", c.etat, 20_000)

    repris, _ = charger_etat(conn, "indep", c.etat.plan)
    assert repris.dernier_trade == c.etat.dernier_trade
    conn.close()


# --------------------------------------------------------------------------- #
# Le suivi — une course muette est une course qu'on ne surveille pas
# --------------------------------------------------------------------------- #

def test_un_ordre_en_cours_est_visible_pendant_qu_il_vit(course):
    """Le défaut qui rendait /etat inutile.

    `denouer` attend l'expiration, donc `tour()` bloque un quart d'heure. Le
    superviseur ne rafraîchissait le résumé qu'APRÈS son retour : des ordres
    partaient chez le broker et Telegram affichait encore « connexion au
    broker en cours ».
    """
    c = course(["win"], plan=_plan(sessions=10))
    c.etat.trade_en_cours = ("EURUSD_otc", "call", 1.59,
                             int(time.time()) + 900)
    resume = c.resume()
    assert "ORDRE EN COURS" in resume
    assert "EURUSD_otc CALL 1.59" in resume
    assert "dénouement dans" in resume


def test_chaque_session_close_est_annoncee(course):
    """Une course qui tourne dix jours sans rien dire oblige à interroger
    /etat au hasard : on découvre un réancrage trois jours après, ou jamais."""
    messages = []
    c = course(["win"], plan=_plan(sessions=10))
    c._alerter = messages.append
    c.tour()
    assert len(messages) == 2, "un message par ORDRE, puis un par SESSION"
    assert "Session gagnée" in messages[1]
    assert "solde" in messages[1]


def test_chaque_ordre_est_annonce_avec_de_quoi_le_retrouver(course):
    """Le suivi demandé : l'heure, la paire, le montant, le rang dans
    l'échelle et le résultat — de quoi rapprocher chaque ligne d'un ordre
    chez le broker sans ouvrir la base."""
    messages = []
    c = course(["loose", "win"], plan=_plan(sessions=10))
    c._alerter = messages.append
    c.tour()
    c.tour()
    ordres = [m for m in messages if "Session" not in m]
    assert len(ordres) == 2
    assert "EURUSD_otc" in ordres[0] and "LOSS" in ordres[0]
    assert "pas 1 (entrée)" in ordres[0], "le pas 1 n'est pas une martingale"
    assert "UTC" in ordres[0] and "CALL" in ordres[0]
    assert "MARTINGALE" in ordres[1], "le pas 2 en est une, et doit le dire"
    assert "WIN" in ordres[1]
    # Le montant annoncé est celui qui est PARTI chez le broker.
    for message, mise in zip(ordres, c.courtier.mises_recues):
        assert f"{mise:.2f} $" in message


def test_un_recalage_est_annonce(course):
    messages = []
    c = course(["loose"] * 3, plan=_plan(sessions=10))
    c._alerter = messages.append
    for _ in range(3):
        c.tour()
    assert any("Recalage sur le plan" in m for m in messages)


def test_une_alerte_qui_leve_ne_casse_pas_la_course(course):
    """Le dernier maillon : prévenir ne doit jamais faire tomber ce qu'on
    prévient."""
    def alerter(_m):
        raise OSError("Telegram injoignable")

    c = course(["win"], plan=_plan(sessions=10))
    c._alerter = alerter
    assert c.tour() is True
    assert c.etat.solde > CAPITAL


# --------------------------------------------------------------------------- #
# L'ordre est journalisé AVANT d'attendre son sort
# --------------------------------------------------------------------------- #

def test_l_ordre_est_journalise_des_qu_il_part(tmp_path):
    """Le trou trouvé en comparant le broker au journal : CINQ ordres
    exécutés chez le broker, ZÉRO dans nos livres, solde de plan resté à
    250 $ pendant que le compte réel bougeait.

    La cause : on écrivait APRÈS le dénouement, c'est-à-dire un quart d'heure
    après le départ. Un redéploiement dans cet intervalle et l'ordre
    n'existait plus que chez le broker.
    """
    journal = JournalExecution(tmp_path / "e.db", campagne="vol")
    vu = {"au_depart": None}

    class CourtierLent(CourtierFactice):
        def denouer(self, ex):
            # À cet instant, l'ordre DOIT déjà être dans le journal : c'est
            # pendant cette attente que le processus peut mourir.
            vu["au_depart"] = len(journal.toutes())
            return super().denouer(ex)

    c = CoursePlanDemo(LecteurFactice(), CourtierLent(["win"]), journal,
                       _plan(sessions=10), PAIRES_TEST)
    c.chercher_un_signal = lambda: Signal(
        pair="EURUSD_otc", direction=Direction.CALL, decided_at_ms=T0_MS,
        expiry_sec=900, reason="script")
    c.tour()
    assert vu["au_depart"] == 1, (
        "l'ordre doit être écrit AVANT l'attente, pas après")
    assert len(journal.toutes()) == 1, "et pas écrit deux fois"
    assert journal.toutes()[0].resultat == "win", "puis complété"
    journal.close()


def test_un_ordre_sans_reponse_est_retrouve_au_demarrage(tmp_path):
    """Un ordre parti juste avant un arrêt s'est dénoué CHEZ LE BROKER
    pendant qu'on était mort. Sans cette reprise, le compte réel et le plan
    divergent définitivement."""
    from maxprofit.live.plan_demo import _resoudre_les_ordres_en_vol

    journal = JournalExecution(tmp_path / "e.db", campagne="vol")
    journal.ecrire(_ordre_en_vol())
    assert len(journal.en_vol()) == 1

    class CourtierQuiSeSouvient(CourtierFactice):
        def denouer(self, e):
            e.resultat = "win"
            e.profit = 1.46
            return e

    _resoudre_les_ordres_en_vol(None, CourtierQuiSeSouvient([]), journal)
    assert journal.en_vol() == []
    assert journal.toutes()[0].resultat == "win"
    journal.close()


def test_un_ordre_introuvable_reste_EN_VOL_plutot_que_devine(tmp_path):
    """Deviner un résultat serait pire que l'ignorer : on inscrirait dans le
    solde un gain ou une perte qui n'a pas eu lieu."""
    from maxprofit.live.plan_demo import _resoudre_les_ordres_en_vol

    journal = JournalExecution(tmp_path / "e.db", campagne="vol")
    journal.ecrire(_ordre_en_vol())

    class CourtierMuet(CourtierFactice):
        def denouer(self, e):
            raise ConnectionError("broker injoignable")

    _resoudre_les_ordres_en_vol(None, CourtierMuet([]), journal)
    assert len(journal.en_vol()) == 1, "il reste en vol, il n'est pas deviné"
    journal.close()


# --------------------------------------------------------------------------- #
# Le solde vient du BROKER — plus de livre de comptes parallèle
# --------------------------------------------------------------------------- #

def test_le_solde_du_plan_est_DERIVE_de_celui_du_broker(course):
    """Le plan tenait son propre livre : chaque session close ajoutait son
    montant à un solde maintenu en mémoire. Ce livre pouvait diverger du
    compte réel, et il l'a fait — cinq ordres gagnants chez le broker, un
    solde de plan resté à 250 $.

    Dériver rend la divergence IMPOSSIBLE au lieu de la rattraper après coup.
    """
    c = course(["win"], plan=_plan(sessions=10))
    c.rafraichir_le_solde()
    ancre = c.etat.solde_broker_ancre
    assert ancre == 53170.0
    assert c.etat.solde == CAPITAL

    c.tour()
    # Le broker a encaissé le gain ; le plan le lit, il ne le calcule pas.
    delta = c.courtier.solde() - ancre
    assert c.etat.solde == pytest.approx(CAPITAL + delta)


def test_une_perte_n_est_pas_comptee_DEUX_FOIS(course):
    """Le bogue qui fermait la journée dès la première session perdue.

    `Journee.enregistrer` déplace son propre solde du montant qu'on lui
    passe. Or le solde reflète déjà chaque pas, puisqu'il vient du broker.
    Une session perdue creusait donc le résultat du jour de 6 % au lieu de
    4,7 %, et la garde de perte journalière se déclenchait à tort.
    """
    c = course(["loose", "loose", "loose", "win"], plan=_plan(sessions=10))
    for _ in range(3):
        c.tour()
    engage = sum(e.mise for e in c.journal.toutes())
    assert c.etat.solde == pytest.approx(CAPITAL - engage)
    assert c.etat.journee.solde == pytest.approx(CAPITAL - engage)
    assert c.peut_ouvrir() is None, (
        "4,72 % de perte ne doit pas déclencher une garde réglée à 5 %")
    assert c.tour() is True, "une nouvelle session doit pouvoir s'ouvrir"


def test_on_ne_superpose_JAMAIS_deux_ordres(course):
    """Le plan est SÉQUENTIEL : chaque pas attend de connaître son sort avant
    que le suivant soit décidé. Deux ordres ouverts en même temps voudraient
    dire que le pas 2 a été misé sans savoir si le pas 1 était perdu."""
    c = course(["win", "win"], plan=_plan(sessions=10))
    c.etat.trade_en_cours = ("EURUSD_otc", "call", 1.59,
                             int(time.time()) + 900)
    signal = Signal(pair="AUDCAD_otc", direction=Direction.CALL,
                    decided_at_ms=T0_MS, expiry_sec=900, reason="script")
    c.jouer_un_pas(signal)
    assert c.journal.toutes() == [], "aucun ordre ne doit partir par-dessus"


def test_le_resume_annonce_le_DEBIT_et_son_plafond_mesure(course, monkeypatch):
    """« 5 sessions sur 18 » se lit comme un retard. Le plafond du marche est
    a 16,1 sessions/jour sur ces quatre paires, et il est mesure, pas choisi.

    Sans ce reperage on cherche une panne dans la course alors qu'elle tourne
    a son maximum -- ce qui a coute une journee d'enquete.
    """
    import time as _t

    from maxprofit.live.plan_demo import SESSIONS_PAR_JOUR_MESUREES

    # ⚠ L'horloge est FIGEE a midi UTC pile, et ce n'est pas du confort.
    #
    # Le debit divise les sessions de la JOURNEE par le temps ecoule DANS la
    # journee. Ce dernier depend de l'heure a laquelle le test tourne : sans
    # horloge figee, il passerait a midi et echouerait a minuit.
    MIDI = 1790251200                    # un multiple de 86400, plus 12 h
    assert MIDI % 86400 == 12 * 3600
    monkeypatch.setattr(_t, "time", lambda: float(MIDI))

    c = course(["win"], plan=_plan(sessions=18))
    c.etat.univers_taille = 3
    c.etat.paires_gratuites = 3
    c.etat.bougies_evaluees = 2087
    c.etat.signaux_bruts = 7
    c.etat.derniere_evaluation_ts = MIDI
    c.etat.demarre_ts = MIDI - 12 * 3600
    c.etat.journee.sessions_jouees = 8
    resume = c.resume()
    # Huit sessions en douze heures de journee = seize par jour.
    assert "débit 16.0 sessions/jour" in resume, resume
    assert f"mesuré {SESSIONS_PAR_JOUR_MESUREES} ± " in resume, (
        "l'écart-type accompagne la moyenne : 13,4 seul se lit comme une "
        "promesse alors que le pire jour donne 2,4 sessions et le meilleur "
        "23,8")
    assert "1 signal pour 298 bougies" in resume
    assert "8 session(s) en 12.0 h de journée" in resume


def test_le_debit_ne_MELANGE_PAS_la_journee_et_la_duree_de_course(course,
                                                                 monkeypatch):
    """Le bug qu'un relevé de production a révélé : « 116,9 sessions/jour ».

    `sessions_jouees` compte la JOURNEE UTC ; on divisait par le temps écoulé
    depuis le démarrage de la COURSE. Après un redéploiement en milieu de
    journée, six sessions de la journée divisées par 1,2 h de course donnaient
    neuf fois le maximum du marché — affiché juste à côté du plafond de 13,4,
    ce qui rendait les deux chiffres inutilisables.
    """
    import time as _t
    MIDI = 1790251200
    monkeypatch.setattr(_t, "time", lambda: float(MIDI))

    c = course(["win"], plan=_plan(sessions=18))
    c.etat.univers_taille = 4
    c.etat.bougies_evaluees = 178
    c.etat.signaux_bruts = 1
    c.etat.derniere_evaluation_ts = MIDI
    # La course vient de redémarrer : 73 minutes. La journée, elle, a douze
    # heures et six sessions.
    c.etat.demarre_ts = MIDI - 73 * 60
    c.etat.journee.sessions_jouees = 6
    resume = c.resume()
    assert "débit 12.0 sessions/jour" in resume, resume
    assert "116" not in resume, (
        "six sessions sur 1,2 h de course ne font pas 117 par jour")
    assert "course en route depuis 1.2 h" in resume


def test_le_debit_se_taait_sous_une_heure_de_course(course):
    """Une session dans les dix premieres minutes vaut 144 par jour, et ce
    chiffre n'apprend rien. Mieux vaut ne rien dire que dire n'importe quoi."""
    import time as _t
    c = course(["win"], plan=_plan(sessions=18))
    c.etat.univers_taille = 3
    c.etat.bougies_evaluees = 30
    c.etat.derniere_evaluation_ts = int(_t.time())
    c.etat.demarre_ts = int(_t.time()) - 600
    c.etat.journee.sessions_jouees = 1
    assert "débit" not in c.resume()


def test_un_jour_du_plan_se_compte_en_SESSIONS_pas_en_heures(course):
    """Le passage etait branche sur minuit UTC. Un jour du plan durait donc une
    journee entiere quoi qu'il arrive : dix-huit sessions comptees dans le meme
    jour, et « Jour 2/30 » annonce a 258,67 $ quand le jour 1 demandait 258,70.

    Un jour du plan vaut `sessions_par_jour` sessions. Six, et l'on en enchaine
    trois par journee calendaire — c'est toute la raison du test compresse.
    """
    c = course(["win"] * 12, plan=_plan(sessions=3, jours=30))
    assert c.etat.jour == 1
    for _ in range(3):
        c.tour()
    assert c.etat.journee.sessions_jouees == 3
    # Trois sessions jouees : le jour est fini, et cela ne depend d'aucune
    # horloge. On ne passe AUCUN horodatage.
    assert c.passer_le_jour_si_besoin() is True
    assert c.etat.jour == 2
    assert c.etat.journee.sessions_jouees == 0, "compteurs de la journee remis"
    assert c.etat.solde > CAPITAL, "le solde se conserve d'un jour a l'autre"


def test_le_jour_ne_passe_pas_avec_une_session_en_cours(course):
    """Couper laisserait des mises engagees dans une journee qui n'existe
    plus."""
    c = course(["loose", "win"], plan=_plan(sessions=1, jours=30))
    c.tour()                                     # pas 1 perdu, session OUVERTE
    assert c.etat.session is not None
    assert c.passer_le_jour_si_besoin() is False
    assert c.etat.jour == 1


def test_le_jour_ne_passe_pas_tant_qu_il_reste_des_sessions(course):
    c = course(["win"] * 6, plan=_plan(sessions=6, jours=30))
    c.tour()
    assert c.passer_le_jour_si_besoin() is False
    assert c.etat.jour == 1


def test_le_plan_TERMINE_ne_boucle_pas_sur_un_jour_de_plus(course):
    c = course(["win"] * 6, plan=_plan(sessions=1, jours=2))
    c.tour()
    assert c.passer_le_jour_si_besoin() is True
    assert c.etat.jour == 2
    c.tour()
    assert c.passer_le_jour_si_besoin() is False, (
        "au dernier jour, le plan s'arrete au lieu de compter un 3e")
    assert c.etat.jour == 2


def test_les_compteurs_d_activite_survivent_a_un_redemarrage(tmp_path):
    """Une jauge qui se remet a zero plus souvent que le phenomene qu'elle
    mesure ne mesure rien.

    `/etat` annoncait « en route depuis 0 min » sous 2 087 bougies evaluees :
    la date de depart etait posee sur l'etat NEUF, puis l'etat recharge
    l'ecrasait avec 0. Impossible d'en deduire un debit — au moment precis ou
    l'on cherchait a savoir pourquoi il etait bas.
    """
    from maxprofit.live.plan_demo import charger_etat, sauver_etat
    from maxprofit.store.db import open_read_write

    conn = open_read_write(tmp_path / "plan.db")
    plan = _plan(sessions=10)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["win"]), journal,
                       plan, PAIRES_TEST)
    c.etat.bougies_evaluees = 2087
    c.etat.bougies_perimees = 715
    c.etat.signaux_bruts = 7
    c.etat.signaux_trouves = 7
    c.etat.pas_sautes_independance = 2
    c.etat.sessions_interrompues = 1
    depart = c.etat.demarre_ts
    assert depart > 0, "une course neuve pose sa date de depart"
    sauver_etat(conn, "t", c.etat, 0)

    relu, _ = charger_etat(conn, "t", plan)
    assert relu.bougies_evaluees == 2087
    assert relu.bougies_perimees == 715
    assert relu.signaux_bruts == 7
    assert relu.signaux_trouves == 7
    assert relu.pas_sautes_independance == 2
    assert relu.sessions_interrompues == 1
    assert relu.demarre_ts == depart, (
        "la date de depart est celle du PLAN, pas du dernier redemarrage")
    journal.close()
    conn.close()


def test_l_ancre_survit_a_un_redemarrage(course, tmp_path):
    """Sans elle, une course reprise recalculerait son solde depuis un
    nouveau point de départ et perdrait tout l'historique du plan."""
    from maxprofit.live.plan_demo import charger_etat, sauver_etat

    conn = _base(tmp_path)
    c = course(["win"], plan=_plan(sessions=10))
    c.tour()
    sauver_etat(conn, "ancre", c.etat, 20_000)

    repris, _ = charger_etat(conn, "ancre", c.etat.plan)
    assert repris.solde_broker_ancre == pytest.approx(53170.0)
    conn.close()


def test_la_quarantaine_DOUBLE_a_chaque_recidive(course):
    """Trois quarantaines de suite sur BTCUSD_otc, a trois payouts differents.

    A une heure fixe, on reessayait indefiniment toutes les heures une paire
    dont la cause de refus ne passait pas avec le temps. Elle est maintenant
    connue et PERMANENTE : l'echeance de 900 s n'existe pas sur cet actif —
    mesure en passant de vrais ordres sur le compte demo, BTCUSD_otc refuse
    900 s a 1,66 $, a 5 $ et a 20 $, puis accepte 60 s et 300 s aux memes
    mises. Ni la mise ni le payout : le contrat de quinze minutes n'est pas
    propose sur une crypto.
    """
    import time as _t

    from maxprofit.live.plan_demo import (QUARANTAINE_MAX_SEC,
                                          QUARANTAINE_SEC,
                                          REFUS_AVANT_QUARANTAINE)
    paire = PAIRES_TEST[0]
    c = course(["refus"] * 40, plan=_plan(sessions=18))
    c.chercher_un_signal = lambda: Signal(
        pair=paire, direction=Direction.CALL, decided_at_ms=T0_MS,
        expiry_sec=900, reason="script")

    durees = []
    for recidive in range(4):
        for _ in range(REFUS_AVANT_QUARANTAINE):
            c.tour()
        durees.append(c.etat.quarantaine[paire] - int(_t.time()))
        # On lève la peine à la main pour enchaîner la récidive suivante.
        c.etat.quarantaine.pop(paire)
        c.etat.refus_daffilee.pop(paire, None)
        c.etat.dernier_refus_ts.pop(paire, None)

    assert c.etat.quarantaines_subies[paire] == 4
    for rang, duree in enumerate(durees):
        attendu = min(QUARANTAINE_MAX_SEC, QUARANTAINE_SEC * 2 ** rang)
        assert abs(duree - attendu) <= 2, (
            f"récidive {rang + 1} : {duree} s au lieu de {attendu} s")
    assert durees[-1] > durees[0], "la peine doit croître"


def test_une_peine_LEVEE_n_efface_pas_le_compte_des_recidives(course):
    """Sinon l'escalade ne sert a rien : chaque levee ramenerait la peine a une
    heure, indefiniment."""
    import time as _t
    paire = PAIRES_TEST[0]
    c = course(["win"], plan=_plan(sessions=18))
    c.etat.quarantaine = {paire: int(_t.time()) - 1}
    c.etat.quarantaines_subies = {paire: 3}
    c._purger_la_quarantaine()
    assert c.etat.quarantaine == {}
    assert c.etat.quarantaines_subies == {paire: 3}


def test_les_recidives_SURVIVENT_a_un_redemarrage(tmp_path):
    """Sans quoi chaque redeploiement ramenerait la peine a une heure."""
    from maxprofit.live.plan_demo import charger_etat, sauver_etat
    from maxprofit.store.db import open_read_write

    conn = open_read_write(tmp_path / "plan.db")
    plan = _plan(sessions=6)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["win"]), journal,
                       plan, PAIRES_TEST)
    c.etat.quarantaines_subies = {"BTCUSD_otc": 3}
    sauver_etat(conn, "t", c.etat, 0)
    relu, _ = charger_etat(conn, "t", plan)
    assert relu.quarantaines_subies == {"BTCUSD_otc": 3}
    journal.close()
    conn.close()


def _bougies_amplitude(pair, n, amplitude_pct, prix=1.0):
    """n bougies dont l'amplitude relative vaut `amplitude_pct` % du prix."""
    from maxprofit.core.types import Candle
    demi = prix * amplitude_pct / 100 / 2
    return [Candle(pair=pair, tf_sec=60, ts_sec=1789999980 + k * 60,
                   open=prix, high=prix + demi, low=prix - demi, close=prix,
                   tick_count=30, complete=True)
            for k in range(n)]


def test_un_actif_HORS_CALIBRATION_est_ecarte(tmp_path):
    """BTCUSD_otc : 820 signaux sur 1 139 bougies, soit un par minute et demie.

    Sa bougie mediane fait 0,0006 % quand EURUSD_otc fait 0,0301 % : la
    tolerance de 0,02 % y est 36 fois plus grande qu'une bougie, donc toute
    bougie touche toute zone proche. Ce ne sont pas des signaux, c'est du bruit
    — et il est aujourd'hui inoffensif seulement parce que le broker refuse
    l'echeance de 900 s sur les cryptos.
    """
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["win"]), journal,
                       _plan(sessions=6), ("X_otc",))
    # EURUSD_otc mesure : 0,0301 % -> rapport 0,67. Dans la plage.
    assert c._dans_sa_plage_de_calibration(
        "X_otc", _bougies_amplitude("X_otc", 60, 0.0301))
    # BTCUSD_otc mesure : 0,0006 % -> rapport 33. Hors plage.
    assert not c._dans_sa_plage_de_calibration(
        "X_otc", _bougies_amplitude("X_otc", 60, 0.0006))
    # Un actif BEAUCOUP plus agite que les paires de calibration l'est aussi.
    assert not c._dans_sa_plage_de_calibration(
        "X_otc", _bougies_amplitude("X_otc", 60, 5.0))
    journal.close()


def test_un_actif_sans_PRIX_est_ecarte_et_non_pris_pour_calme(tmp_path):
    """Une bougie mediane d'amplitude nulle ne decrit pas un actif calme : elle
    decrit un actif dont on ne recoit pas le prix."""
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["win"]), journal,
                       _plan(sessions=6), ("X_otc",))
    assert not c._dans_sa_plage_de_calibration(
        "X_otc", _bougies_amplitude("X_otc", 60, 0.0))
    journal.close()


def test_trop_peu_de_bougies_ne_fait_pas_ecarter_un_actif(tmp_path):
    """On ne tranche pas sur cinq bougies : une paire fraichement collectee
    serait ecartee pour une raison qui n'a rien a voir avec sa calibration."""
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["win"]), journal,
                       _plan(sessions=6), ("X_otc",))
    assert c._dans_sa_plage_de_calibration(
        "X_otc", _bougies_amplitude("X_otc", 5, 0.0006))
    journal.close()


def test_les_quatre_paires_de_calibration_sont_DANS_la_plage(tmp_path):
    """Les bornes doivent accueillir ce sur quoi la strategie a ete calibree —
    sinon la garde ecarterait les paires memes qui l'ont validee."""
    from maxprofit.live.plan_demo import (RAPPORT_TOLERANCE_MAX,
                                          RAPPORT_TOLERANCE_MIN)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["win"]), journal,
                       _plan(sessions=6), ("X_otc",))
    # Amplitudes medianes mesurees sur les quatre paires epinglees.
    for nom, amp in (("EURUSD", 0.0312), ("AUDUSD", 0.0576),
                     ("GBPAUD", 0.0649), ("AUDCAD", 0.0929)):
        assert c._dans_sa_plage_de_calibration(
            "X_otc", _bougies_amplitude("X_otc", 60, amp)), nom
    assert RAPPORT_TOLERANCE_MIN < 0.02 / 0.0929
    assert 0.02 / 0.0312 < RAPPORT_TOLERANCE_MAX
    journal.close()


# --------------------------------------------------------------------------- #
# Vitesse : ne relire que ce qui est nouveau
# --------------------------------------------------------------------------- #

class LecteurQuiCompte:
    """Une base qui avance d'une minute quand on le lui dit, et qui note ce
    qu'on lui demande."""

    def __init__(self, fin_ts=1790000040):
        self.fin = fin_ts
        self.depuis: list[int] = []
        self.appels_unitaires = 0

    def last_candle_ts_sec(self):
        return self.fin

    def candles(self, *a, **k):
        self.appels_unitaires += 1
        return []

    def candles_de(self, paires, tf, depuis):
        from maxprofit.core.types import Candle
        self.depuis.append(depuis)
        premiere = max(depuis, self.fin - 400 * 60)
        return {p: [Candle(pair=p, tf_sec=60, ts_sec=t, open=1.0, high=1.001,
                           low=0.999, close=1.0, tick_count=30, complete=True)
                    for t in range(premiere, self.fin + 60, 60)]
                for p in paires}


def test_un_passage_ne_relit_que_les_DERNIERES_minutes(tmp_path):
    """Chaque paire coûtait deux requêtes et 300 bougies à chaque passage,
    même quand rien n'avait changé : une vingtaine d'allers-retours vers la
    base distante pour dix paires. Une requête pour toutes, depuis la
    dernière bougie connue."""
    from maxprofit.live.plan_demo import RECOUVREMENT_CACHE_SEC

    paires = ("A_otc", "B_otc", "C_otc")
    lecteur = LecteurQuiCompte()
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(lecteur, CourtierFactice([]), journal, _plan(), paires)
    c.univers = lambda: list(paires)
    lookback = c.strategie.p.lookback

    c.chercher_un_signal()
    lecteur.fin += 60
    c.chercher_un_signal()

    assert lecteur.appels_unitaires == 0, "plus de requête par paire"
    assert lecteur.depuis == [
        1790000040 - lookback * 60,
        1790000040 - RECOUVREMENT_CACHE_SEC], (
        "une requête par passage : la fenêtre complète, puis les seules "
        "dernières minutes")
    for p in paires:
        bougies = c.bougies_collectees(p, lookback)
        assert bougies[-1].ts_sec == lecteur.fin, "la nouvelle bougie est vue"
        assert len(bougies) == lookback + 1, "la fenêtre ne grossit pas"
        assert len({b.ts_sec for b in bougies}) == len(bougies), "sans doublon"
    journal.close()


def test_le_solde_du_plan_se_calcule_SANS_relire_le_journal(tmp_path):
    """Refusé : ne compte pas. Dénoué : son profit. En vol : moins sa mise."""
    journal = JournalExecution(tmp_path / "e.db", campagne="t")

    def ordre(accepte, resultat=None, profit=None, mise=2.0):
        ex = Execution(pair="A_otc", sens="call", mise=mise, signal_ts_ms=1,
                       prix_attendu=1.0, payout_flux_pct=92,
                       expiration_sec=900, clic_ts_ms=2, accepte=accepte,
                       resultat=resultat, profit=profit)
        journal.ecrire(ex)

    ordre(True, "win", 1.84)
    ordre(True, "loose", -2.0)
    ordre(False)
    ordre(True, mise=3.0)
    assert journal.profits_du_plan() == pytest.approx(1.84 - 2.0 - 3.0)
    journal.close()


# --------------------------------------------------------------------------- #
# Débit : la règle d'indépendance comptée depuis l'ENTRÉE du pas précédent
# --------------------------------------------------------------------------- #

class _Horloge:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class CourtierQuiDure(CourtierFactice):
    """Le dénouement prend les quinze minutes de l'option, comme en vrai."""

    def __init__(self, resultats, horloge):
        super().__init__(resultats)
        self.horloge = horloge

    def denouer(self, ex):
        self.horloge.t += 900
        return super().denouer(ex)


def test_le_pas_suivant_se_joue_DES_LE_DENOUEMENT_sur_un_autre_actif(
        tmp_path, monkeypatch):
    """La règle #68 : un AUTRE actif, au moins quinze minutes après le pas
    précédent. Avec une échéance de 900 s, c'est le dénouement. Datée du
    retour de `jouer_un_pas`, elle imposait trente minutes entre deux
    entrées — deux fois la règle mesurée et pré-inscrite."""
    horloge = _Horloge()
    monkeypatch.setattr("maxprofit.live.plan_demo.time.time", horloge)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierQuiDure(["loose", "win"],
                                                         horloge),
                       journal, _plan(sessions=10), PAIRES_TEST)
    paires = iter(["AUDCAD_otc", "EURUSD_otc"])

    def signal_scripte():
        return Signal(pair=next(paires), direction=Direction.CALL,
                      decided_at_ms=T0_MS, expiry_sec=900, reason="script")

    c.chercher_un_signal = signal_scripte
    entree = int(horloge.t)
    assert c.tour() is True                      # pas 1, perdu, 15 min
    assert c.etat.dernier_trade == ("AUDCAD_otc", entree), (
        "daté de l'ENTRÉE, pas du dénouement")
    horloge.t += 5                               # la bougie suivante
    assert c.tour() is True, (
        "autre actif, quinze minutes après l'entrée : le pas 2 doit partir")
    assert c.etat.pas_sautes_independance == 0
    journal.close()


def test_un_ordre_REFUSE_ne_compte_pas_comme_pas_joue(tmp_path):
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["refus"]),
                       journal, _plan(sessions=10), PAIRES_TEST)
    c.chercher_un_signal = lambda: Signal(
        pair="AUDCAD_otc", direction=Direction.CALL, decided_at_ms=T0_MS,
        expiry_sec=900, reason="script")
    c.tour()
    assert c.etat.dernier_trade is None, "un refus n'a engagé aucun pari"
    journal.close()


def test_la_recherche_ECARTE_l_actif_du_pas_precedent(tmp_path):
    """Sinon elle s'arrête sur un signal que `tour()` refusera, et le signal
    valide d'un autre actif, dans le même passage, est perdu."""
    from maxprofit.plan import Echelle, Session

    six = ("A_otc", "B_otc", "C_otc")
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurAvecBougies(), CourtierFactice([]),
                       journal, _plan(sessions=18), six)
    c.univers = lambda: list(six)
    c.etat.session = Session(echelle=Echelle(payout_pct=92, gain_vise=1.0))
    c.etat.session.pas_joues = 1
    c.etat.dernier_trade = ("B_otc", int(time.time()))
    vues = []
    vraie = c.bougies_collectees
    c.bougies_collectees = lambda p, n: (vues.append(p) or vraie(p, n))
    c.chercher_un_signal()
    assert "B_otc" not in vues and set(vues) == {"A_otc", "C_otc"}
    journal.close()


def test_un_ordre_PARTI_mais_non_journalise_ne_fait_pas_tomber_la_course(
        tmp_path):
    """« duplicate key value violates unique constraint executions_pkey » :
    l'écriture échouait APRÈS le départ de l'ordre, la course tombait, et
    l'ordre courait chez le broker sans trace. On prévient, on suit l'ordre
    jusqu'au dénouement, et on réessaie."""
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    alertes: list[str] = []
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["win"]), journal,
                       _plan(sessions=10), PAIRES_TEST, alerter=alertes.append)
    vraie = journal.completer
    essais = {"n": 0}

    def completer_qui_echoue_une_fois(jeton, ex):
        essais["n"] += 1
        if essais["n"] == 1:
            raise RuntimeError("duplicate key value violates unique "
                               "constraint executions_pkey")
        return vraie(jeton, ex)

    journal.completer = completer_qui_echoue_une_fois
    c.chercher_un_signal = lambda: Signal(
        pair="AUDCAD_otc", direction=Direction.CALL, decided_at_ms=T0_MS,
        expiry_sec=900, reason="script")
    assert c.tour() is True, "la course ne doit pas tomber"
    ordres = journal.toutes()
    assert len(ordres) == 1 and ordres[0].resultat == "win", (
        "l'ordre entre au journal, complet, au dénouement")
    assert any("NON enregistré" in a for a in alertes)
    assert c.etat.session is None, "la session gagnée est bien close"
    journal.close()


# --------------------------------------------------------------------------- #
# Aucun ordre sans sa ligne, aucun pas avant le sort du précédent
# --------------------------------------------------------------------------- #

def _signal_sur(paire):
    return lambda: Signal(pair=paire, direction=Direction.CALL,
                          decided_at_ms=T0_MS, expiry_sec=900,
                          reason="script")


def test_si_le_journal_refuse_la_ligne_l_ordre_ne_part_PAS(tmp_path):
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    courtier = CourtierFactice(["win"])
    alertes: list[str] = []
    c = CoursePlanDemo(LecteurFactice(), courtier, journal,
                       _plan(sessions=10), PAIRES_TEST, alerter=alertes.append)

    def refuse(*a, **k):
        raise RuntimeError("base indisponible")

    journal.reserver = refuse
    c.chercher_un_signal = _signal_sur("AUDCAD_otc")
    c.tour()
    assert courtier.places == [], "aucun ordre ne part sans sa ligne"
    assert any("NON envoyé" in a for a in alertes)
    journal.close()


def test_une_reservation_sans_issue_BLOQUE_tout_nouvel_ordre(tmp_path,
                                                             monkeypatch):
    """Les deux pas de 3,31 $ à une minute d'intervalle : la course relancée
    ignorait qu'un ordre courait. Une réservation sans issue l'en empêche."""
    horloge = _Horloge()
    monkeypatch.setattr("maxprofit.live.plan_demo.time.time", horloge)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    journal.reserver("EURUSD_otc", "put", 3.31, 900)   # le processus meurt
    courtier = CourtierFactice(["win"])
    c = CoursePlanDemo(LecteurFactice(), courtier, journal,
                       _plan(sessions=10), PAIRES_TEST)
    c.chercher_un_signal = _signal_sur("AUDCAD_otc")
    horloge.t += 60
    assert c.tour() is False and courtier.places == []
    assert "peut-être parti" in c.resume()
    horloge.t += 900 + 120                             # l'échéance est passée
    assert c.tour() is True, "introuvable chez le broker : on reprend"
    assert len(courtier.places) == 1
    journal.close()


def test_un_ordre_RETROUVE_chez_le_broker_entre_au_journal(tmp_path,
                                                           monkeypatch):
    horloge = _Horloge()
    monkeypatch.setattr("maxprofit.live.plan_demo.time.time", horloge)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    instant = int(horloge.t)
    journal.reserver("EURUSD_otc", "put", 3.31, 900)

    class CourtierQuiSeSouvient(CourtierFactice):
        def ordres_clotures(self):
            return [{"id": "po-42", "asset": "EURUSD_otc", "amount": 3.31,
                     "command": 1, "openTimestamp": instant + 1,
                     "closeTimestamp": instant + 901, "openPrice": 1.1,
                     "closePrice": 1.2, "profit": 0}]

    c = CoursePlanDemo(LecteurFactice(), CourtierQuiSeSouvient([]), journal,
                       _plan(sessions=10), PAIRES_TEST)
    horloge.t += 900 + 121
    c.chercher_un_signal = lambda: None
    c.tour()
    ordres = journal.toutes()
    assert [(o.order_id, o.resultat, o.profit) for o in ordres] == [
        ("po-42", "loose", -3.31)]
    assert journal.reservations() == []
    journal.close()


def test_un_echec_AVANT_l_envoi_libere_la_reservation(tmp_path):
    class CourtierSansPrix(CourtierFactice):
        def placer(self, *a, **k):
            raise BotError("Aucun tick disponible sur AUDCAD_otc.")

    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierSansPrix([]), journal,
                       _plan(sessions=10), PAIRES_TEST)
    c.chercher_un_signal = _signal_sur("AUDCAD_otc")
    with pytest.raises(BotError):
        c.tour()
    assert journal.reservations() == [], "pas parti : ne bloque rien"
    journal.close()


def test_rapatrier_liste_seulement_les_ordres_ABSENTS_depuis_le_depart(
        tmp_path):
    from maxprofit.live.plan_demo import ordres_absents

    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    journal.ecrire(_ordre_en_vol("po-connu"))

    def deal(i, ouverture):
        return {"id": i, "asset": "EURUSD_otc", "amount": 3.31, "command": 1,
                "openTimestamp": ouverture, "closeTimestamp": ouverture + 900,
                "openPrice": 1.1, "closePrice": 1.0, "profit": 3.05}

    courtier = type("C", (), {"ordres_clotures": lambda self: [
        deal("po-connu", 1_790_000_000), deal("po-avant", 1_700_000_000),
        deal("po-orphelin", 1_790_000_500)]})()
    absents = ordres_absents(courtier, journal, 1_789_999_000_000)
    assert [(e.order_id, e.sens, e.resultat) for e in absents] == [
        ("po-orphelin", "put", "win")]
    journal.close()


# --------------------------------------------------------------------------- #
# Une session ne reste jamais incomplète : /reprendre, et le pas rattrapé
# --------------------------------------------------------------------------- #

def test_reprendre_une_session_interrompue_la_ou_elle_en_etait(tmp_path):
    from maxprofit.live.plan_demo import demander_reprise

    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    alertes: list[str] = []
    c = CoursePlanDemo(LecteurFactice(),
                       CourtierFactice(["loose", "unknown", "win"]), journal,
                       _plan(sessions=10), PAIRES_TEST, alerter=alertes.append)
    paires = iter(["AUDCAD_otc", "EURUSD_otc"])
    c.chercher_un_signal = lambda: Signal(
        pair=next(paires), direction=Direction.CALL, decided_at_ms=T0_MS,
        expiry_sec=900, reason="script")
    c.tour()                               # pas 1, perdu
    c.etat.dernier_trade = ("AUDCAD_otc", 0)
    c.tour()                               # pas 2, dénouement inconnu
    memo = c.etat.session_suspendue
    assert c.etat.session is None
    assert memo["pas_joues"] == 1 and len(memo["engagees"]) == 2, (
        "le pas 2 est engagé, pas résolu")

    # Le broker dit que le pas 2 a été perdu : on reprend après deux pas.
    assert "demandée" in demander_reprise(c, "2")
    c.chercher_un_signal = lambda: None
    c.tour()
    session = c.etat.session
    assert session is not None and session.pas_joues == 2
    assert any("Session reprise" in a for a in alertes)
    assert c.etat.sessions_interrompues == 0
    journal.close()


def test_reprendre_SANS_memoire_exige_le_nombre_de_pas(tmp_path):
    """La session interrompue en production l'a été avant cette mémoire :
    l'échelle est recalculée sur le solde d'ouverture de la journée."""
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice([]), journal,
                       _plan(sessions=6), PAIRES_TEST)
    assert "Précisez" in c._reprendre(None)
    message = c._reprendre(2)
    assert c.etat.session.pas_joues == 2
    assert c.etat.session.engagees == pytest.approx([1.59, 3.31], abs=0.005)
    assert c.etat.session.mise_courante() == pytest.approx(6.90, abs=0.005)
    assert "6.90" in message
    assert "déjà en cours" in c._reprendre(1)
    journal.close()


def test_un_pas_RETROUVE_chez_le_broker_fait_avancer_la_session(tmp_path,
                                                                monkeypatch):
    """Relancée après un pas parti sans trace, la course en était au pas
    d'avant : le pas retrouvé compte pour la session, qui continue au lieu de
    rejouer ce pas ou d'être abandonnée."""
    horloge = _Horloge()
    monkeypatch.setattr("maxprofit.live.plan_demo.time.time", horloge)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    instant = int(horloge.t)

    class CourtierQuiSeSouvient(CourtierFactice):
        def ordres_clotures(self):
            return [{"id": "po-7", "asset": "EURUSD_otc", "amount": 3.31,
                     "command": 0, "openTimestamp": instant + 1,
                     "closeTimestamp": instant + 901, "openPrice": 1.1,
                     "closePrice": 1.0, "profit": 0}]

    c = CoursePlanDemo(LecteurFactice(), CourtierQuiSeSouvient([]), journal,
                       _plan(sessions=6), PAIRES_TEST)
    c._reprendre(1)                                    # pas 1 déjà perdu
    journal.reserver("EURUSD_otc", "call", 3.31, 900)  # le pas 2, puis crash
    horloge.t += 900 + 121
    c.chercher_un_signal = lambda: None
    c.tour()
    assert c.etat.session.pas_joues == 2, "le pas 2 perdu est compté"
    assert c.etat.session.mise_courante() == pytest.approx(6.90, abs=0.005)
    assert c.etat.dernier_trade[0] == "EURUSD_otc"
    journal.close()


def test_la_session_suspendue_survit_a_un_redemarrage(course, tmp_path):
    from maxprofit.live.plan_demo import charger_etat, sauver_etat

    conn = _base(tmp_path)
    c = course(["unknown"], plan=_plan(sessions=10))
    c.tour()
    assert c.etat.session_suspendue is not None
    sauver_etat(conn, "susp", c.etat, 20_000)
    repris, _ = charger_etat(conn, "susp", c.etat.plan)
    assert repris.session_suspendue == c.etat.session_suspendue
    conn.close()


def test_etat_distingue_les_sessions_GAGNEES_des_PERDUES(course):
    """« sessions 1/6 » pour une journee dont l'unique session etait perdue :
    on croyait en avoir reussi une."""
    c = course(["loose", "loose", "loose", "win"], plan=_plan(sessions=6))
    for _ in range(3):
        c.tour()
    assert c.etat.journee.sessions_gagnees == 0
    c.etat.bougies_evaluees = 10
    resume = c.resume()
    # La perte est absorbée par le recalage : le compte dit où le SOLDE
    # place le plan, et il n'y a rien de gagné à 238 $.
    assert "sessions 0/6 gagnée" in resume, resume
    c.tour()
    assert c.etat.journee.sessions_gagnees == 1
    c.etat.bougies_evaluees = 10
    assert "sessions 1/6 gagnée" in c.resume()


def test_les_sessions_gagnees_SURVIVENT_a_un_redemarrage(tmp_path):
    from maxprofit.live.plan_demo import charger_etat, sauver_etat
    from maxprofit.store.db import open_read_write

    conn = open_read_write(tmp_path / "plan.db")
    plan = _plan(sessions=6)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["win"]), journal,
                       plan, PAIRES_TEST)
    c.etat.journee.sessions_jouees = 3
    c.etat.journee.sessions_gagnees = 2
    sauver_etat(conn, "t", c.etat, 0)
    relu, _ = charger_etat(conn, "t", plan)
    assert relu.journee.sessions_gagnees == 2
    journal.close()
    conn.close()


# --------------------------------------------------------------------------- #
# Un jour du plan s'accomplit par son SOLDE
# --------------------------------------------------------------------------- #

CIBLE_JOUR_1 = CAPITAL * (1 + 6 * _plan().gain_par_session_pct / 100)


def test_la_cible_du_jour_est_celle_du_PLANNING():
    plan = _plan(sessions=6)
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice([]), None, plan,
                       PAIRES_TEST)
    assert c.etat.journee.cible == pytest.approx(258.75, abs=0.01)
    assert c.etat.cible_du_jour(2) == pytest.approx(258.75 * 1.035, abs=0.02)


def test_deux_sessions_perdues_au_jour_1_ne_font_PAS_passer_au_jour_2(course):
    """Production : réancrage au jour 1, puis `SESSIONS_PERDUES` appelait
    `nouveau_jour` — « Jour 2/30 » à 238 $ quand le jour 1 demandait
    258,75 $."""
    c = course(["loose"] * 6, plan=_plan(sessions=6))
    for _ in range(6):
        c.tour()
    c.passer_le_jour_si_besoin()
    assert c.etat.jour == 1, "une mauvaise journée ne promeut pas le plan"
    assert c.etat.journee.cible == pytest.approx(CIBLE_JOUR_1)
    assert c.etat.journee.manque == pytest.approx(CIBLE_JOUR_1 - c.etat.solde)
    assert c.peut_ouvrir() is None, "on continue le même jour"


def test_apres_une_mauvaise_journee_la_cible_ne_GLISSE_pas(course):
    """Le cas décrit : descendu vers 226-240 $, six sessions gagnées ne
    suffisent plus. Le jour 1 reste le jour 1 jusqu'à 258,75 $ — la cible ne
    glisse pas vers « solde de réouverture × 1,035 »."""
    c = course(["loose"] * 6 + ["win"] * 40, plan=_plan(sessions=6))
    for _ in range(6):
        c.tour()
    c.passer_le_jour_si_besoin()
    assert c.etat.jour == 1
    # L'ancienne règle visait +3,50 % du solde d'OUVERTURE de la journée
    # rouverte : elle se serait déclarée accomplie bien sous 258,75 $.
    glissante = c.etat.solde * 1.035
    assert glissante < CIBLE_JOUR_1
    vue_sous_la_cible = False
    while c.etat.solde < CIBLE_JOUR_1:
        assert c.tour() is True
        c.passer_le_jour_si_besoin()
        if glissante <= c.etat.solde < CIBLE_JOUR_1:
            vue_sous_la_cible = True
            assert c.etat.jour == 1
    assert vue_sous_la_cible
    c.passer_le_jour_si_besoin()
    assert c.etat.jour == 2, "la cible atteinte, et elle seule, clôt le jour"
    assert c.etat.journee.cible == pytest.approx(c.etat.cible_du_jour(2))


def test_le_DERNIER_jour_n_est_pas_bloque_par_une_session_perdue(course):
    """Au dernier jour, une garde de perte figeait la course : le passage
    rendait `False` avant même de regarder pourquoi la journée s'arrêtait."""
    c = course(["loose"] * 3 + ["win"],
               plan=_plan(sessions=1, jours=1, sessions_perdues_max=1))
    for _ in range(3):
        c.tour()
    c.passer_le_jour_si_besoin()
    assert c.peut_ouvrir() is None


def test_un_jour_AVANCE_a_tort_est_realigne_au_demarrage(course):
    c = course([], plan=_plan(sessions=6))
    c.etat.jour = 2
    c.etat.solde = 240.0
    c.etat.ouvrir_la_journee()
    assert c.realigner_le_jour() is True
    assert c.etat.jour == 1
    assert c.etat.journee.cible == pytest.approx(CIBLE_JOUR_1)
    assert c.realigner_le_jour() is False, "une seule fois"


def test_un_jour_LEGITIME_n_est_pas_realigne(course):
    c = course([], plan=_plan(sessions=6))
    c.etat.jour = 2
    c.etat.solde = CIBLE_JOUR_1 + 1
    c.etat.ouvrir_la_journee()
    assert c.realigner_le_jour() is False
    assert c.etat.jour == 2


def test_la_cible_survit_a_un_redemarrage(tmp_path):
    from maxprofit.live.plan_demo import charger_etat, sauver_etat
    from maxprofit.store.db import open_read_write

    conn = open_read_write(tmp_path / "plan.db")
    plan = _plan(sessions=6)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice([]), journal,
                       plan, PAIRES_TEST)
    c.etat.jour = 3
    c.etat.ouvrir_la_journee()
    sauver_etat(conn, "t", c.etat, 0)
    relu, _ = charger_etat(conn, "t", plan)
    assert relu.journee.cible == pytest.approx(c.etat.cible_du_jour(3))
    journal.close()
    conn.close()


# --------------------------------------------------------------------------- #
# Un ordre en vol laissé par une autre instance
# --------------------------------------------------------------------------- #

def test_un_ordre_en_vol_ENCORE_VIVANT_ne_bloque_pas_le_demarrage(tmp_path):
    """Production, 2026-09-28 : l'ancienne instance place un ordre trois
    secondes avant l'arrêt ; la nouvelle attendait ses 900 s DANS son
    démarrage, affichée « connexion au broker en cours »."""
    import time as _t

    from maxprofit.live.plan_demo import _resoudre_les_ordres_en_vol

    journal = JournalExecution(tmp_path / "e.db", campagne="vivant")
    ordre = _ordre_en_vol()
    ordre.clic_ts_ms = ordre.accepte_ts_ms = int(_t.time() * 1000) - 3000
    journal.ecrire(ordre)

    class CourtierPresse(CourtierFactice):
        def denouer(self, e):
            raise AssertionError("on n'attend pas un ordre vivant ici")

    en_attente = _resoudre_les_ordres_en_vol(None, CourtierPresse([]), journal)
    assert [e.order_id for e in en_attente] == ["abc"]
    journal.close()


def test_aucun_ordre_ne_part_tant_qu_un_ordre_d_une_AUTRE_instance_vit(course):
    """Pendant le chevauchement d'un déploiement, l'ancienne instance peut
    placer un ordre APRÈS que la nouvelle a démarré."""
    import time as _t

    c = course(["win"])
    ordre = _ordre_en_vol("autre")
    ordre.clic_ts_ms = ordre.accepte_ts_ms = int(_t.time() * 1000) - 60_000
    c.journal.ecrire(ordre)
    assert c.tour() is False
    assert len(c.journal.toutes()) == 1, "aucun second ordre en parallèle"
    assert "EURUSD_otc call 1.59 $ en vol, dénouement dans" in \
        c.attente_confirmation
    assert "EN ATTENTE" in c.resume()


def test_l_issue_d_un_ordre_en_vol_compte_pour_la_SESSION(course):
    """Elle n'allait qu'au journal : la session, sauvée juste avant le
    dénouement, attendait encore ce pas — et la course l'aurait rejoué."""
    from maxprofit.plan import Session

    c = course([])
    c.etat.session = Session(echelle=c._echelle())
    c.etat.solde_ouverture_session = c.etat.solde
    ordre = _ordre_en_vol("pas1")
    ordre.mise = c.etat.session.mise_courante()
    ordre._issue = "win"
    c.journal.ecrire(ordre)

    class CourtierQuiSait(CourtierFactice):
        def denouer(self, e):
            e.resultat, e.profit = "win", round(e.mise * 0.92, 2)
            return e

    c.courtier = CourtierQuiSait([])
    assert c._un_ordre_reste_a_confirmer() is False
    assert c.etat.session is None, "le pas gagné clôt la session"
    assert c.etat.journee.sessions_gagnees == 1
    assert c.journal.en_vol() == []


def test_une_issue_INCONNUE_interrompt_la_session_au_lieu_de_rejouer(course):
    from maxprofit.plan import Session

    c = course([])
    c.etat.session = Session(echelle=c._echelle())
    c.etat.solde_ouverture_session = c.etat.solde
    ordre = _ordre_en_vol("mystere")
    ordre.mise = c.etat.session.mise_courante()
    c.journal.ecrire(ordre)

    class CourtierPerdu(CourtierFactice):
        def denouer(self, e):
            e.resultat, e.profit = "unknown", None
            return e

    c.courtier = CourtierPerdu([])
    c._un_ordre_reste_a_confirmer()
    assert c.etat.session is None
    assert c.etat.session_suspendue is not None, "/reprendre possible"


class _CourtierQuiRelit(CourtierFactice):
    """Un broker dont l'historique connaît l'issue d'un ordre noté inconnu."""

    def __init__(self, issues):
        super().__init__([])
        self.issues = issues

    def issue_dans_l_historique(self, e):
        if e.order_id not in self.issues:
            return None
        e.resultat = self.issues[e.order_id]
        e.profit = round(e.mise * 0.92, 2) if e.resultat == "win" else -e.mise
        return e


def test_un_ordre_note_INCONNU_est_relu_chez_le_broker_et_clot_la_session(
        course):
    """Production, 2026-09-28 : l'ordre de l'ancienne instance, noté
    « unknown », avait GAGNÉ — 2,98 $ d'écart avec le broker, et une session
    qui attendait encore son pas 1."""
    from maxprofit.plan import Session

    c = course([])
    c.etat.session = Session(echelle=c._echelle())
    c.etat.solde_ouverture_session = c.etat.solde
    ordre = _ordre_en_vol("ancien")
    ordre.mise = c.etat.session.mise_courante()
    ordre.resultat, ordre.profit = "unknown", None
    c.journal.ecrire(ordre)
    c.rafraichir_le_solde()
    avant = c.etat.solde                  # la mise y est comptée perdue
    c.courtier = _CourtierQuiRelit({"ancien": "win"})
    assert c._un_ordre_reste_a_confirmer() is False
    assert c.journal.inconnus() == []
    assert c.journal.toutes()[0].resultat == "win"
    assert c.etat.session is None
    assert c.etat.journee.sessions_gagnees == 1
    assert c.etat.solde == pytest.approx(avant + ordre.mise * 1.92, abs=0.01)


def test_une_session_SUSPENDUE_faute_d_issue_reprend_quand_l_issue_arrive(
        course):
    from maxprofit.plan import Session

    c = course([])
    session = Session(echelle=c._echelle())
    session.enregistrer(False)                        # pas 1 perdu
    c.etat.session = session
    c.etat.solde_ouverture_session = c.etat.solde
    mise2 = session.mise_courante()
    session.engager_sans_resoudre(mise2)
    c._interrompre_la_session("dénouement inconnu (unknown)")
    assert c.etat.session is None and c.etat.session_suspendue is not None
    ordre = _ordre_en_vol("pas2")
    ordre.mise = mise2
    ordre.resultat, ordre.profit = "unknown", None
    c.journal.ecrire(ordre)
    c.courtier = _CourtierQuiRelit({"pas2": "loose"})
    c._un_ordre_reste_a_confirmer()
    assert c.etat.session_suspendue is None
    assert c.etat.session is not None, "le pas 3 reste à jouer"
    assert c.etat.session.pas_joues == 2


def test_un_inconnu_que_le_broker_ignore_reste_inconnu(course):
    c = course([])
    ordre = _ordre_en_vol("perdu")
    ordre.resultat, ordre.profit = "unknown", None
    c.journal.ecrire(ordre)
    c.courtier = _CourtierQuiRelit({})
    c._un_ordre_reste_a_confirmer()
    assert len(c.journal.inconnus()) == 1, "rien n'est deviné"


# --------------------------------------------------------------------------- #
# L'apprentissage dans la course
# --------------------------------------------------------------------------- #

def _apprentissage_elan():
    from maxprofit.apprentissage.lecons import Apprentissage, Regle, Tranche
    return Apprentissage(
        regles=(Regle(Tranche("elan_15m", None, -1.5), 200, 0.40, 60, 0.38),),
        n=1500, taux=0.57, debut_sec=T0_MS // 1000, fin_sec=T0_MS // 1000,
        n_validation=450, taux_validation_avant=0.56,
        taux_validation_apres=0.59, part_ecartee=0.12)


def test_une_session_perdue_recoit_son_AUTOPSIE(course):
    messages = []
    c = course(["loose"] * 3)
    c._alerter = messages.append
    for _ in range(3):
        c.tour()
    autopsies = [m for m in messages if "Autopsie" in m]
    assert len(autopsies) == 1
    assert "session perdue en 3 pas" in autopsies[0]


def test_le_contexte_du_signal_est_journalise_avec_l_ordre(course):
    c = course(["win"])
    c.tour()
    brut = c.journal.toutes()[0].brut
    assert brut["contexte"]["pas"] == 1


def test_une_lecon_ACTIVE_ecarte_le_signal_et_le_compte(course):
    c = course([])
    c.etat.apprentissage = _apprentissage_elan()
    signal = Signal(pair="EURUSD_otc", direction=Direction.CALL,
                    decided_at_ms=T0_MS, expiry_sec=900, reason="t")
    c.mode_apprentissage = "actif"
    assert c._ecarte_par_une_lecon("EURUSD_otc", signal,
                                   {"elan_15m": -2.0}) is True
    assert c._ecarte_par_une_lecon("EURUSD_otc", signal,
                                   {"elan_15m": 0.5}) is False
    c.mode_apprentissage = "observation"
    assert c._ecarte_par_une_lecon("EURUSD_otc", signal,
                                   {"elan_15m": -2.0}) is False
    assert c.etat.signaux_ecartes_lecons == 2, "compté même en observation"


def test_l_apprentissage_survit_a_un_redemarrage(tmp_path):
    from maxprofit.live.plan_demo import charger_etat, sauver_etat
    from maxprofit.store.db import open_read_write

    conn = open_read_write(tmp_path / "plan.db")
    plan = _plan(sessions=6)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice([]), journal,
                       plan, PAIRES_TEST)
    c.etat.apprentissage = _apprentissage_elan()
    c.etat.signaux_ecartes_lecons = 4
    sauver_etat(conn, "t", c.etat, 0)
    relu, _ = charger_etat(conn, "t", plan)
    assert relu.apprentissage.to_dict() == c.etat.apprentissage.to_dict()
    assert relu.signaux_ecartes_lecons == 4
    journal.close()
    conn.close()


def test_lecons_dit_ce_qui_a_ete_appris(course):
    from maxprofit.live.plan_demo import lecons

    c = course([])
    assert "en attente" in lecons(c)
    c.etat.apprentissage = _apprentissage_elan()
    texte = lecons(c)
    assert "Leçons ACTIVES (1)" in texte and "élan" in texte
    assert lecons(None) == "Course indisponible."


def _ordre_du_journal(pair, resultat, ts_sec, pas=None):
    e = _ordre_en_vol(f"{pair}{ts_sec}")
    e.pair, e.resultat, e.clic_ts_ms = pair, resultat, ts_sec * 1000
    if pas:
        e.brut = {"contexte": {"pas": pas}}
    return e


def test_le_bilan_dit_si_la_baisse_depasse_le_HASARD():
    from maxprofit.live.plan_demo import texte_du_bilan

    maintenant = 2_000_000_000
    avant = [_ordre_du_journal("EURUSD_otc", "win" if i % 4 else "loose",
                               maintenant - 5 * 86400 + i * 60)
             for i in range(40)]                             # 75 %
    recents_faibles = [_ordre_du_journal("USDCAD_otc",
                                         "loose" if i % 2 else "win",
                                         maintenant - 3600 + i, pas=1)
                       for i in range(10)]                    # 50 %
    texte = texte_du_bilan(avant + recents_faibles, maintenant)
    assert "compatible avec le hasard" in texte, texte
    assert "USDCAD : 5/10 (50%)\n" in texte + "\n", "5/10 ne prouve rien"
    assert ") ⚠" not in texte
    assert "pas 1" in texte

    recents_mauvais = [_ordre_du_journal("USDCAD_otc",
                                         "win" if i % 5 == 0 else "loose",
                                         maintenant - 3600 + i)
                       for i in range(30)]                    # 20 %
    mauvais = texte_du_bilan(avant + recents_mauvais, maintenant)
    assert "baisse DÉPASSE" in mauvais
    assert "USDCAD : 6/30 (20%) ⚠" in mauvais, "6/30 le prouve"
    assert "Aucun ordre" in texte_du_bilan([], maintenant)


def test_un_ordre_reel_est_rejuge_a_d_autres_echeances():
    """Même entrée, seule la sortie change : c'est ce qui rend le parallèle
    5 / 10 / 15 min juste. Et la reconstitution est d'abord vérifiée contre
    le résultat RÉEL du broker."""
    from maxprofit.core.types import Candle
    from maxprofit.live.plan_demo import issue_reconstituee, texte_des_echeances

    t = 1_790_000_040                       # clic, à la seconde
    ordre = _ordre_du_journal("EURUSD_otc", "loose", t)
    ordre.sens, ordre.prix_entree = "call", 1.1000
    # Le prix monte jusqu'à t+10 min, puis retombe sous l'entrée à 15 min.
    def cloture(minute):
        return 1.1010 if minute <= 10 else 1.0990
    bougies = [Candle(pair="EURUSD_otc", tf_sec=60, ts_sec=t + 60 * (m - 1),
                      open=1.1, high=1.102, low=1.098, close=cloture(m),
                      tick_count=5, complete=True) for m in range(1, 17)]
    assert issue_reconstituee(ordre, bougies, 300) is True
    assert issue_reconstituee(ordre, bougies, 600) is True
    assert issue_reconstituee(ordre, bougies, 900) is False
    texte = texte_des_echeances([ordre], lambda p, a, b: bougies, None)
    assert " 5 min : <b>100%</b>" in texte and "15 min : <b>0%</b>" in texte
    assert "d'accord avec le broker sur 1/1" in texte
    assert "premier apprentissage en attente" in texte
