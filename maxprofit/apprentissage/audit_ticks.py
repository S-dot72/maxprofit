"""
Étape 0 — l'audit forensique des ticks, en LECTURE SEULE (cahiers des charges
du 24/10 et v1.0 du 25/10 ; inventaire technique : docs/etape0_ticks.md).

--- La question --------------------------------------------------------------

Avant de chercher dans les ticks ce qui précède un signal gagnant ou perdant,
établir ce qu'ils valent et ce qu'ils permettent d'affirmer : horodatages,
fidélité de la compression, cohérence avec les bougies M1, trous, couverture
autour des signaux ZoneH1, prix d'exécution. Ordre de travail : auditer,
établir la fiabilité, puis seulement étudier la valeur prédictive. La fenêtre
T0/T2/T3 reste gelée : rien d'ici ne la modifie.

--- Ce que dit le CODE (lu, pas supposé) ---------------------------------------

- `collect.collector.CandleAggregator` reçoit chaque tick du flux ; il range
  ceux d'une minute ouverte, et ÉCARTE sans les compter ceux qui arrivent
  après la fermeture de leur minute (« tick en retard … ignoré »).
- `store.chemin_ticks.encoder` TRIE les ticks d'une minute par horodatage
  avant de les écrire (écarts en millisecondes, prix entiers sur la grille
  décimale, zlib). L'ordre d'ARRIVÉE n'est donc pas conservé ; deux ticks au
  même instant sont gardés tous les deux.
- `Tick.ts_ms` est l'horloge du SERVEUR du broker, ramenée en UTC par un
  décalage arrondi à l'heure (`collect.pocketoption._vers_ms`). L'heure de
  RÉCEPTION n'est stockée nulle part.
- `tick_paths` : une ligne par (paire, minute) ; en conflit, le chemin le plus
  fourni l'emporte (`MarketWriter.insert_tick_paths`, et la fusion de bases).
- `candles` : en conflit, l'ouverture d'origine reste, haut = MAX, bas = MIN,
  clôture = la dernière écrite, nombre de ticks = MAX, complète = MAX
  (`MarketWriter.upsert_candles`) ; la fusion de bases garde la bougie déjà
  présente. Une minute coupée par un redémarrage ou une fusion peut donc
  différer de son chemin SANS erreur : ce sont les « écarts explicables ».
- Les signaux ZoneH1 ne sont stockés nulle part : le rejeu les recalcule
  (`apprentissage.historique.exemples_historiques`) ; identifiant
  reproductible `paire|instant de décision`.
- `executions` (ordres réels) : `ouverture_ts_ms` et `expiration_ts_ms` sont
  dans l'horloge du broker ; `decalage_broker_ms` est l'avance de cette
  horloge sur la nôtre, mesurée à l'ordre ; `prix_attendu` est le dernier
  prix vu par la COURSE (sa propre connexion), pas par le collecteur.

--- Les contrôles et leurs seuils, fixés AVANT la première mesure --------------

Voir `SEUILS` et `JUSTIFICATIONS` ; statuts PASS, WARNING, FAIL, NOT TESTABLE.
Chaque anomalie porte son contrôle, son critère, sa mesure, le nombre
d'enregistrements touchés, son impact, la confiance de la détection et le
motif d'une incertitude (`NATURES`).

Ce module ne touche pas la base (règle des couches) : `live` lit les chemins,
les battements et les ordres, et lui passe le décodeur et l'encodeur de
`store.chemin_ticks`. Seules des requêtes SELECT sont exécutées
(`REQUETES`), et la course ne lit rien d'ici.
"""

from __future__ import annotations

import hashlib
import zlib
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from maxprofit.apprentissage import microstructure
from maxprofit.core.errors import BotError
from maxprofit.core.types import Candle, Tick

#: Un écart d'au moins `GEL_SEC` secondes entre deux ticks de minutes
#: contiguës est un gel du flux.
GEL_SEC = 10
#: Gel au-delà duquel l'anomalie passe de INFO à WARNING.
GEL_GRAVE_SEC = 30
#: Tolérance aux bornes d'une fenêtre et avant l'instant de décision.
BORD_SEC = 5
#: En dessous, la minute est « pauvre » (le seuil des bougies peu fiables).
TICKS_MIN_MINUTE = 5
#: La fenêtre autour de chaque signal : 15 min avant la décision, 15 après.
AVANT_SEC = 900
APRES_SEC = 900
#: Minutes consécutives sans ticks à partir desquelles on parle
#: d'interruption longue.
INTERRUPTION_LONGUE_MIN = 30
#: Débit anormal : médiane des ticks par minute d'un jour hors de
#: [0,5 ; 2] fois la médiane de la paire sur toute la fenêtre auditée.
DEBIT_BAS, DEBIT_HAUT = 0.5, 2.0
#: Minutes d'un jour à partir desquelles son débit est jugé.
MINUTES_MIN_JOUR = 60
#: Une minute est « écoutée » si un battement du collecteur tombe dans
#: [minute − 30 s, minute + 90 s[ (battement toutes les 30 s, trou toléré
#: 90 s, comme `MarketReader.couverture`).
BATTEMENT_AVANT_SEC, BATTEMENT_APRES_SEC = 30, 90
#: Fenêtres de recherche d'un prix du broker dans notre flux (ms) ; la
#: deuxième est celle qui juge.
FENETRES_PRIX_MS: tuple[int, ...] = (0, 1000, 2000, 5000)
FENETRE_PRIX_JUGEE_MS = 1000
#: Ordres comparables en dessous desquels le contrôle des prix est non
#: testable ; et en dessous desquels l'analyse n'est que descriptive.
ORDRES_MIN = 10
ORDRES_ANALYSE = 30
#: Tranches de latence (ouverture chez le broker − signal), en ms.
TRANCHES_LATENCE_MS: tuple[int, ...] = (1000, 2000, 5000)
#: Une minute sur `ECHANTILLON_REENCODAGE` est réencodée et comparée à ses
#: octets stockés ; une fenêtre de `LECTURE_MIN` minutes par paire et par jour
#: est relue par `MarketReader.ticks()` et comparée au décodage.
ECHANTILLON_REENCODAGE = 10
LECTURE_MIN = 10
#: Pseudo-contrats témoins de la fin d'échéance : une entrée toutes les
#: `PAS_TEMOIN_MIN` minutes de chaque paire, sans signal.
PAS_TEMOIN_MIN = 31
#: Bornes (ms) de l'histogramme des écarts entre ticks consécutifs.
BORNES_ECARTS_MS: tuple[int, ...] = (
    10, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000)
#: Anomalies gardées en mémoire, par gravité (les comptes, eux, sont
#: exacts).
ANOMALIES_MAX = {"FAIL": 3000, "WARNING": 3000, "INFO": 2000}
GRAVITES = ("FAIL", "WARNING", "INFO")
NON_TESTABLE = "NOT TESTABLE"

SEUILS: dict[str, str] = {
    "horodatages": (
        "FAIL : horodatages à la seconde, tick hors de sa minute ou ordre "
        "décroissant ; WARNING : doublons exacts au-delà de 1 % des ticks, ou "
        "gels de 10 s ou plus sur plus de 1 % des minutes ; PASS sinon"),
    "compression": (
        "FAIL : un chemin illisible, un réencodage différent des octets "
        "stockés, ou une relecture par MarketReader.ticks() différente du "
        "décodage ; PASS sinon ; NOT TESTABLE si rien n'a été contrôlé"),
    "concordance": (
        "FAIL : écarts inexpliqués (dont décalages de minute) sur plus de "
        "0,1 % des minutes comparées ; WARNING : au moins un écart "
        "inexpliqué, une minute de ticks sans bougie, ou plus de 2 % d'écarts "
        "explicables ; PASS sinon"),
    "trous": (
        "sur les minutes où le collecteur battait, paires sans aucun tick "
        "exclues : FAIL au-delà de 5 % sans ticks, WARNING au-delà de 1 %, "
        "PASS sinon ; NOT TESTABLE sans battement"),
    "signaux": (
        "fenêtres de 30 min complètes : FAIL sous 50 %, WARNING sous 80 %, "
        "PASS sinon ; NOT TESTABLE sans signal"),
    "prix": (
        "prix d'ouverture ET de clôture du broker retrouvés dans le flux à "
        "± 1 s : FAIL sous 70 %, WARNING sous 90 %, PASS sinon ; NOT TESTABLE "
        "sous 10 ordres comparables"),
    "ticks_tardifs": "NOT TESTABLE : aucun compteur persistant",
    "heure_reception": "NOT TESTABLE : l'heure de réception n'est pas stockée",
}

JUSTIFICATIONS: dict[str, str] = {
    "horodatages": (
        "une étude sous la minute exige la milliseconde ; un gel de 10 s "
        "représente une vingtaine de ticks au débit de conception"),
    "compression": "« sans perte » doit être démontré sur les données",
    "concordance": (
        "bougie et chemin viennent des mêmes ticks : tout écart inexpliqué "
        "est une erreur de stockage"),
    "trous": (
        "au-delà de 5 %, une fenêtre de 30 min a plus d'une chance sur deux "
        "d'être touchée"),
    "signaux": (
        "sous 50 %, l'échantillon exploitable cesse d'être représentatif"),
    "prix": "le seul contrôle externe au flux",
    "ticks_tardifs": "écartés par l'agrégateur sans trace",
    "heure_reception": "seule l'heure du serveur du broker est conservée",
}

#: Les requêtes, telles qu'exécutées : SELECT seulement. Les deux premières
#: sont celles de `MarketReader.tick_paths` et `MarketReader.candles`.
REQUETES: dict[str, str] = {
    "chemins": ("SELECT pair, minute_sec, n_ticks, echelle, chemin FROM "
                "tick_paths WHERE pair = ? AND minute_sec >= ? AND "
                "minute_sec < ? ORDER BY minute_sec"),
    "bougies": ("SELECT pair, tf_sec, ts_sec, open, high, low, close, "
                "tick_count, complete FROM candles WHERE pair = ? AND "
                "tf_sec = 60 AND ts_sec >= ? AND ts_sec < ? ORDER BY ts_sec"),
    "battements": ("SELECT ts_sec FROM uptime WHERE ts_sec >= ? AND "
                   "ts_sec < ? ORDER BY ts_sec"),
    "ordres": ("SELECT pair, sens, signal_ts_ms, clic_ts_ms, accepte_ts_ms, "
               "prix_attendu, prix_entree, prix_sortie, ouverture_ts_ms, "
               "expiration_ts_ms, decalage_broker_ms, expiration_sec, "
               "accepte, resultat, brut FROM executions ORDER BY "
               "signal_ts_ms"),
}

#: Les codes de complétude d'une fenêtre de signal, dans l'ordre où ils sont
#: testés : le premier rencontré est retenu (docs/etape0_ticks.md §3).
CODES: dict[str, str] = {
    "NO_TICKS": "aucune minute de ticks",
    "DECOMPRESSION_ERROR": "une minute illisible",
    "INTERNAL_GAP": "minute(s) absente(s)",
    "INVALID_ORDER": "tick hors de sa minute ou ordre décroissant",
    "TIMESTAMP_UNCERTAIN": "ticks sans battement du collecteur, ou "
                           "horodatages à la seconde",
    "START_BOUNDARY_MISSING": "premier tick à plus de 5 s du début",
    "END_BOUNDARY_MISSING": "dernier tick à plus de 5 s de la fin",
    "FEED_FREEZE": "gel du flux de 10 s ou plus",
    "INSUFFICIENT_COVERAGE": "minute à moins de 5 ticks",
    "M1_MISMATCH": "concordance M1 inexpliquée",
    "NO_TICK_AT_DECISION": "pas de tick dans les 5 s avant la décision",
}
COMPLETE = "OK"

#: Classes de concordance d'une minute (chemin face à sa bougie), et la
#: catégorie du cahier des charges (§5.2) dont chacune relève.
IDENTIQUE = "identique"
EXPLICABLES = ("bougie incomplète", "minute coupée",
               "chemin moins fourni", "chemin plus fourni")
DECALAGE = "décalage de minute"
INEXPLIQUE = "écart inexpliqué"
CATEGORIES: dict[str, str] = {
    IDENTIQUE: "concordance exacte",
    "bougie incomplète": "convention d'agrégation (bougie écrite à une "
                         "coupure)",
    "chemin plus fourni": "convention d'agrégation (le chemin le plus fourni "
                          "gagne ; fusion de bases)",
    "minute coupée": "bornes temporelles (minute coupée)",
    "chemin moins fourni": "tick manquant dans le chemin (établi par les "
                           "comptes)",
    DECALAGE: "bornes temporelles (décalage de minute)",
    INEXPLIQUE: "divergence inexpliquée",
}

#: nature → (contrôle, critère, impact, confiance, motif d'incertitude).
NATURES: dict[str, tuple[str, str, str, str, str]] = {
    "chemin illisible": (
        "compression", "zlib, échelle ou nombre de ticks incohérents "
        "(decoder lève)", "minute perdue pour toute analyse sous la minute",
        "élevée", ""),
    "réencodage différent du stocké": (
        "compression", "les ticks décodés, réencodés, ne redonnent pas les "
        "octets stockés (avant zlib)", "stockage non démontré sans perte "
        "pour cette minute", "moyenne",
        "un changement de version de l'encodeur l'expliquerait aussi"),
    "relecture différente du décodage": (
        "compression", "MarketReader.ticks() ne rend pas les ticks des "
        "chemins de la fenêtre", "toute lecture par l'API serait faussée",
        "élevée", ""),
    "tick hors de sa minute": (
        "horodatages", "un tick hors de [minute, minute + 60 s[",
        "minute mal rangée", "élevée", ""),
    "horodatages décroissants": (
        "horodatages", "deux ticks consécutifs dans l'ordre inverse",
        "ordre des événements non fiable", "élevée", ""),
    "gel du flux": (
        "horodatages", f"écart de {GEL_SEC} s ou plus entre deux ticks de "
        "minutes contiguës", "trajectoire non observée pendant le gel",
        "élevée", "un marché immobile ne se distingue pas d'un flux muet"),
    "concordance M1 inexpliquée": (
        "concordance", "OHLC ou nombre de ticks différents sans règle "
        "d'écriture qui l'explique", "bougie ou chemin faux sur cette minute",
        "élevée", ""),
    "décalage de minute": (
        "concordance", "le chemin correspond exactement à la bougie de la "
        "minute voisine", "horodatages décalés d'une minute", "élevée", ""),
    "minute de ticks sans bougie": (
        "concordance", "un chemin sans bougie à la même minute",
        "comparaison impossible", "élevée", ""),
    "interruption longue": (
        "trous", f"{INTERRUPTION_LONGUE_MIN} min consécutives ou plus sans "
        "ticks", "fenêtres de signal touchées exclues", "élevée",
        "la cause (collecteur, abonnement, flux) n'est établie que par les "
        "battements"),
    "paire sans aucun tick dans la fenêtre": (
        "trous", "aucun chemin pour la paire sur toute la fenêtre",
        "paire inexploitable au tick", "élevée",
        "paire non souscrite ou stockage des ticks coupé"),
    "ticks sans battement du collecteur": (
        "trous", "minute de ticks sans battement dans [m − 30 s, m + 90 s[",
        "horodatage possiblement décalé (changement d'heure)", "moyenne",
        "des battements perdus l'expliqueraient aussi"),
    "débit anormal": (
        "trous", "médiane du jour hors de [0,5 ; 2] fois la médiane de la "
        "paire sur la fenêtre", "qualité du flux à vérifier ce jour-là",
        "moyenne", "un marché plus calme réduit aussi le débit"),
    "prix d'ouverture absent du flux à ± 1 s": (
        "prix", "le prix d'ouverture du broker n'est égal à aucun tick à "
        "± 1 s de l'ouverture (horloge du broker)", "exécution non "
        "reconstituable depuis le flux", "moyenne",
        "horodatage d'ouverture à la seconde ; synchronisation des horloges"),
    "prix de clôture absent du flux à ± 1 s": (
        "prix", "le prix de clôture du broker n'est égal à aucun tick à "
        "± 1 s de l'expiration", "issue non reconstituable depuis le flux",
        "moyenne",
        "horodatage d'expiration à la seconde ; synchronisation des horloges"),
}

HEURE_MS = 3_600_000
LIBELLES_PRIX = {"ouverture": "d'ouverture", "cloture": "de clôture"}


def _jour(ts: int) -> int:
    return ts // 86400 * 86400


def _horodatage(ts_sec: float | None) -> str:
    if ts_sec is None:
        return ""
    return datetime.fromtimestamp(ts_sec, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")


def _quantile(valeurs: Sequence[float], q: float) -> float | None:
    v = sorted(x for x in valeurs if x is not None)
    if not v:
        return None
    return v[min(len(v) - 1, int(q * len(v)))]


def _quantiles(valeurs: Sequence[float],
               qs: Sequence[float] = (0.1, 0.25, 0.5, 0.75, 0.9)) -> list:
    return [_quantile(valeurs, q) for q in qs]


def _quantile_histogramme(histo: Mapping[int, int], q: float) -> int | None:
    """La borne supérieure du casier qui contient le quantile `q` ; -1 :
    au-delà de la dernière borne."""
    total = sum(histo.values())
    if not total:
        return None
    cumul = 0
    for k in sorted(histo):
        cumul += histo[k]
        if cumul >= q * total:
            return BORNES_ECARTS_MS[k] if k < len(BORNES_ECARTS_MS) else -1
    return -1


def _casier(ecart_ms: int) -> int:
    for k, borne in enumerate(BORNES_ECARTS_MS):
        if ecart_ms < borne:
            return k
    return len(BORNES_ECARTS_MS)


def _decimales(prix: float) -> int:
    """Les décimales significatives d'un prix coté (8 au plus)."""
    for d in range(9):
        if abs(prix * 10 ** d - round(prix * 10 ** d)) < 1e-6:
            return d
    return 8


class _Compteurs:
    """Ce qui se compte par paire, par jour et par heure."""

    def __init__(self) -> None:
        self.minutes = 0
        self.ticks = 0
        self.par_minute: list[int] = []
        self.ecarts: Counter = Counter()
        self.ecart_max = 0
        self.gels = 0
        self.pauvres = 0
        self.doublons_exacts = 0
        self.concordance: Counter = Counter()
        self.manquantes_ecoutees = 0
        self.manquantes_muettes = 0
        self.bougie_sans_chemin = 0
        self.chemin_sans_bougie = 0
        self.interruptions = 0
        self.interruptions_min = 0
        self.signaux = 0
        self.signaux_complets = 0

    def resume(self, attendues: int) -> dict:
        comparees = sum(self.concordance.values())
        return {
            "attendues": attendues, "minutes": self.minutes,
            "couverture": round(self.minutes / attendues, 4)
            if attendues else None,
            "ticks": self.ticks,
            "ticks_min": _quantiles(self.par_minute),
            "ecarts_ms": [_quantile_histogramme(self.ecarts, q)
                          for q in (0.25, 0.5, 0.75, 0.99)]
            + [self.ecart_max],
            "gels": self.gels, "pauvres": self.pauvres,
            "doublons_exacts": self.doublons_exacts,
            "comparees": comparees,
            "identiques": self.concordance.get(IDENTIQUE, 0),
            "inexpliques": self.concordance.get(INEXPLIQUE, 0)
            + self.concordance.get(DECALAGE, 0),
            "manquantes_ecoutees": self.manquantes_ecoutees,
            "manquantes_muettes": self.manquantes_muettes,
            "bougie_sans_chemin": self.bougie_sans_chemin,
            "chemin_sans_bougie": self.chemin_sans_bougie,
            "interruptions": [self.interruptions, self.interruptions_min],
            "signaux": [self.signaux, self.signaux_complets]}


class AuditTicks:
    """L'audit de la fenêtre [debut, fin[, paire après paire.

    `decoder` : chemin compressé → ticks (`store.chemin_ticks.decoder`) ;
    `encoder` : ticks → chemin (`store.chemin_ticks.encoder`), pour le
    contrôle de compression ; `joue` : le prédicat des signaux que la course
    joue (règle de l'heure), pour une couverture à part.
    """

    def __init__(self, debut: int, fin: int, *,
                 decoder: Callable[[Any], Sequence[Tick]],
                 encoder: Callable[[Sequence[Tick]], Any] | None = None,
                 joue: Callable[[Any], bool] | None = None):
        self.decoder = decoder
        self.encoder = encoder
        self.debut = int(debut) // 60 * 60
        self.fin = int(fin) // 60 * 60
        self.joue = joue
        self.battements: list[int] = []
        self._ecoutees: set[int] | None = None
        self.paires: dict[str, _Compteurs] = {}
        self.jours: dict[int, _Compteurs] = {}
        self.heures: dict[int, _Compteurs] = {h: _Compteurs()
                                              for h in range(24)}
        self.global_ = _Compteurs()
        # Horodatages
        self.resolution = Counter()        # multiples de 1000, 100, 10
        self.ecarts: Counter = Counter()   # histogramme des écarts
        self.ecart_max_ms = 0
        self.doublons_instant = 0
        self.hors_minute = 0
        self.decroissants = 0
        self.minutes_gelees = 0
        #: Minutes de ticks sans battement du collecteur : un horodatage
        #: décalé (changement d'heure du broker) se verrait ici.
        self.sans_battement = 0
        # Compression
        self.illisibles = 0
        self.reencodees = [0, 0]           # contrôlées, différentes
        self.relectures = [0, 0, 0]        # fenêtres, ticks, différentes
        # Concordance
        self.concordance: Counter = Counter()
        self.ecarts_concordance: Counter = Counter()   # champ en défaut
        self.exemples: dict[str, list] = {}
        # Signaux
        self.fiches_signaux: list[dict] = []
        self.codes: Counter = Counter()
        self.codes_joues: Counter = Counter()
        self.hors_fenetre = 0
        self.par_jour_signaux: dict[int, list[int]] = {}
        self.par_heure_signaux: dict[int, list[int]] = {}
        # Phase F
        self.trajectoires: list[dict] = []
        self.temoins: list[dict] = []
        # Prix
        self.fiches_ordres: list[dict] = []
        # Anomalies
        self.anomalies: dict[str, list[dict]] = {g: [] for g in GRAVITES}
        self.comptes_anomalies: Counter = Counter()
        self._empreinte = hashlib.sha256()
        self.chemins_lus = 0

    # --- anomalies ------------------------------------------------------------

    def _anomalie(self, gravite: str, nature: str, paire: str, debut: float,
                  fin: float | None = None, mesure: str = "",
                  n: int = 1) -> None:
        self.comptes_anomalies[(gravite, nature)] += 1
        liste = self.anomalies[gravite]
        if len(liste) < ANOMALIES_MAX[gravite]:
            controle, critere, impact, confiance, incertitude = NATURES[nature]
            fin = fin if fin is not None else debut
            liste.append({
                "gravite": gravite, "nature": nature, "controle": controle,
                "paire": paire, "debut": debut, "fin": fin,
                "duree_sec": fin - debut, "mesure": mesure, "n": n,
                "critere": critere, "impact": impact,
                "confiance": confiance, "incertitude": incertitude})

    # --- battements ------------------------------------------------------------

    def noter_battements(self, battements: Iterable[int]) -> None:
        self.battements = sorted(int(b) for b in battements)
        self._ecoutees = None

    def _minutes_ecoutees(self) -> set[int] | None:
        """None quand aucun battement n'est connu : « écoutée » est alors
        indéterminé, pas faux."""
        if not self.battements:
            return None
        if self._ecoutees is None:
            ecoutees = set()
            for b in self.battements:
                premiere = (b - BATTEMENT_APRES_SEC) // 60 * 60
                for m in range(premiere, b + BATTEMENT_AVANT_SEC + 1, 60):
                    if m - BATTEMENT_AVANT_SEC <= b < m + BATTEMENT_APRES_SEC:
                        ecoutees.add(m)
            self._ecoutees = ecoutees
        return self._ecoutees

    # --- une paire ---------------------------------------------------------------

    def _compteurs(self, paire: str, minute: int) -> list[_Compteurs]:
        return [self.global_,
                self.paires.setdefault(paire, _Compteurs()),
                self.jours.setdefault(_jour(minute), _Compteurs()),
                self.heures[minute % 86400 // 3600]]

    def ajouter_paire(self, paire: str, bougies: Sequence[Candle],
                      chemins: Sequence[Any], signaux: Sequence[Any] = (),
                      respirer: Callable[[], None] | None = None,
                      relire: Callable[[str, int, int], Sequence[Tick]]
                      | None = None) -> None:
        """`bougies` : les M1 de la paire (au-delà de la fenêtre, ignorées
        par les contrôles ; les 60 avant un signal donnent son amplitude) ;
        `chemins` : ses chemins de la fenêtre, dans l'ordre ; `signaux` : ses
        signaux ZoneH1 (`ts_sec` = instant de décision) ; `relire` :
        `MarketReader.ticks`, pour le contrôle de relecture."""
        self.paires.setdefault(paire, _Compteurs())
        toutes = {b.ts_sec: b for b in bougies if b.tf_sec == 60}
        bougies_m = {m: b for m, b in toutes.items()
                     if self.debut <= m < self.fin}
        par_minute = {c.minute_sec: c for c in chemins
                      if self.debut <= c.minute_sec < self.fin}
        resumes: dict[int, dict] = {}
        illisibles: set[int] = set()
        dernier_ms = None
        derniere_minute = None
        for k, (m, chemin) in enumerate(sorted(par_minute.items())):
            if respirer is not None and k and k % 2000 == 0:
                respirer()
            self.chemins_lus += 1
            self._empreinte.update(
                f"{paire}|{m}|{chemin.n_ticks}|{chemin.echelle}|".encode())
            self._empreinte.update(chemin.octets)
            try:
                ticks = self.decoder(chemin)
            except BotError as erreur:
                self.illisibles += 1
                illisibles.add(m)
                self._anomalie("FAIL", "chemin illisible", paire, m, m + 60,
                               str(erreur)[:200], chemin.n_ticks)
                continue
            if self.encoder is not None and k % ECHANTILLON_REENCODAGE == 0:
                self._reencoder(paire, m, chemin, ticks)
            r = self._minute(paire, m, ticks, chemin.echelle,
                             dernier_ms if derniere_minute == m - 60
                             else None)
            r["concordance"] = self._concorder(paire, m, r, bougies_m)
            resumes[m] = r
            dernier_ms, derniere_minute = r["dernier"], m
        if relire is not None:
            self._relire(paire, resumes, par_minute, relire)
        self._trous_et_debit(paire, resumes, bougies_m)
        self._signaux(paire, resumes, illisibles, signaux, par_minute,
                      toutes)
        self._temoins(paire, resumes, par_minute)

    def _reencoder(self, paire: str, m: int, chemin: Any,
                   ticks: Sequence[Tick]) -> None:
        """Les ticks décodés, réencodés, redonnent-ils les octets stockés ?
        Comparés AVANT zlib : deux versions de zlib peuvent compresser
        différemment le même contenu."""
        self.reencodees[0] += 1
        try:
            identique = (zlib.decompress(self.encoder(ticks).octets)
                         == zlib.decompress(chemin.octets))
        except Exception:                          # noqa: BLE001
            identique = False
        if not identique:
            self.reencodees[1] += 1
            self._anomalie("FAIL", "réencodage différent du stocké", paire,
                           m, m + 60, f"{len(ticks)} ticks", len(ticks))

    def _relire(self, paire: str, resumes: Mapping[int, dict],
                par_minute: Mapping[int, Any], relire) -> None:
        """Une fenêtre de `LECTURE_MIN` minutes par jour — la première
        minute de ticks après midi UTC, sinon la première du jour —, relue par
        `MarketReader.ticks()` et comparée au décodage des mêmes chemins."""
        premieres: dict[int, int] = {}
        midis: dict[int, int] = {}
        for m in sorted(resumes):
            jour = _jour(m)
            premieres.setdefault(jour, m)
            if m >= jour + 12 * 3600:
                midis.setdefault(jour, m)
        for jour, premiere in sorted(premieres.items()):
            debut = midis.get(jour, premiere)
            fin = debut + 60 * LECTURE_MIN
            attendus = []
            for m in range(debut, fin, 60):
                if m in par_minute and m in resumes:
                    attendus.extend(self.decoder(par_minute[m]))
            try:
                lus = list(relire(paire, debut, fin))
            except Exception as erreur:            # noqa: BLE001
                lus, attendus = [repr(erreur)], []
            self.relectures[0] += 1
            self.relectures[1] += len(attendus)
            if [(t.ts_ms, t.price) for t in attendus] != [
                    (t.ts_ms, t.price) for t in lus
                    if isinstance(t, Tick)] or len(lus) != len(attendus):
                self.relectures[2] += 1
                self._anomalie(
                    "FAIL", "relecture différente du décodage", paire, debut,
                    fin, f"{len(lus)} ticks relus pour {len(attendus)} "
                    f"décodés", len(attendus))

    def _minute(self, paire: str, m: int, ticks: Sequence[Tick],
                echelle: int, dernier_ms: int | None) -> dict:
        base = m * 1000
        ts = [t.ts_ms for t in ticks]
        prix = [t.price for t in ticks]
        n = len(ts)
        hors = sum(1 for x in ts if not base <= x < base + 60_000)
        if hors:
            self.hors_minute += hors
            self._anomalie("FAIL", "tick hors de sa minute", paire, m, m + 60,
                           f"{hors} tick(s)", hors)
        decr = sum(1 for a, b in zip(ts, ts[1:]) if b < a)
        if decr:
            self.decroissants += decr
            self._anomalie("FAIL", "horodatages décroissants", paire, m,
                           m + 60, f"{decr} fois", decr)
        for x in ts:
            if x % 1000 == 0:
                self.resolution[1000] += 1
            if x % 100 == 0:
                self.resolution[100] += 1
            if x % 10 == 0:
                self.resolution[10] += 1
        self.resolution["n"] += n
        meme_instant = exacts = 0
        ecarts = [b - a for a, b in zip(ts, ts[1:])]
        for k, e in enumerate(ecarts):
            if e == 0:
                meme_instant += 1
                exacts += prix[k] == prix[k + 1]
        self.doublons_instant += meme_instant
        entree = ts[0] - dernier_ms if dernier_ms is not None else None
        tous = ecarts + ([entree] if entree is not None else [])
        ecart_max = max(tous, default=0)
        cases = [_casier(e) for e in tous]
        for k in cases:
            self.ecarts[k] += 1
        self.ecart_max_ms = max(self.ecart_max_ms, ecart_max)
        gel = ecart_max >= GEL_SEC * 1000
        if gel:
            self.minutes_gelees += 1
            self._anomalie(
                "WARNING" if ecart_max >= GEL_GRAVE_SEC * 1000 else "INFO",
                "gel du flux", paire, m, m + 60,
                f"{ecart_max / 1000:.1f} s sans tick")
        for c in self._compteurs(paire, m):
            c.minutes += 1
            c.ticks += n
            c.par_minute.append(n)
            for k in cases:
                c.ecarts[k] += 1
            c.ecart_max = max(c.ecart_max, ecart_max)
            c.gels += gel
            c.pauvres += n < TICKS_MIN_MINUTE
            c.doublons_exacts += exacts
        return {"n": n, "premier": ts[0], "dernier": ts[-1],
                "ecart_max": ecart_max, "entree": entree,
                "invalide": bool(hors or decr), "o": prix[0], "h": max(prix),
                "l": min(prix), "c": prix[-1],
                "pas": 10 ** -echelle if echelle else 1.0}

    def _concorder(self, paire: str, m: int, r: dict,
                   bougies_m: Mapping[int, Candle]) -> str | None:
        """La classe de la minute, chemin face à sa bougie (None : pas de
        bougie). Tolérance : une demi-unité de la grille décimale."""
        b = bougies_m.get(m)
        if b is None:
            for c in self._compteurs(paire, m):
                c.chemin_sans_bougie += 1
            self._anomalie("WARNING", "minute de ticks sans bougie", paire,
                           m, m + 60, f"{r['n']} ticks", r["n"])
            return None
        tol = r["pas"] / 2 + 1e-12

        def pareille(bougie: Candle) -> dict[str, bool]:
            return {"o": abs(bougie.open - r["o"]) <= tol,
                    "h": abs(bougie.high - r["h"]) <= tol,
                    "l": abs(bougie.low - r["l"]) <= tol,
                    "c": abs(bougie.close - r["c"]) <= tol,
                    "n": bougie.tick_count == r["n"]}
        pareils = pareille(b)
        sous_ensemble = r["h"] <= b.high + tol and r["l"] >= b.low - tol
        sur_ensemble = b.high <= r["h"] + tol and b.low >= r["l"] - tol
        coupee = (r["premier"] - m * 1000 > BORD_SEC * 1000
                  or (m + 60) * 1000 - r["dernier"] > BORD_SEC * 1000)
        voisine = [v for v in (bougies_m.get(m - 60), bougies_m.get(m + 60))
                   if v is not None and all(pareille(v).values())]
        # L'égalité EXACTE avec une bougie voisine passe avant les
        # explications par inclusion, plus faibles : deux minutes voisines
        # ont souvent des fourchettes qui se recouvrent.
        if all(pareils.values()):
            classe = IDENTIQUE
        elif not b.complete:
            classe = "bougie incomplète"
        elif voisine:
            classe = DECALAGE
            self._anomalie(
                "FAIL", "décalage de minute", paire, m, m + 60,
                f"chemin égal à la bougie de {_horodatage(voisine[0].ts_sec)}",
                r["n"])
        elif coupee and sous_ensemble:
            classe = "minute coupée"
        elif r["n"] < b.tick_count and sous_ensemble:
            classe = "chemin moins fourni"
        elif r["n"] > b.tick_count and sur_ensemble:
            classe = "chemin plus fourni"
        else:
            classe = INEXPLIQUE
            champs = [k for k, v in pareils.items() if not v]
            for k in champs:
                self.ecarts_concordance[k] += 1
            self._anomalie(
                "FAIL", "concordance M1 inexpliquée", paire, m, m + 60,
                f"bougie O {b.open} H {b.high} L {b.low} C {b.close} "
                f"n {b.tick_count} ; chemin O {r['o']} H {r['h']} "
                f"L {r['l']} C {r['c']} n {r['n']}", r["n"])
        self.concordance[classe] += 1
        exemples = self.exemples.setdefault(classe, [])
        if classe != IDENTIQUE and len(exemples) < 5:
            exemples.append([paire, m])
        for c in self._compteurs(paire, m):
            c.concordance[classe] += 1
        return classe

    def _trous_et_debit(self, paire: str, resumes: Mapping[int, dict],
                        bougies_m: Mapping[int, Candle]) -> None:
        ecoutees = self._minutes_ecoutees()
        absentes: list[int] = []

        def clore() -> None:
            if len(absentes) >= INTERRUPTION_LONGUE_MIN:
                ecoute = (sum(1 for x in absentes if x in ecoutees)
                          if ecoutees is not None else 0)
                for c in self._compteurs(paire, absentes[0]):
                    c.interruptions += 1
                    c.interruptions_min += len(absentes)
                self._anomalie(
                    "WARNING" if ecoute * 2 >= len(absentes) else "INFO",
                    "interruption longue", paire, absentes[0],
                    absentes[-1] + 60,
                    f"{len(absentes)} min sans ticks, collecteur à l'écoute "
                    f"{ecoute} min", len(absentes))
            absentes.clear()

        for m in range(self.debut, self.fin, 60):
            if m in resumes:
                clore()
                continue
            absentes.append(m)
            for c in self._compteurs(paire, m):
                if m in bougies_m:
                    c.bougie_sans_chemin += 1
                if ecoutees is not None and m in ecoutees:
                    c.manquantes_ecoutees += 1
                else:
                    c.manquantes_muettes += 1
        clore()
        if not resumes:
            self._anomalie(
                "WARNING", "paire sans aucun tick dans la fenêtre", paire,
                self.debut, self.fin,
                "non souscrite par le collecteur, ou stockage des ticks coupé",
                (self.fin - self.debut) // 60)
        # Des ticks là où le collecteur ne battait pas : horodatage décalé ?
        if ecoutees is not None:
            hors = [m for m in sorted(resumes) if m not in ecoutees]
            self.sans_battement += len(hors)
            debut_serie = None
            for i, m in enumerate(hors):
                if debut_serie is None:
                    debut_serie = m
                if i + 1 == len(hors) or hors[i + 1] != m + 60:
                    duree = (m + 60 - debut_serie) // 60
                    self._anomalie(
                        "WARNING", "ticks sans battement du collecteur",
                        paire, debut_serie, m + 60,
                        f"{duree} min : horodatage décalé (changement "
                        f"d'heure ?) ou battements perdus", duree)
                    debut_serie = None
        # Le débit, jugé contre la paire elle-même.
        par_jour: dict[int, list[int]] = {}
        for m, r in resumes.items():
            par_jour.setdefault(_jour(m), []).append(r["n"])
        mediane = _quantile([r["n"] for r in resumes.values()], 0.5)
        if not mediane:
            return
        for jour, valeurs in sorted(par_jour.items()):
            if len(valeurs) < MINUTES_MIN_JOUR:
                continue
            med = _quantile(valeurs, 0.5)
            if not DEBIT_BAS * mediane <= med <= DEBIT_HAUT * mediane:
                self._anomalie(
                    "WARNING", "débit anormal", paire, jour, jour + 86400,
                    f"médiane {med} ticks/min ce jour, {mediane} sur la "
                    f"fenêtre", len(valeurs))

    # --- les signaux --------------------------------------------------------------

    def _signaux(self, paire: str, resumes: Mapping[int, dict],
                 illisibles: set[int], signaux: Sequence[Any],
                 par_minute: Mapping[int, Any],
                 bougies: Mapping[int, Candle]) -> None:
        ecoutees = self._minutes_ecoutees()
        seconde = self._granularite() >= 1000
        for s in signaux:
            d = int(s.ts_sec)
            if not self.debut <= d < self.fin:
                continue                  # hors de la fenêtre auditée
            if not (self.debut + AVANT_SEC <= d <= self.fin - APRES_SEC):
                # Trop près d'un bord : ses 30 minutes débordent.
                self.hors_fenetre += 1
                continue
            minutes = list(range(d - AVANT_SEC, d + APRES_SEC, 60))
            presentes = [m for m in minutes if m in resumes]
            code = self._code(d, minutes, resumes, illisibles, ecoutees,
                              seconde)
            joue = self.joue(s) if self.joue is not None else None
            ecart = max([resumes[m]["ecart_max"] for m in presentes]
                        + [resumes[m]["entree"] for m in presentes[1:]
                           if resumes[m]["entree"] is not None], default=None)
            fiche = {
                "id": f"{paire}|{d}", "paire": paire, "ts": d,
                "debut": minutes[0], "fin": d + APRES_SEC,
                "n_ticks": sum(resumes[m]["n"] for m in presentes),
                "minutes": len(presentes),
                "couverture": round(len(presentes) / len(minutes), 3),
                "ecart_max_ms": ecart,
                "ecoutees": (sum(1 for m in minutes if m in ecoutees)
                             if ecoutees is not None else None),
                "code": code, "motif": CODES.get(code, ""),
                "joue": joue}
            self.fiches_signaux.append(fiche)
            complet = code == COMPLETE
            for compte, inclus in ((self.codes, True),
                                   (self.codes_joues, joue)):
                if inclus:
                    compte[code] += 1
            for cle, table in ((_jour(d), self.par_jour_signaux),
                               (d % 86400 // 3600, self.par_heure_signaux)):
                v = table.setdefault(cle, [0, 0])
                v[0] += 1
                v[1] += complet
            c = self.paires[paire]
            c.signaux += 1
            c.signaux_complets += complet
            if complet:
                self._suivre(paire, s, d, par_minute, bougies)

    def _code(self, d: int, minutes: Sequence[int],
              resumes: Mapping[int, dict], illisibles: set[int],
              ecoutees: set[int] | None, seconde: bool) -> str:
        if not any(m in resumes for m in minutes):
            return "DECOMPRESSION_ERROR" if any(
                m in illisibles for m in minutes) else "NO_TICKS"
        if any(m in illisibles for m in minutes):
            return "DECOMPRESSION_ERROR"
        if any(m not in resumes for m in minutes):
            return "INTERNAL_GAP"
        if any(resumes[m]["invalide"] for m in minutes):
            return "INVALID_ORDER"
        if seconde or (ecoutees is not None
                       and any(m not in ecoutees for m in minutes)):
            return "TIMESTAMP_UNCERTAIN"
        if resumes[minutes[0]]["premier"] - minutes[0] * 1000 \
                > BORD_SEC * 1000:
            return "START_BOUNDARY_MISSING"
        if (d + APRES_SEC) * 1000 - resumes[minutes[-1]]["dernier"] \
                > BORD_SEC * 1000:
            return "END_BOUNDARY_MISSING"
        for k, m in enumerate(minutes):
            r = resumes[m]
            if max(r["ecart_max"], r["entree"] or 0 if k else 0) \
                    >= GEL_SEC * 1000:
                return "FEED_FREEZE"
        if any(resumes[m]["n"] < TICKS_MIN_MINUTE for m in minutes):
            return "INSUFFICIENT_COVERAGE"
        if any(resumes[m]["concordance"] in (INEXPLIQUE, DECALAGE)
               for m in minutes):
            return "M1_MISMATCH"
        if d * 1000 - resumes[d - 60]["dernier"] > BORD_SEC * 1000:
            return "NO_TICK_AT_DECISION"
        return COMPLETE

    # --- phase F : trajectoires et témoins ------------------------------------

    def _ticks_de(self, par_minute: Mapping[int, Any], debut: int,
                  fin: int) -> list[tuple[int, float]]:
        sortie: list[tuple[int, float]] = []
        for m in range(debut, fin, 60):
            if m in par_minute:
                sortie.extend((t.ts_ms, t.price)
                              for t in self.decoder(par_minute[m]))
        return sortie

    @staticmethod
    def _amplitude(bougies: Mapping[int, Candle], d: int) -> float | None:
        """Moyenne de (haut − bas) des 60 bougies jusqu'à celle du signal
        (convention de `historique._excursions`)."""
        recul = [bougies[m] for m in range(d - 3600, d, 60) if m in bougies]
        if not recul:
            return None
        return sum(b.high - b.low for b in recul) / len(recul) or None

    def _suivre(self, paire: str, s: Any, d: int,
                par_minute: Mapping[int, Any],
                bougies: Mapping[int, Candle]) -> None:
        ctx = getattr(s, "contexte", {}) or {}
        if "sens_call" not in ctx:
            return
        ticks = self._ticks_de(par_minute, d - 60, d + APRES_SEC)
        avant = [p for t, p in ticks if t < d * 1000]
        amplitude = self._amplitude(bougies, d)
        if not avant or not amplitude:
            return
        fiche = microstructure.trajectoire(
            ticks, d * 1000, avant[-1], bool(ctx["sens_call"]), amplitude,
            ctx.get("niveau"))
        issues = getattr(s, "issues", {}) or {}
        fiche.update({"id": f"{paire}|{d}", "paire": paire, "jour": _jour(d),
                      "entree": avant[-1],
                      "entree_bougie": ctx.get("prix_signal"),
                      "issues_bougies": {str(k): v for k, v in issues.items()
                                         if k in microstructure.ECHEANCES}})
        self.trajectoires.append(fiche)

    def _temoins(self, paire: str, resumes: Mapping[int, dict],
                 par_minute: Mapping[int, Any]) -> None:
        """Des pseudo-contrats CALL, entrés toutes les `PAS_TEMOIN_MIN`
        minutes sans aucun signal, suivis comme les signaux — le groupe de
        comparaison de la fin d'échéance."""
        for m in range(self.debut + 60, self.fin - APRES_SEC, 60):
            if (m // 60) % PAS_TEMOIN_MIN:
                continue
            fenetre = range(m - 60, m + APRES_SEC, 60)
            if any(x not in resumes for x in fenetre) or any(
                    resumes[x]["ecart_max"] >= GEL_SEC * 1000
                    for x in fenetre):
                continue
            ticks = self._ticks_de(par_minute, m - 60, m + APRES_SEC)
            avant = [p for t, p in ticks if t < m * 1000]
            if not avant:
                continue
            fiche = microstructure.trajectoire(ticks, m * 1000, avant[-1],
                                               True, 1.0)
            fiche.update({"paire": paire, "jour": _jour(m)})
            self.temoins.append(fiche)

    # --- les ordres réels -------------------------------------------------------

    def comparer_ordres(self, ordres: Sequence[Mapping[str, Any]],
                        ticks_autour: Callable[[str, int, int],
                                               Sequence[Tick]]) -> None:
        """`ordres` : les lignes de `REQUETES["ordres"]`, en dictionnaires ;
        `ticks_autour(paire, debut_sec, fin_sec)` : nos ticks (lecture)."""
        heures = [round(o["decalage_broker_ms"] / HEURE_MS) for o in ordres
                  if o.get("decalage_broker_ms") is not None]
        k_mode = Counter(heures).most_common(1)[0][0] if heures else None
        for i, o in enumerate(ordres, 1):
            rapatrie = "rapatrie_du_broker" in str(o.get("brut") or "")
            decalage = o.get("decalage_broker_ms")
            if decalage is not None:
                k = round(decalage / HEURE_MS)
                reste = decalage - k * HEURE_MS
            else:
                k, reste = k_mode, None
            fiche: dict[str, Any] = {
                "id": f"O{i:04d}", "paire": o["pair"], "sens": o.get("sens"),
                "signal_ms": o.get("signal_ts_ms"),
                "ouverture_ms": o.get("ouverture_ts_ms"),
                "rapatrie": rapatrie, "reste_ms": reste,
                "seconde_ronde": (o.get("ouverture_ts_ms") or 1) % 1000 == 0,
                "resultat": o.get("resultat"), "motif": None}
            self.fiches_ordres.append(fiche)
            if not o.get("accepte"):
                fiche["motif"] = "refusé par le broker"
                continue
            if o.get("prix_entree") is None or o.get("ouverture_ts_ms") is None:
                fiche["motif"] = "prix ou instant d'ouverture absent"
                continue
            if k is None:
                fiche["motif"] = "décalage d'horloge inconnu"
                continue
            for cle, instant, prix in (
                    ("ouverture", o["ouverture_ts_ms"], o.get("prix_entree")),
                    ("cloture", o.get("expiration_ts_ms"),
                     o.get("prix_sortie"))):
                if instant is None or not prix:
                    continue
                fiche[cle] = self._retrouver(
                    o["pair"], int(instant) - k * HEURE_MS, float(prix),
                    ticks_autour)
            self._ecarts_d_execution(o, fiche, rapatrie, reste, k,
                                     ticks_autour)
            if not (fiche.get("ouverture") or {}).get("ticks"):
                fiche["motif"] = "aucun tick autour de l'ouverture"
            for cle in ("ouverture", "cloture"):
                v = fiche.get(cle)
                if v and v["ticks"] and not v["trouve"][
                        FENETRES_PRIX_MS.index(FENETRE_PRIX_JUGEE_MS)]:
                    self._anomalie(
                        "WARNING", f"prix {LIBELLES_PRIX[cle]} absent du "
                                   f"flux à ± 1 s",
                        o["pair"], v["instant_ms"] / 1000, None,
                        f"broker {v['prix']} ; dernier tick "
                        f"{v['dernier']} ({v['ecart_points']} point(s))")

    def _ecarts_d_execution(self, o, fiche, rapatrie, reste, k,
                            ticks_autour) -> None:
        """Les écarts SIGNÉS : prix obtenu − prix de référence, en points ;
        coût pour le pari = l'écart pour un CALL, son opposé pour un PUT
        (positif = défavorable). Et les délais signal → clic → acceptation
        → ouverture."""
        entree = float(o["prix_entree"])
        call = (o.get("sens") or "call") == "call"
        ouverture_locale = (o["ouverture_ts_ms"] - o["decalage_broker_ms"]
                            if o.get("decalage_broker_ms") is not None
                            else None)
        refs: dict[str, float | None] = {}
        if not rapatrie and reste is not None:
            for nom, cle in (("signal", "signal_ts_ms"), ("clic", "clic_ts_ms"),
                             ("acceptation", "accepte_ts_ms")):
                if o.get(cle) is not None:
                    flux = self._prix_flux(o["pair"], int(o[cle]) + reste,
                                           ticks_autour)
                    refs[nom] = flux["prix"] if flux else None
                    fiche[f"age_{nom}_ms"] = flux["age_ms"] if flux else None
            if o.get("prix_attendu"):
                refs["attendu"] = float(o["prix_attendu"])
        flux = self._prix_flux(o["pair"], int(o["ouverture_ts_ms"])
                               - k * HEURE_MS, ticks_autour)
        refs["flux_ouverture"] = flux["prix"] if flux else None
        prix_connus = [entree] + [v for v in refs.values() if v]
        pas = 10 ** -max(_decimales(p) for p in prix_connus)
        ecarts = {}
        for nom, ref in refs.items():
            if ref is None:
                continue
            brut = round((entree - ref) / pas)
            ecarts[nom] = {"ecart": brut, "cout": brut if call else -brut}
        if refs.get("attendu") is not None and refs.get("signal") is not None:
            brut = round((refs["attendu"] - refs["signal"]) / pas)
            ecarts["attendu_moins_flux"] = {"ecart": brut,
                                            "cout": brut if call else -brut}
        fiche["ecarts"] = ecarts
        fiche["delais_ms"] = {
            "signal_clic": (o["clic_ts_ms"] - o["signal_ts_ms"]
                            if not rapatrie and o.get("clic_ts_ms") is not None
                            else None),
            "clic_acceptation": (o["accepte_ts_ms"] - o["clic_ts_ms"]
                                 if not rapatrie and o.get("accepte_ts_ms")
                                 is not None and o.get("clic_ts_ms")
                                 is not None else None),
            "acceptation_ouverture": (
                ouverture_locale - o["accepte_ts_ms"]
                if not rapatrie and ouverture_locale is not None
                and o.get("accepte_ts_ms") is not None else None),
            "signal_ouverture": (
                ouverture_locale - o["signal_ts_ms"]
                if not rapatrie and ouverture_locale is not None else None)}

    @staticmethod
    def _prix_flux(paire: str, instant_ms: int, ticks_autour) -> dict | None:
        """Le dernier tick de NOTRE flux à `instant_ms`, et son âge."""
        ticks = [t for t in ticks_autour(paire, (instant_ms - 10_000) // 1000,
                                         instant_ms // 1000 + 1)
                 if t.ts_ms <= instant_ms]
        if not ticks:
            return None
        return {"prix": ticks[-1].price, "age_ms": instant_ms - ticks[-1].ts_ms}

    @staticmethod
    def _retrouver(paire: str, instant_ms: int, prix: float,
                   ticks_autour) -> dict:
        marge = max(FENETRES_PRIX_MS) + 1000
        ticks = [t for t in ticks_autour(
            paire, (instant_ms - marge) // 1000,
            (instant_ms + marge) // 1000 + 1)
            if abs(t.ts_ms - instant_ms) <= marge]
        sortie = {"instant_ms": instant_ms, "prix": prix, "ticks": len(ticks),
                  "trouve": [False] * len(FENETRES_PRIX_MS),
                  "dernier": None, "ecart_points": None,
                  "proche_ms": None}
        if not ticks:
            return sortie
        avant = [t for t in ticks if t.ts_ms <= instant_ms]
        dernier = (avant[-1] if avant else ticks[0]).price
        pas = 10 ** -max(_decimales(prix), max(_decimales(t.price)
                                               for t in ticks))
        tol = pas / 2
        sortie["dernier"] = dernier
        sortie["ecart_points"] = round(abs(dernier - prix) / pas)
        egaux = [abs(t.ts_ms - instant_ms) for t in ticks
                 if abs(t.price - prix) <= tol]
        sortie["proche_ms"] = min(egaux) if egaux else None
        for i, w in enumerate(FENETRES_PRIX_MS):
            if w == 0:
                sortie["trouve"][i] = abs(dernier - prix) <= tol
            else:
                sortie["trouve"][i] = any(e <= w for e in egaux)
        return sortie

    # --- le résultat ------------------------------------------------------------

    def _granularite(self) -> int:
        n = self.resolution.get("n", 0)
        return next((d for d in (1000, 100, 10)
                     if n and self.resolution.get(d, 0) >= 0.99 * n), 1)

    def anomalies_triees(self) -> list[dict]:
        """Toutes les anomalies gardées, la plus grave d'abord, numérotées."""
        triees = [a for g in GRAVITES
                  for a in sorted(self.anomalies[g],
                                  key=lambda a: (a["debut"], a["paire"]))]
        for i, a in enumerate(triees, 1):
            a["id"] = f"A{i:05d}"
        return triees

    def resultat(self, *, commit: str = "", parametres_extra:
                 Mapping[str, Any] | None = None) -> dict:
        n = self.resolution.get("n", 0)
        g = self.global_
        comparees = sum(self.concordance.values())
        explicables = sum(self.concordance.get(c, 0) for c in EXPLICABLES)
        inexpliques = (self.concordance.get(INEXPLIQUE, 0)
                       + self.concordance.get(DECALAGE, 0))
        ecoutees = self._minutes_ecoutees()
        par_paire_attendues = (self.fin - self.debut) // 60
        # Les (paire, minute) où le collecteur battait : le dénominateur des
        # trous qui comptent.
        attendues_ecoutees = (
            sum(1 for m in range(self.debut, self.fin, 60) if m in ecoutees)
            * len(self.paires) if ecoutees is not None else 0)
        granularite = self._granularite()
        # Les minutes attendues de chaque jour et de chaque heure UTC : les
        # jours du bord de la fenêtre ne sont pas entiers.
        par_jour: Counter = Counter()
        par_heure: Counter = Counter()
        for m in range(self.debut, self.fin, 60):
            par_jour[_jour(m)] += 1
            par_heure[m % 86400 // 3600] += 1

        # 1. Horodatages
        horodatages = {
            "ticks": n, "granularite_ms": granularite if n else None,
            "part_seconde_ronde": (self.resolution.get(1000, 0) / n
                                   if n else None),
            "doublons_instant": self.doublons_instant,
            "doublons_exacts": g.doublons_exacts,
            "hors_minute": self.hors_minute,
            "decroissants": self.decroissants,
            "minutes_gelees": self.minutes_gelees,
            "ecarts_ms": {"p25": _quantile_histogramme(self.ecarts, 0.25),
                          "p50": _quantile_histogramme(self.ecarts, 0.5),
                          "p75": _quantile_histogramme(self.ecarts, 0.75),
                          "p90": _quantile_histogramme(self.ecarts, 0.9),
                          "p99": _quantile_histogramme(self.ecarts, 0.99),
                          "max": self.ecart_max_ms},
            "histogramme": {str(k): v for k, v in sorted(self.ecarts.items())},
        }
        if not n:
            statut_h = NON_TESTABLE
        elif granularite >= 1000 or self.hors_minute or self.decroissants:
            statut_h = "FAIL"
        elif (g.doublons_exacts > 0.01 * n
              or self.minutes_gelees > 0.01 * max(1, g.minutes)):
            statut_h = "WARNING"
        else:
            statut_h = "PASS"
        horodatages["statut"] = statut_h

        # 2. Compression
        controlees = self.reencodees[0] + self.relectures[0] + self.illisibles
        if not controlees:
            statut_z = NON_TESTABLE
        elif self.illisibles or self.reencodees[1] or self.relectures[2]:
            statut_z = "FAIL"
        else:
            statut_z = "PASS"
        compression = {
            "statut": statut_z, "chemins": self.chemins_lus,
            "illisibles": self.illisibles,
            "reencodees": list(self.reencodees),
            "echantillon_reencodage": ECHANTILLON_REENCODAGE,
            "relectures": list(self.relectures),
            "fenetre_relecture_min": LECTURE_MIN}

        # 3. Concordance
        if not comparees:
            statut_c = NON_TESTABLE
        elif inexpliques > 0.001 * comparees:
            statut_c = "FAIL"
        elif (inexpliques or g.chemin_sans_bougie
              or explicables > 0.02 * comparees):
            statut_c = "WARNING"
        else:
            statut_c = "PASS"
        concordance = {
            "statut": statut_c, "comparees": comparees,
            "classes": dict(self.concordance),
            "categories": dict(CATEGORIES),
            "exemples": {k: v for k, v in self.exemples.items()},
            "champs_inexpliques": dict(self.ecarts_concordance),
            "chemin_sans_bougie": g.chemin_sans_bougie,
            "bougie_sans_chemin": g.bougie_sans_chemin}

        # 4. Trous. Une paire sans AUCUN tick n'est pas trouée : elle n'est
        # pas collectée. Elle est signalée, et sortie du statut.
        total_minutes = par_paire_attendues * max(1, len(self.paires))
        sans_ticks = sorted(p for p, c in self.paires.items() if not c.minutes)
        if ecoutees is None:
            statut_t = NON_TESTABLE
            part = None
        else:
            gardees = len(self.paires) - len(sans_ticks)
            manquantes = g.manquantes_ecoutees - sum(
                self.paires[p].manquantes_ecoutees for p in sans_ticks)
            ecoutees_par_paire = (attendues_ecoutees // len(self.paires)
                                  if self.paires else 0)
            denominateur = ecoutees_par_paire * gardees
            part = manquantes / denominateur if denominateur else None
            statut_t = (NON_TESTABLE if part is None else
                        "FAIL" if part > 0.05 else
                        "WARNING" if part > 0.01 else "PASS")
        interruptions = sorted(
            (a for a in self.anomalies["WARNING"] + self.anomalies["INFO"]
             if a["nature"] == "interruption longue"),
            key=lambda a: -a["duree_sec"])[:3]
        trous = {
            "statut": statut_t, "minutes_attendues": total_minutes,
            "definition": "toutes les minutes de [début ; fin[ pour chaque "
                          "paire auditée (marché OTC ouvert 24 h/24)",
            "minutes_avec_ticks": g.minutes,
            "manquantes_ecoutees": g.manquantes_ecoutees,
            "manquantes_muettes": g.manquantes_muettes,
            "part_manquante_ecoutee": part,
            "paires_sans_ticks": sans_ticks,
            "minutes_sans_battement": self.sans_battement,
            "minutes_pauvres": g.pauvres,
            "battements": len(self.battements),
            "interruptions_longues": g.interruptions,
            "interruptions_min": g.interruptions_min,
            "principales_interruptions": [
                [a["paire"], a["debut"], a["fin"], a["mesure"]]
                for a in interruptions],
            "debits_anormaux": sum(
                v for (gr, nat), v in self.comptes_anomalies.items()
                if nat == "débit anormal"),
            "ticks_par_minute": _quantiles(g.par_minute)}

        # 5. Signaux
        total = len(self.fiches_signaux)
        complets = self.codes.get(COMPLETE, 0)
        statut_s = (NON_TESTABLE if not total else
                    "FAIL" if complets < 0.5 * total else
                    "WARNING" if complets < 0.8 * total else "PASS")
        joues = sum(self.codes_joues.values())
        signaux = {"statut": statut_s, "signaux": [total, complets],
                   "joues": [joues, self.codes_joues.get(COMPLETE, 0)],
                   "codes": {c: self.codes.get(c, 0)
                             for c in (*CODES, COMPLETE)},
                   "codes_joues": {c: self.codes_joues.get(c, 0)
                                   for c in (*CODES, COMPLETE)},
                   "hors_fenetre": self.hors_fenetre,
                   "par_jour": {str(j): v for j, v in
                                sorted(self.par_jour_signaux.items())},
                   "par_heure": {str(h): v for h, v in
                                 sorted(self.par_heure_signaux.items())},
                   "exclus": [f for f in self.fiches_signaux
                              if f["code"] != COMPLETE][:5]}

        # 6. Prix
        prix = self._resume_prix()

        controles = {"horodatages": horodatages, "compression": compression,
                     "concordance": concordance, "trous": trous,
                     "signaux": signaux, "prix": prix,
                     "ticks_tardifs": {"statut": NON_TESTABLE},
                     "heure_reception": {"statut": NON_TESTABLE}}
        anomalies = self.anomalies_triees()
        exploration = microstructure.resumer(self.trajectoires, self.temoins)
        return {
            "debut": self.debut, "fin": self.fin,
            "calcule_le": _horodatage(datetime.now(timezone.utc).timestamp()),
            "paires": {p: c.resume(par_paire_attendues)
                       for p, c in sorted(self.paires.items())},
            "jours": {str(j): c.resume(par_jour.get(j, 0) * len(self.paires))
                      for j, c in sorted(self.jours.items())},
            "heures": {str(h): c.resume(par_heure.get(h, 0)
                                        * len(self.paires))
                       for h, c in self.heures.items()},
            "controles": controles,
            "statuts": {k: v["statut"] for k, v in controles.items()},
            "anomalies": {
                "comptes": [[gr, nat, NATURES[nat][0], v]
                            for (gr, nat), v in sorted(
                                self.comptes_anomalies.items(),
                                key=lambda kv: (GRAVITES.index(kv[0][0]),
                                                -kv[1]))],
                "premieres": anomalies[:20]},
            "exploration": exploration,
            "inventaire": self._inventaire(controles, exploration),
            "limites": self._limites(controles),
            "reproductibilite": {
                "commit": commit, "chemins_lus": self.chemins_lus,
                "empreinte": self._empreinte.hexdigest()[:16],
                "requetes": dict(REQUETES), "seuils": dict(SEUILS),
                "justifications": dict(JUSTIFICATIONS),
                "parametres": {
                    "gel_sec": GEL_SEC, "bord_sec": BORD_SEC,
                    "ticks_min_minute": TICKS_MIN_MINUTE,
                    "fenetre_signal_sec": [AVANT_SEC, APRES_SEC],
                    "interruption_longue_min": INTERRUPTION_LONGUE_MIN,
                    "debit": [DEBIT_BAS, DEBIT_HAUT],
                    "battement_sec": [BATTEMENT_AVANT_SEC,
                                      BATTEMENT_APRES_SEC],
                    "fenetres_prix_ms": list(FENETRES_PRIX_MS),
                    "echantillon_reencodage": ECHANTILLON_REENCODAGE,
                    "fenetre_relecture_min": LECTURE_MIN,
                    "pas_temoin_min": PAS_TEMOIN_MIN,
                    "fins_echeance_sec": list(microstructure.FINS_SEC),
                    **dict(parametres_extra or {})}}}

    def _resume_prix(self) -> dict:
        i = FENETRES_PRIX_MS.index(FENETRE_PRIX_JUGEE_MS)
        fiches = self.fiches_ordres
        motifs = Counter(f["motif"] for f in fiches if f["motif"])
        horodates = [f for f in fiches if f.get("ouverture_ms")]
        sortie: dict = {"ordres": len(fiches),
                        "rapatries": sum(f["rapatrie"] for f in fiches),
                        "exclus": dict(motifs),
                        "horodates": len(horodates),
                        "seconde_ronde": sum(f["seconde_ronde"]
                                             for f in horodates)}
        for cle in ("ouverture", "cloture"):
            vus = [f[cle] for f in fiches if f.get(cle)]
            comparables = [v for v in vus if v["ticks"]]
            sortie[cle] = {
                "ordres": len(vus), "comparables": len(comparables),
                "trouves": [sum(v["trouve"][j] for v in comparables)
                            for j in range(len(FENETRES_PRIX_MS))],
                "ecart_points_median": _quantile(
                    [v["ecart_points"] for v in comparables], 0.5),
                "proche_ms": _quantiles([v["proche_ms"] for v in comparables
                                         if v["proche_ms"] is not None],
                                        (0.5, 0.9))}
        restes = [f["reste_ms"] for f in fiches
                  if f.get("reste_ms") is not None]
        sortie["synchronisation_ms"] = _quantiles(restes, (0.1, 0.5, 0.9))
        # Les écarts signés et les délais (descriptifs).
        ecarts: dict = {}
        noms = ("signal", "clic", "acceptation", "flux_ouverture", "attendu",
                "attendu_moins_flux")
        for nom in noms:
            vals = [(f, f["ecarts"][nom]) for f in fiches
                    if nom in (f.get("ecarts") or {})]
            couts = [e["cout"] for _f, e in vals]
            if not couts:
                continue
            ecarts[nom] = {
                "n": len(couts),
                "cout": _quantiles(couts),
                "defavorables": sum(1 for c in couts if c > 0),
                "nuls": sum(1 for c in couts if c == 0),
                "favorables": sum(1 for c in couts if c < 0),
                "call": _quantile([e["cout"] for f, e in vals
                                   if f["sens"] == "call"], 0.5),
                "put": _quantile([e["cout"] for f, e in vals
                                  if f["sens"] == "put"], 0.5),
                "par_paire": {p: [len(v), _quantile(v, 0.5)]
                              for p, v in _grouper(
                                  vals, lambda f: f["paire"]).items()},
                "par_latence": {t: [len(v), _quantile(v, 0.5)]
                                for t, v in _grouper(
                                    vals, _tranche).items()}}
        sortie["ecarts"] = ecarts
        sortie["delais_ms"] = {
            nom: _quantiles([f["delais_ms"][nom] for f in fiches
                             if (f.get("delais_ms") or {}).get(nom)
                             is not None], (0.1, 0.5, 0.9))
            for nom in ("signal_clic", "clic_acceptation",
                        "acceptation_ouverture", "signal_ouverture")}
        comparables = min(sortie["ouverture"]["comparables"],
                          sortie["cloture"]["comparables"])
        sortie["niveau"] = ("analyse" if comparables >= ORDRES_ANALYSE
                            else "descriptif" if comparables else "aucun")
        jugees = [sortie["ouverture"], sortie["cloture"]]
        if comparables < ORDRES_MIN:
            statut = NON_TESTABLE
        else:
            part = min(j["trouves"][i] / j["comparables"] for j in jugees)
            statut = ("FAIL" if part < 0.7 else
                      "WARNING" if part < 0.9 else "PASS")
        sortie["statut"] = statut
        return sortie

    def _inventaire(self, c: Mapping[str, Mapping],
                    exploration: Mapping) -> dict[str, list[str]]:
        """Les analyses faisables, sous réserve, non fiables, impossibles —
        déduites des statuts, jamais d'une préférence."""
        inv: dict[str, list[str]] = {"faisables": [], "sous_reserve": [],
                                     "non_fiables": [], "impossibles": []}

        def classer(nom: str, statuts: Sequence[str], raison: str) -> None:
            if any(s == "FAIL" for s in statuts):
                inv["non_fiables"].append(f"{nom} — {raison}")
            elif any(s == NON_TESTABLE for s in statuts):
                inv["non_fiables"].append(
                    f"{nom} — un contrôle préalable n'a pas pu être testé")
            elif any(s == "WARNING" for s in statuts):
                inv["sous_reserve"].append(f"{nom} — {raison}")
            else:
                inv["faisables"].append(nom)

        h, z, k, t, s = (c["horodatages"]["statut"],
                         c["compression"]["statut"],
                         c["concordance"]["statut"], c["trous"]["statut"],
                         c["signaux"]["statut"])
        classer("Mesures sous la seconde autour du contact (vitesse "
                "d'approche, réaction, persistance)", (h, z, s),
                "horodatages, compression ou couverture des signaux")
        classer("Journal événementiel avant chaque signal (étape 1)",
                (h, z, k, s), "qualité ou couverture des fenêtres")
        classer("Trajectoires au tick, MFE/MAE, franchissements de zone",
                (z, k, s), "compression, concordance ou couverture")
        classer("Couverture et trous par paire, jour et heure", (t,),
                "battements du collecteur")
        p = c["prix"]
        if p["statut"] in ("FAIL",):
            inv["non_fiables"].append(
                "Analyse d'exécution (écarts entre flux et broker) — les "
                "prix du broker ne se retrouvent pas dans le flux")
        elif p["niveau"] == "analyse" and p["statut"] == "PASS":
            inv["faisables"].append("Analyse d'exécution (écarts signés, "
                                    "délais, par sens et par paire)")
        elif p["niveau"] in ("analyse", "descriptif"):
            inv["sous_reserve"].append(
                f"Analyse d'exécution — DESCRIPTIVE seulement "
                f"({p['ouverture']['comparables']} ordre(s) comparable(s), "
                f"il en faut {ORDRES_ANALYSE})")
        else:
            inv["non_fiables"].append(
                "Analyse d'exécution — aucun ordre comparable")
        inv["impossibles"] += [
            "Latence du flux (heure de réception non stockée)",
            "Ordre d'arrivée des ticks dans une minute (trié à l'écriture)",
            "Nombre de ticks tardifs écartés (aucun compteur)",
            "Prix vu par le broker au clic et à l'acceptation (non "
            "enregistrés ; seul NOTRE flux à ces instants est connu)",
            "Volume, prix d'achat et de vente séparés",
            "Intention du broker : aucune donnée ne permet de l'établir",
        ]
        return inv

    def _limites(self, c: Mapping[str, Mapping]) -> list[str]:
        """Ce qui empêche certaines analyses ultérieures : le constat du
        code, puis ce que les données de cette fenêtre y ajoutent."""
        limites = [
            "Heure de RÉCEPTION non conservée : seule l'heure du serveur du "
            "broker est stockée (ramenée en UTC par un décalage arrondi à "
            "l'heure) ; la latence du flux ne se mesure pas sur l'historique.",
            "Ordre d'ARRIVÉE perdu : les ticks d'une minute sont triés par "
            "horodatage à l'écriture ; un tick arrivé dans le désordre au sein "
            "d'une minute est invisible.",
            "Ticks TARDIFS écartés sans compteur : un tick arrivé après la "
            "fermeture de sa minute est ignoré par l'agrégateur et compté "
            "nulle part ; leur nombre est impossible à établir.",
            "Un seul prix par tick : ni achat/vente séparés, ni volume.",
            "Flux REÇU, pas flux interne du broker : on ne voit que ce que le "
            "websocket envoie ; un échantillonnage côté broker serait "
            "invisible.",
            "Concordance M1 ≠ exactitude : bougies et chemins viennent des "
            "mêmes ticks ; leur accord prouve la cohérence du stockage. Le "
            "seul contrôle EXTERNE est la comparaison aux prix des ordres "
            "réels.",
            "Signaux recalculés, jamais stockés : leur identifiant est "
            "paire|instant de décision ; une stratégie ou des paramètres "
            "différents donneraient d'autres signaux.",
            "Paires non collectées (lues au broker par la course) : aucun "
            "tick.",
        ]
        h, t, p = c["horodatages"], c["trous"], c["prix"]
        if h.get("granularite_ms") and h["granularite_ms"] >= 1000:
            limites.append("Horodatages à la SECONDE : aucune mesure de "
                           "vitesse sous la seconde n'est défendable.")
        if t["minutes_attendues"]:
            part = 1 - t["minutes_avec_ticks"] / t["minutes_attendues"]
            limites.append(
                f"{part:.1%} des minutes de la fenêtre auditée sont sans "
                f"ticks (collecteur arrêté, paire non souscrite ou stockage "
                f"des ticks coupé) : une absence de données n'est pas une "
                f"absence de mouvement.")
        if t["paires_sans_ticks"]:
            limites.append(
                "Paire(s) sans aucun tick dans la fenêtre, sorties du "
                "contrôle des trous : " + ", ".join(
                    x.replace("_otc", "") for x in t["paires_sans_ticks"])
                + ".")
        if t["statut"] == NON_TESTABLE:
            limites.append("Aucun battement du collecteur dans la fenêtre : "
                           "un trou de collecte ne se distingue pas d'une "
                           "paire muette.")
        if p["horodates"] and p["seconde_ronde"] >= 0.9 * p["horodates"]:
            limites.append(
                "Horodatages d'ouverture du broker à la seconde entière : "
                "l'instant d'exécution n'est connu qu'à ± 1 s.")
        comparables = p["ouverture"]["comparables"]
        if comparables < ORDRES_ANALYSE:
            limites.append(
                f"{comparables} ordre(s) réel(s) comparable(s) seulement : "
                f"l'analyse d'exécution reste descriptive.")
        return limites


def _tranche(fiche: Mapping) -> str:
    latence = (fiche.get("delais_ms") or {}).get("signal_ouverture")
    if latence is None:
        return "inconnue"
    for borne in TRANCHES_LATENCE_MS:
        if latence <= borne:
            return f"≤ {borne // 1000} s"
    return f"> {TRANCHES_LATENCE_MS[-1] // 1000} s"


def _grouper(vals, cle) -> dict[str, list]:
    groupes: dict[str, list] = {}
    for f, e in vals:
        groupes.setdefault(cle(f), []).append(e["cout"])
    return groupes


def csv_anomalies(anomalies: Sequence[Mapping[str, Any]]) -> bytes:
    """Le registre complet, séparateur « ; », horodatages UTC."""
    colonnes = ("id", "gravite", "controle", "nature", "paire", "debut_utc",
                "fin_utc", "duree_sec", "mesure", "n", "critere", "impact",
                "confiance", "incertitude")
    lignes = [";".join(colonnes)]
    for a in anomalies:
        valeurs = {**a, "debut_utc": _horodatage(a["debut"]),
                   "fin_utc": _horodatage(a["fin"])}
        lignes.append(";".join(
            str(valeurs.get(c, "")).replace(";", ",").replace("\n", " ")
            for c in colonnes))
    return ("\n".join(lignes) + "\n").encode("utf-8")


def csv_signaux(fiches: Sequence[Mapping[str, Any]]) -> bytes:
    """Une ligne par signal admissible : couverture et motif d'exclusion.
    L'issue du contrat n'y figure pas : elle n'entre pas dans le statut."""
    colonnes = ("id", "paire", "decision_utc", "debut_utc", "fin_utc",
                "n_ticks", "minutes", "couverture", "ecart_max_ms",
                "minutes_ecoutees", "code", "motif", "joue")
    lignes = [";".join(colonnes)]
    for f in fiches:
        lignes.append(";".join(str(x) for x in (
            f["id"], f["paire"], _horodatage(f["ts"]),
            _horodatage(f["debut"]), _horodatage(f["fin"]), f["n_ticks"],
            f["minutes"], f["couverture"],
            "" if f["ecart_max_ms"] is None else f["ecart_max_ms"],
            "" if f["ecoutees"] is None else f["ecoutees"], f["code"],
            f["motif"], "" if f["joue"] is None else int(f["joue"]))))
    return ("\n".join(lignes) + "\n").encode("utf-8")


def csv_ordres(fiches: Sequence[Mapping[str, Any]]) -> bytes:
    """Une ligne par ordre : exclusion, prix retrouvés, écarts signés
    (prix obtenu − référence, en points ; coût positif = défavorable),
    délais."""
    colonnes = ("id", "paire", "sens", "ouverture_broker_utc", "motif",
                "rapatrie", "reste_horloge_ms", "ouverture_retrouvee_1s",
                "cloture_retrouvee_1s", "cout_vs_flux_signal",
                "cout_vs_flux_acceptation", "cout_vs_flux_ouverture",
                "cout_vs_attendu", "signal_clic_ms", "clic_acceptation_ms",
                "acceptation_ouverture_ms", "resultat")
    j = FENETRES_PRIX_MS.index(FENETRE_PRIX_JUGEE_MS)
    lignes = [";".join(colonnes)]
    for f in fiches:
        e = f.get("ecarts") or {}
        d = f.get("delais_ms") or {}

        def cout(nom):
            return "" if nom not in e else e[nom]["cout"]

        def trouve(cle):
            v = f.get(cle)
            return "" if not v or not v["ticks"] else int(v["trouve"][j])
        lignes.append(";".join(str(x) for x in (
            f["id"], f["paire"], f["sens"],
            _horodatage(f["ouverture_ms"] / 1000)
            if f.get("ouverture_ms") else "",
            f["motif"] or "", int(f["rapatrie"]),
            "" if f["reste_ms"] is None else f["reste_ms"],
            trouve("ouverture"), trouve("cloture"), cout("signal"),
            cout("acceptation"), cout("flux_ouverture"), cout("attendu"),
            "" if d.get("signal_clic") is None else d["signal_clic"],
            "" if d.get("clic_acceptation") is None
            else d["clic_acceptation"],
            "" if d.get("acceptation_ouverture") is None
            else d["acceptation_ouverture"], f.get("resultat") or "")))
    return ("\n".join(lignes) + "\n").encode("utf-8")
