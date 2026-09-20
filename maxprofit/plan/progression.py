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

#: Profondeur d'échelle par défaut. **Trois**, et c'est une décision de
#: sécurité plutôt qu'un réglage.
#:
#: Mesuré sur un plan 1/7 à 250 $ et 92 % de payout :
#:
#:     2 pas    4,90 $    1,96 % du solde
#:     3 pas   11,81 $    4,72 %      <- le défaut
#:     4 pas   24,22 $    9,69 %
#:     7 pas  250,28 $  100,11 %      <- la liquidation
#:
#: Un défaut est admis ici — contrairement au capital ou au payout, qui n'en
#: ont aucun (§5) — parce qu'il va dans le sens sûr. Se tromper vers 3 coûte
#: des gains manqués ; se tromper vers 7 coûte le compte.
PAS_MAX_PAR_DEFAUT = 3

#: Ce que l'utilisateur doit lire quand une session s'arrête sur sa profondeur.
#:
#: Le message dit la RAISON, pas le fait. « Session perdue » laisse croire à un
#: accident ; nommer la protection dit que le système a fait ce pour quoi il a
#: été réglé — et rappelle ce qu'il vient d'éviter.
#: ⚠ Caractères limités au latin-1 : pas d'exposant « ᵉ ». Ce message part
#: dans un journal, un terminal Windows et un message Telegram ; le premier
#: encodeur qui ne le comprend pas fait disparaître l'avertissement entier.
MESSAGE_PROTECTION = (
    "Pour la protection de votre capital, nous ne risquerons pas une "
    "{suivante}e perte. La descente s'arrête à {pas} pas et coûte {engage:.2f} "
    "{devise} ({part:.2f} % du solde). Sans cette limite, l'échelle irait "
    "jusqu'au {liquidation}e trade, qui engagerait la totalité du capital."
)


@dataclass(frozen=True)
class Risque:
    """Le niveau de risque, choisi en premier — « 1 sur 7 », « 3 sur 7 ».

    **C'est lui qui détermine le gain, et non l'inverse.** Le sens de lecture
    compte : on ne choisit pas un gain en espérant qu'il tienne dans le
    capital ; on choisit combien de pertes d'affilée le capital doit encaisser,
    et le gain par session en découle.

        1/7   le capital couvre exactement 7 pas — le 7ᵉ l'épuise
        3/7   on mise trois fois cette unité : plus gros gain, échelle plus
              courte, le capital ne tient plus que 5 pas

    Mesuré à 250 $ et 92 % de payout :

        plan   gain      gain %    pas tenables   ratio à 6 sessions
        1/7    1,46 $    0,583 %        7              3,50 %
        2/7    2,92 $    1,167 %        6              7,00 %
        3/7    4,38 $    1,750 %        5             10,50 %

    Le 1/7 reproduit la feuille au centime : 1,46 $, 0,58 %, 3,48 %.
    """

    #: Combien d'unités on mise. 1 = le plus prudent du dénominateur choisi.
    numerateur: int
    #: En combien de pas le capital serait épuisé à une unité.
    denominateur: int

    def __post_init__(self) -> None:
        if self.denominateur < 1:
            raise BotError(
                f"denominateur doit valoir au moins 1 : {self.denominateur}")
        if not (1 <= self.numerateur <= self.denominateur):
            raise BotError(
                f"numerateur hors [1,{self.denominateur}] : {self.numerateur}. "
                f"Un plan {self.numerateur}/{self.denominateur} miserait plus "
                f"que ce que le dénominateur définit comme le capital entier."
            )

    def __str__(self) -> str:
        return f"{self.numerateur}/{self.denominateur}"

    def unite(self, capital: float, payout_pct: int) -> float:
        """Le gain d'un plan 1/N : celui qui fait tenir EXACTEMENT N pas.

        Résolu plutôt que tabulé. Toutes les mises étant proportionnelles au
        gain, on déroule l'échelle avec un gain unitaire et l'on met à
        l'échelle par le capital — une table recopiée serait fausse dès qu'on
        change le payout, et elle le serait en silence.
        """
        if capital <= 0:
            raise BotError(f"capital doit être positif : {capital}")
        payout = payout_pct / 100
        cumul = 0.0
        for _ in range(self.denominateur):
            cumul += (cumul + 1.0) / payout
        return capital / cumul

    def gain_par_session(self, capital: float, payout_pct: int) -> float:
        return self.numerateur * self.unite(capital, payout_pct)

    def gain_par_session_pct(self, capital: float, payout_pct: int) -> float:
        """L'« Account Gain » de la feuille, déduit du risque choisi."""
        return 100 * self.gain_par_session(capital, payout_pct) / capital

    def pas_tenables(self, capital: float, payout_pct: int) -> int:
        """Combien de pas le capital encaisse réellement à ce niveau de risque.

        Vaut `denominateur` pour un plan 1/N, et moins dès que le numérateur
        monte : un gain plus gros épuise le capital plus vite.
        """
        gain = self.gain_par_session(capital, payout_pct)
        payout = payout_pct / 100
        cumul, pas = 0.0, 0
        while True:
            mise = (cumul + gain) / payout
            if cumul + mise > capital + 1e-9:
                return pas
            cumul += mise
            pas += 1


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
