"""
Protocole de simulation OTC : EXP-001 (qualité des bougies) et EXP-002
(indicateurs sur toutes les bougies, et à l'entrée ZoneH1).
"""

from __future__ import annotations

import random

import pytest

from maxprofit.apprentissage.indicateurs import (
    ATR_AGITE, ATR_CALME, RSI_BAS, RSI_HAUT, Indicateurs,
    indicateurs_a_l_entree)
from maxprofit.apprentissage.qualite import Qualite
from maxprofit.core.types import Candle
from maxprofit.indicators import atr, besoin_macd, macd, rsi

T0 = 1_704_067_200          # 2024-01-01 0 h UTC
PAIRE = "EURUSD_otc"


def bougie(i: int, o: float, h: float, b: float, c: float, *,
           ticks: int = 30, complete: bool = True,
           paire: str = PAIRE) -> Candle:
    return Candle(pair=paire, tf_sec=60, ts_sec=T0 + 60 * i, open=o, high=h,
                  low=b, close=c, tick_count=ticks, complete=complete)


def marche(n: int, graine: int, *, sauter: tuple[int, ...] = (),
           paire: str = PAIRE) -> list[Candle]:
    """Marche aléatoire grainée, OUVERTURE = CLÔTURE PRÉCÉDENTE comme le
    flux de ticks OTC ; les minutes de `sauter` sont absentes."""
    rng = random.Random(graine)
    prix = 1.1
    sortie = []
    for i in range(n):
        ouverture = prix
        prix *= 1 + rng.gauss(0, 0.0006)
        haut = max(ouverture, prix) * (1 + abs(rng.gauss(0, 0.0002)))
        bas = min(ouverture, prix) * (1 - abs(rng.gauss(0, 0.0002)))
        if i not in sauter:
            sortie.append(bougie(i, ouverture, haut, bas, prix, paire=paire))
    return sortie


# --------------------------------------------------------------------------- #
# EXP-001 — qualité
# --------------------------------------------------------------------------- #

def test_qualite_marque_chaque_defaut_une_fois():
    serie = [
        bougie(0, 1.0, 1.02, 0.99, 1.01),
        bougie(1, 1.01, 1.03, 1.0, 1.02),
        bougie(2, 1.02, 1.04, 1.01, 1.03, complete=False),   # incomplète
        bougie(3, 1.03, 1.04, 1.02, 1.03, ticks=2),          # peu de ticks
        bougie(4, 1.03, 1.05, 1.02, 1.04),
        # minutes 5 et 6 absentes : un trou de 2 minutes
        bougie(7, 1.04, 1.06, 1.03, 1.05),                   # reprise
        bougie(8, 1.05, 1.06, 1.04, 1.05),
        bougie(9, 1.30, 1.31, 1.29, 1.30),                   # saut d'ouverture
        bougie(10, 1.30, 1.30, 1.30, 1.30),                  # trois minutes
        bougie(11, 1.30, 1.30, 1.30, 1.30),                  # au prix unique
        bougie(12, 1.30, 1.30, 1.30, 1.30),
        bougie(13, 1.30, 1.31, 1.29, 1.30),
    ]
    q = Qualite(T0, T0 + 3600, T0 + 60 * 9)
    q.ajouter(serie)
    p = q.resultat()["paires"][PAIRE]
    assert p["drapeaux"] == {"incomplete": 1, "peu_de_ticks": 1,
                             "reprise": 1, "saut": 1, "figee": 3}
    assert p["trous"] == 1 and p["manquantes"] == 2
    assert p["plus_long_trou"] == 2
    assert p["lues"] == 12 and p["ok"] == 12 - 7
    assert p["couvertes"] == 14 and p["non_suivies"] == 0
    # Les périodes : la coupure tombe à la minute 9.
    periodes = q.resultat()["periodes"]
    assert periodes["lues"] == [7, 5]
    assert periodes["saut"] == [0, 1] and periodes["figee"] == [0, 3]


def test_qualite_intervalles_suspects_et_signaux_touches():
    serie = [bougie(i, 1.0, 1.01, 0.99, 1.0) for i in (0, 1, 2, 5, 6, 7, 8)]
    q = Qualite(T0, T0 + 3600, T0 + 3600)
    q.ajouter(serie)
    # Minutes 3 et 4 absentes, minute 5 en reprise : un seul intervalle.
    assert q.intervalles[PAIRE] == [(T0 + 180, T0 + 300)]
    assert q.touche(PAIRE, T0 + 240, T0 + 240)
    assert q.touche(PAIRE, T0, T0 + 180)
    assert not q.touche(PAIRE, T0, T0 + 120)
    assert not q.touche(PAIRE, T0 + 360, T0 + 480)
    assert not q.touche("AUTRE_otc", T0, T0 + 480)


def test_qualite_empreinte_stable_et_sensible():
    serie = marche(50, 1)
    a, b = Qualite(T0, T0 + 3600, T0), Qualite(T0, T0 + 3600, T0)
    a.ajouter(serie)
    b.ajouter(list(reversed(serie)))          # l'ordre de lecture n'y fait rien
    assert a.empreinte() == b.empreinte()
    modifiee = serie[:10] + [bougie(10, serie[10].open, serie[10].high,
                                    serie[10].low, serie[10].close,
                                    ticks=serie[10].tick_count + 1)]
    c = Qualite(T0, T0 + 3600, T0)
    c.ajouter(modifiee + serie[11:])
    assert c.empreinte() != a.empreinte()


def test_qualite_paire_suivie_plus_tard_n_est_pas_un_trou():
    serie = [bougie(i, 1.0, 1.01, 0.99, 1.0) for i in range(30, 40)]
    q = Qualite(T0, T0 + 3600, T0 + 3600)
    q.ajouter(serie)
    p = q.resultat()["paires"][PAIRE]
    assert p["non_suivies"] == 30 and p["trous"] == 0


# --------------------------------------------------------------------------- #
# EXP-002 — indicateurs sur toutes les bougies
# --------------------------------------------------------------------------- #

def _attendu(serie: list[Candle], sec: int, coupure: int) -> dict:
    """Les cases recalculées bougie par bougie avec les fonctions PURES."""
    besoin = besoin_macd()
    closes = {b.ts_sec: b.close for b in serie}
    cases: dict[str, list[int]] = {}
    hist_prec = None
    for t in range(len(serie)):
        fen = serie[max(0, t + 1 - besoin):t + 1]
        fen61 = serie[max(0, t - 60):t + 1]
        valide = (len(fen) == besoin and len(fen61) == 61
                  and fen[-1].ts_sec - fen[0].ts_sec == 60 * (besoin - 1))
        if not valide:
            hist_prec = None
            continue
        hist = macd(fen).histogramme
        croisement = (0 if hist_prec is None else
                      1 if hist_prec <= 0 < hist else
                      -1 if hist_prec >= 0 > hist else 0)
        hist_prec = hist
        r = rsi(fen[-15:], 14)
        rapport = atr(fen[-15:], 14) / atr(fen61, 60)
        noms = ["temoin"]
        if r is not None:
            noms.append("rsi_bas" if r <= RSI_BAS else
                        "rsi_haut" if r >= RSI_HAUT else "rsi_milieu")
        noms.append("macd_pos" if hist > 0 else "macd_neg")
        if croisement:
            noms.append("croise_haut" if croisement > 0 else "croise_bas")
        noms.append("atr_calme" if rapport < ATR_CALME else
                    "atr_agite" if rapport > ATR_AGITE else "atr_normal")
        c = serie[t]
        sortie = closes.get(c.ts_sec + sec)
        if sortie is None or sortie == c.close:
            continue
        hausse = sortie > c.close
        k = 0 if c.ts_sec < coupure else 2
        sens = {"rsi_haut": -1, "macd_neg": -1, "croise_bas": -1}
        for nom in noms:
            gagne = hausse if sens.get(nom, 1) > 0 else not hausse
            compte = cases.setdefault(nom, [0, 0, 0, 0])
            compte[k] += 1
            compte[k + 1] += int(gagne)
    return cases


@pytest.mark.parametrize("graine", [3, 4])
def test_le_calcul_au_fil_de_l_eau_egale_les_fonctions_pures(graine):
    """Même série, trou compris : les cases de l'accumulateur sont
    exactement celles des fonctions pures de `indicators`."""
    serie = marche(700, graine, sauter=(300,))
    coupure = T0 + 60 * 500
    acc = Indicateurs(T0, coupure)
    acc.ajouter(serie)
    res = acc.resultat()
    for sec in (300, 900):
        attendu = _attendu(serie, sec, coupure)
        obtenu = {cle: c[str(sec)]["periodes"]
                  for cle, c in res["cases"].items()
                  if c[str(sec)]["periodes"][0] + c[str(sec)]["periodes"][2]}
        assert obtenu == attendu
    assert res["cases"]["croise_haut"]["900"]["periodes"][0] > 0
    # Le trou remet tout à zéro : 103 minutes avant la première fenêtre
    # entière, puis encore 103 après le trou.
    assert res["exclusions"]["fenetre_incomplete"] == 2 * (besoin_macd() - 1)


def test_semaines_paires_et_test_aveugle_deduits_des_blocs():
    serie = marche(2 * 1440, 5)
    gel = T0 + 86400
    acc = Indicateurs(T0, T0 + 86400, gel_ts=gel + 3600)   # aligné sur 0 h
    acc.ajouter(serie)
    temoin = acc.resultat()["cases"]["temoin"]["900"]
    n_total = temoin["periodes"][0] + temoin["periodes"][2]
    assert sum(n for n, _g in temoin["semaines"].values()) == n_total
    assert temoin["paires"][PAIRE][0] == n_total
    # Le gel est ramené au jour : tout le deuxième jour est « aveugle ».
    assert temoin["gel"][0] == temoin["periodes"][2]


def test_indicateurs_a_l_entree_orientes_et_fenetre_trouee():
    serie = marche(200, 6)
    call = indicateurs_a_l_entree(serie, 150, True)
    put = indicateurs_a_l_entree(serie, 150, False)
    r = rsi(serie[136:151], 14)
    assert call["rsi_14"] == put["rsi_14"] == round(r, 2)
    assert call["rsi_sens"] == round(r, 2)
    assert put["rsi_sens"] == round(100 - r, 2)
    assert call["macd_sens"] == -put["macd_sens"]
    m = macd(serie[151 - besoin_macd():151])
    assert call["macd_hist"] == m.histogramme
    assert call["atr_pct"] > 0
    troue = marche(200, 6, sauter=(140,))
    # La minute 140 manque : RSI et MACD de l'indice 149 (minute 150)
    # n'ont pas de fenêtre entière ; rien n'est réparé.
    assert indicateurs_a_l_entree(troue, 149, True) == {}
    assert "rsi_14" in indicateurs_a_l_entree(troue, 160, True)


def test_les_jours_de_collecte_reelle():
    """L'union des minutes collectées, toutes paires confondues : deux
    paires sur la même minute ne la comptent qu'une fois, une minute
    absente ne compte pas."""
    q = Qualite(T0, T0 + 2 * 86400, T0 + 86400)
    q.ajouter(marche(1440, 1, paire="A_otc"))                 # le jour 1
    q.ajouter(marche(720, 2, paire="B_otc"))                  # sa moitié
    assert q.jours_collectes(T0, T0 + 2 * 86400) == 1.0
    assert q.jours_collectes(T0, T0 + 720 * 60) == 0.5
    assert q.minutes_par_jour() == {T0 // 86400: 1440}
