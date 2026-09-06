"""
Vocabulaire commun aux indicateurs de pivots.

Un `Pivot` porte DEUX indices, et c'est toute la différence entre un backtest
honnête et un backtest flatteur :

    index            l'indice où l'extrême s'est produit
    confirmed_index  l'indice où l'on a pu le SAVOIR

Sur un graphique, seul le premier est dessiné — le pivot apparaît rétroactivement
au bon endroit, et rien ne signale qu'il n'était pas là sur le moment. C'est
exactement ce qui rend le repeint invisible à la relecture.

`latence` (la différence entre les deux) est la mesure directe de ce que
coûterait l'illusion : c'est le nombre de bougies pendant lesquelles un backtest
naïf « connaîtrait » un pivot qui n'existait pas encore.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from maxprofit.core.errors import BotError
from maxprofit.core.types import Candle


class PivotKind(enum.Enum):
    HAUT = "HAUT"   # sommet local
    BAS = "BAS"     # creux local

    @property
    def opposite(self) -> "PivotKind":
        return PivotKind.BAS if self is PivotKind.HAUT else PivotKind.HAUT

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Pivot:
    kind: PivotKind
    index: int              # où l'extrême s'est produit
    ts_sec: int             # début de la bougie de l'extrême
    price: float            # le haut (HAUT) ou le bas (BAS) de cette bougie
    confirmed_index: int    # où on a pu le savoir
    confirmed_ts_sec: int

    def __post_init__(self) -> None:
        if self.confirmed_index < self.index:
            raise BotError(
                f"Pivot confirmé ({self.confirmed_index}) avant de s'être "
                f"produit ({self.index}) : c'est la définition du look-ahead."
            )
        if self.index < 0:
            raise BotError(f"Pivot d'indice négatif : {self.index}")

    @property
    def latence(self) -> int:
        """Nombre de bougies entre l'extrême et sa confirmation. Zéro
        signifierait un indicateur non repeignant — aucun pivot n'a cette
        propriété, par construction."""
        return self.confirmed_index - self.index

    def visible_a(self, index: int) -> bool:
        """Ce pivot était-il connaissable à l'instant `index` ?"""
        return self.confirmed_index <= index


def verifier_serie(candles) -> tuple[Candle, ...]:
    """Contrôles communs à tous les indicateurs.

    Une série mal ordonnée produit des indicateurs faux sans rien lever : les
    fenêtres glissent, les extrêmes se calculent sur des bougies qui ne se
    suivent pas. On vérifie une fois, ici, plutôt que d'espérer.
    """
    serie = tuple(candles)
    if not serie:
        return serie
    pair = serie[0].pair
    tf_sec = serie[0].tf_sec
    precedent = None
    for c in serie:
        if c.pair != pair:
            raise BotError(
                f"Série mélangeant {pair} et {c.pair} : un indicateur porte sur "
                f"une seule paire."
            )
        if c.tf_sec != tf_sec:
            raise BotError(f"Série mélangeant les timeframes {tf_sec} et {c.tf_sec}")
        if precedent is not None and c.ts_sec <= precedent:
            raise BotError(
                f"Série non strictement croissante ({precedent} puis {c.ts_sec})"
            )
        precedent = c.ts_sec
    return serie


def verifier_periode(periode: int, minimum: int = 1) -> int:
    if isinstance(periode, bool) or not isinstance(periode, int):
        raise BotError(f"Période : attendu un int, reçu {type(periode).__name__}")
    if periode < minimum:
        raise BotError(f"Période doit valoir au moins {minimum}, reçu {periode}")
    return periode
