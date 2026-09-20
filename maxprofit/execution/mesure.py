"""
Ce que le journal d'exécution répond, et en quelle unité.

--- ⚠ Un glissement ne se lit pas en prix, il se lit en POINTS DE TAUX -----

« Le glissement moyen est de 0,000012 » ne veut rien dire. La seule question
est : combien de trades cela fait-il basculer de gagnant à perdant ?

La traduction est possible, et elle est simple. Sur une échéance courte, le
prix final moins le prix d'entrée est approximativement gaussien centré,
d'écart-type σ. Décaler l'entrée de δ contre soi déplace le seuil de décision
de δ, et la probabilité de gagner passe de 50 % à :

    P = Φ(-δ / σ)   soit, pour δ petit,   50 % - 39,9 × (δ / σ) points

Le facteur 0,399 est φ(0), la densité de la loi normale en zéro. Aucun réglage
là-dedans : c'est de la géométrie.

**L'ordre de grandeur décide de tout.** À 60 s sur ces paires, σ vaut environ
0,03 % du prix. Un glissement d'un dixième de σ coûte donc ~4 points de taux
de réussite — quand le seuil à battre n'est qu'à 2,08 points au-dessus du
hasard. Autrement dit : une exécution médiocre ne réduit pas un avantage, elle
le rend inatteignable.

C'est pourquoi cette mesure passe AVANT la recherche d'une stratégie, et non
après.
"""

from __future__ import annotations

import math
import statistics as stats
from dataclasses import dataclass
from typing import Sequence

from maxprofit.core.errors import BotError
from maxprofit.execution.journal import Execution

#: φ(0), densité de la loi normale centrée réduite en zéro. Le facteur qui
#: convertit « décalage en écarts-types » en « points de taux de réussite ».
DENSITE_EN_ZERO = 1 / math.sqrt(2 * math.pi)


@dataclass(frozen=True)
class Constat:
    """Une mesure et ce qu'elle coûte. Jamais l'une sans l'autre."""

    nom: str
    #: Ce qui a été observé, dans son unité naturelle.
    valeur: float
    unite: str
    #: Nombre d'ordres sur lesquels la mesure repose.
    n: int
    #: Dispersion, pour savoir si la moyenne veut dire quelque chose.
    ecart_type: float
    #: Le coût traduit en POINTS DE TAUX DE RÉUSSITE. `None` quand la mesure
    #: ne se traduit pas ainsi (un taux de refus, par exemple).
    cout_points: float | None
    verdict: str

    def resume(self) -> str:
        cout = ("" if self.cout_points is None
                else f"  ->  {self.cout_points:+.2f} point(s) de taux")
        return (f"{self.nom:<34}{self.valeur:>12.6f} {self.unite:<10}"
                f"n={self.n:<5}{cout}\n{'':34}{self.verdict}")


def _moyenne_et_ecart(valeurs: Sequence[float]) -> tuple[float, float]:
    if not valeurs:
        raise BotError("Aucune valeur : il n'y a rien à moyenner.")
    if len(valeurs) == 1:
        return valeurs[0], 0.0
    return stats.fmean(valeurs), stats.stdev(valeurs)


def points_de_taux(glissement: float, sigma_horizon: float) -> float:
    """Convertit un glissement de prix en points de taux de réussite.

    ⚠ `glissement` est SIGNÉ EN FAVEUR DU PARI, comme le rend
    `Execution.glissement` : positif quand l'entrée obtenue est meilleure que
    celle attendue. Le résultat suit le même signe — positif = points gagnés.

    Le sens compte, et il s'est trompé une fois : la formule était écrite pour
    un δ « décalage CONTRE soi », appliquée à une entrée déjà signée en faveur.
    Les deux conventions se composaient et une exécution favorable était
    comptée comme un coût. Deux tests tiennent maintenant les deux sens.

    `sigma_horizon` est l'écart-type du mouvement de prix sur l'échéance, dans
    la MÊME unité que le glissement. Il est mesuré sur les données, pas
    supposé : c'est lui qui donne son échelle au résultat, et le supposer
    reviendrait à choisir la réponse.
    """
    if sigma_horizon <= 0:
        raise BotError(
            f"sigma_horizon doit être positif : {sigma_horizon}. Sans lui, un "
            f"glissement n'a pas d'échelle et ne se traduit pas.")
    return 100 * DENSITE_EN_ZERO * glissement / sigma_horizon


def e1_payout(executions: Sequence[Execution]) -> Constat:
    """Le payout appliqué est-il celui lu dans le flux ?

    Traduit directement : un point de payout en moins déplace le seuil de
    rentabilité d'environ 0,27 point à 92 %. Ce n'est pas un coût sur le taux
    de réussite mais sur le SEUIL, ce que le verdict dit explicitement.
    """
    ecarts = [e.ecart_payout_pct for e in executions
              if e.ecart_payout_pct is not None]
    if not ecarts:
        return Constat("E1 payout flux vs broker", 0.0, "pt", 0, 0.0, None,
                       "aucun ordre n'a rendu de payout : indéterminé")
    moyenne, ecart = _moyenne_et_ecart(ecarts)
    # d(seuil)/d(payout) = -100/(1+p)^2, évalué au payout observé.
    reference = stats.fmean(
        e.payout_broker_pct for e in executions
        if e.payout_broker_pct is not None)
    sensibilite = 100 / (1 + reference / 100) ** 2 / 100
    decalage_seuil = -moyenne * sensibilite
    verdict = (
        "le flux dit vrai" if abs(moyenne) < 0.5 else
        f"le broker applique {abs(moyenne):.2f} pt de "
        f"{'MOINS' if moyenne < 0 else 'plus'} : le seuil de rentabilité "
        f"bouge de {decalage_seuil:+.2f} pt, et tout backtest calculé sur le "
        f"payout du flux est faux d'autant")
    return Constat("E1 payout flux vs broker", moyenne, "pt", len(ecarts),
                   ecart, None, verdict)


def e2_glissement(executions: Sequence[Execution],
                  sigma_horizon: float) -> Constat:
    """Le prix d'entrée est-il celui qu'on croyait ?

    `sigma_horizon` vient des données collectées, pas d'une constante : c'est
    l'écart-type du mouvement de prix sur l'échéance testée.
    """
    glissements = [e.glissement for e in executions if e.glissement is not None]
    if not glissements:
        return Constat("E2 glissement à l'entrée", 0.0, "prix", 0, 0.0, None,
                       "aucun prix d'entrée rendu : indéterminé")
    moyenne, ecart = _moyenne_et_ecart(glissements)
    cout = points_de_taux(moyenne, sigma_horizon)
    verdict = (
        "l'entrée est là où le backtest la place" if abs(cout) < 0.5 else
        f"l'entrée coûte {abs(cout):.2f} point(s) de taux de réussite, à "
        f"comparer aux 2,08 points qui séparent le hasard du seuil à 92 %")
    return Constat("E2 glissement à l'entrée", moyenne, "prix",
                   len(glissements), ecart, cout, verdict)


def e3_latence(executions: Sequence[Execution]) -> Constat:
    """Combien de temps entre « entre » et « c'est pris » ?

    La latence n'a pas de coût en soi : son coût est le glissement qu'elle
    produit, et c'est E2 qui le porte. Elle est mesurée à part parce qu'elle
    est la seule des quatre qu'on puisse RÉDUIRE — en rapprochant le code du
    broker, en supprimant une attente, en pré-calculant la décision.
    """
    latences = [e.latence_ms for e in executions if e.latence_ms is not None]
    if not latences:
        return Constat("E3 latence signal -> accepté", 0.0, "ms", 0, 0.0, None,
                       "aucun ordre accepté : indéterminé")
    moyenne, ecart = _moyenne_et_ecart([float(x) for x in latences])
    verdict = (
        "négligeable devant une échéance de 60 s" if moyenne < 500 else
        f"{moyenne / 1000:.2f} s entre la décision et l'exécution : sur une "
        f"échéance de 30 s, c'est {100 * moyenne / 30000:.0f} % du contrat "
        f"passé avant même d'être entré")
    return Constat("E3 latence signal -> accepté", moyenne, "ms",
                   len(latences), ecart, None, verdict)


def e4_expiration(executions: Sequence[Execution]) -> Constat:
    """L'expiration tombe-t-elle à la seconde demandée ?"""
    ecarts = [e.ecart_expiration_sec for e in executions
              if e.ecart_expiration_sec is not None]
    if not ecarts:
        return Constat("E4 durée réelle vs demandée", 0.0, "s", 0, 0.0, None,
                       "aucune expiration constatée : indéterminé")
    moyenne, ecart = _moyenne_et_ecart(ecarts)
    verdict = (
        "la durée demandée est la durée tenue" if abs(moyenne) < 1 else
        f"{moyenne:+.1f} s d'écart : le backtest compare le prix à la "
        f"mauvaise seconde, et l'erreur est systématique — elle ne s'annule "
        f"pas sur un grand nombre de trades")
    return Constat("E4 durée réelle vs demandée", moyenne, "s", len(ecarts),
                   ecart, None, verdict)


def taux_de_refus(executions: Sequence[Execution]) -> Constat:
    """Combien d'ordres le broker a-t-il rejetés ?

    Change le nombre de trades d'une stratégie, donc son résultat. Un backtest
    qui ne garde que les ordres acceptés a un taux de refus nul par
    construction, ce qui est la définition d'une mesure impossible.
    """
    if not executions:
        return Constat("Refus du broker", 0.0, "%", 0, 0.0, None,
                       "aucun ordre tenté")
    refuses = sum(1 for e in executions if not e.accepte)
    part = 100 * refuses / len(executions)
    verdict = ("tous les ordres passent" if refuses == 0 else
               f"{refuses} ordre(s) rejeté(s) : une stratégie qui compte sur "
               f"N signaux n'en place que {100 - part:.0f} %")
    return Constat("Refus du broker", part, "%", len(executions), 0.0, None,
                   verdict)


def rapport(executions: Sequence[Execution],
            sigma_horizon: float) -> list[Constat]:
    """Les cinq constats, dans l'ordre où ils se lisent."""
    return [
        taux_de_refus(executions),
        e1_payout(executions),
        e2_glissement(executions, sigma_horizon),
        e3_latence(executions),
        e4_expiration(executions),
    ]
