"""
Recherche automatique de motifs sur les paires OTC : une étude ISOLÉE, en
LECTURE SEULE.

Protocole figé : `docs/preinscription_motifs.json`, empreinte 028f6370…
La passe refuse de tourner si `PARAMETRES` ne donnent plus cette empreinte.

À la clôture de CHAQUE bougie M1 de chaque paire (fenêtre de 240 minutes
continues), l'état récent du prix est décrit par 22 familles de motifs
objectifs, soit 511 états : couleurs des 1 à 6 dernières bougies, couleur et
taille des corps, mèches, rendements de 1 à 60 minutes, position dans
l'étendue, volatilité, dernier extrême de l'heure, série en cours. Le
mouvement des 1, 2, 3 et 5 minutes suivantes est rejoué aux ticks (entrée
2 s après la clôture, ticks de moins de 10 s, égalité = perte, flux ≥ 84).
Chaque état est jugé seul (2 044 cases), puis deux scores les combinent : un
score bayésien sur 9 familles et une régression logistique sur 18 variables.
Octobre choisit, novembre juge, Benjamini-Hochberg corrige ; une case
validée devra encore se confirmer sur des jours futurs.

Deux passes, pour tenir dans la mémoire du serveur : la première enregistre
chaque décision jouable en colonnes compactes (variables, couleurs, issues) ;
la seconde calcule les états une fois les bornes de l'exploration connues,
compte les cases dans un tableau plat (case × bloc paire-jour × issue), puis
ajuste et juge les scores.

Elle n'importe ni la course, ni l'exécution des ordres, ni la collecte.
Sorties en CSV seulement, aucune écriture en base. Rien n'est ventilé par
heure : la confirmation EURJPY 22 h reste aveugle.
"""

from __future__ import annotations

import html
import json
import logging
import math
import time
from array import array
from bisect import bisect_left, bisect_right, insort
from collections import Counter
from typing import Any, Callable, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core import memoire
from maxprofit.live import engulfing as en
from maxprofit.live import niveaux as nv
from maxprofit.live.selection3 import PAYOUT_AVANT, PAYOUTS_DU_JOUR

log = logging.getLogger(__name__)

PARAMETRES: dict[str, Any] = json.loads(r"""
{
  "objet": "Recherche automatique de motifs sur les paires OTC : à la clôture de CHAQUE bougie M1 de chaque paire, l'état récent du prix est décrit par 22 familles de motifs objectifs (511 états), et le mouvement des 1, 2, 3 et 5 minutes suivantes est rejoué aux ticks. Un motif, seul ou combiné dans un score, annonce-t-il la direction assez souvent pour battre le point mort de 52,0833 % hors échantillon ?",
  "demande": "27/11 : « C'est pas à moi de te décrire quoi que ce soit c'est ton boulot de voir ces trucs là »",
  "donnees": {
    "bougies": "candles, tf_sec = 60, complete = 1, lues sur la relève ; un jour dont le résumé diffère de l'active est lu sur l'active",
    "ticks": "tick_paths, mêmes règles de copie",
    "payouts": "payouts (points de changement), mêmes règles de copie",
    "paires": "toutes les paires qui ont des bougies M1 dans la période",
    "debut_utc": "2026-10-06T00:00:00Z",
    "fin_exploration_utc": "2026-11-03T00:00:00Z",
    "fin_validation": "début du jour UTC du lancement : seuls des jours complets sont lus"
  },
  "decisions": "toutes les bougies M1 i dont les 240 bougies finissant en i (i comprise) se suivent sans trou ; décision à la clôture de i. Il n'y a pas d'épisodes : chaque minute est une décision, et la dépendance entre décisions voisines est prise en compte par le bootstrap par blocs paire × jour",
  "execution": {
    "entree": "dernier tick connu 2 s après la clôture de i, daté de moins de 10 s",
    "sortie": "dernier tick connu à l'entrée + échéance, daté de moins de 10 s ; toutes les minutes de l'entrée à la sortie ont leur chemin de ticks",
    "echeances_min": [1, 2, 3, 5],
    "issue": "hausse si la sortie dépasse strictement l'entrée, baisse si elle est strictement en dessous, égalité sinon ; une égalité est une perte pour les deux sens",
    "jouable": "dernier payout du flux relevé au plus tard à l'entrée ≥ 84"
  },
  "mesures": {
    "echelle": "m = étendue médiane (plus haut − plus bas) des 50 bougies finissant en i ; une décision n'est mesurée que si m, les étendues médianes des 30 et des 240 bougies finissant en i, l'étendue de la bougie i et les étendues des fenêtres de position sont strictement positives",
    "couleur": "V si clôture > ouverture, R si clôture < ouverture, D sinon",
    "variables": {
      "rendement_N": "(clôture de i − clôture de i−N) / m, pour N ∈ {1, 3, 5, 15, 30, 60}",
      "position_N": "(clôture de i − plus bas) / (plus haut − plus bas) des N bougies finissant en i, pour N ∈ {20, 60, 240}",
      "volatilite": "ln(étendue médiane des 30 bougies finissant en i / étendue médiane des 240)",
      "corps_k": "(clôture − ouverture) / m de la bougie i−k, pour k ∈ {0, 1, 2}",
      "meche_haute": "(plus haut − max(ouverture, clôture)) / (plus haut − plus bas) de la bougie i",
      "meche_basse": "(min(ouverture, clôture) − plus bas) / (plus haut − plus bas) de la bougie i",
      "serie": "longueur de la suite de bougies de la couleur de i qui finit en i, plafonnée à 8, positive si V, négative si R, 0 si D",
      "depuis_haut": "minutes entre i et la bougie la plus récente qui porte le plus haut des 60 bougies finissant en i",
      "depuis_bas": "minutes entre i et la bougie la plus récente qui porte le plus bas des 60 bougies finissant en i"
    }
  },
  "familles": {
    "couleurs_k": "les couleurs des k dernières bougies, de la plus ancienne à i, pour k = 1 à 6, sans D : 2 + 4 + 8 + 16 + 32 + 64 = 126 états",
    "couleur_taille_k": "pour k = 1 à 3, chaque bougie codée par sa couleur (V, R ; sans D) et la taille de son corps |corps_k| en tiers (petit, moyen, grand ; bornes = quantiles 1/3 et 2/3 de |corps_0| en exploration) : 6 + 36 + 216 = 258 états",
    "meches": "couleur de i (sans D) × classe de meche_haute × classe de meche_basse, classes < 0,2, de 0,2 à < 0,5, ≥ 0,5 : 18 états",
    "rendement_N": "les déciles de rendement_N (bornes = quantiles k/10 de l'exploration, k = 1 à 9), pour chaque N : 6 × 10 = 60 états",
    "position_N": "les quintiles fixes de position_N ([0 ; 0,2[, …, [0,8 ; 1]), pour chaque N : 3 × 5 = 15 états",
    "volatilite": "tiers de volatilite (bornes de l'exploration) × couleur de i (sans D) : 6 états",
    "extreme_60": "le côté de l'extrême le plus récent des 60 bougies (haut si depuis_haut ≤ depuis_bas, sinon bas) × son âge min(depuis_haut, depuis_bas) en classes {0}, {1-2}, {3-5}, {6-15}, {16-30}, {31-59} : 12 états",
    "serie": "couleur de i (sans D) × longueur de la série (1 à 7, et 8 ou plus) : 16 états",
    "total": "22 familles, 511 états ; × 4 échéances = 2 044 cases"
  },
  "bornes": "le quantile q d'une liste de n valeurs est la valeur de rang ⌊q × n⌋ (rangs comptés à partir de 0) de la liste triée, plafonné au rang n − 1 ; les bornes se calculent sur les décisions jouables et mesurées de l'exploration. Un état est l'indice de la classe : nombre de bornes inférieures ou égales à la valeur ; des bornes égales laissent des états vides, qui ne sont pas jugés",
  "cases": {
    "sens": "chaque case (famille × état × échéance) choisit en exploration entre hausse et baisse (baisse si elle gagne strictement plus souvent), et garde ce choix en validation",
    "retenue": "si son sens choisi atteint 52,0833 % sur au moins 300 décisions jouables en exploration",
    "intervalles": "IC de Wilson en exploration (descriptif) ; bootstrap par blocs en validation"
  },
  "scores": {
    "score_bayesien": "par échéance, un score additif sur 9 familles fixées (couleurs_3, couleur_taille_2, meches, rendement_5, rendement_30, position_60, volatilite, extreme_60, serie) : ln((H + 1) / (B + 1)) global, plus, pour chaque famille, ln((h + 1) / (b + 1)) − ln((H + 1) / (B + 1)) de l'état de la décision, h et b comptant les hausses et les baisses de cet état en exploration (H et B toutes décisions) ; une famille sans état défini ajoute 0. Sens prédit : hausse si le score est positif, baisse s'il est négatif ; un score nul n'est pas joué",
    "score_logistique": "par échéance, une régression logistique (Newton, pénalité L2 λ = 1, interception non pénalisée : etude_micro.Logistique) sur les 18 variables, bornées à leurs quantiles 1 % et 99 % de l'exploration puis centrées et réduites (écart-type de population ; une variable d'écart-type nul vaut 0), ajustée sur les décisions jouables, mesurées et décisives de l'exploration dont la minute de décision est un multiple de 3 (ts // 60 ≡ 0 mod 3, pour tenir dans la mémoire) ; probabilité de la hausse ; sens prédit : hausse si elle dépasse 0,5, baisse sinon",
    "confiance": "|score| pour le bayésien, |probabilité − 0,5| pour le logistique ; seules sont jouées les décisions dont la confiance atteint le quantile 2/3 des confiances des décisions jouables et mesurées de l'exploration",
    "cases": "2 scores × 4 échéances = 8 cases, jugées directement en validation"
  },
  "selection": {
    "exploration": "jours du 06/10 au 02/11 : bornes, sens des cases, poids et ajustement des scores, seuils de confiance",
    "validation": "jours du 03/11 à la veille du lancement : chaque case retenue et chaque case de score est jugée dans son sens contre 52,0833 %, avec au moins 100 décisions ; p unilatérale du bootstrap par blocs paire × jour (1 000 tirages, graine 7) ; Benjamini-Hochberg à 10 % sur l'ensemble des cases jugées en validation",
    "verdict": "validée si BH la retient et que son taux dépasse 52,0833 % ; sinon non reproduite (ou non validé pour un score)",
    "confirmation": "une case validée n'est pas une stratégie : elle est gelée, puis confirmée sur des jours postérieurs au lancement, avant tout essai en démo"
  },
  "rapport": "aucune ventilation par heure ni par paire × heure (la confirmation EURJPY 22 h reste aveugle) ; par famille, le nombre d'états retenus et validés et le meilleur état d'exploration avec son taux de validation ; les scores ; le détail par paire et par sens des cases retenues",
  "interdits": [
    "lecture seule : aucune écriture en base, sortie en CSV seulement",
    "aucun appel à l'exécution des ordres, aucun changement de la course, de la collecte, de ZoneH1, des échéances ni des configurations",
    "aucune famille, aucun état, aucune variable ni aucun réglage ne s'ajoute ou ne se rechoisit après le gel",
    "aucun argent réel sur une case validée avant sa confirmation, et pas même après sans un essai en démo"
  ]
}
""")
EMPREINTE = "028f63700036a68c96cd97a73d7438ed6439cef67a21235790a1740e7e7af6e3"

DEBUT = en._instant(PARAMETRES["donnees"]["debut_utc"])
FIN_EXPLORATION = en._instant(PARAMETRES["donnees"]["fin_exploration_utc"])
ECHEANCES: tuple[int, ...] = tuple(PARAMETRES["execution"]["echeances_min"])
POINT_MORT = en.POINT_MORT
EXPLORATION, VALIDATION = en.EXPLORATION, en.VALIDATION
DELAI_MS = 2_000
FRAICHEUR_MS = 10_000
N_EXPLO_MIN = 300
N_VALID_MIN = 100
JOURS_ESSAI = 2
RESPIRATION_SEC = 0.02
LAMBDA = 1.0

N_FENETRE = 240
N_M = 50
N_COURT = 30
NS_RENDEMENT: tuple[int, ...] = (1, 3, 5, 15, 30, 60)
NS_POSITION: tuple[int, ...] = (20, 60, 240)
N_EXTREME = 60
SEUILS_MECHE = (0.2, 0.5)
AGES_EXTREME = (0, 2, 5, 15, 30)       # classes {0} {1-2} {3-5} {6-15} {16-30} {31-59}
SERIE_MAX = 8
ABSENTE = -9

VARIABLES: tuple[str, ...] = (
    tuple(f"rendement_{n}" for n in NS_RENDEMENT)
    + tuple(f"position_{n}" for n in NS_POSITION)
    + ("volatilite", "corps_0", "corps_1", "corps_2", "meche_haute",
       "meche_basse", "serie", "depuis_haut", "depuis_bas"))
IDX = {nom: k for k, nom in enumerate(VARIABLES)}

#: Les 22 familles et leur nombre d'états, dans l'ordre des rapports.
FAMILLES: tuple[str, ...] = (
    tuple(f"couleurs_{k}" for k in range(1, 7))
    + tuple(f"couleur_taille_{k}" for k in range(1, 4))
    + ("meches",)
    + tuple(f"rendement_{n}" for n in NS_RENDEMENT)
    + tuple(f"position_{n}" for n in NS_POSITION)
    + ("volatilite", "extreme_60", "serie"))
N_ETATS: dict[str, int] = {}
for _k in range(1, 7):
    N_ETATS[f"couleurs_{_k}"] = 2 ** _k
for _k in range(1, 4):
    N_ETATS[f"couleur_taille_{_k}"] = 6 ** _k
N_ETATS["meches"] = 18
for _n in NS_RENDEMENT:
    N_ETATS[f"rendement_{_n}"] = 10
for _n in NS_POSITION:
    N_ETATS[f"position_{_n}"] = 5
N_ETATS.update(volatilite=6, extreme_60=12, serie=16)
DECALAGES: dict[str, int] = {}
_total = 0
for _f in FAMILLES:
    DECALAGES[_f] = _total
    _total += N_ETATS[_f]
N_ETATS_TOTAL = _total

FAMILLES_BAYES: tuple[str, ...] = (
    "couleurs_3", "couleur_taille_2", "meches", "rendement_5",
    "rendement_30", "position_60", "volatilite", "extreme_60", "serie")
SCORES: tuple[str, ...] = ("score_bayesien", "score_logistique")
LIBELLES_SCORES = {"score_bayesien": "Score bayésien",
                   "score_logistique": "Score logistique"}

GROUPES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("couleurs", "Couleurs des 1 à 6 dernières bougies",
     tuple(f"couleurs_{k}" for k in range(1, 7))),
    ("couleur_taille", "Couleur et taille des 1 à 3 dernières",
     tuple(f"couleur_taille_{k}" for k in range(1, 4))),
    ("meches", "Mèches de la dernière bougie", ("meches",)),
    ("rendement", "Rendement sur 1 à 60 minutes",
     tuple(f"rendement_{n}" for n in NS_RENDEMENT)),
    ("position", "Position dans l'étendue des 20, 60 ou 240 dernières minutes",
     tuple(f"position_{n}" for n in NS_POSITION)),
    ("volatilite", "Volatilité × couleur", ("volatilite",)),
    ("extreme", "Dernier extrême de l'heure", ("extreme_60",)),
    ("serie", "Série en cours", ("serie",)),
)

HAUSSE, BAISSE = "hausse", "baisse"

#: Statuts des décisions.
FENETRE = "fenêtre de 240 minutes incomplète"
NON_JOUABLE = "non jouable"
TICKS_ENTREE = "pas de tick frais à l'entrée"
ETENDUE_NULLE = "étendue nulle"
MESUREE = "mesurée"
SANS_TICKS = "sans ticks"


# --------------------------------------------------------------------------- #
# Les quantiles et les classes
# --------------------------------------------------------------------------- #

def quantile(triees: Sequence[float], num: int, den: int) -> float:
    """La valeur de rang ⌊num/den × n⌋ de la liste triée, plafonnée au rang
    n − 1 (calcul entier)."""
    if not triees:
        raise ValueError("quantile d'une liste vide")
    return triees[min(len(triees) - 1, len(triees) * num // den)]


def classe(valeur: float, bornes: Sequence[float]) -> int:
    """Le nombre de bornes inférieures ou égales à la valeur."""
    return bisect_right(bornes, valeur)


# --------------------------------------------------------------------------- #
# Les mesures d'une minute
# --------------------------------------------------------------------------- #

def couleur_de(o: float, c: float) -> int:
    """1 V (verte), 2 R (rouge), 0 D (sans corps)."""
    return 1 if c > o else (2 if c < o else 0)


class Fenetres:
    """Les étendues des 240, 50 et 30 dernières bougies, triées au fil de
    l'eau pour des médianes rapides ; remises à zéro sur un trou."""

    def __init__(self) -> None:
        self.dernieres: list[float] = []
        self.triees = {n: [] for n in (N_FENETRE, N_M, N_COURT)}

    def vider(self) -> None:
        self.dernieres.clear()
        for t in self.triees.values():
            t.clear()

    def ajouter(self, etendue: float) -> None:
        self.dernieres.append(etendue)
        for n, triee in self.triees.items():
            insort(triee, etendue)
            if len(triee) > n:
                sortante = self.dernieres[-n - 1]
                del triee[bisect_left(triee, sortante)]
        if len(self.dernieres) > N_FENETRE:
            del self.dernieres[0]

    def mediane(self, n: int) -> float:
        triee = self.triees[n]
        k = len(triee)
        return (triee[k // 2] if k % 2 else
                (triee[k // 2 - 1] + triee[k // 2]) / 2)


def mesurer(s1: nv.SerieU, i: int, medianes: tuple[float, float, float]
            ) -> tuple[list[float], int] | None:
    """(les 18 variables dans l'ordre de VARIABLES, code des couleurs des
    bougies i à i−5) à la clôture de i, ou None si une étendue est nulle.
    `medianes` : étendues médianes des 50, 30 et 240 bougies finissant en i.
    Rien n'est lu au-delà de la bougie i."""
    o, h, l, c = s1.o, s1.h, s1.l, s1.c
    m, m_court, m_long = medianes
    if m <= 0 or m_court <= 0 or m_long <= 0 or h[i] <= l[i]:
        return None
    variables = [(c[i] - c[i - n]) / m for n in NS_RENDEMENT]
    for n in NS_POSITION:
        haut, bas = max(h[i - n + 1:i + 1]), min(l[i - n + 1:i + 1])
        if haut <= bas:
            return None
        variables.append((c[i] - bas) / (haut - bas))
    variables.append(math.log(m_court / m_long))
    variables += [(c[i - k] - o[i - k]) / m for k in range(3)]
    etendue = h[i] - l[i]
    variables.append((h[i] - max(o[i], c[i])) / etendue)
    variables.append((min(o[i], c[i]) - l[i]) / etendue)
    teinte = couleur_de(o[i], c[i])
    serie = 0
    if teinte:
        k = i
        while (k > i - SERIE_MAX and couleur_de(o[k], c[k]) == teinte):
            serie += 1
            k -= 1
        if teinte == 2:
            serie = -serie
    variables.append(float(serie))
    debut = i - N_EXTREME + 1
    haut, bas = max(h[debut:i + 1]), min(l[debut:i + 1])
    depuis_haut = next(i - k for k in range(i, debut - 1, -1) if h[k] == haut)
    depuis_bas = next(i - k for k in range(i, debut - 1, -1) if l[k] == bas)
    variables += [float(depuis_haut), float(depuis_bas)]
    code = 0
    for j in range(6):
        code += couleur_de(o[i - j], c[i - j]) * 3 ** j
    return variables, code


# --------------------------------------------------------------------------- #
# Les décisions jouables, en colonnes compactes
# --------------------------------------------------------------------------- #

class Decisions:
    """Une ligne par décision jouable et mesurée : 18 variables (float32),
    le code des couleurs, les issues (+1 hausse, −1 baisse, 0 égalité,
    ABSENTE), le bloc paire × jour, la période et l'échantillon du score
    logistique."""

    def __init__(self, paires: Sequence[str], debut: int, fin: int) -> None:
        self.paires = list(paires)
        self.jour0 = debut // 86400
        self.n_jours = max(1, (fin - debut) // 86400)
        self.jour_validation = FIN_EXPLORATION // 86400
        self.variables = [array("f") for _ in VARIABLES]
        self.couleurs = array("h")
        self.issues = {t: array("b") for t in ECHEANCES}
        self.bloc = array("h")
        self.validation = array("b")
        self.echantillon = array("b")
        self.statuts: Counter = Counter()

    @property
    def n_blocs(self) -> int:
        return len(self.paires) * self.n_jours

    def __len__(self) -> int:
        return len(self.bloc)

    def bloc_de(self, paire: str, ts_sec: int) -> int:
        jour = min(self.n_jours - 1, max(0, ts_sec // 86400 - self.jour0))
        return self.paires.index(paire) * self.n_jours + jour

    def paire_du_bloc(self, bloc: int) -> str:
        return self.paires[bloc // self.n_jours]

    def bloc_en_validation(self, bloc: int) -> bool:
        return self.jour0 + bloc % self.n_jours >= self.jour_validation

    def ajouter(self, variables, code: int, issues: Mapping[int, int],
                bloc: int, validation: bool, echantillon: bool) -> None:
        for k, v in enumerate(variables):
            self.variables[k].append(v)
        self.couleurs.append(code)
        for t in ECHEANCES:
            self.issues[t].append(issues.get(t, ABSENTE))
        self.bloc.append(bloc)
        self.validation.append(validation)
        self.echantillon.append(echantillon)

    def indices(self, periode: str) -> list[int]:
        voulu = periode == VALIDATION
        return [r for r in range(len(self)) if bool(self.validation[r]) == voulu]


def paire(nom: str, bougies: Sequence[en.Bougie], fin: int,
          ticks: en.Ticks | None, payout_a: Callable[[float], float | None],
          base: Decisions, respirer: Callable[[], None]) -> int:
    """Ajoute à `base` les décisions jouables et mesurées d'une paire ;
    rend leur nombre."""
    s1 = nv.SerieU(bougies, 60)
    ts, h, l = s1.ts, s1.h, s1.l
    fen = Fenetres()
    ajoutees = 0
    limite_ms = fin * 1000
    for i in range(len(s1)):
        if i and ts[i] != ts[i - 1] + 60:
            fen.vider()
        fen.ajouter(h[i] - l[i])
        if i % 2000 == 1999:
            respirer()
        if not s1.continue_(i, N_FENETRE):
            continue
        decision_ms = (ts[i] + 60) * 1000
        entree_ms = decision_ms + DELAI_MS
        validation = entree_ms >= FIN_EXPLORATION * 1000
        per = VALIDATION if validation else EXPLORATION
        payout = payout_a(entree_ms / 1000)
        if payout is None or payout < en.FLUX_MIN:
            base.statuts[("décisions", per, NON_JOUABLE)] += 1
            continue
        if ticks is None:
            base.statuts[("décisions", per, SANS_TICKS)] += 1
            continue
        if i % 200 == 0:
            ticks.oublier_avant(ts[i])
        entree = ticks.prix_a(entree_ms)
        if entree is None or entree_ms - entree[0] > FRAICHEUR_MS:
            base.statuts[("décisions", per, TICKS_ENTREE)] += 1
            continue
        mesure = mesurer(s1, i, (fen.mediane(N_M), fen.mediane(N_COURT),
                                 fen.mediane(N_FENETRE)))
        if mesure is None:
            base.statuts[("décisions", per, ETENDUE_NULLE)] += 1
            continue
        issues = {}
        for t in ECHEANCES:
            sortie_ms = entree_ms + 60_000 * t
            if sortie_ms > limite_ms:
                statut = "fin des données"
            elif not ticks.continu(entree_ms, sortie_ms):
                statut = "ticks manquants"
            else:
                sortie = ticks.prix_a(sortie_ms)
                if sortie is None or sortie_ms - sortie[0] > FRAICHEUR_MS:
                    statut = "flux gelé à la sortie"
                else:
                    ecart = sortie[1] - entree[1]
                    issues[t] = 1 if ecart > 0 else (-1 if ecart < 0 else 0)
                    statut = "issue"
            base.statuts[("ticks", per, statut, t)] += 1
        base.statuts[("décisions", per, MESUREE)] += 1
        # Le bloc est le jour de l'ENTRÉE, comme la période : une décision
        # de 23 h 59 le 02/11 entre le 03/11, en validation.
        base.ajouter(mesure[0], mesure[1], issues,
                     base.bloc_de(nom, entree_ms // 1000), validation,
                     ts[i] // 60 % 3 == 0)
        ajoutees += 1
    return ajoutees


# --------------------------------------------------------------------------- #
# Les états des familles
# --------------------------------------------------------------------------- #

def bornes_de(base: Decisions, explo: Sequence[int]) -> dict[str, list[float]]:
    """Les bornes de l'exploration : tiers de |corps_0|, déciles des
    rendements, tiers de la volatilité."""
    def valeurs(nom, absolue=False):
        col = base.variables[IDX[nom]]
        return sorted(abs(col[r]) if absolue else col[r] for r in explo)
    sortie: dict[str, list[float]] = {}
    v = valeurs("corps_0", True)
    sortie["taille"] = [quantile(v, 1, 3), quantile(v, 2, 3)] if v else []
    for n in NS_RENDEMENT:
        v = valeurs(f"rendement_{n}")
        sortie[f"rendement_{n}"] = ([quantile(v, k, 10) for k in range(1, 10)]
                                    if v else [])
    v = valeurs("volatilite")
    sortie["volatilite"] = [quantile(v, 1, 3), quantile(v, 2, 3)] if v else []
    return sortie


def _couleur(code: int, j: int) -> int:
    return (code // 3 ** j) % 3


def etat(famille: str, base: Decisions, r: int, bornes) -> int:
    """L'état de la décision r dans la famille, ou −1 s'il n'est pas défini
    (une bougie sans corps, par exemple)."""
    code = base.couleurs[r]
    var = base.variables
    if famille.startswith("couleurs_"):
        k = int(famille[-1])
        indice = 0
        for j in range(k - 1, -1, -1):          # de la plus ancienne à i
            x = _couleur(code, j)
            if x == 0:
                return -1
            indice = indice * 2 + (1 if x == 1 else 0)
        return indice
    if famille.startswith("couleur_taille_"):
        k = int(famille[-1])
        indice = 0
        for j in range(k - 1, -1, -1):
            x = _couleur(code, j)
            if x == 0:
                return -1
            taille = classe(abs(var[IDX[f"corps_{j}"]][r]), bornes["taille"])
            indice = indice * 6 + (0 if x == 1 else 3) + taille
        return indice
    if famille == "meches":
        x = _couleur(code, 0)
        if x == 0:
            return -1
        return ((0 if x == 1 else 9)
                + 3 * classe(var[IDX["meche_haute"]][r], SEUILS_MECHE)
                + classe(var[IDX["meche_basse"]][r], SEUILS_MECHE))
    if famille.startswith("rendement_"):
        return classe(var[IDX[famille]][r], bornes[famille])
    if famille.startswith("position_"):
        return min(4, int(5 * var[IDX[famille]][r]))
    if famille == "volatilite":
        x = _couleur(code, 0)
        if x == 0:
            return -1
        return (0 if x == 1 else 3) + classe(var[IDX["volatilite"]][r],
                                             bornes["volatilite"])
    if famille == "extreme_60":
        dh, db = var[IDX["depuis_haut"]][r], var[IDX["depuis_bas"]][r]
        cote = 0 if dh <= db else 6
        return cote + bisect_left(AGES_EXTREME, min(dh, db))
    if famille == "serie":
        s = int(var[IDX["serie"]][r])
        if s == 0:
            return -1
        return (0 if s > 0 else SERIE_MAX) + min(abs(s), SERIE_MAX) - 1
    raise ValueError(f"famille inconnue : {famille}")


def libelle_etat(famille: str, indice: int, bornes) -> str:
    """Un nom lisible de l'état."""
    if famille.startswith("couleurs_"):
        k = int(famille[-1])
        return "".join("V" if (indice >> (k - 1 - p)) & 1 else "R"
                       for p in range(k))
    if famille.startswith("couleur_taille_"):
        k = int(famille[-1])
        morceaux = []
        for p in range(k):
            symbole = (indice // 6 ** (k - 1 - p)) % 6
            morceaux.append(("V" if symbole < 3 else "R")
                            + "-o+"[symbole % 3])
        return " ".join(morceaux)
    if famille == "meches":
        noms = ("<0,2", "0,2-0,5", "≥0,5")
        return (("V" if indice < 9 else "R") + f" haute {noms[indice % 9 // 3]}"
                f" basse {noms[indice % 3]}")
    if famille.startswith("rendement_"):
        return f"décile {indice + 1}"
    if famille.startswith("position_"):
        return f"quintile {indice + 1}"
    if famille == "volatilite":
        return ("V" if indice < 3 else "R") + " " + ("calme", "moyenne",
                                                      "agitée")[indice % 3]
    if famille == "extreme_60":
        ages = ("0", "1-2", "3-5", "6-15", "16-30", "31-59")
        return (("haut" if indice < 6 else "bas")
                + f" il y a {ages[indice % 6]} min")
    if famille == "serie":
        n = indice % SERIE_MAX + 1
        return (("V" if indice < SERIE_MAX else "R") + " × "
                + (f"{n}" if n < SERIE_MAX else f"{SERIE_MAX}+"))
    raise ValueError(famille)


def etats_de(base: Decisions, bornes, respirer: Callable[[], None]
             ) -> dict[str, array]:
    sortie = {}
    for famille in FAMILLES:
        col = array("h", (etat(famille, base, r, bornes)
                          for r in range(len(base))))
        sortie[famille] = col
        respirer()
    return sortie


# --------------------------------------------------------------------------- #
# Les comptes, en tableau plat
# --------------------------------------------------------------------------- #

class Comptes:
    """[case × bloc × issue] dans un seul tableau d'entiers : une case est
    (famille, état, échéance), un bloc une paire × un jour, l'issue hausse,
    baisse ou égalité."""

    def __init__(self, n_blocs: int) -> None:
        self.n_blocs = n_blocs
        self.n_cases = N_ETATS_TOTAL * len(ECHEANCES)
        self.t = array("i", bytes(4 * self.n_cases * n_blocs * 3))

    def case(self, famille: str, indice: int, t: int) -> int:
        return (DECALAGES[famille] + indice) * len(ECHEANCES) + \
            ECHEANCES.index(t)

    def remplir(self, base: Decisions, etats: Mapping[str, array],
                respirer: Callable[[], None]) -> None:
        tab, nb = self.t, self.n_blocs
        ne = len(ECHEANCES)
        for famille in FAMILLES:
            col = etats[famille]
            dec = DECALAGES[famille]
            for ti, t in enumerate(ECHEANCES):
                iss = base.issues[t]
                for r in range(len(base)):
                    s = col[r]
                    o = iss[r]
                    if s < 0 or o == ABSENTE:
                        continue
                    k = (((dec + s) * ne + ti) * nb + base.bloc[r]) * 3
                    tab[k + (0 if o > 0 else (1 if o < 0 else 2))] += 1
            respirer()

    def par_bloc(self, case: int, blocs: Sequence[int]
                 ) -> dict[int, tuple[int, int, int]]:
        sortie = {}
        base_ = case * self.n_blocs * 3
        for b in blocs:
            k = base_ + b * 3
            x = (self.t[k], self.t[k + 1], self.t[k + 2])
            if x[0] or x[1] or x[2]:
                sortie[b] = x
        return sortie

    def total(self, case: int, blocs: Sequence[int]) -> tuple[int, int, int]:
        u = d = e = 0
        base_ = case * self.n_blocs * 3
        t = self.t
        for b in blocs:
            k = base_ + b * 3
            u += t[k]
            d += t[k + 1]
            e += t[k + 2]
        return u, d, e


def _bootstrap(par_bloc: Mapping[int, tuple[int, int, int]], choisi: int,
               tirage: en.Tirages, taux: float):
    gb = {b: float(x[choisi]) for b, x in par_bloc.items()}
    nb = {b: float(sum(x)) for b, x in par_bloc.items()}
    return tirage.ratio(gb, nb, taux)


def cases_des_familles(comptes: Comptes, base: Decisions, bornes,
                       tirages: Mapping[str, en.Tirages], blocs
                       ) -> list[dict[str, Any]]:
    """Deux lignes par case (exploration, validation) ; le bootstrap n'est
    calculé que pour les cases retenues, jugées en validation."""
    lignes = []
    for famille in FAMILLES:
        for s in range(N_ETATS[famille]):
            nom = libelle_etat(famille, s, bornes)
            for t in ECHEANCES:
                case = comptes.case(famille, s, t)
                u, d, e = comptes.total(case, blocs[EXPLORATION])
                choisi = 1 if d > u else 0
                n_x = u + d + e
                taux_x = (d if choisi else u) / n_x if n_x else None
                retenue = (n_x >= N_EXPLO_MIN and taux_x is not None
                           and taux_x >= POINT_MORT)
                bas, haut = en.wilson(d if choisi else u, n_x)
                lignes.append(_ligne("famille", famille, nom, t, EXPLORATION,
                                     (u, d, e), choisi, taux_x, bas, haut,
                                     None))
                uv, dv, ev = comptes.total(case, blocs[VALIDATION])
                n_v = uv + dv + ev
                taux_v = (dv if choisi else uv) / n_v if n_v else None
                p = None
                if retenue and n_v >= N_VALID_MIN:
                    bas, haut, p = _bootstrap(
                        comptes.par_bloc(case, blocs[VALIDATION]), choisi,
                        tirages[VALIDATION], taux_v)
                else:
                    bas, haut = en.wilson(dv if choisi else uv, n_v)
                lignes.append(_ligne("famille", famille, nom, t, VALIDATION,
                                     (uv, dv, ev), choisi, taux_v, bas, haut,
                                     p))
    return lignes


def _ligne(type_, famille, etat_, t, per, ude, choisi, taux, bas, haut, p):
    u, d, e = ude
    n = u + d + e
    return {"type": type_, "famille": famille, "etat": etat_,
            "echeance_min": t, "periode": per, "n": n, "hausses": u,
            "baisses": d, "egalites": e,
            "taux_hausse": u / n if n else None,
            "sens_choisi": BAISSE if choisi else HAUSSE, "taux": taux,
            "ic_bas": bas, "ic_haut": haut, "p": p, "verdict": ""}


# --------------------------------------------------------------------------- #
# Les deux scores
# --------------------------------------------------------------------------- #

def poids_bayesiens(comptes: Comptes, blocs_explo, t: int):
    """(constante, {famille: [poids par état]}) à l'échéance t. H et B
    comptent toutes les décisions : on les lit dans rendement_1, la famille
    définie pour chacune (couleurs_1 ignore les bougies sans corps)."""
    H = B = 0
    for s in range(N_ETATS["rendement_1"]):
        u, d, _ = comptes.total(comptes.case("rendement_1", s, t),
                                blocs_explo)
        H += u
        B += d
    constante = math.log((H + 1) / (B + 1))
    poids = {}
    for famille in FAMILLES_BAYES:
        poids[famille] = []
        for s in range(N_ETATS[famille]):
            u, d, _ = comptes.total(comptes.case(famille, s, t), blocs_explo)
            poids[famille].append(math.log((u + 1) / (d + 1)) - constante)
    return constante, poids


def preparation(base: Decisions, explo: Sequence[int]):
    """Par variable : (borne 1 %, borne 99 %, moyenne, écart-type) de
    l'exploration, après bornage."""
    sortie = []
    for k in range(len(VARIABLES)):
        col = base.variables[k]
        v = sorted(col[r] for r in explo)
        if not v:
            sortie.append((0.0, 0.0, 0.0, 0.0))
            continue
        bas, haut = quantile(v, 1, 100), quantile(v, 99, 100)
        bornees = [min(max(x, bas), haut) for x in v]
        moy = sum(bornees) / len(bornees)
        et = math.sqrt(sum((x - moy) ** 2 for x in bornees) / len(bornees))
        sortie.append((bas, haut, moy, et))
    return sortie


def z_de(base: Decisions, r: int, prep) -> list[float]:
    z = []
    for k, (bas, haut, moy, et) in enumerate(prep):
        v = min(max(base.variables[k][r], bas), haut)
        z.append((v - moy) / et if et > 0 else 0.0)
    return z


def _proba(coefs: Sequence[float], z: Sequence[float]) -> float:
    s = coefs[0] + sum(c * v for c, v in zip(coefs[1:], z))
    if s >= 0:
        return 1 / (1 + math.exp(-s))
    e = math.exp(s)
    return e / (1 + e)


class Scores:
    """Les deux scores aux quatre échéances : poids ou modèle, seuil de
    confiance, et ce qu'ils jouent."""

    def __init__(self) -> None:
        self.bayes: dict[int, tuple[float, dict]] = {}
        self.logit: dict[int, Any] = {}
        self.n_ajustement: dict[int, int] = {}
        self.prep = None
        self.seuils: dict[tuple[str, int], float] = {}

    def score_bayes(self, t: int, etats, r: int) -> float:
        constante, poids = self.bayes[t]
        total = constante
        for famille in FAMILLES_BAYES:
            s = etats[famille][r]
            if s >= 0:
                total += poids[famille][s]
        return total


def ajuster_les_scores(base: Decisions, etats, comptes: Comptes, blocs,
                       explo: Sequence[int],
                       respirer: Callable[[], None]) -> Scores:
    sc = Scores()
    for t in ECHEANCES:
        sc.bayes[t] = poids_bayesiens(comptes, blocs[EXPLORATION], t)
        confiances = sorted(abs(sc.score_bayes(t, etats, r)) for r in explo)
        if confiances:
            sc.seuils[("score_bayesien", t)] = quantile(confiances, 2, 3)
        respirer()
    sc.prep = preparation(base, explo)
    echantillon = [r for r in explo if base.echantillon[r]]
    # Des tableaux compacts plutôt que des listes de flottants : trois fois
    # moins de mémoire pour l'échantillon, qui compte des dizaines de
    # milliers de lignes.
    lignes = [array("d", z_de(base, r, sc.prep)) for r in echantillon]
    for t in ECHEANCES:
        x_t, y_t = [], []
        for z, r in zip(lignes, echantillon):
            issue = base.issues[t][r]
            if issue in (1, -1):
                x_t.append(z)
                y_t.append(1 if issue == 1 else 0)
        if len(set(y_t)) == 2:
            sc.logit[t] = em.Logistique.ajuster(x_t, y_t, lam=LAMBDA)
            sc.n_ajustement[t] = len(y_t)
        del x_t, y_t
        respirer()
    del lignes
    confiances = {t: [] for t in sc.logit}
    for r in explo:
        z = z_de(base, r, sc.prep)
        for t, modele in sc.logit.items():
            confiances[t].append(abs(_proba(modele.coefficients, z) - 0.5))
    for t, valeurs in confiances.items():
        valeurs.sort()
        if valeurs:
            sc.seuils[("score_logistique", t)] = quantile(valeurs, 2, 3)
    respirer()
    return sc


def cases_des_scores(base: Decisions, etats, sc: Scores,
                     tirages: Mapping[str, en.Tirages],
                     respirer: Callable[[], None]):
    """Deux lignes par score et par échéance, et le détail de validation par
    paire et par sens prédit."""
    actifs = [(nom, t) for (nom, t) in sc.seuils]
    comptes = {cle: {EXPLORATION: {}, VALIDATION: {}} for cle in actifs}
    details: dict[tuple, dict] = {cle: {} for cle in actifs}
    for r in range(len(base)):
        z = None
        per = VALIDATION if base.validation[r] else EXPLORATION
        bloc = base.bloc[r]
        for nom, t in actifs:
            issue = base.issues[t][r]
            if issue == ABSENTE:
                continue
            if nom == "score_bayesien":
                score = sc.score_bayes(t, etats, r)
                confiance, cote = abs(score), (1 if score > 0 else -1)
                if score == 0:
                    continue
            else:
                if z is None:
                    z = z_de(base, r, sc.prep)
                p = _proba(sc.logit[t].coefficients, z)
                confiance, cote = abs(p - 0.5), (1 if p > 0.5 else -1)
            if confiance < sc.seuils[(nom, t)]:
                continue
            gagne = issue == cote
            x = comptes[(nom, t)][per].setdefault(bloc, [0, 0])
            x[0 if gagne else 1] += 1
            if per == VALIDATION:
                for cle in (("paire", base.paire_du_bloc(bloc)),
                            ("sens prédit", HAUSSE if cote > 0 else BAISSE)):
                    y = details[(nom, t)].setdefault(cle, [0, 0])
                    y[0] += gagne
                    y[1] += 1
        if r % 20000 == 19999:
            respirer()
    lignes = []
    for nom, t in actifs:
        for per in (EXPLORATION, VALIDATION):
            par_bloc = comptes[(nom, t)][per]
            g = sum(x[0] for x in par_bloc.values())
            n = g + sum(x[1] for x in par_bloc.values())
            taux = g / n if n else None
            p = None
            if per == VALIDATION and n:
                gb = {b: float(x[0]) for b, x in par_bloc.items()}
                nb = {b: float(sum(x)) for b, x in par_bloc.items()}
                bas, haut, p = tirages[VALIDATION].ratio(gb, nb, taux)
            else:
                bas, haut = en.wilson(g, n)
            lignes.append({"type": "score", "famille": nom, "etat": "",
                           "echeance_min": t, "periode": per, "n": n,
                           "hausses": None, "baisses": None,
                           "egalites": None, "taux_hausse": None,
                           "sens_choisi": "prédit", "taux": taux,
                           "ic_bas": bas, "ic_haut": haut, "p": p,
                           "verdict": ""})
    return lignes, details


# --------------------------------------------------------------------------- #
# Les verdicts
# --------------------------------------------------------------------------- #

def verdicts(lignes: list[dict[str, Any]]) -> None:
    """Familles : retenue en exploration (≥ 300, ≥ 52,08 %), jugée en
    validation (≥ 100). Scores : jugés directement en validation (≥ 100).
    Benjamini-Hochberg à 10 % sur toutes les cases jugées en validation."""
    par_cle = {(l["type"], l["famille"], l["etat"], l["echeance_min"],
                l["periode"]): l for l in lignes}
    jugees: dict[str, float] = {}
    cles: dict[str, dict] = {}
    for (type_, fam, etat_, t, per), l in par_cle.items():
        if per != EXPLORATION:
            continue
        val = par_cle[(type_, fam, etat_, t, VALIDATION)]
        if type_ == "famille":
            if l["n"] < N_EXPLO_MIN:
                l["verdict"] = "trop peu en exploration"
                continue
            if (l["taux"] or 0) < POINT_MORT:
                l["verdict"] = "non retenue en exploration"
                continue
            l["verdict"] = "retenue en exploration"
        else:
            l["verdict"] = "ajusté ici (en échantillon)"
        if val["n"] < N_VALID_MIN or val.get("p") is None:
            val["verdict"] = "trop peu en validation"
            if type_ == "famille":
                l["verdict"] = "retenue, trop peu en validation"
            continue
        cle = f"{type_}|{fam}|{etat_}|{t}"
        jugees[cle] = val["p"]
        cles[cle] = val
    decouvertes = em.benjamini_hochberg(jugees) if jugees else {}
    for cle, val in cles.items():
        if decouvertes[cle] and val["taux"] > POINT_MORT:
            val["verdict"] = "validée (BH 10 %)"
        else:
            val["verdict"] = ("non reproduite" if val["type"] == "famille"
                              else "non validé")


def attendues_au_hasard(lignes: Sequence[Mapping[str, Any]]) -> float:
    total = 0.0
    for l in lignes:
        n = l["n"]
        if (l["type"] != "famille" or l["periode"] != EXPLORATION
                or n < N_EXPLO_MIN):
            continue
        z = (POINT_MORT - 0.5) * 2 * math.sqrt(n)
        total += min(1.0, 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))))
    return total


# --------------------------------------------------------------------------- #
# L'avancement
# --------------------------------------------------------------------------- #

_ETAT: dict[str, Any] = {}


def avancement() -> str | None:
    etat_ = dict(_ETAT)
    if not etat_:
        return None
    minutes = (time.monotonic() - etat_["debut"]) / 60
    faites, total = etat_.get("faites", 0), etat_.get("total", 0)
    texte_ = (f"{etat_['etape']} ; {faites}/{total} paires ; "
              f"{etat_.get('signaux', 0)} décisions ; {minutes:.0f} min")
    debut_paires = etat_.get("debut_paires")
    if debut_paires is not None and 0 < faites < total:
        par_paire = (time.monotonic() - debut_paires) / 60 / faites
        texte_ += (f" ; fin des paires dans "
                   f"~{par_paire * (total - faites):.0f} min")
    texte_ += f" ; {memoire.resume()}"
    if etat_.get("rss_max"):
        texte_ += f" ; pic de l'étude {etat_['rss_max']:.0f} Mo"
    return texte_


def _mesurer_memoire() -> None:
    rss = memoire.residente()
    if rss is not None and rss > _ETAT.get("rss_max", 0):
        _ETAT["rss_max"] = rss


# --------------------------------------------------------------------------- #
# La passe en lecture seule
# --------------------------------------------------------------------------- #

def _lire_paire(nom: str, debut: int, fin: int, base_de, lue, compte: Counter,
                avec_ticks: bool, decisions: Decisions,
                respirer: Callable[[], None]) -> tuple[int, en.Ticks | None,
                                                      int]:
    bougies: list[en.Bougie] = []
    changements: list[tuple[int, float]] = []
    ligne = lue.execute(PAYOUT_AVANT, (nom, debut)).fetchone()
    if ligne and ligne[1] is not None:
        changements.append((int(ligne[0]), float(ligne[1])))
    for jour in range(debut // 86400, fin // 86400):
        bougies += en._lire_bougies(base_de("candles", nom, jour), nom, jour,
                                    compte)
        for ts, pct in base_de("payouts", nom, jour).execute(
                PAYOUTS_DU_JOUR, (nom, jour * 86400,
                                  (jour + 1) * 86400)).fetchall():
            if pct is not None:
                changements.append((int(ts), float(pct)))
        respirer()
    instants = [x[0] for x in changements]

    def payout_a(t: float) -> float | None:
        k = bisect_right(instants, t) - 1
        return changements[k][1] if k >= 0 else None

    ticks = None
    if avec_ticks:
        def charger(jour: int):
            if jour * 86400 >= fin:
                return {}
            return {int(r[0]): (int(r[1]), int(r[2]), bytes(r[3]))
                    for r in base_de("tick_paths", nom, jour).execute(
                        en.SQL_CHEMINS, (nom, jour * 86400,
                                         min(fin, (jour + 1) * 86400)))
                    .fetchall()}
        ticks = en.Ticks(charger)
    n_bougies = len(bougies)
    ajoutees = paire(nom, bougies, fin, ticks, payout_a, decisions, respirer)
    return ajoutees, ticks, n_bougies


def etude(lue_brute, active_brute, course=None, *,
          nom_lecture: str = "relève", maintenant: float | None = None,
          avec_ticks: bool = True,
          respirer: Callable[[], None] | None = None,
          essai: bool = False, version: str | None = None
          ) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers). Les deux bases sont déjà en lecture seule ;
    `active_brute` peut être None. `essai` : le contrôle technique, sans
    aucun taux."""
    em.exiger_le_gel(PARAMETRES, EMPREINTE)
    pause = respirer or (lambda: time.sleep(RESPIRATION_SEC))

    def respirer() -> None:
        _mesurer_memoire()
        pause()

    maintenant = time.time() if maintenant is None else maintenant
    t0 = time.monotonic()
    fin = int(maintenant) // 86400 * 86400
    if fin <= FIN_EXPLORATION and not essai:
        return ("🔬 L'exploration court jusqu'au 02/11 : rien à valider "
                "encore."), []
    _ETAT.clear()
    _ETAT.update(debut=t0, etape="résumés des bases", faites=0, total=0,
                 signaux=0, rss_depart=memoire.residente())
    try:
        if essai:
            return _essai(lue_brute, active_brute, nom_lecture, fin,
                          avec_ticks, respirer, t0, version), []
        return _etude(lue_brute, active_brute, nom_lecture, fin, avec_ticks,
                      respirer, t0, version)
    finally:
        _ETAT.clear()


def analyser(base: Decisions, respirer: Callable[[], None], *,
             essai: bool = False) -> dict[str, Any]:
    """Bornes, états, comptes, cases, scores et verdicts. En essai, tout
    s'ajuste sur l'ensemble des décisions, faute d'exploration."""
    explo = list(range(len(base))) if essai else base.indices(EXPLORATION)
    tous = list(range(base.n_blocs))
    blocs = {VALIDATION: [b for b in tous if base.bloc_en_validation(b)]}
    blocs[EXPLORATION] = (tous if essai else
                          [b for b in tous if not base.bloc_en_validation(b)])
    presents = {per: sorted({base.bloc[r] for r in range(len(base))
                             if bool(base.validation[r])
                             == (per == VALIDATION)})
                for per in (EXPLORATION, VALIDATION)}
    tirages = {per: en.Tirages(presents[per]) for per in presents}
    bornes = bornes_de(base, explo)
    etats = etats_de(base, bornes, respirer)
    comptes = Comptes(base.n_blocs)
    comptes.remplir(base, etats, respirer)
    lignes = cases_des_familles(comptes, base, bornes, tirages, blocs)
    respirer()
    scores = ajuster_les_scores(base, etats, comptes, blocs, explo, respirer)
    lignes_scores, details_scores = cases_des_scores(base, etats, scores,
                                                     tirages, respirer)
    lignes += lignes_scores
    verdicts(lignes)
    return {"bornes": bornes, "scores": scores, "lignes": lignes,
            "details_scores": details_scores, "comptes": comptes,
            "blocs": blocs}


def _etude(lue_brute, active_brute, nom_lecture, fin, avec_ticks, respirer,
           t0, version=None):
    lue, active, lecture, resume_lue, divergents, base_de = en._bases(
        lue_brute, active_brute, nom_lecture, DEBUT, fin)
    paires = sorted({p for (table, p, _) in resume_lue if table == "candles"})
    _ETAT.update(total=len(paires), debut_paires=time.monotonic())
    compte: Counter = Counter()
    decisions = Decisions(paires, DEBUT, fin)
    decodees = 0
    for rang, nom in enumerate(paires):
        _ETAT.update(etape=f"paire {nom}", faites=rang)
        _, ticks, _ = _lire_paire(nom, DEBUT, fin, base_de, lue, compte,
                                  avec_ticks, decisions, respirer)
        _ETAT.update(signaux=len(decisions))
        if ticks is not None:
            compte[("données", "chemins illisibles")] += ticks.illisibles
            decodees += ticks.decodees
        del ticks
        respirer()
    _ETAT.update(etape="états, comptes et scores", faites=len(paires))
    resultats = analyser(decisions, respirer)
    _ETAT.update(etape="rapport et fichiers")
    meta = {"lecture": lecture, "fin": fin, "paires": paires,
            "resume": resume_lue, "compte": compte,
            "divergents": len(divergents), "avec_ticks": avec_ticks,
            "decodees": decodees,
            "requetes": sum(lue.requetes.values()) + (
                sum(active.requetes.values()) if active is not None else 0),
            "refusees": lue.refusees + (active.refusees if active else 0),
            "temps": time.monotonic() - t0, "version": version,
            "rss_depart": _ETAT.get("rss_depart"),
            "rss_max": _ETAT.get("rss_max")}
    return (texte(decisions, resultats, meta),
            fichiers(decisions, resultats, resume_lue, compte))


def _essai(lue_brute, active_brute, nom_lecture, fin, avec_ticks, respirer,
           t0, version=None) -> str:
    """Le contrôle technique : la paire la plus fournie en ticks, sur les
    `JOURS_ESSAI` derniers jours complets (et la veille pour les fenêtres),
    par le même code, rapport et fichiers compris. Aucun taux n'est
    montré."""
    debut = max(DEBUT, fin - JOURS_ESSAI * 86400)
    lue, active, lecture, resume_lue, divergents, base_de = en._bases(
        lue_brute, active_brute, nom_lecture, debut, fin)
    t_resumes = time.monotonic() - t0
    min_ticks: Counter = Counter()
    min_bougies: Counter = Counter()
    for (table, p, _), v in resume_lue.items():
        if v and v[0]:
            if table == "tick_paths":
                min_ticks[p] += v[0]
            elif table == "candles":
                min_bougies[p] += v[0]
    entete = ("🔬 <b>Essai technique /motifs</b> : il ne compte pas, et "
              "aucun taux n'y est montré."
              + (f"\nVersion déployée : {html.escape(version)}."
                 if version else ""))
    if not min_bougies:
        return (entete + f"\nAucune bougie entre le {en._utc(debut)} et le "
                f"{en._utc(fin - 86400)} : rien à essayer.")
    nom = max(sorted(min_bougies),
              key=lambda p: (min_ticks[p], min_bougies[p]))
    _ETAT.update(etape=f"essai, paire {nom}", total=1,
                 debut_paires=time.monotonic())
    compte: Counter = Counter()
    lecture_debut = max(DEBUT, debut - 86400)
    decisions = Decisions([nom], lecture_debut, fin)
    t1 = time.monotonic()
    _, ticks, n_bougies = _lire_paire(nom, lecture_debut, fin, base_de, lue,
                                      compte, avec_ticks, decisions, respirer)
    t_paire = time.monotonic() - t1
    _ETAT.update(etape="essai, états, comptes et scores", faites=1,
                 signaux=len(decisions))
    try:
        resultats = analyser(decisions, respirer, essai=True)
        texte(decisions, resultats, {
            "lecture": lecture, "fin": fin, "paires": [nom],
            "resume": resume_lue, "compte": compte,
            "divergents": len(divergents), "avec_ticks": avec_ticks,
            "decodees": ticks.decodees if ticks else 0, "requetes": 0,
            "refusees": 0, "temps": 0.0})
        tailles = sum(len(c) for c, _, _ in fichiers(
            decisions, resultats, resume_lue, compte))
        n_scores = len(resultats["scores"].seuils)
        suite = (f"États, comptes, scores ({n_scores} ajustés), rapport et "
                 f"fichiers : passés ({tailles // 1024} Kio de fichiers, "
                 "jetés).")
    except Exception as erreur:                          # noqa: BLE001
        log.exception("Essai /motifs : la suite a échoué")
        suite = ("⚠ États, scores ou rapport en échec : "
                 + html.escape(f"{type(erreur).__name__}: {erreur}")[:300])
    t_total = time.monotonic() - t0
    jours_lus = max(1, (fin - lecture_debut) // 86400)
    jours = max(1, (fin - DEBUT) // 86400)
    n_paires = len(min_bougies)
    projection = (t_paire * n_paires * jours / jours_lus
                  + t_resumes * jours / JOURS_ESSAI) / 60
    rss_depart, rss_max = _ETAT.get("rss_depart"), _ETAT.get("rss_max")
    if rss_depart is not None and rss_max is not None:
        hausse = max(0.0, rss_max - rss_depart) * jours * n_paires / jours_lus
        memoire_txt = (f"Mémoire du processus : {rss_depart:.0f} Mo au "
                       f"départ, pic {rss_max:.0f} Mo pendant l'essai ; "
                       f"{memoire.resume()}.")
        pic_txt = (f" ; mémoire au plus de l'ordre de {rss_depart:.0f} + "
                   f"{hausse:.0f} Mo (extrapolation prudente, toutes paires)")
    else:
        memoire_txt = f"Mémoire : {memoire.resume()}."
        pic_txt = ""
    mesurees = sum(n for cle, n in decisions.statuts.items()
                   if cle[0] == "décisions" and cle[2] == MESUREE)
    requetes = sum(lue.requetes.values()) + (
        sum(active.requetes.values()) if active is not None else 0)
    refusees = lue.refusees + (active.refusees if active else 0)
    return "\n".join([
        entete,
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in lecture.items())
        + ". Rien n'est écrit en base, aucun ordre.",
        f"Paire essayée : {html.escape(nom)} (la plus fournie en ticks), du "
        f"{en._utc(lecture_debut)} au {en._utc(fin - 86400)} ({jours_lus} "
        "jours).",
        f"Lu : {en._entier(n_bougies)} bougies, minutes décodées "
        f"{en._entier(ticks.decodees if ticks else 0)} "
        f"({ticks.illisibles if ticks else 0} illisibles) ; décisions "
        f"jouables et mesurées : {en._entier(mesurees)}"
        + ("" if avec_ticks else " ; ⚠ sans les ticks") + ".",
        suite,
        f"Durées : résumés des bases {t_resumes:.0f} s, paire {t_paire:.0f} "
        f"s, total {t_total:.0f} s ; requêtes {requetes}, refusées "
        f"{refusees}.",
        memoire_txt,
        f"Projection prudente de /motifs complet ({n_paires} "
        f"paire{'s' if n_paires > 1 else ''} × {jours} jours) : environ "
        f"{max(1, round(projection))} min" + pic_txt + ".",
        "➡ Si l'essai est passé, lancez /motifs ; un point d'avancement "
        "arrive après 2 minutes, puis toutes les 10 minutes.",
    ])


# --------------------------------------------------------------------------- #
# Le rapport et les fichiers
# --------------------------------------------------------------------------- #

def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}".replace(".", ",")


def texte(base: Decisions, resultats: Mapping, meta: Mapping) -> str:
    lignes = resultats["lignes"]
    fin = meta["fin"]
    paires = meta["paires"]
    st = base.statuts
    mesurees = {per: st[("décisions", per, MESUREE)]
                for per in (EXPLORATION, VALIDATION)}
    non_jouables = sum(st[("décisions", per, NON_JOUABLE)]
                       for per in (EXPLORATION, VALIDATION))
    par_cle = {(l["famille"], l["etat"], l["echeance_min"], l["periode"]): l
               for l in lignes if l["type"] == "famille"}
    morceaux = [
        "🔬 <b>Recherche automatique de motifs sur l'OTC</b> (chaque "
        "minute, 22 familles, 511 états, 2 scores ; recherche isolée, "
        "lecture seule)",
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in meta["lecture"].items())
        + f" ; requêtes refusées : {meta['refusees']}. Rien n'a été écrit "
        "en base, aucun ordre.",
        f"Gel vérifié : empreinte {EMPREINTE[:8]}…",
        f"<b>Données</b> : {len(paires)} "
        f"paire{'s' if len(paires) > 1 else ''}, du {en._utc(DEBUT)} au "
        f"{en._utc(fin - 86400)} ; exploration jusqu'au "
        f"{en._utc(FIN_EXPLORATION - 86400)}, validation ensuite. Décisions "
        f"jouables et mesurées : {en._entier(mesurees[EXPLORATION])} en "
        f"exploration, {en._entier(mesurees[VALIDATION])} en validation "
        f"({en._entier(non_jouables)} minutes non jouables écartées) ; "
        f"minutes décodées : {en._entier(meta['decodees'])} ; résumés lus "
        f"sur l'active : {meta['divergents']}."
        + ("" if meta["avec_ticks"] else
           " ⚠ Lancée SANS les ticks : aucune issue n'est jugée."),
        "<b>Familles</b> (★ retenues en exploration, ✅ validées ; meilleur "
        "état d'octobre → son taux de novembre) :"]
    for _, libelle, familles in GROUPES:
        etats_n = sum(N_ETATS[f] for f in familles)
        cases = [l for l in lignes if l["type"] == "famille"
                 and l["famille"] in familles]
        retenues = sum(1 for l in cases if l["periode"] == EXPLORATION
                       and l["verdict"].startswith("retenue"))
        validees = sum(1 for l in cases
                       if l["verdict"] == "validée (BH 10 %)")
        meilleures = sorted((l for l in cases if l["periode"] == EXPLORATION
                             and l["n"] >= N_EXPLO_MIN and l["taux"]),
                            key=lambda l: -l["taux"])
        txt = f"• {libelle} ({etats_n} états) : ★ {retenues}, ✅ {validees}"
        if meilleures:
            m = meilleures[0]
            v = par_cle.get((m["famille"], m["etat"], m["echeance_min"],
                             VALIDATION), {})
            txt += (f" ; meilleur « {html.escape(m['etat'])} » "
                    f"({html.escape(m['famille'])}) à {m['echeance_min']} "
                    f"min, {m['sens_choisi']} : {_pct(m['taux'])} % → "
                    f"{_pct(v.get('taux'))} %")
        morceaux.append(txt + ".")
    morceaux.append("<b>Les scores</b> (le tiers le plus confiant, en "
                    "validation) :")
    scores = {(l["famille"], l["echeance_min"], l["periode"]): l
              for l in lignes if l["type"] == "score"}
    for nom in SCORES:
        cases = []
        for t in ECHEANCES:
            v = scores.get((nom, t, VALIDATION))
            if v is None:
                cases.append(f"{t} min non ajusté")
                continue
            marque = "✅" if v["verdict"].startswith("validée") else ""
            cases.append(f"{t} min {_pct(v['taux'])} % sur "
                         f"{en._entier(v['n'])}{marque}")
        morceaux.append(f"• {LIBELLES_SCORES[nom]} : " + " ; ".join(cases)
                        + ".")
    retenues = [l for l in lignes if l["type"] == "famille"
                and l["periode"] == EXPLORATION
                and l["verdict"].startswith("retenue")]
    validees = [l for l in lignes if l["verdict"] == "validée (BH 10 %)"]
    hasard = f"{attendues_au_hasard(lignes):.1f}".replace(".", ",")
    s_r = "s" if len(retenues) > 1 else ""
    s_v = "s" if len(validees) > 1 else ""
    morceaux.append(
        f"<b>Bilan</b> : {len(retenues)} case{s_r} retenue{s_r} en "
        f"exploration sur 2 044, quand le hasard en donnerait environ "
        f"{hasard} ; {len(validees)} case{s_v} validée{s_v}, scores "
        "compris.")
    for l in validees[:10]:
        quoi = (f"« {html.escape(l['etat'])} » ({html.escape(l['famille'])})"
                if l["type"] == "famille" else LIBELLES_SCORES[l["famille"]])
        morceaux.append(
            f"• ✅ {quoi}, {l['echeance_min']} min, {l['sens_choisi']} : "
            f"{_pct(l['taux'])} % sur {en._entier(l['n'])} en validation "
            f"[{_pct(l.get('ic_bas'))} ; {_pct(l.get('ic_haut'))}].")
    if validees:
        morceaux.append("Une case validée n'est pas une stratégie : elle se "
                        "gèle, puis se confirme sur des jours futurs, sans "
                        "argent réel.")
    else:
        morceaux.append("Aucun motif, seul ou combiné, n'annonce la "
                        "direction des minutes suivantes au-delà du point "
                        "mort de 52,08 % hors échantillon.")
    duree = f"{meta['temps'] / 60:.1f}".replace(".", ",")
    pied = f"Requêtes : {meta['requetes']}. Durée : {duree} min."
    if meta.get("rss_max") is not None:
        pied += (f" Mémoire : pic {meta['rss_max']:.0f} Mo pendant l'étude"
                 + (f" (départ {meta['rss_depart']:.0f} Mo)"
                    if meta.get("rss_depart") is not None else "") + ".")
    if meta.get("version"):
        pied += f" Version : {html.escape(meta['version'])}."
    morceaux.append(pied)
    return "\n".join(morceaux)


COLONNES_CASES = ("type", "famille", "etat", "echeance_min", "periode", "n",
                  "hausses", "baisses", "egalites", "taux_hausse",
                  "sens_choisi", "taux", "ic_bas", "ic_haut", "p", "verdict")
COLONNES_DETAILS = ("type", "famille", "etat", "echeance_min", "sens_choisi",
                    "periode", "dimension", "valeur", "n", "gains", "taux",
                    "ic_bas", "ic_haut")
COLONNES_PARAMETRES = ("element", "echeance_min", "cle", "valeur")
COLONNES_STATUTS = ("source", "periode", "statut", "echeance_min", "nombre")


def _details(base: Decisions, resultats: Mapping) -> list[dict[str, Any]]:
    """Les cases retenues, par paire, dans leur sens ; les scores, par paire
    et par sens prédit, en validation."""
    sortie = []
    comptes: Comptes = resultats["comptes"]
    bornes = resultats["bornes"]
    noms = {(f, libelle_etat(f, s, bornes)): s for f in FAMILLES
            for s in range(N_ETATS[f])}
    for l in resultats["lignes"]:
        if not (l["type"] == "famille" and l["periode"] == EXPLORATION
                and l["verdict"].startswith("retenue")):
            continue
        s = noms[(l["famille"], l["etat"])]
        case = comptes.case(l["famille"], s, l["echeance_min"])
        choisi = 1 if l["sens_choisi"] == BAISSE else 0
        for per in (EXPLORATION, VALIDATION):
            par_paire: dict[str, list[int]] = {}
            for b, x in comptes.par_bloc(case, resultats["blocs"][per]
                                         ).items():
                y = par_paire.setdefault(base.paire_du_bloc(b), [0, 0])
                y[0] += x[choisi]
                y[1] += sum(x)
            for p, (g, n) in sorted(par_paire.items()):
                bas, haut = en.wilson(g, n)
                sortie.append({"type": "famille", "famille": l["famille"],
                               "etat": l["etat"],
                               "echeance_min": l["echeance_min"],
                               "sens_choisi": l["sens_choisi"],
                               "periode": per, "dimension": "paire",
                               "valeur": p, "n": n, "gains": g,
                               "taux": g / n if n else None, "ic_bas": bas,
                               "ic_haut": haut})
    for (nom, t), detail in sorted(resultats["details_scores"].items()):
        for (dim, valeur), (g, n) in sorted(detail.items()):
            bas, haut = en.wilson(g, n)
            sortie.append({"type": "score", "famille": nom, "etat": "",
                           "echeance_min": t, "sens_choisi": "prédit",
                           "periode": VALIDATION, "dimension": dim,
                           "valeur": valeur, "n": n, "gains": g,
                           "taux": g / n if n else None, "ic_bas": bas,
                           "ic_haut": haut})
    return sortie


def _parametres(resultats: Mapping) -> list[dict[str, Any]]:
    sortie = []
    for cle, valeurs in resultats["bornes"].items():
        for k, v in enumerate(valeurs):
            sortie.append({"element": "borne", "echeance_min": "",
                           "cle": f"{cle}:{k + 1}", "valeur": f"{v:.8g}"})
    sc: Scores = resultats["scores"]
    for t, (constante, poids) in sorted(sc.bayes.items()):
        sortie.append({"element": "bayes", "echeance_min": t,
                       "cle": "constante", "valeur": f"{constante:.8g}"})
        for famille, valeurs in poids.items():
            for s, w in enumerate(valeurs):
                sortie.append({"element": "bayes", "echeance_min": t,
                               "cle": f"{famille}:{s}", "valeur": f"{w:.8g}"})
    if sc.prep:
        for nom, (bas, haut, moy, et) in zip(VARIABLES, sc.prep):
            for element, v in (("borne_1pc", bas), ("borne_99pc", haut),
                               ("moyenne", moy), ("ecart_type", et)):
                sortie.append({"element": "preparation", "echeance_min": "",
                               "cle": f"{element}:{nom}",
                               "valeur": f"{v:.8g}"})
    for t, modele in sorted(sc.logit.items()):
        for nom, c in zip(("interception",) + VARIABLES, modele.coefficients):
            sortie.append({"element": "logistique", "echeance_min": t,
                           "cle": f"coef:{nom}", "valeur": f"{c:.8g}"})
        sortie.append({"element": "logistique", "echeance_min": t,
                       "cle": "n_ajustement",
                       "valeur": str(sc.n_ajustement[t])})
    for (nom, t), seuil in sorted(sc.seuils.items()):
        sortie.append({"element": nom, "echeance_min": t,
                       "cle": "seuil_confiance", "valeur": f"{seuil:.8g}"})
    return sortie


def fichiers(base: Decisions, resultats: Mapping, resume, compte: Counter
             ) -> list[tuple[bytes, str, str]]:
    statuts = []
    for cle, n in sorted(list(base.statuts.items()) + list(compte.items()),
                         key=lambda kv: tuple(str(x) for x in kv[0])):
        if len(cle) == 2:
            valeurs = (cle[0], "", cle[1], "")
        elif len(cle) == 3:
            valeurs = (cle[0], cle[1], cle[2], "")
        else:
            valeurs = cle
        statuts.append(dict(zip(COLONNES_STATUTS, (*valeurs, n))))
    jours: dict[tuple[str, int], dict[str, Any]] = {}
    for (table, p, j), v in resume.items():
        d = jours.setdefault((p, j), {"paire": p, "jour": en._utc(
            j * 86400, "%Y-%m-%d")})
        if table == "candles" and v:
            d["minutes_bougies"] = v[0]
        elif table == "tick_paths" and v:
            d["minutes_ticks"] = v[0]
        elif table == "payouts" and v:
            d["releves_payout"] = v[0]
    donnees = [jours[k] for k in sorted(jours)]
    return [
        (en._csv(resultats["lignes"], COLONNES_CASES), "motifs_cases.csv",
         "Une ligne par case (famille × état × échéance, ou score × "
         "échéance) et par période : taux, IC, verdict"),
        (en._csv(_details(base, resultats), COLONNES_DETAILS),
         "motifs_details.csv",
         "Les cases retenues par paire ; les scores par paire et par sens "
         "prédit"),
        (en._csv(_parametres(resultats), COLONNES_PARAMETRES),
         "motifs_parametres.csv",
         "Bornes, poids bayésiens, préparation, coefficients et seuils des "
         "scores"),
        (en._csv(statuts, COLONNES_STATUTS), "motifs_statuts.csv",
         "Décisions jouables, mesurées, écartées ; statuts des issues"),
        (en._csv(donnees, ("paire", "jour", "minutes_bougies",
                           "minutes_ticks", "releves_payout")),
         "motifs_donnees.csv", "Les données lues sur la relève, par paire "
         "et par jour"),
    ]
