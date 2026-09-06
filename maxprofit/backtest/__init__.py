"""
Couche Backtest : rejoue l'historique et mesure. N'écrit JAMAIS dans les tables
de marché (`ticks`, `candles`, `payouts`, `uptime`) — elle n'y a accès qu'en
lecture. Ses écritures vont dans `experiments`, `evaluations`, `outcomes`.

Le moteur (étape 4) ne cherche pas à être juste — on ne prouve pas la justesse
d'un moteur de backtest. Il cherche à échouer bruyamment sur les erreurs
classiques plutôt qu'à produire silencieusement des résultats flatteurs. Ce
qu'il refuse : un signal mal daté, un signal sur des données douteuses, un
payout d'aujourd'hui appliqué à un trade d'hier, un trade irrésolvable compté
comme perdant.

Sa crédibilité repose sur les cinq tests-oracles du §2.7
(`tests/test_oracles.py`), pas sur sa relecture.
"""

from maxprofit.backtest.engine import BacktestEngine, MotifExclusion, Rapport
from maxprofit.backtest.execution import (
    ExecutionConfig,
    MotifIrresolu,
    PayoutsEnMemoire,
    RegleEgalite,
    Resultat,
    TicksEnMemoire,
    Trade,
    executer,
)

__all__ = [
    "BacktestEngine",
    "ExecutionConfig",
    "MotifExclusion",
    "MotifIrresolu",
    "PayoutsEnMemoire",
    "Rapport",
    "RegleEgalite",
    "Resultat",
    "TicksEnMemoire",
    "Trade",
    "executer",
]
