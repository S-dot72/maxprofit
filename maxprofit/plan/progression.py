"""
L'échelle de mises d'une session — la progression du *Wealth Warriors Trade
Manager*, reproduite depuis la feuille.

--- Ce que la feuille calcule, retrouvé par rétro-ingénierie ----------------

Chaque mise rembourse la TOTALITÉ des pertes précédentes et rajoute toujours le
même gain visé :

    mise(n) = (cumul_perdu + gain_visé) / payout

Vérifié sur les sept lignes de la feuille, capital 250 $, payout 92 %, gain visé
1,46 $ — l'écart au centime près vient de l'arrondi d'affichage :

    pas 1   1,59 $      pas 5   30,07 $
    pas 2   3,31 $      pas 6   62,76 $
    pas 3   6,90 $      pas 7  130,97 $   <- le solde entier

Le rapport d'une mise à la suivante vaut 2,087 — ce n'est pas un réglage, c'est
`(1 + payout) / payout` qui tombe de la formule.

--- ⚠ Ce que la progression NE FAIT PAS ------------------------------------

**Elle ne change pas le taux de réussite nécessaire.** Mesuré :

    pari simple à 92 %        52,0833 %
    échelle à 2 pas           52,0833 %
    échelle à 7 pas           52,0833 %

L'invariance est exacte, et c'est une identité, pas une coïncidence : une
martingale redistribue les résultats — beaucoup de petits gains, et de temps en
temps une perte lourde — sans jamais déplacer l'espérance. Aucune gestion de
mise ne crée un avantage.

Ce qu'elle change, et c'est tout ce qu'elle change, c'est l'EXPOSITION :

    2 pas      4,90 $    1,96 % du capital
    3 pas     11,81 $    4,72 %
    7 pas    250,28 $  100,11 %   <- la liquidation

C'est pourquoi `pas_max` est un garde-fou et non un paramètre de confort.
"""

from __future__ import annotations

from dataclasses import dataclass

from maxprofit.core.errors import BotError

#: Profondeur d'échelle par défaut. **Deux**, et c'est une décision de sécurité
#: plutôt qu'un réglage : à sept pas, une session perdue coûte la totalité du
#: capital ; à deux pas, elle coûte 1,96 %.
#:
#: Un défaut est admis ici — contrairement au capital ou au payout, qui n'en ont
#: aucun (§5) — parce qu'il va dans le sens sûr. Se tromper vers 2 coûte des
#: gains manqués ; se tromper vers 7 coûte le compte.
PAS_MAX_PAR_DEFAUT = 2


@dataclass(frozen=True)
class Echelle:
    """Les mises successives d'une session, et ce qu'elles engagent.

    Immuable : une échelle décrit une règle, pas un état. L'état d'une session
    en cours vit dans `capital.Session`.
    """

    payout_pct: int
    gain_vise: float
    pas_max: int = PAS_MAX_PAR_DEFAUT

    def __post_init__(self) -> None:
        # Aucune valeur par défaut sur ce qui touche à l'argent (§5) : ces
        # trois-là sont fournis, ou l'objet n'existe pas.
        if not (1 <= self.payout_pct <= 100):
            raise BotError(
                f"payout_pct hors [1,100] : {self.payout_pct}. C'est le "
                f"pourcentage rendu par le broker en cas de gain, pas un "
                f"multiplicateur."
            )
        if self.gain_vise <= 0:
            raise BotError(f"gain_vise doit être positif : {self.gain_vise}")
        if self.pas_max < 1:
            raise BotError(f"pas_max doit valoir au moins 1 : {self.pas_max}")

    @property
    def payout(self) -> float:
        return self.payout_pct / 100

    def mises(self) -> tuple[float, ...]:
        """Les mises, du premier au dernier pas.

        Calculées, jamais tabulées : une table recopiée de la feuille serait
        fausse dès qu'on change le capital ou le payout, et elle le serait en
        silence.
        """
        sortie: list[float] = []
        cumul = 0.0
        for _ in range(self.pas_max):
            mise = (cumul + self.gain_vise) / self.payout
            sortie.append(mise)
            cumul += mise
        return tuple(sortie)

    def exposition(self) -> float:
        """Ce qu'une session perdue coûte : la somme de toutes les mises."""
        return sum(self.mises())

    def part_du_capital(self, capital: float) -> float:
        """L'exposition en pourcentage du capital. LE chiffre à regarder."""
        if capital <= 0:
            raise BotError(f"capital doit être positif : {capital}")
        return 100 * self.exposition() / capital

    def seuil_de_rentabilite_pct(self) -> float:
        """Le taux de réussite par TRADE en dessous duquel on perd.

        Ne dépend ni de `pas_max` ni du gain visé — seulement du payout. La
        méthode l'expose quand même, pour que le chiffre soit lisible à côté de
        la progression plutôt que dans une note qu'on ne relit pas.
        """
        return 100 / (1 + self.payout)

    def risque_de_session_perdue(self, taux_reussite_pct: float) -> float:
        """Probabilité de perdre TOUS les pas — donc l'exposition entière."""
        if not (0 <= taux_reussite_pct <= 100):
            raise BotError(
                f"taux_reussite_pct hors [0,100] : {taux_reussite_pct}")
        return (1 - taux_reussite_pct / 100) ** self.pas_max

    def esperance_par_session(self, taux_reussite_pct: float) -> float:
        """Gain moyen d'une session, en monnaie.

        Négative sous le seuil de rentabilité, et c'est le seul chiffre qui dit
        si la progression rapporte. Une courbe de solde qui monte pendant vingt
        sessions ne le dit pas : la perte est dans la queue.
        """
        risque = self.risque_de_session_perdue(taux_reussite_pct)
        return (1 - risque) * self.gain_vise - risque * self.exposition()
