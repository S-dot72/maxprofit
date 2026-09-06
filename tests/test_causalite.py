"""
LE test de l'étape 3 : aucun indicateur ne repeint.

La propriété, énoncée précisément :

    pour toute série S et tout instant t,
    indicateur(S[:t+1])  ==  indicateur(S[:u+1])[valeur à t]  pour tout u > t

Autrement dit : ce qu'un indicateur affiche à l'instant t ne doit JAMAIS changer
quand des bougies postérieures arrivent. C'est la définition opérationnelle du
non-repeint, et elle se teste sans rien savoir de la formule de l'indicateur.

La méthode : on calcule la valeur à t sur la série tronquée à t, puis on
rallonge la série et on recalcule. Toute différence est un repeint.

Ce test attrape des choses qu'aucune relecture n'attrape. Un `.rolling().mean()`
centré, un `argmax` sur la série complète, une moyenne exponentielle réamorcée,
un tri qui remonte le temps : tout cela se lit comme du code normal et échoue
ici.

Les séries de test sont générées par un tirage GRAINÉ, donc reproductibles : un
échec est rejouable à l'identique. Le hasard n'est là que pour balayer plus de
formes que ce que j'écrirais à la main.
"""

from __future__ import annotations

import random

import pytest

from maxprofit.core.types import Candle
from maxprofit.indicators import (
    atr,
    atr_normalise_pct,
    bollinger_percent_b,
    corps_pct,
    distance_ma_pct,
    distance_pivot_pct,
    fractales,
    sma,
    stochastique,
    zigzag,
)
from maxprofit.indicators.zigzag import zigzag_repeignant

T0_SEC = 1_704_067_200
TF_SEC = 60


def serie_aleatoire(n: int, graine: int) -> list[Candle]:
    """Marche aléatoire grainée. Les prix n'ont aucun sens économique : on teste
    une propriété structurelle, pas une performance."""
    rng = random.Random(graine)
    prix = 1.1000
    bougies = []
    for i in range(n):
        prix *= 1 + rng.gauss(0, 0.0008)
        haut = prix * (1 + abs(rng.gauss(0, 0.0004)))
        bas = prix * (1 - abs(rng.gauss(0, 0.0004)))
        ouverture = rng.uniform(bas, haut)
        cloture = rng.uniform(bas, haut)
        bougies.append(Candle(
            pair="TEST_otc", tf_sec=TF_SEC, ts_sec=T0_SEC + i * TF_SEC,
            open=ouverture, high=haut, low=bas, close=cloture,
            tick_count=rng.randint(5, 60), complete=True,
        ))
    return bougies


#: Chaque indicateur ramené à une signature commune : fenêtre -> valeur à t.
INDICATEURS_SCALAIRES = {
    "sma(14)": lambda c: sma(c, 14),
    "distance_ma_pct(14)": lambda c: distance_ma_pct(c, 14),
    "bollinger_percent_b(20)": lambda c: bollinger_percent_b(c, 20),
    "stochastique(14,3)": lambda c: stochastique(c, 14, 3),
    "atr(14)": lambda c: atr(c, 14),
    "atr_normalise_pct(14)": lambda c: atr_normalise_pct(c, 14),
    "corps_pct": lambda c: corps_pct(c[-1]),
    "distance_pivot_pct(0.3%)": lambda c: distance_pivot_pct(c, 0.3),
}


#: Nombre de bougies dont chaque indicateur a besoin. Au-delà, sa valeur ne
#: doit plus bouger : c'est la propriété testée ci-dessous.
BESOIN = {
    "sma(14)": 14,
    "distance_ma_pct(14)": 14,
    "bollinger_percent_b(20)": 20,
    "stochastique(14,3)": 16,
    "atr(14)": 15,
    "atr_normalise_pct(14)": 15,
    "corps_pct": 1,
    # Le ZigZag n'a pas de fenêtre finie : le dernier pivot confirmé peut dater
    # d'aussi loin qu'on veut. Il est couvert par les tests de pivots.
}


@pytest.mark.parametrize("nom", sorted(BESOIN))
@pytest.mark.parametrize("graine", [1, 2, 3])
def test_la_valeur_ne_depend_pas_de_la_longueur_de_l_historique(nom, graine):
    """Un indicateur à fenêtre finie donne la MÊME valeur qu'on lui passe
    exactement sa fenêtre ou mille bougies de plus.

    Ce n'est pas le look-ahead que ce test attrape — l'API l'a déjà rendu
    impossible, puisqu'un indicateur ne reçoit que les bougies jusqu'à t. Il
    attrape l'autre défaut, plus discret : la mémoire infinie.

    Un ATR de Wilder ou une moyenne exponentielle se calculent par récurrence
    depuis le début de la série. Leur valeur dépend donc du point de départ :
    en backtest la récurrence démarre au début de la fenêtre chargée, en live
    au lancement du processus. Les deux ne sont jamais égales. On obtiendrait
    un backtest et un live qui exécutent le même code, respectent l'invariant
    n°1 à la lettre, et donnent des résultats différents — le genre d'écart
    qu'on met des semaines à imputer à la bonne cause.

    `test_temoin_l_atr_de_wilder_echouerait_a_ce_test` prouve que ce test a un
    pouvoir de détection.
    """
    calculer = INDICATEURS_SCALAIRES[nom]
    serie = serie_aleatoire(200, graine)
    fenetre = BESOIN[nom]

    for t in range(fenetre + 10, len(serie)):
        minimale = calculer(serie[t + 1 - fenetre:t + 1])
        complete = calculer(serie[:t + 1])
        assert minimale == complete, (
            f"{nom} à t={t} vaut {minimale} sur sa fenêtre minimale et "
            f"{complete} sur l'historique complet. L'indicateur a une mémoire "
            f"plus longue que sa fenêtre : le live et le backtest divergeront."
        )


def _atr_wilder(candles, periode=14):
    """ATR de Wilder — récurrence depuis le début de la série. Présent
    UNIQUEMENT dans ce fichier de tests, comme témoin négatif. Il n'existe
    nulle part dans `maxprofit/`."""
    if len(candles) < periode + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    valeur = sum(trs[:periode]) / periode
    for tr in trs[periode:]:
        valeur = (valeur * (periode - 1) + tr) / periode
    return valeur


def test_temoin_l_atr_de_wilder_echouerait_a_ce_test():
    """Contrôle du contrôle.

    Si l'ATR de Wilder passait le test ci-dessus, ce test ne prouverait rien.
    Il doit échouer — et c'est la justification chiffrée du choix, documenté
    dans `oscillators.py`, d'une moyenne arithmétique plutôt que le lissage de
    Wilder.
    """
    serie = serie_aleatoire(200, graine=5)
    t = 150
    minimale = _atr_wilder(serie[t + 1 - 15:t + 1])
    complete = _atr_wilder(serie[:t + 1])
    assert minimale != complete, (
        "l'ATR de Wilder donne la même valeur quelle que soit la longueur de "
        "l'historique : le test de fenêtre finie ne détecte donc rien."
    )
    ecart = abs(minimale - complete) / complete * 100
    assert ecart > 1, f"écart de seulement {ecart:.2f} %"


@pytest.mark.parametrize("graine", [1, 2, 3])
def test_les_pivots_deja_confirmes_ne_bougent_plus(graine):
    """Pour les indicateurs de pivots, la propriété est plus forte : un pivot
    déjà confirmé ne doit ni disparaître, ni changer de prix, ni changer
    d'indice de confirmation quand la série s'allonge."""
    serie = serie_aleatoire(200, graine)

    for producteur, nom in ((lambda c: zigzag(c, 0.3), "zigzag"),
                            (fractales, "fractales")):
        precedents: list = []
        for t in range(len(serie)):
            actuels = producteur(serie[:t + 1])
            assert actuels[:len(precedents)] == precedents, (
                f"{nom} a modifié un pivot déjà confirmé à t={t}. Un pivot "
                f"confirmé est un fait acquis ; le réécrire est du repeint."
            )
            precedents = actuels


@pytest.mark.parametrize("graine", [1, 2, 3])
def test_un_pivot_n_est_jamais_visible_avant_sa_confirmation(graine):
    """La propriété qui donne son sens à `confirmed_index`."""
    serie = serie_aleatoire(200, graine)

    for producteur, nom in ((lambda c: zigzag(c, 0.3), "zigzag"),
                            (fractales, "fractales")):
        for t in range(len(serie)):
            for pivot in producteur(serie[:t + 1]):
                assert pivot.confirmed_index <= t, (
                    f"{nom} annonce à t={t} un pivot confirmé seulement en "
                    f"{pivot.confirmed_index}"
                )
                assert pivot.index <= pivot.confirmed_index


@pytest.mark.parametrize("graine", [1, 2, 3])
def test_deux_executions_donnent_le_meme_resultat(graine):
    """Prérequis du test-oracle §2.7.5, appliqué aux indicateurs."""
    serie = serie_aleatoire(120, graine)
    for calculer in INDICATEURS_SCALAIRES.values():
        assert calculer(serie) == calculer(serie)
    assert zigzag(serie, 0.3) == zigzag(serie, 0.3)
    assert fractales(serie) == fractales(serie)


# --------------------------------------------------------------------------- #
# La mesure de l'illusion (spec §2.1)
# --------------------------------------------------------------------------- #

def test_la_version_repeignante_repeint_vraiment():
    """Contrôle du contrôle.

    Si `zigzag_repeignant` passait le test de causalité, cela signifierait que
    mon test ne détecte rien. Elle doit donc échouer — c'est le témoin négatif
    qui prouve que les tests ci-dessus ont un pouvoir de détection.
    """
    serie = serie_aleatoire(200, graine=7)
    repeint_constate = False

    precedents: list = []
    for t in range(len(serie)):
        actuels = zigzag_repeignant(serie[:t + 1], 0.3)
        if actuels[:len(precedents)] != precedents:
            repeint_constate = True
            break
        precedents = actuels

    assert repeint_constate, (
        "zigzag_repeignant n'a pas repeint : le test de causalité ne prouve "
        "donc rien sur la version honnête."
    )


def test_la_version_repeignante_connait_des_pivots_en_avance():
    """L'illusion, chiffrée : combien de bougies d'avance la version
    malhonnête donne-t-elle ?"""
    serie = serie_aleatoire(300, graine=11)

    honnete = zigzag(serie, 0.3)
    repeignant = zigzag_repeignant(serie, 0.3)

    assert len(repeignant) >= len(honnete)
    assert all(p.latence > 0 for p in honnete), (
        "un pivot honnête de latence nulle serait un pivot connu à l'instant "
        "même : impossible pour un ZigZag"
    )

    # Le pivot provisoire de la version repeignante est présenté comme connu
    # au moment de l'extrême, alors qu'il n'est pas encore confirmé du tout.
    if len(repeignant) > len(honnete):
        provisoire = repeignant[-1]
        assert provisoire.latence == 0

    latences = [p.latence for p in honnete]
    assert latences, "aucun pivot confirmé : le seuil est mal choisi pour ce test"
    # Documente l'ordre de grandeur : c'est le nombre de bougies pendant
    # lesquelles un backtest naïf « connaîtrait » un sommet inexistant.
    assert max(latences) >= 2
