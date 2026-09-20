"""
Structure de marché — ce que le PRIX fait, avant tout indicateur.

Le price action mène, les indicateurs confirment. Ce module porte donc les
briques qui ne lisent que des bougies : tendance, séries de bougies, zones
clés, order blocks. Les bandes de Bollinger y figurent aussi, pour une raison
précise — la stratégie a besoin de la VALEUR de la médiane, et
`oscillators.bollinger_percent_b` ne rend qu'une position relative.

--- ⚠ L'order block est non causal si l'on n'y prend pas garde -------------

Sa définition usuelle — *la dernière bougie de sens opposé avant l'impulsion
qui casse la structure* — ne peut être tranchée qu'APRÈS l'impulsion. Le
dessiner sur un graphique le fait apparaître rétroactivement à sa place, et
rien ne signale qu'il n'était pas connaissable sur le moment.

C'est exactement le piège du ZigZag, et la parade est la même : un pliage
causal qui émet le bloc avec DEUX indices — celui de la bougie qui le forme, et
celui de la cassure qui l'a révélé. `visible_a()` répond ensuite à la seule
question qui compte : *le savait-on à cet instant ?*

La latence est VARIABLE : une impulsion peut casser la structure deux bougies
après le bloc, ou trente. Aucun décalage fixe ne rendrait honnête un order
block calculé sur la série complète.
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass
from typing import Sequence

from maxprofit.core.errors import BotError
from maxprofit.core.types import Candle
from maxprofit.indicators.base import (
    Pivot,
    PivotKind,
    verifier_periode,
    verifier_serie,
)


# --------------------------------------------------------------------------- #
# Bandes de Bollinger — la valeur, pas la position
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class Bandes:
    basse: float
    mediane: float
    haute: float

    def largeur_pct(self) -> float:
        """Écartement des bandes, en % de la médiane. Mesure de régime."""
        if self.mediane == 0:
            raise BotError("Médiane nulle : largeur indéfinie.")
        return 100 * (self.haute - self.basse) / self.mediane


def bollinger_bandes(candles: Sequence[Candle], periode: int = 20,
                     ecarts: float = 2.0) -> Bandes | None:
    """Les trois bandes, en prix. `None` si la fenêtre est trop courte.

    `None` plutôt qu'un calcul sur ce qu'on a : une moyenne sur cinq bougies
    quand on en demande vingt répondrait quelque chose de plausible et de faux,
    et la stratégie déciderait dessus sans que rien ne le signale.

    L'écart-type est celui de la POPULATION (division par n), comme le veut la
    définition de Bollinger, et non celui de l'échantillon. La différence est
    faible mais elle déplace les bandes, donc les franchissements.
    """
    serie = verifier_serie(candles)
    periode = verifier_periode(periode, minimum=2)
    if ecarts <= 0:
        raise BotError(f"ecarts doit être positif : {ecarts}")
    if len(serie) < periode:
        return None

    fenetre = [c.close for c in serie[-periode:]]
    moyenne = sum(fenetre) / periode
    variance = sum((x - moyenne) ** 2 for x in fenetre) / periode
    ecart = variance ** 0.5
    return Bandes(
        basse=moyenne - ecarts * ecart,
        mediane=moyenne,
        haute=moyenne + ecarts * ecart,
    )


# --------------------------------------------------------------------------- #
# Tendance — la structure, pas la pente d'une moyenne
# --------------------------------------------------------------------------- #

class Tendance(enum.Enum):
    HAUSSIERE = "haussière"
    BAISSIERE = "baissière"
    #: Ni l'un ni l'autre. Un troisième état EXPLICITE, parce qu'une tendance
    #: absente n'est pas une tendance faible : on ne trade pas dedans.
    INDECISE = "indécise"

    def __str__(self) -> str:
        return self.value


def tendance(candles: Sequence[Candle], fenetre: int = 10) -> Tendance:
    """Haussière si plus-haut ET plus-bas montent d'une demi-fenêtre à l'autre.

    La définition du price action, et non la pente d'une moyenne mobile : une
    moyenne monte encore longtemps après que la structure a cassé, parce
    qu'elle traîne sa fenêtre derrière elle. Comparer deux moitiés de fenêtre
    répond à la question qu'on pose vraiment — *le marché fait-il des sommets
    et des creux plus hauts ?*

    Exiger les DEUX — sommets et creux — est ce qui distingue une tendance
    d'une expansion : un marché qui fait des sommets plus hauts et des creux
    plus bas n'est pas haussier, il s'élargit.
    """
    serie = verifier_serie(candles)
    fenetre = verifier_periode(fenetre, minimum=2)
    if len(serie) < 2 * fenetre:
        return Tendance.INDECISE

    recent, avant = serie[-fenetre:], serie[-2 * fenetre:-fenetre]
    haut_recent = max(c.high for c in recent)
    haut_avant = max(c.high for c in avant)
    bas_recent = min(c.low for c in recent)
    bas_avant = min(c.low for c in avant)

    if haut_recent > haut_avant and bas_recent > bas_avant:
        return Tendance.HAUSSIERE
    if haut_recent < haut_avant and bas_recent < bas_avant:
        return Tendance.BAISSIERE
    return Tendance.INDECISE


# --------------------------------------------------------------------------- #
# Séries de bougies
# --------------------------------------------------------------------------- #

def sens_bougie(candle: Candle) -> int:
    """+1 verte, -1 rouge, 0 doji.

    Le doji ROMPT une série, il ne la prolonge pas : une bougie sans corps
    n'exprime aucune pression, et la compter dans une série de rouges ferait
    dire à la série ce qu'elle ne dit pas.
    """
    if candle.close > candle.open:
        return 1
    if candle.close < candle.open:
        return -1
    return 0


def serie_de_bougies(candles: Sequence[Candle]) -> tuple[int, int]:
    """(sens, longueur) de la série qui se termine à la DERNIÈRE bougie.

    Rend `(0, 0)` si la dernière est un doji. La série est comptée à rebours
    depuis la fin, donc sans jamais regarder au-delà de la bougie courante.
    """
    serie = verifier_serie(candles)
    sens = sens_bougie(serie[-1])
    if sens == 0:
        return 0, 0
    longueur = 0
    for candle in reversed(serie):
        if sens_bougie(candle) != sens:
            break
        longueur += 1
    return sens, longueur


def serie_avant_la_derniere(candles: Sequence[Candle]) -> tuple[int, int]:
    """La série qui précède immédiatement la dernière bougie.

    C'est ce dont la stratégie a besoin : *« une bougie verte APRÈS une série
    de rouges »*. La série concernée est celle qui se termine à l'avant-
    dernière bougie, pas celle qui contient la dernière.
    """
    serie = verifier_serie(candles)
    if len(serie) < 2:
        return 0, 0
    return serie_de_bougies(serie[:-1])


# --------------------------------------------------------------------------- #
# Order blocks — pliage causal
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class OrderBlock:
    """Une zone laissée par la dernière bougie opposée avant une impulsion.

    `kind` dit de quel côté la zone agit :

        BAS    zone de DEMANDE, sous le prix — elle est censée soutenir
        HAUT   zone d'OFFRE, au-dessus — elle est censée freiner

    Deux indices, comme un `Pivot`, et pour la même raison.
    """

    kind: PivotKind
    index: int
    ts_sec: int
    haut: float
    bas: float
    confirmed_index: int
    confirmed_ts_sec: int

    def __post_init__(self) -> None:
        if self.confirmed_index < self.index:
            raise BotError(
                f"Order block confirmé ({self.confirmed_index}) avant de "
                f"s'être formé ({self.index}) : c'est la définition du "
                f"look-ahead."
            )
        if self.haut < self.bas:
            raise BotError(f"Zone inversée : haut={self.haut} bas={self.bas}")

    @property
    def latence(self) -> int:
        """Bougies entre la formation du bloc et la cassure qui l'a révélé."""
        return self.confirmed_index - self.index

    def visible_a(self, index: int) -> bool:
        """Le savait-on à cet instant ? La seule question qui compte."""
        return self.confirmed_index <= index

    def contient(self, prix: float) -> bool:
        return self.bas <= prix <= self.haut


def order_blocks(candles: Sequence[Candle], fenetre: int = 10,
                 recul_max: int = 10) -> list[OrderBlock]:
    """Les order blocks, émis au moment où la cassure les révèle.

    L'automate consomme les bougies une par une :

    1. il tient le plus haut et le plus bas des `fenetre` bougies PRÉCÉDENTES ;
    2. quand une clôture les dépasse, la structure est cassée ;
    3. il remonte alors d'au plus `recul_max` bougies pour trouver la dernière
       de sens opposé — c'est elle qui forme le bloc.

    `recul_max` borne la recherche. Sans lui, une cassure survenant après une
    longue tendance irait chercher un bloc vieux de cent bougies, que plus
    personne ne regarde. La borne est un choix, donc un paramètre.

    **Aucun accès aux bougies suivantes.** À l'étape 3 on ne lit que le passé
    de la cassure, et la cassure est l'instant courant.
    """
    serie = verifier_serie(candles)
    fenetre = verifier_periode(fenetre, minimum=2)
    recul_max = verifier_periode(recul_max, minimum=1)

    blocs: list[OrderBlock] = []
    precedentes: deque[Candle] = deque(maxlen=fenetre)

    for i, courante in enumerate(serie):
        if len(precedentes) == fenetre:
            haut = max(c.high for c in precedentes)
            bas = min(c.low for c in precedentes)

            if courante.close > haut:
                forme = _dernier_oppose(serie, i, -1, recul_max)
                if forme is not None:
                    blocs.append(_bloc(serie, forme, i, PivotKind.BAS))
            elif courante.close < bas:
                forme = _dernier_oppose(serie, i, +1, recul_max)
                if forme is not None:
                    blocs.append(_bloc(serie, forme, i, PivotKind.HAUT))

        precedentes.append(courante)

    return blocs


def order_blocks_repeignants(candles: Sequence[Candle], fenetre: int = 10,
                             recul_max: int = 10) -> list[OrderBlock]:
    """⚠ LA VERSION MALHONNÊTE. Elle n'existe que pour être mesurée.

    Mêmes blocs, mais datés de leur FORMATION au lieu de leur révélation :
    `confirmed_index = index`. C'est ce que fait tout outil qui dessine les
    order blocks sur un graphique — la zone apparaît à sa place, et rien ne
    dit qu'elle n'était pas connaissable sur le moment.

    Un backtest qui l'utilise éviterait des résistances révélées trente
    bougies plus tard. Le résultat serait spectaculaire et entièrement faux.

    **Elle doit ÉCHOUER le test de causalité.** C'est son unique fonction :
    prouver que le test a un pouvoir de détection. Un contrôle qu'aucun
    contre-exemple ne fait rougir ne contrôle rien. Même dispositif que
    `zigzag_repeignant`.

    Interdite hors des tests par `tests/test_layering.py`.
    """
    honnetes = order_blocks(candles, fenetre=fenetre, recul_max=recul_max)
    return [
        OrderBlock(
            kind=b.kind, index=b.index, ts_sec=b.ts_sec,
            haut=b.haut, bas=b.bas,
            confirmed_index=b.index,          # <- le mensonge, ici et nulle part ailleurs
            confirmed_ts_sec=b.ts_sec,
        )
        for b in honnetes
    ]


def _dernier_oppose(serie, cassure: int, sens: int,
                    recul_max: int) -> int | None:
    """Indice de la dernière bougie de `sens` avant la cassure, ou `None`."""
    depart = max(0, cassure - recul_max)
    for j in range(cassure - 1, depart - 1, -1):
        if sens_bougie(serie[j]) == sens:
            return j
    return None


def _bloc(serie, index: int, cassure: int, kind: PivotKind) -> OrderBlock:
    c = serie[index]
    return OrderBlock(
        kind=kind,
        index=index,
        ts_sec=c.ts_sec,
        haut=c.high,
        bas=c.low,
        confirmed_index=cassure,
        confirmed_ts_sec=serie[cassure].ts_sec,
    )


# --------------------------------------------------------------------------- #
# Zones clés — ce qui pourrait empêcher le marché d'aller plus loin
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class Zone:
    """Un niveau qui a déjà arrêté le prix, et à quelle distance il se trouve."""

    kind: PivotKind
    prix: float
    distance_pct: float
    origine: str            # "fractale" ou "order_block"


def zones_au_dessus(candles: Sequence[Candle], prix: float,
                    pivots: Sequence[Pivot] = (),
                    blocs: Sequence[OrderBlock] = (),
                    marge_pct: float = 0.10) -> list[Zone]:
    """Les zones connaissables MAINTENANT qui plafonnent à moins de `marge_pct`.

    C'est la condition que la description appelle *« les zones clés à proximité
    qui pourraient empêcher le marché de monter »*.

    Seuls les pivots et blocs **visibles à la dernière bougie** sont retenus —
    `visible_a()` fait le tri. Sans ce filtre, la stratégie éviterait une
    résistance que personne ne pouvait connaître, et son backtest serait
    flatteur pour une raison invisible à la relecture.
    """
    serie = verifier_serie(candles)
    if prix <= 0:
        raise BotError(f"prix doit être positif : {prix}")
    if marge_pct <= 0:
        raise BotError(f"marge_pct doit être positive : {marge_pct}")
    maintenant = len(serie) - 1

    trouvees: list[Zone] = []
    for pivot in pivots:
        if pivot.kind is PivotKind.HAUT and pivot.visible_a(maintenant):
            _ajouter(trouvees, pivot.price, prix, marge_pct, "fractale")
    for bloc in blocs:
        if bloc.kind is PivotKind.HAUT and bloc.visible_a(maintenant):
            # Le BAS du bloc d'offre : c'est là que le prix rencontre la zone.
            _ajouter(trouvees, bloc.bas, prix, marge_pct, "order_block")
    return sorted(trouvees, key=lambda z: z.distance_pct)


def _ajouter(sortie: list[Zone], niveau: float, prix: float, marge_pct: float,
             origine: str) -> None:
    if niveau <= prix:
        return                      # déjà franchi : ce n'est plus un plafond
    distance = 100 * (niveau - prix) / prix
    if distance <= marge_pct:
        sortie.append(Zone(PivotKind.HAUT, niveau, distance, origine))
