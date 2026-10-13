"""P4 — le retournement après une longue série rouge."""

from __future__ import annotations

import random

import pytest

from maxprofit.apprentissage.historique import exemples_historiques
from maxprofit.apprentissage.persistance import Persistance
from maxprofit.core.errors import BotError
from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.types import Candle, Direction
from maxprofit.strategies.serie_rouge import (
    PARAMETRES_PRE_INSCRITS, Parametres, SerieRouge)

T0 = 1_791_936_000          # 2026-10-14 0 h UTC


def _bougie(i: int, couleur: str, paire: str = "EURUSD_otc") -> Candle:
    o = 1.1
    c = {"r": 1.0995, "v": 1.1005, "d": 1.1}[couleur]
    return Candle(pair=paire, tf_sec=60, ts_sec=T0 + 60 * i, open=o,
                  high=max(o, c) + 0.0001, low=min(o, c) - 0.0001, close=c,
                  tick_count=20, complete=True)


def _serie(motif: str, sauter: tuple[int, ...] = ()) -> list[Candle]:
    return [_bougie(i, c) for i, c in enumerate(motif) if i not in sauter]


def _signal(bougies: list[Candle]):
    return SerieRouge().on_bar(SequenceMarketView("EURUSD_otc", bougies))


def test_les_parametres_pre_inscrits_sont_figes():
    assert PARAMETRES_PRE_INSCRITS == Parametres(n_min=8, expiry_sec=120)
    assert SerieRouge().params == {"n_min": 8, "expiry_sec": 120}


def test_huit_rouges_donnent_un_call_de_deux_minutes():
    s = _signal(_serie("v" + "r" * 8))
    assert s is not None and s.direction is Direction.CALL
    assert s.expiry_sec == 120 and s.features["rouges"] == 8.0


def test_chaque_rouge_au_dela_de_la_huitieme_emet_aussi():
    assert _signal(_serie("r" * 11)) is not None


def test_sept_rouges_ne_suffisent_pas():
    assert _signal(_serie("v" + "r" * 7)) is None


def test_une_verte_ou_un_doji_interrompt_la_serie():
    assert _signal(_serie("r" * 4 + "v" + "r" * 4)) is None
    assert _signal(_serie("r" * 4 + "d" + "r" * 4)) is None


def test_une_minute_absente_interrompt_la_serie():
    assert _signal(_serie("r" * 9, sauter=(4,))) is None


def test_parametres_invalides():
    with pytest.raises(BotError):
        Parametres(n_min=0, expiry_sec=120)
    with pytest.raises(BotError):
        Parametres(n_min=8, expiry_sec=90)


def test_le_rejeu_retrouve_exactement_la_mesure_de_persistance():
    """La stratégie émet sur les mêmes bougies que la case « 8 rouges ou
    plus, 2 min » de la persistance, et gagne exactement ses retournements :
    ce qui est jugé est bien ce qui a été mesuré."""
    rng = random.Random(3)
    prix, bougies = 1.1, []
    for i in range(5 * 1440):
        o = prix
        prix *= 1 + rng.gauss(-0.0002 if (i // 50) % 2 else 0.0002, 0.0006)
        if i in (1000, 1001):
            continue
        bougies.append(Candle(
            pair="X_otc", tf_sec=60, ts_sec=T0 + 60 * i, open=o,
            high=max(o, prix) * 1.0001, low=min(o, prix) * 0.9999,
            close=prix, tick_count=30, complete=True))
    exemples = exemples_historiques(
        {"X_otc": bougies}, SerieRouge(), echeance_sec=120,
        echeances=(120,), avec_contexte=False)
    mesure = Persistance(T0 + 10 ** 9, echeances=(120,))
    mesure.ajouter(bougies)
    cas, continuations = mesure.resultat()["rouge"]["8"]["120"][:2]
    assert len(exemples) == cas > 50
    assert sum(e.gagne for e in exemples) == cas - continuations
