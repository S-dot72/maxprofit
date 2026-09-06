"""
Noyau : contrats partagés par les trois couches.

Règle de dépendance, vérifiée par tests/test_layering.py :
`maxprofit.core` n'importe RIEN des couches Collecte, Backtest, Live ni des
stratégies. C'est ce qui garantit qu'il n'existe qu'une seule définition de
`Candle`, `Signal`, `MarketView` et `Strategy` dans tout le système.
"""

from maxprofit.core.errors import (
    BotError,
    ConfigurationError,
    LayerViolation,
    LookAheadError,
    TimebaseError,
)
from maxprofit.core.market_view import MarketView, SequenceMarketView, assert_no_look_ahead
from maxprofit.core.strategy import Strategy
from maxprofit.core.types import Candle, Direction, PairInfo, Signal, Tick

__all__ = [
    "BotError",
    "Candle",
    "ConfigurationError",
    "Direction",
    "LayerViolation",
    "LookAheadError",
    "MarketView",
    "PairInfo",
    "SequenceMarketView",
    "Signal",
    "Strategy",
    "Tick",
    "TimebaseError",
    "assert_no_look_ahead",
]
