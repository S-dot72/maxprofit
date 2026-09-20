"""
Le payout du flux n'est pas celui qu'on touche.

--- ⚠ La règle, mesurée sur des ordres réels -------------------------------

    payout appliqué = min(payout du flux + 8, 92)

Relevé sur 26 ordres en démo, 12 valeurs de flux distinctes, **sans une seule
exception** :

    flux   71  74  75  77  80  84  85  88  89  90  91  92
    appl.  79  82  83  85  88  92  92  92  92  92  92  92
    écart  +8  +8  +8  +8  +8  +8  +7  +4  +3  +2  +1   0

L'écart vaut exactement +8 jusqu'à saturation à 92, puis ce qu'il faut pour
atteindre le plafond. Aucun ajustement, aucune tolérance : la table est une
fonction, pas une régression.

--- Pourquoi c'est important, et pas un détail comptable -------------------

**Toutes les analyses de ce projet ont utilisé le payout du flux**, parce que
c'est ce que `GetPairs()` rend et donc ce que la table `payouts` contient. Le
seuil de rentabilité en a été surestimé partout où le flux n'était pas déjà à
92 :

    flux 71 %  ->  seuil annoncé 58,48 %,  seuil réel 55,87 %
    flux 80 %  ->  seuil annoncé 55,56 %,  seuil réel 53,19 %
    flux 84 %  ->  seuil annoncé 54,35 %,  seuil réel 52,08 %

--- Ce que ça ne change PAS, et il faut le dire aussi ----------------------

Le palier qui décide est inchangé. À un flux de 92, l'appliqué vaut 92 : la
correction est nulle là où l'on trade. La conclusion mesurée précédemment — le
broker tarife la direction résiduelle au point de la rendre inexploitable —
tient donc intégralement, et l'espérance à 92 % reste **−0,030 $/$**.

--- Ce que ça change vraiment : la FENÊTRE ---------------------------------

Le payout maximal de 92 % est atteint dès que le flux affiche **84**, pas 92.
Mesuré sur l'historique complet des quatre paires épinglées :

    flux = 92    36,3 % du temps
    flux >= 84   51,6 % du temps      <- 1,42 fois plus large

Une stratégie rare gagne 42 % d'occasions de tirer, sans rien concéder sur le
seuil. C'est le seul gain gratuit trouvé jusqu'ici.

--- ⚠ Ce qu'on ne sait pas ------------------------------------------------

Pourquoi. Peut-être le champ lu par la bibliothèque n'est-il pas le payout
affiché à l'écran mais une autre grandeur ; peut-être le broker applique-t-il
un bonus. La règle est décrite, pas expliquée — et elle est vérifiée à chaque
ordre par `execution.mesure.e1_payout`, qui criera si elle change.
"""

from __future__ import annotations

from maxprofit.core.errors import BotError

#: Ce que le broker ajoute au payout affiché dans le flux.
BONUS_PCT = 8

#: Plafond observé. Aucun ordre n'a jamais reçu davantage.
PLAFOND_PCT = 92

#: Payout du flux à partir duquel l'appliqué sature au plafond.
FLUX_POUR_LE_PLAFOND = PLAFOND_PCT - BONUS_PCT      # 84


def payout_applique_pct(payout_flux_pct: float) -> float:
    """Le payout réellement appliqué, depuis celui lu dans le flux.

    À utiliser partout où un calcul d'espérance part de la table `payouts` :
    elle contient le payout du FLUX, et s'en servir directement surestime le
    seuil de rentabilité de plus de deux points sous 84 %.
    """
    if not (0 <= payout_flux_pct <= 100):
        raise BotError(
            f"payout_flux_pct hors [0,100] : {payout_flux_pct}")
    return min(payout_flux_pct + BONUS_PCT, float(PLAFOND_PCT))


def au_plafond(payout_flux_pct: float) -> bool:
    """Ce payout du flux donne-t-il le maximum de 92 % ?

    La fonction existe pour que le filtre d'entrée se lise `au_plafond(p)` et
    non `p >= 92` — cette seconde forme est vraie, mais elle écarte 42 % des
    occasions qui paient pourtant exactement pareil.
    """
    return payout_flux_pct >= FLUX_POUR_LE_PLAFOND
