"""
Étape 3 — les variables de microstructure AVANT la décision
(docs/etape3_plan_execution.md, §0 et §4). Pur : ni base, ni horloge.

--- Ce qu'une variable a le droit de lire ---------------------------------------

Les ticks d'instant t avec d − w ≤ t < d − k, et rien d'autre :
- `d` est la décision du rejeu, la fin de la bougie du signal ;
- `w` est la sous-fenêtre (60, 180, 300 ou 900 s) ;
- `k` est la troncature de robustesse (0, 2 ou 5 s). C'est un TEST, pas
  une règle de disponibilité.
Aucun tick à partir de d − k n'entre dans une variable, et les tests le
vérifient en ajoutant des ticks après la coupure.

--- Les deux niveaux de disponibilité -------------------------------------------

Niveau 1 (indicatif) : tout signal dont la fenêtre a des ticks.
Niveau 2 (étayé par le mécanisme de collecte, sous réserve des tests de
concordance) : les 15 minutes avant d sont toutes présentes et en
concordance « identique » ou « ordre d'arrivée ». Ce niveau n'est PAS une
certitude, et le rapport ne le présente pas comme tel.

Une seule dépendance à une minute postérieure à d existe : la classe de
concordance de la dernière minute peut tester l'égalité du chemin avec la
bougie suivante (« décalage de minute »). Cette classe ne peut rivaliser
avec « identique » ou « ordre d'arrivée » que si deux bougies voisines ont
le même nombre de ticks, le même haut et le même bas. Ce n'est pas une
information de marché, et elle ne change pas l'appartenance au niveau 2.

--- La qualité ne prédit pas -----------------------------------------------------

La famille E décrit la fenêtre [d − 900, d) des ticks bruts. Elle classe les
fenêtres (VALID / PARTIAL / INVALID) et sert à mesurer le biais de
sélection ; elle n'entre dans aucun modèle. Elle ne lit rien après d.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from maxprofit.apprentissage.audit_ticks import IDENTIQUE, ORDRE
from maxprofit.core.types import Tick

FENETRES_SEC: tuple[int, ...] = (60, 180, 300, 900)
TRONCATURES_SEC: tuple[int, ...] = (0, 2, 5)
TRAITEMENTS: tuple[str, ...] = ("distincts", "bruts")
#: La fenêtre de qualité et de disponibilité : 15 minutes avant d.
QUALITE_SEC = 900
#: Classes de fenêtre, figées avant toute lecture (plan §3).
VALID, PARTIAL, INVALID = "VALID", "PARTIAL", "INVALID"
VALID_ECART_SEC = 10
VALID_AGE_SEC = 5
PARTIAL_COUVERTURE = 0.8
PARTIAL_ECART_SEC = 30
#: Les classes de concordance qui laissent une minute au niveau 2 : même
#: ensemble de ticks que la bougie que la stratégie a lue.
CONCORDANCES_NIVEAU_2: frozenset[str] = frozenset({IDENTIQUE, ORDRE})
#: La part des plus grands mouvements de B6.
PART_CONCENTRATION = 0.05

#: Les 23 variables de marché, dans l'ordre du plan (A à D).
VARIABLES: tuple[str, ...] = (
    "A1_dist_fin", "A2_dist_debut", "A3_vitesse", "A4_acceleration",
    "A5_contacts", "A6_temps_zone", "A7_penetration", "A8_depuis_contact",
    "A9_apres_contact",
    "B1_vol", "B2_amplitude", "B3_nulles", "B4_efficience", "B5_ratio_vol",
    "B6_concentration",
    "C1_retournements", "C2_persistance", "C3_part_sens",
    "C4_amplitude_retournement", "C5_delai_retournement",
    "D1_traversees", "D2_sorties_retours", "D3_temps_au_dela",
)
QUALITE: tuple[str, ...] = (
    "E1_couverture", "E2_ecart_max_s", "E3_age_dernier_s",
    "E4_part_doublons", "E5_gels", "E6_minutes_fermees",
    "E7_minutes_doublees", "E8_niveau",
)


@dataclass(frozen=True)
class Contexte:
    """Ce que le signal fixe à la décision : instant `d_sec`, sens `s`
    (+1 CALL, −1 PUT), niveau `L` de la zone, tolérance relative de la
    zone (0,02 % pour ZoneH1), pas de cotation `delta`."""

    d_sec: int
    s: int
    niveau: float
    tolerance_pct: float
    delta: float

    @property
    def marge(self) -> float:
        """μ = L × tolérance : la demi-largeur de la zone, en prix."""
        return self.niveau * self.tolerance_pct / 100


def pas_de_cotation(echelles: Iterable[int]) -> float | None:
    """δ = 10^−e, e la plus grande échelle décimale des chemins de la paire
    (`tick_paths.echelle`). Calculé sur l'exploration, puis figé."""
    e = max(echelles, default=None)
    return None if e is None else 10.0 ** -e


def distincts(ticks: Sequence[Tick]) -> list[Tick]:
    """Une copie exacte (même instant, même prix) ne compte qu'une fois. Un
    TRAITEMENT d'analyse : l'historique n'est jamais modifié."""
    vus: set[tuple[int, float]] = set()
    sortie = []
    for t in ticks:
        cle = (t.ts_ms, t.price)
        if cle not in vus:
            vus.add(cle)
            sortie.append(t)
    return sortie


def ticks_de_la_fenetre(par_minute: Mapping[int, Sequence[Tick]],
                        d_sec: int, w_sec: int, *, k_sec: int = 0,
                        traitement: str = "distincts"
                        ) -> list[tuple[int, float]]:
    """(t_ms, prix) des ticks d − w ≤ t < d − k, triés par instant.

    `par_minute` : minute (début, s) → ticks décodés. Seules les minutes
    qui recoupent la fenêtre sont lues, et chaque tick est filtré sur son
    propre instant : une minute présente ne fait pas entrer ses ticks
    postérieurs à la coupure."""
    if traitement not in TRAITEMENTS:
        raise ValueError(f"traitement inconnu : {traitement}")
    debut_ms = (d_sec - w_sec) * 1000
    fin_ms = (d_sec - k_sec) * 1000
    choisis: list[Tick] = []
    for m in range((d_sec - w_sec) // 60 * 60, d_sec, 60):
        for t in par_minute.get(m, ()):
            if debut_ms <= t.ts_ms < fin_ms:
                choisis.append(t)
    choisis.sort(key=lambda t: t.ts_ms)
    if traitement == "distincts":
        choisis = distincts(choisis)
    return [(t.ts_ms, t.price) for t in choisis]


# --------------------------------------------------------------------------- #
# Les variables A à D
# --------------------------------------------------------------------------- #

def _signe(x: float) -> int:
    return (x > 0) - (x < 0)


def _mediane(v: Sequence[float]) -> float | None:
    v = sorted(v)
    if not v:
        return None
    k = len(v) // 2
    return v[k] if len(v) % 2 else (v[k - 1] + v[k]) / 2


def _vitesse(t: Sequence[int], z: Sequence[float]) -> float | None:
    """(z_n − z_1) par minute ; manquante sous deux ticks ou sans durée."""
    if len(t) < 2 or t[-1] == t[0]:
        return None
    return (z[-1] - z[0]) / ((t[-1] - t[0]) / 60_000)


def _vol(dp: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in dp))


def variables(ticks: Sequence[tuple[int, float]], ctx: Contexte,
              w_sec: int, k_sec: int = 0) -> dict[str, float | None]:
    """Les 23 variables d'une sous-fenêtre. `ticks` : la sortie de
    `ticks_de_la_fenetre` pour les MÊMES w et k (le contrôle d'instant est
    refait ici : un tick hors de [d − w, d − k) est une erreur d'appel)."""
    debut = (ctx.d_sec - w_sec) * 1000
    fin = (ctx.d_sec - k_sec) * 1000
    if any(not debut <= ts < fin for ts, _ in ticks):
        raise ValueError("tick hors de la fenêtre [d − w, d − k)")
    v: dict[str, float | None] = {nom: None for nom in VARIABLES}
    n = len(ticks)
    if n == 0 or not ctx.delta or not ctx.marge:
        return v
    t = [ts for ts, _ in ticks]
    p = [x for _, x in ticks]
    z = [ctx.s * (x - ctx.niveau) / ctx.marge for x in p]
    dp = [(b - a) / ctx.delta for a, b in zip(p, p[1:])]
    dans = [abs(x) <= 1 for x in z]
    # Chaque tick vaut jusqu'au suivant ; le dernier, jusqu'à la coupure.
    durees = [b - a for a, b in zip(t, t[1:] + [fin])]

    # A — approche de la zone
    v["A1_dist_fin"] = z[-1]
    v["A2_dist_debut"] = z[0]
    v["A3_vitesse"] = _vitesse(t, z)
    milieu = (debut + fin) / 2
    gauche = [i for i in range(n) if t[i] < milieu]
    droite = [i for i in range(n) if t[i] >= milieu]
    va = _vitesse([t[i] for i in gauche], [z[i] for i in gauche])
    vb = _vitesse([t[i] for i in droite], [z[i] for i in droite])
    v["A4_acceleration"] = None if va is None or vb is None else vb - va
    v["A5_contacts"] = float(sum(1 for i in range(1, n)
                                 if dans[i] and not dans[i - 1]))
    v["A6_temps_zone"] = sum(d for d, x in zip(durees, dans) if x) / 1000
    v["A7_penetration"] = max(0.0, max(-x for x in z))
    contacts = [i for i in range(n) if dans[i]]
    if contacts:
        dernier = contacts[-1]
        v["A8_depuis_contact"] = (fin - t[dernier]) / 1000
        v["A9_apres_contact"] = z[-1] - z[dernier]

    # B — régime
    if n >= 2:
        absolus = [abs(x) for x in dp]
        somme = sum(absolus)
        vol_w = _vol(dp)
        v["B1_vol"] = vol_w
        v["B3_nulles"] = sum(1 for x in dp if x == 0) / len(dp)
        if somme:
            v["B4_efficience"] = abs(p[-1] - p[0]) / (ctx.delta * somme)
            plus_grands = sorted(absolus, reverse=True)[
                :math.ceil(PART_CONCENTRATION * len(absolus))]
            v["B6_concentration"] = sum(plus_grands) / somme
        if vol_w:
            # Les mouvements dont le tick d'arrivée tombe dans le dernier
            # cinquième de la fenêtre, rapportés à leur durée.
            cinquieme = fin - (fin - debut) / 5
            fin_dp = [x for i, x in enumerate(dp) if t[i + 1] >= cinquieme]
            duree = (fin - debut) / 1000
            v["B5_ratio_vol"] = ((_vol(fin_dp) / math.sqrt(duree / 5))
                                 / (vol_w / math.sqrt(duree)))
    v["B2_amplitude"] = (max(p) - min(p)) / ctx.delta

    # C — retournements, sur les mouvements non nuls
    if n >= 2:
        absolus = [abs(x) for x in dp]
        somme = sum(absolus)
        if somme:
            v["C3_part_sens"] = sum(max(0.0, ctx.s * x) for x in dp) / somme
        non_nuls = [(i, _signe(x)) for i, x in enumerate(dp) if x != 0]
        if len(non_nuls) >= 2:
            # Le tick où une suite s'arrête est un point de retournement :
            # c'est l'extrême local entre deux suites de signes opposés.
            points = [non_nuls[j][0] for j in range(1, len(non_nuls))
                      if non_nuls[j][1] != non_nuls[j - 1][1]]
            v["C1_retournements"] = float(len(points))
            v["C2_persistance"] = len(non_nuls) / (len(points) + 1)
            if len(points) >= 2:
                v["C4_amplitude_retournement"] = sum(
                    abs(p[b] - p[a]) for a, b in zip(points, points[1:])
                ) / ((len(points) - 1) * ctx.delta)
                v["C5_delai_retournement"] = _mediane(
                    [(t[b] - t[a]) / 1000 for a, b in zip(points, points[1:])])

    # D — interactions avec la zone
    signes = [_signe(x) for x in z if x != 0]
    v["D1_traversees"] = float(sum(1 for a, b in zip(signes, signes[1:])
                                   if a != b))
    sorties = [i for i in range(1, n) if dans[i - 1] and not dans[i]]
    v["D2_sorties_retours"] = float(sum(1 for i in sorties
                                        if any(dans[i:])))
    v["D3_temps_au_dela"] = sum(d for d, x in zip(durees, z) if x < -1) / 1000
    return v


# --------------------------------------------------------------------------- #
# Qualité (E), classe de fenêtre, niveau de disponibilité
# --------------------------------------------------------------------------- #

def qualite(par_minute: Mapping[int, Sequence[Tick]], d_sec: int, *,
            concordances: Mapping[int, str | None],
            fermees: Iterable[int] = (),
            doublees: Iterable[int] = ()) -> dict[str, float | int | str]:
    """La famille E sur [d − 900, d), en ticks BRUTS, plus la classe de la
    fenêtre et le niveau de disponibilité.

    `concordances` : minute → classe de concordance de l'audit (None : pas
    de bougie) ; `fermees` : minutes où la paire était fermée chez le broker
    (relevés de payouts antérieurs à d) ; `doublees` : minutes à flux
    doublé (audit)."""
    debut = d_sec - QUALITE_SEC
    minutes = list(range(debut, d_sec, 60))
    presentes = [m for m in minutes if par_minute.get(m)]
    bruts = ticks_de_la_fenetre(par_minute, d_sec, QUALITE_SEC,
                                traitement="bruts")
    uniques = len(set(bruts))
    instants = [debut * 1000] + [ts for ts, _ in bruts] + [d_sec * 1000]
    ecarts = [b - a for a, b in zip(instants, instants[1:])]
    fermees_ici = sorted(set(fermees) & set(minutes))
    sortie: dict[str, float | int | str] = {
        "E1_couverture": len(presentes) / len(minutes),
        "E2_ecart_max_s": max(ecarts) / 1000,
        "E3_age_dernier_s": (ecarts[-1] / 1000 if bruts else QUALITE_SEC),
        "E4_part_doublons": (1 - uniques / len(bruts)) if bruts else 0.0,
        "E5_gels": sum(1 for e in ecarts if e >= VALID_ECART_SEC * 1000),
        "E6_minutes_fermees": len(fermees_ici),
        "E7_minutes_doublees": len(set(doublees) & set(minutes)),
    }
    sortie["E8_niveau"] = niveau(minutes, par_minute, concordances)
    sortie["classe"] = classe_de_fenetre(sortie)
    return sortie


def classe_de_fenetre(q: Mapping[str, float | int | str]) -> str:
    """VALID : couverture totale, aucun écart ≥ 10 s, dernier tick à ≤ 5 s
    de d, aucune minute fermée chez le broker. PARTIAL : couverture ≥ 80 %
    et aucun écart ≥ 30 s. INVALID sinon. Aucune interpolation."""
    if (q["E1_couverture"] == 1.0 and q["E2_ecart_max_s"] < VALID_ECART_SEC
            and q["E3_age_dernier_s"] <= VALID_AGE_SEC
            and not q["E6_minutes_fermees"]):
        return VALID
    if (q["E1_couverture"] >= PARTIAL_COUVERTURE
            and q["E2_ecart_max_s"] < PARTIAL_ECART_SEC):
        return PARTIAL
    return INVALID


def niveau(minutes: Sequence[int], par_minute: Mapping[int, Sequence[Tick]],
           concordances: Mapping[int, str | None]) -> int:
    """2 si TOUTES les minutes ont des ticks et une concordance qui garde le
    même ensemble de ticks que la bougie lue par la stratégie ; 1 sinon."""
    for m in minutes:
        if not par_minute.get(m):
            return 1
        if concordances.get(m) not in CONCORDANCES_NIVEAU_2:
            return 1
    return 2


def ligne_du_signal(par_minute: Mapping[int, Sequence[Tick]], ctx: Contexte,
                    *, concordances: Mapping[int, str | None],
                    fermees: Iterable[int] = (),
                    doublees: Iterable[int] = (),
                    fenetres: Sequence[int] = FENETRES_SEC,
                    troncatures: Sequence[int] = TRONCATURES_SEC,
                    traitements: Sequence[str] = TRAITEMENTS
                    ) -> tuple[dict, list[dict]]:
    """(qualité du signal, une ligne de variables par traitement × k ×
    w)."""
    q = qualite(par_minute, ctx.d_sec, concordances=concordances,
                fermees=fermees, doublees=doublees)
    lignes = []
    for traitement in traitements:
        for k in troncatures:
            for w in fenetres:
                ticks = ticks_de_la_fenetre(par_minute, ctx.d_sec, w,
                                            k_sec=k, traitement=traitement)
                lignes.append({"traitement": traitement, "k": k, "w": w,
                               "n": len(ticks),
                               **variables(ticks, ctx, w, k)})
    return q, lignes


def autotest_coupure(par_minute: Mapping[int, Sequence[Tick]], ctx: Contexte,
                     paire: str, w_sec: int = QUALITE_SEC,
                     k_sec: int = 0) -> bool:
    """Le contrôle anti-fuite, sur une VRAIE fenêtre : des ticks ajoutés à
    partir de d − k (dont un pile à la coupure et un très loin du niveau)
    ne changent aucune variable."""
    avant = variables(ticks_de_la_fenetre(par_minute, ctx.d_sec, w_sec,
                                          k_sec=k_sec), ctx, w_sec, k_sec)
    coupure_ms = (ctx.d_sec - k_sec) * 1000
    pollue = {m: list(t) for m, t in par_minute.items()}
    loin = ctx.niveau * (1 + 50 * ctx.tolerance_pct / 100)
    for decalage, prix in ((0, loin), (1, ctx.niveau), (999, loin)):
        ts = coupure_ms + decalage
        pollue.setdefault(ts // 60_000 * 60, []).append(Tick(paire, ts, prix))
    apres = variables(ticks_de_la_fenetre(pollue, ctx.d_sec, w_sec,
                                          k_sec=k_sec), ctx, w_sec, k_sec)
    return avant == apres
