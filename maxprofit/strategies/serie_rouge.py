"""
Le retournement après une longue série rouge — l'hypothèse P4 pré-inscrite.

--- D'où elle vient --------------------------------------------------------

De la mesure de persistance du laboratoire (`apprentissage.persistance`),
lue le 2026-10-13 : sur toutes les bougies M1 rejouées, après 8 bougies
rouges consécutives ou plus, la position CONTRE la série jugée à 2 minutes
gagnait 55,4 % — 56,4 % sur l'étalonnage, 54,6 % sur la période récente —
sur environ 600 cas, soit une vingtaine de signaux par jour sur vingt paires,
sans aucune zone.

--- ⚠ Ce qu'elle vaut, et ce qu'elle ne vaut pas --------------------------

**Une candidate, pas une découverte.**

- La persistance a fait 112 comparaisons (deux couleurs, huit longueurs,
  sept échéances) : environ six passent le seuil par hasard.
- Elle n'est qu'à 1,6 écart-type au-dessus du seuil de rentabilité de
  52,1 %.
- Rien d'équivalent après 8 VERTES : l'asymétrie n'a pas d'explication, et
  un effet sans mécanisme est le portrait d'un faux positif.

C'est pourquoi elle est pré-inscrite (`live.preinscriptions`, P4) avec ses
paramètres figés AVANT la fenêtre aveugle, et jugée sur cette seule fenêtre.
Ce fichier est ce qui sera exécuté contre elle : la changer après avoir vu
le résultat en ferait une autre hypothèse.

--- La règle --------------------------------------------------------------

À la clôture d'une bougie rouge (clôture strictement sous l'ouverture) qui
termine une série d'au moins `n_min` rouges, CALL pour `expiry_sec`. Chaque
rouge de la série au-delà de la 8e émet aussi — exactement comme la mesure,
qui comptait « 8 ou plus » ; l'indépendance des ordres est l'affaire du
plan, pas de la stratégie.

Comme dans la mesure : une bougie sans corps interrompt la série, une minute
absente aussi (les bougies doivent se suivre minute après minute).

--- Causalité ----------------------------------------------------------------

Tout se lit sur des bougies CLOSES : la série est connue à la clôture de sa
dernière bougie, c'est l'instant où la course décide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from maxprofit.core.errors import BotError
from maxprofit.core.market_view import MarketView
from maxprofit.core.strategy import Strategy
from maxprofit.core.types import (
    ConditionResult,
    Direction,
    Evaluation,
    Signal,
)


@dataclass(frozen=True)
class Parametres:
    """Aucun défaut (§5) : tout ce qui touche à l'argent est fourni."""

    #: Rouges consécutives à partir desquelles on joue le retournement.
    n_min: int
    expiry_sec: int

    def __post_init__(self) -> None:
        if self.n_min < 1:
            raise BotError(f"n_min invalide : {self.n_min}")
        if self.expiry_sec <= 0 or self.expiry_sec % 60:
            raise BotError(f"expiry_sec invalide : {self.expiry_sec}")

    @property
    def lookback(self) -> int:
        return self.n_min


#: Les paramètres pré-inscrits le 2026-10-13 (P4). Ne pas les modifier :
#: une autre valeur est une autre hypothèse, à pré-inscrire à son tour.
PARAMETRES_PRE_INSCRITS = Parametres(n_min=8, expiry_sec=120)


class SerieRouge(Strategy):
    """Au moins `n_min` rouges contiguës : CALL contre la série."""

    name = "serie_rouge"

    def __init__(self, p: Parametres = PARAMETRES_PRE_INSCRITS):
        self.p = p

    @property
    def bougies_minimum(self) -> int:
        return self.p.n_min

    @property
    def params(self) -> Mapping[str, Any]:
        return {"n_min": self.p.n_min, "expiry_sec": self.p.expiry_sec}

    def evaluer(self, view: MarketView) -> Evaluation:
        p = self.p
        b = view.candles(p.n_min)
        assez = len(b) >= p.n_min
        rouges = 0
        contigues = assez
        if assez:
            # On compte depuis la dernière : la série s'arrête à la première
            # bougie qui n'est pas rouge.
            for c in reversed(b):
                if c.close < c.open:
                    rouges += 1
                else:
                    break
            contigues = all(b[k].ts_sec - b[k - 1].ts_sec == b[k].tf_sec
                            for k in range(1, len(b)))
        conditions = (
            ConditionResult("historique_suffisant", assez, float(len(b))),
            ConditionResult("minutes_contigues", contigues, None),
            ConditionResult("serie_rouge", rouges >= p.n_min, float(rouges)),
        )
        signal = None
        if all(c.validee for c in conditions):
            signal = Signal(
                pair=view.pair, direction=Direction.CALL,
                decided_at_ms=view.now_ms, expiry_sec=p.expiry_sec,
                features={"rouges": float(rouges)},
                reason=f"{rouges} rouges consécutives : retournement")
        return Evaluation(
            pair=view.pair, ts_ms=view.now_ms,
            direction_envisagee=Direction.CALL, conditions=conditions,
            features={"rouges": float(rouges)}, signal=signal)
