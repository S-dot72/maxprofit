"""
L'exécution — le seul endroit du projet qui engage de l'argent.

--- Pourquoi ce paquet existe avant toute stratégie ------------------------

Le seuil à battre est 52,08 % à 92 % de payout : **2,08 points au-dessus du
hasard**. C'est une marge si mince que le coût d'exécution n'est pas un détail
à ajouter en fin de calcul — il peut la consommer entièrement.

Quatre suppositions traversent tout le backtest sans avoir jamais été
vérifiées :

    E1  le payout appliqué est celui lu dans le flux
    E2  le prix d'entrée est la clôture de la bougie de décision
    E3  le délai signal -> clic -> acceptation ne déplace pas le prix
    E4  l'expiration tombe à la seconde demandée

Aucune ne se vérifie depuis des données historiques. Toutes se vérifient en
quelques dizaines d'ordres en démo — **et avec des entrées ALÉATOIRES**, parce
qu'aucune ne dépend de la règle qui a décidé d'entrer.

C'est ce qui rend cette mesure faisable aujourd'hui alors qu'aucune stratégie
ne tient : mesurer l'exécution ne demande pas d'avoir raison sur le marché.

--- ⚠ Ce paquet refuse par défaut -----------------------------------------

`garde` exige la preuve, tirée du JETON et non de la configuration, que le
compte est démo. Champ absent = refus, au même titre que champ à zéro : ne pas
savoir doit avoir le même effet que savoir que c'est réel.
"""

from maxprofit.execution.garde import (
    CompteRefuse,
    Plafonds,
    est_demo,
    exiger_un_compte_demo,
)
from maxprofit.execution.journal import Execution, JournalExecution
from maxprofit.execution.mesure import (
    Constat,
    e1_payout,
    e2_glissement,
    e3_latence,
    e4_expiration,
    points_de_taux,
    rapport,
    taux_de_refus,
)

__all__ = [
    "CompteRefuse",
    "Constat",
    "Execution",
    "JournalExecution",
    "Plafonds",
    "e1_payout",
    "e2_glissement",
    "e3_latence",
    "e4_expiration",
    "est_demo",
    "exiger_un_compte_demo",
    "points_de_taux",
    "rapport",
    "taux_de_refus",
]
