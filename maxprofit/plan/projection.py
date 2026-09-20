"""
Le planning — *30-Day Compounder*, et la comparaison au réel.

--- Ce que la feuille projette ---------------------------------------------

    Capital initial   250,00 $
    Gain par session    0,58 %
    Sessions par jour   6
    Ratio journalier    3,48 %        <- 0,58 × 6, une SOMME
    Jour 30           697,64 $

Reproduit ici au centime : jour 1 → 258,70 $, jour 15 → 417,62 $, jour 30 →
697,64 $.

--- ⚠ Ce qu'une projection n'est pas ---------------------------------------

**Ce n'est pas une prévision, c'est une hypothèse rendue lisible.** La courbe
suppose que chaque journée atteint son objectif — autrement dit qu'aucune
session n'est perdue au-delà de ce que le gain visé absorbe. La feuille ne
porte nulle part la probabilité que cela tienne.

C'est pourquoi `ecart_au_plan()` existe : une projection sans comparaison au
réel se lit comme une promesse. Comparée, elle redevient ce qu'elle est — une
référence contre laquelle on mesure.

> À 49,11 % de réussite, l'espérance par session est négative. La courbe
> ci-dessous monte quand même : elle décrit ce qui arriverait si le plan était
> tenu, pas ce qui arrive. Les deux ne se rejoignent que si le taux de réussite
> dépasse 52,0833 %.
"""

from __future__ import annotations

from dataclasses import dataclass

from maxprofit.core.errors import BotError
from maxprofit.plan.capital import PlanCapital


@dataclass(frozen=True)
class JourProjete:
    """Une ligne du planning."""

    jour: int
    gain_du_jour: float
    gain_cumule: float
    solde: float


def projeter(plan: PlanCapital, compose: bool = False) -> list[JourProjete]:
    """La courbe du planning, jour par jour.

    `compose=False` reproduit la feuille : le gain du jour est une fraction du
    solde d'OUVERTURE, les sessions ne se composant pas entre elles. C'est le
    défaut parce que c'est ce que le fichier calcule, et qu'une projection qui
    ne correspond pas à son original ne sert pas de référence.

    `compose=True` donne le comportement réel du *Trade Manager*, dont chaque
    session mise sur le solde courant. L'écart est de 0,05 point par jour en
    faveur de la composition — la feuille sous-estime légèrement.
    """
    ratio = plan.ratio_journalier_pct(compose=compose) / 100
    solde = plan.capital_initial
    cumule = 0.0
    lignes: list[JourProjete] = []
    for jour in range(1, plan.jours + 1):
        gain = solde * ratio
        cumule += gain
        solde += gain
        lignes.append(JourProjete(jour, gain, cumule, solde))
    return lignes


def solde_projete(plan: PlanCapital, jour: int, compose: bool = False) -> float:
    """Le solde attendu à la fin du jour `jour`. `jour=0` = le capital."""
    if jour < 0:
        raise BotError(f"jour ne peut pas être négatif : {jour}")
    if jour == 0:
        return plan.capital_initial
    if jour > plan.jours:
        raise BotError(
            f"le plan ne couvre que {plan.jours} jours, demandé : {jour}")
    return projeter(plan, compose=compose)[jour - 1].solde


@dataclass(frozen=True)
class EcartAuPlan:
    """Où l'on en est par rapport au planning. Le seul chiffre qui juge."""

    jour: int
    solde_reel: float
    solde_projete: float

    @property
    def ecart(self) -> float:
        return self.solde_reel - self.solde_projete

    @property
    def ecart_pct(self) -> float:
        return 100 * self.ecart / self.solde_projete

    @property
    def en_avance(self) -> bool:
        return self.ecart >= 0

    def resume(self) -> str:
        sens = "en avance" if self.en_avance else "en retard"
        return (
            f"Jour {self.jour} : {self.solde_reel:.2f} $ contre "
            f"{self.solde_projete:.2f} $ prévus — {sens} de "
            f"{abs(self.ecart):.2f} $ ({abs(self.ecart_pct):.1f} %)"
        )


def ecart_au_plan(plan: PlanCapital, jour: int, solde_reel: float,
                  compose: bool = False) -> EcartAuPlan:
    """Compare le réel au planning.

    Existe pour que la courbe cesse d'être une promesse. Un planning qu'on
    n'oppose jamais au solde réel finit par être lu comme un relevé.
    """
    return EcartAuPlan(
        jour=jour,
        solde_reel=solde_reel,
        solde_projete=solde_projete(plan, jour, compose=compose),
    )
