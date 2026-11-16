"""
Étude historique ISOLÉE de l'Engulfing sur les paires OTC (mission du
14/11) : un moteur de recherche et de simulation, en LECTURE SEULE.

Il n'importe ni la course, ni l'exécution des ordres, ni la collecte. Pour
les zones, il appelle le code de ZoneH1 tel quel (`ZoneH1._zones`, paramètres
pré-inscrits), sans le modifier.

Protocole figé : `docs/preinscription_engulfing.json`, empreinte 443c3410…
Rapport des sources : `docs/engulfing_rapport_architecture.md`. La passe
refuse de tourner si `PARAMETRES` ne donnent plus cette empreinte.

--- Le motif --------------------------------------------------------------------

c1 englobée, c2 englobante, minutes consécutives et complètes, sur les
CORPS :
- haussier : c1 rouge, c2 verte, open(c2) ≤ close(c1), close(c2) ≥ open(c1),
  corps de c2 strictement plus grand → PUT ;
- baissier : l'inverse → CALL.
Le niveau est open(c1).

--- L'entrée (scénario B, le seul opérationnel) ---------------------------------

Après la clôture de c2, la PREMIÈRE atteinte du niveau dans les 10 minutes :
- ticks (prioritaires) : premier tick, horodaté à partir de la fin de c2, qui
  touche le niveau ; entrée au dernier tick connu à l'atteinte + délai
  (2 s, sensibilité 0 s) ; sortie au dernier tick connu à l'entrée +
  échéance, daté de moins de 10 s ; minutes continues exigées ;
- bougies M1 (approximation, publiée à part) : première bougie dont
  l'étendue atteint le niveau ; entrée au niveau, ou à l'ouverture si elle
  est déjà au-delà ; sortie à la clôture de la bougie d'atteinte + (échéance
  − 1) minutes. L'instant de l'atteinte dans la bougie est inconnu.
Le scénario A (entrée à l'ouverture de c1) est INVALIDE : le motif n'est
connu qu'à la clôture de c2. Il n'est calculé que pour montrer l'effet de
l'anticipation.

--- Ce qui ne lit jamais le futur -----------------------------------------------

Le motif et toutes les variantes ne lisent que c1, c2 et les bougies
d'AVANT c1 ; les zones sont celles que ZoneH1 connaît à la clôture de c2.
Les mesures d'après l'entrée (rendements à 1-10 min, excursion de la
première minute) servent l'étude des pertes, comme description, jamais
comme filtre. Les tests le vérifient.

Sorties en CSV seulement, aucune écriture en base. La course ne lit rien
d'ici.
"""

from __future__ import annotations

import csv
import gzip
import html
import io
import json
import logging
import math
import operator
import random
import time
import zlib
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Any, Callable, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core import memoire
from maxprofit.core.errors import BotError
from maxprofit.core.payout import FLUX_POUR_LE_PLAFOND
from maxprofit.live.etude3 import ConnexionTracee, etat_lecture_seule
from maxprofit.live.selection3 import PAYOUT_AVANT, PAYOUTS_DU_JOUR, resumes
from maxprofit.strategies.zone_h1 import PARAMETRES_PRE_INSCRITS, ZoneH1

log = logging.getLogger(__name__)

PARAMETRES: dict[str, Any] = json.loads(r"""
{
  "donnees": {
    "bougies": "candles, tf_sec = 60, complete = 1, lues sur la relève ; un jour dont le résumé diffère de l'active est lu sur l'active",
    "debut_utc": "2026-10-06T00:00:00Z",
    "fin_exploration_utc": "2026-11-03T00:00:00Z",
    "fin_validation": "début du jour UTC du lancement : seuls des jours complets sont lus",
    "paires": "toutes les paires qui ont des bougies M1 dans la période",
    "payouts": "payouts (points de changement), mêmes règles de copie",
    "ticks": "tick_paths (un chemin par paire et par minute, horodatages à la ms), mêmes règles de copie"
  },
  "episodes": "dans chaque variante, un motif n'est retenu que si sa bougie c1 commence après la fin de l'épisode retenu précédent sur la même paire ; fin d'un épisode : atteinte + 15 minutes, ou fin de c2 + 10 minutes sans atteinte, au rythme des bougies M1",
  "execution": {
    "attente_max_min": 10,
    "bougies_m1": "approximation publiée à part, jamais présentée comme une exécution : première bougie dont l'étendue atteint le niveau ; entrée au niveau, ou à l'ouverture si elle est déjà au-delà ; sortie à la clôture de la bougie d'atteinte + (échéance − 1) minutes ; l'instant de l'atteinte dans la bougie est inconnu",
    "delai_principal_sec": 2,
    "delais_sec": [
      2,
      0
    ],
    "echeances_min": [
      1,
      2,
      5,
      10,
      15
    ],
    "gain": "mouvement strict dans le sens du trade ; une égalité est une perte (sensibilité publiée : égalités exclues)",
    "jouable": "dernier payout du flux relevé au plus tard à l'entrée ≥ 84, soit 92 % appliqués et un point mort de 52,0833 % ; supposé identique à toutes les échéances, ce qui reste à vérifier sur la plateforme",
    "ticks": "prioritaires : l'atteinte est le premier tick, horodaté à partir de la fin de c2, qui touche le niveau ; entrée au dernier tick connu à l'atteinte + délai ; sortie au dernier tick connu à l'entrée + échéance, qui doit dater de moins de 10 s ; toutes les minutes, de la fin de c2 à la sortie, doivent avoir leur chemin de ticks"
  },
  "interdits": [
    "aucune écriture en base, sorties en CSV seulement",
    "aucun appel d'exécution d'ordre",
    "la collecte, la stratégie ZoneH1, le moteur des signaux, les échéances et les réglages de production ne changent pas",
    "aucun paramètre ne se rechoisit après le gel : un changement fait une nouvelle pré-inscription, avec une nouvelle empreinte",
    "le premier lancement complet fait foi ; un nouveau lancement ne se justifie que par une panne technique, notée"
  ],
  "motif": {
    "baissier": "c1 verte, c2 rouge, open(c2) ≥ close(c1), close(c2) ≤ open(c1), corps de c2 strictement plus grand que celui de c1",
    "c1_c2": "c1 est la bougie englobée, c2 la bougie englobante ; minutes consécutives, toutes deux complètes",
    "corps_seulement": "les mèches n'entrent pas dans l'englobement",
    "direction": {
      "baissier": "CALL",
      "haussier": "PUT"
    },
    "haussier": "c1 rouge (close < open), c2 verte (close > open), open(c2) ≤ close(c1), close(c2) ≥ open(c1), corps de c2 strictement plus grand que celui de c1",
    "niveau": "open(c1)"
  },
  "objet": "Étude historique isolée de l'Engulfing sur les paires OTC : le retour au niveau d'ouverture de la bougie englobée, joué dans le sens défini par l'utilisateur, bat-il le point mort de 52,0833 % ?",
  "pertes": {
    "caracteristiques": [
      "retouches de la zone avant le signal (signaux V01)",
      "mouvement des 10 bougies avant c1, signé dans le sens du trade, en étendues médianes",
      "amplitude de ce mouvement",
      "corps / étendue de c1",
      "corps / étendue de c2",
      "corps de c2 / corps de c1",
      "distance du niveau à la zone, en étendues médianes (signaux V01)",
      "attente entre la fin de c2 et l'atteinte, en secondes",
      "rendement dans le sens du trade à 1, 2, 3, 5 et 10 minutes après l'entrée, en étendues médianes",
      "favorable d'abord : dans la première minute, une excursion favorable de 0,5 étendue médiane avant toute excursion défavorable de 0,5",
      "défavorable d'abord : l'inverse"
    ],
    "population": "V00, mode ticks, délai principal, par échéance et par période",
    "regle": "une caractéristique n'est notée que si l'écart gagnants − perdants a un IC 95 % hors de zéro en exploration, puis le même sens avec un IC hors de zéro en validation ; les mesures d'après l'entrée sont descriptives, jamais des filtres"
  },
  "scenarios": {
    "A": "entrée à l'ouverture de c1 : INVALIDE pour une stratégie, car le motif n'est connu qu'à la clôture de c2 ; calculé sur les bougies seulement pour montrer l'effet de l'anticipation",
    "B": "principal : après la clôture de c2, la première atteinte du niveau (haussier : prix ≤ niveau ; baissier : prix ≥ niveau) dans les 10 minutes ; une seule entrée par motif, à la première atteinte ; sans atteinte, le motif n'est pas déclenché"
  },
  "statistique": {
    "avec_sans_filtre": "écart variante − V00 par période, IC bootstrap",
    "cases": "variante × échéance, mode ticks, délai principal",
    "ic": "IC 95 % percentile du bootstrap pour les cases ; Wilson pour les détails par paire, par sens et par semaine",
    "petits_echantillons": "signalés sous 100 décisions jouables pour une case, sous 30 pour un détail",
    "selection": "sur l'exploration : taux jouable ≥ 52,0833 % avec au moins 200 décisions jouables",
    "sensibilites": "délai de 0 s, égalités exclues, approximation M1 : publiées à part, jamais des critères",
    "series": "taux par semaine, et plus longue série de pertes consécutives",
    "validation": "cases retenues seulement, au moins 100 décisions jouables en validation ; p unilatérale contre 52,0833 % (bootstrap par blocs paire × jour, 1 000 tirages, graine 7, approximation normale) ; Benjamini-Hochberg à 10 % sur toutes les cases jugées",
    "verdicts": "« validée » : découverte BH en validation ; « non reproduite » : retenue en exploration, pas en validation ; aucune variante n'est retenue sur l'exploration seule"
  },
  "variantes": {
    "V00_tous": "aucun filtre",
    "V01_zone": "contact d'une zone ZoneH1 vivante par le corps de c1 ou de c2 : zones calculées par le code de ZoneH1, paramètres pré-inscrits (pique à ±12 bougies, tolérance 0,02 %, mémoire 240 bougies, zone non cassée, confirmée avant c2), au moins 300 bougies d'historique",
    "V02_retouches_0_1": "V01, et la zone touchée par 0 ou 1 corps parmi les 20 bougies avant c1 (à la tolérance)",
    "V03_retouches_2_3": "V01, et 2 ou 3 retouches",
    "V04_retouches_4_plus": "V01, et 4 retouches ou plus",
    "V05_corps_c2_petit": "corps de c2 < 1,5 fois le corps médian des 50 bougies avant c1",
    "V06_corps_c2_moyen": "corps de c2 de 1,5 à 3 fois ce corps médian",
    "V07_corps_c2_grand": "corps de c2 ≥ 3 fois ce corps médian",
    "V08_c2_meches_longues": "corps de c2 / étendue de c2 < 0,5",
    "V09_c2_meches_moyennes": "corps de c2 / étendue de c2 de 0,5 à 0,8",
    "V10_c2_corps_plein": "corps de c2 / étendue de c2 ≥ 0,8",
    "V11_apres_mouvement": "|close(c1 − 1) − close(c1 − 11)| ≥ 3 fois l'étendue médiane des 50 bougies avant c1, sur 11 minutes consécutives",
    "V12_apres_compression": "plus haut − plus bas des 10 bougies avant c1 ≤ 2,5 fois l'étendue médiane des 50 bougies avant c1, sur 10 minutes consécutives"
  }
}
""")
EMPREINTE = "443c34106529632c470a055288dab4c6900cb011a0daa59c4146e320315d994f"


def _instant(texte: str) -> int:
    return int(datetime.strptime(texte, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=timezone.utc).timestamp())


DEBUT = _instant(PARAMETRES["donnees"]["debut_utc"])
FIN_EXPLORATION = _instant(PARAMETRES["donnees"]["fin_exploration_utc"])
ECHEANCES: tuple[int, ...] = tuple(PARAMETRES["execution"]["echeances_min"])
DELAIS: tuple[int, ...] = tuple(PARAMETRES["execution"]["delais_sec"])
DELAI_PRINCIPAL: int = PARAMETRES["execution"]["delai_principal_sec"]
ATTENTE_MAX_MIN: int = PARAMETRES["execution"]["attente_max_min"]
POINT_MORT = 1 / 1.92                     # payout appliqué de 92 %
FLUX_MIN = FLUX_POUR_LE_PLAFOND           # 84
FRAICHEUR_SORTIE_MS = 10_000
ECHEANCE_EPISODE_MIN = max(ECHEANCES)

TOLERANCE_PCT = PARAMETRES_PRE_INSCRITS.tolerance_pct     # 0,02 %
MEMOIRE_ZONE = PARAMETRES_PRE_INSCRITS.memoire            # 240 bougies
HISTORIQUE_ZONE = PARAMETRES_PRE_INSCRITS.lookback        # 300 bougies
FENETRE_RETOUCHES = 20
FENETRE_MEDIANE = 50
FENETRE_MOUVEMENT = 10
SEUIL_MOUVEMENT = 3.0
SEUIL_COMPRESSION = 2.5
CORPS_PETIT, CORPS_GRAND = 1.5, 3.0
MECHES_LONGUES, CORPS_PLEIN = 0.5, 0.8
EXCURSION = 0.5
HORIZONS_TRAJECTOIRE_SEC = (60, 120, 180, 300, 600)
N_EXPLO_MIN = 200
N_VALID_MIN = 100
N_DETAIL_MIN = 30
RESPIRATION_SEC = 0.02
#: `/engulfing essai` : une paire, les derniers jours complets, aucun taux.
JOURS_ESSAI = 2

HAUSSIER, BAISSIER = 1, -1
DECLENCHE, NON_DECLENCHE = "déclenché", "non déclenché"
SANS_TICKS = "ticks manquants avant l'atteinte"
EXPLORATION, VALIDATION = "exploration", "validation"
GAIN, PERTE, EGALITE = 1, -1, 0
MODES: tuple[tuple[str, int | None], ...] = tuple(
    [("ticks", d) for d in DELAIS] + [("m1", None)])
MODE_PRINCIPAL = ("ticks", DELAI_PRINCIPAL)

SQL_BOUGIES = ("SELECT ts_sec, open, high, low, close FROM candles WHERE "
               "pair = ? AND tf_sec = 60 AND ts_sec >= ? AND ts_sec < ? AND "
               "complete = 1 ORDER BY ts_sec")
SQL_CHEMINS = ("SELECT minute_sec, n_ticks, echelle, chemin FROM tick_paths "
               "WHERE pair = ? AND minute_sec >= ? AND minute_sec < ? "
               "ORDER BY minute_sec")

_N = NormalDist()


def _utc(ts: float, forme: str = "%d/%m") -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime(forme)


# --------------------------------------------------------------------------- #
# Les bougies et le motif
# --------------------------------------------------------------------------- #

class Bougie:
    """Une bougie M1 complète, sans la validation de `core.types.Candle`
    (vérifiée ici une fois). `open` et `close` suffisent à ZoneH1._zones."""

    __slots__ = ("ts_sec", "open", "high", "low", "close")

    def __init__(self, ts_sec: int, o: float, h: float, l: float, c: float):
        self.ts_sec, self.open, self.high, self.low, self.close = \
            ts_sec, o, h, l, c


def bougie_valide(b: Bougie) -> bool:
    return (b.ts_sec % 60 == 0 and b.low <= min(b.open, b.close)
            and max(b.open, b.close) <= b.high and b.low > 0)


def motif(c1: Bougie, c2: Bougie) -> int:
    """HAUSSIER, BAISSIER ou 0. Corps seulement ; minutes consécutives."""
    if c2.ts_sec != c1.ts_sec + 60:
        return 0
    if not abs(c2.close - c2.open) > abs(c1.close - c1.open):
        return 0
    if (c1.close < c1.open and c2.close > c2.open
            and c2.open <= c1.close and c2.close >= c1.open):
        return HAUSSIER
    if (c1.close > c1.open and c2.close < c2.open
            and c2.open >= c1.close and c2.close <= c1.open):
        return BAISSIER
    return 0


def sens_du_trade(type_: int) -> int:
    """+1 pour un CALL, −1 pour un PUT : haussier → PUT, baissier → CALL."""
    return -type_


def touche(prix: float, niveau: float, type_: int) -> bool:
    """Le prix atteint-il le niveau ? Haussier : prix ≤ niveau ; baissier :
    prix ≥ niveau."""
    return (prix - niveau) * type_ <= 0


def _mediane(v: Sequence[float]) -> float | None:
    if not v:
        return None
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _issue(entree: float, sortie: float, sens: int) -> int:
    d = (sortie - entree) * sens
    return GAIN if d > 0 else PERTE if d < 0 else EGALITE


# --------------------------------------------------------------------------- #
# Les zones de ZoneH1, calculées par son propre code
# --------------------------------------------------------------------------- #

@dataclass
class Zones:
    """(confirmation, prix, sens) de ZoneH1, et l'indice de la première
    clôture qui casse chaque zone (None : pas cassée dans sa mémoire)."""

    conf: list[int]
    prix: list[float]
    sens: list[int]
    cassee: list[int | None]


def zones_de(bougies: Sequence[Bougie]) -> Zones:
    brutes = sorted(ZoneH1()._zones(tuple(bougies)))
    conf, prix, sens, cassee = [], [], [], []
    n = len(bougies)
    for c, p, s in brutes:
        marge = p * TOLERANCE_PCT / 100
        rupture = None
        for k in range(c + 1, min(n, c + MEMOIRE_ZONE + 1)):
            cl = bougies[k].close
            if (s > 0 and cl < p - marge) or (s < 0 and cl > p + marge):
                rupture = k
                break
        conf.append(c)
        prix.append(p)
        sens.append(s)
        cassee.append(rupture)
    return Zones(conf, prix, sens, cassee)


def _extreme(b: Bougie, s: int) -> float:
    """Le bord du corps du côté de la zone : bas pour un support."""
    return min(b.open, b.close) if s > 0 else max(b.open, b.close)


def zone_en_contact(z: Zones, bougies: Sequence[Bougie], i1: int,
                    niveau: float) -> tuple[float, int] | None:
    """La zone vivante, connue à la clôture de c2 (indice i1 + 1), touchée
    par le corps de c1 ou de c2 ; la plus proche du niveau."""
    i2 = i1 + 1
    debut = bisect_left(z.conf, i2 - MEMOIRE_ZONE)
    meilleure = None
    for k in range(debut, len(z.conf)):
        c = z.conf[k]
        if c > i2 - 1:
            break
        if z.cassee[k] is not None and z.cassee[k] <= i2:
            continue
        p, s = z.prix[k], z.sens[k]
        marge = p * TOLERANCE_PCT / 100
        if not any(abs(_extreme(bougies[i], s) - p) <= marge
                   for i in (i1, i2)):
            continue
        if meilleure is None or abs(p - niveau) < abs(meilleure[0] - niveau):
            meilleure = (p, s)
    return meilleure


def retouches(bougies: Sequence[Bougie], i1: int, prix: float,
              s: int) -> int:
    marge = prix * TOLERANCE_PCT / 100
    return sum(1 for k in range(max(0, i1 - FENETRE_RETOUCHES), i1)
               if abs(_extreme(bougies[k], s) - prix) <= marge)


# --------------------------------------------------------------------------- #
# Les variantes : des prédicats sur ce qui est connu à la clôture de c2
# --------------------------------------------------------------------------- #

def _entre(x, bas, haut):
    return x is not None and (bas is None or x >= bas) and \
        (haut is None or x < haut)


VARIANTES: tuple[tuple[str, Callable[["Signal"], bool]], ...] = (
    ("V00_tous", lambda s: True),
    ("V01_zone", lambda s: s.zone is not None),
    ("V02_retouches_0_1", lambda s: s.zone is not None and s.retouches <= 1),
    ("V03_retouches_2_3",
     lambda s: s.zone is not None and 2 <= s.retouches <= 3),
    ("V04_retouches_4_plus",
     lambda s: s.zone is not None and s.retouches >= 4),
    ("V05_corps_c2_petit", lambda s: _entre(s.corps_c2_rel, None, CORPS_PETIT)),
    ("V06_corps_c2_moyen",
     lambda s: _entre(s.corps_c2_rel, CORPS_PETIT, CORPS_GRAND)),
    ("V07_corps_c2_grand", lambda s: _entre(s.corps_c2_rel, CORPS_GRAND, None)),
    ("V08_c2_meches_longues", lambda s: _entre(s.c2_ce, None, MECHES_LONGUES)),
    ("V09_c2_meches_moyennes",
     lambda s: _entre(s.c2_ce, MECHES_LONGUES, CORPS_PLEIN)),
    ("V10_c2_corps_plein", lambda s: _entre(s.c2_ce, CORPS_PLEIN, None)),
    ("V11_apres_mouvement",
     lambda s: s.mouvement is not None and abs(s.mouvement) >= SEUIL_MOUVEMENT),
    ("V12_apres_compression",
     lambda s: s.compression is not None
     and s.compression <= SEUIL_COMPRESSION),
)
NOMS_VARIANTES = tuple(n for n, _ in VARIANTES)


# --------------------------------------------------------------------------- #
# Un signal
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Signal:
    paire: str
    type_: int
    c1_ts: int
    niveau: float
    c1: tuple[float, float, float, float]
    c2: tuple[float, float, float, float]
    # ce qui est connu à la clôture de c2
    med_corps: float | None = None
    med_etendue: float | None = None
    corps_c2_rel: float | None = None
    c1_ce: float | None = None
    c2_ce: float | None = None
    c2_sur_c1: float | None = None
    mouvement: float | None = None
    compression: float | None = None
    zone: float | None = None
    zone_sens: int = 0
    retouches: int | None = None
    distance_zone: float | None = None
    historique_zone: bool = False
    membres: int = 0
    retenus: int = 0
    # bougies M1
    statut_m1: str = ""
    touche_m1_ts: int | None = None
    entree_m1: float | None = None
    entree_m1_ouverture: bool = False
    issues_m1: dict = field(default_factory=dict)
    fin_episode: int = 0
    # ticks
    statut_ticks: str = ""
    touche_ms: int | None = None
    prix_touche: float | None = None
    issues_ticks: dict = field(default_factory=dict)
    statuts_sortie: dict = field(default_factory=dict)
    trajectoire: dict = field(default_factory=dict)
    favorable_d_abord: bool | None = None
    defavorable_d_abord: bool | None = None
    # jouabilité, à l'entrée
    payout: float | None = None
    # scénario A (invalide)
    issues_a: dict = field(default_factory=dict)

    @property
    def c2_ts(self) -> int:
        return self.c1_ts + 60

    @property
    def fin_c2(self) -> int:
        return self.c1_ts + 120

    @property
    def sens(self) -> int:
        return sens_du_trade(self.type_)

    @property
    def jour(self) -> int:
        return self.fin_c2 // 86400

    @property
    def periode(self) -> str:
        return EXPLORATION if self.fin_c2 < FIN_EXPLORATION else VALIDATION

    @property
    def jouable(self) -> bool:
        return self.payout is not None and self.payout >= FLUX_MIN

    @property
    def attente_sec(self) -> float | None:
        return (None if self.touche_ms is None
                else self.touche_ms / 1000 - self.fin_c2)

    def issue(self, mode: tuple[str, int | None], echeance: int) -> int | None:
        if mode[0] == "m1":
            return self.issues_m1.get(echeance)
        return self.issues_ticks.get((mode[1], echeance))


def caracteriser(bougies: Sequence[Bougie], i1: int, type_: int,
                 zones: Zones | None) -> Signal:
    """Le signal et tout ce qui est connu à la clôture de c2 — rien d'après."""
    c1, c2 = bougies[i1], bougies[i1 + 1]
    s = Signal(paire="", type_=type_, c1_ts=c1.ts_sec, niveau=c1.open,
               c1=(c1.open, c1.high, c1.low, c1.close),
               c2=(c2.open, c2.high, c2.low, c2.close))
    corps1, corps2 = abs(c1.close - c1.open), abs(c2.close - c2.open)
    s.c1_ce = corps1 / (c1.high - c1.low)
    s.c2_ce = corps2 / (c2.high - c2.low)
    s.c2_sur_c1 = corps2 / corps1
    if i1 >= FENETRE_MEDIANE:
        avant = bougies[i1 - FENETRE_MEDIANE:i1]
        s.med_corps = _mediane([abs(b.close - b.open) for b in avant])
        s.med_etendue = _mediane([b.high - b.low for b in avant])
        if s.med_corps:
            s.corps_c2_rel = corps2 / s.med_corps
    sens = sens_du_trade(type_)
    if s.med_etendue and i1 >= FENETRE_MOUVEMENT + 1:
        a = i1 - FENETRE_MOUVEMENT - 1
        fen = bougies[a:i1]
        contigues = all(fen[k + 1].ts_sec - fen[k].ts_sec == 60
                        for k in range(len(fen) - 1)) \
            and c1.ts_sec - fen[-1].ts_sec == 60
        if contigues:
            s.mouvement = (fen[-1].close - fen[0].close) * sens / s.med_etendue
            dix = fen[1:]
            s.compression = (max(b.high for b in dix)
                             - min(b.low for b in dix)) / s.med_etendue
    s.historique_zone = i1 + 1 >= HISTORIQUE_ZONE - 1
    if zones is not None and s.historique_zone:
        contact = zone_en_contact(zones, bougies, i1, s.niveau)
        if contact is not None:
            s.zone, s.zone_sens = contact
            s.retouches = retouches(bougies, i1, s.zone, s.zone_sens)
            if s.med_etendue:
                s.distance_zone = abs(s.niveau - s.zone) / s.med_etendue
    s.membres = sum(1 << k for k, (_, f) in enumerate(VARIANTES) if f(s))
    return s


# --------------------------------------------------------------------------- #
# L'entrée et les issues sur les bougies M1 (approximation)
# --------------------------------------------------------------------------- #

def issues_m1(s: Signal, index: Mapping[int, int], bougies: Sequence[Bougie],
              fin: int) -> None:
    """Statut, atteinte et issues M1 ; et la fin de l'épisode."""
    s.fin_episode = s.fin_c2 + ATTENTE_MAX_MIN * 60
    for pas in range(1, ATTENTE_MAX_MIN + 1):
        ts = s.c2_ts + 60 * pas
        i = index.get(ts)
        if i is None:
            s.statut_m1 = ("fin des données" if ts + 60 > fin
                           else "trou avant l'atteinte")
            return
        b = bougies[i]
        if touche(b.low if s.type_ == HAUSSIER else b.high, s.niveau,
                  s.type_):
            s.touche_m1_ts = ts
            s.entree_m1_ouverture = touche(b.open, s.niveau, s.type_)
            s.entree_m1 = b.open if s.entree_m1_ouverture else s.niveau
            s.statut_m1 = DECLENCHE
            s.fin_episode = ts + 60 + ECHEANCE_EPISODE_MIN * 60
            for t in ECHEANCES:
                j = index.get(ts + 60 * (t - 1))
                s.issues_m1[t] = (None if j is None else
                                  _issue(s.entree_m1, bougies[j].close,
                                         s.sens))
            return
    s.statut_m1 = NON_DECLENCHE


def scenario_a(s: Signal, index: Mapping[int, int],
               bougies: Sequence[Bougie]) -> None:
    """INVALIDE : entrée à l'ouverture de c1, connue après la clôture de c2."""
    for t in ECHEANCES:
        j = index.get(s.c1_ts + 60 * (t - 1))
        s.issues_a[t] = (None if j is None else
                         _issue(s.niveau, bougies[j].close, s.sens))


# --------------------------------------------------------------------------- #
# Les ticks
# --------------------------------------------------------------------------- #

def ticks_de_chemin(minute_sec: int, n_ticks: int, echelle: int,
                    octets: bytes) -> tuple[list[int], list[float]]:
    """Le décodage de `chemin_ticks.decoder`, en listes, sans objet par tick
    ni appel de fonction par entier : sur le serveur gratuit, c'est
    l'essentiel du temps de calcul. Mêmes contrôles : il lève sur toute
    incohérence (échelle, chemin tronqué ou trop long)."""
    brut = zlib.decompress(octets)
    n = len(brut)
    i = valeur = decalage = 0
    while True:
        if i >= n:
            raise BotError("chemin tronqué")
        b = brut[i]
        i += 1
        valeur |= (b & 0x7F) << decalage
        if b < 0x80:
            break
        decalage += 7
    if valeur != echelle:
        raise BotError(f"échelle incohérente ({valeur} ≠ {echelle})")
    facteur = 10 ** echelle
    ts = [0] * n_ticks
    px = [0.0] * n_ticks
    ms = minute_sec * 1000
    entier = 0
    try:
        for rang in range(n_ticks):
            b = brut[i]
            i += 1
            ecart = b & 0x7F
            decalage = 7
            while b >= 0x80:
                b = brut[i]
                i += 1
                ecart |= (b & 0x7F) << decalage
                decalage += 7
            ms += ecart
            b = brut[i]
            i += 1
            v = b & 0x7F
            decalage = 7
            while b >= 0x80:
                b = brut[i]
                i += 1
                v |= (b & 0x7F) << decalage
                decalage += 7
            entier = v if rang == 0 else entier + ((v >> 1) ^ -(v & 1))
            ts[rang] = ms
            px[rang] = entier / facteur
    except IndexError as erreur:
        raise BotError("chemin tronqué") from erreur
    if i != n:
        raise BotError("chemin plus long que ses ticks")
    return ts, px


class Ticks:
    """Les ticks d'UNE paire. Les chemins sont chargés jour par jour, à la
    demande ; une minute n'est DÉCODÉE que si un prix y est lu. La
    continuité se juge sur la présence des chemins, sans les décoder. Une
    minute sans chemin, ou illisible, n'est jamais comblée."""

    def __init__(self, charger_jour: Callable[[int], Mapping[int, tuple]]):
        self._charger = charger_jour
        self._jours: dict[int, Mapping[int, tuple]] = {}
        self._decodes: dict[int, tuple[list, list] | None] = {}
        self.illisibles = 0
        self.decodees = 0

    def _jour(self, minute_sec: int) -> Mapping[int, tuple]:
        jour = minute_sec // 86400
        if jour not in self._jours:
            self._jours[jour] = self._charger(jour)
        return self._jours[jour]

    def oublier_avant(self, ts_sec: int) -> None:
        jour = ts_sec // 86400
        for j in [j for j in self._jours if j < jour - 1]:
            del self._jours[j]
        for m in [m for m in self._decodes if m < ts_sec - 3600]:
            del self._decodes[m]

    def present(self, minute_sec: int) -> bool:
        if minute_sec in self._decodes:
            return self._decodes[minute_sec] is not None
        return minute_sec in self._jour(minute_sec)

    def minute(self, minute_sec: int) -> tuple[list, list] | None:
        if minute_sec in self._decodes:
            return self._decodes[minute_sec]
        brut = self._jour(minute_sec).get(minute_sec)
        sortie = None
        if brut is not None:
            try:
                sortie = ticks_de_chemin(minute_sec, *brut)
                self.decodees += 1
            except (BotError, zlib.error, ValueError):
                self.illisibles += 1
        self._decodes[minute_sec] = sortie
        return sortie

    def continu(self, debut_ms: int, fin_ms: int) -> bool:
        m = debut_ms // 60000 * 60
        while m * 1000 <= fin_ms:
            if not self.present(m):
                return False
            m += 60
        return True

    def prix_a(self, t_ms: int) -> tuple[int, float] | None:
        """Le dernier tick connu à l'instant t (minute courante, sinon la
        dernière de la minute précédente)."""
        m = t_ms // 60000 * 60
        for minute in (m, m - 60):
            d = self.minute(minute)
            if d is None:
                return None
            ts, px = d
            k = bisect_right(ts, t_ms) - 1
            if k >= 0:
                return ts[k], px[k]
        return None


def issues_ticks(s: Signal, ticks: Ticks) -> None:
    """Atteinte, entrées aux deux délais, issues et trajectoire."""
    niveau, type_ = s.niveau, s.type_
    for pas in range(ATTENTE_MAX_MIN):
        d = ticks.minute(s.fin_c2 + 60 * pas)
        if d is None:
            s.statut_ticks = SANS_TICKS
            return
        ts, px = d
        for k in range(len(ts)):
            # Les ticks d'une minute ≥ fin de c2 sont tous postérieurs à la
            # clôture de c2 : aucun ne précède la confirmation du motif.
            if (px[k] - niveau) * type_ <= 0:
                s.touche_ms, s.prix_touche = ts[k], px[k]
                break
        if s.touche_ms is not None:
            break
    if s.touche_ms is None:
        s.statut_ticks = NON_DECLENCHE
        return
    s.statut_ticks = DECLENCHE
    for delai in DELAIS:
        t_e = s.touche_ms + 1000 * delai
        if delai == 0:
            p_e = s.prix_touche
        else:
            connu = ticks.prix_a(t_e)
            if connu is None:
                for t in ECHEANCES:
                    s.statuts_sortie[(delai, t)] = "ticks manquants à l'entrée"
                continue
            p_e = connu[1]
        for t in ECHEANCES:
            t_x = t_e + 60000 * t
            if not ticks.continu(s.touche_ms, t_x):
                s.statuts_sortie[(delai, t)] = "ticks manquants à la sortie"
                continue
            connu = ticks.prix_a(t_x)
            if connu is None or t_x - connu[0] > FRAICHEUR_SORTIE_MS:
                s.statuts_sortie[(delai, t)] = "flux gelé à la sortie"
                continue
            s.issues_ticks[(delai, t)] = _issue(p_e, connu[1], s.sens)
            s.statuts_sortie[(delai, t)] = "issue"
        if delai == DELAI_PRINCIPAL:
            _trajectoire(s, ticks, t_e, p_e)


def _trajectoire(s: Signal, ticks: Ticks, t_e: int, p_e: float) -> None:
    """Après l'entrée : descriptif, jamais un filtre."""
    if not s.med_etendue:
        return
    for h in HORIZONS_TRAJECTOIRE_SEC:
        t = t_e + 1000 * h
        if ticks.continu(t_e, t):
            connu = ticks.prix_a(t)
            if connu is not None and t - connu[0] <= FRAICHEUR_SORTIE_MS:
                s.trajectoire[h] = (connu[1] - p_e) * s.sens / s.med_etendue
    if not ticks.continu(t_e, t_e + 60000):
        return
    m = t_e // 60000 * 60
    minutes = [ticks.minute(m), ticks.minute(m + 60)]
    if None in minutes:
        return
    s.favorable_d_abord = s.defavorable_d_abord = False
    for ts, px in minutes:
        for k in range(len(ts)):
            if ts[k] <= t_e or ts[k] > t_e + 60000:
                continue
            x = (px[k] - p_e) * s.sens / s.med_etendue
            if x >= EXCURSION:
                s.favorable_d_abord = True
                return
            if x <= -EXCURSION:
                s.defavorable_d_abord = True
                return


# --------------------------------------------------------------------------- #
# Une paire, de bout en bout
# --------------------------------------------------------------------------- #

def episodes(signaux: Sequence[Signal]) -> None:
    """Dans chaque variante, un motif n'est retenu que si c1 commence après
    la fin de l'épisode retenu précédent sur la même paire."""
    for k in range(len(VARIANTES)):
        bit = 1 << k
        fin = -1
        for s in signaux:
            if s.membres & bit and s.c1_ts >= fin:
                s.retenus |= bit
                fin = s.fin_episode


def paire(nom: str, bougies: Sequence[Bougie], fin: int,
          ticks: Ticks | None, payout_a: Callable[[float], float | None],
          compte: Counter, respirer: Callable[[], None]) -> list[Signal]:
    """Les signaux d'une paire retenus dans au moins une variante, avec
    leurs issues. `compte` reçoit les motifs et les statuts."""
    index = {b.ts_sec: i for i, b in enumerate(bougies)}
    zones = zones_de(bougies) if len(bougies) >= HISTORIQUE_ZONE else None
    signaux = []
    for i1 in range(len(bougies) - 1):
        t = motif(bougies[i1], bougies[i1 + 1])
        if not t:
            continue
        s = caracteriser(bougies, i1, t, zones)
        s.paire = nom
        if s.fin_c2 >= fin:
            continue
        compte[("motifs", s.periode, "haussier" if t > 0 else "baissier")] += 1
        issues_m1(s, index, bougies, fin)
        signaux.append(s)
        if len(signaux) % 2000 == 0:
            respirer()
    episodes(signaux)
    gardes = [s for s in signaux if s.retenus]
    del signaux
    for rang, s in enumerate(gardes):
        scenario_a(s, index, bougies)
        compte[("m1", s.periode, s.statut_m1)] += 1
        if ticks is not None:
            ticks.oublier_avant(s.c1_ts)
            issues_ticks(s, ticks)
            compte[("ticks", s.periode, s.statut_ticks)] += 1
        instant = (s.touche_ms / 1000 if s.touche_ms is not None
                   else s.touche_m1_ts)
        if instant is not None:
            s.payout = payout_a(instant)
        if rang % 200 == 199:
            respirer()
    respirer()
    return gardes


# --------------------------------------------------------------------------- #
# Les statistiques, accumulées paire par paire
# --------------------------------------------------------------------------- #

_sumprod = getattr(math, "sumprod", None) or (
    lambda a, b: sum(map(operator.mul, a, b)))


def wilson(g: int, n: int, z: float = 1.96) -> tuple[float | None,
                                                      float | None]:
    if not n:
        return None, None
    p = g / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    e = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - e, c + e


class Tirages:
    """Les tirages du bootstrap par blocs paire × jour d'UNE période,
    faits une fois (1 000 tirages, graine 7) et partagés par toutes ses
    cases : chaque tirage est le vecteur des multiplicités des blocs."""

    def __init__(self, blocs: Sequence[int]) -> None:
        self.position = {b: j for j, b in enumerate(blocs)}
        k = len(blocs)
        alea = random.Random(em.GRAINE)
        self.tirages: list[array] = []
        for _ in range(em.TIRAGES if k >= 2 else 0):
            m = array("H", bytes(2 * k))
            for i in alea.choices(range(k), k=k):
                m[i] += 1
            self.tirages.append(m)

    def dense(self, par_bloc: Mapping[int, float]) -> list[float]:
        v = [0.0] * len(self.position)
        for b, x in par_bloc.items():
            v[self.position[b]] = x
        return v

    def ratio(self, g: Mapping[int, float], n: Mapping[int, float],
              taux: float) -> tuple[float | None, float | None,
                                    float | None]:
        """(bas, haut, p unilatérale contre le point mort)."""
        if not self.tirages:
            return None, None, None
        gd, nd = self.dense(g), self.dense(n)
        v = []
        for m in self.tirages:
            nn = _sumprod(nd, m)
            if nn:
                v.append(_sumprod(gd, m) / nn)
        if len(v) < len(self.tirages) // 2:
            return None, None, None
        v.sort()
        moy = sum(v) / len(v)
        et = (sum((x - moy) ** 2 for x in v) / (len(v) - 1)) ** 0.5
        p = ((1 - _N.cdf((taux - POINT_MORT) / et)) if et > 0
             else (0.0 if taux > POINT_MORT else 1.0))
        return v[int(0.025 * len(v))], v[int(0.975 * len(v)) - 1], p

    def ecart(self, sa, na, sb, nb) -> tuple[float | None, float | None]:
        """IC 95 % de moyenne A − moyenne B (sommes et effectifs par bloc)."""
        if not self.tirages:
            return None, None
        a, b, c, d = (self.dense(x) for x in (sa, na, sb, nb))
        v = []
        for m in self.tirages:
            n1, n2 = _sumprod(b, m), _sumprod(d, m)
            if n1 and n2:
                v.append(_sumprod(a, m) / n1 - _sumprod(c, m) / n2)
        if len(v) < len(self.tirages) // 2:
            return None, None
        v.sort()
        return v[int(0.025 * len(v))], v[int(0.975 * len(v)) - 1]


CARACTERISTIQUES: tuple[tuple[str, bool, Callable[[Signal], Any]], ...] = (
    # (nom, connue à la décision ?, lecture)
    ("retouches_zone", True, lambda s: s.retouches),
    ("mouvement_signe", True, lambda s: s.mouvement),
    ("mouvement_amplitude", True,
     lambda s: None if s.mouvement is None else abs(s.mouvement)),
    ("c1_corps_sur_etendue", True, lambda s: s.c1_ce),
    ("c2_corps_sur_etendue", True, lambda s: s.c2_ce),
    ("c2_corps_sur_c1", True, lambda s: s.c2_sur_c1),
    ("distance_niveau_zone", True, lambda s: s.distance_zone),
    ("attente_atteinte_sec", True, lambda s: s.attente_sec),
) + tuple(
    (f"rendement_{h // 60}_min", False,
     (lambda h: lambda s: s.trajectoire.get(h))(h))
    for h in HORIZONS_TRAJECTOIRE_SEC) + (
    ("favorable_d_abord", False,
     lambda s: None if s.favorable_d_abord is None
     else float(s.favorable_d_abord)),
    ("defavorable_d_abord", False,
     lambda s: None if s.defavorable_d_abord is None
     else float(s.defavorable_d_abord)),
)

COLONNES_SIGNAUX: tuple[str, ...] = (
    "paire", "type", "direction", "c1_utc", "c1_ts", "periode", "niveau",
    "variantes", "retenu_dans", "corps_c2_rel", "c1_ce", "c2_ce",
    "c2_sur_c1", "mouvement", "compression", "zone", "retouches",
    "distance_zone", "payout", "jouable", "statut_m1", "entree_m1",
    "entree_m1_ouverture", "statut_ticks", "attente_sec", "prix_touche",
    "favorable_d_abord", "defavorable_d_abord",
) + tuple(f"m1_{t}" for t in ECHEANCES) + tuple(
    f"a_{t}" for t in ECHEANCES) + tuple(
    f"ticks_{d}s_{t}" for t in ECHEANCES for d in DELAIS) + tuple(
    f"statut_{d}s_{t}" for t in ECHEANCES for d in DELAIS) + tuple(
    f"rendement_{h // 60}_min" for h in HORIZONS_TRAJECTOIRE_SEC)

ABSENTE = 9            # code d'une issue absente dans les tableaux compacts


def _ligne_signal(s: Signal) -> dict[str, Any]:
    ligne: dict[str, Any] = {
        "paire": s.paire, "type": "haussier" if s.type_ > 0 else "baissier",
        "direction": "CALL" if s.sens > 0 else "PUT",
        "c1_utc": _utc(s.c1_ts, "%Y-%m-%d %H:%M"), "c1_ts": s.c1_ts,
        "periode": s.periode, "niveau": s.niveau,
        "variantes": ",".join(n for k, n in enumerate(NOMS_VARIANTES)
                              if s.membres >> k & 1),
        "retenu_dans": ",".join(n for k, n in enumerate(NOMS_VARIANTES)
                                if s.retenus >> k & 1),
        "corps_c2_rel": s.corps_c2_rel, "c1_ce": s.c1_ce, "c2_ce": s.c2_ce,
        "c2_sur_c1": s.c2_sur_c1, "mouvement": s.mouvement,
        "compression": s.compression, "zone": s.zone,
        "retouches": s.retouches, "distance_zone": s.distance_zone,
        "payout": s.payout, "jouable": int(s.jouable),
        "statut_m1": s.statut_m1, "entree_m1": s.entree_m1,
        "entree_m1_ouverture": int(s.entree_m1_ouverture),
        "statut_ticks": s.statut_ticks, "attente_sec": s.attente_sec,
        "prix_touche": s.prix_touche,
        "favorable_d_abord": (None if s.favorable_d_abord is None
                              else int(s.favorable_d_abord)),
        "defavorable_d_abord": (None if s.defavorable_d_abord is None
                                else int(s.defavorable_d_abord))}
    for t in ECHEANCES:
        ligne[f"m1_{t}"] = s.issues_m1.get(t)
        ligne[f"a_{t}"] = s.issues_a.get(t)
        for d in DELAIS:
            ligne[f"ticks_{d}s_{t}"] = s.issues_ticks.get((d, t))
            ligne[f"statut_{d}s_{t}"] = s.statuts_sortie.get((d, t))
    for h in HORIZONS_TRAJECTOIRE_SEC:
        ligne[f"rendement_{h // 60}_min"] = s.trajectoire.get(h)
    return ligne


def _cellule_csv(v: Any) -> Any:
    return "" if v is None else f"{v:.6f}" if isinstance(v, float) else v


class Agregats:
    """Tout ce que les statistiques demandent, versé paire par paire : les
    signaux d'une paire sont agrégés, écrits dans le fichier des signaux
    (compressé au fil de l'eau), puis oubliés. La mémoire ne grandit
    qu'avec quelques octets par signal jouable."""

    def __init__(self) -> None:
        self.blocs: dict[tuple[str, int], int] = {}
        self.cellules: dict[tuple, dict[int, list[int]]] = defaultdict(dict)
        self.pertes: dict[tuple, dict[int, list[float]]] = defaultdict(dict)
        self.a: dict[int, list[int]] = defaultdict(lambda: [0, 0])
        self.v00: Counter = Counter()
        # Le minimum par signal jouable, pour les séries et les détails.
        self.entree = array("q")
        self.retenus = array("l")
        self.periode = array("b")
        self.paire = array("h")
        self.call = array("b")
        self.semaine = array("h")
        self.issues = array("b")          # 5 par signal, mode principal
        self.noms_paires: list[str] = []
        self.noms_semaines: list[str] = []
        self.n_signaux = 0
        self._tampon = io.BytesIO()
        self._gz = gzip.GzipFile(fileobj=self._tampon, mode="wb", mtime=0)
        self._ecrire([COLONNES_SIGNAUX])

    def _ecrire(self, lignes) -> None:
        texte = io.StringIO()
        w = csv.writer(texte, delimiter=";")
        w.writerows(lignes)
        self._gz.write(texte.getvalue().encode("utf-8"))

    @staticmethod
    def _rang(liste: list[str], valeur: str) -> int:
        try:
            return liste.index(valeur)
        except ValueError:
            liste.append(valeur)
            return len(liste) - 1

    def ajouter(self, signaux: Sequence[Signal]) -> None:
        lignes = []
        for s in signaux:
            self.n_signaux += 1
            ligne = _ligne_signal(s)
            lignes.append([_cellule_csv(ligne.get(c))
                           for c in COLONNES_SIGNAUX])
            b = self.blocs.setdefault((s.paire, s.jour), len(self.blocs))
            periode = s.periode
            if s.retenus & 1:
                self.v00[("ticks", s.statut_ticks)] += 1
                self.v00[("m1", s.statut_m1)] += 1
                for t in ECHEANCES:
                    o = s.issues_a.get(t)
                    if o is not None:
                        self.a[t][0] += o == GAIN
                        self.a[t][1] += 1
            if not s.jouable:
                continue
            for k in range(len(VARIANTES)):
                if not s.retenus >> k & 1:
                    continue
                for mi, mode in enumerate(MODES):
                    for t in ECHEANCES:
                        o = s.issue(mode, t)
                        if o is None:
                            continue
                        cellule = self.cellules[(k, mi, t, periode)]
                        l = cellule.get(b)
                        if l is None:
                            l = cellule[b] = [0, 0, 0]
                        l[0 if o == GAIN else 1 if o == PERTE else 2] += 1
            principales = [s.issue(MODE_PRINCIPAL, t) for t in ECHEANCES]
            if all(o is None for o in principales):
                continue
            if s.retenus & 1:
                for t, o in zip(ECHEANCES, principales):
                    if o is None:
                        continue
                    for nom, _, lire in CARACTERISTIQUES:
                        x = lire(s)
                        if x is None:
                            continue
                        cellule = self.pertes[(t, periode, nom)]
                        l = cellule.get(b)
                        if l is None:
                            l = cellule[b] = [0.0, 0, 0.0, 0]
                        if o == GAIN:
                            l[0] += x
                            l[1] += 1
                        else:
                            l[2] += x
                            l[3] += 1
            self.entree.append(s.touche_ms)
            self.retenus.append(s.retenus)
            self.periode.append(0 if periode == EXPLORATION else 1)
            self.paire.append(self._rang(self.noms_paires, s.paire))
            self.call.append(1 if s.sens > 0 else 0)
            self.semaine.append(self._rang(
                self.noms_semaines,
                datetime.fromtimestamp(s.fin_c2, timezone.utc)
                .strftime("%G-S%V")))
            self.issues.extend(ABSENTE if o is None else o
                               for o in principales)
        self._ecrire(lignes)

    # --- les tirages, une fois par période ------------------------------

    def tirages(self) -> dict[str, Tirages]:
        explo = [b for (_, jour), b in sorted(self.blocs.items(),
                                              key=lambda kv: kv[1])
                 if jour * 86400 < FIN_EXPLORATION]
        valid = [b for (_, jour), b in sorted(self.blocs.items(),
                                              key=lambda kv: kv[1])
                 if jour * 86400 >= FIN_EXPLORATION]
        return {EXPLORATION: Tirages(explo), VALIDATION: Tirages(valid)}

    # --- les cases --------------------------------------------------------

    def cases(self, tirages: Mapping[str, Tirages],
              respirer: Callable[[], None]) -> list[dict[str, Any]]:
        lignes = []
        pmode = MODES.index(MODE_PRINCIPAL)
        ordre = sorted(range(len(self.entree)), key=self.entree.__getitem__)
        for k, nom in enumerate(NOMS_VARIANTES):
            respirer()
            for mi, mode in enumerate(MODES):
                for ti, t in enumerate(ECHEANCES):
                    for periode in (EXPLORATION, VALIDATION):
                        blocs = self.cellules.get((k, mi, t, periode), {})
                        g = sum(l[0] for l in blocs.values())
                        p_ = sum(l[1] for l in blocs.values())
                        e = sum(l[2] for l in blocs.values())
                        n = g + p_ + e
                        ligne: dict[str, Any] = {
                            "variante": nom, "mode": mode[0],
                            "delai_sec": "" if mode[1] is None else mode[1],
                            "echeance_min": t, "periode": periode, "n": n,
                            "gains": g, "pertes": p_, "egalites": e,
                            "taux": g / n if n else None,
                            "taux_sans_egalites": (g / (g + p_) if g + p_
                                                   else None)}
                        if mi == pmode and n:
                            tir = tirages[periode]
                            bas, haut, p = tir.ratio(
                                {b: l[0] for b, l in blocs.items()},
                                {b: sum(l) for b, l in blocs.items()},
                                g / n)
                            ligne.update(ic_bas=bas, ic_haut=haut, p=p)
                            if k:
                                base = self.cellules.get(
                                    (0, mi, t, periode), {})
                                nb = sum(sum(l) for l in base.values())
                                if nb:
                                    eb, eh = tir.ecart(
                                        {b: l[0] for b, l in blocs.items()},
                                        {b: sum(l) for b, l in blocs.items()},
                                        {b: l[0] for b, l in base.items()},
                                        {b: sum(l) for b, l in base.items()})
                                    ligne.update(
                                        ecart_v00=g / n - sum(
                                            l[0] for l in base.values()) / nb,
                                        ecart_bas=eb, ecart_haut=eh)
                            ligne["serie_pertes_max"] = self._serie(
                                ordre, k, ti, periode)
                        if mi == pmode and n < (
                                N_EXPLO_MIN if periode == EXPLORATION
                                else N_VALID_MIN):
                            ligne["drapeau"] = "petit échantillon"
                        lignes.append(ligne)
        _verdicts(lignes)
        return lignes

    def _serie(self, ordre: Sequence[int], k: int, ti: int,
               periode: str) -> int:
        """Plus longue suite de pertes (égalités comprises), toutes paires,
        dans l'ordre des entrées."""
        code = 0 if periode == EXPLORATION else 1
        meilleur = cour = 0
        for i in ordre:
            if self.periode[i] != code or not self.retenus[i] >> k & 1:
                continue
            o = self.issues[5 * i + ti]
            if o == ABSENTE:
                continue
            cour = cour + 1 if o != GAIN else 0
            meilleur = max(meilleur, cour)
        return meilleur

    # --- les détails ------------------------------------------------------

    def details(self, variantes: Sequence[str]) -> list[dict[str, Any]]:
        lignes = []
        for nom in variantes:
            k = NOMS_VARIANTES.index(nom)
            for ti, t in enumerate(ECHEANCES):
                for code, periode in enumerate((EXPLORATION, VALIDATION)):
                    groupes: dict[tuple[str, str], list[int]] = \
                        defaultdict(lambda: [0, 0])
                    for i in range(len(self.entree)):
                        if self.periode[i] != code \
                                or not self.retenus[i] >> k & 1:
                            continue
                        o = self.issues[5 * i + ti]
                        if o == ABSENTE:
                            continue
                        for dim, val in (
                                ("paire", self.noms_paires[self.paire[i]]),
                                ("sens", "CALL" if self.call[i] else "PUT"),
                                ("semaine",
                                 self.noms_semaines[self.semaine[i]])):
                            groupes[(dim, val)][0] += o == GAIN
                            groupes[(dim, val)][1] += 1
                    for (dim, val), (g, n) in sorted(groupes.items()):
                        bas, haut = wilson(g, n)
                        lignes.append({
                            "variante": nom, "echeance_min": t,
                            "periode": periode, "dimension": dim,
                            "valeur": val, "n": n, "gains": g,
                            "taux": g / n, "ic_bas": bas, "ic_haut": haut,
                            "drapeau": ("petit échantillon"
                                        if n < N_DETAIL_MIN else "")})
        return lignes

    # --- gagnants contre perdants ----------------------------------------

    def lignes_pertes(self, tirages: Mapping[str, Tirages]
                      ) -> list[dict[str, Any]]:
        lignes = []
        for t in ECHEANCES:
            par = {}
            for periode in (EXPLORATION, VALIDATION):
                for nom, decision, _ in CARACTERISTIQUES:
                    blocs = self.pertes.get((t, periode, nom), {})
                    ng = sum(l[1] for l in blocs.values())
                    npe = sum(l[3] for l in blocs.values())
                    mg = (sum(l[0] for l in blocs.values()) / ng
                          if ng else None)
                    mp = (sum(l[2] for l in blocs.values()) / npe
                          if npe else None)
                    ecart = mg - mp if ng and npe else None
                    bas = haut = None
                    if ecart is not None:
                        bas, haut = tirages[periode].ecart(
                            {b: l[0] for b, l in blocs.items()},
                            {b: l[1] for b, l in blocs.items()},
                            {b: l[2] for b, l in blocs.items()},
                            {b: l[3] for b, l in blocs.items()})
                    ligne = {
                        "echeance_min": t, "periode": periode,
                        "caracteristique": nom,
                        "nature": ("connue à la décision" if decision
                                   else "après l'entrée (descriptif)"),
                        "n_gagnants": ng, "n_perdants": npe,
                        "moyenne_gagnants": mg, "moyenne_perdants": mp,
                        "ecart": ecart, "ic_bas": bas, "ic_haut": haut}
                    par[(periode, nom)] = ligne
                    lignes.append(ligne)
            for nom, decision, _ in CARACTERISTIQUES:
                x, v = par[(EXPLORATION, nom)], par[(VALIDATION, nom)]
                note = (decision and _hors_zero(x) and _hors_zero(v)
                        and (x["ecart"] > 0) == (v["ecart"] > 0))
                for l in (x, v):
                    l["notee"] = ("oui" if note else
                                  "—" if not decision else "non")
        return lignes

    def signaux_gz(self) -> bytes:
        self._gz.close()
        return self._tampon.getvalue()


def _hors_zero(l: Mapping[str, Any]) -> bool:
    return (l["ic_bas"] is not None and l["ic_haut"] is not None
            and (l["ic_bas"] > 0 or l["ic_haut"] < 0))


def _verdicts(lignes: list[dict[str, Any]]) -> None:
    principal = {(l["variante"], l["echeance_min"], l["periode"]): l
                 for l in lignes if l["mode"] == MODE_PRINCIPAL[0]
                 and l["delai_sec"] == MODE_PRINCIPAL[1]}
    jugees = {}
    for (v, t, periode), l in principal.items():
        if periode != EXPLORATION:
            continue
        val = principal.get((v, t, VALIDATION))
        if l["n"] < N_EXPLO_MIN:
            l["verdict"] = "trop peu en exploration"
        elif (l["taux"] or 0) < POINT_MORT:
            l["verdict"] = "non retenue en exploration"
        elif val is None or val["n"] < N_VALID_MIN or val.get("p") is None:
            l["verdict"] = "retenue, trop peu en validation"
            if val is not None:
                val["verdict"] = "trop peu en validation"
        else:
            l["verdict"] = "retenue en exploration"
            jugees[(v, t)] = val["p"]
    decouvertes = em.benjamini_hochberg(jugees) if jugees else {}
    for (v, t), p in jugees.items():
        val = principal[(v, t, VALIDATION)]
        val["verdict"] = ("validée (BH 10 %)" if decouvertes[(v, t)]
                          and val["taux"] > POINT_MORT else "non reproduite")


# --------------------------------------------------------------------------- #
# L'avancement, pour le bot
# --------------------------------------------------------------------------- #

_ETAT: dict[str, Any] = {}


def avancement() -> str | None:
    """Où en est la passe en cours ; None s'il n'y en a pas. La mémoire y
    figure : si l'hébergeur tue le processus faute de mémoire, la dernière
    valeur reçue le laisse voir venir."""
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
    """Le plus haut résident vu pendant la passe (lu à chaque respiration :
    quelques milliers de lectures de /proc, rien à côté du calcul)."""
    rss = memoire.residente()
    if rss is not None and rss > _ETAT.get("rss_max", 0):
        _ETAT["rss_max"] = rss


# --------------------------------------------------------------------------- #
# La passe en lecture seule
# --------------------------------------------------------------------------- #

def _lire_bougies(conn, nom: str, jour: int, compte: Counter) -> list[Bougie]:
    sortie = []
    for r in conn.execute(SQL_BOUGIES, (nom, jour * 86400,
                                        (jour + 1) * 86400)).fetchall():
        try:
            b = Bougie(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                       float(r[4]))
        except (TypeError, ValueError):
            compte[("données", "bougie illisible")] += 1
            continue
        if bougie_valide(b):
            sortie.append(b)
        else:
            compte[("données", "bougie incohérente")] += 1
    return sortie


def _lire_paire(nom: str, debut: int, fin: int, base, lue, compte: Counter,
                avec_ticks: bool, respirer: Callable[[], None]
                ) -> tuple[list[Signal], "Ticks | None", int]:
    """Bougies, payouts et ticks d'UNE paire sur [debut, fin), lus jour par
    jour (un jour divergent sur l'active), puis ses signaux gardés avec
    leurs issues. → (gardés, ticks, nombre de bougies)."""
    bougies: list[Bougie] = []
    changements: list[tuple[int, float]] = []
    ligne = lue.execute(PAYOUT_AVANT, (nom, debut)).fetchone()
    if ligne and ligne[1] is not None:
        changements.append((int(ligne[0]), float(ligne[1])))
    for jour in range(debut // 86400, fin // 86400):
        bougies += _lire_bougies(base("candles", nom, jour), nom, jour,
                                 compte)
        for ts, pct in base("payouts", nom, jour).execute(
                PAYOUTS_DU_JOUR, (nom, jour * 86400,
                                  (jour + 1) * 86400)).fetchall():
            if pct is not None:
                changements.append((int(ts), float(pct)))
        respirer()
    instants = [c[0] for c in changements]

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
                        SQL_CHEMINS, (nom, jour * 86400,
                                      min(fin, (jour + 1) * 86400)))
                    .fetchall()}
        ticks = Ticks(charger)
    n_bougies = len(bougies)
    gardes = paire(nom, bougies, fin, ticks, payout_a, compte, respirer)
    return gardes, ticks, n_bougies


def etude(lue_brute, active_brute, course=None, *,
          nom_lecture: str = "relève", maintenant: float | None = None,
          avec_ticks: bool = True,
          respirer: Callable[[], None] | None = None,
          essai: bool = False, version: str | None = None
          ) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers). Les deux bases sont déjà en lecture seule ;
    `active_brute` peut être None (copie non vérifiée). `essai` : le
    contrôle technique d'une minute (une paire, `JOURS_ESSAI` jours), qui ne
    montre aucun taux et ne compte pour rien."""
    em.exiger_le_gel(PARAMETRES, EMPREINTE)
    pause = respirer or (lambda: time.sleep(RESPIRATION_SEC))

    def respirer() -> None:
        _mesurer()
        pause()

    maintenant = time.time() if maintenant is None else maintenant
    t0 = time.monotonic()
    fin = int(maintenant) // 86400 * 86400
    if fin <= FIN_EXPLORATION and not essai:
        return ("🕯 L'exploration court jusqu'au 02/11 : rien à valider "
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


def _bases(lue_brute, active_brute, nom_lecture, debut, fin):
    """(lue, active, lecture, résumé lu, divergents, base) : les connexions
    tracées, leur état de lecture seule, et le routage d'un jour divergent
    vers l'active."""
    lue = ConnexionTracee(lue_brute)
    active = (ConnexionTracee(active_brute)
              if active_brute is not None and active_brute is not lue_brute
              else None)
    lecture = {nom_lecture: etat_lecture_seule(lue)}
    if active is not None:
        lecture["active"] = etat_lecture_seule(active)
    resume_lue = resumes(lue, debut, fin)
    divergents: set = set()
    if active is not None:
        resume_active = resumes(active, debut, fin)
        divergents = {c for c in set(resume_lue) | set(resume_active)
                      if resume_lue.get(c) != resume_active.get(c)}

    def base(table: str, nom: str, jour: int):
        return active if (table, nom, jour) in divergents else lue

    return lue, active, lecture, resume_lue, divergents, base


def _etude(lue_brute, active_brute, nom_lecture, fin, avec_ticks, respirer,
           t0, version=None):
    lue, active, lecture, resume_lue, divergents, base = _bases(
        lue_brute, active_brute, nom_lecture, DEBUT, fin)
    paires = sorted({p for (table, p, _) in resume_lue if table == "candles"})
    _ETAT.update(total=len(paires))

    compte: Counter = Counter()
    agregats = Agregats()
    decodees = 0
    _ETAT.update(debut_paires=time.monotonic())
    for rang, nom in enumerate(paires):
        _ETAT.update(etape=f"paire {nom}", faites=rang)
        gardes, ticks, _ = _lire_paire(nom, DEBUT, fin, base, lue, compte,
                                       avec_ticks, respirer)
        agregats.ajouter(gardes)
        _ETAT.update(signaux=agregats.n_signaux)
        if ticks is not None:
            compte[("données", "chemins illisibles")] += ticks.illisibles
            decodees += ticks.decodees
        del gardes, ticks
        respirer()
    _ETAT.update(etape="statistiques", faites=len(paires))
    tirages = agregats.tirages()
    lignes_cases = agregats.cases(tirages, respirer)
    candidates = sorted({l["variante"] for l in lignes_cases
                         if l.get("verdict", "").startswith("retenue")})
    lignes_details = agregats.details(["V00_tous"] + [
        v for v in candidates if v != "V00_tous"])
    lignes_pertes = agregats.lignes_pertes(tirages)
    _ETAT.update(etape="rapport et fichiers")
    rapport = texte(agregats, lignes_cases, lignes_pertes, {
        "nom": nom_lecture, "lecture": lecture, "fin": fin,
        "paires": paires, "resume": resume_lue, "compte": compte,
        "divergents": len(divergents), "avec_ticks": avec_ticks,
        "decodees": decodees,
        "requetes": sum(lue.requetes.values()) + (
            sum(active.requetes.values()) if active is not None else 0),
        "refusees": lue.refusees + (active.refusees if active else 0),
        "temps": time.monotonic() - t0, "version": version,
        "rss_depart": _ETAT.get("rss_depart"),
        "rss_max": _ETAT.get("rss_max")})
    return rapport, fichiers(agregats, lignes_cases, lignes_details,
                             lignes_pertes, resume_lue, compte)


def _essai(lue_brute, active_brute, nom_lecture, fin, avec_ticks, respirer,
           t0, version=None) -> str:
    """Le contrôle technique d'une minute : la paire la plus fournie en
    ticks, sur les `JOURS_ESSAI` derniers jours complets, par le même code
    que l'étude, statistiques, rapport et fichiers compris. Rien de ce qui
    est calculé n'est montré, sauf des comptes, des durées et la mémoire :
    l'essai ne compte pour rien et ne lève le voile sur aucun taux."""
    debut = max(DEBUT, fin - JOURS_ESSAI * 86400)
    lue, active, lecture, resume_lue, divergents, base = _bases(
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
    entete = ("🕯 <b>Essai technique /engulfing</b> : il ne compte pas, et "
              "aucun taux n'y est montré."
              + (f"\nVersion déployée : {html.escape(version)}."
                 if version else ""))
    if not min_bougies:
        return (entete + f"\nAucune bougie entre le {_utc(debut)} et le "
                f"{_utc(fin - 86400)} : rien à essayer.")
    nom = max(sorted(min_bougies),
              key=lambda p: (min_ticks[p], min_bougies[p]))
    _ETAT.update(etape=f"essai, paire {nom}", total=1,
                 debut_paires=time.monotonic())
    compte: Counter = Counter()
    t1 = time.monotonic()
    gardes, ticks, n_bougies = _lire_paire(nom, debut, fin, base, lue,
                                           compte, avec_ticks, respirer)
    agregats = Agregats()
    agregats.ajouter(gardes)
    t_paire = time.monotonic() - t1
    _ETAT.update(etape="essai, statistiques", faites=1,
                 signaux=agregats.n_signaux)
    # Toute la suite, résultats jetés : une erreur qui n'apparaîtrait
    # qu'au bout de 40 minutes apparaît ici en une.
    try:
        tirages = agregats.tirages()
        lignes_cases = agregats.cases(tirages, respirer)
        lignes_details = agregats.details(["V00_tous"])
        lignes_pertes = agregats.lignes_pertes(tirages)
        texte(agregats, lignes_cases, lignes_pertes, {
            "nom": nom_lecture, "lecture": lecture, "fin": fin,
            "paires": [nom], "resume": resume_lue, "compte": compte,
            "divergents": len(divergents), "avec_ticks": avec_ticks,
            "decodees": ticks.decodees if ticks else 0, "requetes": 0,
            "refusees": 0, "temps": 0.0})
        tailles = sum(len(c) for c, _, _ in fichiers(
            agregats, lignes_cases, lignes_details, lignes_pertes,
            resume_lue, compte))
        suite = (f"Statistiques, rapport et fichiers : passés "
                 f"({tailles // 1024} Kio de fichiers, jetés).")
    except Exception as erreur:                          # noqa: BLE001
        log.exception("Essai Engulfing : la suite a échoué")
        suite = ("⚠ Statistiques ou rapport en échec : "
                 + html.escape(f"{type(erreur).__name__}: {erreur}")[:300])
    t_total = time.monotonic() - t0
    jours_essai = max(1, (fin - debut) // 86400)
    jours = max(1, (fin - DEBUT) // 86400)
    n_paires = len(min_bougies)
    projection = (t_paire * n_paires * jours / jours_essai
                  + t_resumes * jours / jours_essai) / 60
    rss_depart, rss_max = _ETAT.get("rss_depart"), _ETAT.get("rss_max")
    _, pic_processus = memoire.processus()
    utilisee, plafond = memoire.conteneur()
    contexte = []
    if pic_processus is not None:
        contexte.append(f"pic depuis le démarrage du service "
                        f"{pic_processus:.0f} Mo")
    if utilisee is not None:
        contexte.append(f"conteneur {utilisee:.0f}"
                        + (f" / {plafond:.0f} Mo" if plafond else " Mo"))
    if rss_depart is not None and rss_max is not None:
        hausse = max(0.0, rss_max - rss_depart) * jours / jours_essai
        memoire_txt = (f"Mémoire du processus : {rss_depart:.0f} Mo au "
                       f"départ, pic {rss_max:.0f} Mo pendant l'essai")
        pic_txt = (f" ; mémoire au plus de l'ordre de {rss_depart:.0f} + "
                   f"{hausse:.0f} Mo (extrapolation linéaire, prudente : "
                   f"les paires passent une à une)")
    else:
        memoire_txt = "Mémoire du processus : inconnue"
        pic_txt = ""
    memoire_txt += (" (" + " ; ".join(contexte) + ")." if contexte else ".")
    illisibles = ticks.illisibles if ticks else 0
    decodees = ticks.decodees if ticks else 0
    motifs = sum(n for cle, n in compte.items()
                 if len(cle) == 3 and cle[0] == "motifs")
    requetes = sum(lue.requetes.values()) + (
        sum(active.requetes.values()) if active is not None else 0)
    refusees = lue.refusees + (active.refusees if active else 0)
    return "\n".join([
        entete,
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in lecture.items())
        + ". Rien n'est écrit en base, aucun ordre.",
        f"Paire essayée : {html.escape(nom)} (la plus fournie en ticks), du "
        f"{_utc(debut)} au {_utc(fin - 86400)} ({jours_essai} jours).",
        f"Lu : {_entier(n_bougies)} bougies, {_entier(min_ticks[nom])} "
        f"minutes de ticks dont {_entier(decodees)} décodées "
        f"({illisibles} illisibles) ; motifs : {_entier(motifs)} ; signaux "
        f"gardés : {_entier(agregats.n_signaux)}"
        + ("" if avec_ticks else " ; ⚠ sans les ticks") + ".",
        suite,
        f"Durées : résumés des bases {t_resumes:.0f} s, paire {t_paire:.0f} "
        f"s, total {t_total:.0f} s ; requêtes {requetes}, refusées "
        f"{refusees}.",
        memoire_txt,
        f"Projection prudente de /engulfing complet ({n_paires} "
        f"paire{'s' if n_paires > 1 else ''} × {jours} jours) : environ "
        f"{max(1, round(projection))} min" + pic_txt + ".",
        "➡ Si l'essai est passé, lancez /engulfing ; un point d'avancement "
        "arrive après 2 minutes, puis toutes les 10 minutes.",
    ])


# --------------------------------------------------------------------------- #
# Le rapport et les fichiers
# --------------------------------------------------------------------------- #

def _entier(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.1f} %".replace(".", ",")


def texte(agregats: Agregats, lignes_cases, lignes_pertes,
          meta: Mapping) -> str:
    fin = meta["fin"]
    compte: Counter = meta["compte"]
    resume = meta["resume"]
    minutes_b = sum(v[0] for (t, _, _), v in resume.items()
                    if t == "candles" and v and v[0])
    minutes_t = sum(v[0] for (t, _, _), v in resume.items()
                    if t == "tick_paths" and v and v[0])
    motifs = {p: sum(n for cle, n in compte.items()
                     if len(cle) == 3 and cle[0] == "motifs" and cle[1] == p)
              for p in (EXPLORATION, VALIDATION)}
    v00 = agregats.v00
    n_v00 = sum(n for (source, _), n in v00.items() if source == "m1")
    principal = {(l["variante"], l["echeance_min"], l["periode"]): l
                 for l in lignes_cases if l["mode"] == "ticks"
                 and l["delai_sec"] == DELAI_PRINCIPAL}
    m1 = {(l["variante"], l["echeance_min"], l["periode"]): l
          for l in lignes_cases if l["mode"] == "m1"}
    morceaux = [
        "🕯 <b>Étude Engulfing OTC</b> (recherche isolée, lecture seule)",
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in meta["lecture"].items())
        + f" ; requêtes refusées : {meta['refusees']}. Rien n'a été écrit "
        "en base, aucun ordre.",
        f"Gel vérifié : empreinte {EMPREINTE[:8]}…",
        f"<b>Données</b> : {len(meta['paires'])} paires, du {_utc(DEBUT)} au "
        f"{_utc(fin - 86400)} ; exploration jusqu'au "
        f"{_utc(FIN_EXPLORATION - 86400)}, validation ensuite. Minutes de "
        f"bougies : {_entier(minutes_b)} ; minutes de ticks : "
        f"{_entier(minutes_t)} ; minutes décodées : "
        f"{_entier(meta['decodees'])} ; jours lus sur l'active : "
        f"{meta['divergents']}."
        + ("" if meta["avec_ticks"] else
           " ⚠ Lancée SANS les ticks : seule l'approximation M1 existe."),
        f"<b>Motifs</b> : {motifs[EXPLORATION]} en exploration, "
        f"{motifs[VALIDATION]} en validation. Épisodes retenus sans filtre "
        f"(V00) : {n_v00}.",
        f"Atteinte du niveau dans les 10 min (V00) : ticks "
        f"{v00[('ticks', DECLENCHE)]} déclenchés, "
        f"{v00[('ticks', NON_DECLENCHE)]} non déclenchés, "
        f"{v00[('ticks', SANS_TICKS)]} sans ticks ; bougies M1 "
        f"{v00[('m1', DECLENCHE)]} déclenchés.",
        "<b>V00, sans filtre</b> (ticks, entrée 2 s après l'atteinte, "
        "décisions jouables) :"]
    for t in ECHEANCES:
        x = principal.get(("V00_tous", t, EXPLORATION), {})
        v = principal.get(("V00_tous", t, VALIDATION), {})
        a = m1.get(("V00_tous", t, VALIDATION), {})
        morceaux.append(
            f"• {t} min : exploration {_pct(x.get('taux'))} sur "
            f"{x.get('n', 0)} ; validation {_pct(v.get('taux'))} sur "
            f"{v.get('n', 0)} [{_pct(v.get('ic_bas'))} ; "
            f"{_pct(v.get('ic_haut'))}] ; M1 approximé "
            f"{_pct(a.get('taux'))}.")
    validees = [l for l in lignes_cases
                if l.get("verdict") == "validée (BH 10 %)"]
    reproduites = [l for l in lignes_cases
                   if l.get("verdict") == "non reproduite"]
    retenues = [l for l in lignes_cases
                if l.get("verdict", "").startswith("retenue")]
    morceaux.append(
        f"<b>Variantes</b> (13 × 5 échéances) : {len(retenues)} cases "
        f"retenues en exploration, {len(validees)} validées, "
        f"{len(reproduites)} non reproduites.")
    for l in validees:
        morceaux.append(
            f"• ✅ {l['variante']} à {l['echeance_min']} min : "
            f"{_pct(l['taux'])} sur {l['n']} en validation "
            f"[{_pct(l.get('ic_bas'))} ; {_pct(l.get('ic_haut'))}].")
    if not validees:
        morceaux.append("Aucune variante ne bat le point mort de 52,08 % "
                        "hors échantillon.")
    morceaux.append(
        "Scénario A, INVALIDE (entrée à l'ouverture de c1, connue après "
        "coup) : " + " ; ".join(
            f"{t} min {_pct(g / n if n else None)}"
            for t, (g, n) in sorted(agregats.a.items()))
        + ". C'est l'illusion de l'anticipation, pas une stratégie.")
    notees = sorted({l["caracteristique"] for l in lignes_pertes
                     if l.get("notee") == "oui"})
    morceaux.append(
        "Étude des pertes : " + (
            "caractéristiques à écart reproduit : " + ", ".join(notees)
            if notees else "aucune caractéristique connue à la décision ne "
            "sépare gagnants et perdants dans les deux périodes") + ".")
    morceaux.append("Les mesures d'après l'entrée sont descriptives. Rien ne "
                    "change dans la course ; une variante validée se gèle "
                    "puis se confirme sur des jours futurs.")
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


def _csv(lignes: Sequence[Mapping], colonnes: Sequence[str]) -> bytes:
    sortie = io.StringIO()
    w = csv.writer(sortie, delimiter=";")
    w.writerow(colonnes)
    for l in lignes:
        w.writerow([_cellule_csv(l.get(c)) for c in colonnes])
    return sortie.getvalue().encode("utf-8")


COLONNES_CASES = (
    "variante", "mode", "delai_sec", "echeance_min", "periode", "n", "gains",
    "pertes", "egalites", "taux", "taux_sans_egalites", "ic_bas", "ic_haut",
    "p", "ecart_v00", "ecart_bas", "ecart_haut", "serie_pertes_max",
    "verdict", "drapeau")
COLONNES_DETAILS = ("variante", "echeance_min", "periode", "dimension",
                    "valeur", "n", "gains", "taux", "ic_bas", "ic_haut",
                    "drapeau")
COLONNES_PERTES = ("echeance_min", "periode", "caracteristique", "nature",
                   "n_gagnants", "n_perdants", "moyenne_gagnants",
                   "moyenne_perdants", "ecart", "ic_bas", "ic_haut", "notee")


def fichiers(agregats: Agregats, lignes_cases, lignes_details, lignes_pertes,
             resume, compte) -> list[tuple[bytes, str, str]]:
    donnees = defaultdict(dict)
    for (table, paire_, jour), v in resume.items():
        if v:
            donnees[(paire_, jour)][table] = v
    lignes_donnees = [{
        "paire": p, "jour": _utc(j * 86400, "%Y-%m-%d"),
        "minutes_bougies": d.get("candles", (None,))[0],
        "bougies_completes": (d["candles"][2] if "candles" in d else None),
        "minutes_ticks": d.get("tick_paths", (None,))[0],
        "ticks": (d["tick_paths"][1] if "tick_paths" in d else None),
        "releves_payout": d.get("payouts", (None,))[0]}
        for (p, j), d in sorted(donnees.items())]
    lignes_statuts = [{"source": k[0], "periode": k[1], "statut": k[2],
                       "nombre": n}
                      for k, n in sorted(compte.items()) if len(k) == 3]
    lignes_statuts += [{"source": k[0], "periode": "", "statut": k[1],
                        "nombre": n}
                       for k, n in sorted(compte.items()) if len(k) == 2]
    return [
        (_csv(lignes_cases, COLONNES_CASES), "engulfing_cases.csv",
         "Variante × mode × échéance × période, verdicts"),
        (_csv(lignes_details, COLONNES_DETAILS), "engulfing_details.csv",
         "Par paire, par sens, par semaine (Wilson)"),
        (_csv(lignes_pertes, COLONNES_PERTES), "engulfing_pertes.csv",
         "Gagnants contre perdants"),
        (_csv(lignes_statuts, ("source", "periode", "statut", "nombre")),
         "engulfing_statuts.csv", "Motifs, déclenchements, données manquantes"),
        (_csv(lignes_donnees, ("paire", "jour", "minutes_bougies",
                               "bougies_completes", "minutes_ticks", "ticks",
                               "releves_payout")),
         "engulfing_donnees.csv", "Données réellement présentes"),
        (agregats.signaux_gz(), "engulfing_signaux.csv.gz",
         "Chaque signal retenu, ses statuts et ses issues"),
    ]
