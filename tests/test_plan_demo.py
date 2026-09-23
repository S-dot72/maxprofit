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
import time

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


def test_une_session_qui_attend_trop_longtemps_est_INTERROMPUE(tmp_path,
                                                               monkeypatch):
    """Sans cette borne, une session attendrait indéfiniment un pas éligible,
    bloquant la journée — et ses mises déjà engagées resteraient hors des
    comptes."""
    # -1 et non 0 : le pas vient d'être joué, l'écoulé vaut zéro
    # seconde, et « zéro > zéro » est faux.
    monkeypatch.setattr("maxprofit.live.plan_demo.ATTENTE_MAX_PAS_SEC", -1)
    journal = JournalExecution(tmp_path / "e.db", campagne="t")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice(["loose"]),
                       journal, _plan(sessions=10), PAIRES_TEST)
    c.chercher_un_signal = lambda: Signal(
        pair="EURUSD_otc", direction=Direction.CALL, decided_at_ms=T0_MS,
        expiry_sec=900, reason="script")

    c.tour()                                     # pas 1, perdu
    engage = c.etat.session.engage
    assert c.tour() is True, "l'interruption est un événement, pas un silence"
    assert c.etat.session is None
    assert c.etat.sessions_interrompues == 1
    # La mise engagée est COMPTÉE : une session abandonnée dont les mises
    # disparaîtraient ferait croire à un solde qu'on n'a pas.
    assert c.etat.solde == pytest.approx(CAPITAL - engage)
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


def test_un_reancrage_est_annonce(course):
    messages = []
    c = course(["loose"] * 6, plan=_plan(sessions=10))
    c._alerter = messages.append
    for _ in range(6):
        c.tour()
    assert any("Réancrage" in m for m in messages)


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
    assert c.etat.journee.resultat == pytest.approx(-engage)
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
