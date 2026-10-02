"""Prise de liquidité au-delà d'une pique M1, retour, retournement, sens H1."""

from __future__ import annotations

from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.types import Candle, Direction
from maxprofit.strategies.prise_de_liquidite import PriseDeLiquidite

T0 = 1_790_000_000 // 3600 * 3600
NIVEAU = 1.1034


def _b(i, o, c, haut=None, bas=None):
    return Candle(pair="EURUSD_otc", tf_sec=60, ts_sec=T0 + 60 * i, open=o,
                  high=haut if haut is not None else max(o, c),
                  low=bas if bas is not None else min(o, c), close=c,
                  tick_count=10, complete=True)


def _serie(**remplacer):
    """300 bougies rouges en pente douce (l'H1 baisse) ; un sommet M1 à
    deux bougies opposées en 250-251, mèches jusqu'à 1,1034 ; puis la prise
    en 298 et le retournement en 299."""
    bougies = []
    for i in range(300):
        base = round(1.1100 - 0.0100 * i / 300, 5)
        bougies.append(_b(i, round(base + 0.00002, 5), base))
    bougies[250] = _b(250, 1.1015, 1.1030, haut=1.1034)
    bougies[251] = _b(251, 1.1029, 1.1018, haut=1.1033)
    # La manipulation : une mèche va chercher les stops au-dessus de 1,1034
    # et la bougie se referme dessous.
    bougies[298] = _b(298, 1.1004, 1.1010, haut=1.1036, bas=1.1003)
    # Le retournement : baissière, sous le plus bas de la précédente.
    bougies[299] = _b(299, 1.1010, 1.0998)
    for i, b in remplacer.items():
        bougies[int(i[1:])] = b
    return bougies


def _evaluer(bougies, strategie=None):
    return (strategie or PriseDeLiquidite()).evaluer(
        SequenceMarketView("EURUSD_otc", bougies))


def test_prise_au_dessus_du_sommet_puis_retournement_donne_une_vente():
    ev = _evaluer(_serie())
    assert ev.signal is not None
    assert ev.signal.direction is Direction.PUT
    assert ev.signal.features["niveau"] == NIVEAU


def test_sans_manipulation_pas_d_ordre():
    """Le prix ne va pas chercher les stops : pas de prise, pas d'ordre."""
    ev = _evaluer(_serie(i298=_b(298, 1.1004, 1.1010, haut=1.1020,
                                 bas=1.1003)))
    assert ev.signal is None


def test_une_liquidite_deja_prise_n_est_plus_une_prise():
    deja = _b(280, 1.1007, 1.1006, haut=1.1040)
    assert _evaluer(_serie(i280=deja)).signal is None


def test_sans_retournement_m1_pas_d_ordre():
    """La prise a lieu, mais la dernière bougie ne casse pas le plus bas de
    la précédente : les vendeurs n'ont pas encore la main."""
    faible = _b(299, 1.1010, 1.1005)
    assert _evaluer(_serie(i299=faible)).signal is None


def test_contre_la_tendance_H1_pas_d_ordre(monkeypatch):
    strategie = PriseDeLiquidite()
    monkeypatch.setattr(strategie, "_tendance_h1_haussiere", lambda b: True)
    assert _evaluer(_serie(), strategie).signal is None


def test_trop_de_clotures_au_dela_c_est_une_cassure_pas_une_manipulation():
    au_dela = {"i297": _b(297, 1.1030, 1.1037, haut=1.1038),
               "i298": _b(298, 1.1037, 1.1036, haut=1.1039, bas=1.1003)}
    serie = _serie(**au_dela)
    serie[299] = _b(299, 1.1036, 1.1000)
    assert _evaluer(serie, PriseDeLiquidite(manipulation_max=1)).signal \
        is None
    assert _evaluer(serie, PriseDeLiquidite(manipulation_max=2)).signal \
        is not None
