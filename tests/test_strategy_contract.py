"""
Le contrat `Strategy` (spec §0, invariant n°1).

Ces tests ne valident aucune stratégie réelle — il n'en existe pas encore, elle
arrive à l'étape 5. Ils valident que le contrat tient : qu'une stratégie écrite
contre `MarketView` seule est mécaniquement incapable de lire le futur, et
qu'elle est déterministe, ce qui est le prérequis du test-oracle §2.7.5.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from maxprofit.core.market_view import MarketView, SequenceMarketView
from maxprofit.core.strategy import Strategy
from maxprofit.core.types import Direction, Signal
from conftest import make_candle


class StrategieDeTest(Strategy):
    """Franchit un seuil : CALL si la clôture dépasse la moyenne des `lookback`
    dernières bougies. Sans intérêt statistique, c'est voulu — elle ne sert
    qu'à exercer le contrat."""

    name = "seuil-moyenne"

    def __init__(self, lookback: int, expiry_sec: int):
        # Aucune valeur par défaut : les deux paramètres touchent à l'argent
        # (taille de fenêtre et durée d'exposition), donc ils sont obligatoires.
        self.lookback = lookback
        self.expiry_sec = expiry_sec
        self.vues = 0

    @property
    def params(self) -> Mapping[str, Any]:
        return {"lookback": self.lookback, "expiry_sec": self.expiry_sec}

    def on_bar(self, view: MarketView) -> Signal | None:
        self.vues += 1
        window = view.candles(self.lookback)
        if len(window) < self.lookback:
            return None  # historique insuffisant : on s'abstient
        moyenne = sum(c.close for c in window) / len(window)
        dernier = window[-1].close
        if dernier <= moyenne:
            return None
        return Signal(
            pair=view.pair,
            direction=Direction.CALL,
            decided_at_ms=view.now_ms,
            expiry_sec=self.expiry_sec,
            features={"ecart_moyenne_pct": (dernier / moyenne - 1) * 100},
            reason="cloture > moyenne",
        )

    def reset(self) -> None:
        self.vues = 0


def _parcours(strategie: Strategy, series) -> list[Signal]:
    view = SequenceMarketView("TEST_otc", series, index=0)
    signaux = []
    while True:
        s = strategie.on_bar(view)
        if s is not None:
            signaux.append(s)
        if not view.advance():
            return signaux


def test_strategy_ne_peut_pas_etre_instanciee_sans_on_bar():
    class Incomplete(Strategy):
        @property
        def params(self):
            return {}

    with pytest.raises(TypeError):
        Incomplete()


def test_s_abstient_tant_que_l_historique_est_trop_court(series):
    strat = StrategieDeTest(lookback=4, expiry_sec=60)
    signaux = _parcours(strat, series)
    assert strat.vues == 10
    # Les 3 premières bougies n'offrent pas 4 bougies d'historique.
    assert all(s.decided_at_ms >= series[3].close_ts_ms for s in signaux)


def test_le_signal_est_date_a_l_instant_de_decision(series):
    strat = StrategieDeTest(lookback=3, expiry_sec=60)
    view = SequenceMarketView("TEST_otc", series, index=5)
    signal = strat.on_bar(view)
    assert signal is not None
    # Contrat : decided_at_ms == view.now_ms. Un moteur rejette tout le reste.
    assert signal.decided_at_ms == view.now_ms
    assert signal.decided_at_ms == series[5].close_ts_ms


def test_deterministe_sur_deux_executions(series):
    """Prérequis du test-oracle §2.7.5 : identiques au bit près."""
    a = StrategieDeTest(lookback=3, expiry_sec=60)
    b = StrategieDeTest(lookback=3, expiry_sec=60)
    sig_a = _parcours(a, series)
    sig_b = _parcours(b, series)
    assert sig_a == sig_b


def test_reset_efface_l_etat_entre_deux_fenetres(series):
    """Un état qui survit ferait fuiter de l'information d'une fenêtre de
    walk-forward à la suivante (spec §2.5)."""
    strat = StrategieDeTest(lookback=3, expiry_sec=60)
    _parcours(strat, series)
    assert strat.vues == 10
    strat.reset()
    assert strat.vues == 0


def test_le_futur_est_hors_de_portee(series):
    """La stratégie voit 100..105 ; la série contient 100..109. Rien de ce qui
    suit le curseur ne doit apparaître, quelle que soit la taille demandée."""
    vues: list[float] = []

    class Curieuse(StrategieDeTest):
        def on_bar(self, view):
            vues.extend(c.close for c in view.candles(1000))
            return None

    strat = Curieuse(lookback=3, expiry_sec=60)
    view = SequenceMarketView("TEST_otc", series, index=5)
    strat.on_bar(view)
    assert max(vues) == 105.0
    assert not hasattr(view, "future")


def test_la_meme_serie_donne_le_meme_resultat_par_deux_chemins(series):
    """Invariant n°1 sous sa forme testable : une vue construite d'un bloc et
    une vue avancée pas à pas produisent exactement la même décision. C'est ce
    qui permettra au live (tampon glissant) et au backtest (curseur sur série
    complète) d'exécuter le même code sans diverger."""
    strat = StrategieDeTest(lookback=3, expiry_sec=60)

    par_curseur = _parcours(strat, series)

    strat.reset()
    par_tranches = []
    for i in range(len(series)):
        # Vue construite uniquement à partir du passé, comme le ferait un
        # tampon circulaire alimenté en direct.
        vue = SequenceMarketView("TEST_otc", series[: i + 1])
        s = strat.on_bar(vue)
        if s is not None:
            par_tranches.append(s)

    assert par_curseur == par_tranches


def test_une_bougie_inutilisable_reste_visible_mais_signalee():
    """La MarketView ne filtre pas : elle montre les bougies telles qu'elles
    ont été collectées, avec leur drapeau de qualité. C'est au moteur
    d'appliquer la règle §2.4 et de COMPTER les exclusions ; un filtrage
    silencieux dans la vue rendrait le taux d'exclusion inobservable."""
    series = [
        make_candle(0, 100.0),
        make_candle(1, 101.0, complete=False),
        make_candle(2, 102.0, tick_count=2),
    ]
    view = SequenceMarketView("TEST_otc", series)
    window = view.candles(3)
    assert len(window) == 3
    assert [c.is_usable() for c in window] == [True, False, False]
