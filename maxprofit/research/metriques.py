"""
Ce qu'on mesure, et pourquoi le taux de réussite n'en fait pas partie seul.

--- ⚠ Le taux de réussite ne dit rien tout seul ----------------------------

Mesuré sur la collecte réelle, en pariant le sens le plus fréquent de chaque
palier de payout :

    payout   réussite   seuil     espérance
    92 %      50,51 %   52,08 %   -0,030 $/$
    < 30 %    53,25 %   79,68 %   -0,335 $/$

La ligne du bas a le MEILLEUR taux de réussite et la PIRE espérance. Un
tableau de bord qui n'afficherait que « 53,25 % » désignerait le pire moment
de la journée comme le meilleur.

C'est pourquoi rien ici ne rend un taux de réussite sans son payout. Le payout
est une donnée d'entrée obligatoire, jamais un défaut (§5) : une espérance
calculée avec un payout supposé est un chiffre faux qui a l'air d'un vrai.

--- Les deux seuils à ne pas confondre -------------------------------------

    seuil_de_rentabilite(92)  =  52,0833 %   il faut le DÉPASSER
    seuil_de_rentabilite(72)  =  58,1395 %   le payout moyen réellement observé

Le module `plan.progression` porte le même calcul pour la martingale, et il y
démontre que la profondeur d'échelle ne le déplace pas. Les deux implémentations
sont volontairement séparées : `plan` dimensionne des mises, `research` juge des
hypothèses, et fusionner les deux ferait dépendre un verdict de recherche d'un
réglage de gestion de capital.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from maxprofit.core.errors import BotError


def seuil_de_rentabilite_pct(payout_pct: float) -> float:
    """Le taux de réussite en dessous duquel on perd, pour ce payout.

    `100 / (1 + payout)`. Aucun défaut : le payout se lit au moment du signal
    et se transmet, il ne se suppose pas.
    """
    if not (0 < payout_pct <= 200):
        raise BotError(
            f"payout_pct hors ]0,200] : {payout_pct}. C'est le pourcentage "
            f"rendu par le broker en cas de gain, pas un multiplicateur.")
    return 100 / (1 + payout_pct / 100)


def esperance(reussite_pct: float, payout_pct: float) -> float:
    """Gain moyen par dollar misé. LE chiffre qui juge.

        EV = p × payout - (1 - p)

    Négative sous le seuil, positive au-dessus, nulle exactement dessus.
    """
    if not (0 <= reussite_pct <= 100):
        raise BotError(f"reussite_pct hors [0,100] : {reussite_pct}")
    p = reussite_pct / 100
    return p * (payout_pct / 100) - (1 - p)


@dataclass(frozen=True)
class Resultat:
    """Le résultat d'une hypothèse sur un échantillon. Immuable : un résultat
    se recalcule, il ne se corrige pas."""

    #: Nombre de signaux. Sans lui, un taux de réussite n'est pas une mesure.
    signaux: int
    #: Nombre de gains. Une égalité est une PERTE : le broker ne rembourse pas.
    gains: int
    #: Payout MOYEN réellement disponible au moment des entrées, pas le payout
    #: affiché en vitrine ni le meilleur observé.
    payout_moyen_pct: float

    def __post_init__(self) -> None:
        if self.signaux < 0:
            raise BotError(f"signaux négatif : {self.signaux}")
        if not (0 <= self.gains <= self.signaux):
            raise BotError(
                f"gains hors [0,{self.signaux}] : {self.gains}")

    @property
    def reussite_pct(self) -> float:
        if not self.signaux:
            raise BotError(
                "Aucun signal : le taux de réussite n'existe pas. Rendre 0 ou "
                "50 laisserait croire à une mesure là où il n'y a rien.")
        return 100 * self.gains / self.signaux

    @property
    def seuil_pct(self) -> float:
        return seuil_de_rentabilite_pct(self.payout_moyen_pct)

    @property
    def esperance(self) -> float:
        return esperance(self.reussite_pct, self.payout_moyen_pct)

    @property
    def ecart_type_pct(self) -> float:
        """Écart-type du taux observé, sous l'hypothèse de tirages
        indépendants. Vaut pour des signaux qui ne se chevauchent pas ; à
        échéance 15 min prise chaque minute, les fenêtres partagent 14/15 de
        leur contenu et cet écart-type est sous-estimé d'un facteur ~√15.
        C'est à l'échantillonnage de régler ça, pas à cette formule."""
        if not self.signaux:
            raise BotError("Aucun signal : pas d'écart-type.")
        return 100 * math.sqrt(0.25 / self.signaux)

    @property
    def sigma_au_seuil(self) -> float:
        """De combien d'écarts-types on dépasse le seuil de rentabilité.

        C'est ce nombre qu'il faut regarder, et non l'écart au hasard (50 %) :
        battre le hasard ne rapporte rien, battre le seuil oui.
        """
        return (self.reussite_pct - self.seuil_pct) / self.ecart_type_pct

    @property
    def rentable(self) -> bool:
        return self.esperance > 0

    def resume(self) -> str:
        return (
            f"{self.signaux} signaux, {self.reussite_pct:.2f} % de réussite, "
            f"payout {self.payout_moyen_pct:.1f} % (seuil {self.seuil_pct:.2f} "
            f"%) -> espérance {self.esperance:+.4f} $/$ "
            f"({self.sigma_au_seuil:+.2f} sigma)")


def signaux_pour_etablir(reussite_visee_pct: float, payout_pct: float,
                         sigmas: float = 3.0) -> int:
    """Combien de signaux il faut pour ÉTABLIR une précision, pas l'espérer.

    Répond à la question qui décide où chercher. Mesuré à 88 % de payout :

        55 %  ->  6 802 signaux
        60 %  ->    466
        65 %  ->    147
        75 %  ->     36

    Un avantage marginal demande un échantillon qu'on n'aura jamais ; un
    avantage fort se tranche en une poignée de signaux. C'est l'argument pour
    chercher RARE ET PRÉCIS plutôt que fréquent et tiède — et l'explication du
    silence du banc d'essai, dont les hypothèses tirent 7 000 à 45 000 signaux
    et vivent toutes entre 49 et 52 %.
    """
    seuil = seuil_de_rentabilite_pct(payout_pct)
    if reussite_visee_pct <= seuil:
        raise BotError(
            f"{reussite_visee_pct} % ne bat pas le seuil de {seuil:.2f} % à "
            f"{payout_pct} % de payout : aucun nombre de signaux ne rendra "
            f"cette hypothèse rentable.")
    if sigmas <= 0:
        raise BotError(f"sigmas doit être positif : {sigmas}")
    p = reussite_visee_pct / 100
    ecart = p - seuil / 100
    return math.ceil((sigmas * math.sqrt(p * (1 - p)) / ecart) ** 2)


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[float]:
    """Les p corrigées du nombre de tests, méthode de Benjamini-Hochberg.

    Contrôle le taux de FAUSSES DÉCOUVERTES : parmi ce qu'on déclare trouvé,
    la part attendue d'erreurs. C'est le bon contrôle ici — on ne cherche pas à
    ne jamais se tromper, on cherche à ne pas bâtir sur du vent.

    ⚠ Le nombre de tests à corriger est celui du REGISTRE, pas celui du script
    en cours. Corriger par les 46 hypothèses d'une seule exécution, après en
    avoir essayé six cents la semaine précédente, ne corrige rien. C'est la
    raison d'être de `research.registre`.
    """
    if not p_values:
        return []
    if not (0 < alpha < 1):
        raise BotError(f"alpha hors ]0,1[ : {alpha}")
    for p in p_values:
        if not (0 <= p <= 1):
            raise BotError(f"p hors [0,1] : {p}")
    n = len(p_values)
    ordre = sorted(range(n), key=lambda i: p_values[i])
    corrigees = [0.0] * n
    minimum = 1.0
    for rang in range(n - 1, -1, -1):
        i = ordre[rang]
        minimum = min(minimum, p_values[i] * n / (rang + 1))
        corrigees[i] = min(1.0, minimum)
    return corrigees
