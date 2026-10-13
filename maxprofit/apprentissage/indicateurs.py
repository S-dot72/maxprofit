"""
EXP-002 / EXP-003 — ce que RSI, MACD et ATR disent de la suite, sans en
faire des règles.

--- D'où elle vient --------------------------------------------------------

Protocole de simulation OTC, version 1 (2026-10-13) : « mesurer si le RSI,
le MACD ou les mesures de volatilité contiennent une information
statistique supplémentaire sur les mouvements futurs, sans les transformer
automatiquement en règles d'entrée », puis « comparer les distributions de
résultats, la couverture des observations et la stabilité temporelle entre
les données associées à ZoneH1 et celles associées aux indicateurs ».

--- Deux populations -------------------------------------------------------

1. TOUTES les bougies M1 rejouées (cet accumulateur) : à la clôture de
   chaque bougie dont la fenêtre est entière, l'état des indicateurs, et
   la position qu'il suggérerait jugée à 5 et 15 minutes. C'est ce qui
   répond à « l'indicateur seul contient-il de l'information ? ».
2. Les entrées ZoneH1 confirmées (`indicateurs_a_l_entree`, appelée par le
   rejeu) : les mêmes indicateurs À L'ENTRÉE, versés aux expériences
   contrôlées. C'est ce qui répond à « apportent-ils quelque chose de PLUS
   que ZoneH1 ? ».

Ni l'une ni l'autre ne touche à une règle : ces valeurs sont hors de
`CARACTERISTIQUES`, les leçons ne les voient pas, et la course ne les lit
jamais.

--- Les hypothèses, fixées AVANT de regarder -------------------------------

Seuils classiques, choisis avant toute mesure et jamais ajustés : RSI(14)
à 30 et 70, signe de l'histogramme MACD(12, 26, 9), croisement de la ligne
et du signal, rapport ATR(14) / ATR(60) à 0,8 et 1,25. Un seuil choisi
après avoir regardé où le résultat est le meilleur ne mesurerait plus
rien.

Les témoins — toutes les bougies, RSI entre 30 et 70, les trois régimes
d'ATR — ne parient sur aucun sens : on y lit la part des hausses, qui doit
rester près de 50 %. Un témoin loin de 50 % dirait que la mesure elle-même
est biaisée.

--- Ce qui rend la mesure honnête ------------------------------------------

- Causal : les indicateurs sont lus sur les bougies closes jusqu'à t
  inclus, l'issue strictement après. Le calcul au fil de l'eau fait
  EXACTEMENT les mêmes opérations que les fonctions pures de
  `indicators.oscillators` (vérifié par les tests).
- Une fenêtre trouée (minute absente ou incomplète) remet le calcul à
  zéro : l'observation manque plutôt que d'être réparée.
- Les observations voisines ne sont pas indépendantes (un RSI sous 30 dure
  plusieurs minutes, et les issues à 15 min se chevauchent) : les comptes
  sont gardés par bloc (paire, jour), pour un intervalle par blocs.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Sequence

from maxprofit.core.types import Candle
from maxprofit.indicators.oscillators import (
    MEMOIRE_EXPO, atr, besoin_macd, expo_tronquee, macd, poids_expo, rsi)

PERIODE_RSI = 14
RSI_BAS, RSI_HAUT = 30.0, 70.0
PERIODE_ATR = 14
PERIODE_ATR_LONGUE = 60
ATR_CALME, ATR_AGITE = 0.8, 1.25
RAPIDE, LENTE, SIGNAL = 12, 26, 9
ECHEANCES: tuple[int, ...] = (300, 900)
FENETRE_JOURS = 7

#: (clé, libellé, sens parié : +1 CALL, −1 PUT, 0 témoin = part des hausses).
#: Les libellés n'ont ni « < » ni « > » : ils partent en HTML vers Telegram.
CASES: tuple[tuple[str, str, int], ...] = (
    ("temoin", "toutes les bougies (témoin)", 0),
    ("rsi_bas", "RSI ≤ 30 → CALL", 1),
    ("rsi_haut", "RSI ≥ 70 → PUT", -1),
    ("rsi_milieu", "RSI entre 30 et 70 (témoin)", 0),
    ("macd_pos", "histogramme MACD positif → CALL", 1),
    ("macd_neg", "histogramme MACD négatif → PUT", -1),
    ("croise_haut", "croisement MACD haussier → CALL", 1),
    ("croise_bas", "croisement MACD baissier → PUT", -1),
    ("atr_calme", "ATR14/ATR60 sous 0,8 (témoin)", 0),
    ("atr_normal", "ATR14/ATR60 de 0,8 à 1,25 (témoin)", 0),
    ("atr_agite", "ATR14/ATR60 au-dessus de 1,25 (témoin)", 0),
)

#: Les hypothèses jugées : des cases réunies en un pari.
PARIS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("H2-RSI", "RSI(14) aux extrêmes → retour à la moyenne "
               "(≤ 30 CALL, ≥ 70 PUT)", ("rsi_bas", "rsi_haut")),
    ("H2-MACD", "histogramme MACD → suivre l'élan (positif CALL, "
                "négatif PUT)",
     ("macd_pos", "macd_neg")),
    ("H2-CROISEMENT", "croisement MACD/signal → dans son sens",
     ("croise_haut", "croise_bas")),
)

#: Les clés posées à l'entrée par `indicateurs_a_l_entree` (préfixées
#: « e_ » par le rejeu) : la fiche CSV de chaque signal les reprend.
CLES_A_L_ENTREE: tuple[str, ...] = (
    "rsi_14", "rsi_sens", "macd_ligne", "macd_signal", "macd_hist",
    "macd_sens", "atr_14", "atr_pct")

_W_LENTE = MEMOIRE_EXPO * LENTE
_W_SIGNAL = MEMOIRE_EXPO * SIGNAL


def _contigues(completes: Sequence[Candle], j: int,
               n: int) -> Sequence[Candle] | None:
    """Les `n` bougies finissant à `j`, si elles se suivent sans trou."""
    if j + 1 < n:
        return None
    fenetre = completes[j + 1 - n:j + 1]
    if fenetre[-1].ts_sec - fenetre[0].ts_sec != 60 * (n - 1):
        return None
    return fenetre


def indicateurs_a_l_entree(completes: Sequence[Candle], j: int,
                           call: bool) -> dict[str, float]:
    """RSI, MACD et ATR à la clôture de `completes[j]`, bruts (pour la fiche
    du signal) et orientés dans le sens du trade (pour les expériences).

    `rsi_sens` : le RSI pour un CALL, 100 − RSI pour un PUT — bas, le prix
    vient d'aller CONTRE le trade (survente au support, surachat à la
    résistance). `macd_sens` : l'histogramme en ATR, positif quand l'élan
    va dans le sens du trade. Une valeur dont la fenêtre est trouée manque.
    """
    sortie: dict[str, float] = {}
    fenetre = _contigues(completes, j, PERIODE_RSI + 1)
    r = rsi(fenetre, PERIODE_RSI) if fenetre else None
    if r is not None:
        sortie["rsi_14"] = round(r, 2)
        sortie["rsi_sens"] = round(r if call else 100 - r, 2)
    fenetre = _contigues(completes, j, PERIODE_ATR + 1)
    a = atr(fenetre, PERIODE_ATR) if fenetre else None
    if a:
        sortie["atr_14"] = a
        sortie["atr_pct"] = round(a / fenetre[-1].close * 100, 5)
    fenetre = _contigues(completes, j, besoin_macd(RAPIDE, LENTE, SIGNAL))
    m = macd(fenetre, RAPIDE, LENTE, SIGNAL) if fenetre else None
    if m is not None:
        sortie["macd_ligne"] = m.ligne
        sortie["macd_signal"] = m.signal
        sortie["macd_hist"] = m.histogramme
        if a:
            sortie["macd_sens"] = round(
                m.histogramme / a * (1 if call else -1), 4)
    return sortie


def _nouvelle_case(echeances: Sequence[int]) -> dict:
    return {str(sec): {"periodes": [0, 0, 0, 0]} for sec in echeances}


class Indicateurs:
    """Accumule, paire après paire, les issues des états d'indicateurs."""

    def __init__(self, debut_ts: int, coupure_ts: int,
                 gel_ts: int | None = None,
                 echeances: Sequence[int] = ECHEANCES):
        self.debut = int(debut_ts)
        self.coupure = int(coupure_ts)
        #: Le test aveugle commence un jour à 0 h UTC : les comptes par
        #: jour suffisent à le séparer.
        self.gel = int(gel_ts) // 86400 * 86400 if gel_ts else None
        self.echeances = tuple(echeances)
        self.cases = {cle: _nouvelle_case(self.echeances)
                      for cle, _l, _s in CASES}
        #: {case: {échéance: {(paire, jour): [n, gagnés]}}} — en mémoire
        #: seulement, pour l'intervalle par blocs.
        self.blocs: dict[str, dict[str, dict[tuple[str, int], list[int]]]] = {
            cle: {str(sec): {} for sec in self.echeances}
            for cle, _l, _s in CASES}
        #: La distribution du RSI : par décile, la part des hausses à
        #: l'échéance la plus longue, [anciens, hausses, récents, hausses].
        self.deciles = [[0, 0, 0, 0] for _ in range(10)]
        self.exclusions = {"fenetre_incomplete": 0, "rsi_indefini": 0,
                           **{f"sortie_absente_{sec}": 0
                              for sec in self.echeances},
                           **{f"egalite_{sec}": 0 for sec in self.echeances}}
        self.observees = 0
        self.paires: dict[str, int] = {}

    def ajouter(self, bougies: Sequence[Candle],
                respirer: Callable[[], None] | None = None) -> None:
        completes = sorted((b for b in bougies if b.complete),
                           key=lambda b: b.ts_sec)
        if not completes:
            return
        pair = completes[0].pair
        closes = {b.ts_sec: b.close for b in completes}
        p_rapide, p_lente, p_signal = (poids_expo(RAPIDE), poids_expo(LENTE),
                                       poids_expo(SIGNAL))
        precedente: Candle | None = None
        observees = 0
        for i, c in enumerate(completes):
            if respirer is not None and i and i % 5000 == 0:
                respirer()
            if precedente is None or c.ts_sec - precedente.ts_sec != 60:
                # Un trou : tout repart de zéro, rien n'est réparé.
                derniers = deque(maxlen=PERIODE_RSI + 1)
                cours = deque(maxlen=_W_LENTE)
                tr14 = deque(maxlen=PERIODE_ATR)
                tr60 = deque(maxlen=PERIODE_ATR_LONGUE)
                lignes = deque(maxlen=_W_SIGNAL)
                hist_prec = None
            else:
                # Les opérations de `oscillators._true_range` et `atr`.
                tr = max(c.high - c.low, abs(c.high - precedente.close),
                         abs(c.low - precedente.close))
                tr14.append(tr)
                tr60.append(tr)
            precedente = c
            derniers.append(c.close)
            cours.append(c.close)
            if len(cours) == _W_LENTE:
                lignes.append(expo_tronquee(cours, p_rapide)
                              - expo_tronquee(cours, p_lente))
            if len(lignes) < _W_SIGNAL or len(tr60) < PERIODE_ATR_LONGUE:
                self.exclusions["fenetre_incomplete"] += 1
                continue
            hist = lignes[-1] - expo_tronquee(lignes, p_signal)
            croisement = (0 if hist_prec is None else
                          1 if hist_prec <= 0 < hist else
                          -1 if hist_prec >= 0 > hist else 0)
            hist_prec = hist
            r = _rsi_de(derniers)
            rapport = (sum(tr14) / PERIODE_ATR) / (
                sum(tr60) / PERIODE_ATR_LONGUE) if sum(tr60) else None
            cases = ["temoin"]
            if r is None:
                self.exclusions["rsi_indefini"] += 1
            elif r <= RSI_BAS:
                cases.append("rsi_bas")
            elif r >= RSI_HAUT:
                cases.append("rsi_haut")
            else:
                cases.append("rsi_milieu")
            if hist > 0:
                cases.append("macd_pos")
            elif hist < 0:
                cases.append("macd_neg")
            if croisement > 0:
                cases.append("croise_haut")
            elif croisement < 0:
                cases.append("croise_bas")
            if rapport is not None:
                cases.append("atr_calme" if rapport < ATR_CALME else
                             "atr_agite" if rapport > ATR_AGITE else
                             "atr_normal")
            observees += 1
            k = 0 if c.ts_sec < self.coupure else 2
            jour = c.ts_sec // 86400
            for sec in self.echeances:
                sortie = closes.get(c.ts_sec + sec)
                if sortie is None:
                    self.exclusions[f"sortie_absente_{sec}"] += 1
                    continue
                if sortie == c.close:
                    self.exclusions[f"egalite_{sec}"] += 1
                    continue
                hausse = sortie > c.close
                cle_sec = str(sec)
                if sec == self.echeances[-1] and r is not None:
                    d = self.deciles[min(9, int(r // 10))]
                    d[k] += 1
                    d[k + 1] += int(hausse)
                for cle in cases:
                    gagne = int(hausse if _SENS[cle] >= 0 else not hausse)
                    periodes = self.cases[cle][cle_sec]["periodes"]
                    periodes[k] += 1
                    periodes[k + 1] += gagne
                    # Semaines, paires et test aveugle se déduisent des
                    # blocs (paire, jour) : un seul compte par issue.
                    b = self.blocs[cle][cle_sec].setdefault((pair, jour),
                                                            [0, 0])
                    b[0] += 1
                    b[1] += gagne
        self.observees += observees
        self.paires[pair] = observees

    def resultat(self) -> dict:
        """En JSON : les clés sont du texte. Les blocs n'y sont pas ; les
        semaines (fenêtres fixes de `FENETRE_JOURS` jours depuis le début
        du rejeu), les paires et le test aveugle (jours à partir du gel)
        en sont déduits."""
        premier_jour = self.debut // 86400
        jour_gel = self.gel // 86400 if self.gel is not None else None
        cases = {}
        for cle, par_sec in self.cases.items():
            cases[cle] = {}
            for cle_sec, compte in par_sec.items():
                semaines: dict[str, list[int]] = {}
                paires: dict[str, list[int]] = {}
                gel = [0, 0]
                for (pair, jour), (n, g) in self.blocs[cle][cle_sec].items():
                    s = semaines.setdefault(
                        str((jour - premier_jour) // FENETRE_JOURS), [0, 0])
                    s[0] += n
                    s[1] += g
                    p = paires.setdefault(pair, [0, 0])
                    p[0] += n
                    p[1] += g
                    if jour_gel is not None and jour >= jour_gel:
                        gel[0] += n
                        gel[1] += g
                cases[cle][cle_sec] = {
                    "periodes": list(compte["periodes"]), "gel": gel,
                    "semaines": dict(sorted(semaines.items(),
                                            key=lambda kv: int(kv[0]))),
                    "paires": dict(sorted(paires.items()))}
        return {"debut": self.debut, "coupure": self.coupure,
                "gel": self.gel, "echeances": list(self.echeances),
                "seuils": {"rsi": [RSI_BAS, RSI_HAUT],
                           "atr": [ATR_CALME, ATR_AGITE],
                           "macd": [RAPIDE, LENTE, SIGNAL],
                           "memoire_expo": MEMOIRE_EXPO},
                "observees": self.observees, "paires": dict(self.paires),
                "exclusions": dict(self.exclusions),
                "deciles": [list(d) for d in self.deciles],
                "cases": cases}


_SENS = {cle: sens for cle, _l, sens in CASES}


def _rsi_de(derniers: Sequence[float]) -> float | None:
    """Les opérations de `oscillators.rsi`, sur les clôtures seules."""
    if len(derniers) < PERIODE_RSI + 1:
        return None
    hausses = baisses = 0.0
    precedent = None
    for close in derniers:
        if precedent is not None:
            ecart = close - precedent
            if ecart > 0:
                hausses += ecart
            else:
                baisses -= ecart
        precedent = close
    if hausses + baisses == 0:
        return None
    return 100 * hausses / (hausses + baisses)
