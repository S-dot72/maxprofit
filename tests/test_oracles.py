"""
Les cinq tests-oracles (spec §2.7) — le critère de sortie de l'étape 4.

    « On ne prouve pas la justesse d'un moteur de backtest. On le soumet à des
    cas dont on connaît la réponse à l'avance. Ces cinq tests sont bloquants.
    [...] Aucun résultat de stratégie n'est crédible tant qu'ils ne passent pas
    tous. »

Le principe est celui du thermomètre : avant de mesurer une fièvre, on vérifie
qu'il affiche 0 dans la glace et 100 dans l'eau bouillante. Une stratégie
aléatoire DOIT perdre 4 % par trade ; une stratégie clairvoyante DOIT gagner
92 %. Si le moteur affiche autre chose, ce n'est pas la stratégie qui est en
cause.

Les stratégies-oracles vivent ici et non dans `maxprofit/strategies/` : ce sont
des instruments de mesure, pas des stratégies. L'une d'elles lit d'ailleurs
l'avenir, ce qui n'a évidemment sa place dans aucun code de production —
`tests/test_layering.py` interdit une telle classe dans le paquet.

Tous les tirages sont GRAINÉS. Ces tests ne sont donc pas statistiquement
instables : ils réussissent ou échouent de façon reproductible, et un échec est
rejouable à l'identique.
"""

from __future__ import annotations

import random
from typing import Any, Mapping

import pytest

from maxprofit.backtest.engine import BacktestEngine
from maxprofit.backtest.execution import (
    ExecutionConfig,
    PayoutsEnMemoire,
    RegleEgalite,
    Resultat,
    TicksEnMemoire,
)
from maxprofit.core.market_view import MarketView
from maxprofit.core.strategy import Strategy
from maxprofit.core.types import Candle, Direction, PairInfo, Signal, Tick

PAIRE = "TEST_otc"
T0_SEC = 1_704_067_200
T0_MS = T0_SEC * 1000
TF_SEC = 60
TICKS_PAR_BOUGIE = 12
PAYOUT_PCT = 92
LATENCE_MS = 3000
EXPIRY_SEC = 60
LOOKBACK = 5


def config() -> ExecutionConfig:
    return ExecutionConfig(
        latence_ms=LATENCE_MS, expiry_sec=EXPIRY_SEC,
        regle_egalite=RegleEgalite.REMBOURSEMENT, payout_min_pct=90,
    )


# --------------------------------------------------------------------------- #
# Données : marche aléatoire symétrique
# --------------------------------------------------------------------------- #

def generer_ticks(n_bougies: int, graine: int) -> list[Tick]:
    """Marche aléatoire à dérive nulle.

    La symétrie est essentielle pour l'oracle n°1 : avec une dérive, une
    stratégie aléatoire gagnerait plus souvent dans un sens que dans l'autre et
    le taux ne serait plus 50 %. On tire donc des log-rendements centrés, dont
    la médiane est nulle, ce qui donne exactement une chance sur deux que le
    prix monte.
    """
    rng = random.Random(graine)
    prix = 1.10000
    ticks: list[Tick] = []
    pas_ms = (TF_SEC * 1000) // TICKS_PAR_BOUGIE
    for i in range(n_bougies * TICKS_PAR_BOUGIE):
        prix *= 1 + rng.gauss(0, 0.00015)
        # 5 décimales : c'est la cotation réelle d'une paire forex, et c'est
        # ce qui rend les égalités possibles.
        ticks.append(Tick(PAIRE, T0_MS + i * pas_ms, round(prix, 5)))
    return ticks


def agreger(ticks: list[Tick]) -> list[Candle]:
    """Bougies M1 à partir des ticks, comme le fait le collecteur."""
    par_bucket: dict[int, list[Tick]] = {}
    for t in ticks:
        par_bucket.setdefault(t.ts_ms // 1000 // TF_SEC * TF_SEC, []).append(t)
    bougies = []
    for ts_sec in sorted(par_bucket):
        groupe = par_bucket[ts_sec]
        prix = [t.price for t in groupe]
        bougies.append(Candle(
            pair=PAIRE, tf_sec=TF_SEC, ts_sec=ts_sec,
            open=prix[0], high=max(prix), low=min(prix), close=prix[-1],
            tick_count=len(groupe), complete=True,
        ))
    return bougies


def payouts() -> PayoutsEnMemoire:
    return PayoutsEnMemoire([(T0_SEC - 3600, PairInfo(PAIRE, True, PAYOUT_PCT))])


def moteur(ticks: list[Tick]) -> BacktestEngine:
    return BacktestEngine(
        TicksEnMemoire(ticks), payouts(), config(), trous_uptime=[],
    )


# --------------------------------------------------------------------------- #
# Stratégies-oracles
# --------------------------------------------------------------------------- #

class Aleatoire(Strategy):
    """Entrées au hasard, direction au hasard. L'espérance est connue d'avance."""

    name = "oracle-aleatoire"

    def __init__(self, graine: int, inverser: bool = False):
        self.graine = graine
        self.inverser = inverser
        self.rng = random.Random(graine)

    @property
    def params(self) -> Mapping[str, Any]:
        return {"graine": self.graine, "inverser": self.inverser}

    def reset(self) -> None:
        self.rng = random.Random(self.graine)

    def on_bar(self, view: MarketView) -> Signal | None:
        if len(view.candles(LOOKBACK)) < LOOKBACK:
            return None
        direction = self.rng.choice([Direction.CALL, Direction.PUT])
        if self.inverser:
            direction = direction.opposite
        return Signal(pair=view.pair, direction=direction,
                      decided_at_ms=view.now_ms, expiry_sec=EXPIRY_SEC)


class Clairvoyante(Strategy):
    """LIT LE PRIX FUTUR DE RÈGLEMENT.

    Elle contourne délibérément la `MarketView` en recevant la source de ticks
    en direct. C'est le seul endroit du projet où cela existe, et c'est
    volontaire : sans une stratégie qui gagne à coup sûr, on ne peut pas
    vérifier que le calcul du P&L et la résolution des trades sont corrects.
    """

    name = "oracle-clairvoyante"

    def __init__(self, ticks: TicksEnMemoire, cfg: ExecutionConfig):
        self.ticks = ticks
        self.cfg = cfg

    @property
    def params(self) -> Mapping[str, Any]:
        return {"triche": True}

    def on_bar(self, view: MarketView) -> Signal | None:
        if len(view.candles(LOOKBACK)) < LOOKBACK:
            return None
        entree_ms = view.now_ms + self.cfg.latence_ms
        reglement_ms = entree_ms + self.cfg.expiry_sec * 1000
        depart = self.ticks.premier_a_partir_de(view.pair, entree_ms, 2000)
        arrivee = self.ticks.dernier_jusqu_a(view.pair, reglement_ms, 2000)
        if depart is None or arrivee is None or arrivee.price == depart.price:
            return None
        direction = Direction.CALL if arrivee.price > depart.price else Direction.PUT
        return Signal(pair=view.pair, direction=direction,
                      decided_at_ms=view.now_ms, expiry_sec=EXPIRY_SEC)


# --------------------------------------------------------------------------- #
# 1. Entrées aléatoires — le test qui attrape le look-ahead
# --------------------------------------------------------------------------- #

def test_oracle_1_entrees_aleatoires():
    """5 000 trades au hasard.

    Espérance attendue : 0,5 × 0,92 − 0,5 = −4 % par trade.
    Taux de réussite attendu : 50 % ± 1,4 %.

    « Si le moteur affiche un profit, il est cassé. » Un moteur qui règle sur
    la clôture de la bougie de signal, qui oublie la latence, ou qui compare
    les prix dans le mauvais sens produit ici un résultat positif — et c'est le
    seul endroit où on le verra, parce qu'une vraie stratégie n'a pas de
    résultat connu d'avance auquel se comparer.
    """
    ticks = generer_ticks(5200, graine=42)
    rapport = moteur(ticks).run(Aleatoire(graine=7), agreger(ticks), lookback=LOOKBACK)

    assert len(rapport.resolus) >= 5000, (
        f"seulement {len(rapport.resolus)} trades résolus, il en faut 5 000 pour "
        f"que l'intervalle de ±1,4 % ait un sens"
    )

    taux = rapport.taux_reussite
    assert taux is not None, (
        f"aucun trade tranché : tous les trades sont des égalités, ce qui "
        f"signale un prix d'entrée lu au mauvais instant.\n"
        f"{rapport.resume()}"
    )
    assert 0.486 <= taux <= 0.514, (
        f"taux de réussite {taux * 100:.2f} % hors de 50 % ± 1,4 %. "
        f"Un moteur qui gagne au hasard lit l'avenir quelque part.\n"
        f"{rapport.resume()}"
    )

    assert rapport.pnl_moyen < 0, (
        f"le hasard est RENTABLE ({rapport.pnl_moyen * 100:+.2f} % par trade) : "
        f"le moteur est cassé.\n{rapport.resume()}"
    )
    assert rapport.pnl_moyen == pytest.approx(-0.04, abs=0.015), (
        f"espérance {rapport.pnl_moyen * 100:+.2f} % au lieu de -4 % attendus"
    )


# --------------------------------------------------------------------------- #
# 2. Clairvoyance — vérifie la résolution et le P&L
# --------------------------------------------------------------------------- #

def test_oracle_2_clairvoyance():
    """Une stratégie qui connaît le prix de règlement doit gagner 100 % des
    trades et rapporter le payout à chaque fois. Si elle ne gagne pas toujours,
    la résolution des trades ne correspond pas à la décision."""
    ticks = generer_ticks(600, graine=43)
    source = TicksEnMemoire(ticks)
    strategie = Clairvoyante(source, config())
    rapport = moteur(ticks).run(strategie, agreger(ticks), lookback=LOOKBACK)

    assert rapport.resolus, "aucun trade résolu"
    assert rapport.n_perdus == 0, (
        f"{rapport.n_perdus} trades perdus alors que la stratégie connaît le "
        f"résultat : la résolution ne correspond pas à la décision.\n"
        f"{rapport.resume()}"
    )
    assert rapport.taux_reussite == 1.0
    assert rapport.pnl_moyen == pytest.approx(PAYOUT_PCT / 100)


def test_oracle_2b_clairvoyante_inversee_perd_toujours():
    """Le miroir : en inversant la stratégie clairvoyante, on doit tout perdre.
    Ce test attrape un moteur qui jugerait tous les trades gagnants."""
    ticks = generer_ticks(600, graine=43)
    source = TicksEnMemoire(ticks)

    class Aveugle(Clairvoyante):
        def on_bar(self, view):
            signal = super().on_bar(view)
            if signal is None:
                return None
            return Signal(pair=signal.pair, direction=signal.direction.opposite,
                          decided_at_ms=signal.decided_at_ms,
                          expiry_sec=signal.expiry_sec)

    rapport = moteur(ticks).run(Aveugle(source, config()), agreger(ticks),
                                lookback=LOOKBACK)
    assert rapport.n_gagnes == 0
    assert rapport.pnl_moyen == pytest.approx(-1.0)


# --------------------------------------------------------------------------- #
# 3. Symétrie — révèle un biais dans la résolution
# --------------------------------------------------------------------------- #

def test_oracle_3_symetrie():
    """Inverser tous les signaux (CALL ↔ PUT) doit inverser le taux de réussite
    autour de 50 %.

    Une asymétrie signifie que le moteur traite les deux sens différemment :
    une inégalité stricte d'un côté et large de l'autre suffit, et le biais
    qu'elle introduit est de l'ordre du pourcent — assez pour transformer une
    stratégie perdante en gagnante sur le papier.
    """
    ticks = generer_ticks(3000, graine=44)
    bougies = agreger(ticks)

    endroit = moteur(ticks).run(Aleatoire(graine=9), bougies, lookback=LOOKBACK)
    envers = moteur(ticks).run(Aleatoire(graine=9, inverser=True), bougies,
                               lookback=LOOKBACK)

    assert endroit.n_gagnes + endroit.n_perdus == envers.n_gagnes + envers.n_perdus
    # Les égalités restent des égalités quel que soit le sens : ce sont
    # exactement les mêmes trades, donc les comptes doivent se croiser.
    assert endroit.n_gagnes == envers.n_perdus, (
        f"{endroit.n_gagnes} gagnés à l'endroit mais {envers.n_perdus} perdus à "
        f"l'envers : la résolution n'est pas symétrique."
    )
    assert endroit.n_egalites == envers.n_egalites
    assert endroit.taux_reussite + envers.taux_reussite == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 4. Données mélangées — toute performance résiduelle est un artefact
# --------------------------------------------------------------------------- #

def test_oracle_4_donnees_melangees():
    """On permute les rendements avant de reconstruire la série.

    Permuter détruit toute structure temporelle : aucune stratégie ne peut
    avoir d'edge sur du bruit réarrangé. Si le moteur affiche encore une
    performance, elle ne vient pas des données — elle vient du moteur.

    On permute les RENDEMENTS et non les bougies déjà formées : réordonner des
    bougies produirait des sauts de prix entre elles et des ticks incohérents
    avec les bougies, ce qui testerait la robustesse du moteur à des données
    absurdes plutôt que l'absence d'artefact.
    """
    rng = random.Random(99)
    origine = generer_ticks(3000, graine=45)

    rendements = [
        origine[i].price / origine[i - 1].price for i in range(1, len(origine))
    ]
    rng.shuffle(rendements)

    prix = origine[0].price
    melanges = [origine[0]]
    for i, r in enumerate(rendements, start=1):
        prix = round(prix * r, 5)
        melanges.append(Tick(PAIRE, origine[i].ts_ms, prix))

    rapport = moteur(melanges).run(Aleatoire(graine=11), agreger(melanges),
                                   lookback=LOOKBACK)

    assert rapport.pnl_moyen < 0, (
        f"performance résiduelle de {rapport.pnl_moyen * 100:+.2f} % sur des "
        f"données mélangées : c'est un artefact du moteur.\n{rapport.resume()}"
    )
    assert rapport.taux_reussite == pytest.approx(0.5, abs=0.02)


# --------------------------------------------------------------------------- #
# 5. Déterminisme — sinon un état fuit entre les trades
# --------------------------------------------------------------------------- #

def test_oracle_5_determinisme():
    """Deux exécutions identiques produisent des résultats identiques.

    On compare les trades un par un, pas seulement les agrégats : deux
    exécutions peuvent donner le même taux de réussite avec des trades
    différents, et c'est justement le symptôme d'un état qui fuit.
    """
    ticks = generer_ticks(800, graine=46)
    bougies = agreger(ticks)

    premier = moteur(ticks).run(Aleatoire(graine=13), bougies, lookback=LOOKBACK)
    second = moteur(ticks).run(Aleatoire(graine=13), bougies, lookback=LOOKBACK)

    assert premier.trades == second.trades
    assert premier.exclusions == second.exclusions
    assert premier.pnl_total == second.pnl_total


def test_oracle_5b_reset_efface_l_etat_entre_deux_executions():
    """Rejouer avec LA MÊME instance doit donner le même résultat.

    Sans `reset()`, le générateur aléatoire de la stratégie continuerait où il
    s'était arrêté et la seconde exécution différerait de la première. C'est
    exactement la fuite d'état qui ferait diverger deux fenêtres de
    walk-forward (§2.5).
    """
    ticks = generer_ticks(800, graine=46)
    bougies = agreger(ticks)
    strategie = Aleatoire(graine=13)
    moteur_unique = moteur(ticks)

    premier = moteur_unique.run(strategie, bougies, lookback=LOOKBACK)
    second = moteur_unique.run(strategie, bougies, lookback=LOOKBACK)

    assert premier.trades == second.trades
