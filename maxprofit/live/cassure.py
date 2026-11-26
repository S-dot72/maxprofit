"""
Cassure ou retournement au toucher d'une zone : une étude ISOLÉE, en LECTURE
SEULE.

Protocole figé : `docs/preinscription_cassure.json`, empreinte 14108d3b…
La passe refuse de tourner si `PARAMETRES` ne donnent plus cette empreinte.

Les zones et les trades sont ceux de /niveaux (gel afbcb91e) pour trois
définitions — M1, M5 et M15, pivot à 5 bougies, sans seuil d'impulsion : même
formation, même premier toucher au tick, mêmes épisodes, mêmes issues. En
moyenne, une zone touchée se retourne une fois sur deux. La question est
donc : un signe connu AVANT le toucher sépare-t-il les zones qui cassent de
celles qui tiennent, assez pour battre 52,08 % hors échantillon ?

Dix signes sont mesurés à chaque toucher, sans rien lire après lui :
vitesse d'arrivée, élan de l'heure, approches manquées, âge de la zone, force
de départ, éloignement avant le retour, compression, dernière bougie,
confluence et position dans le jour. Chacun est jugé seul, par tiers
(120 cases), puis tous ensemble dans deux scores logistiques, simple et
croisé (8 cases). Octobre choisit, novembre juge, Benjamini-Hochberg corrige ;
une case validée devra encore se confirmer sur des jours futurs.

Elle n'importe ni la course, ni l'exécution des ordres, ni la collecte.
Sorties en CSV seulement, aucune écriture en base. Rien n'est ventilé par
heure : la confirmation EURJPY 22 h reste aveugle.
"""

from __future__ import annotations

import csv
import gzip
import heapq
import html
import io
import json
import logging
import math
import time
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from statistics import median
from typing import Any, Callable, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core import memoire
from maxprofit.live import engulfing as en
from maxprofit.live import niveaux as nv
from maxprofit.live.selection3 import PAYOUT_AVANT, PAYOUTS_DU_JOUR

log = logging.getLogger(__name__)

PARAMETRES: dict[str, Any] = json.loads(r"""
{
  "objet": "Cassure ou retournement : au moment où le prix touche une zone, un signe connu AVANT le toucher sépare-t-il les zones qui cassent de celles qui tiennent, assez pour battre le point mort de 52,0833 % hors échantillon ? Les zones sont celles de /niveaux (gel afbcb91e) ; en moyenne, elles se retournent une fois sur deux (49,6 % de retournements et 49,8 % de cassures en validation, rapport du 26/11).",
  "demande": "26/11 : « On a des zones c'est bien ca ? Il suffit qu'on arrive à déterminer ce qui fait que le prix casse la zone ou fait un retournement », puis « Tu dois trouver la solution analyse tout »",
  "donnees": {
    "bougies": "candles, tf_sec = 60, complete = 1, lues sur la relève ; un jour dont le résumé diffère de l'active est lu sur l'active",
    "ticks": "tick_paths, mêmes règles de copie",
    "payouts": "payouts (points de changement), mêmes règles de copie",
    "paires": "toutes les paires qui ont des bougies M1 dans la période",
    "debut_utc": "2026-10-06T00:00:00Z",
    "fin_exploration_utc": "2026-11-03T00:00:00Z",
    "fin_validation": "début du jour UTC du lancement : seuls des jours complets sont lus"
  },
  "zones": "les niveaux R1 de /niveaux (gel afbcb91e) pour trois définitions : M1, M5 et M15, pivot à 5 bougies, sans seuil d'impulsion (R1_M1_w5_s0, R1_M5_w5_s0, R1_M15_w5_s0). Même formation, même activation, même premier toucher au tick dans les 24 h, mêmes épisodes de 5 minutes par définition et par paire : les touchers jugés sont exactement les trades de ces trois définitions dans /niveaux",
  "issue": "le trade au toucher, comme dans /niveaux : entrée au dernier tick connu 2 s après le toucher, sortie à 1, 2, 3 ou 5 minutes, ticks datés de moins de 10 s, minutes continues. Retournement : à l'échéance, le prix est repassé du côté d'où il venait par rapport au prix d'entrée (gain du sens de la règle : PUT sur une zone de vendeurs, CALL sur une zone d'acheteurs). Cassure : il est de l'autre côté (gain du sens inverse). Une égalité est une perte pour les deux sens. Seuls les touchers jouables comptent : dernier payout du flux relevé au plus tard à l'entrée ≥ 84",
  "echelle": "m = étendue médiane (plus haut − plus bas) des 50 bougies M1 closes qui précèdent la minute du toucher ; d = +1 pour une zone de vendeurs (le prix monte vers elle), −1 pour une zone d'acheteurs",
  "fenetre": "un toucher n'est mesuré que si les 240 bougies M1 qui précèdent la minute du toucher se suivent sans trou, si au moins 720 bougies M1 existent dans les 24 h qui précèdent cette minute, et si m, les étendues médianes des 30 et des 240 dernières bougies et l'étendue de ces 24 h sont strictement positives ; sinon il est écarté et compté",
  "signes": {
    "vitesse": "d × (niveau − clôture de la bougie M1 qui commence 5 minutes avant la minute du toucher) / m : le chemin fait vers la zone dans les 5 dernières minutes",
    "elan": "d × (niveau − clôture de la bougie M1 qui commence 60 minutes avant la minute du toucher) / m : le chemin fait vers la zone dans la dernière heure",
    "approches": "ln(1 + nombre de bougies M1 closes, de l'activation à la minute du toucher exclue, dont l'extrême du côté de la zone est passé à strictement moins de 0,5 m du niveau sans l'atteindre)",
    "age": "ln(1 + minutes entre l'activation et le début de la minute du toucher)",
    "impulsion": "ln(1 + écart / m), écart = distance entre le niveau et l'extrême opposé atteint de la bougie du niveau à la bougie de confirmation, dans l'unité de la zone : le même écart que l'impulsion de /niveaux, rapporté ici à m",
    "eloignement": "ln(1 + d × (niveau − extrême opposé) / m), extrême opposé = plus bas (zone de vendeurs) ou plus haut (zone d'acheteurs) des bougies M1 de la dernière minute de la confirmation à la minute du toucher exclue : jusqu'où le prix s'est éloigné avant de revenir",
    "compression": "ln(étendue médiane des 30 dernières bougies M1 closes / étendue médiane des 240 dernières)",
    "derniere_bougie": "d × (clôture − ouverture de la dernière bougie M1 close) / m : un corps qui pousse vers la zone est positif",
    "confluence": "ln(1 + nombre d'autres zones du même camp, des trois définitions, actives au début de la minute du toucher et dont le prix est à strictement moins de 0,5 m du niveau). Active : activée au plus tard au début de cette minute, depuis moins de 24 h, non touchée dans une minute antérieure, et sans trou de minutes entre son activation et cette minute",
    "position_jour": "place de la zone dans l'étendue des bougies M1 closes des 24 h précédentes : (niveau − plus bas) / (plus haut − plus bas) pour une zone de vendeurs, (plus haut − niveau) / (plus haut − plus bas) pour une zone d'acheteurs ; 1 = la zone est à l'extrême du jour dans son sens"
  },
  "signe_seul": {
    "tiers": "pour chaque signe, b1 et b2 = quantiles 1/3 et 2/3 de sa valeur sur les touchers jouables et mesurés de l'exploration. Si b1 < b2 : bas = valeur < b1, milieu = b1 ≤ valeur < b2, haut = valeur ≥ b2. Si b1 = b2 = b (beaucoup de valeurs égales, par exemple zéro) : bas = valeur < b, milieu = valeur égale à b, haut = valeur > b. Les tiers peuvent donc être inégaux, et un tiers vide n'est pas jugé",
    "cases": "10 signes × 3 tiers × 4 échéances = 120 cases",
    "sens": "chaque case choisit en exploration entre retournement et cassure (cassure si elle gagne strictement plus souvent), et garde ce choix en validation",
    "retenue": "si son sens choisi atteint 52,0833 % sur au moins 300 décisions jouables en exploration"
  },
  "scores": {
    "variables": "les dix signes, bornés à leurs quantiles 1 % et 99 % de l'exploration, puis centrés et réduits avec la moyenne et l'écart-type de l'exploration (après bornage) ; plus l'unité (deux indicatrices : M5, M15) et le camp (une indicatrice : zone d'acheteurs)",
    "score_simple": "les 13 variables",
    "score_croise": "les 13 variables et les 45 produits deux à deux des dix signes préparés",
    "modele": "par échéance et par score, une régression logistique (Newton, pénalité L2 λ = 1 sur les coefficients, interception non pénalisée : etude_micro.Logistique) ajustée sur les touchers jouables, mesurés et décisifs de l'exploration (égalités exclues de l'ajustement) ; elle donne la probabilité du retournement",
    "decision": "sens prédit : retournement si la probabilité dépasse 0,5, cassure sinon ; confiance = |probabilité − 0,5|",
    "selection": "seuls les touchers dont la confiance atteint le quantile 2/3 des confiances des touchers jouables et mesurés de l'exploration sont joués : le tiers le plus confiant, seuil fixé en exploration",
    "cases": "2 scores × 4 échéances = 8 cases, jugées directement en validation"
  },
  "selection": {
    "exploration": "jours du 06/10 au 02/11 : bornes des tiers, sens des cases, préparation des variables, ajustement des scores et seuils de confiance",
    "validation": "jours du 03/11 à la veille du lancement : chaque case retenue (signe seul) et chaque case de score est jugée dans son sens contre 52,0833 %, avec au moins 100 décisions ; p unilatérale du bootstrap par blocs paire × jour (1 000 tirages, graine 7) ; Benjamini-Hochberg à 10 % sur l'ensemble des cases jugées en validation",
    "verdict": "validée si BH la retient et que son taux dépasse 52,0833 % ; sinon non reproduite",
    "confirmation": "une case validée n'est pas une stratégie : elle est gelée, puis confirmée sur des jours postérieurs au lancement, avant tout essai en démo"
  },
  "rapport": "aucune ventilation par heure (la confirmation EURJPY 22 h reste aveugle) ; pour chaque signe, les taux de retournement par tiers ; les coefficients des scores ; le détail par paire et par sens pour les cases retenues",
  "interdits": [
    "lecture seule : aucune écriture en base, sortie en CSV seulement",
    "aucun appel à l'exécution des ordres, aucun changement de la course, de la collecte, de ZoneH1, des échéances ni des configurations",
    "aucun signe, aucune variable ni aucun réglage ne s'ajoute ou ne se rechoisit après le gel",
    "aucun argent réel sur une case validée avant sa confirmation, et pas même après sans un essai en démo"
  ],
  "quantiles": "le quantile q d'une liste de n valeurs est la valeur de rang ⌊q × n⌋ (rangs comptés à partir de 0) de la liste triée, plafonné au rang n − 1 ; l'écart-type est celui de la population ; une variable d'écart-type nul vaut 0 une fois préparée"
}
""")
EMPREINTE = "14108d3b4e50ed694861771e54e18ed74e491fcd91fd7205ee68486bced1c875"

DEBUT = en._instant(PARAMETRES["donnees"]["debut_utc"])
FIN_EXPLORATION = en._instant(PARAMETRES["donnees"]["fin_exploration_utc"])
ECHEANCES: tuple[int, ...] = (1, 2, 3, 5)
POINT_MORT = en.POINT_MORT
EXPLORATION, VALIDATION = en.EXPLORATION, en.VALIDATION
N_EXPLO_MIN = 300
N_VALID_MIN = 100
JOURS_ESSAI = 2
RESPIRATION_SEC = 0.02

UNITES: tuple[int, ...] = (1, 5, 15)
FENETRE = 5
DEFINITIONS: tuple[str, ...] = tuple(f"R1_M{u}_w{FENETRE}_s0" for u in UNITES)

N_CONTINU = 240          # bougies M1 qui se suivent avant la minute du toucher
N_JOUR = 720             # bougies M1 au moins dans les 24 h
N_M = 50                 # l'échelle m
N_COURT = 30             # la compression : 30 contre 240
DEMI = 0.5               # approches et confluence : à moins de 0,5 m
LAMBDA = 1.0             # pénalité L2 des scores

SIGNES: tuple[str, ...] = (
    "vitesse", "elan", "approches", "age", "impulsion", "eloignement",
    "compression", "derniere_bougie", "confluence", "position_jour")
LIBELLES: dict[str, str] = {
    "vitesse": "Vitesse d'arrivée (5 min)",
    "elan": "Élan de l'heure",
    "approches": "Approches manquées",
    "age": "Âge de la zone",
    "impulsion": "Force de départ",
    "eloignement": "Éloignement avant le retour",
    "compression": "Compression",
    "derniere_bougie": "Dernière bougie",
    "confluence": "Confluence",
    "position_jour": "Position dans le jour",
}
TIERS: tuple[str, ...] = ("bas", "milieu", "haut")
SCORES: tuple[str, ...] = ("score_simple", "score_croise")
LIBELLES_SCORES = {"score_simple": "Score simple",
                   "score_croise": "Score croisé"}
VARIABLES_FIXES = ("unite_M5", "unite_M15", "zone_acheteurs")
CROISEMENTS: tuple[tuple[int, int], ...] = tuple(
    (a, b) for a in range(len(SIGNES)) for b in range(a + 1, len(SIGNES)))

RETOURNEMENT, CASSURE = "retournement", "cassure"
ABSENTE = -9             # issue manquante dans les colonnes

#: Statuts de la mesure.
MESURE = "mesuré"
FENETRE_INCOMPLETE = "240 minutes d'avant incomplètes"
JOUR_INCOMPLET = "moins de 720 minutes dans les 24 h"
ETENDUE_NULLE = "étendue nulle"


# --------------------------------------------------------------------------- #
# Les quantiles et les tiers, tels que le protocole les définit
# --------------------------------------------------------------------------- #

def quantile(triees: Sequence[float], num: int, den: int) -> float:
    """La valeur de rang ⌊num/den × n⌋ de la liste triée, plafonnée au rang
    n − 1 (calcul entier : aucun arrondi de flottant)."""
    if not triees:
        raise ValueError("quantile d'une liste vide")
    return triees[min(len(triees) - 1, len(triees) * num // den)]


def tiers_de(x: float, b1: float, b2: float) -> int:
    """0 bas, 1 milieu, 2 haut ; si b1 = b2, le milieu est la valeur b1."""
    if b1 < b2:
        return 0 if x < b1 else (1 if x < b2 else 2)
    return 0 if x < b1 else (1 if x == b1 else 2)


# --------------------------------------------------------------------------- #
# Les zones : le chemin de /niveaux, plus la fin de leur activité
# --------------------------------------------------------------------------- #

def parcours(s1: nv.SerieU, niv: nv.Niveau) -> tuple[int | None, str, int]:
    """(indice de la minute du toucher, statut, dernière minute active).

    Le même chemin que `nv.minute_du_toucher`, plus la borne de l'activité :
    la zone est active aux minutes qui commencent entre son activation et
    cette borne, incluses (sa minute du toucher comprise). Après un trou de
    minutes, elle ne l'est plus : le prix a pu la traverser sans qu'on le
    voie."""
    ts, n = s1.ts, len(s1.ts)
    activation = niv.activation
    borne_24h = activation + nv.VALIDITE_SEC - 1
    i = bisect_left(ts, activation)
    if i >= n:
        return None, nv.FIN_DONNEES, borne_24h
    if ts[i] != activation:
        return None, nv.TROU, activation - 1
    prix, vendeur = niv.prix, niv.sens == nv.PUT
    extreme = s1.h if vendeur else s1.l
    attendu = activation
    limite = activation + nv.VALIDITE_SEC
    while i < n:
        t = ts[i]
        if t >= limite:
            return None, nv.SANS_TOUCHER, borne_24h
        if t != attendu:
            return None, nv.TROU, attendu - 1
        x = extreme[i]
        if (x >= prix) if vendeur else (x <= prix):
            return i, nv.TOUCHER, t
        attendu = t + 60
        i += 1
    return None, nv.FIN_DONNEES, borne_24h


class Actives:
    """Les zones actives d'une paire au fil des minutes, pour la confluence.
    Les instants demandés doivent croître."""

    def __init__(self, zones: Sequence[tuple[int, int, float, int, int]]):
        # (activation, dernière minute active, prix, sens, identifiant)
        self._zones = sorted(zones, key=lambda z: z[0])
        self._k = 0
        self._tas: list[tuple[int, int]] = []
        self._actives: dict[int, tuple[float, int]] = {}
        self._dernier = None

    def avancer(self, t: int) -> None:
        if self._dernier is not None and t < self._dernier:
            raise ValueError("la confluence se lit dans l'ordre du temps")
        self._dernier = t
        zones = self._zones
        while self._k < len(zones) and zones[self._k][0] <= t:
            _, fin, prix, sens, ident = zones[self._k]
            self._k += 1
            if fin >= t:
                self._actives[ident] = (prix, sens)
                heapq.heappush(self._tas, (fin, ident))
        while self._tas and self._tas[0][0] < t:
            _, ident = heapq.heappop(self._tas)
            self._actives.pop(ident, None)

    def compter(self, t: int, prix: float, sens: int, soi: int,
                demi: float) -> int:
        """Les AUTRES zones du même camp, actives à la minute t, à
        strictement moins de `demi` du prix."""
        self.avancer(t)
        return sum(1 for ident, (p, s) in self._actives.items()
                   if ident != soi and s == sens and abs(p - prix) < demi)


# --------------------------------------------------------------------------- #
# Les dix signes, mesurés avant le toucher
# --------------------------------------------------------------------------- #

def mesurer(s1: nv.SerieU, i: int, niv: nv.Niveau,
            confluence: Callable[[float], int]
            ) -> tuple[tuple[float, ...] | None, str]:
    """Les dix signes du toucher de `niv` dans la minute d'indice i, dans
    l'ordre de `SIGNES`, ou (None, pourquoi). Seules servent les bougies
    closes AVANT la minute du toucher, le niveau lui-même et les zones
    actives au début de cette minute. `confluence(demi)` compte celles-ci."""
    ts, o, h, l, c = s1.ts, s1.o, s1.h, s1.l, s1.c
    if (i < N_CONTINU or ts[i - 1] != ts[i] - 60
            or not s1.continue_(i - 1, N_CONTINU)):
        return None, FENETRE_INCOMPLETE
    j0 = bisect_left(ts, ts[i] - 86_400)
    if i - j0 < N_JOUR:
        return None, JOUR_INCOMPLET
    etendues = [h[k] - l[k] for k in range(i - N_CONTINU, i)]
    m = median(etendues[-N_M:])
    m_court = median(etendues[-N_COURT:])
    m_long = median(etendues)
    haut, bas = max(h[j0:i]), min(l[j0:i])
    if m <= 0 or m_court <= 0 or m_long <= 0 or haut <= bas:
        return None, ETENDUE_NULLE
    prix = niv.prix
    vendeur = niv.sens == nv.PUT
    d = 1 if vendeur else -1
    age_min = (ts[i] - niv.activation) // 60
    a = i - age_min                     # la minute d'activation
    seuil = DEMI * m
    depuis = max(a - 1, 0)              # la dernière minute de la confirmation
    if vendeur:
        approches = sum(1 for k in range(a, i) if prix - h[k] < seuil)
        loin = prix - min(l[depuis:i])
        position = (prix - bas) / (haut - bas)
    else:
        approches = sum(1 for k in range(a, i) if l[k] - prix < seuil)
        loin = max(h[depuis:i]) - prix
        position = (haut - prix) / (haut - bas)
    return (
        d * (prix - c[i - 5]) / m,
        d * (prix - c[i - 60]) / m,
        math.log1p(approches),
        math.log1p(age_min),
        math.log1p(niv.ecart / m),
        math.log1p(loin / m),
        math.log(m_court / m_long),
        d * (c[i - 1] - o[i - 1]) / m,
        math.log1p(confluence(seuil)),
        position,
    ), MESURE


# --------------------------------------------------------------------------- #
# Les touchers, leurs trades et leurs issues aux ticks
# --------------------------------------------------------------------------- #

class Toucher:
    __slots__ = ("definition", "paire", "reference_ms", "sens", "payout",
                 "issues", "statuts", "niveau", "indice", "zone", "signes",
                 "mesure")

    def __init__(self, definition: int, paire: str, reference_ms: int,
                 niveau: nv.Niveau, indice: int, zone: int) -> None:
        self.definition = definition          # rang dans DEFINITIONS
        self.paire = paire
        self.reference_ms = reference_ms      # le tick du toucher
        self.sens = niveau.sens               # celui de la règle : retournement
        self.payout: float | None = None
        self.issues: dict[int, int] = {}
        self.statuts: dict[int, str] = {}
        self.niveau = niveau
        self.indice = indice                  # la minute du toucher dans s1
        self.zone = zone
        self.signes: tuple[float, ...] | None = None
        self.mesure = ""

    @property
    def entree_ms(self) -> int:
        return self.reference_ms + nv.DELAI_MS

    @property
    def periode(self) -> str:
        return (EXPLORATION if self.entree_ms < FIN_EXPLORATION * 1000
                else VALIDATION)

    @property
    def jouable(self) -> bool:
        return self.payout is not None and self.payout >= en.FLUX_MIN


def evenements(bougies: Sequence[en.Bougie], compte: Counter,
               respirer: Callable[[], None]):
    """(série M1, zones pour la confluence, touchers triés) d'une paire : les
    touchers sont (seconde, définition, zone, niveau, indice de la minute)."""
    s1 = nv.SerieU(bougies, 60)
    zones: list[tuple[int, int, float, int, int]] = []
    sortie: list[tuple[int, int, int, nv.Niveau, int]] = []
    ident = 0
    for k, u in enumerate(UNITES):
        su = s1 if u == 1 else nv.SerieU(nv.regrouper(bougies, u), 60 * u)
        for niv in nv.niveaux_de(su, FENETRE):
            i, statut, fin = parcours(s1, niv)
            compte[("niveaux", DEFINITIONS[k], "formés")] += 1
            compte[("niveaux", DEFINITIONS[k], statut)] += 1
            zones.append((niv.activation, fin, niv.prix, niv.sens, ident))
            if i is not None:
                sortie.append((s1.ts[i], k, ident, niv, i))
            ident += 1
        respirer()
    sortie.sort(key=lambda e: (e[0], e[1]))
    return s1, zones, sortie


def paire(nom: str, bougies: Sequence[en.Bougie], fin: int,
          ticks: en.Ticks | None, payout_a: Callable[[float], float | None],
          compte: Counter, respirer: Callable[[], None]) -> list[Toucher]:
    """Les touchers retenus d'une paire, dans l'ordre des entrées : les
    trades de /niveaux pour les trois définitions, puis, pour les jouables,
    les dix signes et les issues aux ticks. Une seule passe chronologique."""
    s1, zones, tries = evenements(bougies, compte, respirer)
    actives = Actives(zones)
    limite_ms = fin * 1000
    duree_ms = nv.DELAI_MS + 60_000 * max(ECHEANCES)
    derniere: dict[int, int] = {}
    retenus: list[Toucher] = []
    a, n, groupes = 0, len(tries), 0
    while a < n:
        sec, k = tries[a][0], tries[a][1]
        b = a + 1
        while b < n and tries[b][0] == sec and tries[b][1] == k:
            b += 1
        groupe, a = tries[a:b], b
        definition = DEFINITIONS[k]
        if ticks is None:
            compte[("niveaux", definition, nv.SANS_TICKS)] += len(groupe)
            continue
        ticks.oublier_avant(sec)
        touchers = []
        for _, _, zone, niv, i in groupe:
            t_ms, statut = nv.tick_du_toucher(niv, sec, ticks)
            compte[("niveaux", definition, statut)] += 1
            if t_ms is not None:
                touchers.append(Toucher(k, nom, t_ms, niv, i, zone))
        touchers.sort(key=lambda x: x.reference_ms)
        for tch in touchers:
            precedente = derniere.get(k)
            if precedente is not None and \
                    tch.entree_ms < precedente + nv.EPISODE_MS:
                compte[("épisodes", definition, nv.CHEVAUCHE)] += 1
                continue
            derniere[k] = tch.entree_ms
            if tch.reference_ms + duree_ms > limite_ms:
                compte[("épisodes", definition, nv.FIN_DONNEES)] += 1
                continue
            compte[("épisodes", definition, tch.periode)] += 1
            tch.payout = payout_a(tch.entree_ms / 1000)
            if tch.jouable:
                niv = tch.niveau
                tch.signes, tch.mesure = mesurer(
                    s1, tch.indice, niv,
                    lambda demi, t=s1.ts[tch.indice], niv=niv, z=tch.zone:
                        actives.compter(t, niv.prix, niv.sens, z, demi))
                if tch.signes is not None:
                    nv.issues_ticks(tch, ticks)
            retenus.append(tch)
        groupes += 1
        if groupes % 500 == 0:
            respirer()
    del tries, zones
    respirer()
    retenus.sort(key=lambda x: (x.reference_ms, x.definition))
    return retenus


# --------------------------------------------------------------------------- #
# Les touchers mesurés, en colonnes
# --------------------------------------------------------------------------- #

COLONNES_TOUCHERS: tuple[str, ...] = (
    ("definition", "paire", "entree_utc", "reference_ms", "periode", "camp",
     "payout", "jouable", "mesure") + SIGNES
    + tuple(f"issue_{t}" for t in ECHEANCES)
    + tuple(f"statut_{t}" for t in ECHEANCES))


class Touchers:
    """Les touchers jouables et mesurés, en colonnes compactes ; le fichier
    de tous les touchers retenus, compressé au fil de l'eau. Une issue vaut
    +1 (retournement), −1 (cassure), 0 (égalité) ou ABSENTE."""

    def __init__(self) -> None:
        self.signes = [array("d") for _ in SIGNES]
        self.issues = {t: array("b") for t in ECHEANCES}
        self.bloc = array("l")
        self.validation = array("b")
        self.unite = array("b")
        self.acheteur = array("b")
        self.paires: list[str] = []
        self.blocs: dict[tuple[str, int], int] = {}
        self.noms_blocs: list[tuple[str, int]] = []
        self.n_retenus = 0
        self.statuts: Counter = Counter()
        self._tampon = io.BytesIO()
        self._gz = gzip.GzipFile(fileobj=self._tampon, mode="wb", mtime=0)
        self._ecrire([COLONNES_TOUCHERS])

    def __len__(self) -> int:
        return len(self.bloc)

    def _ecrire(self, lignes) -> None:
        texte_ = io.StringIO()
        csv.writer(texte_, delimiter=";").writerows(lignes)
        self._gz.write(texte_.getvalue().encode("utf-8"))

    def ajouter(self, touchers: Sequence[Toucher]) -> None:
        lignes = []
        for tch in touchers:
            self.n_retenus += 1
            definition = DEFINITIONS[tch.definition]
            per = tch.periode
            if not tch.jouable:
                self.statuts[("jouabilité", definition, per, "non jouable",
                              "")] += 1
            else:
                self.statuts[("mesure", definition, per, tch.mesure, "")] += 1
            if tch.jouable and tch.signes is not None:
                for t in ECHEANCES:
                    self.statuts[("ticks", definition, per,
                                  tch.statuts.get(t, "sans ticks"), t)] += 1
                self._colonnes(tch, per)
            lignes.append(
                [definition, tch.paire,
                 en._utc(tch.entree_ms // 1000, "%Y-%m-%d %H:%M:%S"),
                 tch.reference_ms, per,
                 "vendeurs" if tch.sens == nv.PUT else "acheteurs",
                 "" if tch.payout is None else f"{tch.payout:.0f}",
                 int(tch.jouable), tch.mesure]
                + (["" for _ in SIGNES] if tch.signes is None
                   else [f"{v:.6g}" for v in tch.signes])
                + [tch.issues.get(t, "") for t in ECHEANCES]
                + [tch.statuts.get(t, "") for t in ECHEANCES])
        self._ecrire(lignes)

    def _colonnes(self, tch: Toucher, per: str) -> None:
        jour = tch.entree_ms // 86_400_000
        cle = (tch.paire, jour)
        bloc = self.blocs.get(cle)
        if bloc is None:
            bloc = self.blocs[cle] = len(self.noms_blocs)
            self.noms_blocs.append(cle)
        for j, v in enumerate(tch.signes):
            self.signes[j].append(v)
        for t in ECHEANCES:
            self.issues[t].append(tch.issues.get(t, ABSENTE))
        self.bloc.append(bloc)
        self.validation.append(per == VALIDATION)
        self.unite.append(tch.definition)
        self.acheteur.append(tch.sens == nv.CALL)

    def indices(self, periode: str) -> list[int]:
        voulu = periode == VALIDATION
        return [r for r in range(len(self)) if bool(self.validation[r]) == voulu]

    def paire_de(self, r: int) -> str:
        return self.noms_blocs[self.bloc[r]][0]

    def signaux_gz(self) -> bytes:
        self._gz.close()
        return self._tampon.getvalue()


# --------------------------------------------------------------------------- #
# Chaque signe seul, par tiers
# --------------------------------------------------------------------------- #

def bornes_des_tiers(base: Touchers, explo: Sequence[int]
                     ) -> list[tuple[float, float]]:
    sortie = []
    for j in range(len(SIGNES)):
        valeurs = sorted(base.signes[j][r] for r in explo)
        sortie.append((quantile(valeurs, 1, 3), quantile(valeurs, 2, 3))
                      if valeurs else (0.0, 0.0))
    return sortie


def _taux_et_ic(par_bloc: Mapping[int, list[int]], choisi: int,
                tirage: en.Tirages, avec_p: bool):
    """(n, gains du sens choisi, taux, bas, haut, p) d'une case, à partir de
    ses comptes [retournements, cassures, égalités] par bloc."""
    n = sum(sum(x) for x in par_bloc.values())
    gains = sum(x[choisi] for x in par_bloc.values())
    if not n:
        return 0, 0, None, None, None, None
    taux = gains / n
    gb = {b: float(x[choisi]) for b, x in par_bloc.items()}
    nb = {b: float(sum(x)) for b, x in par_bloc.items()}
    bas, haut, p = tirage.ratio(gb, nb, taux)
    return n, gains, taux, bas, haut, (p if avec_p else None)


def cases_des_signes(base: Touchers, bornes, tirages,
                     respirer: Callable[[], None]) -> list[dict[str, Any]]:
    """Une ligne par signe × tiers × échéance × période."""
    comptes: dict[tuple, dict[int, list[int]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0, 0]))
    for j in range(len(SIGNES)):
        b1, b2 = bornes[j]
        colonne = base.signes[j]
        for r in range(len(base)):
            c = tiers_de(colonne[r], b1, b2)
            per = VALIDATION if base.validation[r] else EXPLORATION
            bloc = base.bloc[r]
            for t in ECHEANCES:
                issue = base.issues[t][r]
                if issue == ABSENTE:
                    continue
                rang = 0 if issue > 0 else (1 if issue < 0 else 2)
                comptes[(j, c, t, per)][bloc][rang] += 1
        respirer()
    lignes = []
    for j, signe in enumerate(SIGNES):
        for c, tiers in enumerate(TIERS):
            for t in ECHEANCES:
                explo = comptes.get((j, c, t, EXPLORATION), {})
                r_ = sum(x[0] for x in explo.values())
                k_ = sum(x[1] for x in explo.values())
                choisi = 1 if k_ > r_ else 0
                for per in (EXPLORATION, VALIDATION):
                    par_bloc = comptes.get((j, c, t, per), {})
                    n, _, taux, bas, haut, p = _taux_et_ic(
                        par_bloc, choisi, tirages[per], per == VALIDATION)
                    ret = sum(x[0] for x in par_bloc.values())
                    cas = sum(x[1] for x in par_bloc.values())
                    lignes.append({
                        "famille": "signe", "nom": signe, "tiers": tiers,
                        "echeance_min": t, "periode": per, "n": n,
                        "retournements": ret, "cassures": cas,
                        "egalites": n - ret - cas,
                        "taux_retournement": ret / n if n else None,
                        "sens_choisi": CASSURE if choisi else RETOURNEMENT,
                        "taux": taux, "ic_bas": bas, "ic_haut": haut,
                        "p": p, "verdict": ""})
    return lignes


# --------------------------------------------------------------------------- #
# Les deux scores
# --------------------------------------------------------------------------- #

def preparation(base: Touchers, explo: Sequence[int]
                ) -> list[tuple[float, float, float, float]]:
    """Par signe : (borne 1 %, borne 99 %, moyenne, écart-type) de
    l'exploration, la moyenne et l'écart-type pris après bornage."""
    sortie = []
    for j in range(len(SIGNES)):
        valeurs = sorted(base.signes[j][r] for r in explo)
        if not valeurs:
            sortie.append((0.0, 0.0, 0.0, 0.0))
            continue
        bas, haut = quantile(valeurs, 1, 100), quantile(valeurs, 99, 100)
        bornees = [min(max(v, bas), haut) for v in valeurs]
        moy = sum(bornees) / len(bornees)
        et = math.sqrt(sum((v - moy) ** 2 for v in bornees) / len(bornees))
        sortie.append((bas, haut, moy, et))
    return sortie


def noms_des_variables(score: str) -> list[str]:
    noms = list(SIGNES) + list(VARIABLES_FIXES)
    if score == "score_croise":
        noms += [f"{SIGNES[a]}×{SIGNES[b]}" for a, b in CROISEMENTS]
    return noms


def variables(base: Touchers, r: int, prep, score: str) -> list[float]:
    z = []
    for j, (bas, haut, moy, et) in enumerate(prep):
        v = min(max(base.signes[j][r], bas), haut)
        z.append((v - moy) / et if et > 0 else 0.0)
    u = base.unite[r]
    x = z + [1.0 if u == 1 else 0.0, 1.0 if u == 2 else 0.0,
             float(base.acheteur[r])]
    if score == "score_croise":
        x += [z[a] * z[b] for a, b in CROISEMENTS]
    return x


class Score:
    """Une régression logistique ajustée en exploration, son seuil de
    confiance, et ce qu'elle joue."""

    def __init__(self, nom: str, echeance: int, prep, modele, seuil: float,
                 n_ajustement: int) -> None:
        self.nom, self.echeance, self.prep = nom, echeance, prep
        self.modele, self.seuil, self.n_ajustement = (modele, seuil,
                                                      n_ajustement)

    def decision(self, x: Sequence[float]) -> int | None:
        """Pour les variables préparées x : +1 retournement, −1 cassure,
        None si la confiance n'atteint pas le seuil."""
        p = self.modele.proba(x)
        if abs(p - 0.5) < self.seuil:
            return None
        return 1 if p > 0.5 else -1


def ajuster_les_scores(base: Touchers, explo: Sequence[int], prep,
                       respirer: Callable[[], None]) -> list[Score]:
    """Les deux scores aux quatre échéances, ajustés sur l'exploration.
    Un score sans deux classes d'issues décisives n'est pas ajusté. Les
    variables d'un toucher ne se calculent qu'une fois par score."""
    sortie: list[Score] = []
    for nom in SCORES:
        lignes = [variables(base, r, prep, nom) for r in explo]
        ajustes = []
        for t in ECHEANCES:
            x_t, y_t = [], []
            for x, r in zip(lignes, explo):
                issue = base.issues[t][r]
                if issue in (1, -1):
                    x_t.append(x)
                    y_t.append(1 if issue == 1 else 0)
            if len(set(y_t)) == 2:
                ajustes.append((t, em.Logistique.ajuster(x_t, y_t,
                                                         lam=LAMBDA),
                                len(y_t)))
            del x_t, y_t
            respirer()
        for t, modele, n_ajustement in ajustes:
            confiances = sorted(abs(modele.proba(x) - 0.5) for x in lignes)
            sortie.append(Score(nom, t, prep, modele,
                                quantile(confiances, 2, 3), n_ajustement))
        del lignes
        respirer()
    return sortie


def cases_des_scores(base: Touchers, scores: Sequence[Score], tirages,
                     respirer: Callable[[], None] = lambda: None
                     ) -> tuple[list[dict[str, Any]], dict]:
    """Deux lignes par score (exploration, en échantillon ; validation) et
    le détail de la validation par paire et par sens prédit."""
    lignes, details = [], {}
    par_periode: dict[int, dict] = {}
    detail: dict[int, dict] = {}
    for k in range(len(scores)):
        par_periode[k] = {EXPLORATION: defaultdict(lambda: [0, 0, 0]),
                          VALIDATION: defaultdict(lambda: [0, 0, 0])}
        detail[k] = defaultdict(lambda: [0, 0])
    for nom in SCORES:
        du_score = [(k, sc) for k, sc in enumerate(scores) if sc.nom == nom]
        if not du_score:
            continue
        prep = du_score[0][1].prep
        for r in range(len(base)):
            x = None
            per = VALIDATION if base.validation[r] else EXPLORATION
            for k, sc in du_score:
                issue = base.issues[sc.echeance][r]
                if issue == ABSENTE:
                    continue
                if x is None:
                    x = variables(base, r, prep, nom)
                cote = sc.decision(x)
                if cote is None:
                    continue
                gagne = issue == cote
                par_periode[k][per][base.bloc[r]][0 if gagne else 1] += 1
                if per == VALIDATION:
                    for cle in (("paire", base.paire_de(r)),
                                ("sens prédit",
                                 RETOURNEMENT if cote > 0 else CASSURE)):
                        detail[k][cle][0] += gagne
                        detail[k][cle][1] += 1
        respirer()
    for k, sc in enumerate(scores):
        details[(sc.nom, sc.echeance)] = detail[k]
        for per in (EXPLORATION, VALIDATION):
            n, gains, taux, bas, haut, p = _taux_et_ic(
                par_periode[k][per], 0, tirages[per], per == VALIDATION)
            lignes.append({
                "famille": "score", "nom": sc.nom, "tiers": "",
                "echeance_min": sc.echeance, "periode": per, "n": n,
                "retournements": None, "cassures": None, "egalites": None,
                "taux_retournement": None, "sens_choisi": "prédit",
                "taux": taux, "ic_bas": bas, "ic_haut": haut, "p": p,
                "verdict": ""})
    return lignes, details


# --------------------------------------------------------------------------- #
# Les verdicts
# --------------------------------------------------------------------------- #

def verdicts(lignes: list[dict[str, Any]]) -> None:
    """Signes : retenue en exploration (≥ 300, ≥ 52,08 %), puis jugée en
    validation (≥ 100). Scores : jugés directement en validation (≥ 100).
    Benjamini-Hochberg à 10 % sur toutes les cases jugées en validation."""
    par_cle = {(l["famille"], l["nom"], l["tiers"], l["echeance_min"],
                l["periode"]): l for l in lignes}
    jugees: dict[str, float] = {}
    cles: dict[str, dict] = {}
    for (fam, nom, tiers, t, per), l in par_cle.items():
        if per != EXPLORATION:
            continue
        val = par_cle[(fam, nom, tiers, t, VALIDATION)]
        if fam == "signe":
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
            if fam == "signe":
                l["verdict"] = "retenue, trop peu en validation"
            continue
        cle = f"{fam}|{nom}|{tiers}|{t}"
        jugees[cle] = val["p"]
        cles[cle] = val
    decouvertes = em.benjamini_hochberg(jugees) if jugees else {}
    for cle, val in cles.items():
        if decouvertes[cle] and val["taux"] > POINT_MORT:
            val["verdict"] = "validée (BH 10 %)"
        else:
            val["verdict"] = ("non reproduite" if val["famille"] == "signe"
                              else "non validé")


def attendues_au_hasard(lignes: Sequence[Mapping[str, Any]]) -> float:
    """Combien de cases de signes passeraient l'exploration si tout était à
    50 % : chacune choisit son meilleur sens, donc sa chance double."""
    total = 0.0
    for l in lignes:
        n = l["n"]
        if (l["famille"] != "signe" or l["periode"] != EXPLORATION
                or n < N_EXPLO_MIN):
            continue
        z = (POINT_MORT - 0.5) * 2 * math.sqrt(n)
        total += min(1.0, 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))))
    return total


# --------------------------------------------------------------------------- #
# L'avancement, pour le bot
# --------------------------------------------------------------------------- #

_ETAT: dict[str, Any] = {}


def avancement() -> str | None:
    """Où en est la passe en cours ; None s'il n'y en a pas."""
    etat = dict(_ETAT)
    if not etat:
        return None
    minutes = (time.monotonic() - etat["debut"]) / 60
    faites, total = etat.get("faites", 0), etat.get("total", 0)
    texte_ = (f"{etat['etape']} ; {faites}/{total} paires ; "
              f"{etat.get('signaux', 0)} touchers ; {minutes:.0f} min")
    debut_paires = etat.get("debut_paires")
    if debut_paires is not None and 0 < faites < total:
        par_paire = (time.monotonic() - debut_paires) / 60 / faites
        texte_ += (f" ; fin des paires dans "
                   f"~{par_paire * (total - faites):.0f} min")
    texte_ += f" ; {memoire.resume()}"
    if etat.get("rss_max"):
        texte_ += f" ; pic de l'étude {etat['rss_max']:.0f} Mo"
    return texte_


def _mesurer() -> None:
    rss = memoire.residente()
    if rss is not None and rss > _ETAT.get("rss_max", 0):
        _ETAT["rss_max"] = rss


# --------------------------------------------------------------------------- #
# La passe en lecture seule
# --------------------------------------------------------------------------- #

def _lire_paire(nom: str, debut: int, fin: int, base, lue, compte: Counter,
                avec_ticks: bool, respirer: Callable[[], None]
                ) -> tuple[list[Toucher], en.Ticks | None, int]:
    """Bougies, payouts et ticks d'UNE paire sur [debut, fin), jour par jour
    (un jour divergent sur l'active), puis ses touchers retenus."""
    bougies: list[en.Bougie] = []
    changements: list[tuple[int, float]] = []
    ligne = lue.execute(PAYOUT_AVANT, (nom, debut)).fetchone()
    if ligne and ligne[1] is not None:
        changements.append((int(ligne[0]), float(ligne[1])))
    for jour in range(debut // 86400, fin // 86400):
        bougies += en._lire_bougies(base("candles", nom, jour), nom, jour,
                                    compte)
        for ts, pct in base("payouts", nom, jour).execute(
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
                    for r in base("tick_paths", nom, jour).execute(
                        en.SQL_CHEMINS, (nom, jour * 86400,
                                         min(fin, (jour + 1) * 86400)))
                    .fetchall()}
        ticks = en.Ticks(charger)
    n_bougies = len(bougies)
    touchers = paire(nom, bougies, fin, ticks, payout_a, compte, respirer)
    return touchers, ticks, n_bougies


def etude(lue_brute, active_brute, course=None, *,
          nom_lecture: str = "relève", maintenant: float | None = None,
          avec_ticks: bool = True,
          respirer: Callable[[], None] | None = None,
          essai: bool = False, version: str | None = None
          ) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers). Les deux bases sont déjà en lecture seule ;
    `active_brute` peut être None (copie non vérifiée). `essai` : le
    contrôle technique, sans aucun taux."""
    em.exiger_le_gel(PARAMETRES, EMPREINTE)
    pause = respirer or (lambda: time.sleep(RESPIRATION_SEC))

    def respirer() -> None:
        _mesurer()
        pause()

    maintenant = time.time() if maintenant is None else maintenant
    t0 = time.monotonic()
    fin = int(maintenant) // 86400 * 86400
    if fin <= FIN_EXPLORATION and not essai:
        return ("🧱 L'exploration court jusqu'au 02/11 : rien à valider "
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


def analyser(base: Touchers, respirer: Callable[[], None], *,
             essai: bool = False) -> dict[str, Any]:
    """Les tiers, les scores, les cases et leurs verdicts. En essai, tout
    s'ajuste sur l'ensemble des touchers, faute d'exploration."""
    explo = list(range(len(base))) if essai else base.indices(EXPLORATION)
    tirages = {}
    for per in (EXPLORATION, VALIDATION):
        blocs = sorted({base.bloc[r] for r in range(len(base))
                        if bool(base.validation[r]) == (per == VALIDATION)})
        tirages[per] = en.Tirages(blocs)
    bornes = bornes_des_tiers(base, explo)
    lignes = cases_des_signes(base, bornes, tirages, respirer)
    prep = preparation(base, explo)
    scores = ajuster_les_scores(base, explo, prep, respirer) if explo else []
    lignes_scores, details_scores = cases_des_scores(base, scores, tirages,
                                                     respirer)
    lignes += lignes_scores
    verdicts(lignes)
    return {"bornes": bornes, "prep": prep, "scores": scores,
            "lignes": lignes, "details_scores": details_scores}


def _etude(lue_brute, active_brute, nom_lecture, fin, avec_ticks, respirer,
           t0, version=None):
    lue, active, lecture, resume_lue, divergents, base_de = en._bases(
        lue_brute, active_brute, nom_lecture, DEBUT, fin)
    paires = sorted({p for (table, p, _) in resume_lue if table == "candles"})
    _ETAT.update(total=len(paires), debut_paires=time.monotonic())
    compte: Counter = Counter()
    base = Touchers()
    decodees = 0
    for rang, nom in enumerate(paires):
        _ETAT.update(etape=f"paire {nom}", faites=rang)
        touchers, ticks, _ = _lire_paire(nom, DEBUT, fin, base_de, lue,
                                         compte, avec_ticks, respirer)
        base.ajouter(touchers)
        _ETAT.update(signaux=base.n_retenus)
        if ticks is not None:
            compte[("données", "chemins illisibles")] += ticks.illisibles
            decodees += ticks.decodees
        del touchers, ticks
        respirer()
    _ETAT.update(etape="statistiques et scores", faites=len(paires))
    resultats = analyser(base, respirer)
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
    return (texte(base, resultats, meta),
            fichiers(base, resultats, resume_lue, compte))


def _essai(lue_brute, active_brute, nom_lecture, fin, avec_ticks, respirer,
           t0, version=None) -> str:
    """Le contrôle technique : la paire la plus fournie en ticks, sur les
    `JOURS_ESSAI` derniers jours complets, par le même code, scores,
    rapport et fichiers compris. Aucun taux n'est montré."""
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
    entete = ("🧱 <b>Essai technique /cassure</b> : il ne compte pas, et "
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
    t1 = time.monotonic()
    # Une journée de plus en amont : les signes lisent 24 h en arrière.
    touchers, ticks, n_bougies = _lire_paire(
        nom, max(DEBUT, debut - 86400), fin, base_de, lue, compte,
        avec_ticks, respirer)
    base = Touchers()
    base.ajouter([x for x in touchers if x.reference_ms >= debut * 1000])
    t_paire = time.monotonic() - t1
    _ETAT.update(etape="essai, statistiques et scores", faites=1,
                 signaux=base.n_retenus)
    try:
        resultats = analyser(base, respirer, essai=True)
        texte(base, resultats, {
            "lecture": lecture, "fin": fin, "paires": [nom],
            "resume": resume_lue, "compte": compte,
            "divergents": len(divergents), "avec_ticks": avec_ticks,
            "decodees": ticks.decodees if ticks else 0, "requetes": 0,
            "refusees": 0, "temps": 0.0})
        tailles = sum(len(c) for c, _, _ in fichiers(
            base, resultats, resume_lue, compte))
        suite = (f"Statistiques, scores ({len(resultats['scores'])} "
                 f"ajustés), rapport et fichiers : passés "
                 f"({tailles // 1024} Kio de fichiers, jetés).")
    except Exception as erreur:                          # noqa: BLE001
        log.exception("Essai /cassure : la suite a échoué")
        suite = ("⚠ Statistiques ou rapport en échec : "
                 + html.escape(f"{type(erreur).__name__}: {erreur}")[:300])
    t_total = time.monotonic() - t0
    jours_essai = max(1, (fin - debut) // 86400)
    jours = max(1, (fin - DEBUT) // 86400)
    n_paires = len(min_bougies)
    projection = (t_paire * n_paires * jours / (jours_essai + 1)
                  + t_resumes * jours / jours_essai) / 60
    rss_depart, rss_max = _ETAT.get("rss_depart"), _ETAT.get("rss_max")
    if rss_depart is not None and rss_max is not None:
        hausse = max(0.0, rss_max - rss_depart) * jours / jours_essai
        memoire_txt = (f"Mémoire du processus : {rss_depart:.0f} Mo au "
                       f"départ, pic {rss_max:.0f} Mo pendant l'essai ; "
                       f"{memoire.resume()}.")
        pic_txt = (f" ; mémoire au plus de l'ordre de {rss_depart:.0f} + "
                   f"{hausse:.0f} Mo (extrapolation prudente)")
    else:
        memoire_txt = f"Mémoire : {memoire.resume()}."
        pic_txt = ""
    mesures = sum(n for cle, n in base.statuts.items()
                  if cle[0] == "mesure" and cle[3] == MESURE)
    ecartes = sum(n for cle, n in base.statuts.items()
                  if cle[0] == "mesure" and cle[3] != MESURE)
    requetes = sum(lue.requetes.values()) + (
        sum(active.requetes.values()) if active is not None else 0)
    refusees = lue.refusees + (active.refusees if active else 0)
    return "\n".join([
        entete,
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in lecture.items())
        + ". Rien n'est écrit en base, aucun ordre.",
        f"Paire essayée : {html.escape(nom)} (la plus fournie en ticks), du "
        f"{en._utc(debut)} au {en._utc(fin - 86400)} ({jours_essai} jours, "
        "et la veille pour les signes).",
        f"Lu : {en._entier(n_bougies)} bougies, minutes décodées "
        f"{en._entier(ticks.decodees if ticks else 0)} "
        f"({ticks.illisibles if ticks else 0} illisibles).",
        f"Touchers retenus : {en._entier(base.n_retenus)} ; jouables et "
        f"mesurés : {en._entier(mesures)} ; écartés faute de fenêtre : "
        f"{en._entier(ecartes)}"
        + ("" if avec_ticks else " ; ⚠ sans les ticks") + ".",
        suite,
        f"Durées : résumés des bases {t_resumes:.0f} s, paire {t_paire:.0f} "
        f"s, total {t_total:.0f} s ; requêtes {requetes}, refusées "
        f"{refusees}.",
        memoire_txt,
        f"Projection prudente de /cassure complet ({n_paires} "
        f"paire{'s' if n_paires > 1 else ''} × {jours} jours) : environ "
        f"{max(1, round(projection))} min" + pic_txt + ".",
        "➡ Si l'essai est passé, lancez /cassure ; un point d'avancement "
        "arrive après 2 minutes, puis toutes les 10 minutes.",
    ])


# --------------------------------------------------------------------------- #
# Le rapport et les fichiers
# --------------------------------------------------------------------------- #

def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}".replace(".", ",")


def texte(base: Touchers, resultats: Mapping, meta: Mapping) -> str:
    lignes_cases = resultats["lignes"]
    fin = meta["fin"]
    resume = meta["resume"]
    paires = meta["paires"]
    minutes_b = sum(v[0] for (t, _, _), v in resume.items()
                    if t == "candles" and v and v[0])
    par_def = Counter()
    for cle, n in base.statuts.items():
        if cle[0] in ("mesure", "jouabilité"):
            par_def[cle[1]] += n
    mesures = Counter()
    ecartes = 0
    for cle, n in base.statuts.items():
        if cle[0] == "mesure":
            if cle[3] == MESURE:
                mesures[cle[2]] += n
            else:
                ecartes += n
    morceaux = [
        "🧱 <b>Cassure ou retournement au toucher d'une zone</b> (zones de "
        "/niveaux, recherche isolée, lecture seule)",
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in meta["lecture"].items())
        + f" ; requêtes refusées : {meta['refusees']}. Rien n'a été écrit "
        "en base, aucun ordre.",
        f"Gel vérifié : empreinte {EMPREINTE[:8]}…",
        f"<b>Données</b> : {len(paires)} "
        f"paire{'s' if len(paires) > 1 else ''}, du {en._utc(DEBUT)} au "
        f"{en._utc(fin - 86400)} ; exploration jusqu'au "
        f"{en._utc(FIN_EXPLORATION - 86400)}, validation ensuite. Minutes de "
        f"bougies (relève) : {en._entier(minutes_b)} ; minutes décodées : "
        f"{en._entier(meta['decodees'])} ; résumés lus sur l'active : "
        f"{meta['divergents']}."
        + ("" if meta["avec_ticks"] else
           " ⚠ Lancée SANS les ticks : aucune issue n'est jugée."),
        f"<b>Touchers</b> retenus : {en._entier(base.n_retenus)} ("
        + ", ".join(f"M{u} {en._entier(par_def[d])}"
                    for u, d in zip(UNITES, DEFINITIONS))
        + f") ; jouables et mesurés : "
        f"{en._entier(mesures[EXPLORATION])} en exploration, "
        f"{en._entier(mesures[VALIDATION])} en validation ; écartés faute "
        f"de fenêtre : {en._entier(ecartes)}.",
        "<b>Chaque signe seul</b> : retournements par tiers bas / milieu / "
        "haut, toutes échéances, exploration → validation :"]
    signes = [l for l in lignes_cases if l["famille"] == "signe"]
    for signe in SIGNES:
        taux = {}
        for per in (EXPLORATION, VALIDATION):
            for tiers in TIERS:
                ret = n = 0
                for l in signes:
                    if (l["nom"], l["tiers"], l["periode"]) == (
                            signe, tiers, per):
                        ret += l["retournements"]
                        n += l["n"]
                taux[(per, tiers)] = ret / n if n else None
        marques = []
        for l in signes:
            if l["nom"] != signe:
                continue
            if l["verdict"] == "validée (BH 10 %)":
                marques.append(f"✅ {l['tiers']} {l['echeance_min']} min "
                               f"{l['sens_choisi']}")
            elif (l["periode"] == EXPLORATION
                  and l["verdict"].startswith("retenue")):
                marques.append(f"★ {l['tiers']} {l['echeance_min']} min "
                               f"{l['sens_choisi']}")
        morceaux.append(
            f"• {LIBELLES[signe]} : "
            + " / ".join(_pct(taux[(EXPLORATION, x)]) for x in TIERS)
            + " → "
            + " / ".join(_pct(taux[(VALIDATION, x)]) for x in TIERS)
            + (f" ; {', '.join(marques)}" if marques else "") + ".")
    morceaux.append("<b>Les scores</b> (le tiers le plus confiant, en "
                    "validation) :")
    scores = {(l["nom"], l["echeance_min"], l["periode"]): l
              for l in lignes_cases if l["famille"] == "score"}
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
    retenues = [l for l in signes if l["periode"] == EXPLORATION
                and l["verdict"].startswith("retenue")]
    validees = [l for l in lignes_cases
                if l["verdict"] == "validée (BH 10 %)"]
    hasard = f"{attendues_au_hasard(lignes_cases):.1f}".replace(".", ",")
    s_r = "s" if len(retenues) > 1 else ""
    s_v = "s" if len(validees) > 1 else ""
    morceaux.append(
        f"<b>Bilan</b> : {len(retenues)} case{s_r} de signes retenue{s_r} "
        f"en exploration (★) sur 120, quand le hasard en donnerait environ "
        f"{hasard} ; {len(validees)} case{s_v} validée{s_v} (✅), scores "
        "compris.")
    for l in validees:
        quoi = (f"{LIBELLES[l['nom']]}, tiers {l['tiers']}"
                if l["famille"] == "signe" else LIBELLES_SCORES[l["nom"]])
        morceaux.append(
            f"• ✅ {quoi}, {l['echeance_min']} min, sens {l['sens_choisi']} : "
            f"{_pct(l['taux'])} % sur {en._entier(l['n'])} en validation "
            f"[{_pct(l.get('ic_bas'))} ; {_pct(l.get('ic_haut'))}].")
    if validees:
        morceaux.append("Une case validée n'est pas une stratégie : elle se "
                        "gèle, puis se confirme sur des jours futurs, sans "
                        "argent réel.")
    else:
        morceaux.append("Aucun signe, seul ou combiné, ne sépare les "
                        "cassures des retournements au-delà du point mort "
                        "de 52,08 % hors échantillon.")
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


COLONNES_CASES = ("famille", "nom", "tiers", "echeance_min", "periode", "n",
                  "retournements", "cassures", "egalites",
                  "taux_retournement", "sens_choisi", "taux", "ic_bas",
                  "ic_haut", "p", "verdict")
COLONNES_DETAILS = ("famille", "nom", "tiers", "echeance_min", "sens_choisi",
                    "periode", "dimension", "valeur", "n", "gains", "taux",
                    "ic_bas", "ic_haut")
COLONNES_SCORES = ("score", "echeance_min", "element", "valeur")
COLONNES_STATUTS = ("source", "definition", "periode", "statut",
                    "echeance_min", "nombre")


def _details(base: Touchers, resultats: Mapping) -> list[dict[str, Any]]:
    """Les cases de signes retenues, par paire et par camp, dans leur sens ;
    les scores, par paire et par sens prédit, en validation."""
    sortie = []
    bornes = resultats["bornes"]
    retenues = [l for l in resultats["lignes"]
                if l["famille"] == "signe" and l["periode"] == EXPLORATION
                and l["verdict"].startswith("retenue")]
    for l in retenues:
        j, c = SIGNES.index(l["nom"]), TIERS.index(l["tiers"])
        t = l["echeance_min"]
        choisi = 1 if l["sens_choisi"] == CASSURE else 0
        comptes: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
        b1, b2 = bornes[j]
        for r in range(len(base)):
            issue = base.issues[t][r]
            if issue == ABSENTE or tiers_de(base.signes[j][r], b1, b2) != c:
                continue
            gagne = (issue < 0) if choisi else (issue > 0)
            per = VALIDATION if base.validation[r] else EXPLORATION
            for cle in ((per, "paire", base.paire_de(r)),
                        (per, "camp", "acheteurs" if base.acheteur[r]
                         else "vendeurs")):
                comptes[cle][0] += gagne
                comptes[cle][1] += 1
        for (per, dim, valeur), (g, n) in sorted(comptes.items()):
            bas, haut = en.wilson(g, n)
            sortie.append({"famille": "signe", "nom": l["nom"],
                           "tiers": l["tiers"], "echeance_min": t,
                           "sens_choisi": l["sens_choisi"], "periode": per,
                           "dimension": dim, "valeur": valeur, "n": n,
                           "gains": g, "taux": g / n if n else None,
                           "ic_bas": bas, "ic_haut": haut})
    for (nom, t), detail in sorted(resultats["details_scores"].items()):
        for (dim, valeur), (g, n) in sorted(detail.items()):
            bas, haut = en.wilson(g, n)
            sortie.append({"famille": "score", "nom": nom, "tiers": "",
                           "echeance_min": t, "sens_choisi": "prédit",
                           "periode": VALIDATION, "dimension": dim,
                           "valeur": valeur, "n": n, "gains": g,
                           "taux": g / n if n else None, "ic_bas": bas,
                           "ic_haut": haut})
    return sortie


def _scores_csv(resultats: Mapping) -> list[dict[str, Any]]:
    sortie = []
    for j, signe in enumerate(SIGNES):
        b1, b2 = resultats["bornes"][j]
        bas, haut, moy, et = resultats["prep"][j]
        for element, valeur in (("tiers_b1", b1), ("tiers_b2", b2),
                                ("borne_1pc", bas), ("borne_99pc", haut),
                                ("moyenne", moy), ("ecart_type", et)):
            sortie.append({"score": "préparation", "echeance_min": "",
                           "element": f"{element}:{signe}",
                           "valeur": f"{valeur:.8g}"})
    for sc in resultats["scores"]:
        coefs = sc.modele.coefficients
        sortie.append({"score": sc.nom, "echeance_min": sc.echeance,
                       "element": "seuil_confiance",
                       "valeur": f"{sc.seuil:.8g}"})
        sortie.append({"score": sc.nom, "echeance_min": sc.echeance,
                       "element": "n_ajustement",
                       "valeur": str(sc.n_ajustement)})
        for nom, c in zip(["interception"] + noms_des_variables(sc.nom),
                          coefs):
            sortie.append({"score": sc.nom, "echeance_min": sc.echeance,
                           "element": f"coef:{nom}", "valeur": f"{c:.8g}"})
    return sortie


def _ligne_statut(cle: tuple, n: int) -> dict[str, Any]:
    if len(cle) == 2:
        valeurs = (cle[0], "", "", cle[1], "")
    elif len(cle) == 3:
        source, definition, quoi = cle
        if quoi in (EXPLORATION, VALIDATION):
            valeurs = (source, definition, quoi, "", "")
        else:
            valeurs = (source, definition, "", quoi, "")
    else:
        valeurs = cle
    return dict(zip(COLONNES_STATUTS, (*valeurs, n)))


def fichiers(base: Touchers, resultats: Mapping, resume, compte: Counter
             ) -> list[tuple[bytes, str, str]]:
    statuts = [_ligne_statut(cle, n) for cle, n in sorted(
        list(compte.items()) + list(base.statuts.items()),
        key=lambda kv: tuple(str(x) for x in kv[0]))]
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
        (en._csv(resultats["lignes"], COLONNES_CASES), "cassure_cases.csv",
         "Une ligne par case (signe × tiers × échéance, ou score × échéance)"
         " et par période : taux, IC, verdict"),
        (en._csv(_details(base, resultats), COLONNES_DETAILS),
         "cassure_details.csv",
         "Les cases de signes retenues par paire et par camp ; les scores "
         "par paire et par sens prédit"),
        (en._csv(_scores_csv(resultats), COLONNES_SCORES),
         "cassure_scores.csv",
         "Bornes des tiers, préparation des variables, coefficients et "
         "seuils des scores"),
        (en._csv(statuts, COLONNES_STATUTS), "cassure_statuts.csv",
         "Zones formées et touchées, épisodes, mesure, statuts des issues"),
        (en._csv(donnees, ("paire", "jour", "minutes_bougies",
                           "minutes_ticks", "releves_payout")),
         "cassure_donnees.csv", "Les données lues sur la relève, par paire "
         "et par jour"),
        (base.signaux_gz(), "cassure_touchers.csv.gz",
         "Chaque toucher retenu, ses dix signes, ses issues et ses statuts"),
    ]
