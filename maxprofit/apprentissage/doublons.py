"""
Enquête sur la double réception des ticks : des politiques de déduplication
comparées HORS LIGNE, en mémoire, sur des chemins déjà enregistrés. Rien n'est
écrit, le collecteur n'est pas touché (docs/enquete_doublons.md).

--- Ce que les données permettent, et ce qu'elles ne permettent pas -----------

Un tick stocké n'a que (paire, instant en ms, prix). Le broker n'envoie ni
identifiant ni numéro de séquence (`updateStream` : [[actif, instant, prix]]),
la bibliothèque ne garde que {time, price}, et le socket d'origine n'est
conservé nulle part. Deux enregistrements identiques peuvent donc être un même
tick reçu deux fois, ou deux ticks légitimes au même instant et au même prix :
AUCUNE donnée stockée ne les distingue.

Politiques :

  aucune         le flux tel qu'il est stocké ;
  prouvee        les doublons démontrés par une identité d'événement — NOT
                 TESTABLE : cette identité n'existe pas ;
  experimentale  un (instant, prix) déjà vu dans la minute est écarté ;
                 heuristique, pour comparaison seulement.

Le taux de faux positifs de l'heuristique ne se mesure pas : il se BORNE. Dans
les minutes « simples » (moins de `PART_SIMPLE` de doublons exacts : un seul
flux), les enregistrements identiques sont des coïncidences légitimes ou des
doublons isolés ; leur part majore ce que l'heuristique écarterait à tort dans
un flux sain.

Ce module ne lit pas la base (règle des couches) : on lui passe des ticks
décodés, et facultativement la bougie de la minute.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from maxprofit.core.types import Candle, Tick

POLITIQUES = ("aucune", "prouvee", "experimentale")
NON_TESTABLE = "NOT TESTABLE"
RAISON_PROUVEE = (
    "aucune identité d'événement : le broker envoie [[actif, instant, prix]] "
    "sans identifiant ni numéro de séquence, la bibliothèque ne garde que "
    "{time, price}, et le socket d'origine n'est pas conservé")
#: Une minute est « simple » sous 10 % de doublons exacts, « doublée » à 40 %
#: ou plus (chaque tick reçu deux fois en donne 50 %), « mixte » entre les
#: deux.
PART_SIMPLE, PART_DOUBLE = 0.10, 0.40
#: Le seuil des minutes pauvres, partout le même : EXP-001 (`peu_de_ticks`),
#: `Candle.is_usable`, le moteur de backtest, l'audit (INSUFFICIENT_COVERAGE).
TICKS_MIN = 5
#: Écart du débit journalier à la médiane de la paire au-delà duquel EXP-001
#: signale la journée (`qualite.ECART_DEBIT`).
ECART_DEBIT = 0.5
#: Bornes (ms) de l'histogramme des écarts entre ticks consécutifs.
BORNES_ECARTS_MS: tuple[int, ...] = (0, 10, 100, 500, 1000, 2000, 10000)


def dedoublonner(ticks: Sequence[Tick], politique: str) -> list[Tick] | None:
    """Les ticks d'une minute selon la politique ; None si elle n'est pas
    applicable (« prouvee »). La première occurrence est gardée : l'ordre
    des ticks restants ne change pas."""
    if politique == "aucune":
        return list(ticks)
    if politique == "prouvee":
        return None
    if politique != "experimentale":
        raise ValueError(f"politique inconnue : {politique!r}")
    vus: set[tuple[int, float]] = set()
    sortie = []
    for t in ticks:
        cle = (t.ts_ms, t.price)
        if cle not in vus:
            vus.add(cle)
            sortie.append(t)
    return sortie


def _quantile_histo(histo: Mapping[int, int], q: float) -> int | None:
    total = sum(histo.values())
    if not total:
        return None
    seuil, cumul = q * total, 0
    for borne in sorted(histo, key=lambda b: (b < 0, b)):
        cumul += histo[borne]
        if cumul >= seuil:
            return borne
    return None


def _mediane(valeurs: Sequence[float]) -> float | None:
    v = sorted(valeurs)
    if not v:
        return None
    k = len(v) // 2
    return v[k] if len(v) % 2 else (v[k - 1] + v[k]) / 2


class _Politique:
    def __init__(self) -> None:
        self.ticks = 0
        self.ecarts: Counter = Counter()
        self.pauvres = 0
        self.concordance_n = [0, 0]           # bougies comparées, n égal
        self.decroissants = 0
        self.par_jour: dict[tuple[str, int], list[int]] = {}


class ComparaisonDoublons:
    """Les politiques, minute après minute, sur des chemins déjà stockés."""

    def __init__(self) -> None:
        self.politiques = {p: _Politique() for p in ("aucune",
                                                       "experimentale")}
        self.minutes = Counter()              # par régime
        self.ticks_regime = Counter()         # ticks stockés, par régime
        self.exacts_regime = Counter()        # doublons exacts, par régime
        self.multiplicites = Counter()        # 1, 2, 3+ : copies d'un tick
        self.orphelins_doublees = [0, 0]      # distincts uniques, distincts
        self.meme_instant_autre_prix = 0
        self.ohlc_change = Counter()          # champ → minutes changées
        self.pauvres_changent = 0
        self.minutes_doublees_par_jour: dict[int, list[int]] = {}

    def ajouter_minute(self, paire: str, minute: int, ticks: Sequence[Tick],
                       bougie: Candle | None = None) -> None:
        """Une minute de ticks stockés. Tourne dans le rejeu sur chaque
        chemin : une passe sur les ticks, le reste sur des listes d'entiers."""
        if not ticks:
            return
        n = len(ticks)
        ts = [t.ts_ms for t in ticks]
        prix = [t.price for t in ticks]
        cles = Counter(zip(ts, prix))
        exacts = n - len(cles)
        part = exacts / n
        regime = ("simple" if part < PART_SIMPLE else
                  "doublee" if part >= PART_DOUBLE else "mixte")
        self.minutes[regime] += 1
        self.ticks_regime[regime] += n
        self.exacts_regime[regime] += exacts
        copies = Counter(min(c, 3) for c in cles.values())
        self.multiplicites.update(copies)
        if regime == "doublee":
            self.orphelins_doublees[0] += copies.get(1, 0)
            self.orphelins_doublees[1] += len(cles)
        # Même instant, prix différents : distincts par instant moins un.
        par_instant = Counter(t for t, _p in cles)
        self.meme_instant_autre_prix += sum(c - 1 for c in par_instant.values()
                                            if c > 1)
        jour = minute // 86400 * 86400
        v = self.minutes_doublees_par_jour.setdefault(jour, [0, 0])
        v[0] += 1
        v[1] += regime == "doublee"

        # La politique expérimentale : la première occurrence de chaque
        # (instant, prix), dans l'ordre.
        if exacts:
            vus: set = set()
            garde = []
            for k, cle in enumerate(zip(ts, prix)):
                if cle not in vus:
                    vus.add(cle)
                    garde.append(k)
            ts_e = [ts[k] for k in garde]
            prix_e = [prix[k] for k in garde]
        else:
            ts_e, prix_e = ts, prix
        flux = {"aucune": (ts, prix), "experimentale": (ts_e, prix_e)}
        ohlc: dict[str, tuple] = {}
        pauvres: dict[str, bool] = {}
        for nom, pol in self.politiques.items():
            t_, p_ = flux[nom]
            m = len(t_)
            pol.ticks += m
            ecarts = [b - a for a, b in zip(t_, t_[1:])]
            pol.decroissants += sum(1 for e in ecarts if e < 0)
            pol.ecarts.update(BORNES_ECARTS_MS[i]
                              if (i := bisect_left(BORNES_ECARTS_MS, e))
                              < len(BORNES_ECARTS_MS) else -1
                              for e in ecarts)
            pauvres[nom] = m < TICKS_MIN
            pol.pauvres += pauvres[nom]
            if bougie is not None:
                pol.concordance_n[0] += 1
                pol.concordance_n[1] += bougie.tick_count == m
            pol.par_jour.setdefault((paire, jour), []).append(m)
            ohlc[nom] = (p_[0], max(p_), min(p_), p_[-1])
        for i, champ in enumerate("ohlc"):
            if ohlc["aucune"][i] != ohlc["experimentale"][i]:
                self.ohlc_change[champ] += 1
        self.pauvres_changent += pauvres["aucune"] != pauvres["experimentale"]

    def _jours_signales(self, pol: _Politique) -> list[list]:
        """Les (paire, jour) qu'EXP-001 signalerait pour débit anormal."""
        par_paire: dict[str, list[int]] = {}
        for (paire, _jour), valeurs in pol.par_jour.items():
            par_paire.setdefault(paire, []).extend(valeurs)
        signales = []
        for (paire, jour), valeurs in sorted(pol.par_jour.items()):
            reference = _mediane(par_paire[paire])
            m = _mediane(valeurs)
            if reference and m is not None and \
                    abs(m - reference) > ECART_DEBIT * reference:
                signales.append([paire, jour, m, reference])
        return signales

    def resultat(self) -> dict:
        minutes = sum(self.minutes.values())
        simples = self.minutes.get("simple", 0)
        sortie: dict[str, Any] = {
            "minutes": minutes,
            "regimes": {r: {"minutes": self.minutes.get(r, 0),
                            "ticks": self.ticks_regime.get(r, 0),
                            "doublons_exacts": self.exacts_regime.get(r, 0)}
                        for r in ("simple", "mixte", "doublee")},
            # Borne haute des faux positifs : part des enregistrements
            # identiques dans les minutes à un seul flux.
            "borne_faux_positifs": (
                self.exacts_regime.get("simple", 0)
                / self.ticks_regime["simple"]
                if simples and self.ticks_regime.get("simple") else None),
            "multiplicites": {str(k): v for k, v in
                              sorted(self.multiplicites.items())},
            "orphelins_doublees": (
                self.orphelins_doublees[0] / self.orphelins_doublees[1]
                if self.orphelins_doublees[1] else None),
            "meme_instant_autre_prix": self.meme_instant_autre_prix,
            "ohlc_change": {c: self.ohlc_change.get(c, 0) for c in "ohlc"},
            "pauvres_changent": self.pauvres_changent,
            "doublees_par_jour": {str(j): v for j, v in
                                  sorted(self.minutes_doublees_par_jour
                                         .items())},
            "politiques": {
                "prouvee": {"statut": NON_TESTABLE,
                            "raison": RAISON_PROUVEE}},
        }
        for nom, pol in self.politiques.items():
            sortie["politiques"][nom] = {
                "ticks": pol.ticks,
                "ecarts_ms": {f"p{int(q * 100)}": _quantile_histo(pol.ecarts,
                                                                   q)
                              for q in (0.25, 0.5, 0.75)},
                "ecarts_nuls": pol.ecarts.get(0, 0),
                "decroissants": pol.decroissants,
                "minutes_pauvres": pol.pauvres,
                "tick_count_egal": pol.concordance_n[1],
                "bougies_comparees": pol.concordance_n[0],
                "jours_debit_anormal": self._jours_signales(pol),
            }
        return sortie


def comparer(minutes: Iterable[tuple[str, int, Sequence[Tick],
                                     Candle | None]]) -> dict:
    """Raccourci : (paire, minute, ticks, bougie) → résultat."""
    c = ComparaisonDoublons()
    for paire, minute, ticks, bougie in minutes:
        c.ajouter_minute(paire, minute, ticks, bougie)
    return c.resultat()


def _pc(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.2f} %"


def texte(r: Mapping[str, Any]) -> str:
    """Le rapport, chaque phrase étiquetée."""
    a, e = r["politiques"]["aucune"], r["politiques"]["experimentale"]
    g = r["regimes"]
    lignes = [
        "Enquête doublons — comparaison hors ligne (en mémoire, rien "
        "d'écrit)",
        f"[Fait observé] {r['minutes']} minutes : simples "
        f"{g['simple']['minutes']}, mixtes {g['mixte']['minutes']}, doublées "
        f"{g['doublee']['minutes']}.",
        "[Fait observé] Copies par (instant, prix) : " + ", ".join(
            f"{'3 ou plus' if k == '3' else k} → {v}"
            for k, v in r["multiplicites"].items()) + ".",
        f"[Borne] Faux positifs de l'heuristique, au plus "
        f"{_pc(r['borne_faux_positifs'])} des ticks d'un flux simple "
        f"({g['simple']['doublons_exacts']} enregistrement(s) identique(s) "
        f"sur {g['simple']['ticks']} ticks des minutes simples).",
        f"[Fait observé] Minutes doublées : {_pc(r['orphelins_doublees'])} "
        f"des ticks distincts n'ont pas de jumeau.",
        f"[Fait observé] Même instant, autre prix (jamais écartés) : "
        f"{r['meme_instant_autre_prix']}.",
        f"[Fait observé] Ticks : aucune {a['ticks']}, expérimentale "
        f"{e['ticks']} ; écarts nuls {a['ecarts_nuls']} → {e['ecarts_nuls']} ;"
        f" médiane des écarts {a['ecarts_ms']['p50']} → "
        f"{e['ecarts_ms']['p50']} ms (borne de casier).",
        "[Fait observé] OHLC changé par la politique expérimentale : "
        + ", ".join(f"{c.upper()} {v}" for c, v in r["ohlc_change"].items())
        + f" minute(s) ; ordre temporel : {e['decroissants']} inversion(s).",
        f"[Fait observé] tick_count des bougies égal au nombre de ticks : "
        f"aucune {a['tick_count_egal']}/{a['bougies_comparees']}, "
        f"expérimentale {e['tick_count_egal']}/{e['bougies_comparees']}.",
        f"[Fait observé] Minutes pauvres (< {TICKS_MIN} ticks : EXP-001, "
        f"is_usable, backtest, audit) : aucune {a['minutes_pauvres']}, "
        f"expérimentale {e['minutes_pauvres']} ; {r['pauvres_changent']} "
        f"changent de classe.",
        f"[Fait observé] Jours signalés pour débit anormal (EXP-001) : "
        f"aucune {len(a['jours_debit_anormal'])}, expérimentale "
        f"{len(e['jours_debit_anormal'])}.",
        f"[Non testable] Politique « prouvée » : {r['politiques']['prouvee']['raison']}.",
    ]
    return "\n".join(lignes)
