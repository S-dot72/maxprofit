"""Les zones hautes et basses du ZigZag : seuls les pivots CONFIRMÉS."""

from __future__ import annotations

from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.types import Candle
from maxprofit.indicators.base import PivotKind
from maxprofit.strategies.zones_zigzag import (
    PriseZigZag, ZoneZigZag, pivots, seuil_pct)

T0 = 1_790_000_000 // 3600 * 3600


def _b(i, o, c, haut=None, bas=None):
    return Candle(pair="EURUSD_otc", tf_sec=60, ts_sec=T0 + 60 * i, open=o,
                  high=haut if haut is not None else max(o, c) + 0.00002,
                  low=bas if bas is not None else min(o, c) - 0.00002,
                  close=c, tick_count=10, complete=True)


def _vagues(n=300, amplitude=0.0030, periode=60):
    """Des vagues régulières : un sommet et un creux toutes les 30 min."""
    import math
    serie = []
    for i in range(n):
        a = 1.1 + amplitude * math.sin(2 * math.pi * i / periode)
        b = 1.1 + amplitude * math.sin(2 * math.pi * (i + 1) / periode)
        serie.append(_b(i, round(a, 5), round(b, 5)))
    return serie


def test_le_seuil_suit_la_volatilite_de_la_paire():
    calme = [_b(i, 1.1, 1.1, haut=1.10002, bas=1.09998) for i in range(60)]
    agite = [_b(i, 1.1, 1.1, haut=1.1010, bas=1.0990) for i in range(60)]
    assert seuil_pct(agite) > 10 * seuil_pct(calme)


def test_les_vagues_donnent_des_sommets_et_des_creux_alternes():
    p = pivots(_vagues())
    assert len(p) >= 8
    assert all(a.kind is not b.kind for a, b in zip(p, p[1:]))


def test_un_pivot_n_existe_qu_une_fois_confirme():
    serie = _vagues()
    tous = pivots(serie)
    dernier = tous[-1]
    tronquee = serie[:dernier.confirmed_index]
    assert all(p.index != dernier.index or p.kind is not dernier.kind
               for p in pivots(tronquee)), "pas avant sa confirmation"


def test_les_zones_sont_sur_les_corps_et_la_liquidite_sur_les_meches():
    serie = _vagues()
    zones = ZoneZigZag()._zones(serie)
    liquidites = PriseZigZag()._liquidites(serie)
    assert len(zones) == len(liquidites) >= 8
    for (conf_z, niveau_z, s_z), (conf_l, niveau_l, s_l) in zip(zones,
                                                               liquidites):
        assert conf_z == conf_l and s_z == s_l
        # La mèche est au-delà du corps.
        assert (niveau_l >= niveau_z) if s_z < 0 else (niveau_l <= niveau_z)


def test_les_deux_strategies_evaluent_sans_erreur():
    vue = SequenceMarketView("EURUSD_otc", _vagues())
    for s in (ZoneZigZag(), PriseZigZag()):
        ev = s.evaluer(vue)
        assert ev.pair == "EURUSD_otc"
    assert {p.kind for p in pivots(_vagues())} == {PivotKind.HAUT,
                                                   PivotKind.BAS}


def test_le_rythme_mesure_la_duree_des_vagues():
    from maxprofit.strategies.zones_zigzag import rythme_minutes
    lentes = rythme_minutes(_vagues(periode=60))
    rapides = rythme_minutes(_vagues(periode=12))
    assert lentes is not None and rapides is not None
    assert 25 <= lentes <= 35, "demi-période de 60 min"
    assert rapides < 15, "des vagues de 6 min ne tiennent pas 15 min"
