"""
Le chemin des ticks : sans perte, ou il ne sert à rien.

Ce fichier ne mesure pas seulement que le code « marche ». Il mesure les deux
choses pour lesquelles l'encodage existe :

    1. `decoder(encoder(t)) == t`, au bit près, sur des cas tordus
    2. le TAUX DE COMPRESSION réellement obtenu — parce que c'est lui qui
       décide si les ticks tiennent dans le quota, et qu'une régression
       silencieuse sur ce point ramènerait le problème d'origine
"""

from __future__ import annotations

import random

import pytest

from maxprofit.core.errors import BotError
from maxprofit.core.types import Tick
from maxprofit.store.chemin_ticks import (
    ECHELLE_MAX,
    CheminTicks,
    decoder,
    encoder,
    grouper_par_minute,
    minute_de,
)

MINUTE = 1_758_000_000 // 60 * 60


def _ticks(prix: list[float], depart_ms: int | None = None,
           pas_ms: int = 500, pair: str = "EURUSD_otc") -> list[Tick]:
    base = MINUTE * 1000 if depart_ms is None else depart_ms
    return [Tick(pair=pair, ts_ms=base + i * pas_ms, price=p)
            for i, p in enumerate(prix)]


# --------------------------------------------------------------------------- #
# Ce pour quoi le module existe : l'aller-retour exact
# --------------------------------------------------------------------------- #

def test_aller_retour_exact_sur_des_prix_forex():
    ticks = _ticks([1.08231, 1.08230, 1.08232, 1.08233, 1.08229])
    assert decoder(encoder(ticks)) == ticks


def test_aller_retour_sur_un_seul_tick():
    ticks = _ticks([1.08231])
    assert decoder(encoder(ticks)) == ticks


def test_aller_retour_sur_des_prix_a_deux_decimales():
    """Un indice ne se code pas comme une paire forex : l'échelle est
    cherchée, et elle doit descendre à 2 sans rien perdre."""
    ticks = _ticks([182.45, 182.44, 182.51])
    chemin = encoder(ticks)
    assert chemin.echelle == 2
    assert decoder(chemin) == ticks


def test_aller_retour_sur_des_prix_entiers():
    ticks = _ticks([40000.0, 40001.0, 39999.0], pair="BTCUSD_otc")
    chemin = encoder(ticks)
    assert chemin.echelle == 0
    assert decoder(chemin) == ticks


def test_les_ticks_sont_rendus_dans_l_ordre_du_temps():
    """Ils arrivent d'un flux réseau : exiger qu'ils soient triés en amont
    serait une exigence qu'on finirait par oublier."""
    ticks = _ticks([1.1, 1.2, 1.3])
    melanges = [ticks[2], ticks[0], ticks[1]]
    assert decoder(encoder(melanges)) == ticks


def test_deux_ticks_au_meme_instant_sont_tous_les_deux_conserves():
    """C'est le marché qui le dit. Déduplication = donnée inventée."""
    base = MINUTE * 1000
    ticks = [Tick(pair="EURUSD_otc", ts_ms=base, price=1.1),
             Tick(pair="EURUSD_otc", ts_ms=base, price=1.2)]
    rendus = decoder(encoder(ticks))
    assert len(rendus) == 2
    assert sorted(t.price for t in rendus) == [1.1, 1.2]


def test_un_prix_ecarte_de_la_grille_par_du_bruit_flottant_y_revient():
    """La limite exacte de « sans perte », énoncée plutôt que subie.

    `1.1 + 11/10000` vaut `1.1011000000000002` : ce double n'est PAS celui que
    Python donne pour `1.1011`. L'encodage ramène le prix sur la grille
    décimale — une normalisation, d'au plus 10⁻¹¹ en relatif.

    Le test existe pour que ce comportement soit une décision visible. S'il
    changeait, quelqu'un le saurait.
    """
    ecarte = 1.1 + 11 / 10_000
    assert ecarte != 1.1011, "le cas du test n'existe plus, le revoir"

    rendus = decoder(encoder(_ticks([ecarte])))
    assert rendus[0].price == 1.1011
    assert abs(rendus[0].price - ecarte) < 1e-11


def test_un_vrai_prix_a_six_decimales_n_est_pas_ramene_a_cinq():
    """L'autre côté de la même règle, et le plus important des deux.

    La tolérance absorbe le bruit d'arithmétique ; elle ne doit surtout pas
    absorber une décimale réelle. 1.081235 rate la grille à cinq décimales de
    0,5 unité — cinq cent mille fois la tolérance — donc l'échelle monte à 6.
    """
    chemin = encoder(_ticks([1.081235, 1.081236]))
    assert chemin.echelle == 6
    assert decoder(chemin)[0].price == 1.081235


def test_aller_retour_sur_une_minute_aleatoire_realiste():
    """Une minute entière comme le collecteur en voit : ~125 ticks, marche au
    hasard à cinq décimales, intervalles irréguliers."""
    alea = random.Random(20260920)
    ts, prix = MINUTE * 1000, 108231
    ticks = []
    for _ in range(125):
        ts += alea.randint(50, 900)
        prix += alea.randint(-3, 3)
        if ts >= (MINUTE + 60) * 1000:
            break
        ticks.append(Tick(pair="EURUSD_otc", ts_ms=ts, price=prix / 100000))
    assert decoder(encoder(ticks)) == ticks


# --------------------------------------------------------------------------- #
# Le chiffre qui décide : la compression
# --------------------------------------------------------------------------- #

def test_une_minute_realiste_tient_dans_moins_de_500_octets():
    """C'est CE chiffre qui a fait rallumer les ticks.

    Une ligne par tick coûtait ~1 Go sur quatorze jours pour 0,5 Go de quota.
    À 500 octets par minute et ~5 000 minutes/jour toutes paires confondues,
    on retombe à ~35 Mo sur quatorze jours.

    Le test échoue si l'encodage se dégrade — c'est le seul garde-fou entre
    une régression d'encodage et un quota de base de données dépassé en
    production, deux semaines plus tard, sans message d'erreur.
    """
    alea = random.Random(7)
    ts, prix = MINUTE * 1000, 108231
    ticks = []
    while True:
        ts += alea.randint(300, 700)
        if ts >= (MINUTE + 60) * 1000:
            break
        prix += alea.randint(-2, 2)
        ticks.append(Tick(pair="EURUSD_otc", ts_ms=ts, price=prix / 100000))

    chemin = encoder(ticks)
    assert chemin.n_ticks > 80, "minute non représentative"
    assert len(chemin.octets) < 500, (
        f"{len(chemin.octets)} octets pour {chemin.n_ticks} ticks : "
        f"l'encodage s'est dégradé.")
    # Et la comparaison qui donne le sens du chiffre.
    par_ligne = chemin.n_ticks * 40      # estimation basse en PostgreSQL
    assert len(chemin.octets) < par_ligne / 5


# --------------------------------------------------------------------------- #
# Les refus — §5 : rien ne s'arrondit en silence
# --------------------------------------------------------------------------- #

def test_un_prix_inencodable_leve_au_lieu_d_arrondir():
    """Le cas qui compte : un prix qu'aucune puissance de dix ne rend entier.

    Arrondir produirait un prix faux que rien ne signalerait, et le backtest
    sous la minute mesurerait un marché qui n'a pas existé.
    """
    ticks = _ticks([1.0 / 3.0, 1.1])
    with pytest.raises(BotError, match="échelle"):
        encoder(ticks)


def test_encoder_refuse_deux_minutes():
    ticks = _ticks([1.1], depart_ms=MINUTE * 1000) + \
        _ticks([1.2], depart_ms=(MINUTE + 60) * 1000)
    with pytest.raises(BotError, match="UNE minute"):
        encoder(ticks)


def test_encoder_refuse_deux_paires():
    ticks = _ticks([1.1]) + _ticks([1.2], pair="GBPUSD_otc")
    with pytest.raises(BotError, match="mélange pas les paires"):
        encoder(ticks)


def test_encoder_refuse_une_liste_vide():
    with pytest.raises(BotError, match="rien à écrire"):
        encoder([])


def test_un_chemin_vide_ne_se_construit_pas():
    """Une minute sans tick est l'ABSENCE d'une ligne. La distinction entre
    « marché immobile » et « collecte interrompue » tient à ça."""
    with pytest.raises(BotError, match="ABSENCE"):
        CheminTicks("EURUSD_otc", MINUTE, 0, 5, b"x")


def test_une_minute_non_alignee_est_refusee():
    with pytest.raises(BotError, match="aligné sur la minute"):
        CheminTicks("EURUSD_otc", MINUTE + 1, 3, 5, b"x")


def test_une_echelle_hors_bornes_est_refusee():
    with pytest.raises(BotError, match="echelle hors"):
        CheminTicks("EURUSD_otc", MINUTE, 3, ECHELLE_MAX + 1, b"x")


# --------------------------------------------------------------------------- #
# Les corruptions — on lève, on ne rend pas des prix faux
# --------------------------------------------------------------------------- #

def test_des_octets_illisibles_levent():
    chemin = CheminTicks("EURUSD_otc", MINUTE, 3, 5, b"pas du zlib")
    with pytest.raises(BotError, match="illisible"):
        decoder(chemin)


def test_une_echelle_qui_ne_correspond_pas_aux_octets_leve():
    """Deux enregistrements mélangés donneraient des prix faux d'un facteur
    dix — silencieusement plausibles, et c'est bien le danger."""
    vrai = encoder(_ticks([1.08231, 1.08232]))
    menteur = CheminTicks(vrai.pair, vrai.minute_sec, vrai.n_ticks,
                          vrai.echelle - 1, vrai.octets)
    with pytest.raises(BotError, match="Échelle incohérente"):
        decoder(menteur)


def test_un_nombre_de_ticks_trop_petit_leve():
    vrai = encoder(_ticks([1.1, 1.2, 1.3]))
    tronque = CheminTicks(vrai.pair, vrai.minute_sec, 2, vrai.echelle,
                          vrai.octets)
    with pytest.raises(BotError, match="en trop"):
        decoder(tronque)


def test_un_nombre_de_ticks_trop_grand_leve():
    vrai = encoder(_ticks([1.1, 1.2]))
    gonfle = CheminTicks(vrai.pair, vrai.minute_sec, 5, vrai.echelle,
                         vrai.octets)
    with pytest.raises(BotError, match="tronqué"):
        decoder(gonfle)


# --------------------------------------------------------------------------- #
# Le groupage, qui est la responsabilité de l'appelant
# --------------------------------------------------------------------------- #

def test_grouper_separe_les_paires_et_les_minutes():
    ticks = (
        _ticks([1.1, 1.2]) +
        _ticks([1.3], depart_ms=(MINUTE + 60) * 1000) +
        _ticks([1.4], pair="GBPUSD_otc")
    )
    paquets = grouper_par_minute(ticks)
    assert set(paquets) == {
        ("EURUSD_otc", MINUTE),
        ("EURUSD_otc", MINUTE + 60),
        ("GBPUSD_otc", MINUTE),
    }
    assert len(paquets[("EURUSD_otc", MINUTE)]) == 2
    for (pair, minute), paquet in paquets.items():
        chemin = encoder(paquet)
        assert (chemin.pair, chemin.minute_sec) == (pair, minute)


def test_minute_de_arrondit_vers_le_bas():
    assert minute_de(MINUTE * 1000) == MINUTE
    assert minute_de(MINUTE * 1000 + 59_999) == MINUTE
    assert minute_de(MINUTE * 1000 + 60_000) == MINUTE + 60


# --------------------------------------------------------------------------- #
# La relecture à la seconde — le piège de l'index à la minute
# --------------------------------------------------------------------------- #

def test_une_fenetre_plus_courte_qu_une_minute_rend_quand_meme_ses_ticks(
        tmp_path):
    """Le bug trouvé au premier usage réel.

    `tick_paths` filtre sur `minute_sec`, qui est un multiple de 60. Une
    fenêtre de seize secondes n'en contient aucun : la requête rendait le vide,
    sans erreur, et l'appelant concluait « pas de données à cet instant » alors
    que la minute était en base.

    Le symptôme était d'autant plus trompeur qu'une fenêtre LARGE fonctionnait
    parfaitement — c'est la mesure fine qui échouait, c'est-à-dire précisément
    celle pour laquelle les ticks ont été rallumés.
    """
    from maxprofit.store.db import open_read_only, open_read_write
    from maxprofit.store.market import MarketReader, MarketWriter

    base = tmp_path / "market.db"
    # Une minute de ticks, une toutes les deux secondes.
    ticks = [Tick("EURUSD_otc", (MINUTE + 2 * i) * 1000, 1.1 + i / 100000)
             for i in range(30)]
    ecrivain = MarketWriter(open_read_write(base))
    ecrivain.insert_tick_paths([encoder(ticks)])
    ecrivain.conn.commit()
    ecrivain.close()

    lecteur = MarketReader(open_read_only(base))
    try:
        # Fenêtre de 16 s au MILIEU de la minute : aucune frontière de minute.
        etroite = lecteur.ticks("EURUSD_otc", MINUTE + 20, MINUTE + 36)
        assert etroite, "une fenêtre infra-minute ne doit pas rendre le vide"
        assert [t.ts_ms for t in etroite] == [
            (MINUTE + s) * 1000 for s in (20, 22, 24, 26, 28, 30, 32, 34)]

        # Et le filtrage est exact : la borne haute est exclue.
        assert all(MINUTE + 20 <= t.ts_ms / 1000 < MINUTE + 36
                   for t in etroite)
        # La minute entière reste accessible.
        assert len(lecteur.ticks("EURUSD_otc", MINUTE, MINUTE + 60)) == 30
    finally:
        lecteur.close()
