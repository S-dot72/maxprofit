"""
Six stratégies CLASSIQUES des options binaires sur les paires OTC (demande
du 17/11) : une étude ISOLÉE, en LECTURE SEULE.

Protocole figé : `docs/preinscription_classiques.json`, empreinte 31e85658…
La passe refuse de tourner si `PARAMETRES` ne donnent plus cette empreinte.
Elle n'importe ni la course, ni l'exécution des ordres, ni la collecte ; le
décodage des ticks, le bootstrap et la lecture des bases sont ceux, déjà
éprouvés, de l'étude Engulfing (`live.engulfing`).

--- Les six familles ------------------------------------------------------------

Réglages standards des plateformes, sans aucun réglage fin :
- F1 RSI(14) sortie de zone (70 / 30), retournement ;
- F2 Bollinger(20, 2) + RSI(14) extrême, retournement ;
- F3 stochastique lent (14, 3, 3), croisement au-delà de 80 / en deçà de 20 ;
- F4 pin bar (mèche ≥ 2/3, corps ≤ 1/3) au plus haut / plus bas de 20 ;
- F5 chiffre rond (pas 0,005 ou 0,5), premier contact en 60 bougies, rejeté ;
- F6 cassure du canal des 20 bougies précédentes, continuation.
Les indicateurs ont les définitions de `maxprofit.indicators` (RSI de
Cutler, écart-type de population, stochastique lent) : les tests le
vérifient valeur par valeur. Leur fenêtre doit être faite de minutes
consécutives.

--- L'exécution, aux ticks --------------------------------------------------------

Décision à la clôture de la bougie qui déclenche ; entrée au dernier tick
connu 2 s plus tard ; sortie à l'échéance (1, 2, 3, 5 min). Les deux ticks
datent de moins de 10 s, et chaque minute entre les deux a ses ticks. Une
égalité est une perte. Seules les décisions jouables (flux ≥ 84) comptent.
Dans une famille, deux décisions retenues sur une paire sont séparées d'au
moins 5 minutes : les trades ne se chevauchent pas.

Sorties en CSV seulement, aucune écriture en base. Rien n'est ventilé par
heure : la confirmation EURJPY 22 h reste aveugle.
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
from bisect import bisect_right
from collections import Counter, defaultdict
from typing import Any, Callable, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core import memoire
from maxprofit.live import engulfing as en
from maxprofit.live.selection3 import PAYOUT_AVANT, PAYOUTS_DU_JOUR

log = logging.getLogger(__name__)

PARAMETRES: dict[str, Any] = json.loads(r"""
{
  "objet": "Six stratégies classiques des options binaires, aux réglages standards des plateformes, jamais testées sur nos données OTC : l'une d'elles bat-elle le point mort de 52,0833 % hors échantillon, exécutée aux ticks ?",
  "demande": "17/11 : « tu vas tester une nouvelle stratégie sur les paires otc »",
  "donnees": {
    "bougies": "candles, tf_sec = 60, complete = 1, lues sur la relève ; un jour dont le résumé diffère de l'active est lu sur l'active",
    "ticks": "tick_paths (un chemin par paire et par minute, horodatages à la ms), mêmes règles de copie",
    "payouts": "payouts (points de changement), mêmes règles de copie",
    "paires": "toutes les paires qui ont des bougies M1 dans la période",
    "debut_utc": "2026-10-06T00:00:00Z",
    "fin_exploration_utc": "2026-11-03T00:00:00Z",
    "fin_validation": "début du jour UTC du lancement : seuls des jours complets sont lus"
  },
  "fenetres": "chaque indicateur ne lit que la bougie de décision et celles d'avant ; sa fenêtre doit être faite de minutes consécutives, sans trou, sinon il n'y a pas de signal",
  "familles": {
    "F1_rsi_sortie": "RSI(14) à moyennes simples (Cutler, comme maxprofit.indicators.oscillators.rsi) : il passe de ≥ 70 à < 70 → PUT ; de ≤ 30 à > 30 → CALL",
    "F2_bollinger_rsi": "Bollinger(20, 2), écart-type de population (comme maxprofit.indicators.structure.bollinger_bandes), et RSI(14) : clôture au-dessus de la bande haute et RSI ≥ 70 → PUT ; clôture sous la bande basse et RSI ≤ 30 → CALL",
    "F3_stochastique_croisement": "stochastique lent (14, 3, 3) (comme maxprofit.indicators.oscillators.stochastique_lente) : %K passe sous %D alors que %K et %D étaient tous deux au-dessus de 80 à la bougie précédente → PUT ; %K passe au-dessus de %D alors que tous deux étaient sous 20 → CALL",
    "F4_pin_bar": "étendue e > 0, corps ≤ e/3 ; mèche haute ≥ 2e/3 et plus haut des 20 dernières bougies (la bougie comprise) → PUT ; mèche basse ≥ 2e/3 et plus bas des 20 dernières bougies → CALL",
    "F5_chiffre_rond": "pas de 0,005 si la clôture précédente est sous 20, de 0,5 sinon ; la bougie touche un seul multiple du pas (bas ≤ niveau ≤ haut), qu'aucune des 60 bougies précédentes n'a touché ; venue d'en dessous (clôture précédente < niveau) et clôture sous le niveau → PUT ; venue d'au-dessus et clôture au-dessus → CALL",
    "F6_cassure_canal": "clôture au-dessus du plus haut des 20 bougies précédentes → CALL ; sous leur plus bas → PUT"
  },
  "sens": "chaque famille a son sens conventionnel ci-dessus ; en exploration, chaque case (famille × échéance) choisit entre ce sens et l'inverse, et garde ce choix en validation",
  "execution": {
    "decision": "à la clôture de la bougie qui déclenche (fin de sa minute)",
    "entree": "dernier tick connu à la décision + 2 s, daté de moins de 10 s",
    "sortie": "dernier tick connu à l'entrée + échéance, daté de moins de 10 s",
    "continuite": "toutes les minutes, de l'entrée à la sortie, doivent avoir leur chemin de ticks",
    "echeances_min": [1, 2, 3, 5],
    "gain": "mouvement strict dans le sens du trade ; une égalité est une perte",
    "jouable": "dernier payout du flux relevé au plus tard à la décision ≥ 84, soit 92 % appliqués et un point mort de 52,0833 %"
  },
  "episodes": "dans chaque famille, un signal n'est retenu que si sa décision tombe au moins 5 minutes après la décision du signal retenu précédent sur la même paire : les trades d'une famille ne se chevauchent jamais",
  "selection": {
    "exploration": "jours du 06/10 au 02/11 : une case est retenue si son meilleur sens atteint 52,0833 % avec au moins 300 décisions jouables",
    "validation": "jours du 03/11 à la veille du lancement : chaque case retenue est rejugée dans son sens, contre 52,0833 %, avec au moins 100 décisions ; p unilatérale du bootstrap par blocs paire × jour (1 000 tirages, graine 7) ; Benjamini-Hochberg à 10 % sur les cases rejugées",
    "verdict": "validée si BH la retient et que son taux dépasse 52,0833 % ; sinon non reproduite",
    "confirmation": "une case validée n'est pas une stratégie : elle est gelée, puis confirmée sur des jours postérieurs au lancement, avec le jour pour unité, avant tout essai en démo"
  },
  "rapport": "aucune ventilation par heure (la confirmation EURJPY 22 h reste aveugle) ; détail par paire et par sens pour les cases retenues",
  "interdits": [
    "lecture seule : aucune écriture en base, sortie en CSV seulement",
    "aucun appel à l'exécution des ordres, aucun changement de la course, de la collecte, de ZoneH1, des échéances ni des configurations",
    "aucun paramètre ne se rechoisit après le gel ; aucune nouvelle famille ne s'ajoute après avoir vu un résultat",
    "aucun argent réel sur une case validée avant sa confirmation, et pas même après sans un essai en démo"
  ]
}
""")
EMPREINTE = "31e8565808605ceda03db67bd7f6cba2dec96c7dee62392b2d4d997b3fd85889"

DEBUT = en._instant(PARAMETRES["donnees"]["debut_utc"])
FIN_EXPLORATION = en._instant(PARAMETRES["donnees"]["fin_exploration_utc"])
ECHEANCES: tuple[int, ...] = tuple(PARAMETRES["execution"]["echeances_min"])
POINT_MORT = en.POINT_MORT
FLUX_MIN = en.FLUX_MIN
DELAI_MS = 2_000
FRAICHEUR_MS = 10_000
EPISODE_SEC = 300
N_EXPLO_MIN = 300
N_VALID_MIN = 100
JOURS_ESSAI = 2
RESPIRATION_SEC = 0.02

CALL, PUT = 1, -1
EXPLORATION, VALIDATION = en.EXPLORATION, en.VALIDATION
CONVENTIONNEL, INVERSE = "conventionnel", "inverse"

RSI_P, RSI_HAUT, RSI_BAS = 14, 70.0, 30.0
BB_P, BB_K = 20, 2.0
STOCH_K, STOCH_LISSAGE, STOCH_D = 14, 3, 3
STOCH_HAUT, STOCH_BAS = 80.0, 20.0
PIN_FENETRE = 20
ROND_SEUIL, ROND_PAS_BAS, ROND_PAS_HAUT, ROND_MEMOIRE = 20.0, 0.005, 0.5, 60
CANAL = 20

FAMILLES: tuple[str, ...] = tuple(PARAMETRES["familles"])
LIBELLES = {
    "F1_rsi_sortie": "F1 RSI, sortie de zone",
    "F2_bollinger_rsi": "F2 Bollinger + RSI",
    "F3_stochastique_croisement": "F3 stochastique, croisement",
    "F4_pin_bar": "F4 pin bar",
    "F5_chiffre_rond": "F5 chiffre rond",
    "F6_cassure_canal": "F6 cassure de canal",
}

#: Statuts d'une issue aux ticks.
ISSUE = "issue"
MANQUE_ENTREE = "ticks manquants à l'entrée"
MANQUE = "ticks manquants"
GELE = "flux gelé à la sortie"
ABSENTE = 9            # issue absente, dans les tableaux compacts


# --------------------------------------------------------------------------- #
# Une paire en colonnes
# --------------------------------------------------------------------------- #

class Serie:
    """Les bougies M1 complètes d'une paire, en colonnes, avec pour chaque
    bougie l'indice du début de sa suite de minutes consécutives."""

    __slots__ = ("ts", "o", "h", "l", "c", "debut")

    def __init__(self, bougies: Sequence[en.Bougie]) -> None:
        self.ts = [b.ts_sec for b in bougies]
        self.o = [b.open for b in bougies]
        self.h = [b.high for b in bougies]
        self.l = [b.low for b in bougies]
        self.c = [b.close for b in bougies]
        self.debut = [0] * len(bougies)
        for i in range(1, len(bougies)):
            self.debut[i] = (self.debut[i - 1]
                             if self.ts[i] == self.ts[i - 1] + 60 else i)

    def __len__(self) -> int:
        return len(self.ts)

    def continue_(self, i: int, n: int) -> bool:
        """Les `n` bougies qui finissent en i sont-elles consécutives ?"""
        return i - self.debut[i] + 1 >= n


# --------------------------------------------------------------------------- #
# Les indicateurs, aux définitions de maxprofit.indicators
# --------------------------------------------------------------------------- #

def rsi_serie(s: Serie, periode: int = RSI_P) -> list[float | None]:
    """RSI de Cutler en chaque bougie : mêmes opérations, dans le même ordre,
    que `oscillators.rsi` sur la fenêtre des `periode + 1` dernières."""
    c = s.c
    sortie: list[float | None] = [None] * len(c)
    for i in range(periode, len(c)):
        if not s.continue_(i, periode + 1):
            continue
        hausses = baisses = 0.0
        for k in range(i - periode + 1, i + 1):
            ecart = c[k] - c[k - 1]
            if ecart > 0:
                hausses += ecart
            else:
                baisses -= ecart
        if hausses + baisses:
            sortie[i] = 100 * hausses / (hausses + baisses)
    return sortie


def bollinger_en(s: Serie, i: int, periode: int = BB_P,
                 ecarts: float = BB_K) -> tuple[float, float] | None:
    """(basse, haute) en i, comme `structure.bollinger_bandes`."""
    if i + 1 < periode or not s.continue_(i, periode):
        return None
    fenetre = s.c[i - periode + 1:i + 1]
    moyenne = sum(fenetre) / periode
    variance = sum((x - moyenne) ** 2 for x in fenetre) / periode
    ecart = variance ** 0.5
    return moyenne - ecarts * ecart, moyenne + ecarts * ecart


def stochastique_serie(s: Serie, periode_k: int = STOCH_K,
                       lissage_k: int = STOCH_LISSAGE,
                       periode_d: int = STOCH_D
                       ) -> list[tuple[float, float] | None]:
    """(%K lent, %D) en chaque bougie, comme `oscillators.stochastique_lente`
    (mêmes opérations, même ordre)."""
    n = len(s)
    bruts: list[float | None] = [None] * n
    for i in range(periode_k - 1, n):
        if not s.continue_(i, periode_k):
            continue
        haut = max(s.h[i - periode_k + 1:i + 1])
        bas = min(s.l[i - periode_k + 1:i + 1])
        if haut != bas:
            bruts[i] = (s.c[i] - bas) / (haut - bas) * 100
    besoin = periode_k + lissage_k + periode_d - 2
    sortie: list[tuple[float, float] | None] = [None] * n
    for i in range(besoin - 1, n):
        if not s.continue_(i, besoin):
            continue
        fenetre = bruts[i - (lissage_k + periode_d - 2):i + 1]
        if any(x is None for x in fenetre):
            continue
        lents = [sum(fenetre[k:k + lissage_k]) / lissage_k
                 for k in range(periode_d)]
        sortie[i] = (lents[-1], sum(lents) / periode_d)
    return sortie


def pas_rond(prix: float) -> float:
    return ROND_PAS_HAUT if prix >= ROND_SEUIL else ROND_PAS_BAS


def niveaux_touches(bas: float, haut: float, pas: float) -> list[float]:
    """Les multiples du pas entre bas et haut, bornes comprises."""
    k0 = math.ceil(round(bas / pas, 9))
    k1 = math.floor(round(haut / pas, 9))
    return [round(k * pas, 10) for k in range(k0, k1 + 1)]


# --------------------------------------------------------------------------- #
# Les déclencheurs : (indice de la bougie de décision, sens conventionnel)
# --------------------------------------------------------------------------- #

def f1_rsi_sortie(s: Serie, rsi: Sequence[float | None]) -> list[tuple]:
    sortie = []
    for i in range(1, len(s)):
        a, b = rsi[i - 1], rsi[i]
        if a is None or b is None or not s.continue_(i, RSI_P + 2):
            continue
        if a >= RSI_HAUT > b:
            sortie.append((i, PUT))
        elif a <= RSI_BAS < b:
            sortie.append((i, CALL))
    return sortie


def f2_bollinger_rsi(s: Serie, rsi: Sequence[float | None]) -> list[tuple]:
    sortie = []
    for i in range(len(s)):
        r = rsi[i]
        if r is None or RSI_BAS < r < RSI_HAUT:
            continue
        bandes = bollinger_en(s, i)
        if bandes is None:
            continue
        basse, haute = bandes
        if r >= RSI_HAUT and s.c[i] > haute:
            sortie.append((i, PUT))
        elif r <= RSI_BAS and s.c[i] < basse:
            sortie.append((i, CALL))
    return sortie


def f3_stochastique(s: Serie, sto: Sequence[tuple[float, float] | None]
                    ) -> list[tuple]:
    sortie = []
    for i in range(1, len(s)):
        a, b = sto[i - 1], sto[i]
        if a is None or b is None or not s.continue_(i, 19):
            continue
        (ka, da), (kb, db) = a, b
        if ka >= da and kb < db and min(ka, da) > STOCH_HAUT:
            sortie.append((i, PUT))
        elif ka <= da and kb > db and max(ka, da) < STOCH_BAS:
            sortie.append((i, CALL))
    return sortie


def f4_pin_bar(s: Serie) -> list[tuple]:
    sortie = []
    o, h, l, c = s.o, s.h, s.l, s.c
    for i in range(PIN_FENETRE - 1, len(s)):
        e = h[i] - l[i]
        if e <= 0 or 3 * abs(c[i] - o[i]) > e:
            continue
        haute = h[i] - max(o[i], c[i])
        basse = min(o[i], c[i]) - l[i]
        if 3 * haute >= 2 * e:
            if (s.continue_(i, PIN_FENETRE)
                    and h[i] >= max(h[i - PIN_FENETRE + 1:i + 1])):
                sortie.append((i, PUT))
        elif 3 * basse >= 2 * e:
            if (s.continue_(i, PIN_FENETRE)
                    and l[i] <= min(l[i - PIN_FENETRE + 1:i + 1])):
                sortie.append((i, CALL))
    return sortie


def f5_chiffre_rond(s: Serie) -> list[tuple]:
    sortie = []
    h, l, c = s.h, s.l, s.c
    for i in range(ROND_MEMOIRE, len(s)):
        niveaux = niveaux_touches(l[i], h[i], pas_rond(c[i - 1]))
        if len(niveaux) != 1 or not s.continue_(i, ROND_MEMOIRE + 1):
            continue
        niveau = niveaux[0]
        if any(l[j] <= niveau <= h[j] for j in range(i - ROND_MEMOIRE, i)):
            continue
        if c[i - 1] < niveau and c[i] < niveau:
            sortie.append((i, PUT))
        elif c[i - 1] > niveau and c[i] > niveau:
            sortie.append((i, CALL))
    return sortie


def f6_cassure_canal(s: Serie) -> list[tuple]:
    sortie = []
    h, l, c = s.h, s.l, s.c
    for i in range(CANAL, len(s)):
        if not s.continue_(i, CANAL + 1):
            continue
        if c[i] > max(h[i - CANAL:i]):
            sortie.append((i, CALL))
        elif c[i] < min(l[i - CANAL:i]):
            sortie.append((i, PUT))
    return sortie


def declencheurs(s: Serie) -> dict[str, list[tuple]]:
    """Tous les déclenchements bruts de la paire, par famille."""
    rsi = rsi_serie(s)
    sto = stochastique_serie(s)
    return {
        "F1_rsi_sortie": f1_rsi_sortie(s, rsi),
        "F2_bollinger_rsi": f2_bollinger_rsi(s, rsi),
        "F3_stochastique_croisement": f3_stochastique(s, sto),
        "F4_pin_bar": f4_pin_bar(s),
        "F5_chiffre_rond": f5_chiffre_rond(s),
        "F6_cassure_canal": f6_cassure_canal(s),
    }


# --------------------------------------------------------------------------- #
# Les signaux retenus et leurs issues
# --------------------------------------------------------------------------- #

class Signal:
    __slots__ = ("famille", "paire", "decision", "direction", "payout",
                 "issues", "statuts")

    def __init__(self, famille: int, paire: str, decision: int,
                 direction: int) -> None:
        self.famille = famille
        self.paire = paire
        self.decision = decision           # fin de la bougie, en secondes
        self.direction = direction
        self.payout: float | None = None
        self.issues: dict[int, int] = {}
        self.statuts: dict[int, str] = {}

    @property
    def periode(self) -> str:
        return EXPLORATION if self.decision < FIN_EXPLORATION else VALIDATION

    @property
    def jouable(self) -> bool:
        return self.payout is not None and self.payout >= FLUX_MIN


def episodes(bruts: Sequence[tuple], s: Serie) -> list[tuple]:
    """Les déclenchements retenus d'une famille : 5 minutes au moins entre
    deux décisions retenues."""
    gardes, derniere = [], None
    for i, sens in bruts:
        decision = s.ts[i] + 60
        if derniere is None or decision >= derniere + EPISODE_SEC:
            gardes.append((i, sens))
            derniere = decision
    return gardes


def issues_ticks(sig: Signal, ticks: en.Ticks) -> None:
    t_e = sig.decision * 1000 + DELAI_MS
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
        ecart = (sortie[1] - p_e) * sig.direction
        sig.issues[t] = 1 if ecart > 0 else (-1 if ecart < 0 else 0)
        sig.statuts[t] = ISSUE


def paire(nom: str, bougies: Sequence[en.Bougie], fin: int,
          ticks: en.Ticks | None, payout_a: Callable[[float], float | None],
          compte: Counter, respirer: Callable[[], None]) -> list[Signal]:
    """Les signaux retenus d'une paire, toutes familles, dans l'ordre des
    décisions, avec leurs issues aux ticks."""
    s = Serie(bougies)
    bruts = declencheurs(s)
    respirer()
    retenus: list[Signal] = []
    limite_ms = fin * 1000
    for k, famille in enumerate(FAMILLES):
        for i, sens in bruts[famille]:
            per = (EXPLORATION if s.ts[i] + 60 < FIN_EXPLORATION
                   else VALIDATION)
            compte[("déclenchements", famille, per)] += 1
        for i, sens in episodes(bruts[famille], s):
            sig = Signal(k, nom, s.ts[i] + 60, sens)
            if sig.decision * 1000 + DELAI_MS + 60_000 * max(ECHEANCES) \
                    > limite_ms:
                compte[("épisodes", famille, "fin des données")] += 1
                continue
            compte[("épisodes", famille, sig.periode)] += 1
            retenus.append(sig)
    del bruts
    retenus.sort(key=lambda x: (x.decision, x.famille))
    for rang, sig in enumerate(retenus):
        sig.payout = payout_a(sig.decision)
        if ticks is not None:
            ticks.oublier_avant(sig.decision)
            issues_ticks(sig, ticks)
        if rang % 500 == 499:
            respirer()
    respirer()
    return retenus


# --------------------------------------------------------------------------- #
# Les statistiques, accumulées paire par paire
# --------------------------------------------------------------------------- #

COLONNES_SIGNAUX: tuple[str, ...] = (
    ("famille", "paire", "decision_utc", "decision_ts", "periode",
     "direction", "payout", "jouable")
    + tuple(f"issue_{t}" for t in ECHEANCES)
    + tuple(f"statut_{t}" for t in ECHEANCES))


def _inverse(glt: Sequence[int]) -> list[int]:
    g, l, t = glt
    return [l, g, t]


class Agregats:
    """Les comptes par case (famille × échéance × période) et par bloc
    paire × jour, dans le sens conventionnel ; le fichier des signaux,
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
        texte = io.StringIO()
        csv.writer(texte, delimiter=";").writerows(lignes)
        self._gz.write(texte.getvalue().encode("utf-8"))

    def ajouter(self, signaux: Sequence[Signal]) -> None:
        lignes = []
        for sig in signaux:
            self.n_signaux += 1
            famille = FAMILLES[sig.famille]
            per = sig.periode
            jour = sig.decision // 86400
            cle_bloc = (sig.paire, jour)
            bloc = self.blocs.setdefault(cle_bloc, len(self.blocs))
            if not sig.jouable:
                self.statuts[("jouabilité", famille, per, "non jouable")] += 1
            for t in ECHEANCES:
                self.statuts[("ticks", famille, per,
                              sig.statuts.get(t, "sans ticks"), t)] += 1
                issue = sig.issues.get(t)
                if issue is None or not sig.jouable:
                    continue
                rang = 0 if issue > 0 else (1 if issue < 0 else 2)
                self.cellules[(sig.famille, t, per)][bloc][rang] += 1
                self.blocs_par_periode[per].add(bloc)
                self.detail[(sig.famille, t, per, "paire", sig.paire)][
                    rang] += 1
                self.detail[(sig.famille, t, per, "direction",
                             "CALL" if sig.direction > 0 else "PUT")][
                    rang] += 1
            lignes.append(
                [famille, sig.paire, en._utc(sig.decision, "%Y-%m-%d %H:%M"),
                 sig.decision, per, "CALL" if sig.direction > 0 else "PUT",
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
        for k, famille in enumerate(FAMILLES):
            for t in ECHEANCES:
                g, l, e = self.total(k, t, EXPLORATION)
                n = g + l + e
                sens = (INVERSE if n and l > g else CONVENTIONNEL)
                for per in (EXPLORATION, VALIDATION):
                    glt = self.total(k, t, per)
                    gc, lc, ec = glt
                    nn = gc + lc + ec
                    choisi = glt if sens == CONVENTIONNEL else _inverse(glt)
                    taux = choisi[0] / nn if nn else None
                    par_bloc = self.cellules.get((k, t, per), {})
                    gb = {b: float(v[0] if sens == CONVENTIONNEL else v[1])
                          for b, v in par_bloc.items()}
                    nb = {b: float(sum(v)) for b, v in par_bloc.items()}
                    bas = haut = p = None
                    if nn and taux is not None:
                        bas, haut, p = tirages[per].ratio(gb, nb, taux)
                    lignes.append({
                        "famille": famille, "echeance_min": t,
                        "periode": per, "n": nn, "gains_conv": gc,
                        "pertes_conv": lc, "egalites": ec,
                        "taux_conventionnel": gc / nn if nn else None,
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
        choix = {(f, t): sens for f, t, sens in retenues}
        for (k, t, per, dim, valeur), glt in sorted(
                self.detail.items(), key=lambda kv: (kv[0][0], kv[0][1],
                                                     kv[0][2], kv[0][3],
                                                     kv[0][4])):
            famille = FAMILLES[k]
            sens = choix.get((famille, t))
            if sens is None:
                continue
            g, l, e = glt if sens == CONVENTIONNEL else _inverse(glt)
            n = g + l + e
            bas, haut = en.wilson(g, n)
            lignes.append({"famille": famille, "echeance_min": t,
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
    par_cle = {(l["famille"], l["echeance_min"], l["periode"]): l
               for l in lignes}
    jugees = {}
    for (f, t, per), l in par_cle.items():
        if per != EXPLORATION:
            continue
        val = par_cle[(f, t, VALIDATION)]
        if l["n"] < N_EXPLO_MIN:
            l["verdict"] = "trop peu en exploration"
        elif (l["taux"] or 0) < POINT_MORT:
            l["verdict"] = "non retenue en exploration"
        elif val["n"] < N_VALID_MIN or val.get("p") is None:
            l["verdict"] = "retenue, trop peu en validation"
            val["verdict"] = "trop peu en validation"
        else:
            l["verdict"] = "retenue en exploration"
            jugees[f"{f}|{t}"] = val["p"]
    decouvertes = em.benjamini_hochberg(jugees) if jugees else {}
    for cle, p in jugees.items():
        f, t = cle.split("|")
        val = par_cle[(f, int(t), VALIDATION)]
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
    texte = (f"{etat['etape']} ; {faites}/{total} paires ; "
             f"{etat.get('signaux', 0)} signaux ; {minutes:.0f} min")
    debut_paires = etat.get("debut_paires")
    if debut_paires is not None and 0 < faites < total:
        par_paire = (time.monotonic() - debut_paires) / 60 / faites
        texte += f" ; fin des paires dans ~{par_paire * (total - faites):.0f} min"
    texte += f" ; {memoire.resume()}"
    if etat.get("rss_max"):
        texte += f" ; pic de l'étude {etat['rss_max']:.0f} Mo"
    return texte


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
    (un jour divergent sur l'active), puis ses signaux retenus."""
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
    contrôle technique d'une minute, sans aucun taux."""
    em.exiger_le_gel(PARAMETRES, EMPREINTE)
    pause = respirer or (lambda: time.sleep(RESPIRATION_SEC))

    def respirer() -> None:
        _mesurer()
        pause()

    maintenant = time.time() if maintenant is None else maintenant
    t0 = time.monotonic()
    fin = int(maintenant) // 86400 * 86400
    if fin <= FIN_EXPLORATION and not essai:
        return ("🧪 L'exploration court jusqu'au 02/11 : rien à valider "
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
    retenues = [(l["famille"], l["echeance_min"], l["sens_choisi"])
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
    rapport et fichiers compris. Seuls des comptes, des durées et la mémoire
    sont montrés : aucun taux."""
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
    entete = ("🧪 <b>Essai technique /classiques</b> : il ne compte pas, et "
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
            [(f, t, CONVENTIONNEL) for f in FAMILLES for t in ECHEANCES])
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
        log.exception("Essai /classiques : la suite a échoué")
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
    par_famille = " ; ".join(
        f"{LIBELLES[f].split(' ')[0]} "
        f"{sum(n for cle, n in compte.items() if len(cle) == 3 and cle[0] == 'déclenchements' and cle[1] == f)}"
        for f in FAMILLES)
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
        f"Déclenchements bruts : {par_famille} ; signaux retenus : "
        f"{en._entier(agregats.n_signaux)}"
        + ("" if avec_ticks else " ; ⚠ sans les ticks") + ".",
        suite,
        f"Durées : résumés des bases {t_resumes:.0f} s, paire {t_paire:.0f} "
        f"s, total {t_total:.0f} s ; requêtes {requetes}, refusées "
        f"{refusees}.",
        memoire_txt,
        f"Projection prudente de /classiques complet ({n_paires} "
        f"paire{'s' if n_paires > 1 else ''} × {jours} jours) : environ "
        f"{max(1, round(projection))} min" + pic_txt + ".",
        "➡ Si l'essai est passé, lancez /classiques ; un point d'avancement "
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
    par_cle = {(l["famille"], l["echeance_min"], l["periode"]): l
               for l in lignes_cases}
    morceaux = [
        "🧪 <b>Six stratégies classiques sur l'OTC</b> (recherche isolée, "
        "lecture seule)",
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in meta["lecture"].items())
        + f" ; requêtes refusées : {meta['refusees']}. Rien n'a été écrit "
        "en base, aucun ordre.",
        f"Gel vérifié : empreinte {EMPREINTE[:8]}…",
        f"<b>Données</b> : {len(meta['paires'])} "
        f"paire{'s' if len(meta['paires']) > 1 else ''}, du "
        f"{en._utc(DEBUT)} au {en._utc(fin - 86400)} ; exploration jusqu'au "
        f"{en._utc(FIN_EXPLORATION - 86400)}, validation ensuite. Minutes de "
        f"bougies : {en._entier(minutes_b)} ; minutes de ticks : "
        f"{en._entier(minutes_t)} ; minutes décodées : "
        f"{en._entier(meta['decodees'])} ; jours lus sur l'active : "
        f"{meta['divergents']}."
        + ("" if meta["avec_ticks"] else
           " ⚠ Lancée SANS les ticks : aucune issue n'est jugée."),
        "<b>Par famille</b> (sens choisi en exploration, décisions jouables, "
        "exécution aux ticks ; exploration → validation) :"]
    for famille in FAMILLES:
        episodes_ = sum(compte[("épisodes", famille, p)]
                        for p in (EXPLORATION, VALIDATION))
        cases = []
        for t in ECHEANCES:
            x = par_cle.get((famille, t, EXPLORATION), {})
            v = par_cle.get((famille, t, VALIDATION), {})
            marque = ("✅" if v.get("verdict", "").startswith("validée")
                      else "★" if x.get("verdict", "").startswith("retenue")
                      else "")
            inv = " inv." if x.get("sens_choisi") == INVERSE else ""
            cases.append(f"{t} min{inv} {en._pct(x.get('taux'))} → "
                         f"{en._pct(v.get('taux'))}{marque}")
        morceaux.append(f"• {LIBELLES[famille]} ({en._entier(episodes_)} "
                        f"signaux) : " + " ; ".join(cases) + ".")
    retenues = [l for l in lignes_cases if l["periode"] == EXPLORATION
                and l["verdict"].startswith("retenue")]
    validees = [l for l in lignes_cases
                if l["verdict"] == "validée (BH 10 %)"]
    reproduites = [l for l in lignes_cases
                   if l["verdict"] == "non reproduite"]
    hasard = f"{attendues_au_hasard(lignes_cases):.1f}".replace(".", ",")
    morceaux.append(
        f"<b>Cases</b> (6 familles × 4 échéances) : {len(retenues)} retenues "
        f"en exploration (★), {len(validees)} validées (✅), "
        f"{len(reproduites)} non reproduites. Si tout était à 50 %, environ "
        f"{hasard} cases passeraient l'exploration par hasard, chacune "
        "choisissant son meilleur sens.")
    for l in validees:
        morceaux.append(
            f"• ✅ {LIBELLES[l['famille']]} à {l['echeance_min']} min, sens "
            f"{l['sens_choisi']} : {en._pct(l['taux'])} sur {l['n']} en "
            f"validation [{en._pct(l.get('ic_bas'))} ; "
            f"{en._pct(l.get('ic_haut'))}].")
    if validees:
        morceaux.append("Une case validée n'est pas une stratégie : elle se "
                        "gèle, puis se confirme sur des jours futurs, sans "
                        "argent réel.")
    else:
        morceaux.append("Aucune de ces six stratégies ne bat le point mort de "
                        "52,08 % hors échantillon.")
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


COLONNES_CASES = ("famille", "echeance_min", "periode", "n", "gains_conv",
                  "pertes_conv", "egalites", "taux_conventionnel",
                  "taux_inverse", "sens_choisi", "taux", "ic_bas", "ic_haut",
                  "p", "verdict")
COLONNES_DETAILS = ("famille", "echeance_min", "sens_choisi", "periode",
                    "dimension", "valeur", "n", "gains", "taux", "ic_bas",
                    "ic_haut")


def fichiers(agregats: Agregats, lignes_cases, lignes_details, resume,
             compte: Counter) -> list[tuple[bytes, str, str]]:
    statuts = []
    for cle, n in sorted(compte.items(), key=lambda kv: tuple(
            str(x) for x in kv[0])):
        if len(cle) == 2:              # ("données", « bougie illisible »…)
            statuts.append({"source": cle[0], "famille": "", "periode": "",
                            "statut": cle[1], "echeance_min": "",
                            "nombre": n})
        elif len(cle) == 3:
            statuts.append({"source": cle[0], "famille": cle[1],
                            "periode": cle[2], "statut": "",
                            "echeance_min": "", "nombre": n})
    for cle, n in sorted(agregats.statuts.items(), key=lambda kv: tuple(
            str(x) for x in kv[0])):
        source, famille, per, statut = cle[:4]
        statuts.append({"source": source, "famille": famille,
                        "periode": per, "statut": statut,
                        "echeance_min": cle[4] if len(cle) > 4 else "",
                        "nombre": n})
    donnees = []
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
        (en._csv(lignes_cases, COLONNES_CASES), "classiques_cases.csv",
         "Une ligne par famille × échéance × période : sens choisi, taux, "
         "IC, verdict"),
        (en._csv(lignes_details, COLONNES_DETAILS), "classiques_details.csv",
         "Les cases retenues, par paire et par sens"),
        (en._csv(statuts, ("source", "famille", "periode", "statut",
                           "echeance_min", "nombre")),
         "classiques_statuts.csv",
         "Déclenchements, épisodes, statuts des issues aux ticks"),
        (en._csv(donnees, ("paire", "jour", "minutes_bougies",
                           "minutes_ticks", "releves_payout")),
         "classiques_donnees.csv", "Les données lues, par paire et par jour"),
        (agregats.signaux_gz(), "classiques_signaux.csv.gz",
         "Chaque signal retenu, ses issues aux ticks et ses statuts"),
    ]
