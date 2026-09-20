"""
Gestion de capital et de risque — le mode PLAN.

Deux modes, et la frontière entre eux est ce qui rend le backtest lisible :

    Mode.TRADE_SEUL   n'émet que des signaux
    Mode.PLAN         les dimensionne et applique les gardes

Ce paquet ne connaît AUCUNE stratégie : `tests/test_layering.py` le refuse. Un
dimensionnement qui pourrait influencer un signal ferait mesurer au backtest la
stratégie *plus* la mise, sans moyen de séparer les deux.
"""

from maxprofit.plan.capital import Arret, Journee, Mode, PlanCapital
from maxprofit.plan.progression import PAS_MAX_PAR_DEFAUT, Echelle
from maxprofit.plan.projection import (
    EcartAuPlan,
    JourProjete,
    ecart_au_plan,
    projeter,
    solde_projete,
)
from maxprofit.plan.session import EtatSession, Session

__all__ = [
    "PAS_MAX_PAR_DEFAUT",
    "Arret",
    "EcartAuPlan",
    "Echelle",
    "EtatSession",
    "JourProjete",
    "Journee",
    "Mode",
    "PlanCapital",
    "Session",
    "ecart_au_plan",
    "projeter",
    "solde_projete",
]
