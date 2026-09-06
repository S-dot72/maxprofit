"""
Strategy — l'invariant n°1 de la spec (§0).

« Le code de stratégie est identique en backtest et en live. Une seule classe
Strategy, une seule méthode on_bar(view) -> Signal | None. Deux implémentations
divergeront, toujours, et le backtest deviendra un mensonge. »

Cet invariant n'est pas une consigne, il est vérifié mécaniquement :
tests/test_layering.py refuse toute sous-classe de `Strategy` définie ailleurs
que dans `maxprofit/strategies/`, et refuse que `maxprofit.live` ou
`maxprofit.backtest` définissent la moindre logique de décision. Les deux
moteurs importent le même objet ; il n'y a pas de second endroit où diverger.

Contrat que doit respecter toute implémentation :

1. `on_bar` est PURE vis-à-vis de la vue. Aucune lecture de l'horloge murale
   (`time.time()`, `datetime.now()`), aucun accès réseau ou disque, aucun
   aléatoire non graine. Le test-oracle de déterminisme (spec §2.7.5) exige que
   deux exécutions identiques produisent des résultats identiques au bit près ;
   une seule de ces impuretés le fait échouer.
2. Tout état interne accumulé d'une bougie à l'autre est remis à zéro par
   `reset()`. Un état qui survit entre deux backtests fait fuiter de
   l'information d'une fenêtre de walk-forward à la suivante.
3. `params` décrit intégralement la configuration de l'instance. C'est ce qui
   est enregistré dans la table `experiments` (spec §2.6) : un backtest qu'on
   ne peut pas rejouer à l'identique ne compte pas comme une expérience.
4. Aucune valeur par défaut sur ce qui touche à l'argent : expiration, seuils,
   marge. Absence de configuration = arrêt (spec §5).
"""

from __future__ import annotations

import abc
from typing import Any, Mapping

from maxprofit.core.market_view import MarketView
from maxprofit.core.types import Signal


class Strategy(abc.ABC):
    """Base de toute stratégie. Une seule méthode compte : `on_bar`."""

    #: Nom stable, enregistré avec chaque expérience. Le changer casse la
    #: traçabilité : préférer une nouvelle classe.
    name: str = ""

    @property
    @abc.abstractmethod
    def params(self) -> Mapping[str, Any]:
        """Paramètres complets de l'instance, sérialisables en JSON.

        Doit suffire, avec le commit git et le hash du jeu de données, à
        reproduire le backtest à l'identique (spec §2.6).
        """

    @abc.abstractmethod
    def on_bar(self, view: MarketView) -> Signal | None:
        """Décide à la clôture d'une bougie.

        `view` n'expose que l'historique jusqu'à `view.now_ms` inclus. Il n'y a
        pas d'accès au futur à contourner : il n'existe pas.

        Retourne `None` pour ne rien faire — ce qui est le cas de la très
        grande majorité des bougies. Retourner `None` n'est pas une absence
        d'information : la couche de journalisation enregistre l'évaluation
        quand même, avec les features et la condition bloquante (spec §3.1).

        Si un `Signal` est retourné, son `decided_at_ms` DOIT valoir
        `view.now_ms` — le moteur le vérifie et rejette le signal sinon.
        """

    def reset(self) -> None:
        """Remet l'état interne à zéro. Appelée par le moteur avant chaque
        exécution et entre deux fenêtres de walk-forward. Par défaut : rien à
        faire, ce qui n'est correct que pour une stratégie sans état."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} params={dict(self.params)!r}>"
