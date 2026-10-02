"""
Les zones hautes et basses tracées par le ZigZag.

Demandé le 2026-10-02 : « utilise ZigZag en support pour identifier les zones
hautes et basses ». Jusqu'ici, un sommet ou un creux était une pique à deux
bougies opposées, extrême des corps sur ±12 bougies : une règle locale, qui
marque aussi bien un vrai sommet de marché qu'un accident de deux minutes.

Le ZigZag ne retient un sommet que si le prix en redescend d'au moins un seuil
— ici quatre fois l'amplitude M1 médiane de la dernière heure, soit environ
huit sommets et creux par tranche de cinq heures : les « vagues » qu'un trader
tracerait à l'œil. Le seuil suit la volatilité de chaque paire, au lieu d'un
pourcentage fixe qui serait trop grand sur l'une et trop petit sur l'autre.

⚠ Le ZigZag est un indicateur qui REPEINT sur les plateformes : le dernier
sommet dessiné n'est souvent pas encore acquis. Ici, seuls les pivots
CONFIRMÉS servent (`maxprofit.indicators.zigzag`) : un sommet n'existe qu'à
partir de la bougie où le retournement l'a établi, jamais avant.

Deux stratégies en découlent, jugées toutes les deux au laboratoire :

- `ZoneZigZag` : ZoneH1 à l'identique, mais ses zones sont les sommets et
  creux du ZigZag (sur les corps, comme ZoneH1).
- `PriseZigZag` : la prise de liquidité, au-delà des MÈCHES des sommets et
  creux du ZigZag.
"""

from __future__ import annotations

from maxprofit.indicators.base import PivotKind
from maxprofit.indicators.zigzag import zigzag
from maxprofit.strategies.prise_de_liquidite import PriseDeLiquidite
from maxprofit.strategies.zone_h1 import ZoneH1

#: Seuil du ZigZag, en multiples de l'amplitude M1 médiane.
MULTIPLE_AMPLITUDE = 4.0
#: Bougies sur lesquelles cette amplitude est mesurée.
FENETRE_AMPLITUDE = 60


def seuil_pct(bougies) -> float:
    """Le retournement minimal d'un pivot, en % du prix."""
    recentes = bougies[-FENETRE_AMPLITUDE:]
    amplitudes = sorted(100 * (b.high - b.low) / b.close
                        for b in recentes if b.close)
    mediane = amplitudes[len(amplitudes) // 2] if amplitudes else 0.0
    return max(mediane * MULTIPLE_AMPLITUDE, 1e-6)


def pivots(bougies):
    return zigzag(bougies, seuil_pct(bougies))


class ZoneZigZag(ZoneH1):
    """ZoneH1, zones = sommets et creux confirmés du ZigZag."""

    name = "zone_zigzag_m1"

    def _zones(self, bougies) -> list[tuple[int, float, int]]:
        sortie = []
        for p in pivots(bougies):
            c = bougies[p.index]
            if p.kind is PivotKind.HAUT:
                sortie.append((p.confirmed_index, max(c.open, c.close), -1))
            else:
                sortie.append((p.confirmed_index, min(c.open, c.close), +1))
        return sortie


class PriseZigZag(PriseDeLiquidite):
    """Prise de liquidité au-delà des sommets et creux du ZigZag."""

    name = "prise_de_liquidite_zigzag"

    def _liquidites(self, bougies) -> list[tuple[int, float, int]]:
        return [(p.confirmed_index, p.price,
                 -1 if p.kind is PivotKind.HAUT else +1)
                for p in pivots(bougies)]
