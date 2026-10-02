"""
La prise de liquidité : manipulation d'une zone M1, puis retournement.

--- D'où elle vient --------------------------------------------------------

De l'utilisateur, le 2026-10-02 :

  « La stratégie n'arrive pas à déterminer la prise de liquidité avec
    manipulation d'une zone et retournement. »
  « On doit vérifier la prise de liquidité en M1 : c'est vrai pour une pique
    plus haute ou plus basse avec deux bougies opposées, avec des mèches en
    haut ou en bas — mais elles peuvent aussi ne pas en avoir. Il faut
    également surveiller la manipulation du marché. »
  Tendance : « dans le sens H1 ».

ZoneH1 faisait l'inverse de ce qu'il faut sur deux points : elle ne
déclenche que si le CORPS touche la zone — une longue mèche qui va chercher
les stops passe inaperçue — et elle déclare morte une zone qu'une clôture
traverse — alors qu'une fausse cassure suivie d'un retour est justement la
manipulation à guetter.

--- La règle -----------------------------------------------------------------

1. La LIQUIDITÉ : les piques M1 de ZoneH1 (deux bougies opposées, extrême
   des corps sur ±12 bougies). Les stops dorment au-delà de leurs MÈCHES :
   le niveau est le plus haut des deux bougies pour un sommet, le plus bas
   pour un creux — leur corps quand elles n'ont pas de mèche.
2. Une liquidité FRAÎCHE : aucune bougie n'est encore allée au-delà depuis
   que la pique est formée.
3. La MANIPULATION : dans les `manipulation_max` + 1 dernières bougies, le
   prix est passé au-delà du niveau — en mèche, ou en clôture pour au plus
   `manipulation_max` bougies.
4. Le RETOUR et le RETOURNEMENT : la dernière bougie se referme de l'autre
   côté du niveau, va dans le sens du trade et casse l'extrême de la
   précédente.
5. Le SENS H1 : on vend une prise au-dessus d'un sommet si l'H1 baisse, on
   achète une prise sous un creux si l'H1 monte.

--- ⚠ Rien n'est établi ---------------------------------------------------

C'est une hypothèse, jugée dans `/laboratoire` avant de trader.
"""

from __future__ import annotations

from maxprofit.core.market_view import MarketView
from maxprofit.core.types import ConditionResult, Direction, Evaluation, Signal
from maxprofit.strategies.zone_h1 import PARAMETRES_PRE_INSCRITS, ZoneH1

#: Bougies qui peuvent clôturer au-delà du niveau sans que la prise devienne
#: une vraie cassure.
MANIPULATION_MAX = 2


class PriseDeLiquidite(ZoneH1):
    """Prise de liquidité sur une pique M1, retour, retournement, sens H1."""

    name = "prise_de_liquidite_m1"

    def __init__(self, p=PARAMETRES_PRE_INSCRITS,
                 manipulation_max: int = MANIPULATION_MAX):
        super().__init__(p)
        self.manipulation_max = manipulation_max

    @property
    def params(self):
        return {**super().params, "manipulation_max": self.manipulation_max}

    def evaluer(self, view: MarketView) -> Evaluation:
        bougies = view.candles(self.p.lookback)
        ts_ms = view.now_ms
        assez = len(bougies) >= self.p.lookback
        sens_bougie = 0
        if assez:
            c, prec = bougies[-1], bougies[-2]
            if c.close < c.open and c.close < prec.low:
                sens_bougie = -1
            elif c.close > c.open and c.close > prec.high:
                sens_bougie = +1
        hausse = None
        niveau = None
        if sens_bougie:
            hausse = self._tendance_h1_haussiere(bougies)
            niveau = self._prise(bougies, sens_bougie)
        aligne = (niveau is not None and hausse is not None
                  and (sens_bougie > 0) == hausse)
        direction = None
        if sens_bougie:
            direction = Direction.CALL if sens_bougie > 0 else Direction.PUT
        conditions = (
            ConditionResult("historique_suffisant", assez,
                            float(len(bougies))),
            ConditionResult("retournement_m1", sens_bougie != 0, None),
            ConditionResult("liquidite_prise_puis_rendue", niveau is not None,
                            None if niveau is None else float(niveau)),
            ConditionResult("sens_aligne_sur_h1", aligne, None),
        )
        signal = None
        if aligne:
            signal = Signal(
                pair=view.pair, direction=direction, decided_at_ms=ts_ms,
                expiry_sec=self.p.expiry_sec,
                features={"niveau": float(niveau)},
                reason="liquidité prise au-delà d'une pique M1, retour et "
                       "retournement, dans le sens H1")
        return Evaluation(pair=view.pair, ts_ms=ts_ms,
                          direction_envisagee=direction,
                          conditions=conditions,
                          features={"niveau": float(niveau or 0.0)},
                          signal=signal)

    def _prise(self, bougies, sens: int) -> float | None:
        """Le niveau de liquidité pris puis rendu, ou `None`.

        `sens` est celui du trade : -1 (vente) cherche une prise au-dessus
        d'un sommet, +1 (achat) une prise sous un creux.
        """
        w = self.p.fenetre_pique
        i = len(bougies) - 1
        debut_manip = i - self.manipulation_max
        derniere = bougies[i]
        meilleur = None
        for conf, _prix, s in self._zones(bougies):
            # Une résistance (s < 0) porte une liquidité d'acheteurs piégés
            # au-dessus : elle donne une VENTE. Un support, un achat.
            if s != sens or conf >= debut_manip or i - conf > self.p.memoire:
                continue
            a, b = bougies[conf - w], bougies[conf - w + 1]
            if s < 0:
                niveau = max(a.high, b.high)
                au_dela = lambda x: x.high > niveau          # noqa: E731
                clos_au_dela = lambda x: x.close > niveau    # noqa: E731
                rendu = derniere.close < niveau
            else:
                niveau = min(a.low, b.low)
                au_dela = lambda x: x.low < niveau           # noqa: E731
                clos_au_dela = lambda x: x.close < niveau    # noqa: E731
                rendu = derniere.close > niveau
            if not rendu:
                continue
            # Fraîche : personne n'est encore allé chercher ces stops.
            if any(au_dela(bougies[k]) for k in range(conf + 1, debut_manip)):
                continue
            fenetre = bougies[debut_manip:i + 1]
            if not any(au_dela(x) for x in fenetre):
                continue
            if sum(1 for x in fenetre if clos_au_dela(x)) \
                    > self.manipulation_max:
                continue
            # La plus récente : c'est la liquidité que le marché vient de
            # prendre.
            if meilleur is None or conf > meilleur[0]:
                meilleur = (conf, niveau)
        return None if meilleur is None else meilleur[1]
