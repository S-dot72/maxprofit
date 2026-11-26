"""
Les deux règles des vidéos du trader OTC (« El Maestro », lues le 25/11),
confirmées par l'utilisateur : une étude ISOLÉE, en LECTURE SEULE.

Protocole figé : `docs/preinscription_niveaux.json`, empreinte afbcb91e…
La passe refuse de tourner si `PARAMETRES` ne donnent plus cette empreinte.
Elle n'importe ni la course, ni l'exécution des ordres, ni la collecte ; le
décodage des ticks, le bootstrap et la lecture des bases sont ceux, déjà
éprouvés, des études Engulfing et `/classiques`.

--- R1 : l'entrée des vendeurs ou des acheteurs --------------------------------

Un sommet (creux) est une bougie dont le plus haut (bas) dépasse strictement
ceux des w bougies de chaque côté. Le niveau des vendeurs est l'ouverture de
la première bougie rouge à partir du sommet ; celui des acheteurs,
l'ouverture de la première bougie verte à partir du creux. Le niveau devient
actif à la clôture de la bougie de confirmation (w bougies après le pivot),
si le prix est alors du bon côté. Le premier tick qui le touche dans les
24 h déclenche le trade : PUT sur un niveau vendeur, CALL sur un niveau
acheteur. Les « bons niveaux » ne sont pas choisis à l'œil : douze
définitions objectives sont jugées chacune seule (unité M1, M5 ou M15 ;
pivot à 5 ou 10 bougies ; impulsion d'au moins 0 ou 3 étendues médianes).

--- R2 : la continuation après un repli -----------------------------------------

Au moins 3 bougies M1 de même couleur, puis 1 ou 2 bougies contraires, puis
une bougie qui reprend la couleur de la série : entrée dans le sens de la
série, à la clôture de cette bougie.

--- L'exécution, aux ticks --------------------------------------------------------

Entrée au dernier tick connu 2 s après le toucher (R1) ou la décision (R2),
sortie à 1, 2, 3 ou 5 minutes ; ticks datés de moins de 10 s, minutes
continues ; une égalité est une perte ; seules les décisions jouables (flux
≥ 84) comptent ; 5 minutes au moins entre deux trades d'une définition sur
une paire. Sorties en CSV seulement, aucune écriture en base. Rien n'est
ventilé par heure : la confirmation EURJPY 22 h reste aveugle.
"""

from __future__ import annotations

import csv
import gzip
import html
import io
import json
import logging
import math
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from statistics import median
from typing import Any, Callable, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core import memoire
from maxprofit.live import engulfing as en
from maxprofit.live.selection3 import PAYOUT_AVANT, PAYOUTS_DU_JOUR

log = logging.getLogger(__name__)

PARAMETRES: dict[str, Any] = json.loads(r"""
{
  "objet": "Les deux règles des vidéos du trader OTC (« El Maestro », lues le 25/11) que l'utilisateur a confirmées : R1, l'entrée des vendeurs ou des acheteurs au retour du prix sur leur niveau, avec douze définitions objectives des « bons niveaux » ; R2, la continuation après un repli. L'une d'elles bat-elle le point mort de 52,0833 % hors échantillon, exécutée aux ticks ?",
  "demande": "25/11 : « Le 1 et 2 sont correct mais il faut déterminer les bon niveaux on a toujours eu ce problème »",
  "donnees": {
    "bougies": "candles, tf_sec = 60, complete = 1, lues sur la relève ; un jour dont le résumé diffère de l'active est lu sur l'active",
    "ticks": "tick_paths (un chemin par paire et par minute, horodatages à la ms), mêmes règles de copie",
    "payouts": "payouts (points de changement), mêmes règles de copie",
    "paires": "toutes les paires qui ont des bougies M1 dans la période",
    "debut_utc": "2026-10-06T00:00:00Z",
    "fin_exploration_utc": "2026-11-03T00:00:00Z",
    "fin_validation": "début du jour UTC du lancement : seuls des jours complets sont lus"
  },
  "R1_niveaux": {
    "unites_min": [1, 5, 15],
    "regroupement": "une bougie M5 ou M15 regroupe les bougies M1 de sa période (ouverture de la première, plus haut et plus bas de toutes, clôture de la dernière) ; elle n'existe que si toutes ses minutes sont présentes ; les bougies de chaque unité doivent se suivre sans trou pour les fenêtres ci-dessous",
    "pivot": "sommet : bougie dont le plus haut dépasse strictement les plus hauts des w bougies de chaque côté ; creux : bougie dont le plus bas est strictement sous les plus bas des w bougies de chaque côté ; w ∈ {5, 10}",
    "niveau": "vendeurs : ouverture de la première bougie rouge (clôture < ouverture) à partir du sommet, sommet compris, au plus tard à la bougie de confirmation ; acheteurs : ouverture de la première bougie verte à partir du creux ; sans telle bougie, pas de niveau",
    "impulsion": "écart entre le niveau et l'extrême opposé atteint de la bougie du niveau à la bougie de confirmation (plus bas pour un niveau vendeur, plus haut pour un niveau acheteur), en multiples de l'étendue médiane des 50 bougies de la même unité qui précèdent le pivot ; seuils s ∈ {0, 3} : l'écart doit être au moins s fois l'étendue médiane",
    "activation": "à la clôture de la bougie de confirmation, w bougies après le pivot ; la clôture de cette bougie doit être strictement du bon côté (sous un niveau vendeur, au-dessus d'un niveau acheteur), sinon le niveau est écarté",
    "toucher": "premier tick qui atteint le niveau (prix ≥ niveau vendeur, prix ≤ niveau acheteur), horodaté après l'activation et au plus tard 24 h après ; la minute de ce tick est la première bougie M1 dont l'étendue atteint le niveau ; un niveau ne sert qu'une fois",
    "sens": "niveau vendeur → PUT ; niveau acheteur → CALL",
    "definitions": "3 unités × 2 fenêtres × 2 seuils d'impulsion = 12 définitions, chacune jugée seule"
  },
  "R2_continuation": {
    "regle": "bougies M1 : au moins 3 bougies consécutives de même couleur (la série), puis 1 ou 2 bougies de la couleur contraire (le repli), puis une bougie qui clôture dans la couleur de la série (la reprise) ; décision à la clôture de la reprise, dans le sens de la série ; une bougie sans corps (clôture = ouverture) interrompt tout",
    "fenetre": "toutes ces bougies sont des minutes consécutives"
  },
  "execution": {
    "entree_R1": "dernier tick connu au toucher + 2 s",
    "entree_R2": "dernier tick connu à la décision + 2 s",
    "fraicheur": "les ticks d'entrée et de sortie datent de moins de 10 s",
    "sortie": "dernier tick connu à l'entrée + échéance",
    "continuite": "toutes les minutes, de l'entrée à la sortie, doivent avoir leur chemin de ticks",
    "echeances_min": [1, 2, 3, 5],
    "gain": "mouvement strict dans le sens du trade ; une égalité est une perte",
    "jouable": "dernier payout du flux relevé au plus tard à l'entrée ≥ 84, soit 92 % appliqués et un point mort de 52,0833 %"
  },
  "episodes": "dans chaque définition, un trade n'est retenu que si son entrée tombe au moins 5 minutes après l'entrée du trade retenu précédent sur la même paire",
  "sens_choisi": "chaque case (définition × échéance) choisit en exploration entre le sens de la règle et l'inverse, et garde ce choix en validation",
  "selection": {
    "exploration": "jours du 06/10 au 02/11 : une case est retenue si son meilleur sens atteint 52,0833 % avec au moins 300 décisions jouables",
    "validation": "jours du 03/11 à la veille du lancement : chaque case retenue est rejugée dans son sens, contre 52,0833 %, avec au moins 100 décisions ; p unilatérale du bootstrap par blocs paire × jour (1 000 tirages, graine 7) ; Benjamini-Hochberg à 10 % sur les cases rejugées",
    "verdict": "validée si BH la retient et que son taux dépasse 52,0833 % ; sinon non reproduite",
    "confirmation": "une case validée n'est pas une stratégie : elle est gelée, puis confirmée sur des jours postérieurs au lancement, avant tout essai en démo"
  },
  "martingale": "pas une stratégie : le rejeu du 25/11 sur nos issues réelles montre qu'elle ruine 41 à 44 % des campagnes de 50 $ et qu'elle exige, comme la mise fixe, 52,0 % de trades gagnants ; les cases sont jugées à mise fixe",
  "rapport": "aucune ventilation par heure (la confirmation EURJPY 22 h reste aveugle) ; détail par paire et par sens pour les cases retenues",
  "interdits": [
    "lecture seule : aucune écriture en base, sortie en CSV seulement",
    "aucun appel à l'exécution des ordres, aucun changement de la course, de la collecte, de ZoneH1, des échéances ni des configurations",
    "aucun paramètre ne se rechoisit après le gel ; aucune nouvelle définition ne s'ajoute après avoir vu un résultat",
    "aucun argent réel sur une case validée avant sa confirmation, et pas même après sans un essai en démo"
  ]
}
""")
EMPREINTE = "afbcb91e085ae1b00bf6f9fd2a4955b10afbecd680d43582312ed7457d674fa4"

DEBUT = en._instant(PARAMETRES["donnees"]["debut_utc"])
FIN_EXPLORATION = en._instant(PARAMETRES["donnees"]["fin_exploration_utc"])
ECHEANCES: tuple[int, ...] = tuple(PARAMETRES["execution"]["echeances_min"])
POINT_MORT = en.POINT_MORT
FLUX_MIN = en.FLUX_MIN
DELAI_MS = 2_000
FRAICHEUR_MS = 10_000
EPISODE_MS = 300_000
N_EXPLO_MIN = 300
N_VALID_MIN = 100
JOURS_ESSAI = 2
RESPIRATION_SEC = 0.02

UNITES: tuple[int, ...] = tuple(PARAMETRES["R1_niveaux"]["unites_min"])
FENETRES: tuple[int, ...] = (5, 10)
SEUILS: tuple[float, ...] = (0.0, 3.0)
MEDIANE_N = 50
VALIDITE_SEC = 86_400

CALL, PUT = 1, -1
EXPLORATION, VALIDATION = en.EXPLORATION, en.VALIDATION
CONVENTIONNEL, INVERSE = "règle", "inverse"

#: Les treize définitions, dans l'ordre des rapports.
DEFINITIONS: tuple[str, ...] = tuple(
    f"R1_M{u}_w{w}_s{int(s)}" for u in UNITES for w in FENETRES
    for s in SEUILS) + ("R2_continuation",)
R2 = len(DEFINITIONS) - 1


def libelle(definition: str) -> str:
    if definition == "R2_continuation":
        return "R2 continuation après repli"
    _, unite, fenetre, seuil = definition.split("_")
    return (f"R1 niveaux {unite}, pivot {fenetre[1:]}, impulsion "
            f"≥ {seuil[1:]}")


#: Statuts des issues.
ISSUE = "issue"
MANQUE_ENTREE = "ticks manquants à l'entrée"
MANQUE = "ticks manquants"
GELE = "flux gelé à la sortie"

#: Statuts des niveaux (R1) et des épisodes.
TOUCHER = "toucher"
TROU = "trou avant le toucher"
SANS_TOUCHER = "pas de toucher en 24 h"
FIN_DONNEES = "fin des données"
TICKS_TOUCHER = "ticks manquants au toucher"
TOUCHER_SANS_TICK = "toucher sans tick"
TOUCHER_TICK = "toucher au tick"
SANS_TICKS = "sans ticks"
CHEVAUCHE = "chevauche le trade précédent"


# --------------------------------------------------------------------------- #
# Les bougies d'une unité, en colonnes
# --------------------------------------------------------------------------- #

class SerieU:
    """Les bougies d'une unité (60, 300 ou 900 s), en colonnes, avec pour
    chacune l'indice du début de sa suite de bougies consécutives."""

    __slots__ = ("pas", "ts", "o", "h", "l", "c", "debut")

    def __init__(self, bougies: Sequence[en.Bougie], pas: int = 60) -> None:
        self.pas = pas
        self.ts = [b.ts_sec for b in bougies]
        self.o = [b.open for b in bougies]
        self.h = [b.high for b in bougies]
        self.l = [b.low for b in bougies]
        self.c = [b.close for b in bougies]
        self.debut = [0] * len(bougies)
        for i in range(1, len(bougies)):
            self.debut[i] = (self.debut[i - 1]
                             if self.ts[i] == self.ts[i - 1] + pas else i)

    def __len__(self) -> int:
        return len(self.ts)

    def continue_(self, i: int, n: int) -> bool:
        """Les `n` bougies qui finissent en i se suivent-elles ?"""
        return i - self.debut[i] + 1 >= n


def regrouper(bougies: Sequence[en.Bougie], minutes: int
              ) -> list[en.Bougie]:
    """Les bougies M5 ou M15 faites des bougies M1, seulement quand toutes
    les minutes de la période sont là."""
    if minutes == 1:
        return list(bougies)
    pas = 60 * minutes
    sortie, groupe = [], []
    for b in bougies:
        if groupe and b.ts_sec // pas != groupe[0].ts_sec // pas:
            if len(groupe) == minutes:
                sortie.append(_fusion(groupe, pas))
            groupe = []
        groupe.append(b)
    if len(groupe) == minutes:
        sortie.append(_fusion(groupe, pas))
    return sortie


def _fusion(groupe: Sequence[en.Bougie], pas: int) -> en.Bougie:
    return en.Bougie(groupe[0].ts_sec // pas * pas, groupe[0].open,
                     max(b.high for b in groupe), min(b.low for b in groupe),
                     groupe[-1].close)


# --------------------------------------------------------------------------- #
# R1 : les niveaux des vendeurs et des acheteurs
# --------------------------------------------------------------------------- #

class Niveau:
    __slots__ = ("activation", "prix", "sens", "impulsion", "ecart")

    def __init__(self, activation: int, prix: float, sens: int,
                 impulsion: float | None, ecart: float | None = None) -> None:
        self.activation = activation    # secondes, clôture de la confirmation
        self.prix = prix
        self.sens = sens                # PUT : niveau vendeur ; CALL : acheteur
        self.impulsion = impulsion      # en étendues médianes ; None : inconnue
        self.ecart = ecart              # le même écart, en prix (pour /cassure)


def niveaux_de(su: SerieU, w: int) -> list[Niveau]:
    """Tous les niveaux de l'unité pour la fenêtre w, avec leur impulsion ;
    le seuil d'impulsion se choisit ensuite. Aucun ne lit au-delà de sa
    bougie de confirmation."""
    sortie = []
    n = len(su)
    o, h, l, c = su.o, su.h, su.l, su.c
    for j in range(w, n - w):
        hj, lj = h[j], l[j]
        # Les deux voisines d'abord : la plupart des bougies s'arrêtent là.
        sommet = (hj > h[j - 1] and hj > h[j + 1] and hj > max(h[j - w:j])
                  and hj > max(h[j + 1:j + w + 1]))
        creux = (lj < l[j - 1] and lj < l[j + 1] and lj < min(l[j - w:j])
                 and lj < min(l[j + 1:j + w + 1]))
        if not (sommet or creux) or not su.continue_(j + w, 2 * w + 1):
            continue
        med = None
        if j >= MEDIANE_N and su.continue_(j - 1, MEDIANE_N):
            med = median(h[k] - l[k] for k in range(j - MEDIANE_N, j))
        activation = su.ts[j + w] + su.pas
        if sommet:
            k = next((k for k in range(j, j + w + 1) if c[k] < o[k]), None)
            if k is not None:
                prix = o[k]
                ecart = prix - min(l[k:j + w + 1])
                if c[j + w] < prix:
                    sortie.append(Niveau(activation, prix, PUT,
                                         ecart / med if med else None, ecart))
        if creux:
            k = next((k for k in range(j, j + w + 1) if c[k] > o[k]), None)
            if k is not None:
                prix = o[k]
                ecart = max(h[k:j + w + 1]) - prix
                if c[j + w] > prix:
                    sortie.append(Niveau(activation, prix, CALL,
                                         ecart / med if med else None, ecart))
    return sortie


def passe_le_seuil(niveau: Niveau, seuil: float) -> bool:
    if seuil <= 0:
        return True
    return niveau.impulsion is not None and niveau.impulsion >= seuil


def minute_du_toucher(s1: SerieU, niveau: Niveau) -> tuple[int | None, str]:
    """L'indice de la première bougie M1, après l'activation et dans les
    24 h, dont l'étendue atteint le niveau ; sinon pourquoi. Les minutes
    doivent se suivre depuis l'activation : un trou pourrait cacher le
    vrai premier toucher."""
    ts, n = s1.ts, len(s1.ts)
    i = bisect_left(ts, niveau.activation)
    if i >= n:
        return None, FIN_DONNEES
    if ts[i] != niveau.activation:
        return None, TROU
    limite = niveau.activation + VALIDITE_SEC
    prix, vendeur = niveau.prix, niveau.sens == PUT
    extreme = s1.h if vendeur else s1.l
    attendu = niveau.activation
    while i < n:
        t = ts[i]
        if t >= limite:
            return None, SANS_TOUCHER
        if t != attendu:
            return None, TROU
        x = extreme[i]
        if (x >= prix) if vendeur else (x <= prix):
            return i, TOUCHER
        attendu = t + 60
        i += 1
    return None, FIN_DONNEES


def tick_du_toucher(niveau: Niveau, minute_sec: int, ticks: en.Ticks
                    ) -> tuple[int | None, str]:
    d = ticks.minute(minute_sec)
    if d is None:
        return None, TICKS_TOUCHER
    ts, px = d
    seuil_ms = niveau.activation * 1000
    for k in range(len(ts)):
        if ts[k] >= seuil_ms and ((px[k] >= niveau.prix) if niveau.sens == PUT
                                   else (px[k] <= niveau.prix)):
            return ts[k], TOUCHER_TICK
    return None, TOUCHER_SANS_TICK


# --------------------------------------------------------------------------- #
# R2 : la continuation après un repli
# --------------------------------------------------------------------------- #

def couleur(s: SerieU, i: int) -> int:
    return 1 if s.c[i] > s.o[i] else (-1 if s.c[i] < s.o[i] else 0)


def r2_continuation(s1: SerieU) -> list[tuple[int, int]]:
    """(indice de la bougie de reprise, sens de la série)."""
    n = len(s1)
    coul = [couleur(s1, i) for i in range(n)]
    sortie = []
    for i in range(4, n):
        x = coul[i]
        if x == 0:
            continue
        for repli in (1, 2):
            if i - repli - 3 < 0 or not s1.continue_(i, repli + 4):
                continue
            if all(coul[i - q] == -x for q in range(1, repli + 1)) and all(
                    coul[i - repli - q] == x for q in range(1, 4)):
                sortie.append((i, x))
                break
    return sortie


# --------------------------------------------------------------------------- #
# Les trades et leurs issues aux ticks
# --------------------------------------------------------------------------- #

class Signal:
    __slots__ = ("definition", "paire", "reference_ms", "sens", "payout",
                 "issues", "statuts")

    def __init__(self, definition: int, paire: str, reference_ms: int,
                 sens: int) -> None:
        self.definition = definition
        self.paire = paire
        self.reference_ms = reference_ms   # toucher (R1) ou décision (R2)
        self.sens = sens
        self.payout: float | None = None
        self.issues: dict[int, int] = {}
        self.statuts: dict[int, str] = {}

    @property
    def entree_ms(self) -> int:
        return self.reference_ms + DELAI_MS

    @property
    def periode(self) -> str:
        return (EXPLORATION if self.entree_ms < FIN_EXPLORATION * 1000
                else VALIDATION)

    @property
    def jouable(self) -> bool:
        return self.payout is not None and self.payout >= FLUX_MIN


def issues_ticks(sig: Signal, ticks: en.Ticks) -> None:
    t_e = sig.entree_ms
    entree = ticks.prix_a(t_e)
    if entree is None or t_e - entree[0] > FRAICHEUR_MS:
        for t in ECHEANCES:
            sig.statuts[t] = MANQUE_ENTREE
        return
    p_e = entree[1]
    for t in ECHEANCES:
        t_x = t_e + 60_000 * t
        if not ticks.continu(t_e, t_x):
            sig.statuts[t] = MANQUE
            continue
        sortie = ticks.prix_a(t_x)
        if sortie is None or t_x - sortie[0] > FRAICHEUR_MS:
            sig.statuts[t] = GELE
            continue
        ecart = (sortie[1] - p_e) * sig.sens
        sig.issues[t] = 1 if ecart > 0 else (-1 if ecart < 0 else 0)
        sig.statuts[t] = ISSUE


def evenements(nom: str, bougies: Sequence[en.Bougie], compte: Counter,
               respirer: Callable[[], None]
               ) -> tuple[SerieU, list[tuple[int, int, Any]]]:
    """Les candidats d'une paire, triés : (seconde, définition, niveau) pour
    R1, à la minute du toucher ; (seconde, R2, sens) pour R2, à la décision.
    La minute du toucher d'un niveau se cherche une seule fois pour les deux
    seuils d'impulsion."""
    s1 = SerieU(bougies, 60)
    sortie: list[tuple[int, int, Any]] = []
    for u in UNITES:
        su = s1 if u == 1 else SerieU(regrouper(bougies, u), 60 * u)
        for w in FENETRES:
            tous = niveaux_de(su, w)
            touchers = [minute_du_toucher(s1, niv) for niv in tous]
            for s in SEUILS:
                k = DEFINITIONS.index(f"R1_M{u}_w{w}_s{int(s)}")
                for niv, (i, statut) in zip(tous, touchers):
                    if not passe_le_seuil(niv, s):
                        continue
                    compte[("niveaux", DEFINITIONS[k], "formés")] += 1
                    compte[("niveaux", DEFINITIONS[k], statut)] += 1
                    if i is not None:
                        sortie.append((s1.ts[i], k, niv))
            del tous, touchers
            respirer()
    for i, sens in r2_continuation(s1):
        sortie.append((s1.ts[i] + 60, R2, sens))
    sortie.sort(key=lambda e: (e[0], e[1]))
    return s1, sortie


def paire(nom: str, bougies: Sequence[en.Bougie], fin: int,
          ticks: en.Ticks | None, payout_a: Callable[[float], float | None],
          compte: Counter, respirer: Callable[[], None]) -> list[Signal]:
    """Les trades retenus d'une paire, toutes définitions, dans l'ordre des
    entrées, avec leurs issues aux ticks. Une seule passe chronologique :
    chaque jour de ticks n'est chargé qu'une fois. Les candidats d'une même
    définition dans une même minute sont rangés par l'heure de leur tick,
    donc les épisodes se jugent dans l'ordre exact des entrées."""
    _, tries = evenements(nom, bougies, compte, respirer)
    limite_ms = fin * 1000
    duree_ms = DELAI_MS + 60_000 * max(ECHEANCES)
    derniere: dict[int, int] = {}
    retenus: list[Signal] = []
    a, n, groupes = 0, len(tries), 0
    while a < n:
        sec, k = tries[a][0], tries[a][1]
        b = a + 1
        while b < n and tries[b][0] == sec and tries[b][1] == k:
            b += 1
        groupe, a = tries[a:b], b
        definition = DEFINITIONS[k]
        if ticks is not None:
            ticks.oublier_avant(sec)
        if k == R2:
            sigs = [Signal(R2, nom, sec * 1000, x) for _, _, x in groupe]
        elif ticks is None:
            compte[("niveaux", definition, SANS_TICKS)] += len(groupe)
            continue
        else:
            sigs = []
            for _, _, niv in groupe:
                t_ms, statut = tick_du_toucher(niv, sec, ticks)
                compte[("niveaux", definition, statut)] += 1
                if t_ms is not None:
                    sigs.append(Signal(k, nom, t_ms, niv.sens))
            sigs.sort(key=lambda x: x.reference_ms)
        for sig in sigs:
            precedente = derniere.get(k)
            if precedente is not None and \
                    sig.entree_ms < precedente + EPISODE_MS:
                compte[("épisodes", definition, CHEVAUCHE)] += 1
                continue
            derniere[k] = sig.entree_ms
            if sig.reference_ms + duree_ms > limite_ms:
                compte[("épisodes", definition, FIN_DONNEES)] += 1
                continue
            compte[("épisodes", definition, sig.periode)] += 1
            sig.payout = payout_a(sig.entree_ms / 1000)
            if ticks is not None:
                issues_ticks(sig, ticks)
            retenus.append(sig)
        groupes += 1
        if groupes % 500 == 0:
            respirer()
    del tries
    respirer()
    retenus.sort(key=lambda x: (x.reference_ms, x.definition))
    return retenus


# --------------------------------------------------------------------------- #
# Les statistiques, accumulées paire par paire
# --------------------------------------------------------------------------- #

COLONNES_SIGNAUX: tuple[str, ...] = (
    ("definition", "paire", "entree_utc", "reference_ms", "periode", "sens",
     "payout", "jouable")
    + tuple(f"issue_{t}" for t in ECHEANCES)
    + tuple(f"statut_{t}" for t in ECHEANCES))


def _inverse(glt: Sequence[int]) -> list[int]:
    g, l, t = glt
    return [l, g, t]


class Agregats:
    """Les comptes par case (définition × échéance × période) et par bloc
    paire × jour, dans le sens de la règle ; le fichier des trades,
    compressé au fil de l'eau."""

    def __init__(self) -> None:
        self.cellules: dict[tuple, dict[int, list[int]]] = defaultdict(
            lambda: defaultdict(lambda: [0, 0, 0]))
        self.detail: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0])
        self.blocs: dict[tuple[str, int], int] = {}
        self.blocs_par_periode: dict[str, set[int]] = {
            EXPLORATION: set(), VALIDATION: set()}
        self.n_signaux = 0
        self.statuts: Counter = Counter()
        self._tampon = io.BytesIO()
        self._gz = gzip.GzipFile(fileobj=self._tampon, mode="wb", mtime=0)
        self._ecrire([COLONNES_SIGNAUX])

    def _ecrire(self, lignes) -> None:
        texte_ = io.StringIO()
        csv.writer(texte_, delimiter=";").writerows(lignes)
        self._gz.write(texte_.getvalue().encode("utf-8"))

    def ajouter(self, signaux: Sequence[Signal]) -> None:
        lignes = []
        for sig in signaux:
            self.n_signaux += 1
            definition = DEFINITIONS[sig.definition]
            per = sig.periode
            jour = sig.entree_ms // 86_400_000
            bloc = self.blocs.setdefault((sig.paire, jour), len(self.blocs))
            if not sig.jouable:
                self.statuts[("jouabilité", definition, per,
                              "non jouable", "")] += 1
            for t in ECHEANCES:
                self.statuts[("ticks", definition, per,
                              sig.statuts.get(t, "sans ticks"), t)] += 1
                issue = sig.issues.get(t)
                if issue is None or not sig.jouable:
                    continue
                rang = 0 if issue > 0 else (1 if issue < 0 else 2)
                self.cellules[(sig.definition, t, per)][bloc][rang] += 1
                self.blocs_par_periode[per].add(bloc)
                self.detail[(sig.definition, t, per, "paire", sig.paire)][
                    rang] += 1
                self.detail[(sig.definition, t, per, "sens",
                             "CALL" if sig.sens > 0 else "PUT")][rang] += 1
            lignes.append(
                [definition, sig.paire,
                 en._utc(sig.entree_ms // 1000, "%Y-%m-%d %H:%M:%S"),
                 sig.reference_ms, per, "CALL" if sig.sens > 0 else "PUT",
                 "" if sig.payout is None else f"{sig.payout:.0f}",
                 int(sig.jouable)]
                + [sig.issues.get(t, "") for t in ECHEANCES]
                + [sig.statuts.get(t, "") for t in ECHEANCES])
        self._ecrire(lignes)

    def tirages(self) -> dict[str, en.Tirages]:
        return {per: en.Tirages(sorted(blocs))
                for per, blocs in self.blocs_par_periode.items()}

    def total(self, k: int, t: int, per: str) -> list[int]:
        somme = [0, 0, 0]
        for glt in self.cellules.get((k, t, per), {}).values():
            for j in range(3):
                somme[j] += glt[j]
        return somme

    def cases(self, tirages: Mapping[str, en.Tirages],
              respirer: Callable[[], None]) -> list[dict[str, Any]]:
        lignes = []
        for k, definition in enumerate(DEFINITIONS):
            for t in ECHEANCES:
                g, l, e = self.total(k, t, EXPLORATION)
                sens = INVERSE if (g + l + e) and l > g else CONVENTIONNEL
                for per in (EXPLORATION, VALIDATION):
                    glt = self.total(k, t, per)
                    gc, lc, ec = glt
                    nn = gc + lc + ec
                    choisi = glt if sens == CONVENTIONNEL else _inverse(glt)
                    taux = choisi[0] / nn if nn else None
                    par_bloc = self.cellules.get((k, t, per), {})
                    gb = {b: float(x[0] if sens == CONVENTIONNEL else x[1])
                          for b, x in par_bloc.items()}
                    nb = {b: float(sum(x)) for b, x in par_bloc.items()}
                    bas = haut = p = None
                    if nn and taux is not None:
                        bas, haut, p = tirages[per].ratio(gb, nb, taux)
                    lignes.append({
                        "definition": definition, "echeance_min": t,
                        "periode": per, "n": nn, "gains_regle": gc,
                        "pertes_regle": lc, "egalites": ec,
                        "taux_regle": gc / nn if nn else None,
                        "taux_inverse": lc / nn if nn else None,
                        "sens_choisi": sens, "taux": taux,
                        "ic_bas": bas, "ic_haut": haut,
                        "p": p if per == VALIDATION else None,
                        "verdict": ""})
                respirer()
        _verdicts(lignes)
        return lignes

    def details(self, retenues: Sequence[tuple[str, int, str]]
                ) -> list[dict[str, Any]]:
        lignes = []
        choix = {(d, t): sens for d, t, sens in retenues}
        for (k, t, per, dim, valeur), glt in sorted(self.detail.items()):
            definition = DEFINITIONS[k]
            sens = choix.get((definition, t))
            if sens is None:
                continue
            g, l, e = glt if sens == CONVENTIONNEL else _inverse(glt)
            n = g + l + e
            bas, haut = en.wilson(g, n)
            lignes.append({"definition": definition, "echeance_min": t,
                           "sens_choisi": sens, "periode": per,
                           "dimension": dim, "valeur": valeur, "n": n,
                           "gains": g, "taux": g / n if n else None,
                           "ic_bas": bas, "ic_haut": haut})
        return lignes

    def signaux_gz(self) -> bytes:
        self._gz.close()
        return self._tampon.getvalue()


def attendues_au_hasard(lignes: Sequence[Mapping[str, Any]]) -> float:
    """Combien de cases passeraient l'exploration si tout était à 50 % :
    chacune choisit son meilleur sens, donc sa chance double."""
    total = 0.0
    for l in lignes:
        n = l["n"]
        if l["periode"] != EXPLORATION or n < N_EXPLO_MIN:
            continue
        z = (POINT_MORT - 0.5) * 2 * math.sqrt(n)
        total += min(1.0, 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))))
    return total


def _verdicts(lignes: list[dict[str, Any]]) -> None:
    par_cle = {(l["definition"], l["echeance_min"], l["periode"]): l
               for l in lignes}
    jugees = {}
    for (d, t, per), l in par_cle.items():
        if per != EXPLORATION:
            continue
        val = par_cle[(d, t, VALIDATION)]
        if l["n"] < N_EXPLO_MIN:
            l["verdict"] = "trop peu en exploration"
        elif (l["taux"] or 0) < POINT_MORT:
            l["verdict"] = "non retenue en exploration"
        elif val["n"] < N_VALID_MIN or val.get("p") is None:
            l["verdict"] = "retenue, trop peu en validation"
            val["verdict"] = "trop peu en validation"
        else:
            l["verdict"] = "retenue en exploration"
            jugees[f"{d}|{t}"] = val["p"]
    decouvertes = em.benjamini_hochberg(jugees) if jugees else {}
    for cle, p in jugees.items():
        d, t = cle.split("|")
        val = par_cle[(d, int(t), VALIDATION)]
        val["verdict"] = ("validée (BH 10 %)" if decouvertes[cle]
                          and val["taux"] > POINT_MORT else "non reproduite")


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
              f"{etat.get('signaux', 0)} trades ; {minutes:.0f} min")
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
                ) -> tuple[list[Signal], en.Ticks | None, int]:
    """Bougies, payouts et ticks d'UNE paire sur [debut, fin), jour par jour
    (un jour divergent sur l'active), puis ses trades retenus."""
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
    signaux = paire(nom, bougies, fin, ticks, payout_a, compte, respirer)
    return signaux, ticks, n_bougies


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
        return ("📏 L'exploration court jusqu'au 02/11 : rien à valider "
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


def _etude(lue_brute, active_brute, nom_lecture, fin, avec_ticks, respirer,
           t0, version=None):
    lue, active, lecture, resume_lue, divergents, base = en._bases(
        lue_brute, active_brute, nom_lecture, DEBUT, fin)
    paires = sorted({p for (table, p, _) in resume_lue if table == "candles"})
    _ETAT.update(total=len(paires), debut_paires=time.monotonic())
    compte: Counter = Counter()
    agregats = Agregats()
    decodees = 0
    for rang, nom in enumerate(paires):
        _ETAT.update(etape=f"paire {nom}", faites=rang)
        signaux, ticks, _ = _lire_paire(nom, DEBUT, fin, base, lue, compte,
                                        avec_ticks, respirer)
        agregats.ajouter(signaux)
        _ETAT.update(signaux=agregats.n_signaux)
        if ticks is not None:
            compte[("données", "chemins illisibles")] += ticks.illisibles
            decodees += ticks.decodees
        del signaux, ticks
        respirer()
    _ETAT.update(etape="statistiques", faites=len(paires))
    tirages = agregats.tirages()
    lignes_cases = agregats.cases(tirages, respirer)
    retenues = [(l["definition"], l["echeance_min"], l["sens_choisi"])
                for l in lignes_cases if l["periode"] == EXPLORATION
                and l["verdict"].startswith("retenue")]
    lignes_details = agregats.details(retenues)
    _ETAT.update(etape="rapport et fichiers")
    meta = {"nom": nom_lecture, "lecture": lecture, "fin": fin,
            "paires": paires, "resume": resume_lue, "compte": compte,
            "divergents": len(divergents), "avec_ticks": avec_ticks,
            "decodees": decodees,
            "requetes": sum(lue.requetes.values()) + (
                sum(active.requetes.values()) if active is not None else 0),
            "refusees": lue.refusees + (active.refusees if active else 0),
            "temps": time.monotonic() - t0, "version": version,
            "rss_depart": _ETAT.get("rss_depart"),
            "rss_max": _ETAT.get("rss_max")}
    rapport = texte(agregats, lignes_cases, meta)
    return rapport, fichiers(agregats, lignes_cases, lignes_details,
                             resume_lue, compte)


def _essai(lue_brute, active_brute, nom_lecture, fin, avec_ticks, respirer,
           t0, version=None) -> str:
    """Le contrôle technique : la paire la plus fournie en ticks, sur les
    `JOURS_ESSAI` derniers jours complets, par le même code, statistiques,
    rapport et fichiers compris. Aucun taux n'est montré."""
    debut = max(DEBUT, fin - JOURS_ESSAI * 86400)
    lue, active, lecture, resume_lue, divergents, base = en._bases(
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
    entete = ("📏 <b>Essai technique /niveaux</b> : il ne compte pas, et "
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
    signaux, ticks, n_bougies = _lire_paire(nom, debut, fin, base, lue,
                                            compte, avec_ticks, respirer)
    agregats = Agregats()
    agregats.ajouter(signaux)
    t_paire = time.monotonic() - t1
    _ETAT.update(etape="essai, statistiques", faites=1,
                 signaux=agregats.n_signaux)
    try:
        tirages = agregats.tirages()
        lignes_cases = agregats.cases(tirages, respirer)
        lignes_details = agregats.details(
            [(d, t, CONVENTIONNEL) for d in DEFINITIONS for t in ECHEANCES])
        texte(agregats, lignes_cases, {
            "nom": nom_lecture, "lecture": lecture, "fin": fin,
            "paires": [nom], "resume": resume_lue, "compte": compte,
            "divergents": len(divergents), "avec_ticks": avec_ticks,
            "decodees": ticks.decodees if ticks else 0, "requetes": 0,
            "refusees": 0, "temps": 0.0})
        tailles = sum(len(c) for c, _, _ in fichiers(
            agregats, lignes_cases, lignes_details, resume_lue, compte))
        suite = (f"Statistiques, rapport et fichiers : passés "
                 f"({tailles // 1024} Kio de fichiers, jetés).")
    except Exception as erreur:                          # noqa: BLE001
        log.exception("Essai /niveaux : la suite a échoué")
        suite = ("⚠ Statistiques ou rapport en échec : "
                 + html.escape(f"{type(erreur).__name__}: {erreur}")[:300])
    t_total = time.monotonic() - t0
    jours_essai = max(1, (fin - debut) // 86400)
    jours = max(1, (fin - DEBUT) // 86400)
    n_paires = len(min_bougies)
    projection = (t_paire * n_paires * jours / jours_essai
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
    niveaux_formes = sum(n for cle, n in compte.items()
                         if cle[0] == "niveaux" and cle[2] == "formés")
    touches = sum(n for cle, n in compte.items()
                  if cle[0] == "niveaux" and cle[2] == TOUCHER)
    touches_tick = sum(n for cle, n in compte.items()
                       if cle[0] == "niveaux" and cle[2] == TOUCHER_TICK)
    requetes = sum(lue.requetes.values()) + (
        sum(active.requetes.values()) if active is not None else 0)
    refusees = lue.refusees + (active.refusees if active else 0)
    return "\n".join([
        entete,
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in lecture.items())
        + ". Rien n'est écrit en base, aucun ordre.",
        f"Paire essayée : {html.escape(nom)} (la plus fournie en ticks), du "
        f"{en._utc(debut)} au {en._utc(fin - 86400)} ({jours_essai} jours).",
        f"Lu : {en._entier(n_bougies)} bougies, "
        f"{en._entier(min_ticks[nom])} minutes de ticks dont "
        f"{en._entier(ticks.decodees if ticks else 0)} décodées "
        f"({ticks.illisibles if ticks else 0} illisibles).",
        f"Niveaux formés (toutes définitions) : {en._entier(niveaux_formes)}"
        f", touchés : {en._entier(touches)} à la bougie, "
        f"{en._entier(touches_tick)} au tick ; trades retenus : "
        f"{en._entier(agregats.n_signaux)}"
        + ("" if avec_ticks else " ; ⚠ sans les ticks") + ".",
        suite,
        f"Durées : résumés des bases {t_resumes:.0f} s, paire {t_paire:.0f} "
        f"s, total {t_total:.0f} s ; requêtes {requetes}, refusées "
        f"{refusees}.",
        memoire_txt,
        f"Projection prudente de /niveaux complet ({n_paires} "
        f"paire{'s' if n_paires > 1 else ''} × {jours} jours) : environ "
        f"{max(1, round(projection))} min" + pic_txt + ".",
        "➡ Si l'essai est passé, lancez /niveaux ; un point d'avancement "
        "arrive après 2 minutes, puis toutes les 10 minutes.",
    ])


# --------------------------------------------------------------------------- #
# Le rapport et les fichiers
# --------------------------------------------------------------------------- #

def texte(agregats: Agregats, lignes_cases, meta: Mapping) -> str:
    fin = meta["fin"]
    compte: Counter = meta["compte"]
    resume = meta["resume"]
    minutes_b = sum(v[0] for (t, _, _), v in resume.items()
                    if t == "candles" and v and v[0])
    minutes_t = sum(v[0] for (t, _, _), v in resume.items()
                    if t == "tick_paths" and v and v[0])
    par_cle = {(l["definition"], l["echeance_min"], l["periode"]): l
               for l in lignes_cases}
    paires = meta["paires"]
    morceaux = [
        "📏 <b>Niveaux des vendeurs et des acheteurs, continuation après "
        "repli</b> (règles des vidéos, recherche isolée, lecture seule)",
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in meta["lecture"].items())
        + f" ; requêtes refusées : {meta['refusees']}. Rien n'a été écrit "
        "en base, aucun ordre.",
        f"Gel vérifié : empreinte {EMPREINTE[:8]}…",
        f"<b>Données</b> : {len(paires)} "
        f"paire{'s' if len(paires) > 1 else ''}, du "
        f"{en._utc(DEBUT)} au {en._utc(fin - 86400)} ; exploration jusqu'au "
        f"{en._utc(FIN_EXPLORATION - 86400)}, validation ensuite. Minutes de "
        f"bougies : {en._entier(minutes_b)} ; minutes de ticks : "
        f"{en._entier(minutes_t)} ; minutes décodées : "
        f"{en._entier(meta['decodees'])} ; jours lus sur l'active : "
        f"{meta['divergents']}."
        + ("" if meta["avec_ticks"] else
           " ⚠ Lancée SANS les ticks : aucune issue n'est jugée."),
        "<b>Par définition</b> (sens choisi en exploration, décisions "
        "jouables, exécution aux ticks ; exploration → validation) :"]
    for definition in DEFINITIONS:
        n_trades = sum(compte[("épisodes", definition, p)]
                       for p in (EXPLORATION, VALIDATION))
        cases = []
        for t in ECHEANCES:
            x = par_cle.get((definition, t, EXPLORATION), {})
            v = par_cle.get((definition, t, VALIDATION), {})
            marque = ("✅" if v.get("verdict", "").startswith("validée")
                      else "★" if x.get("verdict", "").startswith("retenue")
                      else "")
            inv = " inv." if x.get("sens_choisi") == INVERSE else ""
            cases.append(f"{t} min{inv} {en._pct(x.get('taux'))} → "
                         f"{en._pct(v.get('taux'))}{marque}")
        morceaux.append(f"• {libelle(definition)} ({en._entier(n_trades)} "
                        f"trades) : " + " ; ".join(cases) + ".")
    retenues = [l for l in lignes_cases if l["periode"] == EXPLORATION
                and l["verdict"].startswith("retenue")]
    validees = [l for l in lignes_cases
                if l["verdict"] == "validée (BH 10 %)"]
    reproduites = [l for l in lignes_cases
                   if l["verdict"] == "non reproduite"]
    hasard = f"{attendues_au_hasard(lignes_cases):.1f}".replace(".", ",")
    morceaux.append(
        f"<b>Cases</b> (13 définitions × 4 échéances) : {len(retenues)} "
        f"retenues en exploration (★), {len(validees)} validées (✅), "
        f"{len(reproduites)} non reproduites. Si tout était à 50 %, environ "
        f"{hasard} cases passeraient l'exploration par hasard, chacune "
        "choisissant son meilleur sens.")
    for l in validees:
        morceaux.append(
            f"• ✅ {libelle(l['definition'])} à {l['echeance_min']} min, "
            f"sens {l['sens_choisi']} : {en._pct(l['taux'])} sur {l['n']} en "
            f"validation [{en._pct(l.get('ic_bas'))} ; "
            f"{en._pct(l.get('ic_haut'))}].")
    if validees:
        morceaux.append("Une case validée n'est pas une stratégie : elle se "
                        "gèle, puis se confirme sur des jours futurs, sans "
                        "argent réel.")
    else:
        morceaux.append("Aucune définition des niveaux, ni la continuation "
                        "après repli, ne bat le point mort de 52,08 % hors "
                        "échantillon.")
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


COLONNES_CASES = ("definition", "echeance_min", "periode", "n",
                  "gains_regle", "pertes_regle", "egalites", "taux_regle",
                  "taux_inverse", "sens_choisi", "taux", "ic_bas", "ic_haut",
                  "p", "verdict")
COLONNES_DETAILS = ("definition", "echeance_min", "sens_choisi", "periode",
                    "dimension", "valeur", "n", "gains", "taux", "ic_bas",
                    "ic_haut")
COLONNES_STATUTS = ("source", "definition", "periode", "statut",
                    "echeance_min", "nombre")


def _ligne_statut(cle: tuple, n: int) -> dict[str, Any]:
    """Les clés de comptage (longueurs 2, 3 ou 5) en une ligne de
    `niveaux_statuts.csv`."""
    if len(cle) == 2:                 # ("données", « bougie illisible »…)
        valeurs = (cle[0], "", "", cle[1], "")
    elif len(cle) == 3:               # (source, définition, statut/période)
        source, definition, quoi = cle
        if quoi in (EXPLORATION, VALIDATION):
            valeurs = (source, definition, quoi, "", "")
        else:
            valeurs = (source, definition, "", quoi, "")
    else:
        valeurs = cle
    return dict(zip(COLONNES_STATUTS, (*valeurs, n)))


def fichiers(agregats: Agregats, lignes_cases, lignes_details, resume,
             compte: Counter) -> list[tuple[bytes, str, str]]:
    statuts = [_ligne_statut(cle, n) for cle, n in sorted(
        list(compte.items()) + list(agregats.statuts.items()),
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
        (en._csv(lignes_cases, COLONNES_CASES), "niveaux_cases.csv",
         "Une ligne par définition × échéance × période : sens choisi, taux, "
         "IC, verdict"),
        (en._csv(lignes_details, COLONNES_DETAILS), "niveaux_details.csv",
         "Les cases retenues, par paire et par sens"),
        (en._csv(statuts, COLONNES_STATUTS), "niveaux_statuts.csv",
         "Niveaux formés, touchés, écartés ; épisodes ; statuts des issues"),
        (en._csv(donnees, ("paire", "jour", "minutes_bougies",
                           "minutes_ticks", "releves_payout")),
         "niveaux_donnees.csv", "Les données lues, par paire et par jour"),
        (agregats.signaux_gz(), "niveaux_signaux.csv.gz",
         "Chaque trade retenu, ses issues aux ticks et ses statuts"),
    ]
