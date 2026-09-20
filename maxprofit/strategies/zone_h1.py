"""
La zone respectée, dans le sens de la tendance H1 — l'hypothèse pré-inscrite.

--- D'où elle vient --------------------------------------------------------

Elle n'est pas de moi. Elle est la traduction d'une méthode décrite ainsi :

  « On trace le niveau sur une pique plus haut ou plus bas où l'on trouve
    DEUX BOUGIES OPPOSÉES. On vérifie si cette zone est respectée : deux
    touches la rendent plus ou moins fiable. Utiliser une unité de temps
    supérieure pour la TENDANCE, et venir sur la petite pour tracer les zones. »
  « Sur les corps de bougie. »

Et une règle que j'avais omise, qui a divisé le nombre de signaux par quinze :
**une zone cassée est morte.** « On vérifie si cette zone est RESPECTÉE. »

--- ⚠ Ce qu'elle vaut, et ce qu'elle ne vaut pas --------------------------

Mesurée sur 12 jours, 4 paires OTC, échéance 15 min, payout appliqué :

    221 signaux, 57,9 % de réussite, seuil 54,3 %, espérance +0,067 $/$

**Ce n'est pas établi.** Le test de permutation donne p = 0,33 : un tiers des
jeux décalés font aussi bien. Corrigée des 150 expériences du registre, la p
vaut 1,000. Il faudrait 2 071 signaux pour trancher à 3 sigma, soit trois à
quatre mois au débit actuel.

Elle est ici parce qu'elle est la SEULE des 150 à n'être pas morte, et parce
qu'elle a été pré-inscrite (registre #58, #59) sur une période future. Ce
fichier est ce qui sera exécuté contre elle — pas une version réécrite.

--- ⚠ Causalité ----------------------------------------------------------

Une pique demande `fenetre_pique` bougies POSTÉRIEURES pour être reconnue : la
zone n'existe donc qu'à partir de là, jamais dès la bougie du creux. La
tendance H1 se lit sur des heures CLOSES — utiliser l'heure en cours
reviendrait à lire une clôture qui n'a pas eu lieu.
"""

from __future__ import annotations

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

    #: Bougies de chaque côté pour qu'une pique RESSORTE. À 3, on trouvait
    #: seize niveaux par écran là où un trader en trace deux.
    fenetre_pique: int
    #: Tolérance d'une touche, et largeur au-delà de laquelle la zone est
    #: considérée cassée. Les deux sont le même nombre : une zone qu'on touche
    #: à ±x est une zone qu'on casse au-delà de x.
    tolerance_pct: float
    #: Touches nécessaires avant de trader la suivante.
    touches_requises: int
    #: Au-delà, la zone n'est plus surveillée.
    memoire: int
    #: Heures closes de recul pour juger la tendance.
    recul_h1: int
    expiry_sec: int
    lookback: int

    def __post_init__(self) -> None:
        if self.fenetre_pique < 1:
            raise BotError(f"fenetre_pique invalide : {self.fenetre_pique}")
        if self.tolerance_pct <= 0:
            raise BotError(f"tolerance_pct invalide : {self.tolerance_pct}")
        if self.touches_requises < 0:
            raise BotError(f"touches_requises invalide : {self.touches_requises}")
        if self.expiry_sec <= 0:
            raise BotError(f"expiry_sec invalide : {self.expiry_sec}")
        if self.lookback < self.memoire + 2 * self.fenetre_pique:
            raise BotError(
                f"lookback={self.lookback} trop court pour mémoire="
                f"{self.memoire} et pique={self.fenetre_pique} : la zone la "
                f"plus ancienne serait hors de la vue, et la stratégie "
                f"trouverait moins de signaux en live qu'en backtest.")


#: Les valeurs exactes de l'hypothèse pré-inscrite. Les changer fait une AUTRE
#: hypothèse, qui doit être pré-inscrite à son tour.
PARAMETRES_PRE_INSCRITS = Parametres(
    fenetre_pique=12,
    tolerance_pct=0.02,
    touches_requises=1,
    memoire=240,
    recul_h1=2,
    expiry_sec=900,
    lookback=300,
)


def _corps(c: Candle) -> tuple[float, float]:
    return min(c.open, c.close), max(c.open, c.close)


class ZoneH1(Strategy):
    """Une zone respectée, touchée dans le sens de la tendance H1."""

    name = "zone_h1"

    def __init__(self, p: Parametres = PARAMETRES_PRE_INSCRITS):
        self.p = p

    @property
    def params(self) -> Mapping[str, Any]:
        return {
            "fenetre_pique": self.p.fenetre_pique,
            "tolerance_pct": self.p.tolerance_pct,
            "touches_requises": self.p.touches_requises,
            "memoire": self.p.memoire,
            "recul_h1": self.p.recul_h1,
            "expiry_sec": self.p.expiry_sec,
            "lookback": self.p.lookback,
        }

    # --- les briques, toutes causales ------------------------------------

    def _zones(self, bougies: tuple[Candle, ...]) -> list[tuple[int, float, int]]:
        """(index de confirmation, prix, sens). Sens +1 = support."""
        w = self.p.fenetre_pique
        out: list[tuple[int, float, int]] = []
        for j in range(w, len(bougies) - w - 1):
            a, b = bougies[j], bougies[j + 1]
            bas_a, haut_a = _corps(a)
            rouge_a, vert_a = a.close < a.open, a.close > a.open
            rouge_b, vert_b = b.close < b.open, b.close > b.open
            voisins = bougies[j - w:j + w + 1]
            if rouge_a and vert_b:
                bas = [_corps(x)[0] for x in voisins]
                if bas_a == min(bas) and bas.count(bas_a) == 1:
                    out.append((j + w, bas_a, +1))
            if vert_a and rouge_b:
                hauts = [_corps(x)[1] for x in voisins]
                if haut_a == max(hauts) and hauts.count(haut_a) == 1:
                    out.append((j + w, haut_a, -1))
        return out

    def _tendance_h1_haussiere(
            self, bougies: tuple[Candle, ...]) -> bool | None:
        """Vrai si l'heure close précédente est au-dessus de celle d'avant.

        `None` quand il n'y a pas assez d'heures closes : c'est un refus, pas
        un défaut — répondre « baissière » faute d'heures ferait trader dans
        un sens choisi par l'absence de données.
        """
        pas = 3600
        fermetures: dict[int, float] = {}
        for c in bougies:
            fermetures[(c.ts_sec // pas) * pas] = c.close
        heures = sorted(fermetures)
        # L'heure EN COURS est la dernière : on ne s'en sert pas.
        closes = [fermetures[h] for h in heures[:-1]]
        if len(closes) < self.p.recul_h1 + 1:
            return None
        return closes[-1] > closes[-1 - self.p.recul_h1]

    # --- l'évaluation ----------------------------------------------------

    def evaluer(self, view: MarketView) -> Evaluation:
        bougies = view.candles(self.p.lookback)
        derniere = bougies[-1] if bougies else None
        ts_ms = view.now_ms

        assez = len(bougies) >= 2 * self.p.fenetre_pique + 2
        hausse = self._tendance_h1_haussiere(bougies) if assez else None

        touche_nom = None
        sens = 0
        valeur_touche = None
        if assez:
            i = len(bougies) - 1
            bas_i, haut_i = _corps(bougies[i])
            for conf, prix, s in self._zones(bougies):
                if conf > i - 1 or i - conf > self.p.memoire:
                    continue
                marge = prix * self.p.tolerance_pct / 100
                # Une zone cassée est morte : on la déclare telle dès qu'une
                # clôture postérieure est passée au travers.
                cassee = any(
                    (s > 0 and bougies[k].close < prix - marge) or
                    (s < 0 and bougies[k].close > prix + marge)
                    for k in range(conf + 1, i + 1))
                if cassee:
                    continue
                touches = sum(
                    1 for k in range(conf + 1, i)
                    if abs((_corps(bougies[k])[0] if s > 0
                            else _corps(bougies[k])[1]) - prix) <= marge)
                if touches < self.p.touches_requises:
                    continue
                proche = bas_i if s > 0 else haut_i
                if abs(proche - prix) > marge:
                    continue
                respecte = (derniere.close > prix if s > 0
                            else derniere.close < prix)
                if not respecte:
                    continue
                touche_nom = prix
                sens = s
                valeur_touche = float(touches)
                break

        aligne = (sens != 0 and hausse is not None
                  and ((sens > 0) == hausse))
        direction = (Direction.CALL if sens > 0 else Direction.PUT) \
            if sens != 0 else None

        conditions = (
            ConditionResult("historique_suffisant", assez, float(len(bougies))),
            ConditionResult("zone_vivante_touchee", touche_nom is not None,
                            valeur_touche),
            ConditionResult("tendance_h1_connue", hausse is not None,
                            None if hausse is None else float(hausse)),
            ConditionResult("sens_aligne_sur_h1", aligne, None),
        )
        signal = None
        if aligne and derniere is not None:
            signal = Signal(
                pair=view.pair,
                direction=direction,
                decided_at_ms=ts_ms,
                expiry_sec=self.p.expiry_sec,
                features={"niveau": float(touche_nom),
                          "touches": float(valeur_touche or 0)},
                reason="zone respectée, touchée dans le sens de la tendance H1",
            )
        return Evaluation(
            pair=view.pair,
            ts_ms=ts_ms,
            direction_envisagee=direction,
            conditions=conditions,
            features={"niveau": float(touche_nom or 0.0),
                      "tendance_h1": float(hausse) if hausse is not None else -1.0},
            signal=signal,
        )
