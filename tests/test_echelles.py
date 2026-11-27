"""
Les échelles de l'imprévisibilité OTC (recherche isolée) : le gel, la grille
et les phases, le quantile sans tri complet, le prix frais au tick près,
des décisions faites à la main (continuation, retournement, égalité, aucune
lecture entre t et l'entrée), tout le balayage contre un recalcul brut sur
des ticks irréguliers (statuts, comptes par sens, borne du grand mouvement,
corrélations), les bornes et leurs égalités, les cases et le détail par
sens, les verdicts, le hasard attendu, l'isolation du module, une passe de
bout en bout en lecture seule, l'essai sans taux, et la commande.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import math
import random
import shutil
import statistics
from array import array
from bisect import bisect_right
from collections import Counter
from pathlib import Path

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core.types import Tick
from maxprofit.live import echelles as ec
from maxprofit.live import engulfing as en
from maxprofit.live import motifs as mf
from maxprofit.store.db import open_read_only, open_read_write
from tests.test_engulfing import FIN_DONNEES, Espion, _base, _chemins

RACINE = Path(__file__).resolve().parents[1]
T0 = (ec.DEBUT + 10 * 86400) // 86400 * 86400
X, V = ec.EXPLORATION, ec.VALIDATION


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def _payout_de(changements):
    instants = [c for c, _ in changements]

    def payout_a(t):
        k = bisect_right(instants, t) - 1
        return changements[k][1] if k >= 0 else None
    return payout_a


def test_le_gel_tient():
    doc = json.loads((RACINE / "docs" / "preinscription_echelles.json")
                     .read_text(encoding="utf-8"))
    assert doc == ec.PARAMETRES
    assert em.empreinte(ec.PARAMETRES) == ec.EMPREINTE
    autre = json.loads(json.dumps(ec.PARAMETRES))
    autre["echelles_sec"] = autre["echelles_sec"][:-1]
    with pytest.raises(ValueError):
        em.exiger_le_gel(autre, ec.EMPREINTE)
    assert ec.ECHELLES == (5, 15, 30, 60, 120, 180, 300, 600, 900, 1800,
                           3600)
    assert len(ec.REGLES) * len(ec.ECHELLES) == 33
    assert [ec.PAS[k] for k in ec.ECHELLES] == [15, 15, 15, 30, 60, 90, 150,
                                               300, 450, 900, 1800]
    assert [ec.FRAICHEUR_MS[k] for k in ec.ECHELLES] == [
        2500, 7500] + [10_000] * 9
    assert ec.DEBUT == mf.DEBUT and ec.FIN_EXPLORATION == mf.FIN_EXPLORATION
    assert ec.DELAI_MS == 2000 and ec.DECILE == (9, 10)
    assert (ec.N_EXPLO_MIN, ec.N_VALID_MIN) == (300, 100)


def test_la_grille_et_les_phases():
    for k in ec.ECHELLES:
        assert ec.CYCLE_SEC % ec.PAS[k] == 0
        assert ec.PAS[k] % ec.GRILLE_SEC == 0
    for t in range(T0, T0 + 2 * ec.CYCLE_SEC, ec.GRILLE_SEC):
        phase = ec.PHASES[t % ec.CYCLE_SEC // ec.GRILLE_SEC]
        assert [k for _, k, _ in phase] == [
            k for k in ec.ECHELLES if t % max(15, k / 2) == 0], t
        assert all(ec.ECHELLES[ki] == k and f == ec.FRAICHEUR_MS[k]
                   for ki, k, f in phase)
    assert ec.MEMOIRE_TICKS_SEC >= 2 * 3600 + 60


def test_le_quantile_contre_le_tri():
    alea = random.Random(4)
    for n in list(range(1, 40)) + [999, 1000, 1001, 4321]:
        valeurs = [alea.choice((0.1, 0.2, 0.2, 0.3)) if n % 2
                   else alea.random() for _ in range(n)]
        for num, den in ((9, 10), (1, 2), (1, 10), (99, 100)):
            attendu = sorted(valeurs)[min(n - 1, n * num // den)]
            assert ec.quantile(valeurs, num, den) == attendu, (n, num)
            assert ec.quantile(array("d", valeurs), num, den) == attendu
    assert ec.quantile([5.0], 9, 10) == 5.0
    with pytest.raises(ValueError):
        ec.quantile([], 9, 10)
    assert ec.simple(0.1) == array("f", [0.1])[0] != 0.1


def test_le_prix_frais_au_tick_pres():
    m = T0 + 600
    ticks = [Tick("EURUSD_otc", (m - 60 + 50) * 1000, 1.1),
             Tick("EURUSD_otc", (m + 20) * 1000, 1.2),
             Tick("EURUSD_otc", (m + 120 + 5) * 1000, 1.3)]
    t = en.Ticks(_chemins(ticks))
    assert ec.prix(t, (m + 20) * 1000, 2500) == 1.2           # à t pile
    assert ec.prix(t, (m + 22) * 1000 + 499, 2500) == 1.2
    assert ec.prix(t, (m + 22) * 1000 + 500, 2500) is None    # 2,5 s : périmé
    assert ec.prix(t, (m + 30) * 1000 - 1, 10_000) == 1.2
    assert ec.prix(t, (m + 30) * 1000, 10_000) is None        # 10 s tout juste
    assert ec.prix(t, (m + 5) * 1000, 10_000) is None         # 15 s d'âge
    assert ec.prix(t, (m + 5) * 1000, 20_000) == 1.1          # minute d'avant
    # La minute m + 60 n'a pas de chemin : aucun prix, même frais.
    assert ec.prix(t, (m + 61) * 1000, 60_000) is None
    # La minute m + 120 a un chemin, mais pas la précédente.
    assert ec.prix(t, (m + 121) * 1000, 60_000) is None
    assert ec.prix(t, (m + 125) * 1000, 2500) == 1.3


# --------------------------------------------------------------------------- #
# Des décisions faites à la main
# --------------------------------------------------------------------------- #

def _seconde_par_seconde(f, debut, fin, pair="EURUSD_otc"):
    return [Tick(pair, s * 1000, round(f(s), 5)) for s in range(debut, fin)]


def _une_decision(f, t, piege=None):
    ticks = _seconde_par_seconde(f, T0, T0 + 3 * 3600 + 1)
    if piege is not None:
        ticks.append(Tick("EURUSD_otc", (t + 1) * 1000, piege))
        ticks.sort(key=lambda x: x.ts_ms)
    cumuls = ec.Cumuls(["EURUSD_otc"], T0, T0 + 86400)
    cumuls.bornes_fixees = True                    # la validation, sans borne
    n = ec.balayer("EURUSD_otc", en.Ticks(_chemins(ticks)),
                   lambda _: 92.0, t, t + 1, T0 + 86400, V, cumuls,
                   lambda: None)
    return cumuls, n


def _issues(cumuls, regle):
    return {k: next(iter(cumuls.comptes[(regle, k, V)].values()), None)
            for k in ec.ECHELLES}


def test_une_hausse_suivie_d_une_hausse():
    t = T0 + 7200

    def f(s):
        if s <= t - 60:
            return 1.1
        if s <= t + 100:
            return 1.1 + (s - (t - 60)) * 1e-5
        return 1.1016

    for piege in (None, 0.5):
        cumuls, n = _une_decision(f, t, piege)
        assert n == 11                       # toutes les échelles jouent à t
        dernier = _issues(cumuls, "dernier_mouvement")
        assert all(x == [1, 0, 0, 0, 0, 0] for x in dernier.values()), \
            dernier
        deux = _issues(cumuls, "deux_mouvements")
        assert [k for k, x in deux.items() if x] == [5, 15, 30]
        assert _issues(cumuls, "grand_mouvement") == dict.fromkeys(
            ec.ECHELLES)
        x = cumuls.correlation[(60, V)]
        assert x[0] == 1 and x[1] == pytest.approx(0.0006 / 1.1)
        assert x[2] == pytest.approx(0.0006 / 1.10062)


def test_une_hausse_puis_une_baisse_et_une_baisse_puis_le_plat():
    t = T0 + 7200

    def retour(s):
        if s <= t - 60:
            return 1.1
        if s <= t:
            return 1.1 + (s - (t - 60)) * 1e-5
        return 1.1006 - (s - t) * 1e-5

    cumuls, _ = _une_decision(retour, t)
    assert _issues(cumuls, "dernier_mouvement")[60] == [0, 1, 0, 0, 0, 0]

    def plat(s):
        if s <= t - 60:
            return 1.1
        if s <= t:
            return 1.1 - (s - (t - 60)) * 1e-5
        return 1.0994

    cumuls, _ = _une_decision(plat, t)
    dernier = _issues(cumuls, "dernier_mouvement")
    # Après la baisse, l'entrée (t + 2 s) et la sortie sont au même prix :
    # une égalité, perdue dans les deux sens.
    assert dernier[60] == [0, 0, 0, 0, 0, 1]
    assert dernier[5] == [0, 0, 0, 0, 0, 1]

    cumuls, n = _une_decision(lambda s: 1.1, t)
    assert n == 0
    assert cumuls.statuts[(V, 60, "mouvement passé nul")] == 1


# --------------------------------------------------------------------------- #
# Tout le balayage contre un recalcul brut
# --------------------------------------------------------------------------- #

def _ticks_varies(jours=2, graine=5):
    """Des ticks aux secondes 0, 1, 4, 9, 16, 25, 36, 49 ; une minute sur
    37 n'a que les secondes 7 et 37 (une entrée à + 2 s y trouve un tick de
    10 s tout juste : périmé) ; une minute sur 53 manque ; des pas de 0, 1
    ou 2 points (des mouvements nuls et des égalités)."""
    alea = random.Random(graine)
    prix, ticks = 1.1, []
    for k in range(jours * 1440):
        m = T0 + 60 * k
        if k % 53 == 20:
            continue
        secondes = (7, 37) if k % 37 == 10 else (0, 1, 4, 9, 16, 25, 36, 49)
        for s in secondes:
            prix = round(prix + alea.choice((-2, -1, 0, 0, 1, 2)) * 1e-5, 5)
            ticks.append(Tick("EURUSD_otc", (m + s) * 1000, prix))
    return ticks


#: Des changements de payout une seconde après un instant de la grille :
#: seule l'entrée (t + 2 s) les voit. 84 tout juste est jouable.
PAYOUTS = [(T0 - 100, 92.0), (T0 + 5 * 3600 + 1, 80.0),
           (T0 + 7 * 3600 + 1, 90.0), (T0 + 30 * 3600 + 1, 84.0),
           (T0 + 31 * 3600 + 1, 83.0), (T0 + 32 * 3600 + 1, 92.0)]


def _brut(ticks, debut, separation, fin):
    par_minute: dict[int, list[tuple[int, float]]] = {}
    for x in ticks:
        par_minute.setdefault(x.ts_ms // 60000 * 60, []).append(
            (x.ts_ms, x.price))

    def p(instant_ms, f):
        m = instant_ms // 60000 * 60
        for minute in (m, m - 60):
            if minute not in par_minute:
                return None
            avant = [x for x in par_minute[minute] if x[0] <= instant_ms]
            if avant:
                return avant[-1][1] if instant_ms - avant[-1][0] < f else None
        return None

    def payout(instant):
        v = None
        for c, pct in PAYOUTS:
            if c <= instant:
                v = pct
        return v

    def f32(x):
        return array("f", [x])[0]

    comptes, statuts = Counter(), Counter()
    attente: dict[int, list] = {k: [] for k in ec.ECHELLES}
    valides: list[tuple] = []
    sommes = {(k, per): [0, 0.0, 0.0, 0.0, 0.0, 0.0]
              for k in ec.ECHELLES for per in (X, V)}
    for t in range(debut, fin, 15):
        per = X if t < separation else V
        bloc = t // 86400 - debut // 86400
        for k in ec.ECHELLES:
            if t % max(15, k / 2):
                continue
            f = min(10.0, k / 2) * 1000
            pct = payout(t + 2)
            if pct is None or pct < 84:
                statuts[(per, k, "non jouable")] += 1
                continue
            p_t, p_k = p(t * 1000, f), p((t - k) * 1000, f)
            if p_t is None or p_k is None:
                statuts[(per, k, "pas de prix frais à t ou t − k")] += 1
                continue
            d = p_t - p_k
            if d == 0:
                statuts[(per, k, "mouvement passé nul")] += 1
                continue
            if t + 2 + k > fin:
                statuts[(per, k, "fin des données")] += 1
                continue
            if any(m not in par_minute for m in range(
                    (t - k) // 60 * 60, (t + 2 + k) // 60 * 60 + 1, 60)):
                statuts[(per, k, "minutes manquantes")] += 1
                continue
            p_e = p((t + 2) * 1000, f)
            if p_e is None:
                statuts[(per, k, "pas de prix frais à l'entrée")] += 1
                continue
            p_x = p((t + 2 + k) * 1000, f)
            if p_x is None:
                statuts[(per, k, "pas de prix frais à la sortie")] += 1
                continue
            statuts[(per, k, "jugée")] += 1
            meme = (p_x > p_e) if d > 0 else (p_x < p_e)
            contre = (p_x < p_e) if d > 0 else (p_x > p_e)
            code = (0 if meme else 1 if contre else 2) + (0 if d > 0 else 3)
            comptes[("dernier_mouvement", k, per, bloc, code)] += 1
            p_2k = p((t - 2 * k) * 1000, f)
            if p_2k is not None and p_k != p_2k and (p_k > p_2k) == (d > 0):
                comptes[("deux_mouvements", k, per, bloc, code)] += 1
            a = f32(abs(d) / p_k)
            (attente[k] if per == X else valides).append((k, a, bloc, code))
            s = sommes[(k, per)]
            xr, yr = d / p_k, (p_x - p_e) / p_e
            for i, v in enumerate((1, xr, yr, xr * xr, yr * yr, xr * yr)):
                s[i] += v
    bornes = {}
    for k, liste in attente.items():
        if liste:
            triees = sorted(a for _, a, _, _ in liste)
            bornes[k] = triees[min(len(triees) - 1, len(triees) * 9 // 10)]
            for _, a, bloc, code in liste:
                if a >= bornes[k]:
                    comptes[("grand_mouvement", k, X, bloc, code)] += 1
    for k, a, bloc, code in valides:
        if k in bornes and a >= bornes[k]:
            comptes[("grand_mouvement", k, V, bloc, code)] += 1
    return comptes, statuts, bornes, sommes


def _comptes_de(cumuls):
    sortie = Counter()
    for (regle, k, per), par_bloc in cumuls.comptes.items():
        for bloc, x in par_bloc.items():
            for code, n in enumerate(x):
                if n:
                    sortie[(regle, k, per, bloc, code)] = n
    return sortie


def test_tout_le_balayage_contre_un_recalcul_brut():
    ticks = _ticks_varies()
    debut, separation, fin = T0, T0 + 86400, T0 + 2 * 86400
    cumuls = ec.Cumuls(["EURUSD_otc"], debut, fin)
    payout_a = _payout_de(PAYOUTS)
    n_x = ec.balayer("EURUSD_otc", en.Ticks(_chemins(ticks)), payout_a,
                     debut, separation, fin, X, cumuls, lambda: None)
    with pytest.raises(ValueError):
        ec.balayer("EURUSD_otc", en.Ticks(_chemins(ticks)), payout_a,
                   separation, fin, fin, V, cumuls, lambda: None)
    attente = cumuls.octets_en_attente()
    assert attente == n_x * (4 + 2)
    cumuls.fixer_les_bornes()
    assert cumuls.octets_en_attente() == 0
    with pytest.raises(ValueError):
        ec.balayer("EURUSD_otc", en.Ticks(_chemins(ticks)), payout_a,
                   debut, separation, fin, X, cumuls, lambda: None)
    n_v = ec.balayer("EURUSD_otc", en.Ticks(_chemins(ticks)), payout_a,
                     separation, fin, fin, V, cumuls, lambda: None)
    comptes, statuts, bornes, sommes = _brut(ticks, debut, separation, fin)
    assert dict(cumuls.statuts) == dict(statuts)
    assert _comptes_de(cumuls) == comptes
    assert {k: b for k, b in zip(ec.ECHELLES, cumuls.bornes)
            if b is not None} == bornes
    for cle, s in sommes.items():
        assert cumuls.correlation[cle][0] == s[0]
        assert cumuls.correlation[cle][1:] == pytest.approx(s[1:], rel=1e-9,
                                                           abs=1e-15)
    jugees = sum(n for (_, _, s), n in statuts.items() if s == "jugée")
    assert n_x + n_v == jugees == cumuls.n_decisions
    # Chaque branche a servi.
    vus = {s for (_, _, s) in statuts}
    assert vus == set(ec.STATUTS), set(ec.STATUTS) - vus
    assert any(code in (2, 5) for (*_, code) in comptes)
    assert {r for (r, *_) in comptes} == set(ec.REGLES)


def test_le_bloc_est_la_paire_et_le_jour():
    c = ec.Cumuls(["A", "B", "C"], T0, T0 + 5 * 86400)
    assert c.n_jours == 5
    assert c.bloc_de("A", T0) == 0 and c.bloc_de("A", T0 + 86399) == 0
    assert c.bloc_de("B", T0 + 86400) == 6 and c.bloc_de("C", T0 + 4 * 86400
                                                         + 5) == 14
    assert c.paire_du_bloc(14) == "C" and c.paire_du_bloc(5) == "B"
    assert c.type_attente == "h"
    assert ec.Cumuls([str(i) for i in range(60)], T0,
                     T0 + 200 * 86400).type_attente == "i"


def test_les_bornes_et_leurs_egalites():
    c = ec.Cumuls(["A"], T0, T0 + 2 * 86400)
    ki = ec.ECHELLES.index(60)
    ampleurs = [1e-4] * 85 + [3e-4] * 10 + [5e-4] * 5      # rang 90 : 3e-4
    alea = random.Random(1)
    alea.shuffle(ampleurs)
    a, codes = c.attente[ki]
    for i, x in enumerate(ampleurs):
        a.append(x)
        codes.append(6 * (i % 2) + i % 6)
    c.fixer_les_bornes()
    assert c.bornes[ki] == ec.simple(3e-4)
    assert c.bornes[ec.ECHELLES.index(5)] is None
    grand = c.comptes[("grand_mouvement", 60, X)]
    assert sum(sum(x) for x in grand.values()) == 15     # égalités comprises
    attendu = Counter()
    for i, x in enumerate(ampleurs):
        if x >= 3e-4:
            attendu[(i % 2, i % 6)] += 1
    assert {(b, code): n for b, x in grand.items()
            for code, n in enumerate(x) if n} == dict(attendu)


# --------------------------------------------------------------------------- #
# Les cases, les verdicts, le hasard
# --------------------------------------------------------------------------- #

class _FauxTirages:
    def __init__(self):
        self.vus = []

    def ratio(self, g, n, taux):
        self.vus.append((dict(g), dict(n), taux))
        return 0.5, 0.6, 0.001


def _cumuls_faits_main():
    c = ec.Cumuls(["A", "B"], T0, T0 + 2 * 86400)
    # 5 s, dernier mouvement : le retournement gagne en exploration.
    c.comptes[("dernier_mouvement", 5, X)].update({
        0: [100, 120, 10, 90, 110, 5], 2: [50, 70, 2, 40, 60, 3]})
    c.comptes[("dernier_mouvement", 5, V)].update({
        1: [30, 40, 1, 20, 35, 2], 3: [10, 15, 0, 12, 14, 1]})
    # 60 s, deux mouvements : égalité des deux sens : la continuation.
    c.comptes[("deux_mouvements", 60, X)].update({0: [10, 10, 0, 5, 5, 1]})
    return c


def test_les_cases_les_sens_et_le_detail():
    c = _cumuls_faits_main()
    tirages = _FauxTirages()
    lignes = ec.cases(c, tirages)
    assert len(lignes) == 66
    par = {(l["regle"], l["echelle_sec"], l["periode"]): l for l in lignes}
    x = par[("dernier_mouvement", 5, X)]
    assert (x["continuations"], x["retournements"], x["egalites"]) == (
        280, 360, 20)
    assert x["n"] == 660 and x["sens_choisi"] == "retournement"
    assert x["taux"] == pytest.approx(360 / 660)
    assert x["taux_continuation"] == pytest.approx(280 / 660)
    v = par[("dernier_mouvement", 5, V)]
    assert v["n"] == 180 and v["taux"] == pytest.approx(104 / 180)
    assert v["p"] == 0.001 and (v["ic_bas"], v["ic_haut"]) == (0.5, 0.6)
    assert tirages.vus == [({1: 75.0, 3: 29.0}, {1: 128.0, 3: 52.0},
                            pytest.approx(104 / 180))]
    assert par[("deux_mouvements", 60, X)]["sens_choisi"] == "continuation"
    assert par[("grand_mouvement", 5, X)]["n"] == 0
    assert par[("grand_mouvement", 5, X)]["taux"] is None
    ec.verdicts(lignes)
    assert x["verdict"] == "retenue en exploration"
    assert v["verdict"] == "validée (BH 10 %)"
    assert par[("deux_mouvements", 60, X)]["verdict"] == \
        "trop peu en exploration"
    details = ec._details(c, lignes)
    assert {(d["periode"], d["paire"], d["mouvement_passe"]): (d["gains"],
                                                              d["n"])
            for d in details} == {
        (X, "A", "hausse"): (120, 230), (X, "A", "baisse"): (110, 205),
        (X, "B", "hausse"): (70, 122), (X, "B", "baisse"): (60, 103),
        (V, "A", "hausse"): (40, 71), (V, "A", "baisse"): (35, 57),
        (V, "B", "hausse"): (15, 25), (V, "B", "baisse"): (14, 27)}


def test_analyser_tire_les_blocs_de_la_validation():
    lignes = ec.analyser(_cumuls_faits_main())
    v = next(l for l in lignes if l["regle"] == "dernier_mouvement"
             and l["echelle_sec"] == 5 and l["periode"] == V)
    assert v["p"] is not None and v["ic_bas"] <= v["taux"] <= v["ic_haut"]
    assert v["verdict"] in ("validée (BH 10 %)", "non reproduite")


def test_les_sources_ne_lisent_rien_hors_de_l_etude():
    lus = []

    class Resultat:
        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class Base:
        def execute(self, sql, args):
            lus.append(("tick_paths" if "tick_paths" in sql else "payouts",
                        args))
            return Resultat()

    base = Base()
    debut, fin = T0, T0 + 3 * 86400
    payout_a, ticks = ec._sources("EURUSD_otc", T0 + 86400, T0 + 2 * 86400,
                                  debut, fin, lambda *_: base, base)
    assert lus == [("payouts", ("EURUSD_otc", T0 + 86400)),
                   ("payouts", ("EURUSD_otc", T0 + 86400, T0 + 2 * 86400))]
    assert payout_a(T0 + 86400 + 5) is None
    lus.clear()
    assert ticks.prix_a((debut - 30) * 1000) is None       # avant le début
    assert ticks.prix_a((fin + 30) * 1000) is None         # après la fin
    assert lus == []
    assert ticks.prix_a((debut + 30) * 1000) is None
    assert lus == [("tick_paths", ("EURUSD_otc", debut, debut + 86400))]


def test_les_verdicts_et_benjamini_hochberg():
    def l(cle, per, n, taux, p=None):
        return {"regle": "dernier_mouvement", "echelle_sec": cle,
                "periode": per, "n": n, "taux": taux, "p": p, "verdict": ""}
    lignes = [l(5, X, 299, 0.6), l(5, V, 500, 0.6, 0.01),
              l(15, X, 400, 0.52), l(15, V, 500, 0.6, 0.01),
              l(30, X, 400, 0.55), l(30, V, 99, 0.6, None),
              l(60, X, 400, 0.55), l(60, V, 500, 0.58, 0.001),
              l(120, X, 400, 0.55), l(120, V, 500, 0.5, 0.6),
              l(180, X, 400, 0.55), l(180, V, 500, 0.52, 0.0001)]
    ec.verdicts(lignes)
    v = {(x["echelle_sec"], x["periode"]): x["verdict"] for x in lignes}
    assert v[(5, X)] == "trop peu en exploration" and v[(5, V)] == ""
    assert v[(15, X)] == "non retenue en exploration"
    assert v[(30, X)] == "retenue, trop peu en validation"
    assert v[(30, V)] == "trop peu en validation"
    assert v[(60, V)] == "validée (BH 10 %)"
    assert v[(120, V)] == "non reproduite"
    assert v[(180, V)] == "non reproduite", "52 % ne dépasse pas 52,08 %"


def test_le_hasard_attendu_tient_compte_des_egalites():
    def l(n, egalites, per=X):
        return {"periode": per, "n": n, "egalites": egalites}
    z = (ec.POINT_MORT - 0.5) * 2 * math.sqrt(400)
    attendu = 2 * (1 - statistics.NormalDist().cdf(z))
    assert ec.attendues_au_hasard([l(400, 0)]) == pytest.approx(attendu)
    q = 0.45
    z = (ec.POINT_MORT - q) / math.sqrt(q * (1 - q) / 400)
    assert ec.attendues_au_hasard([l(400, 40)]) == pytest.approx(
        2 * (1 - statistics.NormalDist().cdf(z)))
    assert ec.attendues_au_hasard([l(299, 0), l(400, 0, V),
                                   l(400, 400)]) == 0.0


def test_la_correlation():
    alea = random.Random(8)
    xs = [alea.gauss(0, 1e-4) for _ in range(500)]
    ys = [0.3 * x + alea.gauss(0, 1e-4) for x in xs]

    def sommes(a, b):
        return [len(a), sum(a), sum(b), sum(x * x for x in a),
                sum(y * y for y in b), sum(x * y for x, y in zip(a, b))]
    assert ec.correlation(sommes(xs, ys)) == pytest.approx(
        statistics.correlation(xs, ys), rel=1e-9)
    assert ec.correlation(sommes(xs, [-x for x in xs])) == pytest.approx(-1)
    assert ec.correlation(sommes(xs[:2], ys[:2])) is None
    assert ec.correlation(sommes(xs, [1.0] * 500)) is None


def test_le_module_est_isole():
    source = (RACINE / "maxprofit" / "live" / "echelles.py").read_text(
        encoding="utf-8")
    importes = set()
    for noeud in ast.walk(ast.parse(source)):
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


# --------------------------------------------------------------------------- #
# De bout en bout, en lecture seule
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def passe(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("echelles")
    active = dossier / "active.db"
    _base(active)
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    empreintes = [hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (active, releve)]
    a, r = open_read_only(active), open_read_only(releve)
    espion = Espion(r)
    try:
        rapport, fichiers = ec.etude(espion, a, maintenant=FIN_DONNEES + 3600,
                                     respirer=lambda: None, version="abc1234")
    finally:
        a.close()
        r.close()
    return active, releve, empreintes, espion, rapport, fichiers


def _lignes(fichiers, nom):
    contenu = next(c for c, n, _ in fichiers if n == nom)
    lignes = contenu.decode("utf-8").splitlines()
    tete = lignes[0].split(";")
    return [dict(zip(tete, l.split(";"))) for l in lignes[1:]]


def test_passe_en_lecture_seule_dans_ses_bornes(passe):
    active, releve, empreintes, espion, rapport, fichiers = passe
    assert [hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (active, releve)] == empreintes
    assert "requêtes refusées : 0" in rapport and "Gel vérifié" in rapport
    assert "aucun ordre" in rapport and "Version : abc1234" in rapport
    assert espion.bornes and max(espion.bornes) <= FIN_DONNEES
    assert min(espion.bornes) >= ec.DEBUT
    assert [n for _, n, _ in fichiers] == [
        "echelles_cases.csv", "echelles_details.csv",
        "echelles_correlations.csv", "echelles_statuts.csv",
        "echelles_donnees.csv"]


def test_passe_complete_cases_et_rapport(passe):
    *_, rapport, fichiers = passe
    cases = _lignes(fichiers, "echelles_cases.csv")
    assert len(cases) == 66
    jugees = Counter()
    for c in cases:
        n = int(c["n"])
        assert n == (int(c["continuations"]) + int(c["retournements"])
                     + int(c["egalites"]))
        if n:
            g = int(c["retournements" if c["sens_choisi"] == "retournement"
                      else "continuations"])
            assert abs(float(c["taux"]) - g / n) < 1e-6
        if c["regle"] == "dernier_mouvement":
            jugees[c["periode"]] += n
    assert jugees[X] > 10_000 and jugees[V] > 10_000
    statuts = _lignes(fichiers, "echelles_statuts.csv")
    assert sum(int(s["nombre"]) for s in statuts
               if s["statut"] == "jugée") == sum(jugees.values())
    correlations = _lignes(fichiers, "echelles_correlations.csv")
    assert len(correlations) == 22
    assert all(c["borne_grand_mouvement"] for c in correlations)
    for k in ec.ECHELLES:
        assert f"• {ec.libelle_echelle(k)} : " in rapport
    assert "Bilan" in rapport and "sur 33" in rapport
    assert "22 h" not in rapport and "par heure" not in rapport


def test_l_essai_ne_montre_aucun_taux(passe):
    active, releve, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    try:
        texte, fichiers = ec.etude(r, a, maintenant=FIN_DONNEES + 3600,
                                   respirer=lambda: None, essai=True,
                                   version="abc1234")
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "%" not in texte
    assert texte.startswith("⏱ <b>Essai technique /echelles</b>")
    assert "cases, rapport et fichiers : passés" in texte
    assert "en attente prévues" in texte
    assert ec.avancement() is None


def test_avant_la_fin_de_l_exploration_rien_a_valider(passe):
    active, releve, *_ = passe
    a, r = open_read_only(active), open_read_only(releve)
    try:
        texte, fichiers = ec.etude(r, a, maintenant=ec.FIN_EXPLORATION - 60,
                                   respirer=lambda: None)
    finally:
        a.close()
        r.close()
    assert fichiers == [] and "rien à valider" in texte


def test_l_avancement_suit_la_passe():
    assert ec.avancement() is None
    import time as horloge
    maintenant = horloge.monotonic()
    ec._ETAT.update(debut=maintenant - 600,
                    etape="exploration, paire EURUSD_otc", faites=5,
                    total=20, signaux=90000, debut_paires=maintenant - 300,
                    rss_max=250.0)
    try:
        point = ec.avancement()
        assert "5/20 passes de paires" in point
        assert "décisions jugées" in point
        assert "fin des paires dans ~15 min" in point
    finally:
        ec._ETAT.clear()


def test_le_service_et_la_commande(monkeypatch, tmp_path):
    from maxprofit.hosting import service
    from maxprofit.store import pool
    vus = []

    def faux(lue, active, course=None, *, nom_lecture, essai=False,
             version=None):
        vus.append((nom_lecture, essai))
        return "⏱ Échelles", []

    monkeypatch.setattr(ec, "etude", faux)
    chemin = tmp_path / "active.db"
    open_read_write(chemin).close()
    texte, _ = service._echelles(chemin, None, "")
    assert "Aucune relève" in texte
    for argument in ("", "essai"):
        texte, _ = service._echelles(
            chemin, None, argument, environ={pool.ENV_RELEVE: str(chemin)},
            ouvrir_url=lambda url: open_read_only(url))
        assert texte == "⏱ Échelles"
    assert vus == [("relève", False), ("relève", True)]

    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "echelles" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))

    async def echelles(argument):
        await asyncio.sleep(0.2)
        return "rapport", [(b"a;b\n", "echelles_cases.csv", "")]

    bot._echelles = echelles
    bot._echelles_avancement = lambda: "exploration, paire EURUSD_otc"
    bot.premier_avancement_sec = 0.05
    bot.intervalle_avancement_sec = 0.05
    monkeypatch.setattr("maxprofit.hosting.version.commit",
                        lambda: "abc1234")

    async def deroule():
        await bot._traiter(_message("/echelles", chat=INTRUS)["message"])
        assert documents == []
        await bot._traiter(_message("/echelles")["message"])
        await asyncio.sleep(0.01)
        await bot._traiter(_message("/echelles")["message"])
        await bot._echelles_en_cours
        await bot._echelles_veille
        assert bot._en_fond == {}

    asyncio.run(deroule())
    assert documents == ["echelles_cases.csv"]
    textes = [t for _, t in client.envoyes]
    assert any(t.startswith("⏱ Échelles de l'imprévisibilité lancées "
                            "(version abc1234") for t in textes)
    assert any(t.startswith("⏱ L'étude des échelles est déjà en cours : "
                            "exploration") for t in textes)
    assert any(t.startswith("⏳ ⏱ Échelles : exploration") for t in textes)
