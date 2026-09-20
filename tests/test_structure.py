"""
La structure de marché : tendance, séries, order blocks, zones clés.

Le test qui porte le plus est `test_temoin_un_order_block_repeignant_echoue` :
sans lui, rien ne prouverait que les contrôles de causalité ci-dessus détectent
quoi que ce soit.
"""

from __future__ import annotations

import random

import pytest

from maxprofit.core.errors import BotError
from maxprofit.core.types import Candle
from maxprofit.indicators.base import PivotKind
from maxprofit.indicators.structure import (
    Tendance,
    bollinger_bandes,
    order_blocks,
    order_blocks_repeignants,
    sens_bougie,
    serie_avant_la_derniere,
    serie_de_bougies,
    tendance,
    zones_au_dessus,
)

T0_SEC = 1_704_067_200
TF_SEC = 60


def bougie(i: int, ouverture: float, cloture: float,
           haut: float | None = None, bas: float | None = None) -> Candle:
    return Candle(
        pair="TEST_otc", tf_sec=TF_SEC, ts_sec=T0_SEC + i * TF_SEC,
        open=ouverture, high=haut if haut is not None else max(ouverture, cloture),
        low=bas if bas is not None else min(ouverture, cloture),
        close=cloture, tick_count=50, complete=True,
    )


def plate(n: int, prix: float = 1.0) -> list[Candle]:
    return [bougie(i, prix, prix) for i in range(n)]


def serie_aleatoire(n: int, graine: int) -> list[Candle]:
    rng = random.Random(graine)
    prix = 1.1000
    sortie = []
    for i in range(n):
        ouverture = prix
        prix *= 1 + rng.gauss(0, 0.0008)
        haut = max(ouverture, prix) * (1 + abs(rng.gauss(0, 0.0004)))
        bas = min(ouverture, prix) * (1 - abs(rng.gauss(0, 0.0004)))
        sortie.append(bougie(i, ouverture, prix, haut, bas))
    return sortie


# --------------------------------------------------------------------------- #
# Bandes de Bollinger
# --------------------------------------------------------------------------- #

def test_une_fenetre_trop_courte_rend_None_plutot_qu_un_chiffre_plausible():
    """Une moyenne sur cinq bougies quand on en demande vingt répondrait
    quelque chose de faux, et la stratégie déciderait dessus."""
    assert bollinger_bandes(plate(5), periode=20) is None


def test_sur_une_serie_plate_les_trois_bandes_se_confondent():
    b = bollinger_bandes(plate(25, prix=1.5), periode=20)
    assert b is not None
    assert b.basse == b.mediane == b.haute == pytest.approx(1.5)
    assert b.largeur_pct() == pytest.approx(0.0)


def test_la_mediane_est_la_moyenne_des_clotures():
    serie = [bougie(i, 1.0, 1.0 + i / 1000) for i in range(20)]
    b = bollinger_bandes(serie, periode=20)
    attendu = sum(c.close for c in serie) / 20
    assert b.mediane == pytest.approx(attendu)


def test_les_bandes_encadrent_la_mediane():
    b = bollinger_bandes(serie_aleatoire(60, graine=3), periode=20)
    assert b.basse < b.mediane < b.haute


def test_un_ecart_type_non_positif_est_refuse():
    with pytest.raises(BotError, match="ecarts"):
        bollinger_bandes(plate(25), periode=20, ecarts=0)


# --------------------------------------------------------------------------- #
# Tendance — exiger les DEUX, sommets et creux
# --------------------------------------------------------------------------- #

def test_des_sommets_ET_des_creux_plus_hauts_font_une_tendance_haussiere():
    serie = [bougie(i, 1.0 + i / 100, 1.0 + i / 100) for i in range(20)]
    assert tendance(serie, fenetre=10) is Tendance.HAUSSIERE


def test_des_sommets_ET_des_creux_plus_bas_font_une_tendance_baissiere():
    serie = [bougie(i, 2.0 - i / 100, 2.0 - i / 100) for i in range(20)]
    assert tendance(serie, fenetre=10) is Tendance.BAISSIERE


def test_une_expansion_n_est_PAS_une_tendance():
    """Sommets plus hauts ET creux plus bas : le marché s'élargit, il ne monte
    pas. Exiger les deux est ce qui fait la différence."""
    avant = [bougie(i, 1.0, 1.0, haut=1.01, bas=0.99) for i in range(10)]
    apres = [bougie(10 + i, 1.0, 1.0, haut=1.05, bas=0.95) for i in range(10)]
    assert tendance(avant + apres, fenetre=10) is Tendance.INDECISE


def test_une_serie_trop_courte_est_indecise_et_non_haussiere():
    """L'état INDÉCIS est explicite : on ne trade pas dans une tendance
    qu'on n'a pas pu constater."""
    assert tendance(plate(5), fenetre=10) is Tendance.INDECISE


# --------------------------------------------------------------------------- #
# Séries de bougies
# --------------------------------------------------------------------------- #

def test_le_sens_d_une_bougie():
    assert sens_bougie(bougie(0, 1.0, 1.1)) == 1
    assert sens_bougie(bougie(0, 1.1, 1.0)) == -1
    assert sens_bougie(bougie(0, 1.0, 1.0)) == 0


def test_une_serie_de_rouges_est_comptee():
    serie = [bougie(i, 1.0, 0.99) for i in range(4)]
    assert serie_de_bougies(serie) == (-1, 4)


def test_un_doji_ROMPT_la_serie():
    """Une bougie sans corps n'exprime aucune pression : la compter dans une
    série de rouges ferait dire à la série ce qu'elle ne dit pas."""
    serie = [bougie(0, 1.0, 0.99), bougie(1, 1.0, 1.0), bougie(2, 1.0, 0.99)]
    assert serie_de_bougies(serie) == (-1, 1)


def test_la_serie_AVANT_la_derniere_est_celle_qui_interesse_la_strategie():
    """« une bougie verte APRÈS une série de rouges » : la série concernée se
    termine à l'avant-dernière bougie."""
    serie = [bougie(i, 1.0, 0.99) for i in range(3)] + [bougie(3, 1.0, 1.01)]
    assert serie_de_bougies(serie) == (1, 1)
    assert serie_avant_la_derniere(serie) == (-1, 3)


# --------------------------------------------------------------------------- #
# Order blocks — causalité
# --------------------------------------------------------------------------- #

def serie_avec_cassure() -> list[Candle]:
    """Un plateau, une bougie rouge, puis une impulsion qui casse le plateau."""
    serie = [bougie(i, 1.00, 1.00, haut=1.002, bas=0.998) for i in range(12)]
    serie.append(bougie(12, 1.000, 0.996))            # la rouge : le bloc
    serie.append(bougie(13, 0.996, 1.010))            # l'impulsion : la cassure
    return serie


def test_un_order_block_est_date_de_sa_CASSURE_pas_de_sa_formation():
    blocs = order_blocks(serie_avec_cassure(), fenetre=10, recul_max=5)
    assert blocs, "aucun bloc détecté sur une cassure franche"
    bloc = blocs[0]
    assert bloc.kind is PivotKind.BAS, "une cassure haussière laisse une demande"
    assert bloc.index == 12
    assert bloc.confirmed_index == 13
    assert bloc.latence == 1


def test_un_order_block_n_est_jamais_visible_avant_sa_confirmation():
    for bloc in order_blocks(serie_aleatoire(300, graine=11)):
        assert not bloc.visible_a(bloc.index - 1)
        assert not bloc.visible_a(bloc.confirmed_index - 1)
        assert bloc.visible_a(bloc.confirmed_index)


@pytest.mark.parametrize("graine", [1, 5, 42])
def test_un_bloc_deja_confirme_ne_bouge_plus(graine):
    """La propriété qui rend l'indicateur utilisable en live : allonger
    l'historique ne doit rien réécrire du passé."""
    serie = serie_aleatoire(250, graine=graine)
    court = order_blocks(serie[:200])
    long = order_blocks(serie)

    connus = {(b.index, b.confirmed_index, b.bas, b.haut) for b in long}
    for bloc in court:
        if bloc.confirmed_index < 200:
            assert (bloc.index, bloc.confirmed_index, bloc.bas,
                    bloc.haut) in connus, f"bloc {bloc.index} réécrit"


@pytest.mark.parametrize("graine", [2, 9])
def test_deux_executions_donnent_le_meme_resultat(graine):
    serie = serie_aleatoire(200, graine=graine)
    assert order_blocks(serie) == order_blocks(serie)


def test_recul_max_borne_la_recherche():
    """Sans borne, une cassure après une longue tendance irait chercher un
    bloc vieux de cent bougies, que plus personne ne regarde."""
    serie = [bougie(0, 1.000, 0.990)]                       # rouge, loin
    serie += [bougie(i, 1.00, 1.00, haut=1.002, bas=0.998)
              for i in range(1, 13)]
    serie.append(bougie(13, 1.000, 1.010))                  # cassure
    assert order_blocks(serie, fenetre=10, recul_max=3) == []
    assert order_blocks(serie, fenetre=10, recul_max=20) != []


# --------------------------------------------------------------------------- #
# ⚠ Le témoin — sans lui, les tests ci-dessus ne prouvent rien
# --------------------------------------------------------------------------- #

def test_temoin_un_order_block_repeignant_echoue_a_la_causalite():
    """Contrôle du contrôle.

    Si `order_blocks_repeignants` passait les tests de causalité, cela
    signifierait qu'ils ne détectent rien. Elle doit donc échouer — et elle
    échoue exactement là où il faut : elle prétend connaître le bloc dès sa
    formation, alors que seule la cassure le révèle.
    """
    serie = serie_avec_cassure()
    honnetes = order_blocks(serie, fenetre=10, recul_max=5)
    menteurs = order_blocks_repeignants(serie, fenetre=10, recul_max=5)

    assert honnetes and menteurs
    assert len(honnetes) == len(menteurs), "mêmes blocs, dates différentes"

    # Le menteur se croit connaissable dès sa formation.
    assert all(b.latence == 0 for b in menteurs)
    assert any(b.latence > 0 for b in honnetes)

    # Et c'est exactement là que le backtest deviendrait flatteur : à l'indice
    # de formation, le menteur est « visible » alors que l'honnête ne l'est pas.
    menteur, honnete = menteurs[0], honnetes[0]
    assert menteur.visible_a(menteur.index)
    assert not honnete.visible_a(honnete.index)


def test_la_latence_mesure_ce_que_couterait_l_illusion():
    """Sur données aléatoires, l'écart n'est pas anecdotique."""
    serie = serie_aleatoire(400, graine=17)
    latences = [b.latence for b in order_blocks(serie)]
    assert latences, "aucun bloc sur 400 bougies : le réglage est suspect"
    assert max(latences) >= 1
    moyenne = sum(latences) / len(latences)
    assert moyenne > 0, (
        f"latence moyenne nulle ({moyenne}) : l'indicateur ne serait pas "
        f"repeignant, ce qu'aucun order block n'est par construction"
    )


# --------------------------------------------------------------------------- #
# Zones clés
# --------------------------------------------------------------------------- #

def test_une_zone_deja_franchie_n_est_plus_un_plafond():
    from maxprofit.indicators.base import Pivot

    serie = plate(30, prix=1.0)
    dessous = Pivot(kind=PivotKind.HAUT, index=5, ts_sec=T0_SEC, price=0.95,
                    confirmed_index=7, confirmed_ts_sec=T0_SEC)
    assert zones_au_dessus(serie, prix=1.0, pivots=[dessous]) == []


def test_une_zone_proche_au_dessus_est_signalee():
    from maxprofit.indicators.base import Pivot

    serie = plate(30, prix=1.0)
    proche = Pivot(kind=PivotKind.HAUT, index=5, ts_sec=T0_SEC, price=1.0005,
                   confirmed_index=7, confirmed_ts_sec=T0_SEC)
    zones = zones_au_dessus(serie, prix=1.0, pivots=[proche], marge_pct=0.10)
    assert len(zones) == 1
    assert zones[0].distance_pct == pytest.approx(0.05, abs=0.001)
    assert zones[0].origine == "fractale"


def test_une_zone_trop_lointaine_n_est_pas_signalee():
    from maxprofit.indicators.base import Pivot

    serie = plate(30, prix=1.0)
    loin = Pivot(kind=PivotKind.HAUT, index=5, ts_sec=T0_SEC, price=1.05,
                 confirmed_index=7, confirmed_ts_sec=T0_SEC)
    assert zones_au_dessus(serie, prix=1.0, pivots=[loin], marge_pct=0.10) == []


def test_une_zone_NON_ENCORE_CONFIRMEE_est_ignoree():
    """Sans ce filtre, la stratégie éviterait une résistance que personne ne
    pouvait connaître — et son backtest serait flatteur pour une raison
    invisible à la relecture."""
    from maxprofit.indicators.base import Pivot

    serie = plate(30, prix=1.0)
    futur = Pivot(kind=PivotKind.HAUT, index=25, ts_sec=T0_SEC, price=1.0005,
                  confirmed_index=99, confirmed_ts_sec=T0_SEC)
    assert zones_au_dessus(serie, prix=1.0, pivots=[futur]) == []
