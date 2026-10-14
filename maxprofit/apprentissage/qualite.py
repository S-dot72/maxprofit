"""
EXP-001 — la qualité des bougies rejouées, mesurée avant d'en rien conclure.

--- D'où elle vient --------------------------------------------------------

Protocole de simulation OTC, version 1 (2026-10-13) : « vérifier les
horodatages, les bougies manquantes, les doublons, les changements de flux
et la cohérence des prix » ; « si les données sont indisponibles ou
incohérentes, l'observation doit être marquée plutôt que réparée
silencieusement ».

--- La source, et ce qu'elle implique --------------------------------------

Les bougies OTC ne viennent d'aucun historique de marché : le collecteur
les CONSTRUIT, minute par minute, à partir des ticks que Pocket Option
pousse sur son websocket (`collect.collector.CandleAggregator`). Les prix
OTC sont générés par le broker, sept jours sur sept : une minute absente
n'est jamais un marché fermé, c'est une minute que le collecteur n'a pas
vue (déconnexion, paire non suivie, redémarrage).

Deux contrôles du protocole sont donc garantis EN AMONT, et seulement
rapportés ici :
- doublons : impossibles, la clé primaire de `candles` est (paire,
  échelle, début de minute) ;
- horodatage aligné et OHLC cohérent (bas ≤ ouverture, clôture ≤ haut) :
  le type `Candle` refuse toute bougie qui viole l'un ou l'autre, et la
  lecture échoue alors bruyamment — le rejeu ne peut pas s'en servir en
  silence.

--- Ce qui est marqué, bougie par bougie ------------------------------------

Un seul drapeau par bougie, le plus grave d'abord :

1. `incomplete` : la minute n'a pas été observée jusqu'au bout (coupure,
   arrêt). Le rejeu l'écarte déjà.
2. `peu_de_ticks` : moins de `TICKS_MIN` ticks, seuil de la spec §2.4.
3. `reprise` : la première minute après un trou. Son ouverture est le
   premier tick reçu APRÈS la reconnexion, pas le vrai prix d'ouverture :
   le collecteur la marque complète faute de savoir qu'il a raté le début.
4. `saut` : l'ouverture s'écarte de la clôture précédente de plus de
   `SAUT_AMPLITUDES` amplitudes médianes de la paire. Les deux sont deux
   ticks consécutifs du même flux : un tel écart signale un changement de
   flux, pas un mouvement de marché.
5. `figee` : une série d'au moins `FIGEE_SERIE` minutes au prix unique
   (haut = bas) malgré des ticks — le flux répète une valeur.

Les minutes ABSENTES entre la première et la dernière bougie d'une paire
sont comptées à part (trous), et rejoignent les intervalles suspects :
c'est ce qui permet de dire, signal par signal, si sa fenêtre ou son
chemin jusqu'à l'échéance traversait une donnée douteuse.

--- L'empreinte ------------------------------------------------------------

SHA-256 des bougies lues, paire par paire puis globale : deux rejeux sur
la même période et les mêmes données donnent la même empreinte. Une
empreinte qui change pour une période identique dit que la base a bougé —
une minute partielle remplacée par la complète, par exemple.
"""

from __future__ import annotations

import bisect
import hashlib
import statistics
from typing import Callable, Sequence

from maxprofit.core.types import Candle

DRAPEAUX: tuple[str, ...] = ("incomplete", "peu_de_ticks", "reprise",
                             "saut", "figee")
LIBELLES_DRAPEAUX: dict[str, str] = {
    "incomplete": "minute incomplète (coupure, arrêt)",
    "peu_de_ticks": "moins de 5 ticks",
    "reprise": "première minute après un trou (ouverture manquée)",
    "saut": "saut d'ouverture (changement de flux ?)",
    "figee": "prix figé plusieurs minutes",
}
#: Seuil de la spec §2.4, le même que `Candle.is_usable`.
TICKS_MIN = 5
#: Écart ouverture / clôture précédente, en amplitudes médianes.
SAUT_AMPLITUDES = 3.0
#: Minutes consécutives au prix unique pour parler de flux figé.
FIGEE_SERIE = 3
#: Écart du débit de ticks journalier à la médiane de la paire au-delà
#: duquel la journée est signalée (changement de flux ?).
ECART_DEBIT = 0.5
#: Source des bougies. Elle n'est pas enregistrée bougie par bougie : le
#: collecteur hébergé n'en a qu'une.
SOURCE = "Pocket Option, websocket — ticks agrégés en M1 par le collecteur"


def _ligne(b: Candle) -> bytes:
    """Forme canonique d'une bougie pour l'empreinte. `repr` d'un flottant
    est exact et stable : deux bougies identiques donnent les mêmes octets."""
    return (f"{b.ts_sec},{b.open!r},{b.high!r},{b.low!r},{b.close!r},"
            f"{b.tick_count},{int(b.complete)}\n").encode()


class Qualite:
    """Accumule, paire après paire, le contrôle des bougies lues."""

    def __init__(self, debut_ts: int, fin_ts: int, coupure_ts: int):
        self.debut, self.fin = int(debut_ts), int(fin_ts)
        self.coupure = int(coupure_ts)
        #: {paire: {...}} — voir `ajouter`.
        self.paires: dict[str, dict] = {}
        #: Par période : {drapeau: [anciens, récents]}, et les bougies lues.
        self.periodes: dict[str, list[int]] = {
            d: [0, 0] for d in ("lues", "ok", *DRAPEAUX)}
        #: {paire: [(début, fin), ...]} triés et fusionnés, en débuts de
        #: minute inclus : les minutes absentes ou marquées. En mémoire
        #: seulement, pour juger les signaux du même rejeu.
        self.intervalles: dict[str, list[tuple[int, int]]] = {}
        self._empreintes: dict[str, str] = {}
        #: Les minutes où AU MOINS UNE paire a une bougie complète : le
        #: temps de collecte réel. Diviser un nombre de signaux par des
        #: jours de calendrier, quand la collecte n'écoutait que la moitié
        #: du temps, divisait le débit par deux.
        self._minutes: set[int] = set()

    def ajouter(self, bougies: Sequence[Candle],
                respirer: Callable[[], None] | None = None) -> None:
        if not bougies:
            return
        pair = bougies[0].pair
        serie = sorted(bougies, key=lambda b: b.ts_sec)
        empreinte = hashlib.sha256()
        amplitudes = [b.high - b.low for b in serie
                      if b.complete and b.high > b.low]
        mediane = statistics.median(amplitudes) if amplitudes else 0.0
        comptes = {d: 0 for d in DRAPEAUX}
        trous, manquantes, plus_long = 0, 0, 0
        suspects: list[tuple[int, int]] = []
        ticks_par_jour: dict[int, list[int]] = {}
        precedente: Candle | None = None
        #: {début de minute: drapeau} des bougies marquées.
        drapeaux: dict[int, str] = {}
        figees: list[Candle] = []

        def clore_figees() -> None:
            if len(figees) >= FIGEE_SERIE:
                for f in figees:
                    drapeaux.setdefault(f.ts_sec, "figee")
            figees.clear()

        for i, b in enumerate(serie):
            if respirer is not None and i and i % 5000 == 0:
                respirer()
            empreinte.update(_ligne(b))
            if b.complete:
                self._minutes.add(b.ts_sec)
            if precedente is not None and b.ts_sec - precedente.ts_sec > 60:
                absentes = (b.ts_sec - precedente.ts_sec) // 60 - 1
                trous += 1
                manquantes += absentes
                plus_long = max(plus_long, absentes)
                suspects.append((precedente.ts_sec + 60, b.ts_sec - 60))
            contigue = (precedente is not None
                        and precedente.ts_sec == b.ts_sec - 60)
            if not b.complete:
                drapeaux[b.ts_sec] = "incomplete"
            elif b.tick_count < TICKS_MIN:
                drapeaux[b.ts_sec] = "peu_de_ticks"
            elif precedente is not None and (not contigue
                                             or not precedente.complete):
                drapeaux[b.ts_sec] = "reprise"
            elif (contigue and mediane > 0 and abs(b.open - precedente.close)
                  > SAUT_AMPLITUDES * mediane):
                drapeaux[b.ts_sec] = "saut"
            if b.complete:
                ticks_par_jour.setdefault(b.ts_sec // 86400, []).append(
                    b.tick_count)
            plate = b.complete and b.high == b.low
            if not (plate and figees and contigue):
                clore_figees()
            if plate:
                figees.append(b)
            precedente = b
        clore_figees()

        for b in serie:
            k = 0 if b.ts_sec < self.coupure else 1
            self.periodes["lues"][k] += 1
            drapeau = drapeaux.get(b.ts_sec)
            if drapeau is None:
                self.periodes["ok"][k] += 1
                continue
            comptes[drapeau] += 1
            self.periodes[drapeau][k] += 1
            suspects.append((b.ts_sec, b.ts_sec))
        suspects.sort()
        fusion: list[tuple[int, int]] = []
        for debut, fin in suspects:
            if fusion and debut <= fusion[-1][1] + 60:
                fusion[-1] = (fusion[-1][0], max(fin, fusion[-1][1]))
            else:
                fusion.append((debut, fin))
        self.intervalles[pair] = fusion

        medianes_jour = {j: statistics.median(t)
                         for j, t in ticks_par_jour.items()}
        reference = (statistics.median(medianes_jour.values())
                     if medianes_jour else 0)
        anormaux = sorted(
            j * 86400 for j, m in medianes_jour.items()
            if reference and abs(m - reference) > ECART_DEBIT * reference)
        premier, dernier = serie[0].ts_sec, serie[-1].ts_sec
        self._empreintes[pair] = empreinte.hexdigest()
        self.paires[pair] = {
            "lues": len(serie),
            "premiere": premier, "derniere": dernier,
            "couvertes": (dernier - premier) // 60 + 1,
            # Minutes de la période AVANT la première bougie : la paire
            # n'était pas suivie, ce n'est pas un trou de collecte.
            "non_suivies": max(0, (premier - self.debut) // 60),
            "trous": trous, "manquantes": manquantes,
            "plus_long_trou": plus_long,
            "drapeaux": comptes,
            "ok": len(serie) - sum(comptes.values()),
            "ticks_median": reference,
            "jours_debit_anormal": anormaux,
            "empreinte": self._empreintes[pair][:16],
        }

    def minutes_par_jour(self) -> dict[int, int]:
        """{jour UTC (en jours depuis l'époque) : minutes collectées}."""
        jours: dict[int, int] = {}
        for m in self._minutes:
            jours[m // 86400] = jours.get(m // 86400, 0) + 1
        return jours

    def jours_collectes(self, debut: int, fin: int) -> float:
        """Les jours de collecte réelle dans [debut, fin) : minutes
        collectées / 1 440."""
        return sum(1 for m in self._minutes if debut <= m < fin) / 1440

    def touche(self, pair: str, debut: int, fin: int) -> bool:
        """Un intervalle suspect de `pair` coupe-t-il [debut, fin] (débuts
        de minute inclus) ?"""
        intervalles = self.intervalles.get(pair)
        if not intervalles:
            return False
        i = bisect.bisect_right(intervalles, (fin, float("inf"))) - 1
        return i >= 0 and intervalles[i][1] >= debut

    def empreinte(self) -> str:
        """L'empreinte de TOUTES les paires lues, indépendante de l'ordre."""
        globale = hashlib.sha256()
        for pair in sorted(self._empreintes):
            globale.update(f"{pair}:{self._empreintes[pair]}\n".encode())
        return globale.hexdigest()

    def resultat(self) -> dict:
        """En JSON : les clés sont du texte."""
        return {"debut": self.debut, "fin": self.fin,
                "coupure": self.coupure, "source": SOURCE,
                "seuils": {"ticks_min": TICKS_MIN,
                           "saut_amplitudes": SAUT_AMPLITUDES,
                           "figee_serie": FIGEE_SERIE,
                           "ecart_debit": ECART_DEBIT},
                "empreinte": self.empreinte(),
                "periodes": {d: list(c) for d, c in self.periodes.items()},
                "paires": self.paires}
