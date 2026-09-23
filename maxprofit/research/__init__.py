"""
Le protocole de recherche — l'infrastructure AVANT la stratégie.

--- Pourquoi ce paquet existe ----------------------------------------------

Ce qui a été tenté à la résolution d'une minute est revenu vide, et de plus en
plus sévèrement à mesure que l'épreuve durcissait :

    46 hypothèses simples, 3 échéances     aucune ne survit à la correction
    1 402 conjonctions, test de permutation   p = 0,55 / 0,49 / 0,80
    un modèle boosté en validation glissante  50,38 % sur tout,
                                              49,73 % au décile confiant,
                                              45,31 % au centile

La dernière ligne est la plus instructive : **la précision DESCEND quand la
confiance monte.** Un modèle qui a trouvé quelque chose fait l'inverse.

La leçon n'est pas « ajouter des indicateurs ». C'est que sans dispositif, on
ne peut pas distinguer une découverte d'un tirage chanceux — et qu'avec assez
d'essais, le tirage chanceux arrive toujours.

--- Ce que ce paquet fait, et dans quel ordre ------------------------------

    univers      quels actifs, quelles échéances — et l'interdit OTC/réel
    decoupage    la période SCELLÉE, qui ne sert qu'une fois
    metriques    l'espérance après payout, jamais le taux de réussite seul
    registre     TOUTES les expériences, surtout celles qui échouent

Aucune stratégie n'est déclarée ici, et c'est volontaire : la phase 1 du
protocole interdit de choisir RSI, Bollinger, ICT ou martingale avant d'avoir
une anomalie. Ce sont des hypothèses candidates, pas des vérités.

--- ⚠ Ce que ce paquet ne peut pas faire -----------------------------------

Il ne rend pas une recherche honnête. Il rend la malhonnêteté VISIBLE — le
sceau ouvert laisse une trace, l'expérience abandonnée reste au registre, la
correction porte sur le compte réel. Contourner reste possible ; le faire sans
le savoir ne l'est plus.
"""

from maxprofit.research.decoupage import Decoupage, Periode
from maxprofit.research.metriques import (
    Resultat,
    benjamini_hochberg,
    esperance,
    seuil_de_rentabilite_pct,
    signaux_pour_etablir,
)
from maxprofit.core.payout import (
    BONUS_PCT,
    FLUX_POUR_LE_PLAFOND,
    PLAFOND_PCT,
    au_plafond,
    payout_applique_pct,
)
from maxprofit.research.registre import Experience, Registre
from maxprofit.research.univers import Univers, est_otc

__all__ = [
    "BONUS_PCT",
    "Decoupage",
    "Experience",
    "Periode",
    "Registre",
    "Resultat",
    "FLUX_POUR_LE_PLAFOND",
    "PLAFOND_PCT",
    "Univers",
    "au_plafond",
    "benjamini_hochberg",
    "esperance",
    "est_otc",
    "payout_applique_pct",
    "seuil_de_rentabilite_pct",
    "signaux_pour_etablir",
]
