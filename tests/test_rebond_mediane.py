"""Le rebond sur la médiane de Bollinger, règle par règle — telles que
l'utilisateur les a données, capture d'écran à l'appui."""

from __future__ import annotations

from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.types import Candle, Direction
from maxprofit.strategies.rebond_mediane import (
    RebondMediane, _mediane_et_sigma)

T0 = 1_790_000_040


def _c(i, o, cl, bas=None):
    return Candle(pair="GBPAUD_otc", tf_sec=60, ts_sec=T0 + 60 * i, open=o,
                  high=max(o, cl) + 0.0002,
                  low=(min(o, cl) - 0.0002) if bas is None else bas,
                  close=cl, tick_count=30, complete=True)


def _figure(pas=0.0004, cloture_rouge=None, rouges=2, verte=True):
    """Montée, sommet, `rouges` bougies rouges dont la dernière vient
    toucher la médiane, puis une verte (ou une rouge si `verte` est faux)."""
    b, p = [], 1.9000
    for i in range(60):
        o = p
        p = p + pas + (0.0003 if i % 3 else -0.0002)
        b.append(_c(i, o, p))
    m, _ = _mediane_et_sigma(tuple(b), len(b) - 1, 20)
    for k in range(rouges):
        haut = b[-1].close
        m, _ = _mediane_et_sigma(tuple(b + [_c(len(b), haut, haut)]),
                                 len(b), 20)
        derniere = k == rouges - 1
        cl = (cloture_rouge if derniere and cloture_rouge is not None
              else haut - (haut - m) * (0.9 if derniere else 0.5))
        b.append(_c(len(b), haut, cl,
                    bas=min(m, cl) if derniere else None))
    o = b[-1].close
    b.append(_c(len(b), o, o + (0.0010 if verte else -0.0010)))
    return b


def _evaluer(bougies):
    return RebondMediane().evaluer(SequenceMarketView("GBPAUD_otc", bougies))


def test_le_setup_de_la_capture_donne_un_CALL_d_une_minute():
    e = _evaluer(_figure())
    assert e.signal is not None
    assert e.signal.direction is Direction.CALL
    assert e.signal.expiry_sec == 60


def test_une_seule_rouge_suffit_aussi():
    assert _evaluer(_figure(rouges=1)).signal is not None


def test_trois_rouges_ce_n_est_plus_le_setup():
    """« Si c'est seulement 2 bougies, le setup reste valable. »"""
    assert _evaluer(_figure(rouges=3)).signal is None


def test_une_rouge_qui_CLOTURE_sous_la_mediane_casse_le_setup():
    b = _figure()
    m, _ = _mediane_et_sigma(tuple(b), len(b) - 2, 20)
    casse = _figure(cloture_rouge=m - 0.0005)
    e = _evaluer(casse)
    assert e.signal is None
    assert not dict((c.nom, c.validee) for c in e.conditions)[
        "repli_sans_cassure_de_la_mediane"]


def test_sans_bougie_verte_on_attend():
    assert _evaluer(_figure(verte=False)).signal is None


def test_jamais_contre_la_tendance():
    """La position perdue sur GBP/AUD : prise contre une tendance haussière.
    Ici, le miroir : en baisse, pas d'achat — et jamais de vente."""
    bougies, prix = [], 1.9000
    for i in range(62):
        o = prix
        prix = prix - 0.0006 + (0.0003 if i % 3 else -0.0002)
        bougies.append(_c(i, o, prix))
    bougies.append(_c(62, prix, prix + 0.0004))       # une verte, en baisse
    e = _evaluer(bougies)
    assert e.signal is None
    assert e.direction_envisagee is Direction.CALL


def test_trop_peu_d_historique():
    assert _evaluer(_figure()[-30:]).signal is None
