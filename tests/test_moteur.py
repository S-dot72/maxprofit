"""
Les garanties propres du moteur (spec §2.2 et §2.4).

Les oracles vérifient que le moteur ne ment pas sur les résultats. Ces
tests-ci vérifient qu'il refuse ce qu'il doit refuser — et surtout qu'il le
COMPTE, parce qu'une exclusion silencieuse et une absence de signal se
ressemblent parfaitement dans un rapport.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from maxprofit.backtest.engine import BacktestEngine, MotifExclusion
from maxprofit.backtest.execution import (
    ExecutionConfig,
    MotifIrresolu,
    PayoutsEnMemoire,
    RegleEgalite,
    Resultat,
    TicksEnMemoire,
    executer,
)
from maxprofit.core.errors import BotError
from maxprofit.core.market_view import MarketView
from maxprofit.core.strategy import Strategy
from maxprofit.core.types import Candle, Direction, PairInfo, Signal, Tick

PAIRE = "TEST_otc"
T0_SEC = 1_704_067_200
T0_MS = T0_SEC * 1000
TF_SEC = 60


def cfg(**kw) -> ExecutionConfig:
    base = dict(latence_ms=3000, expiry_sec=60,
                regle_egalite=RegleEgalite.REMBOURSEMENT, payout_min_pct=90)
    return ExecutionConfig(**{**base, **kw})


def bougie(i: int, prix: float, *, complete=True, tick_count=10) -> Candle:
    return Candle(pair=PAIRE, tf_sec=TF_SEC, ts_sec=T0_SEC + i * TF_SEC,
                  open=prix, high=prix, low=prix, close=prix,
                  tick_count=tick_count, complete=complete)


def ticks_reguliers(n_bougies: int, prix: float = 1.1) -> list[Tick]:
    return [Tick(PAIRE, T0_MS + i * 1000, prix) for i in range(n_bougies * 60 + 300)]


class ToujoursCall(Strategy):
    name = "toujours-call"

    @property
    def params(self) -> Mapping[str, Any]:
        return {}

    def on_bar(self, view: MarketView) -> Signal | None:
        return Signal(pair=view.pair, direction=Direction.CALL,
                      decided_at_ms=view.now_ms, expiry_sec=60)


def construire(bougies, ticks, *, trous=(), payout=92, ouvert=True):
    return BacktestEngine(
        TicksEnMemoire(ticks),
        PayoutsEnMemoire([(T0_SEC - 60, PairInfo(PAIRE, ouvert, payout))]),
        cfg(), trous_uptime=list(trous),
    ), bougies


# --------------------------------------------------------------------------- #
# §5 — pas de valeur par défaut sur ce qui touche à l'argent
# --------------------------------------------------------------------------- #

def test_latence_nulle_refusee():
    """Une latence de 0 signifie « je clique à l'instant même de la clôture ».
    C'est le raccourci qui rend un backtest faux, il doit lever."""
    with pytest.raises(BotError, match="latence"):
        cfg(latence_ms=0)


def test_regle_d_egalite_obligatoire():
    with pytest.raises(BotError):
        ExecutionConfig(latence_ms=3000, expiry_sec=60,
                        regle_egalite="remboursement", payout_min_pct=90)


# --------------------------------------------------------------------------- #
# §2.4 — qualité des données
# --------------------------------------------------------------------------- #

def test_bougie_incomplete_dans_la_fenetre_bloque_le_signal():
    """La fenêtre entière compte, pas seulement la bougie courante : une
    moyenne calculée sur une bougie incomplète est fausse même si la dernière
    bougie est parfaite."""
    bougies = [bougie(i, 1.1) for i in range(10)]
    bougies[5] = bougie(5, 1.1, complete=False)
    moteur, serie = construire(bougies, ticks_reguliers(10))

    rapport = moteur.run(ToujoursCall(), serie, lookback=3)

    assert rapport.exclusions[MotifExclusion.BOUGIE_INCOMPLETE] == 3
    assert all(t.decided_at_ms != bougies[5].close_ts_ms for t in rapport.trades)


def test_ticks_insuffisants_bloquent_le_signal():
    bougies = [bougie(i, 1.1) for i in range(10)]
    bougies[7] = bougie(7, 1.1, tick_count=4)     # seuil = 5
    moteur, serie = construire(bougies, ticks_reguliers(10))

    rapport = moteur.run(ToujoursCall(), serie, lookback=2)
    assert rapport.exclusions[MotifExclusion.TICKS_INSUFFISANTS] == 2


def test_trou_d_uptime_bloque_le_signal():
    """Un trou de connexion ne se voit pas dans les bougies : elles sont
    simplement absentes, ou complètes de part et d'autre. C'est la table
    `uptime` qui le sait."""
    bougies = [bougie(i, 1.1) for i in range(10)]
    trou = (T0_SEC + 4 * TF_SEC, T0_SEC + 6 * TF_SEC)
    moteur, serie = construire(bougies, ticks_reguliers(10), trous=[trou])

    rapport = moteur.run(ToujoursCall(), serie, lookback=1)
    assert rapport.exclusions[MotifExclusion.TROU_UPTIME] == 2


def test_le_pourcentage_ecarte_est_rapporte():
    """§2.4 : au-delà de 10 %, c'est la collecte qu'il faut réparer."""
    bougies = [bougie(i, 1.1, complete=(i % 2 == 0)) for i in range(20)]
    moteur, serie = construire(bougies, ticks_reguliers(20))

    rapport = moteur.run(ToujoursCall(), serie, lookback=1)
    assert rapport.pct_bougies_ecartees == pytest.approx(50.0)
    assert rapport.collecte_suspecte
    assert "ATTENTION" in rapport.resume()


def test_historique_insuffisant_n_est_pas_un_defaut_de_qualite():
    """Les premières bougies d'une série ne sont pas de mauvaises données :
    elles sont juste au début. Les compter dans le taux d'exclusion qualité
    ferait accuser la collecte à tort."""
    bougies = [bougie(i, 1.1) for i in range(10)]
    moteur, serie = construire(bougies, ticks_reguliers(10))

    rapport = moteur.run(ToujoursCall(), serie, lookback=5)
    assert rapport.exclusions[MotifExclusion.HISTORIQUE_INSUFFISANT] == 4
    assert rapport.pct_bougies_ecartees == 0.0


# --------------------------------------------------------------------------- #
# §2.3 — payout d'époque
# --------------------------------------------------------------------------- #

def test_paire_fermee_ne_genere_aucun_trade():
    """« Un trade sur une paire qui n'était pas éligible à cet instant n'est
    pas généré du tout. » Pas perdu : pas généré."""
    bougies = [bougie(i, 1.1) for i in range(10)]
    moteur, serie = construire(bougies, ticks_reguliers(10), ouvert=False)

    rapport = moteur.run(ToujoursCall(), serie, lookback=1)
    assert rapport.trades == []
    assert rapport.n_signaux == 10
    assert rapport.exclusions[MotifExclusion.PAIRE_NON_ELIGIBLE] == 10


def test_payout_sous_le_minimum_ne_genere_aucun_trade():
    bougies = [bougie(i, 1.1) for i in range(10)]
    moteur, serie = construire(bougies, ticks_reguliers(10), payout=70)

    rapport = moteur.run(ToujoursCall(), serie, lookback=1)
    assert rapport.trades == []


def test_le_payout_du_trade_est_celui_de_l_epoque():
    bougies = [bougie(i, 1.1) for i in range(6)]
    payouts = PayoutsEnMemoire([
        (T0_SEC - 60, PairInfo(PAIRE, True, 92)),
        (T0_SEC + 3 * TF_SEC, PairInfo(PAIRE, True, 95)),
    ])
    moteur = BacktestEngine(TicksEnMemoire(ticks_reguliers(6)), payouts, cfg(),
                            trous_uptime=[])

    rapport = moteur.run(ToujoursCall(), bougies, lookback=1)
    payouts_utilises = [t.payout_pct for t in rapport.trades]
    assert 92 in payouts_utilises and 95 in payouts_utilises
    assert payouts_utilises == sorted(payouts_utilises), (
        "le payout doit changer au moment du relevé, pas avant"
    )


# --------------------------------------------------------------------------- #
# §2.2 — exécution
# --------------------------------------------------------------------------- #

def test_le_prix_d_entree_est_le_premier_tick_apres_la_latence():
    signal = Signal(PAIRE, Direction.CALL, T0_MS, expiry_sec=60)
    ticks = TicksEnMemoire([
        Tick(PAIRE, T0_MS, 1.10000),              # clôture de la bougie
        Tick(PAIRE, T0_MS + 2500, 1.20000),       # avant la latence
        Tick(PAIRE, T0_MS + 3200, 1.30000),       # <- premier après 3000 ms
        Tick(PAIRE, T0_MS + 63000, 1.40000),      # règlement
    ])
    trade = executer(signal, PairInfo(PAIRE, True, 92), ticks, cfg())

    assert trade.entry_price == 1.30000, "le moteur n'a pas appliqué la latence"
    assert trade.entry_ts_ms == T0_MS + 3200


def test_trade_irresolvable_est_exclu_et_non_compte_perdant():
    """Le compter perdant biaiserait les résultats dans une direction qui dépend
    de la qualité de la collecte : les trous surviennent pendant les
    déconnexions, qui ne sont pas réparties au hasard dans la journée."""
    signal = Signal(PAIRE, Direction.CALL, T0_MS, expiry_sec=60)
    ticks = TicksEnMemoire([Tick(PAIRE, T0_MS + 3200, 1.3)])   # aucun règlement
    trade = executer(signal, PairInfo(PAIRE, True, 92), ticks, cfg())

    assert trade.resultat is Resultat.IRRESOLU
    assert trade.motif_irresolu is MotifIrresolu.AUCUN_TICK_REGLEMENT
    assert trade.pnl is None
    assert not trade.compte_dans_les_stats


def test_tolerance_de_deux_secondes():
    signal = Signal(PAIRE, Direction.CALL, T0_MS, expiry_sec=60)
    dans = TicksEnMemoire([Tick(PAIRE, T0_MS + 3000, 1.1),
                           Tick(PAIRE, T0_MS + 61000, 1.2)])   # 2 s avant
    hors = TicksEnMemoire([Tick(PAIRE, T0_MS + 3000, 1.1),
                           Tick(PAIRE, T0_MS + 60000, 1.2)])   # 3 s avant

    assert executer(signal, PairInfo(PAIRE, True, 92), dans, cfg()).resultat \
        is Resultat.GAGNE
    assert executer(signal, PairInfo(PAIRE, True, 92), hors, cfg()).resultat \
        is Resultat.IRRESOLU


def test_regle_d_egalite_remboursement_contre_perte():
    signal = Signal(PAIRE, Direction.CALL, T0_MS, expiry_sec=60)
    ticks = TicksEnMemoire([Tick(PAIRE, T0_MS + 3000, 1.10000),
                            Tick(PAIRE, T0_MS + 63000, 1.10000)])
    info = PairInfo(PAIRE, True, 92)

    rembourse = executer(signal, info, ticks, cfg(regle_egalite=RegleEgalite.REMBOURSEMENT))
    perdu = executer(signal, info, ticks, cfg(regle_egalite=RegleEgalite.PERTE))

    assert rembourse.resultat is Resultat.EGALITE and rembourse.pnl == 0.0
    assert perdu.resultat is Resultat.EGALITE and perdu.pnl == -1.0


# --------------------------------------------------------------------------- #
# Rejets
# --------------------------------------------------------------------------- #

def test_signal_mal_date_est_rejete():
    """Rejeté, pas recalé : recaler silencieusement réparerait le symptôme d'un
    bug qui continuerait ailleurs."""
    class MalDatee(ToujoursCall):
        def on_bar(self, view):
            return Signal(pair=view.pair, direction=Direction.CALL,
                          decided_at_ms=view.now_ms - 60_000, expiry_sec=60)

    moteur, serie = construire([bougie(i, 1.1) for i in range(5)], ticks_reguliers(5))
    with pytest.raises(BotError, match="instant de décision"):
        moteur.run(MalDatee(), serie, lookback=1)


def test_expiration_incoherente_est_rejetee():
    """Le rapport annoncerait une expiration qui n'est pas celle simulée."""
    class MauvaiseExpiration(ToujoursCall):
        def on_bar(self, view):
            return Signal(pair=view.pair, direction=Direction.CALL,
                          decided_at_ms=view.now_ms, expiry_sec=300)

    moteur, serie = construire([bougie(i, 1.1) for i in range(5)], ticks_reguliers(5))
    with pytest.raises(BotError, match="expiration"):
        moteur.run(MauvaiseExpiration(), serie, lookback=1)


def test_serie_vide_refusee():
    moteur, _ = construire([], [])
    with pytest.raises(BotError, match="série vide"):
        moteur.run(ToujoursCall(), [], lookback=1)
