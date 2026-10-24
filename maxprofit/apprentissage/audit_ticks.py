"""
Étape 0 — la qualité des ticks, en LECTURE SEULE (cahier des charges du
2026-10-24).

--- La question --------------------------------------------------------------

Avant de chercher dans les ticks ce qui précède un signal gagnant ou perdant,
établir ce qu'ils valent : horodatages, cohérence avec les bougies M1, trous,
couverture autour des signaux ZoneH1, et accord avec les prix que le broker a
réellement appliqués. Ordre de travail : auditer, établir la fiabilité, puis
seulement étudier la valeur prédictive. La fenêtre T0/T2/T3 reste gelée :
rien d'ici ne la modifie.

--- Ce que dit le CODE (lu le 24/10, pas supposé) -----------------------------

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
- `executions` (ordres réels) : `ouverture_ts_ms` et `expiration_ts_ms` sont
  dans l'horloge du broker ; `decalage_broker_ms` est l'avance de cette
  horloge sur la nôtre, mesurée à l'ordre ; `prix_attendu` est le dernier
  prix vu par la COURSE (sa propre connexion), pas par le collecteur.

--- Les contrôles et leurs seuils, fixés AVANT la première mesure --------------

Voir `SEUILS` ; chaque contrôle rend PASS, WARNING, FAIL ou INDÉTERMINÉ.

1. Horodatages : résolution réelle, doublons, ordre, position dans la
   minute, écarts entre ticks consécutifs (« gel » au-delà de `GEL_SEC`, dans
   des minutes contiguës seulement).
2. Concordance M1 : ouverture, haut, bas, clôture et nombre de ticks
   recalculés depuis chaque chemin et comparés à la bougie, écarts classés en
   explicables (règles d'écriture ci-dessus) et inexpliqués. ⚠ Bougie et
   chemin viennent des MÊMES ticks : leur accord prouve la cohérence du
   stockage, pas l'exactitude du flux du broker.
3. Trous : minutes sans chemin, en distinguant celles où le collecteur
   battait (table `uptime`) ; interruptions longues ; débit comparé à la
   distribution observée de la paire elle-même, jamais à une cadence
   théorique.
4. Couverture des signaux : les 30 minutes autour de la décision (15 avant,
   15 après) ne sont « complètes » que si chaque minute est présente, si les
   bornes sont couvertes à `BORD_SEC` près, sans gel, sans minute pauvre, sans
   écart de concordance inexpliqué, et avec un tick dans les `BORD_SEC`
   secondes qui précèdent la décision. Sinon, le PREMIER motif dans cet ordre
   est retenu.
5. Prix d'exécution : le prix d'ouverture et le prix de clôture appliqués par
   le broker sont-ils dans notre flux, à l'instant de l'horloge du broker ?
   Le dernier tick reçu n'est PAS supposé être le prix du clic : on cherche le
   prix dans des fenêtres de 0, 1, 2 et 5 s, et l'on mesure la
   synchronisation (reste de `decalage_broker_ms` une fois les heures
   entières ôtées).

Rien n'est écrit : seules des requêtes SELECT (`REQUETES`), et la course ne
lit rien d'ici. Ce module ne touche pas la base (règle des couches) : `live`
lit les chemins, les battements et les ordres, et lui passe le décodeur de
`store.chemin_ticks`.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

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
#: Ordres comparables en dessous desquels le contrôle des prix est
#: indéterminé.
ORDRES_MIN = 10
#: Bornes (ms) de l'histogramme des écarts entre ticks consécutifs.
BORNES_ECARTS_MS: tuple[int, ...] = (
    10, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000)
#: Anomalies gardées en mémoire, par gravité (les comptes, eux, sont
#: exacts).
ANOMALIES_MAX = {"FAIL": 3000, "WARNING": 3000, "INFO": 2000}
GRAVITES = ("FAIL", "WARNING", "INFO")

SEUILS: dict[str, str] = {
    "horodatages": (
        "FAIL : horodatages à la seconde, tick hors de sa minute, ordre "
        "décroissant ou chemin illisible ; WARNING : doublons exacts au-delà "
        "de 1 % des ticks, ou gels de 10 s ou plus sur plus de 1 % des "
        "minutes ; PASS sinon"),
    "concordance": (
        "FAIL : écarts inexpliqués sur plus de 0,1 % des minutes comparées ; "
        "WARNING : au moins un écart inexpliqué, une minute de ticks sans "
        "bougie, ou plus de 2 % d'écarts explicables ; PASS sinon"),
    "trous": (
        "sur les minutes où le collecteur battait : FAIL au-delà de 5 % sans "
        "ticks, WARNING au-delà de 1 %, PASS sinon ; INDÉTERMINÉ sans "
        "battement"),
    "signaux": (
        "fenêtres de 30 min complètes : FAIL sous 50 %, WARNING sous 80 %, "
        "PASS sinon ; INDÉTERMINÉ sans signal"),
    "prix": (
        "prix d'ouverture ET de clôture du broker retrouvés dans le flux à "
        "± 1 s : FAIL sous 70 %, WARNING sous 90 %, PASS sinon ; "
        "INDÉTERMINÉ sous 10 ordres comparables"),
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
    "ordres": ("SELECT pair, sens, signal_ts_ms, prix_attendu, prix_entree, "
               "prix_sortie, ouverture_ts_ms, expiration_ts_ms, "
               "decalage_broker_ms, brut FROM executions WHERE accepte = 1 "
               "AND prix_entree IS NOT NULL AND ouverture_ts_ms IS NOT NULL "
               "ORDER BY ouverture_ts_ms"),
}

#: Les motifs d'exclusion d'une fenêtre de signal, dans l'ordre où ils sont
#: testés : le premier rencontré est retenu.
MOTIFS: tuple[str, ...] = (
    "minute(s) absente(s)",
    "borne de début non couverte",
    "borne de fin non couverte",
    "gel du flux de 10 s ou plus",
    "minute à moins de 5 ticks",
    "concordance M1 en défaut",
    "pas de tick dans les 5 s avant la décision",
)

#: Classes de concordance d'une minute (chemin face à sa bougie).
IDENTIQUE = "identique"
EXPLICABLES = ("bougie incomplète", "minute coupée",
               "chemin moins fourni", "chemin plus fourni")
INEXPLIQUE = "écart inexpliqué"

HEURE_MS = 3_600_000
LIBELLES_PRIX = {"ouverture": "d'ouverture", "cloture": "de clôture"}


def _jour(ts: int) -> int:
    return ts // 86400 * 86400


def _horodatage(ts_sec: float) -> str:
    return datetime.fromtimestamp(ts_sec, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")


def _quantile(valeurs: Sequence[float], q: float) -> float | None:
    if not valeurs:
        return None
    ordonnees = sorted(valeurs)
    return ordonnees[min(len(ordonnees) - 1, int(q * len(ordonnees)))]


def _quantile_histogramme(histo: Mapping[int, int], q: float) -> int | None:
    """La borne supérieure du casier qui contient le quantile `q`."""
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
        self.gels = 0
        self.doublons_exacts = 0
        self.concordance: Counter = Counter()
        self.manquantes_ecoutees = 0
        self.manquantes_muettes = 0
        self.bougie_sans_chemin = 0
        self.chemin_sans_bougie = 0
        self.signaux = 0
        self.signaux_complets = 0

    def resume(self) -> dict:
        comparees = sum(self.concordance.values())
        return {
            "minutes": self.minutes, "ticks": self.ticks,
            "ticks_min": [_quantile(self.par_minute, 0.1),
                          _quantile(self.par_minute, 0.5),
                          _quantile(self.par_minute, 0.9)],
            "gels": self.gels, "doublons_exacts": self.doublons_exacts,
            "comparees": comparees,
            "identiques": self.concordance.get(IDENTIQUE, 0),
            "inexpliques": self.concordance.get(INEXPLIQUE, 0),
            "manquantes_ecoutees": self.manquantes_ecoutees,
            "manquantes_muettes": self.manquantes_muettes,
            "bougie_sans_chemin": self.bougie_sans_chemin,
            "chemin_sans_bougie": self.chemin_sans_bougie,
            "signaux": [self.signaux, self.signaux_complets]}


class AuditTicks:
    """L'audit de la fenêtre [debut, fin[, paire après paire.

    `decoder` : chemin compressé → ticks (`store.chemin_ticks.decoder`) ;
    `joue` : le prédicat des signaux que la course joue (règle de l'heure),
    pour une couverture à part.
    """

    def __init__(self, debut: int, fin: int, *,
                 decoder: Callable[[Any], Sequence[Tick]],
                 joue: Callable[[Any], bool] | None = None):
        self.decoder = decoder
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
        self.illisibles = 0
        self.minutes_gelees = 0
        #: Minutes de ticks sans battement du collecteur : un horodatage
        #: décalé (changement d'heure du broker) se verrait ici.
        self.sans_battement = 0
        # Concordance
        self.concordance: Counter = Counter()
        self.ecarts_concordance: Counter = Counter()   # champ en défaut
        # Signaux
        self.motifs: Counter = Counter()
        self.motifs_joues: Counter = Counter()
        self.signaux = [0, 0]
        self.signaux_joues = [0, 0]
        self.hors_fenetre = 0
        self.par_jour_signaux: dict[int, list[int]] = {}
        # Prix
        self.ordres: list[dict] = []
        # Anomalies
        self.anomalies: dict[str, list[dict]] = {g: [] for g in GRAVITES}
        self.comptes_anomalies: Counter = Counter()
        self._empreinte = hashlib.sha256()
        self.chemins_lus = 0

    # --- anomalies ------------------------------------------------------------

    def _anomalie(self, gravite: str, nature: str, paire: str, debut: float,
                  fin: float | None = None, detail: str = "") -> None:
        self.comptes_anomalies[(gravite, nature)] += 1
        liste = self.anomalies[gravite]
        if len(liste) < ANOMALIES_MAX[gravite]:
            liste.append({"gravite": gravite, "nature": nature,
                          "paire": paire, "debut": debut,
                          "fin": fin if fin is not None else debut,
                          "detail": detail})

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
                      chemins: Sequence[Any],
                      signaux: Sequence[Any] = (),
                      respirer: Callable[[], None] | None = None) -> None:
        """`bougies` : les M1 de la paire (au-delà de la fenêtre, ignorées) ;
        `chemins` : ses chemins de la fenêtre, dans l'ordre ; `signaux` : ses
        signaux ZoneH1 (`ts_sec` = instant de décision)."""
        self.paires.setdefault(paire, _Compteurs())
        bougies_m = {b.ts_sec: b for b in bougies
                     if b.tf_sec == 60 and self.debut <= b.ts_sec < self.fin}
        resumes: dict[int, dict] = {}
        dernier_ms = None
        derniere_minute = None
        for k, chemin in enumerate(chemins):
            if respirer is not None and k and k % 2000 == 0:
                respirer()
            m = chemin.minute_sec
            if not self.debut <= m < self.fin:
                continue
            self.chemins_lus += 1
            self._empreinte.update(
                f"{paire}|{m}|{chemin.n_ticks}|{chemin.echelle}|".encode())
            self._empreinte.update(chemin.octets)
            try:
                ticks = self.decoder(chemin)
            except BotError as erreur:
                self.illisibles += 1
                self._anomalie("FAIL", "chemin illisible", paire, m, m + 60,
                               str(erreur)[:200])
                continue
            r = self._minute(paire, m, ticks, chemin.echelle,
                             dernier_ms if derniere_minute == m - 60
                             else None)
            r["concordance"] = self._concorder(paire, m, r,
                                               bougies_m.get(m))
            resumes[m] = r
            dernier_ms, derniere_minute = r["dernier"], m
        self._trous_et_debit(paire, resumes, bougies_m)
        self._signaux(paire, resumes, signaux)

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
                           f"{hors} tick(s)")
        decr = sum(1 for a, b in zip(ts, ts[1:]) if b < a)
        if decr:
            self.decroissants += decr
            self._anomalie("FAIL", "horodatages décroissants", paire, m,
                           m + 60, f"{decr} fois")
        for x in ts:
            if x % 1000 == 0:
                self.resolution[1000] += 1
            if x % 100 == 0:
                self.resolution[100] += 1
            if x % 10 == 0:
                self.resolution[10] += 1
        self.resolution["n"] += n
        meme_instant = exacts = 0
        ecart_max = 0
        ecarts = [b - a for a, b in zip(ts, ts[1:])]
        for k, e in enumerate(ecarts):
            if e == 0:
                meme_instant += 1
                exacts += prix[k] == prix[k + 1]
        self.doublons_instant += meme_instant
        entree = ts[0] - dernier_ms if dernier_ms is not None else None
        for e in ecarts + ([entree] if entree is not None else []):
            self.ecarts[_casier(e)] += 1
            ecart_max = max(ecart_max, e)
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
            c.gels += gel
            c.doublons_exacts += exacts
        return {"n": n, "premier": ts[0], "dernier": ts[-1],
                "ecart_max": ecart_max, "o": prix[0], "h": max(prix),
                "l": min(prix), "c": prix[-1],
                "pas": 10 ** -echelle if echelle else 1.0}

    def _concorder(self, paire: str, m: int, r: dict,
                   b: Candle | None) -> str | None:
        """La classe de la minute, chemin face à sa bougie (None : pas de
        bougie). Tolérance : une demi-unité de la grille décimale."""
        if b is None:
            for c in self._compteurs(paire, m):
                c.chemin_sans_bougie += 1
            self._anomalie("WARNING", "minute de ticks sans bougie", paire,
                           m, m + 60, f"{r['n']} ticks")
            return None
        tol = r["pas"] / 2 + 1e-12
        pareils = {"o": abs(b.open - r["o"]) <= tol,
                   "h": abs(b.high - r["h"]) <= tol,
                   "l": abs(b.low - r["l"]) <= tol,
                   "c": abs(b.close - r["c"]) <= tol,
                   "n": b.tick_count == r["n"]}
        sous_ensemble = r["h"] <= b.high + tol and r["l"] >= b.low - tol
        sur_ensemble = b.high <= r["h"] + tol and b.low >= r["l"] - tol
        coupee = (r["premier"] - m * 1000 > BORD_SEC * 1000
                  or (m + 60) * 1000 - r["dernier"] > BORD_SEC * 1000)
        if all(pareils.values()):
            classe = IDENTIQUE
        elif not b.complete:
            classe = "bougie incomplète"
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
                f"L {r['l']} C {r['c']} n {r['n']}")
        self.concordance[classe] += 1
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
                self._anomalie(
                    "WARNING" if ecoute * 2 >= len(absentes) else "INFO",
                    "interruption longue", paire, absentes[0],
                    absentes[-1] + 60,
                    f"{len(absentes)} min sans ticks, collecteur à l'écoute "
                    f"{ecoute} min")
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
                "non souscrite par le collecteur, ou stockage des ticks coupé")
        # Des ticks là où le collecteur ne battait pas : horodatage décalé ?
        if ecoutees is not None:
            hors = [m for m in sorted(resumes) if m not in ecoutees]
            self.sans_battement += len(hors)
            debut_serie = None
            for i, m in enumerate(hors):
                if debut_serie is None:
                    debut_serie = m
                if i + 1 == len(hors) or hors[i + 1] != m + 60:
                    self._anomalie(
                        "WARNING", "ticks sans battement du collecteur",
                        paire, debut_serie, m + 60,
                        f"{(m + 60 - debut_serie) // 60} min : horodatage "
                        f"décalé (changement d'heure ?) ou battements "
                        f"perdus")
                    debut_serie = None
        # Le débit, jugé contre la paire elle-même.
        par_jour: dict[int, list[int]] = {}
        for m, r in resumes.items():
            par_jour.setdefault(_jour(m), []).append(r["n"])
        tout = [r["n"] for r in resumes.values()]
        mediane = _quantile(tout, 0.5)
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
                    f"fenêtre ({len(valeurs)} min)")

    def _signaux(self, paire: str, resumes: Mapping[int, dict],
                 signaux: Sequence[Any]) -> None:
        for s in signaux:
            d = int(s.ts_sec)
            if not self.debut <= d < self.fin:
                continue                  # hors de la fenêtre auditée
            if not (self.debut + AVANT_SEC <= d <= self.fin - APRES_SEC):
                # Trop près d'un bord : ses 30 minutes débordent.
                self.hors_fenetre += 1
                continue
            motif = self._motif(d, resumes)
            joue = self.joue(s) if self.joue is not None else None
            for compte, motifs, inclus in (
                    (self.signaux, self.motifs, True),
                    (self.signaux_joues, self.motifs_joues, joue)):
                if not inclus:
                    continue
                compte[0] += 1
                if motif is None:
                    compte[1] += 1
                else:
                    motifs[motif] += 1
            jour = self.par_jour_signaux.setdefault(_jour(d), [0, 0])
            jour[0] += 1
            jour[1] += motif is None
            c = self.paires[paire]
            c.signaux += 1
            c.signaux_complets += motif is None

    @staticmethod
    def _motif(d: int, resumes: Mapping[int, dict]) -> str | None:
        minutes = list(range(d - AVANT_SEC, d + APRES_SEC, 60))
        if any(m not in resumes for m in minutes):
            return MOTIFS[0]
        premiere, derniere = resumes[minutes[0]], resumes[minutes[-1]]
        if premiere["premier"] - minutes[0] * 1000 > BORD_SEC * 1000:
            return MOTIFS[1]
        if (d + APRES_SEC) * 1000 - derniere["dernier"] > BORD_SEC * 1000:
            return MOTIFS[2]
        for k, m in enumerate(minutes):
            r = resumes[m]
            entree = (r["premier"] - resumes[minutes[k - 1]]["dernier"]
                      if k else 0)
            if max(r["ecart_max"], entree) >= GEL_SEC * 1000:
                return MOTIFS[3]
        if any(resumes[m]["n"] < TICKS_MIN_MINUTE for m in minutes):
            return MOTIFS[4]
        if any(resumes[m]["concordance"] == INEXPLIQUE for m in minutes):
            return MOTIFS[5]
        if d * 1000 - resumes[d - 60]["dernier"] > BORD_SEC * 1000:
            return MOTIFS[6]
        return None

    # --- les ordres réels -------------------------------------------------------

    def comparer_ordres(self, ordres: Sequence[Mapping[str, Any]],
                        ticks_autour: Callable[[str, int, int],
                                               Sequence[Tick]]) -> None:
        """`ordres` : les lignes de `REQUETES["ordres"]`, en dictionnaires ;
        `ticks_autour(paire, debut_sec, fin_sec)` : nos ticks (lecture)."""
        heures = [round(o["decalage_broker_ms"] / HEURE_MS) for o in ordres
                  if o.get("decalage_broker_ms") is not None]
        k_mode = Counter(heures).most_common(1)[0][0] if heures else None
        for o in ordres:
            rapatrie = "rapatrie_du_broker" in str(o.get("brut") or "")
            decalage = o.get("decalage_broker_ms")
            if decalage is not None:
                k = round(decalage / HEURE_MS)
                reste = decalage - k * HEURE_MS
            else:
                k, reste = k_mode, None
            ligne = {"paire": o["pair"], "ouverture_ms": o["ouverture_ts_ms"],
                     "rapatrie": rapatrie, "reste_ms": reste,
                     "seconde_ronde": o["ouverture_ts_ms"] % 1000 == 0}
            if k is None:
                ligne["motif"] = "décalage d'horloge inconnu"
                self.ordres.append(ligne)
                continue
            for cle, instant, prix in (
                    ("ouverture", o["ouverture_ts_ms"], o.get("prix_entree")),
                    ("cloture", o.get("expiration_ts_ms"),
                     o.get("prix_sortie"))):
                if instant is None or not prix:
                    continue
                ligne[cle] = self._retrouver(
                    o["pair"], int(instant) - k * HEURE_MS, float(prix),
                    ticks_autour)
            if not rapatrie and reste is not None and o.get("prix_attendu"):
                ligne["attendu"] = self._retrouver(
                    o["pair"], int(o["signal_ts_ms"]) + reste,
                    float(o["prix_attendu"]), ticks_autour)
            for cle in ("ouverture", "cloture"):
                v = ligne.get(cle)
                if v and v["ticks"] and not v["trouve"][
                        FENETRES_PRIX_MS.index(FENETRE_PRIX_JUGEE_MS)]:
                    self._anomalie(
                        "WARNING", f"prix {LIBELLES_PRIX[cle]} absent du flux "
                                   f"à ± 1 s",
                        o["pair"], v["instant_ms"] / 1000, None,
                        f"broker {v['prix']} ; dernier tick "
                        f"{v['dernier']} ({v['ecart_points']} point(s))")
            self.ordres.append(ligne)

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
                  "dernier": None, "ecart_points": None}
        if not ticks:
            return sortie
        avant = [t for t in ticks if t.ts_ms <= instant_ms]
        dernier = (avant[-1] if avant else ticks[0]).price
        pas = 10 ** -max(_decimales(prix), max(_decimales(t.price)
                                               for t in ticks))
        tol = pas / 2
        sortie["dernier"] = dernier
        sortie["ecart_points"] = round(abs(dernier - prix) / pas)
        for i, w in enumerate(FENETRES_PRIX_MS):
            if w == 0:
                sortie["trouve"][i] = abs(dernier - prix) <= tol
            else:
                sortie["trouve"][i] = any(
                    abs(t.ts_ms - instant_ms) <= w
                    and abs(t.price - prix) <= tol for t in ticks)
        return sortie

    # --- le résultat ------------------------------------------------------------

    def anomalies_triees(self) -> list[dict]:
        return [a for g in GRAVITES
                for a in sorted(self.anomalies[g],
                                key=lambda a: (a["debut"], a["paire"]))]

    def resultat(self, *, commit: str = "", parametres_extra:
                 Mapping[str, Any] | None = None) -> dict:
        n = self.resolution.get("n", 0)
        g = self.global_
        comparees = sum(self.concordance.values())
        explicables = sum(self.concordance.get(c, 0) for c in EXPLICABLES)
        inexpliques = self.concordance.get(INEXPLIQUE, 0)
        ecoutees = self._minutes_ecoutees()
        # Les (paire, minute) où le collecteur battait : le dénominateur des
        # trous qui comptent.
        attendues_ecoutees = (
            sum(1 for m in range(self.debut, self.fin, 60) if m in ecoutees)
            * len(self.paires) if ecoutees is not None else 0)

        # 1. Horodatages
        granularite = next((d for d in (1000, 100, 10)
                            if n and self.resolution.get(d, 0) >= 0.99 * n),
                           1)
        horodatages = {
            "ticks": n, "granularite_ms": granularite if n else None,
            "part_seconde_ronde": (self.resolution.get(1000, 0) / n
                                   if n else None),
            "doublons_instant": self.doublons_instant,
            "doublons_exacts": g.doublons_exacts,
            "hors_minute": self.hors_minute,
            "decroissants": self.decroissants,
            "illisibles": self.illisibles,
            "minutes_gelees": self.minutes_gelees,
            "ecarts_ms": {"p50": _quantile_histogramme(self.ecarts, 0.5),
                          "p90": _quantile_histogramme(self.ecarts, 0.9),
                          "p99": _quantile_histogramme(self.ecarts, 0.99),
                          "max": self.ecart_max_ms},
            "histogramme": {str(k): v for k, v in sorted(self.ecarts.items())},
        }
        if not n:
            statut_h = "INDÉTERMINÉ"
        elif (granularite >= 1000 or self.hors_minute or self.decroissants
              or self.illisibles):
            statut_h = "FAIL"
        elif (g.doublons_exacts > 0.01 * n
              or self.minutes_gelees > 0.01 * max(1, g.minutes)):
            statut_h = "WARNING"
        else:
            statut_h = "PASS"
        horodatages["statut"] = statut_h

        # 2. Concordance
        if not comparees:
            statut_c = "INDÉTERMINÉ"
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
            "champs_inexpliques": dict(self.ecarts_concordance),
            "chemin_sans_bougie": g.chemin_sans_bougie,
            "bougie_sans_chemin": g.bougie_sans_chemin}

        # 3. Trous. Une paire sans AUCUN tick n'est pas trouée : elle n'est
        # pas collectée. Elle est signalée, et sortie du statut.
        total_minutes = (self.fin - self.debut) // 60 * max(1, len(self.paires))
        sans_ticks = sorted(p for p, c in self.paires.items() if not c.minutes)
        if ecoutees is None:
            statut_t = "INDÉTERMINÉ"
            part = None
        else:
            gardees = len(self.paires) - len(sans_ticks)
            manquantes = g.manquantes_ecoutees - sum(
                self.paires[p].manquantes_ecoutees for p in sans_ticks)
            ecoutees_par_paire = (attendues_ecoutees // len(self.paires)
                                  if self.paires else 0)
            denominateur = ecoutees_par_paire * gardees
            part = manquantes / denominateur if denominateur else None
            statut_t = ("INDÉTERMINÉ" if part is None else
                        "FAIL" if part > 0.05 else
                        "WARNING" if part > 0.01 else "PASS")
        trous = {
            "statut": statut_t, "minutes_attendues": total_minutes,
            "minutes_avec_ticks": g.minutes,
            "manquantes_ecoutees": g.manquantes_ecoutees,
            "manquantes_muettes": g.manquantes_muettes,
            "part_manquante_ecoutee": part,
            "paires_sans_ticks": sans_ticks,
            "minutes_sans_battement": self.sans_battement,
            "battements": len(self.battements),
            "interruptions_longues": sum(
                v for (gr, nat), v in self.comptes_anomalies.items()
                if nat == "interruption longue"),
            "debits_anormaux": sum(
                v for (gr, nat), v in self.comptes_anomalies.items()
                if nat == "débit anormal"),
            "ticks_par_minute": g.resume()["ticks_min"]}

        # 4. Signaux
        total, complets = self.signaux
        statut_s = ("INDÉTERMINÉ" if not total else
                    "FAIL" if complets < 0.5 * total else
                    "WARNING" if complets < 0.8 * total else "PASS")
        signaux = {"statut": statut_s, "signaux": list(self.signaux),
                   "joues": list(self.signaux_joues),
                   "motifs": {m: self.motifs.get(m, 0) for m in MOTIFS},
                   "motifs_joues": {m: self.motifs_joues.get(m, 0)
                                    for m in MOTIFS},
                   "hors_fenetre": self.hors_fenetre,
                   "par_jour": {str(j): v for j, v in
                                sorted(self.par_jour_signaux.items())}}

        # 5. Prix
        prix = self._resume_prix()

        controles = {"horodatages": horodatages, "concordance": concordance,
                     "trous": trous, "signaux": signaux, "prix": prix}
        anomalies = self.anomalies_triees()
        return {
            "debut": self.debut, "fin": self.fin,
            "paires": {p: c.resume() for p, c in sorted(self.paires.items())},
            "jours": {str(j): c.resume() for j, c in sorted(self.jours.items())},
            "heures": {str(h): c.resume() for h, c in self.heures.items()},
            "controles": controles,
            "statuts": {k: v["statut"] for k, v in controles.items()},
            "anomalies": {
                "comptes": [[gr, nat, v] for (gr, nat), v in sorted(
                    self.comptes_anomalies.items(),
                    key=lambda kv: (GRAVITES.index(kv[0][0]), -kv[1]))],
                "premieres": anomalies[:20]},
            "limites": self._limites(controles),
            "reproductibilite": {
                "commit": commit, "chemins_lus": self.chemins_lus,
                "empreinte": self._empreinte.hexdigest()[:16],
                "requetes": dict(REQUETES), "seuils": dict(SEUILS),
                "parametres": {
                    "gel_sec": GEL_SEC, "bord_sec": BORD_SEC,
                    "ticks_min_minute": TICKS_MIN_MINUTE,
                    "fenetre_signal_sec": [AVANT_SEC, APRES_SEC],
                    "interruption_longue_min": INTERRUPTION_LONGUE_MIN,
                    "debit": [DEBIT_BAS, DEBIT_HAUT],
                    "battement_sec": [BATTEMENT_AVANT_SEC,
                                      BATTEMENT_APRES_SEC],
                    "fenetres_prix_ms": list(FENETRES_PRIX_MS),
                    **dict(parametres_extra or {})}}}

    def _resume_prix(self) -> dict:
        i = FENETRES_PRIX_MS.index(FENETRE_PRIX_JUGEE_MS)
        sortie: dict = {"ordres": len(self.ordres),
                        "rapatries": sum(o["rapatrie"] for o in self.ordres),
                        "sans_decalage": sum(
                            1 for o in self.ordres
                            if o.get("motif") == "décalage d'horloge inconnu"),
                        "seconde_ronde": sum(o["seconde_ronde"]
                                             for o in self.ordres)}
        for cle in ("ouverture", "cloture", "attendu"):
            vus = [o[cle] for o in self.ordres if o.get(cle)]
            comparables = [v for v in vus if v["ticks"]]
            sortie[cle] = {
                "ordres": len(vus), "comparables": len(comparables),
                "trouves": [sum(v["trouve"][j] for v in comparables)
                            for j in range(len(FENETRES_PRIX_MS))],
                "ecart_points_median": _quantile(
                    [v["ecart_points"] for v in comparables], 0.5)}
        restes = [o["reste_ms"] for o in self.ordres
                  if o.get("reste_ms") is not None]
        sortie["synchronisation_ms"] = [_quantile(restes, 0.1),
                                        _quantile(restes, 0.5),
                                        _quantile(restes, 0.9)]
        jugees = [sortie["ouverture"], sortie["cloture"]]
        if min(j["comparables"] for j in jugees) < ORDRES_MIN:
            statut = "INDÉTERMINÉ"
        else:
            part = min(j["trouves"][i] / j["comparables"] for j in jugees)
            statut = ("FAIL" if part < 0.7 else
                      "WARNING" if part < 0.9 else "PASS")
        sortie["statut"] = statut
        return sortie

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
            "nulle part ; leur nombre n'est pas mesurable.",
            "Un seul prix par tick : ni achat/vente séparés, ni volume.",
            "Flux REÇU, pas flux interne du broker : on ne voit que ce que le "
            "websocket envoie ; un échantillonnage côté broker serait "
            "invisible.",
            "Concordance M1 ≠ exactitude : bougies et chemins viennent des "
            "mêmes ticks ; leur accord prouve la cohérence du stockage. Le "
            "seul contrôle EXTERNE est la comparaison aux prix des ordres "
            "réels.",
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
                    p.replace("_otc", "") for p in t["paires_sans_ticks"])
                + ".")
        if t["statut"] == "INDÉTERMINÉ":
            limites.append("Aucun battement du collecteur dans la fenêtre : "
                           "un trou de collecte ne se distingue pas d'une "
                           "paire muette.")
        if p["ordres"] and p["seconde_ronde"] >= 0.9 * p["ordres"]:
            limites.append(
                "Horodatages d'ouverture du broker à la seconde entière : "
                "l'instant d'exécution n'est connu qu'à ± 1 s.")
        comparables = p["ouverture"]["comparables"]
        if comparables < 30:
            limites.append(
                f"{comparables} ordre(s) réel(s) comparable(s) seulement : le "
                f"contrôle externe des prix reste faible.")
        return limites


def csv_anomalies(anomalies: Sequence[Mapping[str, Any]]) -> bytes:
    """La fiche complète des anomalies, séparateur « ; », horodatages UTC."""
    lignes = ["gravite;nature;paire;debut_utc;fin_utc;detail"]
    for a in anomalies:
        detail = str(a.get("detail", "")).replace(";", ",").replace("\n", " ")
        lignes.append(";".join([
            a["gravite"], a["nature"], a["paire"], _horodatage(a["debut"]),
            _horodatage(a["fin"]), detail]))
    return ("\n".join(lignes) + "\n").encode("utf-8")

