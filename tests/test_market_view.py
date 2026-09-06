"""
Le garde-fou anti-look-ahead (spec §2.1).

« L'accès au futur devient physiquement impossible, pas simplement déconseillé. »
Ces tests vérifient l'adverbe : ils essaient réellement d'atteindre le futur par
tous les chemins qu'offre l'objet, et constatent qu'aucun n'aboutit.
"""

from __future__ import annotations

import pytest

from maxprofit.core.errors import BotError, LookAheadError
from maxprofit.core.market_view import (
    MarketView,
    SequenceMarketView,
    assert_no_look_ahead,
)
from conftest import TF_SEC, make_candle


def test_la_vue_satisfait_le_protocole(series):
    assert isinstance(SequenceMarketView("TEST_otc", series), MarketView)


def test_candles_ne_depasse_jamais_le_curseur(series):
    # Curseur sur la bougie d'indice 4 (clôture 104). Tout ce qui suit est le
    # futur et ne doit apparaître sous aucune forme.
    view = SequenceMarketView("TEST_otc", series, index=4)
    for n in range(1, 20):
        window = view.candles(n)
        assert all(c.close <= 104.0 for c in window), (
            f"candles({n}) a laissé fuir une bougie postérieure au curseur"
        )
        assert window[-1].close == 104.0
        assert len(window) == min(n, 5)


def test_now_ms_est_la_cloture_de_la_derniere_bougie(series):
    view = SequenceMarketView("TEST_otc", series, index=0)
    assert view.now_ms == series[0].close_ts_ms
    assert view.now_ms == (series[0].ts_sec + TF_SEC) * 1000


def test_fenetre_plus_longue_que_l_historique_ne_leve_pas(series):
    # Au démarrage, la stratégie a moins d'historique que sa fenêtre. Elle doit
    # pouvoir le constater et s'abstenir, pas planter ni recevoir du remplissage.
    view = SequenceMarketView("TEST_otc", series, index=1)
    assert len(view.candles(50)) == 2


def test_n_invalide_leve(series):
    view = SequenceMarketView("TEST_otc", series)
    for bad in (0, -1, -100):
        with pytest.raises(BotError):
            view.candles(bad)
    with pytest.raises(BotError):
        view.candles(True)


def test_la_fenetre_est_immuable_et_ne_partage_rien(series):
    view = SequenceMarketView("TEST_otc", series, index=4)
    window = view.candles(3)
    assert isinstance(window, tuple)
    with pytest.raises((AttributeError, TypeError)):
        window[0].close = 999.0          # Candle est frozen
    # Muter la liste source après coup ne doit pas changer la vue : elle a copié.
    series.append(make_candle(99, 999.0))
    assert view.candles(50)[-1].close == 104.0


def test_la_serie_complete_n_est_pas_atteignable_par_le_protocole():
    # Le protocole MarketView n'expose que `pair`, `now_ms` et `candles`.
    # Toute autre voie d'accès serait une porte ouverte au look-ahead.
    exposed = {a for a in dir(MarketView) if not a.startswith("_")}
    assert exposed == {"pair", "now_ms", "candles"}


def test_advance_progresse_puis_s_arrete(series):
    view = SequenceMarketView("TEST_otc", series, index=0)
    steps = 0
    while view.advance():
        steps += 1
    assert steps == 9
    assert view.candles(1)[-1].close == 109.0
    assert view.advance() is False


def test_serie_desordonnee_refusee():
    # Un moteur qui trie mal produit du look-ahead sans le savoir.
    desordre = [make_candle(2, 102.0), make_candle(1, 101.0)]
    with pytest.raises(BotError, match="croissante"):
        SequenceMarketView("TEST_otc", desordre)


def test_doublon_refuse():
    with pytest.raises(BotError, match="croissante"):
        SequenceMarketView("TEST_otc", [make_candle(1, 101.0), make_candle(1, 101.0)])


def test_melange_de_paires_refuse():
    melange = [make_candle(0, 100.0), make_candle(1, 101.0, pair="AUTRE_otc")]
    with pytest.raises(BotError, match="une seule paire"):
        SequenceMarketView("TEST_otc", melange)


def test_melange_de_timeframes_refuse():
    melange = [make_candle(0, 100.0), make_candle(1, 101.0, tf_sec=300)]
    with pytest.raises(BotError):
        SequenceMarketView("TEST_otc", melange)


def test_serie_vide_refusee():
    with pytest.raises(BotError):
        SequenceMarketView("TEST_otc", [])


def test_assert_no_look_ahead_accepte_une_vue_saine(series):
    assert_no_look_ahead(SequenceMarketView("TEST_otc", series, index=3), tf_sec=TF_SEC)


def test_assert_no_look_ahead_detecte_une_vue_incoherente(series):
    class VueTruquee:
        """Vue dont `now_ms` précède la clôture de sa dernière bougie : la
        stratégie verrait une bougie pas encore terminée."""

        pair = "TEST_otc"

        def __init__(self, candles):
            self._c = tuple(candles)

        @property
        def now_ms(self):
            return self._c[-1].ts_sec * 1000   # début, pas clôture

        def candles(self, n):
            return self._c[-n:]

    with pytest.raises(LookAheadError, match="jamais être visible"):
        assert_no_look_ahead(VueTruquee(series[:4]), tf_sec=TF_SEC)


def test_assert_no_look_ahead_detecte_le_mauvais_timeframe(series):
    view = SequenceMarketView("TEST_otc", series, index=3)
    with pytest.raises(LookAheadError, match="tf="):
        assert_no_look_ahead(view, tf_sec=300)
