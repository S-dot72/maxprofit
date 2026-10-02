"""Le support cassé devient résistance : le cas USD/JPY du 02/10."""

from __future__ import annotations

from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.types import Candle, Direction
from maxprofit.strategies.zone_h1 import ZoneH1
from maxprofit.strategies.zone_inversee import ZoneInversee

T0 = 1_790_000_000 // 3600 * 3600
NIVEAU = 156.400


def _b(i, o, c, haut=None, bas=None):
    return Candle(pair="USDJPY_otc", tf_sec=60, ts_sec=T0 + 60 * i, open=o,
                  high=haut if haut is not None else max(o, c) + 0.002,
                  low=bas if bas is not None else min(o, c) - 0.002,
                  close=c, tick_count=10, complete=True)


def _serie():
    """L'H1 baisse ; un support à 156,400 (creux 150-151), cassé en 200,
    retesté par en dessous en 280 puis touché et rejeté en 299."""
    bougies = []
    for i in range(300):
        prix = round(156.700 - 0.300 * i / 300, 3)
        bougies.append(_b(i, prix + 0.001, prix))
    for i in range(130, 200):
        bougies[i] = _b(i, 156.430, 156.420)            # au-dessus du support
    bougies[150] = _b(150, 156.420, NIVEAU)               # rouge : le creux
    bougies[151] = _b(151, 156.401, 156.425)              # verte : le rebond
    for i in range(200, 299):
        bougies[i] = _b(i, 156.350, 156.340)            # cassé : en dessous
    bougies[200] = _b(200, 156.420, 156.340)              # la cassure
    bougies[280] = _b(280, 156.350, 156.398, haut=156.401)  # 1er retest
    bougies[281] = _b(281, 156.398, 156.345)
    # Le retour sur l'ancien support, refermé dessous.
    bougies[299] = _b(299, 156.360, 156.392, haut=156.401)
    return bougies


def test_zoneh1_oublie_le_support_casse():
    vue = SequenceMarketView("USDJPY_otc", _serie())
    assert ZoneH1().evaluer(vue).signal is None


def test_le_support_casse_devient_une_resistance_a_vendre():
    vue = SequenceMarketView("USDJPY_otc", _serie())
    signal = ZoneInversee().evaluer(vue).signal
    assert signal is not None
    assert signal.direction is Direction.PUT
    assert signal.features["niveau"] == NIVEAU
    assert signal.features["inversee"] == 1.0
    assert "devenu résistance" in signal.reason


def test_un_niveau_inverse_recasse_est_mort():
    serie = _serie()
    serie[290] = _b(290, 156.390, 156.440)              # recassé par le haut
    vue = SequenceMarketView("USDJPY_otc", serie)
    signal = ZoneInversee().evaluer(vue).signal
    assert signal is None or signal.features.get("niveau") != NIVEAU
