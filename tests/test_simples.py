"""Trois stratégies simples sur toutes les bougies M1 (EXP-030, /simples)."""

from __future__ import annotations

import re

from maxprofit.apprentissage import simples as acc
from maxprofit.apprentissage.simples import REGLES, StrategiesSimples
from maxprofit.core.types import Candle
from maxprofit.live import preinscriptions
from maxprofit.live.preinscriptions import (
    ECHEANCES_S, N_MIN_S, PREINSCRIPTIONS_S, ouverture_s)
from maxprofit.live.simples import hypotheses, simples, texte

T0 = 1_790_000_040 // 60 * 60
LOIN = 10 ** 12


def _bougies(closes, debut=T0, sauter=(), pair="EURUSD_otc"):
    """Une bougie par clôture, ouverte à la clôture précédente : la couleur
    suit le sens du mouvement. `sauter` : indices des minutes absentes."""
    sortie, precedent = [], closes[0] - 0.0001
    for i, close in enumerate(closes):
        if i not in sauter:
            sortie.append(Candle(
                pair=pair, tf_sec=60, ts_sec=debut + 60 * i, open=precedent,
                high=max(precedent, close), low=min(precedent, close),
                close=close, tick_count=10, complete=True))
        precedent = close
    return sortie


def _comptes(a, cle, sec=60):
    an, ag, rn, rg = a.periodes[cle][sec]
    return an + rn, ag + rg


def test_le_momentum_compte_la_7e_verte_seulement():
    # Une rouge, puis 9 vertes, puis une rouge : un seul signal, à la 7e.
    closes = [1.0, 0.999] + [1.0 + 0.001 * k for k in range(9)] + [1.0]
    a = StrategiesSimples(T0, LOIN, LOIN)
    a.ajouter(_bougies(closes))
    assert _comptes(a, "mom7_vertes") == (1, 1), "la 8e verte gagne"
    assert _comptes(a, "mom7_rouges") == (0, 0)
    # Une minute absente dans la série : pas de signal.
    trou = StrategiesSimples(T0, LOIN, LOIN)
    trou.ajouter(_bougies(closes, sauter={5}))
    assert _comptes(trou, "mom7_vertes") == (0, 0)
    # Le miroir : 7 rouges, puis une rouge de plus — le PUT gagne.
    miroir = StrategiesSimples(T0, LOIN, LOIN)
    miroir.ajouter(_bougies([1.0, 1.001] + [1.0 - 0.001 * k
                                            for k in range(9)]))
    assert _comptes(miroir, "mom7_rouges") == (1, 1)


def test_le_croisement_de_moyennes_suit_le_prix_apres_la_chauffe():
    # 160 minutes de baisse, puis la hausse : la rapide repasse au-dessus.
    closes = ([1.0 - 0.0001 * i for i in range(160)]
              + [0.984 + 0.0003 * i for i in range(1, 80)])
    a = StrategiesSimples(T0, LOIN, LOIN)
    a.ajouter(_bougies(closes))
    for r, l in acc.COUPLES_EMA:
        n, g = _comptes(a, f"ema_{r}_{l}")
        assert n == 1 and g == 1, (r, l, n, g)
    # Pendant la chauffe, aucun croisement n'est lu : la baisse dès la
    # deuxième minute ne fait pas un PUT.
    courte = StrategiesSimples(T0, LOIN, LOIN)
    courte.ajouter(_bougies(closes[:60]))
    assert all(_comptes(courte, f"ema_{r}_{l}") == (0, 0)
               for r, l in acc.COUPLES_EMA)


def test_bollinger_parie_le_retour_vers_la_mediane():
    # 20 minutes calmes, 3 vertes qui s'écartent, puis le retour.
    calme = [1.0 + 0.0001 * ((i + 1) % 2) for i in range(20)]  # finit rouge
    montee = [1.001, 1.002, 1.003]
    retour = [1.002, 1.0015]          # deux rouges : pas de série de 3
    a = StrategiesSimples(T0, LOIN, LOIN)
    a.ajouter(_bougies(calme + montee + retour))
    n, g = _comptes(a, "bb_mediane")
    assert n == 1 and g == 1, "la 3e verte au-dessus de la médiane : PUT"
    n, g = _comptes(a, "bb_bande")
    assert n == 1 and g == 1, "et hors de la bande haute"


def test_anciens_recents_avant_et_fenetre():
    closes = [1.0, 0.999] + [1.0 + 0.001 * k for k in range(9)] + [1.0]
    jour = 86400
    debut = (T0 // jour + 3) * jour + 600
    a = StrategiesSimples(debut - 30 * jour, debut, debut + 2 * jour)
    a.ajouter(_bougies(closes, debut=debut - jour))       # anciens, avant
    a.ajouter(_bougies(closes, debut=debut + 3 * jour,    # récents, fenêtre
                       pair="GBPUSD_otc"))
    assert a.periodes["mom7_vertes"][60] == [1, 1, 1, 1]
    cellule = a.resultat()["regles"]["mom7_vertes"]["60"]
    assert cellule["avant"] == [1, 1]
    assert cellule["blocs_fenetre"] == [[1, 1]]


# --------------------------------------------------------------------------- #
# Le rapport
# --------------------------------------------------------------------------- #

def _res(cellules=None, fenetre=None):
    """Un `resultat()` synthétique : {(règle, échéance): (périodes, avant)}
    et {(règle, échéance): blocs de la fenêtre}."""
    cellules, fenetre = cellules or {}, fenetre or {}
    regles = {}
    for cle, _f, _l in REGLES:
        regles[cle] = {}
        for sec in acc.ECHEANCES:
            periodes, avant = cellules.get(
                (cle, sec), ([7000, 3500, 3000, 1500], [10000, 5000]))
            regles[cle][str(sec)] = {
                "periodes": periodes, "avant": avant,
                "blocs": [[periodes[0] // 10 + periodes[2] // 10,
                           periodes[1] // 10 + periodes[3] // 10]] * 10,
                "blocs_fenetre": fenetre.get((cle, sec), [])}
    return {"debut": T0, "coupure": T0 + 20 * 86400,
            "ouverture": ouverture_s(), "echeances": list(acc.ECHEANCES),
            "parametres": {}, "paires": {"EURUSD_otc": 1}, "regles": regles}


def test_le_rapport_marque_et_compte_ce_que_ferait_le_hasard():
    r = simples(_res({("mom7_vertes", 60): ([341, 197, 742, 390],
                                            [1083, 587]),
                      ("bb_bande", 300): ([400, 180, 200, 90], [600, 270])}),
                ancien=None, fin=ouverture_s() - 3600)
    assert r["regles"]["mom7_vertes"]["60"]["marque"] == "✅"
    assert r["regles"]["bb_bande"]["300"]["marque"] == "🔁"
    assert r["regles"]["ema_9_21"]["60"]["marque"] == ""
    marquees, attendues, total = r["cases"]
    assert (marquees, total) == (2, len(REGLES) * len(acc.ECHEANCES))
    assert 0 < attendues < 5
    assert "blocs" not in r["regles"]["mom7_vertes"]["60"], "rien de lourd"
    assert r["fenetre"]["note"] == "pas encore ouverte"
    t = texte(r)
    assert "7 vertes de suite → CALL (suivre), 1 min : 54.2% sur 1083" in t
    assert "À L’ENVERS" in t
    nu = re.sub(r"</?b>", "", t)
    assert "<" not in nu and ">" not in nu and "&" not in nu


def test_la_fenetre_fige_son_choix_puis_son_verdict():
    ouv = ouverture_s()
    meilleure = {("ema_20_50", 300): ([300, 170, 100, 60], [400, 240])}
    blocs = [[40, 22]] * 10                     # 400 signaux, 55 %
    r1 = simples(_res(meilleure, {("ema_20_50", 300): blocs[:2],
                                  ("mom7_vertes", 60): blocs[:2]}),
                 ancien=None, fin=ouv + 86400)
    f1 = r1["fenetre"]
    assert f1["choix"]["S-EMA"]["regle"] == "ema_20_50"
    assert f1["choix"]["S-EMA"]["echeance"] == 300
    assert f1["choix"]["S-MOM7"]["regle"] == "mom7_vertes"
    assert f1["verdicts"]["S-EMA"]["verdict"].startswith("indécise")
    # Le lendemain, une autre case serait meilleure : le choix ne bouge pas.
    autre = {("ema_9_21", 60): ([300, 210, 100, 70], [400, 280])}
    r2 = simples(_res(autre, {("ema_20_50", 300): blocs,
                              ("mom7_vertes", 60): blocs[:3]}),
                 ancien=r1, fin=ouv + 5 * 86400)
    f2 = r2["fenetre"]
    assert f2["choix"]["S-EMA"]["regle"] == "ema_20_50"
    assert f2["verdicts"]["S-EMA"]["n"] == 400 >= N_MIN_S
    assert f2["verdicts"]["S-EMA"]["final"]
    # Le volume ne suffit pas : trois jours d'abord.
    tot = simples(_res(autre, {("ema_20_50", 300): blocs}), ancien=r1,
                  fin=ouv + 2 * 86400)["fenetre"]["verdicts"]["S-EMA"]
    assert "final" not in tot and "au plus tôt le" in tot["verdict"]
    # Une fois le volume atteint, le verdict est figé.
    r3 = simples(_res(autre, {("ema_20_50", 300): [[40, 10]] * 10}),
                 ancien=r2, fin=ouv + 9 * 86400)
    assert r3["fenetre"]["verdicts"]["S-EMA"] == f2["verdicts"]["S-EMA"]
    idents = [h[0] for h in hypotheses(r3)]
    assert {"S-MOM7", "S-EMA", "S-BB"} <= set(idents)
    assert "EXP-030 mom7_vertes" in idents
    assert "S-EMA" in texte(r3)


def test_les_pre_inscriptions_designent_des_regles_mesurees():
    cles = {cle for cle, _f, _l in REGLES}
    assert ECHEANCES_S == acc.ECHEANCES
    for p in PREINSCRIPTIONS_S:
        assert set(p.parametres["regles"]) <= cles
        assert set(p.parametres["echeances"]) <= set(acc.ECHEANCES)
    assert preinscriptions.DECLAREES_S < preinscriptions.OUVERTURE_S
