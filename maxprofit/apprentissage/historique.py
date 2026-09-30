"""
Les exemples : la stratégie rejouée sur l'historique, avec contexte et issue.

Même évaluation que la course — fenêtre de `lookback` minutes, bougies closes,
garde de calibration, puis la stratégie — et même issue que le rejeu du plan :
entrée à la clôture de la bougie de signal, sortie à la clôture de la bougie
qui finit à l'échéance. Une égalité n'est ni gagnée ni perdue : elle n'est
pas un exemple.
"""

from __future__ import annotations

import bisect
from typing import Callable, Mapping, Sequence

from maxprofit.apprentissage.contexte import contexte
from maxprofit.apprentissage.lecons import ECHEANCES_COMPAREES, Exemple
from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.types import Candle, Direction


def exemples_historiques(
        bougies_par_paire: Mapping[str, Sequence[Candle]], strategie, *,
        echeance_sec: int,
        garde: Callable[[str, Sequence[Candle]], bool] = lambda p, f: True,
        respirer: Callable[[], None] | None = None,
        echeances: Sequence[int] = ECHEANCES_COMPAREES) -> list[Exemple]:
    """Chaque signal que la stratégie aurait émis, avec son contexte.

    `respirer`, appelée régulièrement, laisse la main aux autres fils du
    processus : le rejeu tourne à côté de la collecte et de la course.
    """
    lookback = strategie.p.lookback
    sortie: list[Exemple] = []
    evaluees = 0
    for pair, serie in bougies_par_paire.items():
        completes = sorted((b for b in serie if b.complete),
                           key=lambda b: b.ts_sec)
        debuts = [b.ts_sec for b in completes]
        closes = {b.ts_sec: b.close for b in completes}
        for i, bougie in enumerate(completes):
            j = bisect.bisect_left(debuts, bougie.ts_sec - lookback * 60)
            if i + 1 - j < lookback:
                continue                  # la stratégie refuserait de trancher
            fenetre = completes[j:i + 1]
            if not garde(pair, fenetre):
                continue
            evaluees += 1
            if respirer is not None and evaluees % 200 == 0:
                respirer()
            signal = strategie.on_bar(SequenceMarketView(pair, fenetre))
            if signal is None:
                continue
            sortie_prix = closes.get(bougie.ts_sec + echeance_sec)
            if sortie_prix is None or sortie_prix == bougie.close:
                continue
            call = signal.direction is Direction.CALL
            # Le MÊME signal jugé à d'autres échéances : c'est ce qui rend
            # la comparaison juste — mêmes entrées, seule la sortie change.
            issues = {}
            for sec in echeances:
                prix = closes.get(bougie.ts_sec + sec)
                if prix is not None and prix != bougie.close:
                    issues[sec] = (prix > bougie.close) == call
            sortie.append(Exemple(
                ts_sec=bougie.ts_sec + 60, pair=pair,
                contexte=contexte(fenetre, call, signal.features),
                gagne=(sortie_prix > bougie.close) == call, issues=issues))
    sortie.sort(key=lambda e: e.ts_sec)
    return sortie
