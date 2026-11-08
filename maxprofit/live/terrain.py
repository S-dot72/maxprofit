"""
`/terrain` : le prix M1 des paires OTC est-il prévisible par une règle
simple, au-delà du point mort ? LECTURE SEULE, sur la relève. Demandé le
08/11 :

> « Si ZoneH1 n'est pas rentable tu dois trouver autre chose. »

Pré-inscription : `docs/preinscription_terrain.json` (et `.md`), empreinte
`60e6ddaf…`. La passe refuse de tourner si `PARAMETRES` ne donnent plus
cette empreinte.

--- Les cinq familles, sans ZoneH1 ----------------------------------------------

- F1 suite : le mouvement des k dernières minutes continue, ou revient ;
- F2 excès : idem, après un mouvement de plus de 3 fois la médiane de la
  veille ;
- F3 meneur : une paire qui partage une devise annonce une autre paire ;
- F4 triangle : une paire s'écarte du prix que lui donnent les deux autres
  paires du triangle, puis revient ;
- F5 heure : une paire monte ou baisse selon l'heure UTC.

Chaque case (famille × portée × k × échéance) choisit son sens sur
l'EXPLORATION (avant le 03/11), puis se juge sur la VALIDATION (03/11 au
09/11), contre 52,08 %, en BH à 10 %. Les bougies du 10/11 et après ne sont
jamais lues : elles confirmeront une piste gelée.

--- Lectures du JSON, écrites avant toute donnée ---------------------------------

- F3 : contiguïté et sauts sur X de t − k à t, sur Y de t à t + h ;
- F4 et F5 : k = 0. F4 exige les trois paires présentes à t, sans saut entre
  t − 1 et t, et la paire jouée contiguë de t à t + h ;
- F2 : la médiane de la veille porte sur les mouvements dont les bougies
  sont présentes et sans saut ;
- un écart-type de F4 sous 1e-9 est un écart-type nul (bruit d'arrondi).

Sorties en CSV seulement, aucune écriture en base. La course ne lit rien
d'ici.
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import math
import random
import re
import time
from array import array
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from itertools import combinations
from statistics import NormalDist
from typing import Any, Callable, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.live.etude3 import ConnexionTracee, etat_lecture_seule
from maxprofit.live.selection3 import (PAYOUT_AVANT, PAYOUTS_DU_JOUR,
                                       REQUETES_COPIE)

log = logging.getLogger(__name__)

PARAMETRES: dict[str, Any] = json.loads(r"""
{
  "cases": {
    "F1": "k × horizon, toutes paires ensemble, et par paire",
    "F2": "k × horizon, toutes paires ensemble, et par paire",
    "F3": "k × horizon, tous couples ensemble, et par couple ordonné",
    "F4": "horizon, tous triangles ensemble, et par (triangle, paire jouée)",
    "F5": "paire × heure UTC"
  },
  "descriptif": "par paire : minutes lues, part jouable, part des minutes sans mouvement, autocorrélation des rendements 1 min aux retards 1 à 5 ; jamais un critère",
  "donnees": {
    "confirmation": "les bougies à partir du 10/11 00:00 UTC ne sont jamais lues par /terrain : elles servent à confirmer une piste gelée",
    "debut_utc": "2026-10-06T00:00:00Z",
    "fin_exploration_utc": "2026-11-03T00:00:00Z",
    "fin_validation_utc": "2026-11-10T00:00:00Z",
    "paires": "toutes les paires qui ont des bougies M1 dans la période (résumé calculé par le serveur)",
    "table": "candles, tf_sec = 60, complete = 1, lues sur la relève ; un jour dont le résumé diffère de l'active est lu sur l'active",
    "validation": "jours UTC complets du 03/11 à la veille du lancement, au plus jusqu'au 09/11 inclus"
  },
  "familles": {
    "F1_suite": "signe de close_t − close_{t−k}, k ∈ {1, 5, 15} ; mouvement nul : pas de décision ; pari dans ce sens (suite) ou à l'inverse (retour)",
    "F2_exces": "|close_t − close_{t−k}| ≥ 3 × la médiane des |close_u − close_{u−k}| du jour UTC précédent de la paire (au moins 720 valeurs), k ∈ {1, 5} ; pari dans le sens du mouvement ou à l'inverse",
    "F3_meneur": "couples ordonnés (X, Y) de paires distinctes qui partagent exactement une devise, bougies de la même minute ; signe du mouvement de X sur k minutes, k ∈ {1, 5}, orienté (+1 si la devise commune est à la même place dans X et Y, −1 sinon) ; pari sur Y dans ce sens ou à l'inverse",
    "F4_triangle": "trois paires qui forment un triangle de devises ; pour chaque paire T = U/V du triangle, e = ln(T) − ln(prix de U/V impliqué par les deux autres) ; z = (e − moyenne des 60 minutes précédentes) / écart-type de ces 60 minutes (au moins 50 valeurs, écart-type > 0) ; si |z| ≥ 2, pari que T revient (baisse si z > 0, hausse si z < 0) ou l'inverse",
    "F5_heure": "pari haussier ou baissier sur la paire, selon l'heure UTC de la décision, horizon 15 seulement"
  },
  "incertitude": "bootstrap par blocs (paire jouée × jour UTC), 1 000 tirages, graine 7 ; IC 95 % percentile ; p unilatérale par l'approximation normale de l'écart-type bootstrap",
  "interdits": [
    "aucune écriture en base, sorties en CSV seulement",
    "aucune lecture des bougies à partir du 10/11 00:00 UTC",
    "la course, la collecte, la stratégie, les entrées et les échéances ne changent pas",
    "aucun paramètre ne se rechoisit après le gel : un changement fait une nouvelle pré-inscription, avec une nouvelle empreinte"
  ],
  "issue": {
    "contiguite": "toutes les bougies de t − k à t + h présentes et complètes (k = 0 pour F5)",
    "decision": "à la clôture de chaque bougie M1 t",
    "entree": "clôture de la bougie t",
    "gain": "mouvement strict dans le sens parié ; une égalité est une perte (sensibilité publiée : égalités exclues)",
    "horizons_min": [
      1,
      5,
      15
    ],
    "saut_aberrant": "une variation de clôture à clôture de plus de 1 % en une minute ; toute décision dont la fenêtre [t − k, t + h] en contient un est écartée et comptée à part",
    "sortie": "clôture de la bougie t + h"
  },
  "jouable": "dernier payout du flux relevé au plus tard à la décision (fin de la bougie t) ≥ 84, soit 92 % appliqués ; sans relevé, la décision n'est pas jouable",
  "objet": "Le prix M1 des paires OTC est-il prévisible par une règle simple, au-delà du point mort de 52,0833 % ? Étude du terrain, indépendante de ZoneH1.",
  "point_mort": 0.5208333333,
  "selection": "sur l'exploration, chaque case prend le sens (suite / retour, hausse / baisse) au meilleur taux jouable ; elle est retenue si ce taux atteint 52,0833 % avec au moins 500 décisions jouables",
  "validation": "chaque case retenue est jugée sur la validation dans le sens choisi : taux jouable, IC 95 % et p unilatérale contre 52,0833 % ; une case qui a moins de 200 décisions jouables en validation n'est pas jugée ; Benjamini-Hochberg à 10 % sur toutes les cases jugées",
  "verdicts": {
    "piste": "case découverte en BH dans la validation : elle est gelée puis confirmée sur les bougies à partir du 10/11, avant toute autre décision",
    "terrain_muet": "aucune case découverte : aucune de ces règles ne bat le point mort sur l'OTC"
  }
}
""")
EMPREINTE = "60e6ddaf41de7d3fe7409ca753cab7f10c956861658ae64278c6fbdb47138f79"


def _instant(texte: str) -> int:
    return int(datetime.strptime(texte, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc).timestamp())


DEBUT = _instant(PARAMETRES["donnees"]["debut_utc"])
FIN_EXPLORATION = _instant(PARAMETRES["donnees"]["fin_exploration_utc"])
FIN_VALIDATION = _instant(PARAMETRES["donnees"]["fin_validation_utc"])
HORIZONS: tuple[int, ...] = tuple(PARAMETRES["issue"]["horizons_min"])
POINT_MORT: float = PARAMETRES["point_mort"]

K_SUITE = (1, 5, 15)
K_EXCES = (1, 5)
K_MENEUR = (1, 5)
EXCES_FACTEUR = 3.0
EXCES_MIN = 720
Z_FENETRE = 60
Z_MIN = 50
Z_SEUIL = 2.0
VARIANCE_NULLE = 1e-18
HORIZON_HEURE = 15
SAUT = 0.01
FLUX_MIN = 84
N_EXPLO_MIN = 500
N_VALID_MIN = 200
MINUTES = 1440
RESPIRATION_SEC = 0.02

FAMILLES = ("F1", "F2", "F3", "F4", "F5")
NOMS = {"F1": "suite", "F2": "excès", "F3": "meneur", "F4": "triangle",
        "F5": "heure"}
SENS = {"F1": ("suite", "retour"), "F2": ("suite", "retour"),
        "F3": ("comme le meneur", "à l'inverse du meneur"),
        "F4": ("retour à la parité", "écart qui se creuse"),
        "F5": ("hausse", "baisse")}
TOUTES = "toutes"

#: Codes des tableaux de signes : 9 = bougie absente, 8 = saut aberrant.
ABSENT, SAUTE = 9, 8

SQL_BOUGIES = ("SELECT ts_sec, close FROM candles WHERE pair = ? AND "
               "tf_sec = 60 AND ts_sec >= ? AND ts_sec < ? AND complete = 1 "
               "ORDER BY ts_sec")

_N = NormalDist()


def _utc(ts: float, forme: str = "%d/%m") -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime(forme)


def fin_de_validation(maintenant: float) -> int:
    """Les jours UTC complets jusqu'à la veille du lancement, au plus
    jusqu'au 09/11 inclus."""
    return min(FIN_VALIDATION, int(maintenant) // 86400 * 86400)


# --------------------------------------------------------------------------- #
# Les séries
# --------------------------------------------------------------------------- #

class Serie:
    """Une paire, minute par minute depuis `debut` : clôtures (NaN si la
    bougie manque), jouabilité à la décision, et les sommes cumulées qui
    disent en temps constant si une fenêtre est contiguë et sans saut."""

    def __init__(self, paire: str, debut: int, minutes: int) -> None:
        self.paire = paire
        self.debut = debut
        self.m = minutes
        self.close = array("d", [math.nan]) * minutes
        self.jouable = bytearray(minutes)
        self.manque = array("i", [0]) * (minutes + 1)
        self.sauts = array("i", [0]) * (minutes + 1)
        self.signes_passe: dict[int, array] = {}
        self.signes_futur: dict[int, array] = {}

    def finir(self) -> None:
        c, manque, sauts = self.close, self.manque, self.sauts
        mq = sa = 0
        prec = math.nan
        for i in range(self.m):
            x = c[i]
            if x != x:
                mq += 1
            elif prec == prec and abs(x / prec - 1.0) > SAUT:
                sa += 1
            manque[i + 1] = mq
            sauts[i + 1] = sa
            prec = x
        for k in sorted(set(K_SUITE) | set(K_EXCES) | set(K_MENEUR)):
            self.signes_passe[k] = self._signes(-k)
        for h in HORIZONS:
            self.signes_futur[h] = self._signes(h)

    def etat(self, a: int, b: int) -> int:
        """0 si les bougies a..b sont présentes sans saut entre elles,
        ABSENT s'il en manque une (ou hors bornes), SAUTE sinon."""
        if a < 0 or b >= self.m or a > b:
            return ABSENT
        if self.manque[b + 1] - self.manque[a]:
            return ABSENT
        if self.sauts[b + 1] - self.sauts[a + 1]:
            return SAUTE
        return 0

    def _signes(self, decalage: int) -> array:
        """Pour décalage −k : signe de close_t − close_{t−k} ; pour +h :
        signe de close_{t+h} − close_t ; ou ABSENT / SAUTE."""
        sortie = array("b", [ABSENT]) * self.m
        c = self.close
        for t in range(self.m):
            a, b = (t + decalage, t) if decalage < 0 else (t, t + decalage)
            e = self.etat(a, b)
            if e:
                sortie[t] = e
                continue
            d = c[b] - c[a]
            sortie[t] = 1 if d > 0 else -1 if d < 0 else 0
        return sortie

    def minutes_presentes(self) -> int:
        return self.m - self.manque[self.m]


def devises(paire: str) -> tuple[str, str] | None:
    m = re.fullmatch(r"([A-Z]{3})([A-Z]{3})(?:_otc)?", paire)
    return (m.group(1), m.group(2)) if m else None


def orientation_meneur(x: str, y: str) -> int:
    """+1 si la devise commune est à la même place dans X et Y, −1 sinon,
    0 si les paires ne partagent pas exactement une devise."""
    dx, dy = devises(x), devises(y)
    if not dx or not dy or x == y:
        return 0
    communes = set(dx) & set(dy)
    if len(communes) != 1:
        return 0
    c = next(iter(communes))
    return (1 if dx[0] == c else -1) * (1 if dy[0] == c else -1)


def triangles(paires: Sequence[str]
              ) -> list[tuple[tuple[str, str, str], list[str]]]:
    """Les trios de devises dont les trois paires sont présentes."""
    par_devises: dict[frozenset, str] = {}
    for p in sorted(paires):
        d = devises(p)
        if d and d[0] != d[1]:
            par_devises.setdefault(frozenset(d), p)
    monnaies = sorted({c for cle in par_devises for c in cle})
    sortie = []
    for a, b, c in combinations(monnaies, 3):
        trio = [par_devises.get(frozenset(x))
                for x in ((a, b), (b, c), (a, c))]
        if all(trio):
            sortie.append(((a, b, c), trio))
    return sortie


# --------------------------------------------------------------------------- #
# Les comptes
# --------------------------------------------------------------------------- #

class Comptes:
    """case → bloc (paire jouée, jour UTC) → [gains, pertes, égalités]
    jouables puis toutes décisions. Le « gain » est celui du pari
    principal de la famille ; la « perte », celui du pari inverse."""

    def __init__(self) -> None:
        self.cases: dict[tuple, dict[tuple[str, int], list[int]]] = {}
        self.paire_jouee: dict[tuple, str] = {}
        self.sauts: Counter = Counter()

    def case(self, famille: str, portee: str, k: int, h: int,
             paire_jouee: str) -> dict[tuple[str, int], list[int]]:
        cle = (famille, portee, k, h)
        if cle not in self.cases:
            self.cases[cle] = defaultdict(lambda: [0] * 6)
            self.paire_jouee[cle] = paire_jouee
        return self.cases[cle]


def _compter(blocs, paire: str, jour0: int, t: int, base: int, issue: int,
             jouable: int) -> None:
    rel = base * issue
    i = 0 if rel > 0 else 1 if rel < 0 else 2
    ligne = blocs[(paire, jour0 + t // MINUTES)]
    ligne[3 + i] += 1
    if jouable:
        ligne[i] += 1


def familles_d_une_paire(s: Serie, comptes: Comptes,
                         respirer: Callable[[], None]) -> None:
    """F1, F2 et F5 sur une paire."""
    jour0 = s.debut // 86400
    p = s.paire
    jouable = s.jouable
    futurs = s.signes_futur
    # F1 suite
    for k in K_SUITE:
        passe = s.signes_passe[k]
        cases = {h: comptes.case("F1", p, k, h, p) for h in HORIZONS}
        for t in range(s.m):
            sk = passe[t]
            if sk == ABSENT or sk == 0:
                continue
            for h in HORIZONS:
                oh = futurs[h][t]
                if oh == ABSENT:
                    continue
                if sk == SAUTE or oh == SAUTE:
                    comptes.sauts["F1"] += 1
                    continue
                _compter(cases[h], p, jour0, t, sk, oh, jouable[t])
        respirer()
    # F2 excès : l'échelle d'un jour est la médiane de la veille.
    c = s.close
    for k in K_EXCES:
        passe = s.signes_passe[k]
        par_jour: dict[int, list[float]] = defaultdict(list)
        for t in range(s.m):
            if passe[t] in (-1, 0, 1):
                par_jour[t // MINUTES].append(abs(c[t] - c[t - k]))
        echelle = {}
        for j, v in par_jour.items():
            if len(v) >= EXCES_MIN:
                v.sort()
                n = len(v)
                echelle[j + 1] = (v[n // 2] if n % 2
                                  else (v[n // 2 - 1] + v[n // 2]) / 2)
        cases = {h: comptes.case("F2", p, k, h, p) for h in HORIZONS}
        for t in range(s.m):
            sk = passe[t]
            if sk == ABSENT or sk == 0:
                continue
            seuil = echelle.get(t // MINUTES)
            if not seuil:
                continue
            if abs(c[t] - c[t - k]) < EXCES_FACTEUR * seuil:
                continue
            for h in HORIZONS:
                oh = futurs[h][t]
                if oh == ABSENT:
                    continue
                if sk == SAUTE or oh == SAUTE:
                    comptes.sauts["F2"] += 1
                    continue
                _compter(cases[h], p, jour0, t, sk, oh, jouable[t])
        respirer()
    # F5 heure : décision à la fin de la bougie t.
    oh15 = futurs[HORIZON_HEURE]
    cases5 = {}
    for t in range(s.m):
        oh = oh15[t]
        if oh == ABSENT:
            continue
        if oh == SAUTE:
            comptes.sauts["F5"] += 1
            continue
        heure = ((s.debut + 60 * (t + 1)) // 3600) % 24
        if heure not in cases5:
            cases5[heure] = comptes.case("F5", f"{p}@{heure:02d}h", 0,
                                         HORIZON_HEURE, p)
        _compter(cases5[heure], p, jour0, t, 1, oh, jouable[t])
    respirer()


def famille_meneur(x: Serie, y: Serie, comptes: Comptes,
                   respirer: Callable[[], None]) -> None:
    """F3 : le mouvement de X sur k minutes, orienté, parie sur Y."""
    o = orientation_meneur(x.paire, y.paire)
    if not o or x.m != y.m:
        return
    jour0 = y.debut // 86400
    portee = f"{x.paire}→{y.paire}"
    jouable = y.jouable
    for k in K_MENEUR:
        passe = x.signes_passe[k]
        cases = {h: comptes.case("F3", portee, k, h, y.paire)
                 for h in HORIZONS}
        for t in range(y.m):
            sk = passe[t]
            if sk == ABSENT or sk == 0:
                continue
            for h in HORIZONS:
                oh = y.signes_futur[h][t]
                if oh == ABSENT:
                    continue
                if sk == SAUTE or oh == SAUTE:
                    comptes.sauts["F3"] += 1
                    continue
                _compter(cases[h], y.paire, jour0, t, o * sk, oh, jouable[t])
        respirer()


def famille_triangle(trio: tuple[str, str, str], series: Sequence[Serie],
                     comptes: Comptes, respirer: Callable[[], None]) -> None:
    """F4 : pour chaque paire T = U/V du triangle, l'écart entre ln T et le
    ln du prix de U/V que donnent les deux autres paires."""
    for t_serie in series:
        u, v = devises(t_serie.paire)
        w = next(c for c in trio if c not in (u, v))
        jambes = []
        for a, b in ((u, w), (w, v)):
            s = next(x for x in series if set(devises(x.paire)) == {a, b})
            jambes.append((s, 1.0 if devises(s.paire)[0] == a else -1.0))
        portee = f"{'-'.join(trio)}:{t_serie.paire}"
        cases = {h: comptes.case("F4", portee, 0, h, t_serie.paire)
                 for h in HORIZONS}
        jour0 = t_serie.debut // 86400
        fen: deque = deque()
        s1 = s2 = 0.0
        ref = None
        tous = [t_serie] + [s for s, _ in jambes]
        for t in range(t_serie.m):
            while fen and fen[0][0] < t - Z_FENETRE:
                _, ancien = fen.popleft()
                s1 -= ancien
                s2 -= ancien * ancien
            if not all(_present_sans_saut(s, t) for s in tous):
                continue
            e = math.log(t_serie.close[t]) - sum(
                signe * math.log(s.close[t]) for s, signe in jambes)
            if ref is None:
                ref = e
            val = e - ref
            n = len(fen)
            if n >= Z_MIN:
                moy = s1 / n
                var = (s2 - s1 * s1 / n) / (n - 1)
                if var > VARIANCE_NULLE:
                    z = (val - moy) / math.sqrt(var)
                    if abs(z) >= Z_SEUIL:
                        base = -1 if z > 0 else 1
                        for h in HORIZONS:
                            oh = t_serie.signes_futur[h][t]
                            if oh == ABSENT:
                                continue
                            if oh == SAUTE:
                                comptes.sauts["F4"] += 1
                                continue
                            _compter(cases[h], t_serie.paire, jour0, t, base,
                                     oh, t_serie.jouable[t])
            fen.append((t, val))
            s1 += val
            s2 += val * val
        respirer()


def _present_sans_saut(s: Serie, t: int) -> bool:
    """La bougie t est là, et pas de saut depuis t − 1 (si t − 1 manque,
    la présence suffit)."""
    return s.close[t] == s.close[t] and (t == 0 or s.etat(t - 1, t) != SAUTE)


# --------------------------------------------------------------------------- #
# Les verdicts
# --------------------------------------------------------------------------- #

def _somme(blocs: Mapping[tuple[str, int], Sequence[int]],
           jours: Callable[[int], bool]) -> list[int]:
    t = [0] * 6
    for (_, jour), ligne in blocs.items():
        if jours(jour):
            for i in range(6):
                t[i] += ligne[i]
    return t


def _bootstrap(par_bloc: Sequence[tuple[int, int]], taux: float
               ) -> tuple[float | None, float | None, float | None]:
    """(bas, haut, p unilatérale contre le point mort)."""
    k = len(par_bloc)
    if k < 2:
        return None, None, None
    alea = random.Random(em.GRAINE)
    valeurs = []
    for _ in range(em.TIRAGES):
        g = n = 0
        for i in alea.choices(range(k), k=k):
            g += par_bloc[i][0]
            n += par_bloc[i][1]
        if n:
            valeurs.append(g / n)
    if len(valeurs) < em.TIRAGES // 2:
        return None, None, None
    valeurs.sort()
    moy = sum(valeurs) / len(valeurs)
    et = (sum((v - moy) ** 2 for v in valeurs) / (len(valeurs) - 1)) ** 0.5
    p = ((1 - _N.cdf((taux - POINT_MORT) / et)) if et > 0
         else (0.0 if taux > POINT_MORT else 1.0))
    return (valeurs[int(0.025 * len(valeurs))],
            valeurs[int(0.975 * len(valeurs)) - 1], p)


def juger(comptes: Comptes, fin_valid: int,
          respirer: Callable[[], None] | None = None) -> list[dict[str, Any]]:
    """Une ligne par case, et par case « toutes » de F1 à F4."""
    respirer = respirer or (lambda: None)
    j_explo = FIN_EXPLORATION // 86400
    j_valid = fin_valid // 86400
    en_explo = (lambda j: j < j_explo)
    en_valid = (lambda j: j_explo <= j < j_valid)
    cases: dict[tuple, Mapping] = dict(comptes.cases)
    jouees = dict(comptes.paire_jouee)
    regroupees: dict[tuple, dict] = {}
    for (famille, portee, k, h), blocs in comptes.cases.items():
        if famille == "F5":
            continue
        cible = regroupees.setdefault((famille, TOUTES, k, h),
                                      defaultdict(lambda: [0] * 6))
        for bloc, ligne in blocs.items():
            for i in range(6):
                cible[bloc][i] += ligne[i]
    for cle, blocs in regroupees.items():
        cases[cle] = blocs
        jouees[cle] = TOUTES
    lignes = []
    for rang, (cle, blocs) in enumerate(sorted(cases.items())):
        if rang % 50 == 0:
            respirer()
        famille, portee, k, h = cle
        x = _somme(blocs, en_explo)
        n = x[0] + x[1] + x[2]
        if not n:
            continue
        sens = 1 if x[0] >= x[1] else -1
        g = x[0] if sens > 0 else x[1]
        n_toutes = x[3] + x[4] + x[5]
        ligne: dict[str, Any] = {
            "famille": famille, "portee": portee, "paire_jouee": jouees[cle],
            "k": k, "horizon": h, "explo_n": n, "explo_gains": x[0],
            "explo_pertes": x[1], "explo_egalites": x[2],
            "sens": SENS[famille][0 if sens > 0 else 1],
            "explo_taux": g / n,
            "explo_taux_sans_egalites": (g / (x[0] + x[1])
                                         if x[0] + x[1] else None),
            "explo_n_toutes": n_toutes,
            "explo_taux_toutes": ((x[3] if sens > 0 else x[4]) / n_toutes
                                  if n_toutes else None),
            "retenue": n >= N_EXPLO_MIN and g / n >= POINT_MORT}
        if not ligne["retenue"]:
            ligne["verdict"] = "non retenue en exploration"
            lignes.append(ligne)
            continue
        par_bloc = []
        gv = nv = gs = ns = 0
        for (paire, jour), l in sorted(blocs.items()):
            if not en_valid(jour):
                continue
            gb = l[0] if sens > 0 else l[1]
            nb = l[0] + l[1] + l[2]
            if nb:
                par_bloc.append((gb, nb))
            gv += gb
            nv += nb
            gs += gb
            ns += l[0] + l[1]
        ligne.update(valid_n=nv)
        if nv < N_VALID_MIN:
            ligne["verdict"] = "trop peu de décisions en validation"
            lignes.append(ligne)
            continue
        taux = gv / nv
        bas, haut, p = _bootstrap(par_bloc, taux)
        ligne.update(valid_taux=taux,
                     valid_taux_sans_egalites=gs / ns if ns else None,
                     valid_bas=bas, valid_haut=haut, p=p)
        if p is None:
            ligne["verdict"] = "trop peu de blocs en validation"
        lignes.append(ligne)
    jugees = {i: l["p"] for i, l in enumerate(lignes)
              if l.get("p") is not None and "verdict" not in l}
    decouvertes = em.benjamini_hochberg(jugees) if jugees else {}
    for i, l in enumerate(lignes):
        if i in jugees:
            l["verdict"] = ("découverte (BH 10 %)" if decouvertes[i]
                            else "au-dessus du point mort, non significatif"
                            if l["valid_taux"] > POINT_MORT
                            else "sous le point mort en validation")
    return lignes


def descriptif(s: Serie) -> dict[str, Any]:
    """Minutes, part jouable, minutes immobiles, autocorrélations des
    rendements 1 min (retards 1 à 5), sauts."""
    c = s.close
    r = [None] * s.m
    immobiles = mouvements = 0
    for t in range(1, s.m):
        if s.etat(t - 1, t) == 0:
            r[t] = math.log(c[t] / c[t - 1])
            mouvements += 1
            immobiles += c[t] == c[t - 1]
    valides = [x for x in r if x is not None]
    presentes = s.minutes_presentes()
    sortie: dict[str, Any] = {
        "paire": s.paire, "minutes": presentes,
        "part_jouable": (sum(1 for t in range(s.m) if s.jouable[t]
                             and c[t] == c[t]) / presentes
                         if presentes else None),
        "part_immobile": immobiles / mouvements if mouvements else None,
        "sauts": s.sauts[s.m]}
    if len(valides) < 100:
        return sortie
    moy = sum(valides) / len(valides)
    var = sum((x - moy) ** 2 for x in valides)
    for retard in range(1, 6):
        cov = 0.0
        for t in range(retard, s.m):
            a, b = r[t], r[t - retard]
            if a is not None and b is not None:
                cov += (a - moy) * (b - moy)
        sortie[f"ac{retard}"] = cov / var if var > 0 else None
    return sortie


# --------------------------------------------------------------------------- #
# La passe
# --------------------------------------------------------------------------- #

def resumes(conn, debut: int, fin: int) -> dict[tuple[str, str, int], tuple]:
    """(table, paire, jour) → comptes et sommes calculés par le serveur,
    pour les bougies et les payouts."""
    sortie = {}
    for table in ("candles", "payouts"):
        for ligne in conn.execute(REQUETES_COPIE[table],
                                  (int(debut), int(fin))).fetchall():
            sortie[(table, str(ligne[0]), int(ligne[1]))] = tuple(
                None if v is None else int(round(float(v)))
                for v in ligne[2:])
    return sortie


def lire_serie(paire: str, lecteurs: Mapping[str, Any], divergents: set,
               debut: int, fin: int, lus: Counter,
               respirer: Callable[[], None]) -> Serie:
    """Les clôtures M1 complètes et la jouabilité d'UNE paire, jour par
    jour ; un jour divergent est lu sur l'active."""
    s = Serie(paire, debut, (fin - debut) // 60)
    ligne = lecteurs["lue"].execute(PAYOUT_AVANT, (paire, debut)).fetchone()
    payout = None if not ligne or ligne[1] is None else float(ligne[1])
    for jour in range(debut // 86400, fin // 86400):
        a, b = jour * 86400, (jour + 1) * 86400
        ou = "active" if ("candles", paire, jour) in divergents else "lue"
        lus[ou] += 1
        for ts, close in lecteurs[ou].execute(SQL_BOUGIES,
                                              (paire, a, b)).fetchall():
            i = (int(ts) - debut) // 60
            if 0 <= i < s.m and close is not None and float(close) > 0:
                s.close[i] = float(close)
        ou = "active" if ("payouts", paire, jour) in divergents else "lue"
        changements = lecteurs[ou].execute(PAYOUTS_DU_JOUR,
                                           (paire, a, b)).fetchall()
        k = 0
        for i in range((a - debut) // 60, (b - debut) // 60):
            decision = debut + 60 * (i + 1)
            while k < len(changements) and int(changements[k][0]) <= decision:
                payout = (None if changements[k][1] is None
                          else float(changements[k][1]))
                k += 1
            if payout is not None and payout >= FLUX_MIN:
                s.jouable[i] = 1
        respirer()
    s.finir()
    respirer()
    return s


def terrain(lue_brute, active_brute, course=None, *,
            nom_lecture: str = "relève", maintenant: float | None = None,
            partielle: bool = False,
            respirer: Callable[[], None] | None = None
            ) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers). Les deux bases sont déjà en lecture seule ;
    `active_brute` peut être None (copie non vérifiée)."""
    em.exiger_le_gel(PARAMETRES, EMPREINTE)
    respirer = respirer or (lambda: time.sleep(RESPIRATION_SEC))
    maintenant = time.time() if maintenant is None else maintenant
    t0 = time.monotonic()
    fin = fin_de_validation(maintenant)
    if fin <= FIN_EXPLORATION:
        return "🧭 La validation commence le 03/11 : rien à juger encore.", []
    if fin < FIN_VALIDATION and not partielle:
        return ("🧭 La validation court jusqu'au 09/11 inclus. /terrain se "
                "lance à partir du 10/11 00:00 UTC, pour une seule lecture "
                f"de la semaine entière. /terrain partielle juge dès "
                f"maintenant sur les jours complets ({_utc(FIN_EXPLORATION)} "
                f"au {_utc(fin - 86400)}), et ce premier jugement sera "
                "alors le seul."), []
    lue = ConnexionTracee(lue_brute)
    active = (ConnexionTracee(active_brute)
              if active_brute is not None and active_brute is not lue_brute
              else None)
    lecture = {nom_lecture: etat_lecture_seule(lue)}
    if active is not None:
        lecture["active"] = etat_lecture_seule(active)
    resume_lue = resumes(lue, DEBUT, fin)
    divergents: set = set()
    if active is not None:
        resume_active = resumes(active, DEBUT, fin)
        divergents = {c for c in set(resume_lue) | set(resume_active)
                      if resume_lue.get(c) != resume_active.get(c)}
    paires = sorted({p for (table, p, _) in resume_lue if table == "candles"})
    lecteurs = {"lue": lue, "active": active}
    lus: Counter = Counter()
    series = [lire_serie(p, lecteurs, divergents, DEBUT, fin, lus, respirer)
              for p in paires]
    series = [s for s in series if s.minutes_presentes()]
    comptes = Comptes()
    for s in series:
        familles_d_une_paire(s, comptes, respirer)
    couples = [(x, y) for x in series for y in series
               if orientation_meneur(x.paire, y.paire)]
    for x, y in couples:
        famille_meneur(x, y, comptes, respirer)
    par_nom = {s.paire: s for s in series}
    trios = triangles(list(par_nom))
    for trio, noms in trios:
        famille_triangle(trio, [par_nom[n] for n in noms], comptes, respirer)
    lignes = juger(comptes, fin, respirer)
    paires_desc = [descriptif(s) for s in series]
    rapport = texte(lignes, paires_desc, {
        "nom": nom_lecture, "lecture": lecture, "fin": fin,
        "divergences": len(divergents), "lus": lus,
        "couples": len(couples), "triangles": len(trios),
        "sauts": comptes.sauts,
        "requetes": sum(lue.requetes.values()) + (
            sum(active.requetes.values()) if active is not None else 0),
        "refusees": lue.refusees + (active.refusees if active else 0),
        "temps": time.monotonic() - t0})
    return rapport, fichiers(lignes, paires_desc)


# --------------------------------------------------------------------------- #
# Le rapport
# --------------------------------------------------------------------------- #

def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.1f} %".replace(".", ",")


def _p(p: float) -> str:
    return "< 0,0001" if p < 1e-4 else f"{p:.4f}".replace(".", ",")


def _v(x: float) -> str:
    return f"{x:+.3f}".replace(".", ",")


def _libelle(l: Mapping) -> str:
    k = f", k = {l['k']}" if l["famille"] in ("F1", "F2", "F3") else ""
    return (f"{l['famille']} {NOMS[l['famille']]} [{l['portee']}{k}, "
            f"{l['horizon']} min, {l['sens']}]")


def texte(lignes: Sequence[Mapping], paires: Sequence[Mapping],
          meta: Mapping) -> str:
    fin = meta["fin"]
    jours_valid = (fin - FIN_EXPLORATION) // 86400
    par_famille = Counter(l["famille"] for l in lignes)
    retenues = [l for l in lignes if l["retenue"]]
    jugees = [l for l in lignes if l.get("p") is not None]
    decouvertes = [l for l in lignes
                   if l.get("verdict") == "découverte (BH 10 %)"]
    minutes = sum(p["minutes"] for p in paires)
    jouables = sum((p["part_jouable"] or 0) * p["minutes"] for p in paires)
    ac1 = [p["ac1"] for p in paires if p.get("ac1") is not None]
    morceaux = [
        "🧭 <b>Terrain OTC : le prix M1 est-il prévisible ?</b> "
        "(lecture seule)",
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in meta["lecture"].items())
        + f" ; requêtes refusées : {meta['refusees']}. Rien n'a été écrit "
        "en base.",
        f"Gel vérifié : empreinte {EMPREINTE[:8]}…",
        f"Données : {len(paires)} paires, du {_utc(DEBUT)} au "
        f"{_utc(fin - 86400)} ; exploration jusqu'au "
        f"{_utc(FIN_EXPLORATION - 86400)}, validation du "
        f"{_utc(FIN_EXPLORATION)} au {_utc(fin - 86400)} "
        f"({jours_valid} jour{'s' if jours_valid > 1 else ''}"
        + ("" if fin >= FIN_VALIDATION else ", partielle") + ").",
        f"Minutes lues : {minutes:,}".replace(",", " ")
        + f", dont {_pct(jouables / minutes if minutes else None)} "
        f"jouables. Jours lus sur l'active (copie divergente) : "
        f"{meta['lus'].get('active', 0)}.",
        "Cases : " + ", ".join(f"{f} {par_famille.get(f, 0)}"
                               for f in FAMILLES)
        + f" ({meta['couples']} couples meneur → suiveur, "
        f"{meta['triangles']} triangle"
        f"{'s' if meta['triangles'] > 1 else ''}).",
        f"Retenues en exploration (≥ 52,08 %, ≥ {N_EXPLO_MIN} décisions "
        f"jouables) : {len(retenues)}. Jugées en validation : {len(jugees)}. "
        f"<b>Découvertes (BH 10 %) : {len(decouvertes)}</b>."]
    if decouvertes:
        morceaux.append("<b>Pistes</b> (à geler, puis à confirmer sur les "
                        "bougies à partir du 10/11) :")
        for l in sorted(decouvertes, key=lambda l: l["p"])[:10]:
            morceaux.append(
                f"• {html.escape(_libelle(l))} : validation "
                f"{_pct(l['valid_taux'])} sur {l['valid_n']} "
                f"[{_pct(l['valid_bas'])} ; {_pct(l['valid_haut'])}], "
                f"p = {_p(l['p'])} ; exploration {_pct(l['explo_taux'])}.")
    else:
        morceaux.append("<b>Terrain muet</b> : aucune de ces règles ne bat "
                        "le point mort en validation.")
        meilleures = sorted(jugees, key=lambda l: -l["valid_taux"])[:3]
        if meilleures:
            morceaux.append("Les meilleures en validation (descriptif) :")
            for l in meilleures:
                morceaux.append(
                    f"• {html.escape(_libelle(l))} : {_pct(l['valid_taux'])}"
                    f" sur {l['valid_n']}, p = {_p(l['p'])}.")
    sauts = sum(meta["sauts"].values())
    morceaux.append(f"Décisions écartées pour saut aberrant (> 1 % en une "
                    f"minute) : {sauts:,}".replace(",", " ") + ".")
    if ac1:
        morceaux.append(
            f"Empreinte du générateur : autocorrélation des rendements "
            f"1 min au retard 1, médiane {_v(sorted(ac1)[len(ac1) // 2])} "
            f"(de {_v(min(ac1))} à {_v(max(ac1))}) sur {len(ac1)} paires.")
    morceaux.append("Rien ne change dans la course. Une découverte n'est "
                    "qu'une piste : elle se gèle, puis se juge sur des "
                    "bougies jamais lues.")
    duree = f"{meta['temps'] / 60:.1f}".replace(".", ",")
    morceaux.append(f"Requêtes : {meta['requetes']}. Durée : {duree} min.")
    return "\n".join(morceaux)


COLONNES_CASES = (
    "famille", "portee", "paire_jouee", "k", "horizon", "explo_n",
    "explo_gains", "explo_pertes", "explo_egalites", "sens", "explo_taux",
    "explo_taux_sans_egalites", "explo_n_toutes", "explo_taux_toutes",
    "retenue", "valid_n", "valid_taux", "valid_taux_sans_egalites",
    "valid_bas", "valid_haut", "p", "verdict")
COLONNES_PAIRES = ("paire", "minutes", "part_jouable", "part_immobile",
                   "sauts", "ac1", "ac2", "ac3", "ac4", "ac5")


def _csv(lignes: Sequence[Mapping], colonnes: Sequence[str]) -> bytes:
    sortie = io.StringIO()
    w = csv.writer(sortie, delimiter=";")
    w.writerow(colonnes)
    for l in lignes:
        rang = []
        for c in colonnes:
            v = l.get(c)
            rang.append("" if v is None else f"{v:.6f}"
                        if isinstance(v, float) else int(v)
                        if isinstance(v, bool) else v)
        w.writerow(rang)
    return sortie.getvalue().encode("utf-8")


def fichiers(lignes: Sequence[Mapping], paires: Sequence[Mapping]
             ) -> list[tuple[bytes, str, str]]:
    return [(_csv(lignes, COLONNES_CASES), "terrain_cases.csv",
             "Une ligne par case : exploration, validation, verdict"),
            (_csv(paires, COLONNES_PAIRES), "terrain_paires.csv",
             "Descriptif par paire (jamais un critère)")]
