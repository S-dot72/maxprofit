"""
Le rebond sur la bande médiane de Bollinger, dans une tendance haussière.

--- D'où elle vient --------------------------------------------------------

De l'utilisateur, capture d'écran à l'appui (AED/CNY OTC, 2026-09-27) :

  « La tendance est haussière, le marché a fait un plus haut, il retourne sur
    la bande médiane de Bollinger. Si la dernière bougie rouge ne casse pas la
    bande médiane, on surveille s'il va donner une bougie verte ; si ça arrive
    on entre à la hausse sur la bougie suivant la verte, pour une minute. »

Précisions données ensuite, et traduites telles quelles :

- casser la médiane = CLÔTURER sous la médiane ;
- toucher = la mèche atteint la médiane, ou s'en approche au point de
  quasiment la toucher (`tolerance_sigma`) ;
- deux, trois ou quatre bougies rouges de repli restent un setup valable
  (`rouges_max`) ;
- achat seulement : pas de symétrique en tendance baissière.

Et la raison de son existence : ne JAMAIS prendre position contre la tendance
visible. ZoneH1 juge la tendance sur deux heures closes et peut vendre une
résistance pendant que la dernière heure monte — c'est ce qui a coûté la
position sur GBP/AUD le 2026-09-27.

--- Ce qu'elle vaut --------------------------------------------------------

Rien encore. Elle doit passer par `outils/rejeu_plan.py --strategie
rebond_mediane` et tenir sur la période de VALIDATION avant d'approcher le
direct.

--- Causalité ----------------------------------------------------------------

L'entrée « sur la bougie suivant la verte » a lieu à l'ouverture de cette
bougie, c'est-à-dire à la clôture de la verte : c'est l'instant où la course
décide déjà. Tout se lit sur des bougies CLOSES.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from maxprofit.core.errors import BotError
from maxprofit.core.market_view import MarketView
from maxprofit.core.strategy import Strategy
from maxprofit.core.types import (
    Candle,
    ConditionResult,
    Direction,
    Evaluation,
    Signal,
)


@dataclass(frozen=True)
class Parametres:
    """Aucun défaut (§5) : tout ce qui touche à l'argent est fourni."""

    #: Période de la moyenne mobile qui fait la bande médiane.
    periode: int
    #: « Quasiment toucher » : la mèche basse d'une rouge doit descendre à
    #: moins de `tolerance_sigma` écarts-types au-dessus de la médiane.
    tolerance_sigma: float
    #: Bougies rouges de repli acceptées avant la verte.
    rouges_max: int
    #: Le sommet d'avant repli doit être le plus haut de cette fenêtre…
    fenetre_plus_haut: int
    #: … et avoir été atteint dans ces dernières bougies avant la 1re rouge.
    plus_haut_recent: int
    #: La médiane doit monter sur ce nombre de bougies.
    pente_bougies: int
    expiry_sec: int

    def __post_init__(self) -> None:
        for nom in ("periode", "rouges_max", "fenetre_plus_haut",
                    "plus_haut_recent", "pente_bougies", "expiry_sec"):
            if getattr(self, nom) < 1:
                raise BotError(f"{nom} invalide : {getattr(self, nom)}")
        if self.tolerance_sigma < 0:
            raise BotError(f"tolerance_sigma invalide : {self.tolerance_sigma}")
        if self.plus_haut_recent > self.fenetre_plus_haut:
            raise BotError("plus_haut_recent dépasse fenetre_plus_haut")

    @property
    def lookback(self) -> int:
        return (self.periode + self.fenetre_plus_haut + self.rouges_max
                + self.pente_bougies + 1)


PARAMETRES_PAR_DEFAUT = Parametres(
    periode=20, tolerance_sigma=0.25, rouges_max=4, fenetre_plus_haut=20,
    plus_haut_recent=3, pente_bougies=5, expiry_sec=60)


def _mediane_et_sigma(bougies: tuple[Candle, ...], k: int,
                      periode: int) -> tuple[float, float]:
    closes = [b.close for b in bougies[k - periode + 1:k + 1]]
    moyenne = sum(closes) / periode
    variance = sum((c - moyenne) ** 2 for c in closes) / periode
    return moyenne, math.sqrt(variance)


class RebondMediane(Strategy):
    """Repli sans cassure sur la médiane de Bollinger, puis bougie verte."""

    name = "rebond_mediane"

    def __init__(self, p: Parametres = PARAMETRES_PAR_DEFAUT):
        self.p = p

    @property
    def bougies_minimum(self) -> int:
        return self.p.lookback

    @property
    def params(self) -> Mapping[str, Any]:
        return {
            "periode": self.p.periode,
            "tolerance_sigma": self.p.tolerance_sigma,
            "rouges_max": self.p.rouges_max,
            "fenetre_plus_haut": self.p.fenetre_plus_haut,
            "plus_haut_recent": self.p.plus_haut_recent,
            "pente_bougies": self.p.pente_bougies,
            "expiry_sec": self.p.expiry_sec,
        }

    def evaluer(self, view: MarketView) -> Evaluation:
        p = self.p
        b = view.candles(p.lookback)
        i = len(b) - 1
        assez = len(b) >= p.lookback

        verte = tendance = sommet = repli = False
        n_rouges = 0
        mediane_i = None
        if assez:
            mediane_i, _ = _mediane_et_sigma(b, i, p.periode)
            derniere = b[i]
            verte = derniere.close > derniere.open and derniere.close > mediane_i

            # Le repli : exactement 1 à `rouges_max` rouges juste avant la
            # verte, précédées d'une bougie qui n'est pas rouge.
            while (n_rouges < p.rouges_max
                   and b[i - 1 - n_rouges].close < b[i - 1 - n_rouges].open):
                n_rouges += 1
            debut = i - n_rouges
            precedente_rouge = b[debut - 1].close < b[debut - 1].open
            if n_rouges and not precedente_rouge:
                cassee = touchee = False
                for k in range(debut, i):
                    m, s = _mediane_et_sigma(b, k, p.periode)
                    if b[k].close < m:
                        cassee = True
                    if b[k].low <= m + p.tolerance_sigma * s:
                        touchee = True
                repli = touchee and not cassee

                # Le plus haut récent : le sommet de la fenêtre qui précède
                # le repli a été atteint juste avant la première rouge.
                fenetre = range(debut - p.fenetre_plus_haut, debut)
                haut = max(fenetre, key=lambda k: (b[k].high, k))
                sommet = haut >= debut - p.plus_haut_recent

                # La tendance : médiane qui monte, et le prix au-dessus
                # d'elle avant le repli.
                m_avant, _ = _mediane_et_sigma(b, debut - 1, p.periode)
                m_passee, _ = _mediane_et_sigma(b, i - p.pente_bougies,
                                                p.periode)
                tendance = (mediane_i > m_passee
                            and b[debut - 1].close > m_avant)

        conditions = (
            ConditionResult("historique_suffisant", assez, float(len(b))),
            ConditionResult("tendance_haussiere", tendance, None),
            ConditionResult("plus_haut_recent", sommet, None),
            ConditionResult("repli_sans_cassure_de_la_mediane", repli,
                            float(n_rouges)),
            ConditionResult("bougie_verte_au_dessus", verte, None),
        )
        signal = None
        if all(c.validee for c in conditions):
            signal = Signal(
                pair=view.pair, direction=Direction.CALL,
                decided_at_ms=view.now_ms, expiry_sec=p.expiry_sec,
                features={"mediane": float(mediane_i),
                          "rouges": float(n_rouges)},
                reason="repli sans cassure sur la médiane, puis verte")
        return Evaluation(
            pair=view.pair, ts_ms=view.now_ms,
            direction_envisagee=Direction.CALL, conditions=conditions,
            features={"mediane": float(mediane_i or 0.0),
                      "rouges": float(n_rouges)},
            signal=signal)
