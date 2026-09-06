"""
Critère de sortie de l'étape 5 : « 400+ évaluations avec features et
contrefactuels ».

Et surtout le §3.1, que la spec appelle elle-même le point le plus important :

    « Puis, dans outcomes, le résultat de chaque évaluation Y COMPRIS CELLES
    QUI N'ONT PAS GÉNÉRÉ DE SIGNAL : quel aurait été le résultat si on avait
    pris le trade. [...] Sans les quasi-signaux et leur résultat contrefactuel,
    vous ne pouvez pas répondre à "qu'est-ce que le bot a raté". Avec eux,
    chaque condition devient mesurable. »

Le test décisif est `test_les_quasi_signaux_ont_un_contrefactuel` : il vérifie
qu'une bougie SANS signal porte quand même un résultat. C'est ce qui distingue
un journal exploitable d'un journal qui ne sait raconter que ce qu'on a déjà
fait.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from maxprofit.backtest.engine import BacktestEngine
from maxprofit.backtest.execution import (
    ExecutionConfig,
    PayoutsEnMemoire,
    RegleEgalite,
    TicksEnMemoire,
)
from maxprofit.core.types import PairInfo
from maxprofit.store.research import (
    Journal,
    commit_git_courant,
    compter_experiences,
    hash_jeu_de_donnees,
    open_recherche,
)
from maxprofit.strategies.six_conditions import PARAMETRES_DEPART, SixConditions

from test_oracles import PAIRE, T0_SEC, agreger, generer_ticks

LOOKBACK = PARAMETRES_DEPART.lookback


def _config() -> ExecutionConfig:
    return ExecutionConfig(latence_ms=3000, expiry_sec=60,
                           regle_egalite=RegleEgalite.REMBOURSEMENT,
                           payout_min_pct=90)


@pytest.fixture(scope="module")
def execution(tmp_path_factory):
    """Un backtest complet, journalisé. Partagé par le module : le rejouer à
    chaque test coûterait des secondes sans rien prouver de plus."""
    dossier = tmp_path_factory.mktemp("recherche")
    ticks = generer_ticks(900, graine=101)
    bougies = agreger(ticks)

    moteur = BacktestEngine(
        TicksEnMemoire(ticks),
        PayoutsEnMemoire([(T0_SEC - 3600, PairInfo(PAIRE, True, 92))]),
        _config(), trous_uptime=[],
    )
    strategie = SixConditions(PARAMETRES_DEPART)

    conn = open_recherche(dossier / "research.db")
    journal = Journal(conn)
    journal.demarrer(
        strategie=strategie.name, params=strategie.params, config=_config(),
        commit_git=commit_git_courant(), hash_donnees=hash_jeu_de_donnees(bougies),
        note="test de journalisation",
    )
    rapport = moteur.run(strategie, bougies, lookback=LOOKBACK, journal=journal)
    journal.terminer({
        "n_evaluations": rapport.n_evaluations,
        "n_signaux": rapport.n_signaux,
        "taux_reussite": rapport.taux_reussite,
        "pnl_moyen": rapport.pnl_moyen,
    })
    conn.commit()
    return rapport, dossier / "research.db"


def _lire(db: Path, requete: str, *args):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(requete, args).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Le critère de sortie
# --------------------------------------------------------------------------- #

def test_au_moins_400_evaluations(execution):
    rapport, db = execution
    (ligne,) = _lire(db, "SELECT COUNT(*) AS n FROM evaluations")
    assert ligne["n"] >= 400, (
        f"seulement {ligne['n']} évaluations enregistrées. En dessous de 400, "
        f"un taux de réussite mesuré ne permet pas de distinguer 52 % de 58 %."
    )
    assert ligne["n"] == rapport.n_evaluations


def test_chaque_evaluation_porte_ses_features(execution):
    _, db = execution
    lignes = _lire(db, "SELECT features_json FROM evaluations LIMIT 200")
    attendues = {"ma_distance_pct", "bb_percent_b", "stoch_k", "stoch_d",
                 "atr_normalise_pct", "corps_pct", "meche_haute_pct",
                 "meche_basse_pct", "heure_utc"}
    for ligne in lignes:
        features = json.loads(ligne["features_json"])
        manquantes = attendues - set(features)
        assert not manquantes, f"features absentes : {sorted(manquantes)}"
        assert all(isinstance(v, (int, float)) for v in features.values())


def test_les_quasi_signaux_ont_un_contrefactuel(execution):
    """LE test du §3.1.

    Une bougie qui n'a PAS produit de signal doit quand même porter le résultat
    qu'aurait eu le trade. Sans cela, on ne peut comparer les bougies retenues
    qu'à elles-mêmes, et aucune condition n'est mesurable.
    """
    _, db = execution
    (ligne,) = _lire(db, """
        SELECT COUNT(*) AS n
        FROM evaluations e JOIN outcomes o ON o.evaluation_id = e.id
        WHERE e.signal_emis = 0
    """)
    assert ligne["n"] >= 400, (
        f"seulement {ligne['n']} contrefactuels sur des bougies sans signal. "
        f"C'est précisément ce qui manque pour répondre à « qu'est-ce que le "
        f"bot a raté »."
    )


def test_chaque_evaluation_bloquee_nomme_sa_condition(execution):
    """« condition_bloquante : laquelle a échoué (NULL si signal émis) »."""
    _, db = execution
    lignes = _lire(db, """
        SELECT signal_emis, condition_bloquante, COUNT(*) AS n
        FROM evaluations GROUP BY signal_emis, condition_bloquante
    """)
    for ligne in lignes:
        if ligne["signal_emis"]:
            assert ligne["condition_bloquante"] is None
        else:
            assert ligne["condition_bloquante"] is not None

    noms = {l["condition_bloquante"] for l in lignes if l["condition_bloquante"]}
    assert len(noms) >= 2, (
        f"une seule condition bloque jamais ({noms}) : soit elle est placée en "
        f"tête et masque les autres, soit les cinq autres ne servent à rien. "
        f"C'est exactement ce que l'étape 6 doit trancher."
    )


def test_les_conditions_gardent_leur_valeur_numerique(execution):
    """Un booléen ne dit pas si la condition a échoué de peu. La valeur brute,
    si — et c'est elle qui permettra de savoir si un seuil est mal placé."""
    _, db = execution
    lignes = _lire(db, "SELECT conditions_json FROM evaluations LIMIT 100")
    avec_valeur = 0
    for ligne in lignes:
        conditions = json.loads(ligne["conditions_json"])
        assert conditions
        for c in conditions:
            assert set(c) == {"nom", "validee", "valeur"}
            if c["valeur"] is not None:
                avec_valeur += 1
    assert avec_valeur > 0


# --------------------------------------------------------------------------- #
# §2.6 — le compteur d'hypothèses
# --------------------------------------------------------------------------- #

def test_l_experience_enregistre_de_quoi_la_rejouer(execution):
    _, db = execution
    (exp,) = _lire(db, "SELECT * FROM experiments")
    assert exp["strategie"] == "six-conditions-v1"
    assert exp["commit_git"]
    assert exp["hash_donnees"]
    params = json.loads(exp["params_json"])
    # Les seuils complets, pas un résumé : un backtest qu'on ne peut pas
    # rejouer à l'identique ne compte pas comme une expérience.
    assert params["seuil_stoch_bas"] == PARAMETRES_DEPART.seuil_stoch_bas
    assert params["zigzag_seuil_pct"] == PARAMETRES_DEPART.zigzag_seuil_pct
    config = json.loads(exp["config_json"])
    assert config["latence_ms"] == 3000


def test_une_experience_ne_peut_pas_etre_modifiee(execution):
    _, db = execution
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immuable"):
            conn.execute("UPDATE experiments SET strategie = 'menteuse'")
    finally:
        conn.close()


def test_une_experience_ne_peut_pas_etre_supprimee(execution):
    """La tentation d'effacer « les essais ratés qui polluent » arrive
    précisément quand le compteur commence à dire quelque chose de
    désagréable."""
    _, db = execution
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="indelebile"):
            conn.execute("DELETE FROM experiments WHERE id = 1")
    finally:
        conn.close()


def test_le_compteur_distingue_les_executions_interrompues(execution):
    _, db = execution
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        compte = compter_experiences(conn)
    finally:
        conn.close()
    assert compte == {"total": 1, "abouties": 1, "interrompues": 0}


def test_le_hash_change_quand_les_donnees_changent():
    """Deux backtests aux métriques identiques mais aux empreintes différentes
    n'ont pas tourné sur les mêmes données."""
    a = agreger(generer_ticks(50, graine=1))
    b = agreger(generer_ticks(50, graine=2))
    assert hash_jeu_de_donnees(a) == hash_jeu_de_donnees(a)
    assert hash_jeu_de_donnees(a) != hash_jeu_de_donnees(b)
    assert hash_jeu_de_donnees(a) != hash_jeu_de_donnees(a[:-1])


# --------------------------------------------------------------------------- #
# Cohérence journal / rapport
# --------------------------------------------------------------------------- #

def test_le_journal_et_le_rapport_racontent_la_meme_chose(execution):
    rapport, db = execution
    (ligne,) = _lire(db, "SELECT COUNT(*) AS n FROM evaluations WHERE signal_emis = 1")
    assert ligne["n"] == rapport.n_signaux
    (outcomes,) = _lire(db, "SELECT COUNT(*) AS n FROM outcomes")
    assert outcomes["n"] == len(rapport.trades) + len(rapport.contrefactuels)


def test_le_contrefactuel_suit_le_meme_chemin_que_le_trade_reel(execution):
    """Les deux passent par `executer`. S'ils étaient calculés différemment, la
    comparaison de l'étape 6 opposerait deux mesures incomparables."""
    _, db = execution
    lignes = _lire(db, """
        SELECT e.signal_emis, o.resultat, o.payout_pct, o.entry_price, o.settle_price
        FROM evaluations e JOIN outcomes o ON o.evaluation_id = e.id
    """)
    resultats = {l["resultat"] for l in lignes}
    assert resultats <= {"GAGNE", "PERDU", "EGALITE", "IRRESOLU"}
    for ligne in lignes:
        assert ligne["payout_pct"] == 92
        if ligne["resultat"] != "IRRESOLU":
            assert ligne["entry_price"] is not None
            assert ligne["settle_price"] is not None


# --------------------------------------------------------------------------- #
# Ce que les contrefactuels révèlent immédiatement
# --------------------------------------------------------------------------- #

def test_les_contrefactuels_demasquent_un_taux_flatteur(execution):
    """La démonstration de tout le §3.1, sur les données de ce test.

    Ces bougies sont une marche aléatoire : il n'y a AUCUN edge à trouver, par
    construction. Pourtant la stratégie affiche un taux de réussite flatteur —
    parce qu'elle n'a émis qu'une douzaine de signaux, et qu'une douzaine de
    tirages à pile ou face donne souvent 58 %.

    Les 800+ contrefactuels, eux, sont sans appel : ils reviennent à 50 %. Le
    taux affiché sur les signaux n'était pas un edge, c'était la taille de
    l'échantillon.

    C'est exactement ce que la spec §2.6 annonce — « vous confondrez chance et
    découverte » — et ce que les quasi-signaux permettent de voir tout de
    suite, au lieu d'attendre trois semaines de collecte pour s'en apercevoir.
    """
    rapport, db = execution

    (contre,) = _lire(db, """
        SELECT
            SUM(o.resultat = 'GAGNE') AS gagnes,
            SUM(o.resultat = 'PERDU') AS perdus
        FROM evaluations e JOIN outcomes o ON o.evaluation_id = e.id
        WHERE e.signal_emis = 0
    """)
    decides = contre["gagnes"] + contre["perdus"]
    taux_contrefactuel = contre["gagnes"] / decides

    assert decides >= 400, "pas assez de contrefactuels tranchés pour conclure"
    assert 0.45 <= taux_contrefactuel <= 0.55, (
        f"les quasi-signaux affichent {taux_contrefactuel * 100:.1f} % sur une "
        f"marche aléatoire : le moteur ou les données ont un biais"
    )

    # Et le contraste avec le taux affiché sur les signaux réels.
    assert len(rapport.resolus) < 100, (
        "avec autant de trades, l'argument de la taille d'échantillon ne "
        "tiendrait plus et ce test devrait être repensé"
    )


def test_une_condition_qui_ne_bloque_jamais_est_deja_suspecte(execution):
    """Première mesure d'attribution, presque gratuite.

    Une condition qui n'apparaît jamais comme bloquante n'a jamais écarté une
    seule bougie. Elle ne réduit pas le nombre de trades, donc elle n'améliore
    rien non plus : elle est décorative. L'étape 6 le confirmera proprement par
    ablation, mais le journal le dit déjà.
    """
    _, db = execution
    lignes = _lire(db, """
        SELECT condition_bloquante, COUNT(*) AS n FROM evaluations
        WHERE signal_emis = 0 GROUP BY 1
    """)
    bloquantes = {l["condition_bloquante"] for l in lignes}
    toutes = {"distance_ma", "bollinger", "stochastique", "volatilite",
              "pivot_zigzag", "fractale"}
    inertes = toutes - bloquantes

    # On n'exige pas qu'il n'y en ait aucune : on exige de le SAVOIR. Le jour
    # où ce test échoue parce que la liste change, c'est une information sur la
    # stratégie, pas une régression du code.
    assert inertes <= {"volatilite"}, (
        f"conditions jamais bloquantes : {sorted(inertes)}. À vérifier à "
        f"l'étape 6 : une condition qui n'écarte rien ne sert à rien."
    )
