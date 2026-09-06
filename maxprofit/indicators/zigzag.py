"""
ZigZag — avec latence de confirmation explicite (spec §2.1).

    « Le ZigZag est un indicateur repeignant. Un pivot n'est confirmé qu'après
    un retournement d'amplitude suffisante, donc plusieurs bougies plus tard.
    Si le backtest lit zigzag[t] calculé sur la série complète, il utilise une
    information qui n'existait pas à l'instant t. Le résultat sera
    spectaculaire et entièrement faux. »

Le ZigZag est plus vicieux que les fractales. La latence des fractales est
constante et vaut 2 : on la corrige en décalant de deux bougies. Celle du
ZigZag est VARIABLE et dépend du marché — un sommet peut rester non confirmé
pendant trois bougies dans un retournement brutal, ou quarante dans un plateau.
Il n'existe aucun décalage fixe qui rende un ZigZag honnête ; il faut le
recalculer causalement.

**Comment la causalité est garantie ici, structurellement.**

`ZigZag` est un automate qui consomme les bougies une par une et n'a aucun accès
à celles qui suivent. Un pivot n'est émis qu'au moment où le mouvement inverse
atteint le seuil, et il porte alors deux indices : celui de l'extrême, et celui
de la bougie où on l'a su. La fonction `zigzag()` n'est qu'un pliage de cet
automate sur la série. Le look-ahead n'est donc pas évité par vigilance : la
forme du calcul le rend impossible.

L'automate ne conserve en mémoire que les bougies de la jambe en cours (depuis
le dernier pivot confirmé), ce qui le rend utilisable tel quel en live sur un
flux infini.
"""

from __future__ import annotations

from typing import Sequence

from maxprofit.core.errors import BotError
from maxprofit.core.types import Candle
from maxprofit.indicators.base import Pivot, PivotKind, verifier_serie


class _Suiveur:
    """Suit l'extrême d'un sens et le contre-mouvement qui le confirmera.

    `ext` est le candidat pivot (le plus haut si HAUT, le plus bas si BAS).
    `contre` est l'extrême opposé observé DEPUIS que `ext` a été fixé : c'est
    lui qui mesure le retournement. Le remettre à zéro à chaque nouvel `ext` est
    essentiel — sinon un creux antérieur au sommet servirait à confirmer ce
    sommet, ce qui est du look-ahead à l'envers.
    """

    __slots__ = ("kind", "ext_index", "ext_price", "ext_ts", "contre_index",
                 "contre_price", "contre_ts")

    def __init__(self, kind: PivotKind):
        self.kind = kind
        self.ext_index: int | None = None
        self.ext_price: float = 0.0
        self.ext_ts: int = 0
        self.contre_index: int | None = None
        self.contre_price: float = 0.0
        self.contre_ts: int = 0

    def _prix_ext(self, c: Candle) -> float:
        return c.high if self.kind is PivotKind.HAUT else c.low

    def _prix_contre(self, c: Candle) -> float:
        return c.low if self.kind is PivotKind.HAUT else c.high

    def _est_plus_extreme(self, prix: float) -> bool:
        return prix > self.ext_price if self.kind is PivotKind.HAUT else prix < self.ext_price

    def _est_plus_contre(self, prix: float) -> bool:
        return prix < self.contre_price if self.kind is PivotKind.HAUT else prix > self.contre_price

    def absorber(self, index: int, c: Candle) -> None:
        if self.ext_index is None:
            self.ext_index, self.ext_price, self.ext_ts = index, self._prix_ext(c), c.ts_sec
            self.contre_index, self.contre_price, self.contre_ts = None, self._prix_contre(c), c.ts_sec
            return
        if self._est_plus_extreme(self._prix_ext(c)):
            self.ext_index, self.ext_price, self.ext_ts = index, self._prix_ext(c), c.ts_sec
            # Nouvel extrême : le contre-mouvement repart de zéro.
            self.contre_index, self.contre_price, self.contre_ts = None, self._prix_contre(c), c.ts_sec
            return
        prix_contre = self._prix_contre(c)
        if self.contre_index is None or self._est_plus_contre(prix_contre):
            self.contre_index, self.contre_price, self.contre_ts = index, prix_contre, c.ts_sec

    def retournement_pct(self) -> float:
        """Amplitude du contre-mouvement, en pourcentage de l'extrême."""
        if self.ext_index is None or self.contre_index is None or self.ext_price == 0:
            return 0.0
        if self.kind is PivotKind.HAUT:
            return (self.ext_price - self.contre_price) / self.ext_price * 100
        return (self.contre_price - self.ext_price) / self.ext_price * 100

    def pivot(self) -> Pivot:
        return Pivot(
            kind=self.kind, index=self.ext_index, ts_sec=self.ext_ts,
            price=self.ext_price, confirmed_index=self.contre_index,
            confirmed_ts_sec=self.contre_ts,
        )


class ZigZag:
    """Automate causal. Consomme les bougies dans l'ordre, émet les pivots au
    moment où ils deviennent connaissables — jamais avant."""

    def __init__(self, seuil_pct: float):
        if not (seuil_pct > 0):
            raise BotError(
                f"seuil_pct doit être strictement positif, reçu {seuil_pct}. "
                f"Un seuil nul ferait de chaque bougie un pivot."
            )
        self.seuil_pct = float(seuil_pct)
        self.pivots: list[Pivot] = []
        # Bougies de la jambe en cours (postérieures au dernier pivot confirmé).
        self._jambe: list[tuple[int, Candle]] = []
        self._suiveurs = [_Suiveur(PivotKind.HAUT), _Suiveur(PivotKind.BAS)]

    # --- API ----------------------------------------------------------------

    def consommer(self, index: int, candle: Candle) -> list[Pivot]:
        """Absorbe une bougie et retourne les pivots devenus confirmés.

        Retourne une LISTE et non un pivot : un retournement en V confirme le
        sommet et le creux sur la même bougie. Renvoyer un seul pivot perdrait
        le second sans que rien ne le signale.
        """
        self._jambe.append((index, candle))
        a_traiter = [(index, candle)]
        emis: list[Pivot] = []

        while a_traiter:
            i, c = a_traiter.pop(0)
            for suiveur in self._suiveurs:
                suiveur.absorber(i, c)

            pivot = self._declenchement()
            if pivot is None:
                continue

            emis.append(pivot)
            self.pivots.append(pivot)
            # Nouvelle jambe : tout ce qui suit le pivot est rejoué avec le
            # sens opposé. Les bougies déjà absorbées peuvent en confirmer un
            # autre immédiatement — d'où la boucle.
            self._jambe = [x for x in self._jambe if x[0] > pivot.index]
            a_traiter = list(self._jambe)
            self._suiveurs = [_Suiveur(pivot.kind.opposite)]

        return emis

    def _declenchement(self) -> Pivot | None:
        """Le premier suiveur dont le retournement atteint le seuil.

        Tant qu'aucun pivot n'est encore confirmé, les deux sens sont suivis en
        parallèle : on ne sait pas si le marché a commencé par monter ou par
        descendre. Si les deux atteignent le seuil sur la même bougie, on retient
        l'extrême le plus ANCIEN — c'est celui qui s'est produit en premier. À
        égalité d'indice, le sommet l'emporte : arbitraire, mais déterministe,
        ce qu'exige le test-oracle §2.7.5.
        """
        candidats = [
            s for s in self._suiveurs
            if s.ext_index is not None
            and s.contre_index is not None
            and s.contre_index > s.ext_index
            and s.retournement_pct() >= self.seuil_pct
        ]
        if not candidats:
            return None
        candidats.sort(key=lambda s: (s.ext_index, 0 if s.kind is PivotKind.HAUT else 1))
        return candidats[0].pivot()

    @property
    def provisoire(self) -> Pivot | None:
        """L'extrême en cours de formation, PAS ENCORE confirmé.

        N'est là que pour la mesure de l'illusion (voir `zigzag_repeignant`).
        Aucune décision ne doit s'appuyer dessus : sa valeur et son indice
        changent encore, et c'est précisément ce qu'on appelle repeindre.
        """
        vivants = [s for s in self._suiveurs if s.ext_index is not None]
        if not vivants:
            return None
        suiveur = vivants[0] if len(vivants) == 1 else max(
            vivants, key=lambda s: s.retournement_pct()
        )
        return Pivot(
            kind=suiveur.kind, index=suiveur.ext_index, ts_sec=suiveur.ext_ts,
            price=suiveur.ext_price,
            # Le mensonge est ici : on prétend l'avoir su au moment même.
            confirmed_index=suiveur.ext_index, confirmed_ts_sec=suiveur.ext_ts,
        )


# --------------------------------------------------------------------------- #
# Fonctions
# --------------------------------------------------------------------------- #

def zigzag(candles: Sequence[Candle], seuil_pct: float) -> list[Pivot]:
    """Pivots CONFIRMÉS à la fin de la fenêtre reçue. La version honnête.

    Rejoue l'automate sur toute la fenêtre à chaque appel. C'est le prix de
    la commodité, et il est acceptable parce que la fenêtre d'une MarketView
    est bornée. En live, sur un flux continu, utiliser directement `ZigZag`
    et lui donner les bougies une par une : le coût devient constant par
    bougie au lieu de linéaire.
    """
    serie = verifier_serie(candles)
    automate = ZigZag(seuil_pct)
    for i, c in enumerate(serie):
        automate.consommer(i, c)
    return automate.pivots


def dernier_pivot(candles: Sequence[Candle], seuil_pct: float,
                  kind: PivotKind | None = None) -> Pivot | None:
    pivots = zigzag(candles, seuil_pct)
    if kind is not None:
        pivots = [p for p in pivots if p.kind is kind]
    return pivots[-1] if pivots else None


def distance_pivot_pct(candles: Sequence[Candle], seuil_pct: float) -> float | None:
    """Écart de la clôture au dernier pivot confirmé, en pourcentage.

    Feature `distance_pivot_pct` de la spec §3.1. Retourne `None` tant qu'aucun
    pivot n'est confirmé — au démarrage, cela peut durer longtemps, et c'est une
    information exacte : à ce moment-là, la stratégie ne SAIT rien des pivots.
    """
    pivot = dernier_pivot(candles, seuil_pct)
    if pivot is None or pivot.price == 0:
        return None
    return (candles[-1].close / pivot.price - 1) * 100


def zigzag_repeignant(candles: Sequence[Candle], seuil_pct: float) -> list[Pivot]:
    """VERSION MALHONNÊTE — instrument de mesure, jamais une source de décision.

    C'est ce que dessine une plateforme de graphiques : les pivots confirmés,
    plus l'extrême en cours présenté comme s'il était déjà acquis. Un backtest
    qui lit ce tableau connaît, à l'instant t, un sommet qui ne sera établi que
    plus tard.

    La spec §2.1 demande explicitement de la conserver :

        « Comparer les deux versions sur un même jeu de données : l'écart de
        performance entre la version repeignante et la version honnête vous
        donnera la mesure directe de l'illusion. »

    Sans cette comparaison, on ne sait pas si la stratégie a un edge ou si elle
    lisait l'avenir. Avec elle, l'écart chiffre exactement ce que valait
    l'illusion.

    `tests/test_layering.py` interdit l'import de cette fonction depuis
    `maxprofit/strategies/` : elle ne peut alimenter qu'un rapport de mesure.
    """
    serie = verifier_serie(candles)
    automate = ZigZag(seuil_pct)
    for i, c in enumerate(serie):
        automate.consommer(i, c)
    pivots = list(automate.pivots)
    provisoire = automate.provisoire
    if provisoire is not None and (not pivots or provisoire.index > pivots[-1].index):
        pivots.append(provisoire)
    return pivots
