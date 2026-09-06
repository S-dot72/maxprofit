"""
Géométrie de la bougie : corps et mèches (features de la spec §3.1).

Ces trois grandeurs décrivent la FORME de la bougie, pas son amplitude. Elles
sont donc rapportées à l'amplitude haut-bas et non au prix : une bougie de
même forme doit donner les mêmes valeurs qu'elle soit large ou étroite, sinon
la feature mesure la volatilité (déjà couverte par l'ATR) au lieu de la forme.

Les trois somment à 100 % par construction, ce qui en fait deux features
indépendantes et non trois — utile à savoir au moment de l'analyse
d'attribution (§3.2), où une condition redondante affichera un écart nul sans
que cela veuille dire qu'elle est inutile.

Aucune de ces fonctions ne regarde plus d'une bougie : elles sont causales de
manière triviale.
"""

from __future__ import annotations

from maxprofit.core.types import Candle


def _amplitude(candle: Candle) -> float | None:
    """`None` si la bougie est parfaitement plate : sa forme est indéfinie.

    Le cas n'est pas théorique. Sur une paire OTC peu volatile cotée à cinq
    décimales, une minute creuse peut n'avoir qu'un seul prix. Répondre
    « corps = 100 % » ou « corps = 0 % » serait une invention ; `None` oblige
    la stratégie à s'abstenir, ce qui est la bonne réponse.
    """
    amplitude = candle.high - candle.low
    return None if amplitude <= 0 else amplitude


def corps_pct(candle: Candle) -> float | None:
    """Part de l'amplitude occupée par le corps, de 0 à 100."""
    amplitude = _amplitude(candle)
    if amplitude is None:
        return None
    return abs(candle.close - candle.open) / amplitude * 100


def meche_haute_pct(candle: Candle) -> float | None:
    amplitude = _amplitude(candle)
    if amplitude is None:
        return None
    return (candle.high - max(candle.open, candle.close)) / amplitude * 100


def meche_basse_pct(candle: Candle) -> float | None:
    amplitude = _amplitude(candle)
    if amplitude is None:
        return None
    return (min(candle.open, candle.close) - candle.low) / amplitude * 100
