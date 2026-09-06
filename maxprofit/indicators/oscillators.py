"""
Moyennes et oscillateurs — les quatre conditions non repeignantes.

Toutes ces fonctions prennent une fenêtre de bougies dont la DERNIÈRE est
l'instant de décision, et retournent la valeur à cet instant. Elles retournent
`None` quand l'historique est insuffisant ou quand la valeur est mathématiquement
indéfinie ; jamais une valeur de repli. Une stratégie qui reçoit `None`
s'abstient — c'est un cas normal, pas une erreur.

**Décision de conception : aucune récurrence à mémoire infinie.**

L'ATR de Wilder et les moyennes exponentielles se calculent par récurrence
depuis le début de la série : leur valeur à l'instant t dépend de TOUT
l'historique, pas des N dernières bougies. En backtest, la récurrence démarre
au début de la fenêtre chargée ; en live, elle démarre au lancement du
processus. Les deux valeurs convergent mais ne sont jamais égales, et l'écart
est le plus grand juste après un redémarrage — c'est-à-dire précisément quand
on regarde les logs.

Cela suffirait à faire diverger le backtest et le live tout en respectant
l'invariant n°1 à la lettre : même code, mêmes règles, résultats différents.
On utilise donc uniquement des moyennes à fenêtre finie, dont la valeur est une
fonction pure des N dernières bougies. C'est un peu moins standard et beaucoup
plus vérifiable.
"""

from __future__ import annotations

from typing import NamedTuple, Sequence

from maxprofit.core.types import Candle
from maxprofit.indicators.base import verifier_periode, verifier_serie


def sma(candles: Sequence[Candle], periode: int) -> float | None:
    """Moyenne mobile simple des clôtures sur `periode` bougies."""
    verifier_periode(periode)
    serie = verifier_serie(candles)
    if len(serie) < periode:
        return None
    fenetre = serie[-periode:]
    return sum(c.close for c in fenetre) / periode


def distance_ma_pct(candles: Sequence[Candle], periode: int) -> float | None:
    """Écart de la clôture à sa moyenne mobile, en pourcentage de la moyenne.

    C'est la feature `ma14_distance_pct` de la spec §3.1. Le signe compte :
    positif au-dessus de la moyenne, négatif en dessous. Normaliser en
    pourcentage rend la valeur comparable entre paires cotées à 1,08 et à 148.
    """
    moyenne = sma(candles, periode)
    if moyenne is None or moyenne == 0:
        return None
    return (candles[-1].close / moyenne - 1) * 100


def _ecart_type_population(valeurs: Sequence[float], moyenne: float) -> float:
    """Écart-type de POPULATION (division par n), convention des bandes de
    Bollinger. L'écart-type d'échantillon (n-1) donnerait des bandes ~4 % plus
    larges sur une période de 20 : l'écart est petit, systématique, et suffit à
    décaler un seuil."""
    return (sum((v - moyenne) ** 2 for v in valeurs) / len(valeurs)) ** 0.5


def bollinger_percent_b(candles: Sequence[Candle], periode: int = 20,
                        k: float = 2.0) -> float | None:
    """Position de la clôture dans les bandes de Bollinger.

    0 = sur la bande basse, 1 = sur la bande haute, 0,5 = sur la moyenne. La
    valeur sort de [0, 1] quand le prix perce une bande, et c'est voulu : la
    borner masquerait justement les cas extrêmes qui intéressent la stratégie.

    Retourne `None` si l'écart-type est nul (marché parfaitement plat sur la
    fenêtre) : les bandes sont confondues et la position dans la bande n'a pas
    de sens. Répondre 0,5 dans ce cas inventerait une information.
    """
    verifier_periode(periode, minimum=2)
    serie = verifier_serie(candles)
    if len(serie) < periode:
        return None
    clotures = [c.close for c in serie[-periode:]]
    moyenne = sum(clotures) / periode
    ecart = _ecart_type_population(clotures, moyenne)
    if ecart == 0:
        return None
    bas = moyenne - k * ecart
    haut = moyenne + k * ecart
    return (serie[-1].close - bas) / (haut - bas)


class Stochastique(NamedTuple):
    k: float
    d: float


def stochastique(candles: Sequence[Candle], periode_k: int = 14,
                 periode_d: int = 3) -> Stochastique | None:
    """Oscillateur stochastique %K et %D, en points de 0 à 100.

    %K = position de la clôture dans l'amplitude haut/bas des `periode_k`
    dernières bougies. %D = moyenne simple des `periode_d` derniers %K.

    Il faut donc `periode_k + periode_d - 1` bougies pour produire un %D.

    Retourne `None` si l'amplitude est nulle sur l'une des fenêtres : la
    position dans un intervalle de largeur zéro est indéfinie. La convention
    répandue (renvoyer 50, ou reconduire la valeur précédente) fabrique une
    donnée là où il n'y en a pas, et sur des paires OTC peu volatiles à cinq
    décimales, ce cas se produit vraiment.
    """
    verifier_periode(periode_k, minimum=1)
    verifier_periode(periode_d, minimum=1)
    serie = verifier_serie(candles)
    besoin = periode_k + periode_d - 1
    if len(serie) < besoin:
        return None

    valeurs_k: list[float] = []
    for decalage in range(periode_d):
        fin = len(serie) - (periode_d - 1 - decalage)
        fenetre = serie[fin - periode_k:fin]
        plus_haut = max(c.high for c in fenetre)
        plus_bas = min(c.low for c in fenetre)
        if plus_haut == plus_bas:
            return None
        valeurs_k.append((fenetre[-1].close - plus_bas) / (plus_haut - plus_bas) * 100)

    return Stochastique(k=valeurs_k[-1], d=sum(valeurs_k) / periode_d)


def _true_range(courante: Candle, precedente: Candle) -> float:
    """Amplitude vraie : tient compte du saut par rapport à la clôture
    précédente, que l'amplitude haut-bas seule ignore."""
    return max(
        courante.high - courante.low,
        abs(courante.high - precedente.close),
        abs(courante.low - precedente.close),
    )


def atr(candles: Sequence[Candle], periode: int = 14) -> float | None:
    """Amplitude vraie moyenne, en unités de prix.

    Moyenne ARITHMÉTIQUE des amplitudes vraies, pas le lissage de Wilder — voir
    l'en-tête du module : le lissage de Wilder dépend de tout l'historique et
    ferait diverger le live du backtest sans que rien ne le signale.

    Il faut `periode + 1` bougies : la première amplitude vraie a besoin d'une
    clôture précédente.
    """
    verifier_periode(periode)
    serie = verifier_serie(candles)
    if len(serie) < periode + 1:
        return None
    fenetre = serie[-(periode + 1):]
    amplitudes = [
        _true_range(fenetre[i], fenetre[i - 1]) for i in range(1, len(fenetre))
    ]
    return sum(amplitudes) / periode


def atr_normalise_pct(candles: Sequence[Candle], periode: int = 14) -> float | None:
    """ATR rapporté au prix, en pourcentage.

    C'est la feature `atr_normalisé` de la spec §3.1, et c'est elle qu'il faut
    utiliser pour segmenter par régime de volatilité (§3.2) : un ATR brut de
    0,0004 sur EUR/USD et de 0,04 sur USD/JPY décrivent la même agitation, mais
    un tercile calculé sur les valeurs brutes ne regrouperait que des paires.
    """
    valeur = atr(candles, periode)
    if valeur is None:
        return None
    reference = candles[-1].close
    if reference == 0:
        return None
    return valeur / reference * 100
