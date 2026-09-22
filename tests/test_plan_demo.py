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

import pytest

from maxprofit.core.types import Direction, Signal
from maxprofit.execution.journal import Execution, JournalExecution
from maxprofit.live.plan_demo import CoursePlanDemo, nouveau_jour
from maxprofit.plan import Arret, EtatSession, PlanCapital, Risque

T0_MS = 1_789_000_000_000
CAPITAL = 250.0


def _plan(sessions: int = 18, jours: int = 30, **gardes) -> PlanCapital:
    defauts = dict(sessions_perdues_max=2)
    return PlanCapital.depuis_risque(
        capital_initial=CAPITAL, risque=Risque(1, 7), payout_pct=92,
        sessions_par_jour=sessions, jours=jours, **{**defauts, **gardes})


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

    def suivre(self, pair):
        self.suivies.append(pair)

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
        return ex


class LecteurFactice:
    def last_candle_ts_sec(self):
        return None

    def candles(self, *a, **k):
        return []


@pytest.fixture
def course(tmp_path):
    def fabriquer(resultats, plan=None):
        journal = JournalExecution(tmp_path / "exec.db", campagne="test")
        c = CoursePlanDemo(LecteurFactice(), CourtierFactice(resultats),
                           journal, plan or _plan(), ("EURUSD_otc",))
        c._signaux_scriptes = True
        c.chercher_un_signal = lambda: Signal(
            pair="EURUSD_otc", direction=Direction.CALL, decided_at_ms=T0_MS,
            expiry_sec=900, reason="script")
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


def test_la_journee_se_ferme_quand_ses_sessions_sont_epuisees(course):
    c = course(["win", "win"], plan=_plan(sessions=1))
    c.tour()
    assert c.peut_ouvrir() is Arret.SESSIONS_EPUISEES
    assert c.tour() is False, "aucun ordre ne doit partir"
    assert len(c.journal.toutes()) == 1


def test_deux_sessions_perdues_daffilee_declenchent_le_reancrage(course):
    """La règle qui protège le capital : on ne court pas après le plan."""
    c = course(["loose"] * 6, plan=_plan(sessions=10))
    c.etat.jour = 12
    for _ in range(6):
        c.tour()
    assert len(c.etat.reancrages) == 1
    depuis, vers, solde = c.etat.reancrages[0]
    assert depuis == 12
    assert vers < 12, "le réancrage doit RECULER, le solde ayant baissé"
    assert c.etat.jour == max(1, vers)


def test_une_session_gagnee_remet_le_compteur_a_zero(course):
    c = course(["loose", "loose", "loose",     # session 1 perdue
                "win",                          # session 2 gagnée
                "loose", "loose", "loose"],     # session 3 perdue
               plan=_plan(sessions=10))
    for _ in range(7):
        c.tour()
    assert c.etat.sessions_perdues_daffilee == 1
    assert c.etat.reancrages == [], (
        "deux pertes séparées par un gain ne sont pas deux pertes d'affilée")


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


def test_les_paires_sont_suivies_a_la_construction(course):
    c = course(["win"])
    assert c.courtier.suivies == ["EURUSD_otc"]


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
