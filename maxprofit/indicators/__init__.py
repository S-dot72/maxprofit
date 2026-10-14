"""
Indicateurs — tous causaux, tous fonctions pures d'une fenêtre de bougies.

Règle unique de ce paquet, et elle explique toutes les décisions qui suivent :

    la valeur d'un indicateur à l'instant t ne dépend QUE des bougies
    clôturées jusqu'à t inclus, et ne change JAMAIS quand des bougies
    postérieures arrivent.

C'est la propriété que `tests/test_causalite.py` vérifie mécaniquement sur
chaque fonction exportée ici, en rejouant la même série tronquée à chaque
instant. Un indicateur qui la viole « repeint » : il affiche aujourd'hui une
valeur qu'il n'aurait pas pu afficher à l'époque, et le backtest qui s'appuie
dessus produit un résultat spectaculaire et entièrement faux (spec §2.1).

Deux indicateurs de la stratégie sont nativement repeignants et reçoivent donc
un traitement explicite :

- **ZigZag** : un pivot n'est confirmé qu'après un retournement d'amplitude
  suffisante, donc plusieurs bougies plus tard.
- **Fractales de Chaos** : une fractale à l'indice i n'est confirmée qu'à i+2.

Les deux exposent, pour chaque pivot, l'indice où l'extrême s'est produit ET
l'indice où on l'a su. La stratégie ne voit que les pivots déjà confirmés.
"""

from maxprofit.indicators.base import Pivot, PivotKind
from maxprofit.indicators.fractals import derniere_fractale, fractales
from maxprofit.indicators.geometry import corps_pct, meche_basse_pct, meche_haute_pct
from maxprofit.indicators.oscillators import (
    Macd,
    atr,
    atr_normalise_pct,
    besoin_macd,
    besoin_stochastique_lente,
    bollinger_percent_b,
    distance_ma_pct,
    ema,
    macd,
    rsi,
    sma,
    stochastique,
    stochastique_lente,
)
from maxprofit.indicators.zigzag import ZigZag, dernier_pivot, distance_pivot_pct, zigzag

__all__ = [
    "Macd",
    "Pivot",
    "PivotKind",
    "ZigZag",
    "atr",
    "atr_normalise_pct",
    "besoin_macd",
    "besoin_stochastique_lente",
    "bollinger_percent_b",
    "corps_pct",
    "dernier_pivot",
    "derniere_fractale",
    "distance_ma_pct",
    "distance_pivot_pct",
    "ema",
    "fractales",
    "macd",
    "meche_basse_pct",
    "meche_haute_pct",
    "rsi",
    "sma",
    "stochastique",
    "stochastique_lente",
    "zigzag",
]
