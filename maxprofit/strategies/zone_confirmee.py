"""
La zone tracée en H1, l'entrée confirmée en M1.

--- D'où elle vient --------------------------------------------------------

De l'utilisateur, le 2026-10-02, après quatre pertes d'affilée dont une
vente AUD/CHF prise alors que le marché était en range, qu'une bougie de
rejet venait de défendre la zone et que les acheteurs avaient l'élan :

  « H1 c'est pour déterminer la zone, M1 pour confirmer qu'on peut prendre
    un ordre dans cette zone. »
  « Sur M1 il faut qu'il définisse clairement qu'on va changer de direction. »

ZoneH1 fait l'inverse : elle trace ses zones sur des piques M1 de douze
minutes et ne regarde l'H1 que pour la tendance. Sa seule « confirmation »
est qu'une bougie M1 touche la zone et clôture du bon côté — rien ne dit que
le marché se retourne.

--- La règle -----------------------------------------------------------------

1. Les zones sont des piques H1 à deux bougies opposées, sur les corps,
   comme celles de ZoneH1 mais sur des heures closes : support sous le
   prix, résistance au-dessus. Sa largeur est un quart de l'amplitude H1
   moyenne. Une zone qu'une clôture H1 a traversée est morte ; une zone
   déjà touchée par plus de `touches_max` heures est usée.
2. Le prix doit être VENU à la zone dans les dernières `approche_m1`
   minutes : la mèche haute (résistance) ou basse (support) l'a atteinte.
3. Le retournement doit être ÉCRIT en M1 : la dernière bougie close va
   dans le sens du trade ET clôture au-delà de l'extrême de la précédente
   (sous son plus bas pour une vente, au-dessus de son plus haut pour un
   achat) ET se referme du bon côté du niveau. Les vendeurs (ou les acheteurs)
   ont repris la main — pas seulement « le prix a touché ».

Le sens vient de la zone : on vend une résistance rejetée, on achète un
support défendu. Jamais l'inverse : une zone que les acheteurs viennent de
défendre ne peut pas donner une vente.

--- ⚠ Rien n'est établi ---------------------------------------------------

C'est une hypothèse. Elle est jugée dans `/laboratoire`, sur les jours que
rien n'a vus, face à ZoneH1 telle qu'en direct, avant de remplacer quoi que
ce soit.

--- Causalité et cache ----------------------------------------------------

Seules les heures CLOSES servent, et la première heure de la fenêtre, en
général partielle, est écartée. Pendant toute une heure, ces heures closes
sont donc les mêmes : les zones sont calculées une fois par (paire, heure)
et gardées. C'est une optimisation pure — le résultat est identique avec ou
sans cache, sur les mêmes bougies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from maxprofit.core.errors import BotError
from maxprofit.core.market_view import MarketView
from maxprofit.core.strategy import Strategy
from maxprofit.core.types import (
    Candle, ConditionResult, Direction, Evaluation, Signal)

HEURE = 3600


@dataclass(frozen=True)
class Parametres:
    #: Heures de chaque côté pour qu'une pique H1 ressorte.
    fenetre_pique_h1: int
    #: Demi-largeur de la zone, en part de l'amplitude H1 moyenne. Un
    #: pourcentage fixe ne tient pas : à 0,03 % du prix, n'importe quelle
    #: heure ordinaire « traversait » la zone, et il n'en restait aucune.
    largeur_amplitude: float
    #: Heures H1 ayant déjà touché la zone au-delà desquelles elle est usée.
    touches_max: int
    #: Minutes pendant lesquelles le prix doit être venu à la zone.
    approche_m1: int
    #: Âge maximal d'une zone, en heures.
    memoire_h: int
    expiry_sec: int
    #: Minutes de recul demandées à la vue.
    lookback: int
    #: Part minimale de `lookback` réellement présente : une coupure de
    #: collecte ne doit pas faire taire la stratégie pendant deux jours.
    couverture_min: float

    def __post_init__(self) -> None:
        if self.fenetre_pique_h1 < 1 or self.largeur_amplitude <= 0 \
                or self.approche_m1 < 1 or self.expiry_sec <= 0:
            raise BotError(f"paramètres invalides : {self}")
        if self.lookback < (self.memoire_h + 2 * self.fenetre_pique_h1 + 2) \
                * 60:
            raise BotError("lookback trop court pour la mémoire des zones")


PARAMETRES = Parametres(
    fenetre_pique_h1=3, largeur_amplitude=0.25, touches_max=2, approche_m1=5,
    memoire_h=40, expiry_sec=900, lookback=2880, couverture_min=0.9)


def _corps(c) -> tuple[float, float]:
    return min(c.open, c.close), max(c.open, c.close)


@dataclass(frozen=True)
class _H1:
    debut: int
    open: float
    high: float
    low: float
    close: float


class ZoneConfirmee(Strategy):
    """Zone H1, retournement confirmé en M1."""

    name = "zone_h1_confirmee_m1"

    def __init__(self, p: Parametres = PARAMETRES):
        self.p = p
        self._cache: dict[tuple[str, int], list] = {}

    @property
    def params(self) -> Mapping[str, Any]:
        return {k: getattr(self.p, k) for k in self.p.__dataclass_fields__}

    def reset(self) -> None:
        self._cache.clear()

    # --- les heures et les zones ------------------------------------------

    @staticmethod
    def _heures_closes(bougies) -> list[_H1]:
        """Les heures closes, sans la première (partielle) ni l'heure en
        cours — sauf si la dernière bougie la termine."""
        groupes: dict[int, list[Candle]] = {}
        for c in bougies:
            groupes.setdefault(c.ts_sec // HEURE * HEURE, []).append(c)
        debuts = sorted(groupes)
        derniere = bougies[-1]
        if (derniere.ts_sec + 60) % HEURE:
            debuts = debuts[:-1]             # l'heure en cours n'est pas close
        debuts = debuts[1:]                  # la première est partielle
        return [_H1(h, groupes[h][0].open, max(c.high for c in groupes[h]),
                    min(c.low for c in groupes[h]), groupes[h][-1].close)
                for h in debuts]

    def _zones(self, heures: list[_H1]) -> list[tuple[float, int, int, float]]:
        """(prix, sens, touches, marge) des zones vivantes. Sens +1 = support."""
        w = self.p.fenetre_pique_h1
        sortie = []
        n = len(heures)
        if not n:
            return sortie
        marge = self.p.largeur_amplitude * sum(
            h.high - h.low for h in heures) / n
        for j in range(max(w, n - self.p.memoire_h), n - w - 1):
            a, b = heures[j], heures[j + 1]
            voisins = heures[j - w:j + w + 1]
            bas_a, haut_a = _corps(a)
            for sens in (+1, -1):
                # ⚠ L'ÉGALITÉ AVEC LA BOUGIE SUIVANTE EST LA RÈGLE, pas
                # l'exception : en H1, l'ouverture d'une heure vaut presque
                # toujours la clôture de la précédente. Exiger un extrême
                # unique, comme sur les piques M1, ne laissait aucune zone.
                autres = voisins[:w] + voisins[w + 2:]
                if sens > 0:
                    forme = a.close < a.open and b.close > b.open
                    prix = bas_a
                    ok = forme and prix <= _corps(b)[0] and \
                        all(_corps(x)[0] > prix for x in autres)
                else:
                    forme = a.close > a.open and b.close < b.open
                    prix = haut_a
                    ok = forme and prix >= _corps(b)[1] and \
                        all(_corps(x)[1] < prix for x in autres)
                if not ok:
                    continue
                apres = heures[j + w + 1:]
                if any((sens > 0 and h.close < prix - marge)
                       or (sens < 0 and h.close > prix + marge)
                       for h in apres):
                    continue                 # traversée : morte
                touches = sum(
                    1 for h in apres
                    if (sens > 0 and h.low <= prix + marge)
                    or (sens < 0 and h.high >= prix - marge))
                sortie.append((prix, sens, touches, marge))
        return sortie

    def _zones_de(self, pair: str, bougies
                  ) -> list[tuple[float, int, int, float]]:
        derniere = bougies[-1]
        cle = (pair, (derniere.ts_sec + 60) // HEURE)
        if cle not in self._cache:
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[cle] = self._zones(self._heures_closes(bougies))
        return self._cache[cle]

    # --- l'évaluation -----------------------------------------------------

    def evaluer(self, view: MarketView) -> Evaluation:
        bougies = view.candles(self.p.lookback)
        ts_ms = view.now_ms
        assez = len(bougies) >= self.p.lookback * self.p.couverture_min
        sens_bougie = 0
        if assez and len(bougies) >= 2:
            c, prec = bougies[-1], bougies[-2]
            if c.close < c.open and c.close < prec.low:
                sens_bougie = -1
            elif c.close > c.open and c.close > prec.high:
                sens_bougie = +1
        zone = None
        touches = 0.0
        if sens_bougie:
            c = bougies[-1]
            recentes = bougies[-self.p.approche_m1:]
            for prix, sens, n, marge in self._zones_de(view.pair, bougies):
                if sens != sens_bougie or n > self.p.touches_max:
                    continue
                # Venu dans la zone, puis refermé du bon côté du niveau.
                if sens < 0:
                    venu = max(b.high for b in recentes) >= prix - marge
                    sorti = c.close < prix
                else:
                    venu = min(b.low for b in recentes) <= prix + marge
                    sorti = c.close > prix
                if venu and sorti and (zone is None or
                                       abs(c.close - prix) <
                                       abs(c.close - zone)):
                    zone, touches = prix, float(n)
        direction = None
        if sens_bougie:
            direction = Direction.PUT if sens_bougie < 0 else Direction.CALL
        conditions = (
            ConditionResult("historique_suffisant", assez,
                            float(len(bougies))),
            ConditionResult("retournement_m1", sens_bougie != 0, None),
            ConditionResult("zone_h1_rejetee", zone is not None, touches),
        )
        signal = None
        if zone is not None:
            signal = Signal(
                pair=view.pair, direction=direction, decided_at_ms=ts_ms,
                expiry_sec=self.p.expiry_sec,
                features={"niveau": float(zone), "touches": touches},
                reason="zone H1 rejetée, retournement confirmé en M1")
        return Evaluation(pair=view.pair, ts_ms=ts_ms,
                          direction_envisagee=direction,
                          conditions=conditions,
                          features={"niveau": float(zone or 0.0)},
                          signal=signal)
