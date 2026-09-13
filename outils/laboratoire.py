#!/usr/bin/env python
r"""
Banc d'essai d'hypothèses — chercher un avantage, et savoir quand on n'en a pas.

    .venv313\Scripts\python.exe outils\laboratoire.py
    .venv313\Scripts\python.exe outils\laboratoire.py --payout 88 --min-signaux 200

--- Ce que cet outil est, et ce qu'il n'est pas -----------------------------

Il EXPLORE. Il teste en bloc les familles d'hypothèses classiques — tendance,
retour à la moyenne, cassure de range, structure en `higher high / higher low`,
supports et résistances, extrêmes d'oscillateurs, géométrie des bougies, heure
de la journée — et rapporte ce que chacune donne sur les données collectées.

Il ne VALIDE rien. Une hypothèse qui ressort ici est un candidat, pas une
stratégie : il faudra l'écrire comme une `Strategy`, la passer au moteur de
backtest (§2), à ses contrefactuels (§3.1) et au walk-forward (§2.5), sur des
données qu'elle n'a pas vues. Confondre les deux est précisément la façon
normale de se ruiner.

--- Pourquoi la correction des comparaisons multiples est au centre ---------

C'est le coeur du problème, et la raison d'être de cet outil plutôt que d'un
essai à la main. Tester quarante hypothèses au seuil de 5 %, c'est s'attendre à
DEUX « découvertes » même si aucune n'est vraie. Le vrai risque de ce projet
n'est pas de rater un avantage : c'est d'en croire un qui n'existe pas.

Chaque hypothèse reçoit donc :

    p            probabilité d'obtenir ce résultat par pur hasard
    p corrigé    la même, corrigée du nombre d'hypothèses testées
                 (Benjamini-Hochberg, qui contrôle le taux de fausses
                 découvertes plutôt que d'écraser tout comme Bonferroni)

Une hypothèse dont le p corrigé dépasse 0,05 n'a rien montré. Le dire est le
service le plus utile que cet outil puisse rendre.

--- Le seuil à battre -------------------------------------------------------

Un binaire à 88 % de payout rend 0,88 quand on gagne et prend 1,00 quand on
perd. Le taux de réussite d'équilibre est donc 1/1,88 = 53,2 %, et non 50 %. Une
hypothèse à 51 % n'est pas « légèrement gagnante » : elle perd de l'argent.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _interpreteur import exiger  # noqa: E402

exiger("psycopg", "outils\\laboratoire.py")

from maxprofit.core.config import charger_env_local  # noqa: E402
from maxprofit.core.types import Candle  # noqa: E402
from maxprofit.indicators.oscillators import (  # noqa: E402
    atr_normalise_pct,
    bollinger_percent_b,
    sma,
    stochastique,
)

#: Longueur minimale d'une tranche continue pour être exploitée. En dessous, on
#: n'observe qu'un fragment dont on ne sait même pas s'il est représentatif.
SEGMENT_MIN = 60

#: Contexte minimal avant d'évaluer une hypothèse : les indicateurs les plus
#: longs regardent 20 bougies en arrière.
CONTEXTE = 25


# --------------------------------------------------------------------------- #
# Les hypothèses
# --------------------------------------------------------------------------- #
#
# Chacune reçoit l'historique JUSQU'À `i` inclus et rend :
#     +1  on parie que la bougie suivante montera
#     -1  on parie qu'elle descendra
#      0  pas de signal
#
# Aucune ne voit `i+1`. C'est la seule règle, et elle est structurelle : la
# fonction ne reçoit tout simplement pas la suite.

def _sens(c: Candle) -> int:
    if c.close > c.open:
        return 1
    if c.close < c.open:
        return -1
    return 0


def _serie_identiques(h: list[Candle], n: int) -> int:
    """Sens commun des `n` dernières bougies, 0 si elles ne s'accordent pas."""
    if len(h) < n:
        return 0
    sens = {_sens(c) for c in h[-n:]}
    if len(sens) != 1:
        return 0
    (s,) = sens
    return s


def _suite(n: int, direction: int):
    """`direction` = +1 : on suit la série. -1 : on parie contre."""
    def hypothese(h: list[Candle]) -> int:
        return _serie_identiques(h, n) * direction
    return hypothese


def _cassure(n: int, direction: int):
    """Le prix sort du plus haut / plus bas des `n` bougies précédentes."""
    def hypothese(h: list[Candle]) -> int:
        if len(h) < n + 1:
            return 0
        fenetre = h[-n - 1:-1]
        haut = max(c.high for c in fenetre)
        bas = min(c.low for c in fenetre)
        if h[-1].close > haut:
            return direction
        if h[-1].close < bas:
            return -direction
        return 0
    return hypothese


def _structure_hh_hl(n: int, direction: int):
    """`higher high` ET `higher low` sur `n` bougies : la définition usuelle
    d'une tendance haussière. L'inverse pour la baissière."""
    def hypothese(h: list[Candle]) -> int:
        if len(h) < 2 * n:
            return 0
        recent, avant = h[-n:], h[-2 * n:-n]
        hh = max(c.high for c in recent) > max(c.high for c in avant)
        hl = min(c.low for c in recent) > min(c.low for c in avant)
        lh = max(c.high for c in recent) < max(c.high for c in avant)
        ll = min(c.low for c in recent) < min(c.low for c in avant)
        if hh and hl:
            return direction
        if lh and ll:
            return -direction
        return 0
    return hypothese


def _bollinger(seuil: float, direction: int):
    """`%B` hors des bandes : l'extrême classique du retour à la moyenne."""
    def hypothese(h: list[Candle]) -> int:
        b = bollinger_percent_b(h, periode=20)
        if b is None:
            return 0
        if b > seuil:
            return -direction          # haut de bande -> on parie la baisse
        if b < 1 - seuil:
            return direction
        return 0
    return hypothese


def _stochastique(bas: float, haut: float, direction: int):
    def hypothese(h: list[Candle]) -> int:
        k = stochastique(h, periode_k=14)
        if k is None:
            return 0
        k = k[0] if isinstance(k, tuple) else k
        if k > haut:
            return -direction
        if k < bas:
            return direction
        return 0
    return hypothese


def _distance_moyenne(periode: int, seuil_pct: float, direction: int):
    """Écart à la moyenne mobile : l'élastique se détend-il ou se tend-il ?"""
    def hypothese(h: list[Candle]) -> int:
        m = sma(h, periode)
        if m is None or m == 0:
            return 0
        ecart = 100 * (h[-1].close - m) / m
        if ecart > seuil_pct:
            return -direction
        if ecart < -seuil_pct:
            return direction
        return 0
    return hypothese


def _meche(part: float, direction: int):
    """Une longue mèche est censée signaler un rejet du niveau atteint."""
    def hypothese(h: list[Candle]) -> int:
        c = h[-1]
        etendue = c.high - c.low
        if etendue <= 0:
            return 0
        haute = (c.high - max(c.open, c.close)) / etendue
        basse = (min(c.open, c.close) - c.low) / etendue
        if haute > part and haute > 2 * basse:
            return -direction
        if basse > part and basse > 2 * haute:
            return direction
        return 0
    return hypothese


def _niveau_teste(n: int, tolerance_pct: float, direction: int):
    """Support / résistance : le prix revient sur un extrême récent.

    `direction` = +1 : on parie que le niveau TIENT (rebond).
    `direction` = -1 : on parie qu'il cède (cassure).
    """
    def hypothese(h: list[Candle]) -> int:
        if len(h) < n + 1:
            return 0
        fenetre = h[-n - 1:-1]
        haut = max(c.high for c in fenetre)
        bas = min(c.low for c in fenetre)
        c = h[-1]
        if haut > 0 and abs(c.high - haut) / haut * 100 < tolerance_pct:
            return -direction          # sous résistance -> baisse si elle tient
        if bas > 0 and abs(c.low - bas) / bas * 100 < tolerance_pct:
            return direction
        return 0
    return hypothese


def _volatilite(seuil_pct: float, haute: bool, sous_jacente):
    """Filtre : la même hypothèse, mais seulement en régime calme ou agité.

    Une hypothèse peut être vraie dans un régime et fausse dans l'autre ; les
    mélanger noierait le signal dans le bruit de l'autre moitié.
    """
    def hypothese(h: list[Candle]) -> int:
        v = atr_normalise_pct(h, periode=14)
        if v is None:
            return 0
        if (v > seuil_pct) != haute:
            return 0
        return sous_jacente(h)
    return hypothese


def catalogue() -> dict:
    """Toutes les hypothèses testées. Nommées pour être relues, pas devinées."""
    h = {}
    for n in (2, 3, 4, 5):
        h[f"suite {n} bougies : ça continue"] = _suite(n, +1)
        h[f"suite {n} bougies : ça se retourne"] = _suite(n, -1)
    for n in (5, 10, 20):
        h[f"cassure du haut/bas {n} : ça continue"] = _cassure(n, +1)
        h[f"cassure du haut/bas {n} : faux signal"] = _cassure(n, -1)
        h[f"structure HH/HL sur {n} : ça continue"] = _structure_hh_hl(n, +1)
        h[f"structure HH/HL sur {n} : ça se retourne"] = _structure_hh_hl(n, -1)
        h[f"niveau {n} retouché : il tient"] = _niveau_teste(n, 0.02, +1)
        h[f"niveau {n} retouché : il cède"] = _niveau_teste(n, 0.02, -1)
    for s in (0.95, 1.0):
        h[f"Bollinger %B > {s} : retour"] = _bollinger(s, +1)
        h[f"Bollinger %B > {s} : poursuite"] = _bollinger(s, -1)
    h["stochastique extrême : retour"] = _stochastique(20, 80, +1)
    h["stochastique extrême : poursuite"] = _stochastique(20, 80, -1)
    for p, seuil in ((10, 0.05), (20, 0.05), (20, 0.1)):
        h[f"écart SMA{p} > {seuil}% : retour"] = _distance_moyenne(p, seuil, +1)
        h[f"écart SMA{p} > {seuil}% : poursuite"] = _distance_moyenne(p, seuil, -1)
    for part in (0.5, 0.65):
        h[f"mèche > {part:.0%} : rejet"] = _meche(part, +1)
        h[f"mèche > {part:.0%} : continuation"] = _meche(part, -1)
    # Les deux meilleures familles, filtrées par régime de volatilité.
    h["suite 3 : ça se retourne (calme)"] = _volatilite(0.03, False, _suite(3, -1))
    h["suite 3 : ça se retourne (agité)"] = _volatilite(0.03, True, _suite(3, -1))
    h["cassure 10 : continue (calme)"] = _volatilite(0.03, False, _cassure(10, +1))
    h["cassure 10 : continue (agité)"] = _volatilite(0.03, True, _cassure(10, +1))
    return h


# --------------------------------------------------------------------------- #
# Statistiques
# --------------------------------------------------------------------------- #

def _phi(z: float) -> float:
    """Loi normale centrée réduite, sans dépendance externe."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def p_unilateral(reussites: int, total: int, seuil: float) -> float:
    """Probabilité d'observer AU MOINS ce taux si la vérité est `seuil`.

    Unilatéral à dessein : une hypothèse qui perd n'intéresse personne, et la
    question posée est « bat-elle le seuil de rentabilité ? », pas « diffère-
    t-elle de lui ? ».
    """
    if total == 0:
        return 1.0
    ecart_type = math.sqrt(seuil * (1 - seuil) / total)
    if ecart_type == 0:
        return 1.0
    z = (reussites / total - seuil) / ecart_type
    return 1 - _phi(z)


def benjamini_hochberg(ps: list[float]) -> list[float]:
    """Corrige les p pour le nombre d'hypothèses testées.

    Contrôle le taux de FAUSSES DÉCOUVERTES. Préféré à Bonferroni, qui divise
    le seuil par le nombre de tests et enterre aussi les vraies trouvailles —
    inutilement sévère quand on explore.
    """
    m = len(ps)
    indexes = sorted(range(m), key=lambda i: ps[i])
    corriges = [1.0] * m
    precedent = 1.0
    for rang, i in enumerate(reversed(indexes), start=1):
        valeur = min(precedent, ps[i] * m / (m - rang + 1))
        corriges[i] = valeur
        precedent = valeur
    return corriges


# --------------------------------------------------------------------------- #
# Données
# --------------------------------------------------------------------------- #

def segments(lignes, tf_sec: int = 60) -> list[list[Candle]]:
    """Tranches CONTIGUES de bougies complètes.

    Un trou interrompt la tranche : une hypothèse qui regarde vingt bougies en
    arrière ne doit jamais enjamber une interruption de collecte, sinon elle
    compare des prix séparés par plusieurs heures en croyant lire une minute.
    """
    sortie, courant, precedent = [], [], None
    for pair, ts, o, hi, lo, c, tc in lignes:
        if precedent is not None and ts - precedent != tf_sec:
            if len(courant) >= SEGMENT_MIN:
                sortie.append(courant)
            courant = []
        courant.append(Candle(pair=pair, tf_sec=tf_sec, ts_sec=ts, open=o,
                              high=hi, low=lo, close=c, tick_count=tc,
                              complete=True))
        precedent = ts
    if len(courant) >= SEGMENT_MIN:
        sortie.append(courant)
    return sortie


def charger() -> dict[str, list[list[Candle]]]:
    from maxprofit.store.db import open_read_only

    conn = open_read_only(Path("lecture"))
    try:
        paires = [r[0] for r in conn.execute(
            "SELECT DISTINCT pair FROM candles").fetchall()]
        par_paire = {}
        for p in paires:
            lignes = conn.execute(
                "SELECT pair, ts_sec, open, high, low, close, tick_count "
                "FROM candles WHERE pair = ? AND complete = 1 "
                "ORDER BY ts_sec", (p,)).fetchall()
            segs = segments(lignes)
            if segs:
                par_paire[p] = segs
        return par_paire
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Évaluation
# --------------------------------------------------------------------------- #

def evaluer(par_paire, hypotheses, par_paire_aussi: bool = False):
    """Compte, pour chaque hypothèse, signaux et réussites.

    La règle du binaire M1 : on décide à la clôture de la bougie `i`, on gagne
    si la bougie `i+1` clôture dans le sens prédit. Une clôture identique est
    une perte — le broker ne rembourse pas l'égalité sur ces contrats.
    """
    total = defaultdict(lambda: [0, 0])
    detail = defaultdict(lambda: defaultdict(lambda: [0, 0]))

    for paire, segs in par_paire.items():
        for seg in segs:
            for i in range(CONTEXTE, len(seg) - 1):
                historique = seg[:i + 1]
                suivant = seg[i + 1]
                resultat = _sens(suivant)
                for nom, f in hypotheses.items():
                    pari = f(historique)
                    if pari == 0:
                        continue
                    total[nom][0] += 1
                    if pari == resultat:
                        total[nom][1] += 1
                    if par_paire_aussi:
                        detail[nom][paire][0] += 1
                        if pari == resultat:
                            detail[nom][paire][1] += 1
    return total, detail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--payout", type=float, default=88.0,
                    help="Payout en %% (défaut 88, mesuré sur les paires "
                         "épinglées). Il fixe le seuil de rentabilité.")
    ap.add_argument("--min-signaux", type=int, default=100,
                    help="Hypothèses sous ce nombre de signaux : écartées. "
                         "En dessous, la marge d'erreur dépasse l'avantage "
                         "cherché et le résultat ne veut rien dire.")
    a = ap.parse_args(argv)

    charger_env_local()
    seuil = 1 / (1 + a.payout / 100)

    par_paire = charger()
    if not par_paire:
        print("Aucune tranche continue d'au moins "
              f"{SEGMENT_MIN} bougies. Rien à analyser.", file=sys.stderr)
        return 1

    n_seg = sum(len(s) for s in par_paire.values())
    n_bougies = sum(len(x) for s in par_paire.values() for x in s)
    print("=" * 78)
    print("BANC D'ESSAI D'HYPOTHÈSES")
    print("=" * 78)
    print(f"{len(par_paire)} paire(s), {n_seg} tranche(s) continue(s), "
          f"{n_bougies} bougies exploitables")
    print(f"Payout {a.payout:.0f} % -> il faut battre {100 * seuil:.2f} % "
          f"de réussite pour ne rien perdre")
    print()

    hypotheses = catalogue()
    total, _ = evaluer(par_paire, hypotheses)

    retenues = [(nom, r, n) for nom, (n, r) in total.items()
                if n >= a.min_signaux]
    ecartees = len(total) - len(retenues)
    if not retenues:
        print("Aucune hypothèse n'atteint le nombre minimal de signaux.",
              file=sys.stderr)
        return 1

    ps = [p_unilateral(r, n, seuil) for _, r, n in retenues]
    corriges = benjamini_hochberg(ps)

    lignes = sorted(zip(retenues, ps, corriges), key=lambda x: x[1])
    print(f"{'hypothèse':<40}{'signaux':>9}{'réussite':>10}"
          f"{'p':>9}{'p corrigé':>11}")
    print("-" * 78)
    for (nom, r, n), p, pc in lignes:
        marque = "  <-- " if pc < 0.05 else ""
        print(f"{nom:<40}{n:>9}{100 * r / n:>9.1f}%{p:>9.3f}{pc:>11.3f}{marque}")

    print("-" * 78)
    print(f"{len(retenues)} hypothèses testées, {ecartees} écartées faute de "
          f"signaux.")
    gagnantes = [x for x in lignes if x[2] < 0.05]
    print()
    if gagnantes:
        print(f"{len(gagnantes)} hypothèse(s) survivent à la correction pour "
              f"comparaisons multiples.")
        print("Ce sont des CANDIDATES. Chacune doit maintenant être écrite "
              "comme une Strategy,")
        print("passée au backtest (§2), à ses contrefactuels (§3.1) et au "
              "walk-forward (§2.5),")
        print("sur des données qu'elle n'a pas vues.")
    else:
        print("AUCUNE hypothèse ne bat le seuil de rentabilité une fois "
              "corrigée du nombre")
        print("de tests. C'est le résultat le plus fréquent, et le dire est "
              "tout l'intérêt")
        print("de cet outil : sans la correction, les meilleures lignes "
              "ci-dessus auraient")
        print("l'air prometteuses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
