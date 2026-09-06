"""
Indicateurs : valeurs calculées à la main.

Spec §5 : « Tests unitaires sur les indicateurs avec des séries construites à
la main dont le résultat est calculable à la main. »

Chaque test porte le calcul en commentaire. C'est la seule façon de distinguer
« l'indicateur fait ce que je crois » de « l'indicateur fait ce qu'il fait » :
un test qui compare le code à lui-même (valeur figée capturée d'une exécution)
valide un bug aussi volontiers qu'une implémentation correcte.
"""

from __future__ import annotations

import pytest

from maxprofit.core.errors import BotError
from maxprofit.core.types import Candle
from maxprofit.indicators import (
    atr,
    bollinger_percent_b,
    corps_pct,
    derniere_fractale,
    distance_ma_pct,
    fractales,
    meche_basse_pct,
    meche_haute_pct,
    sma,
    stochastique,
    zigzag,
)
from maxprofit.indicators.base import PivotKind

T0_SEC = 1_704_067_200
TF_SEC = 60


def bougie(i: int, o: float, h: float, b: float, c: float) -> Candle:
    return Candle(pair="T_otc", tf_sec=TF_SEC, ts_sec=T0_SEC + i * TF_SEC,
                  open=o, high=h, low=b, close=c, tick_count=10, complete=True)


def plates(prix: list[float]) -> list[Candle]:
    """Bougies sans amplitude : haut = bas = ouverture = clôture. Rend les
    calculs de pivots lisibles — le prix est la seule variable."""
    return [bougie(i, p, p, p, p) for i, p in enumerate(prix)]


# --------------------------------------------------------------------------- #
# Moyennes
# --------------------------------------------------------------------------- #

def test_sma():
    # (1+2+3+4+5)/5 = 3
    assert sma(plates([1, 2, 3, 4, 5]), 5) == 3.0
    # Sur 3 bougies : (3+4+5)/3 = 4
    assert sma(plates([1, 2, 3, 4, 5]), 3) == 4.0


def test_sma_historique_insuffisant():
    """`None`, jamais une moyenne partielle : une moyenne sur 3 bougies
    présentée comme une moyenne sur 14 est un chiffre faux."""
    assert sma(plates([1, 2, 3]), 14) is None


def test_distance_ma_pct():
    # Moyenne de 1..5 = 3 ; clôture = 5 ; (5/3 - 1) * 100 = 66,666...
    assert distance_ma_pct(plates([1, 2, 3, 4, 5]), 5) == pytest.approx(66.6666667)
    # Sous la moyenne : clôture 1, moyenne 3 -> (1/3 - 1)*100 = -66,666...
    assert distance_ma_pct(plates([5, 4, 3, 2, 1]), 5) == pytest.approx(-66.6666667)


def test_bollinger_percent_b():
    # Clôtures 1..5, période 5. Moyenne = 3.
    # Variance de population = (4+1+0+1+4)/5 = 2 ; écart-type = √2 ≈ 1,4142136
    # Bande haute = 3 + 2√2 ≈ 5,8284271 ; bande basse = 3 - 2√2 ≈ 0,1715729
    # %B = (5 - 0,1715729) / (5,8284271 - 0,1715729) ≈ 0,8535534
    assert bollinger_percent_b(plates([1, 2, 3, 4, 5]), 5) == pytest.approx(0.8535534)


def test_bollinger_sur_le_milieu_de_bande():
    # Clôtures 1,2,3,4,5,3 sur période 6 : moyenne = 18/6 = 3.
    # La dernière clôture vaut 3, donc exactement la moyenne -> %B = 0,5
    # quel que soit l'écart-type.
    assert bollinger_percent_b(plates([1, 2, 3, 4, 5, 3]), 6) == pytest.approx(0.5)


def test_bollinger_marche_plat_est_indefini():
    """Écart-type nul : les bandes sont confondues. Répondre 0,5 inventerait
    une position dans une bande de largeur nulle."""
    assert bollinger_percent_b(plates([2, 2, 2, 2, 2]), 5) is None


def test_percent_b_sort_des_bornes_quand_le_prix_perce():
    """Ne pas borner à [0, 1] : les cas extrêmes sont justement ceux qui
    intéressent la stratégie."""
    valeur = bollinger_percent_b(plates([1, 1, 1, 1, 1, 1, 1, 1, 1, 20]), 10)
    assert valeur is not None and valeur > 1.0


# --------------------------------------------------------------------------- #
# Stochastique
# --------------------------------------------------------------------------- #

def test_stochastique_au_plus_haut_et_au_plus_bas():
    # Amplitude de la fenêtre : bas = 10, haut = 20.
    serie = [bougie(i, 15, 20, 10, 20) for i in range(3)]
    # Clôture = 20 = plus haut -> %K = 100. Les 3 %K valent 100 -> %D = 100.
    resultat = stochastique(serie, periode_k=1, periode_d=3)
    assert resultat == (100.0, 100.0)

    serie_bas = [bougie(i, 15, 20, 10, 10) for i in range(3)]
    assert stochastique(serie_bas, periode_k=1, periode_d=3) == (0.0, 0.0)


def test_stochastique_calcul_a_la_main():
    # periode_k = 2, periode_d = 2 -> il faut 3 bougies.
    #   i0 : H=10 B=0  C=5
    #   i1 : H=12 B=2  C=6
    #   i2 : H=14 B=4  C=9
    # %K(i1) sur {i0,i1} : haut=12, bas=0 -> (6-0)/(12-0)*100 = 50
    # %K(i2) sur {i1,i2} : haut=14, bas=2 -> (9-2)/(14-2)*100 = 58,3333
    # %D = (50 + 58,3333) / 2 = 54,1667
    serie = [bougie(0, 5, 10, 0, 5), bougie(1, 5, 12, 2, 6), bougie(2, 6, 14, 4, 9)]
    resultat = stochastique(serie, periode_k=2, periode_d=2)
    assert resultat.k == pytest.approx(58.333333)
    assert resultat.d == pytest.approx(54.166667)


def test_stochastique_amplitude_nulle_est_indefini():
    assert stochastique(plates([5, 5, 5, 5]), periode_k=2, periode_d=2) is None


# --------------------------------------------------------------------------- #
# ATR
# --------------------------------------------------------------------------- #

def test_atr_calcul_a_la_main():
    #   i0 : H=10 B=8  C=9
    #   i1 : H=12 B=9  C=11   TR = max(12-9, |12-9|, |9-9|)  = 3
    #   i2 : H=13 B=11 C=12   TR = max(13-11, |13-11|, |11-11|) = 2
    # ATR(2) = (3 + 2) / 2 = 2,5
    serie = [bougie(0, 9, 10, 8, 9), bougie(1, 10, 12, 9, 11), bougie(2, 11, 13, 11, 12)]
    assert atr(serie, periode=2) == pytest.approx(2.5)


def test_atr_tient_compte_du_saut_de_cloture():
    """L'amplitude haut-bas seule sous-estime un gap. C'est la raison d'être de
    l'amplitude VRAIE."""
    #   i0 : C = 100
    #   i1 : H = 110, B = 109 -> amplitude 1, mais saut de 9 depuis 100
    #        TR = max(1, |110-100|, |109-100|) = 10
    serie = [bougie(0, 100, 100, 100, 100), bougie(1, 109, 110, 109, 110)]
    assert atr(serie, periode=1) == pytest.approx(10.0)


def test_atr_a_besoin_d_une_bougie_de_plus_que_sa_periode():
    serie = plates([1, 2, 3])
    assert atr(serie, periode=3) is None      # il faudrait 4 bougies
    assert atr(serie, periode=2) is not None


# --------------------------------------------------------------------------- #
# Géométrie
# --------------------------------------------------------------------------- #

def test_geometrie_de_la_bougie():
    # O=2 H=6 B=0 C=4 : amplitude 6, corps 2, mèche haute 2, mèche basse 2
    c = bougie(0, 2, 6, 0, 4)
    assert corps_pct(c) == pytest.approx(100 * 2 / 6)
    assert meche_haute_pct(c) == pytest.approx(100 * 2 / 6)
    assert meche_basse_pct(c) == pytest.approx(100 * 2 / 6)


def test_les_trois_parts_somment_a_cent():
    c = bougie(0, 3, 10, 1, 8)
    total = corps_pct(c) + meche_haute_pct(c) + meche_basse_pct(c)
    assert total == pytest.approx(100.0)


def test_bougie_plate_est_indefinie():
    assert corps_pct(bougie(0, 5, 5, 5, 5)) is None


def test_marteau():
    # O=9 H=10 B=0 C=10 : corps 1/10, mèche basse 9/10, mèche haute 0
    c = bougie(0, 9, 10, 0, 10)
    assert corps_pct(c) == pytest.approx(10.0)
    assert meche_basse_pct(c) == pytest.approx(90.0)
    assert meche_haute_pct(c) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Fractales — latence constante de 2
# --------------------------------------------------------------------------- #

def test_fractale_haute_confirmee_deux_bougies_plus_tard():
    # Hauts : 1, 2, 5, 2, 1 -> sommet strict à l'indice 2.
    serie = plates([1, 2, 5, 2, 1])
    (pivot,) = [p for p in fractales(serie) if p.kind is PivotKind.HAUT]
    assert pivot.index == 2
    assert pivot.price == 5
    assert pivot.confirmed_index == 4
    assert pivot.latence == 2


def test_aucune_fractale_sur_les_deux_dernieres_bougies():
    """La propriété qui rend l'indicateur honnête : à l'indice i, il manque
    encore i+1 et i+2 pour trancher."""
    serie = plates([1, 2, 5, 2, 1])
    for t in range(len(serie)):
        for pivot in fractales(serie[:t + 1]):
            assert pivot.index <= t - 2, (
                f"fractale annoncée à l'indice {pivot.index} alors qu'on n'est "
                f"qu'à t={t} : il manque {2 - (t - pivot.index)} bougie(s)"
            )


def test_la_fractale_n_apparait_qu_a_t_plus_2():
    serie = plates([1, 2, 5, 2, 1])
    assert fractales(serie[:3]) == []   # le sommet vient d'avoir lieu
    assert fractales(serie[:4]) == []   # une seule bougie après : pas assez
    assert len(fractales(serie[:5])) == 1


def test_egalite_ne_fait_pas_une_fractale():
    """Comparaison stricte : sur une paire peu volatile à cinq décimales, deux
    bougies voisines partagent souvent le même haut. Accepter l'égalité
    fabriquerait des fractales en série sur un marché plat."""
    assert fractales(plates([1, 5, 5, 5, 1])) == []


def test_derniere_fractale_filtre_par_sens():
    serie = plates([5, 4, 1, 4, 5, 4, 9, 4, 3])
    bas = derniere_fractale(serie, PivotKind.BAS)
    assert bas is not None and bas.index == 2 and bas.price == 1


# --------------------------------------------------------------------------- #
# ZigZag — latence VARIABLE, c'est là qu'est le piège
# --------------------------------------------------------------------------- #

def test_zigzag_calcul_a_la_main():
    # Seuil 10 %. Bougies plates : le prix est haut, bas et clôture.
    #   i0 100  i1 105  i2 110  i3 104  i4 99  i5 95
    #
    # i2 : la hausse depuis le creux de 100 atteint (110-100)/100 = 10 %
    #      -> pivot BAS à l'indice 0 (prix 100), confirmé à l'indice 2.
    # i4 : la baisse depuis le sommet de 110 atteint (110-99)/110 = 10 %
    #      -> pivot HAUT à l'indice 2 (prix 110), confirmé à l'indice 4.
    pivots = zigzag(plates([100, 105, 110, 104, 99, 95]), seuil_pct=10)

    assert len(pivots) == 2
    bas, haut = pivots
    assert (bas.kind, bas.index, bas.price, bas.confirmed_index) == (PivotKind.BAS, 0, 100, 2)
    assert (haut.kind, haut.index, haut.price, haut.confirmed_index) == (PivotKind.HAUT, 2, 110, 4)
    assert haut.latence == 2


def test_le_sommet_est_invisible_avant_sa_confirmation():
    """LE point de la spec §2.1. Le sommet est atteint à l'indice 2, mais rien
    ne permet de le savoir avant l'indice 4. Un backtest qui lit le tableau des
    pivots calculé après coup croit le connaître dès l'indice 2 — et prend deux
    bougies d'avance sur le marché."""
    prix = [100, 105, 110, 104, 99, 95]
    for t in (2, 3):
        sommets = [p for p in zigzag(plates(prix[:t + 1]), 10) if p.kind is PivotKind.HAUT]
        assert sommets == [], f"sommet annoncé à t={t}, il ne l'est qu'à t=4"

    sommets = [p for p in zigzag(plates(prix[:5]), 10) if p.kind is PivotKind.HAUT]
    assert len(sommets) == 1


def test_un_mouvement_sous_le_seuil_ne_confirme_rien():
    # Oscillation de 5 % avec un seuil à 10 % : aucun pivot.
    assert zigzag(plates([100, 105, 100, 105, 100]), seuil_pct=10) == []


def test_la_latence_du_zigzag_est_variable():
    """Contrairement aux fractales, il n'existe aucun décalage fixe qui
    rendrait le ZigZag honnête : la même série donne des latences différentes
    selon la vitesse du retournement."""
    # Retournement immédiat après le sommet.
    rapide = zigzag(plates([100, 110, 99, 95]), seuil_pct=10)
    # Long plateau avant le retournement.
    lent = zigzag(plates([100, 110, 109, 108, 107, 106, 105, 99]), seuil_pct=10)

    sommet_rapide = [p for p in rapide if p.kind is PivotKind.HAUT][0]
    sommet_lent = [p for p in lent if p.kind is PivotKind.HAUT][0]

    assert sommet_rapide.index == sommet_lent.index == 1
    assert sommet_rapide.latence == 1
    assert sommet_lent.latence == 6
    assert sommet_lent.latence != sommet_rapide.latence


def test_seuil_nul_refuse():
    with pytest.raises(BotError, match="strictement positif"):
        zigzag(plates([1, 2, 3]), seuil_pct=0)


def test_serie_vide_ou_trop_courte():
    assert zigzag([], seuil_pct=10) == []
    assert fractales([]) == []
    assert sma([], 5) is None
