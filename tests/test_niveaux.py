"""
Les deux règles des vidéos du trader OTC (recherche isolée) : le gel, le
regroupement M5/M15, les niveaux des vendeurs et des acheteurs, l'impulsion,
la lecture du futur, la minute et le tick du toucher (contrôlés contre une
recherche brute sur les ticks), la continuation après repli, les épisodes,
les issues aux ticks, les comptes jouables, les verdicts, l'isolation du
module, une passe de bout en bout en lecture seule, l'essai sans taux, et la
commande.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import math
import random
import shutil
from bisect import bisect_left
from collections import Counter
from pathlib import Path

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core.types import Tick
from maxprofit.live import engulfing as en
from maxprofit.live import niveaux as nv
from maxprofit.store.db import open_read_only, open_read_write
from tests.test_engulfing import FIN_DONNEES, Espion, _base, _chemins

RACINE = Path(__file__).resolve().parents[1]
T0 = (nv.DEBUT + 10 * 86400) // 3600 * 3600
PAIRE = "EURUSD_otc"


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def B(i: int, o: float, h: float, l: float, c: float) -> en.Bougie:
    return en.Bougie(T0 + 60 * i, o, h, l, c)


def _marche(n: int, graine: int = 3):
    alea = random.Random(graine)
    prix, sortie = 1.1000, []
    for i in range(n):
        o = prix
        c = round(o * math.exp(alea.gauss(0, 3e-4)), 5)
        h = round(max(o, c) * (1 + abs(alea.gauss(0, 1e-4))), 5)
        l = round(min(o, c) * (1 - abs(alea.gauss(0, 1e-4))), 5)
        sortie.append(B(i, o, h, l, c))
        prix = c
    return sortie


def _cle(niv: nv.Niveau):
    return (niv.activation, niv.prix, niv.sens,
            None if niv.impulsion is None else round(niv.impulsion, 9))


def test_le_gel_tient():
    doc = json.loads((RACINE / "docs" / "preinscription_niveaux.json")
                     .read_text(encoding="utf-8"))
    assert doc == nv.PARAMETRES
    assert em.empreinte(nv.PARAMETRES) == nv.EMPREINTE
    autre = json.loads(json.dumps(nv.PARAMETRES))
    autre["R1_niveaux"]["unites_min"] = [1, 5]
    with pytest.raises(ValueError):
        em.exiger_le_gel(autre, nv.EMPREINTE)
    assert nv.DEFINITIONS == (
        "R1_M1_w5_s0", "R1_M1_w5_s3", "R1_M1_w10_s0", "R1_M1_w10_s3",
        "R1_M5_w5_s0", "R1_M5_w5_s3", "R1_M5_w10_s0", "R1_M5_w10_s3",
        "R1_M15_w5_s0", "R1_M15_w5_s3", "R1_M15_w10_s0", "R1_M15_w10_s3",
        "R2_continuation")
    assert nv.ECHEANCES == (1, 2, 3, 5) and nv.UNITES == (1, 5, 15)
    assert nv.FENETRES == (5, 10) and nv.SEUILS == (0.0, 3.0)
    assert nv.MEDIANE_N == 50 and nv.VALIDITE_SEC == 86400
    assert (nv.DELAI_MS, nv.FRAICHEUR_MS, nv.EPISODE_MS) == (
        2000, 10000, 300000)
    assert (nv.N_EXPLO_MIN, nv.N_VALID_MIN) == (300, 100)
    assert nv.libelle("R1_M5_w10_s3") == "R1 niveaux M5, pivot 10, impulsion ≥ 3"
    assert nv.libelle("R2_continuation") == "R2 continuation après repli"


def test_le_regroupement_n_existe_que_complet():
    bougies = [B(i, 1.0 + i / 1000, 1.0 + i / 1000 + 0.0005,
                 1.0 + i / 1000 - 0.0005, 1.0 + i / 1000 + 0.0002)
               for i in range(15)]
    m5 = nv.regrouper(bougies, 5)
    assert [b.ts_sec for b in m5] == [T0, T0 + 300, T0 + 600]
    b = m5[1]
    assert (b.open, b.close) == (bougies[5].open, bougies[9].close)
    assert b.high == max(x.high for x in bougies[5:10])
    assert b.low == min(x.low for x in bougies[5:10])
    trouee = bougies[:7] + bougies[8:]                 # une minute manque
    m5 = nv.regrouper(trouee, 5)
    assert [b.ts_sec for b in m5] == [T0, T0 + 600]
    su = nv.SerieU(m5, 300)
    assert not su.continue_(1, 2), "la période incomplète fait un trou"
    assert nv.regrouper(bougies, 15)[0].ts_sec == T0
    assert nv.regrouper(bougies, 1) == bougies


def _base_plate():
    """50 bougies d'étendue 0,0002 (sans pivot strict : plus hauts et plus
    bas égaux), pour que l'étendue médiane soit connue."""
    return [B(i, 1.1000, 1.1002, 1.1000, 1.1001) for i in range(50)]


def _sommet(rouge_au_sommet: bool = False, confirmation_close=1.0997,
            egalite: bool = False):
    """Une montée de 5 bougies, le sommet (indice 55), puis 5 bougies qui
    descendent ; la confirmation est l'indice 60."""
    b = _base_plate()
    montee = [(1.1001, 1.1004, 1.1000, 1.1003), (1.1003, 1.1006, 1.1002, 1.1005),
              (1.1005, 1.1008, 1.1004, 1.1007), (1.1007, 1.1010, 1.1006, 1.1009),
              (1.1009, 1.1012, 1.1008, 1.1011)]
    for k, x in enumerate(montee):
        b.append(B(50 + k, *x))
    if rouge_au_sommet:
        b.append(B(55, 1.1015, 1.1020, 1.1010, 1.1012))
    else:
        b.append(B(55, 1.1011, 1.1020, 1.1010, 1.1018))
    descente = [(1.1018, 1.1020 if egalite else 1.1019, 1.1012, 1.1013),
                (1.1013, 1.1014, 1.1008, 1.1009),
                (1.1009, 1.1010, 1.1004, 1.1005),
                (1.1005, 1.1006, 1.1000, 1.1001)]
    for k, x in enumerate(descente):
        b.append(B(56 + k, *x))
    haut = max(1.1002, confirmation_close)
    b.append(B(60, 1.1001, haut, 1.0996, confirmation_close))
    return b


def _miroir(bougies):
    """Le même graphique retourné : un creux à la place du sommet."""
    return [en.Bougie(x.ts_sec, 2.2 - x.open, 2.2 - x.low, 2.2 - x.high,
                      2.2 - x.close) for x in bougies]


def test_le_sommet_donne_le_niveau_des_vendeurs():
    niveaux = [n for n in nv.niveaux_de(nv.SerieU(_sommet()), 5)
               if n.sens == nv.PUT]
    assert len(niveaux) == 1
    n = niveaux[0]
    assert n.prix == 1.1018, "ouverture de la première bougie rouge (56)"
    assert n.activation == T0 + 60 * 61, "clôture de la confirmation (60)"
    # Écart 1,1018 − 1,0996 ; étendue médiane des 50 bougies d'avant : 0,0002.
    assert n.impulsion == pytest.approx(0.0022 / 0.0002)
    rouge = [n for n in nv.niveaux_de(nv.SerieU(_sommet(True)), 5)
             if n.sens == nv.PUT]
    assert [x.prix for x in rouge] == [1.1015], "le sommet rouge compte"


def test_le_creux_donne_le_niveau_des_acheteurs():
    niveaux = [n for n in nv.niveaux_de(nv.SerieU(_miroir(_sommet())), 5)
               if n.sens == nv.CALL]
    assert len(niveaux) == 1
    n = niveaux[0]
    assert n.prix == pytest.approx(2.2 - 1.1018)
    assert n.activation == T0 + 60 * 61
    assert n.impulsion == pytest.approx(11.0)


def test_les_niveaux_ecartes():
    mauvais_cote = _sommet(confirmation_close=1.1019)
    assert not [n for n in nv.niveaux_de(nv.SerieU(mauvais_cote), 5)
                if n.sens == nv.PUT], "la confirmation clôture au-dessus"
    assert not [n for n in nv.niveaux_de(nv.SerieU(_sommet(egalite=True)), 5)
                if n.sens == nv.PUT], "un plus haut égal : pas de sommet strict"
    sans_rouge = _sommet()
    for k in range(56, 61):
        x = sans_rouge[k]
        sans_rouge[k] = en.Bougie(x.ts_sec, x.low, x.high, x.low, x.high)
    assert not [n for n in nv.niveaux_de(nv.SerieU(sans_rouge), 5)
                if n.sens == nv.PUT], "aucune bougie rouge : pas de niveau"
    suite = _sommet() + [B(61 + k, 1.0997, 1.0998, 1.0995, 1.0996)
                         for k in range(6)]
    assert len([n for n in nv.niveaux_de(nv.SerieU(suite), 5)
                if n.sens == nv.PUT]) == 1
    del suite[58]
    assert not [n for n in nv.niveaux_de(nv.SerieU(suite), 5)
                if n.sens == nv.PUT], "les 11 bougies du pivot doivent se suivre"
    court = _sommet()[10:]                # 40 bougies avant le pivot
    n, = [x for x in nv.niveaux_de(nv.SerieU(court), 5) if x.sens == nv.PUT]
    assert n.impulsion is None
    assert nv.passe_le_seuil(n, 0.0) and not nv.passe_le_seuil(n, 3.0)


def test_le_seuil_d_impulsion():
    n = nv.Niveau(0, 1.0, nv.PUT, 2.99)
    assert nv.passe_le_seuil(n, 0.0) and not nv.passe_le_seuil(n, 3.0)
    n.impulsion = 3.0
    assert nv.passe_le_seuil(n, 3.0)


def test_le_futur_ne_change_aucun_niveau():
    bougies = _marche(1500, graine=17)
    for u in nv.UNITES:
        complet_u = nv.regrouper(bougies, u)
        for w in nv.FENETRES:
            complet = nv.niveaux_de(nv.SerieU(complet_u, 60 * u), w)
            assert complet, (u, w)
            for coupe in (300, 451, 777):
                partiel_u = nv.regrouper(bougies[:coupe], u)
                partiel = nv.niveaux_de(nv.SerieU(partiel_u, 60 * u), w)
                fin = bougies[coupe - 1].ts_sec + 60
                assert [_cle(x) for x in partiel] == [
                    _cle(x) for x in complet if x.activation <= fin], (u, w)


def test_la_minute_du_toucher():
    bougies = [B(i, 1.1000, 1.1002, 1.0998, 1.1001) for i in range(30)]
    bougies[12] = B(12, 1.1000, 1.1010, 1.0998, 1.1001)
    s1 = nv.SerieU(bougies)
    vendeur = nv.Niveau(T0 + 60 * 5, 1.1010, nv.PUT, None)
    assert nv.minute_du_toucher(s1, vendeur) == (12, nv.TOUCHER)
    acheteur = nv.Niveau(T0 + 60 * 5, 1.0998, nv.CALL, None)
    assert nv.minute_du_toucher(s1, acheteur) == (5, nv.TOUCHER)
    loin = nv.Niveau(T0 + 60 * 5, 1.2, nv.PUT, None)
    assert nv.minute_du_toucher(s1, loin) == (None, nv.FIN_DONNEES)
    trouee = nv.SerieU(bougies[:9] + bougies[10:])
    assert nv.minute_du_toucher(trouee, vendeur) == (None, nv.TROU)
    absente = nv.Niveau(T0 + 60 * 9, 1.1010, nv.PUT, None)
    assert nv.minute_du_toucher(trouee, absente) == (None, nv.TROU)
    jour = [B(i, 1.1000, 1.1002, 1.0998, 1.1001) for i in range(1500)]
    jour[1450] = B(1450, 1.1000, 1.1010, 1.0998, 1.1001)
    vingt_quatre = nv.Niveau(T0, 1.1010, nv.PUT, None)
    assert nv.minute_du_toucher(nv.SerieU(jour), vingt_quatre) == (
        None, nv.SANS_TOUCHER)
    jour[1439] = B(1439, 1.1000, 1.1010, 1.0998, 1.1001)
    assert nv.minute_du_toucher(nv.SerieU(jour), vingt_quatre) == (
        1439, nv.TOUCHER), "la dernière minute des 24 h compte"


def test_le_tick_du_toucher():
    m = T0 + 600
    ticks = [Tick(PAIRE, (m + s) * 1000, p) for s, p in
             ((0, 1.1000), (10, 1.1004), (20, 1.1011), (30, 1.1009))]
    t = en.Ticks(_chemins(ticks))
    vendeur = nv.Niveau(m, 1.1010, nv.PUT, None)
    assert nv.tick_du_toucher(vendeur, m, t) == ((m + 20) * 1000,
                                                  nv.TOUCHER_TICK)
    acheteur = nv.Niveau(m, 1.1001, nv.CALL, None)
    assert nv.tick_du_toucher(acheteur, m, t) == (m * 1000, nv.TOUCHER_TICK)
    apres = nv.Niveau(m + 1, 1.1001, nv.CALL, None)
    assert nv.tick_du_toucher(apres, m, t) == (None, nv.TOUCHER_SANS_TICK), \
        "un tick d'avant l'activation ne compte pas"
    assert nv.tick_du_toucher(vendeur, m + 60, t) == (None, nv.TICKS_TOUCHER)


def _couleurs(motif: str, debut: int = 0):
    """V : verte, R : rouge, D : sans corps ; un espace : une minute qui
    manque."""
    sortie, i = [], debut
    for x in motif:
        if x != " ":
            o, c = {"V": (1.1, 1.1001), "R": (1.1001, 1.1),
                    "D": (1.1, 1.1)}[x]
            sortie.append(B(i, o, 1.1002, 1.0999, c))
        i += 1
    return sortie


def test_la_continuation_apres_repli():
    def r2(motif):
        return nv.r2_continuation(nv.SerieU(_couleurs(motif)))
    assert r2("VVVRV") == [(4, nv.CALL)]
    assert r2("VVVRRV") == [(5, nv.CALL)]
    assert r2("RRRVR") == [(4, nv.PUT)]
    assert r2("RRRRVVR") == [(6, nv.PUT)], "une série plus longue compte"
    for motif in ("VVRV", "VVVRRRV", "VVVDV", "VVVRD", "VDVVRV", "VV VRV",
                  "VVVR V", "VVVV"):
        assert r2(motif) == [], motif
    assert r2("VVVRVRV") == [(4, nv.CALL)], \
        "la reprise ne refait pas une série de trois"
    assert r2("VVVRVVVRV") == [(4, nv.CALL), (8, nv.CALL)]


def _flux_ticks(debut: int, prix, pas: int = 5, minutes: int = 12):
    return [Tick(PAIRE, t * 1000, prix(t))
            for t in range(debut, debut + 60 * minutes, pas)]


def test_issues_aux_ticks_gain_perte_egalite_et_entree_a_deux_secondes():
    ref = T0 + 600
    monte = _flux_ticks(ref - 60, lambda t: 1.1000 + 0.0001 * ((t - ref) // 60))
    sig = nv.Signal(0, PAIRE, ref * 1000, nv.CALL)
    nv.issues_ticks(sig, en.Ticks(_chemins(monte)))
    assert sig.issues == {1: 1, 2: 1, 3: 1, 5: 1}
    sig = nv.Signal(0, PAIRE, ref * 1000, nv.PUT)
    nv.issues_ticks(sig, en.Ticks(_chemins(monte)))
    assert sig.issues == {1: -1, 2: -1, 3: -1, 5: -1}
    plat = _flux_ticks(ref - 60, lambda t: 1.1000)
    sig = nv.Signal(0, PAIRE, ref * 1000, nv.CALL)
    nv.issues_ticks(sig, en.Ticks(_chemins(plat)))
    assert sig.issues == {1: 0, 2: 0, 3: 0, 5: 0}, "égalité notée 0"
    ticks = [Tick(PAIRE, (ref - 30) * 1000, 1.1000),
             Tick(PAIRE, ref * 1000 + 1500, 1.1010),       # connu à + 2 s
             Tick(PAIRE, ref * 1000 + 2500, 1.0990)]
    ticks += _flux_ticks(ref + 5, lambda t: 1.1005, minutes=7)
    sig = nv.Signal(0, PAIRE, ref * 1000, nv.CALL)
    nv.issues_ticks(sig, en.Ticks(_chemins(ticks)))
    assert sig.entree_ms == ref * 1000 + 2000
    assert sig.issues[1] == -1, "entrée à 1,1010, sortie à 1,1005"


def test_donnees_manquantes_et_flux_gele():
    ref = T0 + 600
    ticks = _flux_ticks(ref - 60, lambda t: 1.1000, minutes=8)
    sig = nv.Signal(0, PAIRE, ref * 1000, nv.CALL)
    nv.issues_ticks(sig, en.Ticks(_chemins(ticks, {ref + 180})))
    assert [sig.statuts[t] for t in nv.ECHEANCES] == [
        nv.ISSUE, nv.ISSUE, nv.MANQUE, nv.MANQUE]
    vieux = [Tick(PAIRE, (ref - 30) * 1000, 1.1000)]
    sig = nv.Signal(0, PAIRE, ref * 1000, nv.CALL)
    nv.issues_ticks(sig, en.Ticks(_chemins(vieux)))
    assert set(sig.statuts.values()) == {nv.MANQUE_ENTREE}
    gel = _flux_ticks(ref - 60, lambda t: 1.1000, minutes=3)
    gel += [Tick(PAIRE, (ref + 60 * m + 30) * 1000, 1.1) for m in range(2, 8)]
    sig = nv.Signal(0, PAIRE, ref * 1000, nv.CALL)
    nv.issues_ticks(sig, en.Ticks(_chemins(gel)))
    assert [sig.statuts[t] for t in nv.ECHEANCES] == [
        nv.ISSUE, nv.ISSUE, nv.GELE, nv.GELE]


def _ticks_et_bougies(minutes: int, graine: int = 21):
    """Des ticks toutes les 5 s (marche au hasard) et les bougies M1 qui en
    sont faites, à partir d'un début de jour."""
    alea = random.Random(graine)
    debut = T0 // 86400 * 86400
    prix, ticks, bougies = 1.1000, [], []
    for k in range(minutes):
        m = debut + 60 * k
        px = []
        for s in range(0, 60, 5):
            prix = round(prix * math.exp(alea.gauss(0, 6e-5)), 5)
            ticks.append(Tick(PAIRE, (m + s) * 1000, prix))
            px.append(prix)
        bougies.append(en.Bougie(m, px[0], max(px), min(px), px[-1]))
    return debut, ticks, bougies


def _episodes_bruts(touchers, fin):
    """(référence ms, sens) triés → ceux qu'on garde, à la main."""
    gardes, derniere = [], None
    for t, sens in touchers:
        if derniere is not None and t + 2000 < derniere + 300_000:
            continue
        derniere = t + 2000
        if t + 2000 + 300_000 <= fin * 1000:
            gardes.append((t, sens))
    return gardes


def test_les_trades_contre_une_recherche_brute_sur_les_ticks():
    """Pour chaque définition R1, le premier tick qui atteint chaque niveau
    est cherché tick par tick, sans les bougies ; les épisodes sont rejoués
    à la main. La passe doit trouver exactement les mêmes trades."""
    debut, ticks, bougies = _ticks_et_bougies(2 * 1440 + 300)
    fin = bougies[-1].ts_sec + 60
    temps = [t.ts_ms for t in ticks]
    compte: Counter = Counter()
    signaux = nv.paire(PAIRE, bougies, fin, en.Ticks(_chemins(ticks)),
                       lambda t: 92.0, compte, lambda: None)
    par_def: dict[int, list] = {}
    for s in signaux:
        par_def.setdefault(s.definition, []).append((s.reference_ms, s.sens))
    total = 0
    for u in nv.UNITES:
        su = nv.SerieU(nv.regrouper(bougies, u), 60 * u)
        for w in nv.FENETRES:
            tous = nv.niveaux_de(su, w)
            for s in nv.SEUILS:
                k = nv.DEFINITIONS.index(f"R1_M{u}_w{w}_s{int(s)}")
                touchers = []
                for niv in tous:
                    if not nv.passe_le_seuil(niv, s):
                        continue
                    limite = (niv.activation + nv.VALIDITE_SEC) * 1000
                    for j in range(bisect_left(temps, niv.activation * 1000),
                                   len(ticks)):
                        if temps[j] >= limite:
                            break
                        p = ticks[j].price
                        if (p >= niv.prix) if niv.sens == nv.PUT \
                                else (p <= niv.prix):
                            touchers.append((temps[j], niv.sens))
                            break
                touchers.sort(key=lambda x: x[0])
                attendus = _episodes_bruts(touchers, fin)
                assert par_def.get(k, []) == attendus, nv.DEFINITIONS[k]
                total += len(attendus)
    assert total > 200
    s1 = nv.SerieU(bougies)
    r2 = _episodes_bruts([((s1.ts[i] + 60) * 1000, sens)
                          for i, sens in nv.r2_continuation(s1)], fin)
    assert par_def[nv.R2] == r2 and r2
    for s in signaux:
        assert set(s.statuts) == set(nv.ECHEANCES) and s.payout == 92.0
        assert all(s.statuts[t] == nv.ISSUE for t in nv.ECHEANCES)
    assert [s.reference_ms for s in signaux] == sorted(
        s.reference_ms for s in signaux)
    for d in nv.DEFINITIONS[:-1]:
        formes = compte[("niveaux", d, "formés")]
        assert formes == sum(n for (src, dd, st), n in compte.items()
                             if src == "niveaux" and dd == d and st in (
                                 nv.TOUCHER, nv.TROU, nv.SANS_TOUCHER,
                                 nv.FIN_DONNEES))
        assert compte[("niveaux", d, nv.TOUCHER)] == compte[
            ("niveaux", d, nv.TOUCHER_TICK)], "bougies faites des ticks"


def test_sans_ticks_aucun_trade_r1():
    _, ticks, bougies = _ticks_et_bougies(600)
    compte: Counter = Counter()
    signaux = nv.paire(PAIRE, bougies, bougies[-1].ts_sec + 60, None,
                       lambda t: 92.0, compte, lambda: None)
    assert signaux and {s.definition for s in signaux} == {nv.R2}
    assert all(not s.issues for s in signaux)
    assert any(cle[2] == nv.SANS_TICKS for cle in compte)


def _signal(issues, payout=92.0, ref=None, sens=nv.CALL, definition=0):
    s = nv.Signal(definition, PAIRE, ref or (T0 + 600) * 1000, sens)
    s.payout = payout
    s.issues = dict(issues)
    s.statuts = {t: nv.ISSUE for t in issues}
    return s


def test_seul_le_jouable_compte_et_l_egalite_perd():
    ag = nv.Agregats()
    ag.ajouter([_signal({1: 1, 2: 0, 3: -1, 5: 1}),
                _signal({1: 1, 2: 1, 3: 1, 5: 1}, payout=80.0),
                _signal({1: 1, 2: 1, 3: 1, 5: 1}, payout=None)])
    per = nv.EXPLORATION
    assert ag.total(0, 1, per) == [1, 0, 0]
    assert ag.total(0, 2, per) == [0, 0, 1]
    assert ag.total(0, 3, per) == [0, 1, 0]
    lignes = ag.cases(ag.tirages(), lambda: None)
    assert len(lignes) == len(nv.DEFINITIONS) * 4 * 2
    l2 = next(l for l in lignes if l["definition"] == nv.DEFINITIONS[0]
              and l["echeance_min"] == 2 and l["periode"] == per)
    assert l2["n"] == 1 and l2["taux"] == 0.0, "une égalité est une perte"
    l3 = next(l for l in lignes if l["definition"] == nv.DEFINITIONS[0]
              and l["echeance_min"] == 3 and l["periode"] == per)
    assert l3["sens_choisi"] == nv.INVERSE and l3["taux"] == 1.0
    assert ag.n_signaux == 3
    assert ag.statuts[("jouabilité", nv.DEFINITIONS[0], per, "non jouable",
                       "")] == 2


def test_les_verdicts_et_benjamini_hochberg():
    X, V = nv.EXPLORATION, nv.VALIDATION

    def l(d, per, n, taux, p=None):
        return {"definition": d, "echeance_min": 1, "periode": per, "n": n,
                "taux": taux, "p": p, "verdict": ""}
    lignes = [l("A", X, 299, 0.60), l("A", V, 150, 0.60, 0.01),
              l("B", X, 400, 0.52), l("B", V, 150, 0.60, 0.01),
              l("C", X, 400, 0.55), l("C", V, 99, 0.60, 0.01),
              l("D", X, 400, 0.55), l("D", V, 150, 0.58, 0.001),
              l("E", X, 400, 0.55), l("E", V, 150, 0.50, 0.60)]
    nv._verdicts(lignes)
    v = {(x["definition"], x["periode"]): x["verdict"] for x in lignes}
    assert v[("A", X)] == "trop peu en exploration"
    assert v[("B", X)] == "non retenue en exploration"
    assert v[("C", X)] == "retenue, trop peu en validation"
    assert v[("C", V)] == "trop peu en validation"
    assert v[("D", X)] == "retenue en exploration"
    assert v[("D", V)] == "validée (BH 10 %)"
    assert v[("E", V)] == "non reproduite"


def test_les_cases_attendues_au_hasard():
    lignes = [{"periode": "exploration", "n": 1000},
              {"periode": "exploration", "n": 100},
              {"periode": "validation", "n": 1000}]
    z = (nv.POINT_MORT - 0.5) * 2 * math.sqrt(1000)
    attendu = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))
    assert abs(nv.attendues_au_hasard(lignes) - attendu) < 1e-12


def test_le_module_est_isole():
    source = (RACINE / "maxprofit" / "live" / "niveaux.py").read_text(
        encoding="utf-8")
    arbre = ast.parse(source)
    importes = set()
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.ImportFrom) and noeud.module:
            importes.add(noeud.module)
        elif isinstance(noeud, ast.Import):
            importes.update(a.name for a in noeud.names)
    for interdit in ("maxprofit.execution", "maxprofit.collect",
                     "maxprofit.plan", "maxprofit.live.plan_demo",
                     "maxprofit.hosting", "maxprofit.strategies"):
        assert not any(m == interdit or m.startswith(interdit + ".")
                       for m in importes), interdit
    for mot in ("INSERT", "UPDATE ", "DELETE", "upsert", "insert_"):
        assert mot not in source, mot


@pytest.fixture(scope="module")
def passe(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("niveaux")
    active = dossier / "active.db"
    _base(active)
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    empreintes = [hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (active, releve)]
    a, r = open_read_only(active), open_read_only(releve)
    espion = Espion(r)
    try:
        rapport, fichiers = nv.etude(espion, a, maintenant=FIN_DONNEES + 3600,
                                     respirer=lambda: None, version="abc1234")
    finally:
        a.close()
        r.close()
    return active, releve, empreintes, espion, rapport, fichiers


def _lignes(fichiers, nom):
    import gzip
    contenu = next(c for c, n, _ in fichiers if n == nom)
    if nom.endswith(".gz"):
        contenu = gzip.decompress(contenu)
    lignes = contenu.decode("utf-8").splitlines()
    tete = lignes[0].split(";")
    return [dict(zip(tete, l.split(";"))) for l in lignes[1:]]


def test_passe_en_lecture_seule_sans_rien_lire_apres_la_fin(passe):
    active, releve, empreintes, espion, rapport, fichiers = passe
    assert [hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (active, releve)] == empreintes
    assert "requêtes refusées : 0" in rapport and "Gel vérifié" in rapport
    assert "aucun ordre" in rapport and "Version : abc1234" in rapport
    assert espion.bornes and max(espion.bornes) <= FIN_DONNEES
    assert min(espion.bornes) >= nv.DEBUT - 86400
    assert [n for _, n, _ in fichiers] == [
        "niveaux_cases.csv", "niveaux_details.csv", "niveaux_statuts.csv",
        "niveaux_donnees.csv", "niveaux_signaux.csv.gz"]


def test_passe_complete_cases_signaux_et_statuts(passe):
    *_, rapport, fichiers = passe
    cases = _lignes(fichiers, "niveaux_cases.csv")
    assert len(cases) == 13 * 4 * 2
    assert {c["definition"] for c in cases} == set(nv.DEFINITIONS)
    assert {c["sens_choisi"] for c in cases} <= {"règle", "inverse"}
    for c in cases:
        n = int(c["n"])
        if n:
            g, l = int(c["gains_regle"]), int(c["pertes_regle"])
            attendu = (g if c["sens_choisi"] == "règle" else l) / n
            assert abs(float(c["taux"]) - attendu) < 1e-6
    assert any(int(c["n"]) for c in cases if c["periode"] == "validation")
    signaux = _lignes(fichiers, "niveaux_signaux.csv.gz")
    assert signaux and {s["sens"] for s in signaux} == {"CALL", "PUT"}
    assert {s["definition"] for s in signaux} == set(nv.DEFINITIONS)
    assert {s["periode"] for s in signaux} == {"exploration", "validation"}
    par_def = {}
    for s in signaux:
        par_def.setdefault((s["definition"], s["paire"]), []).append(
            int(s["reference_ms"]))
    for refs in par_def.values():
        assert refs == sorted(refs)
        assert all(b - a >= nv.EPISODE_MS for a, b in zip(refs, refs[1:]))
    statuts = _lignes(fichiers, "niveaux_statuts.csv")
    sources = {s["source"] for s in statuts}
    assert {"niveaux", "épisodes", "ticks"} <= sources
    donnees = _lignes(fichiers, "niveaux_donnees.csv")
    assert {d["paire"] for d in donnees} == {"EURUSD_otc", "GBPUSD_otc"}
    assert max(d["jour"] for d in donnees) < "2026-11-05"
    for definition in nv.DEFINITIONS:
        assert nv.libelle(definition) in rapport
    assert "passeraient l'exploration par hasard" in rapport
    assert "heure" not in rapport.lower(), "aucune ventilation par heure"


def test_l_essai_ne_montre_aucun_taux(passe):
    active, releve, empreintes, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    espion = Espion(r)
    try:
        texte, fichiers = nv.etude(espion, a, maintenant=FIN_DONNEES + 3600,
                                   respirer=lambda: None, essai=True,
                                   version="abc1234")
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "%" not in texte
    assert texte.startswith("📏 <b>Essai technique /niveaux</b>")
    assert "Statistiques, rapport et fichiers : passés" in texte
    assert "Version déployée : abc1234" in texte
    assert "au tick" in texte and "Projection prudente" in texte
    assert max(espion.bornes) <= FIN_DONNEES
    assert nv.avancement() is None


def test_avant_la_fin_de_l_exploration_rien_a_valider(passe):
    active, releve, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    try:
        texte, fichiers = nv.etude(r, a, maintenant=nv.FIN_EXPLORATION - 60,
                                   respirer=lambda: None)
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "rien à valider" in texte


def test_l_avancement_suit_la_passe(passe):
    assert nv.avancement() is None
    import time as horloge
    maintenant = horloge.monotonic()
    nv._ETAT.update(debut=maintenant - 600, etape="paire EURUSD_otc",
                    faites=5, total=20, signaux=900,
                    debut_paires=maintenant - 300, rss_max=250.0)
    try:
        point = nv.avancement()
        assert "5/20 paires" in point and "900 trades" in point
        assert "fin des paires dans ~15 min" in point
        assert "pic de l'étude 250 Mo" in point
    finally:
        nv._ETAT.clear()


def test_le_service_et_la_commande(monkeypatch, tmp_path):
    from maxprofit.hosting import service
    from maxprofit.store import pool
    vus = []

    def faux(lue, active, course=None, *, nom_lecture, essai=False,
             version=None):
        vus.append((nom_lecture, essai))
        return "📏 Niveaux", []

    monkeypatch.setattr(nv, "etude", faux)
    chemin = tmp_path / "active.db"
    open_read_write(chemin).close()
    texte, _ = service._niveaux(chemin, None, "")
    assert "Aucune relève" in texte
    for argument in ("", "essai"):
        texte, _ = service._niveaux(
            chemin, None, argument, environ={pool.ENV_RELEVE: str(chemin)},
            ouvrir_url=lambda url: open_read_only(url))
        assert texte == "📏 Niveaux"
    assert vus == [("relève", False), ("relève", True)]

    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "niveaux" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))

    async def niveaux(argument):
        await asyncio.sleep(0.2)
        return "rapport", [(b"a;b\n", "niveaux_cases.csv", "")]

    bot._niveaux = niveaux
    bot._niveaux_avancement = lambda: "paire EURUSD_otc ; 3/20 paires"
    bot.premier_avancement_sec = 0.05
    bot.intervalle_avancement_sec = 0.05
    monkeypatch.setattr("maxprofit.hosting.version.commit",
                        lambda: "abc1234")

    async def deroule():
        await bot._traiter(_message("/niveaux", chat=INTRUS)["message"])
        assert documents == []
        await bot._traiter(_message("/niveaux")["message"])
        await asyncio.sleep(0.01)
        await bot._traiter(_message("/niveaux")["message"])
        await bot._niveaux_en_cours
        await bot._niveaux_veille
        assert bot._en_fond == {}

    asyncio.run(deroule())
    assert documents == ["niveaux_cases.csv"]
    textes = [t for _, t in client.envoyes]
    assert any(t.startswith("📏 Niveaux des vendeurs et des acheteurs, et "
                            "continuation après repli : étude lancée "
                            "(version abc1234") for t in textes)
    assert any(t.startswith("📏 L'étude des niveaux est déjà en cours : "
                            "paire") for t in textes)
    assert any(t.startswith("⏳ 📏 Niveaux : paire") for t in textes)
