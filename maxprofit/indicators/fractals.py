"""
Fractales de Chaos (Bill Williams) — avec leur latence de confirmation.

Définition : une fractale haussière existe à l'indice i si le haut de la bougie
i dépasse strictement les hauts des deux bougies qui la précèdent ET des deux
qui la suivent. Symétriquement pour une fractale baissière sur les bas.

**Le piège.** « Des deux qui la suivent » signifie qu'à l'instant i, la fractale
n'est pas connaissable : il faut attendre i+2. Sur un graphique, la flèche est
dessinée au-dessus de la bougie i — rétroactivement, sans que rien n'indique
qu'elle est apparue deux bougies plus tard. Un backtest qui lit le tableau des
fractales tel qu'il est aujourd'hui croit donc, à l'instant i, connaître un
sommet que personne ne pouvait voir avant i+2.

C'est une des deux conditions repeignantes de la stratégie (spec §2.1), et la
plus facile à corriger : la latence est constante et vaut exactement 2.

`fractales()` ne retourne que les fractales CONFIRMÉES à la fin de la fenêtre
reçue. Les deux dernières bougies ne peuvent donc jamais porter de fractale,
quelle que soit leur forme.
"""

from __future__ import annotations

from typing import Sequence

from maxprofit.core.types import Candle
from maxprofit.indicators.base import Pivot, PivotKind, verifier_serie

#: Nombre de bougies nécessaires de chaque côté. 2 est la définition de Bill
#: Williams ; c'est aussi la latence de confirmation, en bougies.
AILE = 2


def fractales(candles: Sequence[Candle]) -> list[Pivot]:
    """Toutes les fractales confirmées à la fin de la fenêtre, dans l'ordre.

    « Confirmées » veut dire : l'indice i+2 existe dans la fenêtre reçue. La
    fonction ne peut donc pas retourner une fractale sur les deux dernières
    bougies — c'est ce que vérifie
    `test_aucune_fractale_sur_les_deux_dernieres_bougies`.
    """
    serie = verifier_serie(candles)
    trouvees: list[Pivot] = []
    # Le dernier centre possible est len-1-AILE : il faut AILE bougies après lui.
    for i in range(AILE, len(serie) - AILE):
        centre = serie[i]
        gauche = serie[i - AILE:i]
        droite = serie[i + 1:i + 1 + AILE]
        confirmation = serie[i + AILE]

        # Comparaison STRICTE : sur des paires peu volatiles à cinq décimales,
        # deux bougies voisines partagent souvent le même haut. Accepter
        # l'égalité fabriquerait des fractales en série sur un marché plat.
        if all(centre.high > c.high for c in gauche + droite):
            trouvees.append(Pivot(
                kind=PivotKind.HAUT, index=i, ts_sec=centre.ts_sec,
                price=centre.high, confirmed_index=i + AILE,
                confirmed_ts_sec=confirmation.ts_sec,
            ))
        if all(centre.low < c.low for c in gauche + droite):
            trouvees.append(Pivot(
                kind=PivotKind.BAS, index=i, ts_sec=centre.ts_sec,
                price=centre.low, confirmed_index=i + AILE,
                confirmed_ts_sec=confirmation.ts_sec,
            ))

    return trouvees


def derniere_fractale(candles: Sequence[Candle],
                      kind: PivotKind | None = None) -> Pivot | None:
    """La fractale confirmée la plus récente, éventuellement filtrée par sens.

    « La plus récente » se juge sur l'indice de l'EXTRÊME, pas sur celui de la
    confirmation : les deux ordres coïncident ici puisque la latence est
    constante, mais s'appuyer sur l'extrême rend l'intention explicite.
    """
    candidates = fractales(candles)
    if kind is not None:
        candidates = [p for p in candidates if p.kind is kind]
    return candidates[-1] if candidates else None
