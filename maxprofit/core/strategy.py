"""
Strategy — l'invariant n°1 de la spec (§0).

« Le code de stratégie est identique en backtest et en live. Une seule classe
Strategy, une seule méthode on_bar(view) -> Signal | None. Deux implémentations
divergeront, toujours, et le backtest deviendra un mensonge. »

`on_bar(view) -> Signal | None` reste la méthode que les deux moteurs
appellent, avec la même signature. Elle est devenue CONCRÈTE et dérive de
`evaluer`, qui retourne le compte rendu complet exigé par le §3.1. Voir la
docstring de `on_bar` pour le raisonnement : c'est le même invariant,
appliqué au couple décision/journal au lieu du couple backtest/live.

Cet invariant n'est pas une consigne, il est vérifié mécaniquement :
tests/test_layering.py refuse toute sous-classe de `Strategy` définie ailleurs
que dans `maxprofit/strategies/`, et refuse que `maxprofit.live` ou
`maxprofit.backtest` définissent la moindre logique de décision. Les deux
moteurs importent le même objet ; il n'y a pas de second endroit où diverger.

Contrat que doit respecter toute implémentation :

1. `evaluer` est PURE vis-à-vis de la vue. Aucune lecture de l'horloge murale
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
from maxprofit.core.types import Evaluation, Signal


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
    def evaluer(self, view: MarketView) -> Evaluation:
        """Évalue une bougie et retourne le compte rendu COMPLET.

        C'est la méthode que l'on implémente. Elle retourne une `Evaluation`
        même quand aucun signal n'est émis — ce qui est le cas de la très
        grande majorité des bougies — avec les features observées, l'état de
        chaque condition et la direction envisagée.

        `view` n'expose que l'historique jusqu'à `view.now_ms` inclus. Il n'y a
        pas d'accès au futur à contourner : il n'existe pas.

        Si un `Signal` est produit, son `decided_at_ms` DOIT valoir
        `view.now_ms` — le moteur le vérifie et rejette le signal sinon.
        """

    def on_bar(self, view: MarketView) -> Signal | None:
        """Le contrat de la spec §0, désormais DÉRIVÉ de `evaluer`.

        C'est toujours cette méthode que les deux moteurs appellent, et elle a
        toujours la même signature. Ce qui change : elle n'est plus
        implémentable séparément.

        Pourquoi. Le §3.1 demande d'enregistrer, à chaque bougie, les features
        et la condition bloquante — donc quelqu'un doit les calculer. Si
        `on_bar` restait la seule méthode, le journal devrait les recalculer de
        son côté, et l'on aurait deux implémentations de la même logique. C'est
        exactement ce que l'invariant n°1 interdit, appliqué non plus au couple
        backtest/live mais au couple décision/journal : elles divergeraient, et
        l'analyse d'attribution porterait alors sur des conditions qui ne sont
        pas celles qui ont décidé.

        Rendre `on_bar` concrète garantit qu'il n'existe qu'un seul calcul. Le
        signal enregistré est littéralement celui qui est émis.
        """
        return self.evaluer(view).signal

    def reset(self) -> None:
        """Remet l'état interne à zéro. Appelée par le moteur avant chaque
        exécution et entre deux fenêtres de walk-forward. Par défaut : rien à
        faire, ce qui n'est correct que pour une stratégie sans état."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} params={dict(self.params)!r}>"
