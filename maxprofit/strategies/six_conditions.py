"""
La stratégie initiale : six conditions en ET (spec §4, étape 5).

**Avertissement sur la provenance des six conditions.** La spec ne les énumère
nulle part. Elle en dit trois choses : qu'il y en a six, que deux d'entre elles
reposent sur des indicateurs repeignants (ZigZag et fractales de Chaos, §2.1),
et elle donne la liste des features à journaliser (§3.1). J'ai reconstruit les
six à partir de cette liste, en retour à la moyenne — le schéma habituel sur des
options binaires en M1. Si vos règles réelles diffèrent, ce sont les seuils et
les prédicats de ce fichier qu'il faut changer ; tout le reste du système est
indifférent à leur contenu.

Cela n'a d'ailleurs pas beaucoup d'importance à ce stade, et la spec le dit
elle-même (§3.2) :

    « Une condition dont l'écart est inférieur à 2 points ne sert à rien [...]
    Attendez-vous à ce que la majorité des six tombe dans ce cas. »

Ces six conditions ne sont pas une conviction, ce sont six hypothèses à
mesurer. Le livrable de l'étape 5 n'est pas la stratégie : c'est la
journalisation qui permettra à l'étape 6 de dire lesquelles méritent d'exister.

**Ce qui compte vraiment ici.** `evaluer` retourne une `Evaluation` à CHAQUE
bougie, signal ou non, avec la valeur numérique de chaque condition et toutes
les features. Sans cela, il n'y a pas de quasi-signaux, donc pas de
contrefactuels, donc aucune réponse à « qu'est-ce que le bot a raté ».

Aucun seuil n'a de valeur par défaut (§5). Six conditions dont on ne saurait
plus quels seuils ont produit quel résultat ne se mesurent pas.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from maxprofit.core.errors import BotError
from maxprofit.core.market_view import MarketView
from maxprofit.core.strategy import Strategy
from maxprofit.core.types import ConditionResult, Direction, Evaluation, Signal
from maxprofit.indicators.base import PivotKind
from maxprofit.indicators.fractals import derniere_fractale
from maxprofit.indicators.geometry import corps_pct, meche_basse_pct, meche_haute_pct
from maxprofit.indicators.oscillators import (
    atr_normalise_pct,
    bollinger_percent_b,
    distance_ma_pct,
    stochastique,
)
from maxprofit.indicators.zigzag import dernier_pivot

SECONDES_PAR_JOUR = 86_400


@dataclass(frozen=True)
class Parametres:
    """Tous obligatoires. Un seuil oublié est un seuil qu'on ne pourra pas
    incriminer quand le résultat sera décevant."""

    lookback: int
    expiry_sec: int

    periode_ma: int
    seuil_distance_ma_pct: float

    periode_bollinger: int
    ecarts_bollinger: float
    seuil_bb_bas: float
    seuil_bb_haut: float

    periode_stoch_k: int
    periode_stoch_d: int
    seuil_stoch_bas: float
    seuil_stoch_haut: float

    periode_atr: int
    atr_min_pct: float
    atr_max_pct: float

    zigzag_seuil_pct: float
    tolerance_pivot_pct: float

    fractale_age_max: int

    def __post_init__(self) -> None:
        if self.lookback < 2:
            raise BotError(f"lookback trop court : {self.lookback}")
        if self.expiry_sec <= 0:
            raise BotError("expiry_sec doit être > 0")
        if not (0 <= self.seuil_bb_bas < self.seuil_bb_haut <= 1):
            raise BotError(
                f"Seuils Bollinger incohérents : bas={self.seuil_bb_bas} "
                f"haut={self.seuil_bb_haut}. Attendu 0 <= bas < haut <= 1."
            )
        if not (0 <= self.seuil_stoch_bas < self.seuil_stoch_haut <= 100):
            raise BotError("Seuils stochastiques incohérents")
        if not (0 <= self.atr_min_pct < self.atr_max_pct):
            raise BotError("Plage d'ATR incohérente")
        besoin = max(
            self.periode_ma, self.periode_bollinger,
            self.periode_stoch_k + self.periode_stoch_d - 1, self.periode_atr + 1,
        )
        if self.lookback < besoin:
            raise BotError(
                f"lookback={self.lookback} est plus court que la plus longue "
                f"période d'indicateur ({besoin}). Les conditions concernées "
                f"échoueraient systématiquement, ce qui ressemblerait à une "
                f"stratégie trop sélective plutôt qu'à une erreur de réglage."
            )


class SixConditions(Strategy):
    """Retour à la moyenne : on entre CONTRE l'écart, quand six conditions
    concordent."""

    name = "six-conditions-v1"

    def __init__(self, params: Parametres):
        self.p = params

    @property
    def params(self) -> Mapping[str, Any]:
        return asdict(self.p)

    # --- évaluation ---------------------------------------------------------

    def evaluer(self, view: MarketView) -> Evaluation:
        bougies = view.candles(self.p.lookback)
        ts_sec = view.now_ms // 1000

        if len(bougies) < self.p.lookback:
            return Evaluation(
                pair=view.pair, ts_ms=view.now_ms, direction_envisagee=None,
                conditions=(ConditionResult("historique", False, len(bougies)),),
                features={},
            )

        features = self._features(bougies, ts_sec)
        direction = self._direction(features)
        if direction is None:
            # Sans distance à la moyenne, on ne sait même pas de quel côté on
            # se placerait : pas de contrefactuel possible non plus.
            return Evaluation(
                pair=view.pair, ts_ms=view.now_ms, direction_envisagee=None,
                conditions=(ConditionResult("distance_ma", False, None),),
                features=features,
            )

        conditions = self._conditions(bougies, features, direction)

        signal = None
        if all(c.validee for c in conditions):
            signal = Signal(
                pair=view.pair, direction=direction, decided_at_ms=view.now_ms,
                expiry_sec=self.p.expiry_sec, features=features,
                reason="six conditions validées",
            )

        return Evaluation(
            pair=view.pair, ts_ms=view.now_ms, direction_envisagee=direction,
            conditions=conditions, features=features, signal=signal,
        )

    # --- features -----------------------------------------------------------

    def _features(self, bougies, ts_sec: int) -> dict[str, float]:
        """Toutes les features du §3.1, calculées AU MOMENT de la décision.

        `heure_utc` est dérivée par arithmétique entière plutôt qu'avec
        `datetime` : une stratégie ne doit importer ni horloge ni fuseau (voir
        `tests/test_layering.py`), et l'heure locale se déduira à l'analyse,
        pas ici — le fuseau d'Haïti change deux fois par an, une valeur locale
        figée en base ne serait plus interprétable.
        """
        derniere = bougies[-1]
        stoch = stochastique(bougies, self.p.periode_stoch_k, self.p.periode_stoch_d)
        pivot_bas = dernier_pivot(bougies, self.p.zigzag_seuil_pct, PivotKind.BAS)
        pivot_haut = dernier_pivot(bougies, self.p.zigzag_seuil_pct, PivotKind.HAUT)

        brut = {
            "ma_distance_pct": distance_ma_pct(bougies, self.p.periode_ma),
            "bb_percent_b": bollinger_percent_b(
                bougies, self.p.periode_bollinger, self.p.ecarts_bollinger),
            "stoch_k": stoch.k if stoch else None,
            "stoch_d": stoch.d if stoch else None,
            "atr_normalise_pct": atr_normalise_pct(bougies, self.p.periode_atr),
            "distance_pivot_bas_pct": self._ecart(derniere.close, pivot_bas),
            "distance_pivot_haut_pct": self._ecart(derniere.close, pivot_haut),
            "corps_pct": corps_pct(derniere),
            "meche_haute_pct": meche_haute_pct(derniere),
            "meche_basse_pct": meche_basse_pct(derniere),
            "heure_utc": float((ts_sec // 3600) % 24),
            "minute_utc": float((ts_sec % 3600) // 60),
            "minutes_depuis_minuit_utc": float((ts_sec % SECONDES_PAR_JOUR) // 60),
            "tick_count": float(derniere.tick_count),
        }
        # Les features absentes sont OMISES et non remplacées par un zéro : un
        # zéro se confondrait avec une valeur mesurée, et l'analyse
        # d'attribution moyennerait des chiffres inventés.
        return {k: float(v) for k, v in brut.items() if v is not None}

    @staticmethod
    def _ecart(prix: float, pivot) -> float | None:
        if pivot is None or pivot.price == 0:
            return None
        return (prix / pivot.price - 1) * 100

    # --- direction et conditions --------------------------------------------

    def _direction(self, features: Mapping[str, float]) -> Direction | None:
        """Retour à la moyenne : sous la moyenne on envisage un CALL.

        La direction est envisagée AVANT que les conditions soient évaluées, et
        indépendamment d'elles. C'est ce qui permet d'enregistrer un
        contrefactuel pour les bougies qui n'ont pas produit de signal (§3.1).
        """
        distance = features.get("ma_distance_pct")
        if distance is None or distance == 0:
            return None
        return Direction.CALL if distance < 0 else Direction.PUT

    def _conditions(self, bougies, features, direction) -> tuple[ConditionResult, ...]:
        achat = direction is Direction.CALL
        f = features.get

        distance = f("ma_distance_pct")
        bb = f("bb_percent_b")
        k, d = f("stoch_k"), f("stoch_d")
        atr = f("atr_normalise_pct")
        ecart_pivot = f("distance_pivot_bas_pct" if achat else "distance_pivot_haut_pct")
        fractale = derniere_fractale(
            bougies, PivotKind.BAS if achat else PivotKind.HAUT)

        age_fractale = None
        if fractale is not None:
            age_fractale = float(len(bougies) - 1 - fractale.index)

        return (
            # 1. Le prix s'est suffisamment écarté de sa moyenne.
            ConditionResult(
                "distance_ma",
                distance is not None
                and abs(distance) >= self.p.seuil_distance_ma_pct,
                distance,
            ),
            # 2. Et il est sorti de la bande de Bollinger du bon côté.
            ConditionResult(
                "bollinger",
                bb is not None and (bb <= self.p.seuil_bb_bas if achat
                                    else bb >= self.p.seuil_bb_haut),
                bb,
            ),
            # 3. Stochastique en zone extrême ET en train de se retourner.
            #    Le croisement compte : une survente qui s'enfonce n'est pas un
            #    signal de retour, c'est une tendance.
            ConditionResult(
                "stochastique",
                k is not None and d is not None
                and ((k <= self.p.seuil_stoch_bas and k > d) if achat
                     else (k >= self.p.seuil_stoch_haut and k < d)),
                k,
            ),
            # 4. Volatilité dans une plage exploitable. Trop basse, le prix ne
            #    bougera pas assez en 60 s ; trop haute, le retour à la moyenne
            #    n'a plus de sens.
            ConditionResult(
                "volatilite",
                atr is not None and self.p.atr_min_pct <= atr <= self.p.atr_max_pct,
                atr,
            ),
            # 5. Proximité d'un pivot ZigZag CONFIRMÉ. Le pivot en cours de
            #    formation est invisible ici, par construction (§2.1).
            ConditionResult(
                "pivot_zigzag",
                ecart_pivot is not None
                and abs(ecart_pivot) <= self.p.tolerance_pivot_pct,
                ecart_pivot,
            ),
            # 6. Fractale de Chaos confirmée du bon côté, et récente. Elle est
            #    forcément vieille d'au moins 2 bougies : c'est sa latence de
            #    confirmation, pas un retard de l'implémentation.
            ConditionResult(
                "fractale",
                age_fractale is not None and age_fractale <= self.p.fractale_age_max,
                age_fractale,
            ),
        )


#: Point de départ pour l'étape 5 : des valeurs plausibles, PAS optimisées.
#: Les calibrer avant d'avoir mesuré quoi que ce soit reviendrait à choisir
#: parmi des dizaines de variantes sur du bruit — c'est précisément ce que le
#: compteur d'expériences du §2.6 sert à rendre visible.
PARAMETRES_DEPART = Parametres(
    lookback=60,
    expiry_sec=60,
    periode_ma=14,
    seuil_distance_ma_pct=0.03,
    periode_bollinger=20,
    ecarts_bollinger=2.0,
    seuil_bb_bas=0.10,
    seuil_bb_haut=0.90,
    periode_stoch_k=14,
    periode_stoch_d=3,
    seuil_stoch_bas=25.0,
    seuil_stoch_haut=75.0,
    periode_atr=14,
    atr_min_pct=0.005,
    atr_max_pct=0.20,
    zigzag_seuil_pct=0.10,
    tolerance_pivot_pct=0.08,
    fractale_age_max=12,
)
