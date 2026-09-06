"""
Les types du domaine se valident à la construction : une donnée incohérente
lève là où l'on sait encore d'où elle vient, pas trois semaines plus tard dans
un rapport de backtest.
"""

from __future__ import annotations

import pytest

from maxprofit.core.errors import BotError, TimebaseError
from maxprofit.core.types import Candle, Direction, PairInfo, Signal, Tick
from conftest import T0_SEC, TF_SEC, make_candle

T0_MS = T0_SEC * 1000


def test_direction_opposite():
    assert Direction.CALL.opposite is Direction.PUT
    assert Direction.PUT.opposite is Direction.CALL
    # Le test-oracle de symétrie (§2.7.3) repose sur cette involution.
    assert Direction.CALL.opposite.opposite is Direction.CALL


def test_tick_en_secondes_leve():
    with pytest.raises(TimebaseError):
        Tick("EURUSD_otc", T0_SEC, 1.1)


def test_tick_prix_invalide_leve():
    for bad in (0.0, -1.0):
        with pytest.raises(BotError):
            Tick("EURUSD_otc", T0_MS, bad)


def test_candle_ohlc_incoherent_leve():
    with pytest.raises(BotError, match="OHLC"):
        Candle("P_otc", TF_SEC, T0_SEC, open=5.0, high=1.0, low=0.5,
               close=0.8, tick_count=3, complete=True)


def test_candle_non_alignee_leve():
    # Une bougie M1 commence sur une frontière de minute. Sinon l'agrégation
    # est fausse et les fenêtres d'indicateurs glissent.
    with pytest.raises(BotError, match="aligné"):
        Candle("P_otc", TF_SEC, T0_SEC + 30, 1.0, 1.0, 1.0, 1.0, 5, True)


def test_candle_close_ts():
    c = make_candle(0, 100.0)
    assert c.close_ts_sec == T0_SEC + TF_SEC
    assert c.close_ts_ms == (T0_SEC + TF_SEC) * 1000


def test_is_usable_applique_le_critere_de_la_spec_2_4():
    assert make_candle(0, 100.0, tick_count=5, complete=True).is_usable()
    assert not make_candle(0, 100.0, tick_count=4, complete=True).is_usable()
    assert not make_candle(0, 100.0, tick_count=50, complete=False).is_usable()


def test_payout_ratio():
    assert PairInfo("EURUSD_otc", True, 92).payout_ratio == pytest.approx(0.92)


def test_payout_hors_bornes_leve():
    with pytest.raises(BotError):
        PairInfo("EURUSD_otc", True, 120)


def test_signal_sans_expiration_leve():
    # §5 : aucune valeur par défaut sur ce qui touche à l'argent.
    with pytest.raises(BotError, match="expiry_sec"):
        Signal("EURUSD_otc", Direction.CALL, T0_MS, expiry_sec=0)


def test_signal_direction_doit_etre_un_enum():
    with pytest.raises(BotError):
        Signal("EURUSD_otc", "CALL", T0_MS, expiry_sec=60)


def test_signal_features_sont_gelees():
    # Les features sont enregistrées au moment de la décision et ne doivent pas
    # pouvoir être réécrites après coup (spec §3.1).
    d = {"ma14_distance_pct": 0.4}
    s = Signal("EURUSD_otc", Direction.PUT, T0_MS, 60, features=d)
    d["ma14_distance_pct"] = 99.0
    assert s.features["ma14_distance_pct"] == 0.4
    with pytest.raises(TypeError):
        s.features["nouveau"] = 1.0


def test_signal_expires_at():
    s = Signal("EURUSD_otc", Direction.CALL, T0_MS, expiry_sec=60)
    assert s.expires_at_ms == T0_MS + 60_000
