"""La zone tracée en H1, l'entrée confirmée en M1."""

from __future__ import annotations

from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.types import Candle, Direction
from maxprofit.strategies.zone_confirmee import ZoneConfirmee

T0 = 1_790_000_000 // 3600 * 3600
PAIRE = "AUDCHF_otc"


def _m1(i, o, c, haut=None, bas=None):
    return Candle(pair=PAIRE, tf_sec=60, ts_sec=T0 + 60 * i, open=o,
                  high=haut if haut is not None else max(o, c) + 0.00001,
                  low=bas if bas is not None else min(o, c) - 0.00001,
                  close=c, tick_count=10, complete=True)


def _heures():
    """47 heures : un va-et-vient 1,1000 / 1,1010, et une résistance H1 à
    1,1050 (heure 30 verte jusqu'à 1,1050, heure 31 rouge qui en repart)."""
    chemin = []
    for h in range(47):
        if h == 30:
            chemin.append((1.1000, 1.1050))
        elif h == 31:
            chemin.append((1.1050, 1.1000))
        else:
            chemin.append((1.1000, 1.1010) if h % 2 == 0 else (1.1010, 1.1000))
    bougies = []
    for h, (o, c) in enumerate(chemin):
        for m in range(60):
            a = o + (c - o) * m / 60
            b = o + (c - o) * (m + 1) / 60
            bougies.append(_m1(h * 60 + m, round(a, 5), round(b, 5)))
    return bougies


def _remontee_vers_la_zone():
    """L'heure en cours : le prix remonte de 1,1000 à 1,1049 en 30 min."""
    base = 47 * 60
    return [_m1(base + m, round(1.1000 + 0.0049 * m / 30, 5),
                round(1.1000 + 0.0049 * (m + 1) / 30, 5)) for m in range(30)]


def _evaluer(bougies, strategie=None):
    vue = SequenceMarketView(PAIRE, bougies)
    return (strategie or ZoneConfirmee()).evaluer(vue)


def test_resistance_H1_rejetee_et_vendeurs_aux_commandes_donne_une_vente():
    bougies = _heures() + _remontee_vers_la_zone()
    # Mèche dans la zone, clôture sous le plus bas de la précédente.
    bougies.append(_m1(47 * 60 + 30, 1.1049, 1.1041, haut=1.1051, bas=1.1040))
    ev = _evaluer(bougies)
    assert ev.signal is not None
    assert ev.signal.direction is Direction.PUT
    assert ev.signal.features["niveau"] == 1.1050


def test_le_cas_AUDCHF_les_acheteurs_ont_l_elan_donc_pas_de_vente():
    """Le prix touche la zone mais la bougie M1 est HAUSSIÈRE et casse le
    plus haut de la précédente : les acheteurs ont la main. Aucune vente."""
    bougies = _heures() + _remontee_vers_la_zone()
    bougies.append(_m1(47 * 60 + 30, 1.1049, 1.1056, haut=1.1057, bas=1.1048))
    ev = _evaluer(bougies)
    assert ev.signal is None


def test_toucher_la_zone_ne_suffit_pas_il_faut_le_retournement():
    """Bougie baissière, mais qui ne casse pas le plus bas de la précédente :
    pas de retournement écrit en M1, pas d'ordre."""
    bougies = _heures() + _remontee_vers_la_zone()
    bougies.append(_m1(47 * 60 + 30, 1.1049, 1.10485, haut=1.1051,
                       bas=1.10484))
    assert _evaluer(bougies).signal is None


def test_sans_zone_H1_aucun_ordre():
    bougies = _heures()[:31 * 60] + [
        _m1(31 * 60 + m, 1.1000, 1.1000) for m in range(16 * 60)]
    bougies += _remontee_vers_la_zone()
    bougies.append(_m1(47 * 60 + 30, 1.1049, 1.1041, haut=1.1051, bas=1.1040))
    # La seule pique est trop récente pour avoir ses trois heures de chaque
    # côté, ou absente : aucune zone, aucun ordre.
    assert _evaluer(bougies).signal is None


def test_le_cache_ne_change_aucun_verdict():
    bougies = _heures() + _remontee_vers_la_zone()
    bougies.append(_m1(47 * 60 + 30, 1.1049, 1.1041, haut=1.1051, bas=1.1040))
    partagee = ZoneConfirmee()
    for fin in range(len(bougies) - 40, len(bougies) + 1):
        avec = _evaluer(bougies[:fin], partagee).signal
        sans = _evaluer(bougies[:fin]).signal
        assert (avec is None) == (sans is None)
        if avec is not None:
            assert avec.direction is sans.direction


def test_trop_peu_d_historique_on_s_abstient():
    bougies = _heures()[-600:]
    assert _evaluer(bougies).signal is None
