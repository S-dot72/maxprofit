"""
/reference8 — mission Reference 8 (2026-10-18) : audit de l'entrée,
validation des filtres gelés et préparation du moteur d'échéance.

--- Les trois questions, dans l'ordre -----------------------------------

A. La confirmation M1 améliore-t-elle ZoneH1, ou la fait-elle entrer trop
   tard ? Sur les MÊMES signaux : entrée immédiate (clôture de la bougie du
   signal) et entrée confirmée (clôture de la bougie qui casse son extrême
   dans le sens du trade). Table croisée, écarts de distance à la zone,
   MFE/MAE, B/C/D au signal et à l'entrée, et la part de l'écart qui vient
   de la SÉLECTION (les signaux confirmés valaient-ils mieux, entrés au
   signal ?) et celle qui vient du RETARD (les mêmes, entrés plus tard).
B. B (taille de la bougie d'entrée) et C (part du chemin parcourue)
   apportent-ils une information indépendante, et E (B + C + D) améliore-
   t-il le compromis qualité / volume ? M0, M1 = M0 + B, M2 = M0 + C,
   M3 = M0 + E ; D seul en secondaire, et P2-E (C + D) gardée telle que
   pré-inscrite. Seuils gelés (`live.preinscriptions`), mêmes signaux.
C. Les caractéristiques connues à l'entrée distinguent-elles les mouvements
   rapides, lents et invalidés, et permettent-elles de choisir 4, 10 ou
   15 min ? Échéances comparées signal par signal ; temps avant le premier
   mouvement d'une amplitude, pour et contre ; une sélection d'échéance
   apprise sur le passé et jugée sur une période qu'elle n'a pas vue.

Avant tout : les populations, la disponibilité de chaque variable et la
couverture des données (paire, jour, heure, paire-heures exploitables). Le
rapport de cohérence (définitions, divergences entre rapports) est
`docs/reference8_audit.md`.

--- Les règles du jeu ------------------------------------------------------

- Rien ne change dans la stratégie ni dans la course : des mesures.
- Seuils gelés : B 1,31, C 0,46, D 1,95. Le champ libre (D = 99) n'est
  jamais écarté ; une valeur manquante n'écarte rien.
- Les données qui ont inspiré un seuil ne le valident pas. Sur l'historique,
  un filtre ou une échéance favorable reste « indéterminée » ; défavorable
  même là, il est « non soutenu ». « Soutenue » ne vient, pour eux, que de
  la fenêtre ouverte le 19/10, close au volume calculé d'avance.
- Les classes de trajectoire (rapide, lente, invalidée) sont des ISSUES :
  décrites, jamais des filtres. Une association n'est pas une cause.
- Intervalles à 95 % par blocs paire × jour (1 000 tirages, graine 7).
- L'espérance suppose un payout de 92 % à toutes les échéances : à
  recalculer si le payout réel varie selon l'échéance ou la paire.

⚠ Des mesures, pas des règles : la course ne lit rien d'ici.
"""

from __future__ import annotations

import math
import random
import statistics
from typing import Callable, Mapping, Sequence

from maxprofit.apprentissage.contexte import CHAMP_LIBRE
from maxprofit.apprentissage.lecons import Exemple, wilson
from maxprofit.live.experiences import _compte
from maxprofit.live.laboratoire import (
    N_MIN_VALIDATION, PART_ETALONNAGE, confirmes)
from maxprofit.live.preinscriptions import (
    EXCLUSION_B, EXCLUSION_C, EXCLUSION_D, EXCLUSION_DISTANCE, MODELES_R8,
    OUVERTURE_R8, motifs_de_rejet, ouverture_r8, retenue)
from maxprofit.live.validation_p2 import (
    GAINS_PROPOSES, _criteres, _ecart, _effet_de_plan, _intervalles,
    _robustesse, conclusion, esperance, gain_minimal, volume_requis)

ECHEANCE = 900
#: Les trois horizons de la mission, et la minute en témoin.
HORIZONS: tuple[int, ...] = (240, 600, 900)
TEMOIN = 60
#: Les comparaisons d'échéances, appariées : (a, b) lu « a moins b ».
COMPARAISONS: tuple[tuple[int, int], ...] = ((240, 900), (600, 900),
                                             (240, 600))
TIRAGES = 1000
GRAINE = 7
SEMAINE = 7 * 86400
#: Une paire-heure est EXPLOITABLE si au moins 54 de ses 60 minutes ont
#: une bougie complète. Fixé avant lecture.
HEURE_EXPLOITABLE = 54
#: Couverture de la paire-journée d'une entrée : élevée, moyenne, faible.
COUVERTURE_ELEVEE = 0.8
COUVERTURE_FAIBLE = 0.5
#: Entrées minimales d'une strate pour que la règle d'échéance s'en écarte.
N_MIN_STRATE = 30
#: Entrées de la fenêtre avant un verdict sur la sélection d'échéance.
N_MIN_ECHEANCE = 100
#: Les autres lectures des mêmes terciles, pour la sensibilité.
AUTRES_LECTURES = (("taille_bougie", 1.27, 1.31, "B entre 1,27 et 1,31"),
                   ("espace_obstacle", 1.94, 1.95, "D entre 1,94 et 1,95"))

#: (lettre, clé à l'entrée, exclusion gelée)
VARIABLES: tuple[tuple[str, str, tuple], ...] = (
    ("B", "taille_bougie", EXCLUSION_B),
    ("C", "part_parcourue", EXCLUSION_C),
    ("D", "espace_obstacle", EXCLUSION_D),
)
#: Ce que l'on compare entre gagnées, perdues et classes de trajectoire —
#: toutes connues à l'entrée.
PREALABLES: tuple[tuple[str, str], ...] = (
    ("taille_bougie", "B taille bougie"),
    ("part_parcourue", "C part parcourue"),
    ("espace_obstacle", "D place (hors 99)"),
    ("distance_niveau", "distance zone"),
    ("depuis_contact", "depuis contact"),
    ("percees_zone", "percées zone 1 h"),
    ("rebond_15", "rebond 15 min"),
    ("volatilite_relative", "volatilité rel."),
)
CLASSES: tuple[tuple[str, str], ...] = (
    ("rapide", "+1 en 4 min au plus, avant −1"),
    ("lente", "+1 entre 5 et 15 min, avant −1"),
    ("invalidee", "−1 d'abord"),
    ("meme_minute", "+1 et −1 dans la même minute"),
    ("sans_mouvement", "ni +1 ni −1 en 15 min"),
)
STRATES: tuple[str, ...] = ("C", "D", "B", "reste")
#: « 19/10 », pour les textes.
JOUR_R8 = f"{OUVERTURE_R8[8:]}/{OUVERTURE_R8[5:7]}"


# --------------------------------------------------------------------------- #
# Petits calculs
# --------------------------------------------------------------------------- #

def _ev(n: int, g: int) -> float | None:
    return round(esperance(g / n), 4) if n else None


def _rond(x: float | None, k: int = 3) -> float | None:
    return None if x is None else round(x, k)


def _quartiles(valeurs: Sequence[float]) -> list[float] | None:
    if not valeurs:
        return None
    v = sorted(valeurs)
    return [round(v[len(v) // 4], 3), round(statistics.median(v), 3),
            round(v[(3 * len(v)) // 4], 3)]


def _mediane(valeurs: Sequence[float]) -> float | None:
    return round(statistics.median(valeurs), 3) if valeurs else None


def _bloc(e: Exemple) -> tuple[str, int]:
    return e.pair, e.ts_sec // 86400


def _bootstrap(blocs: Mapping[tuple, Sequence[float]],
               f: Callable[[list[float]], float | None]
               ) -> list[float] | None:
    """Intervalle à 95 % de `f(sommes)`, en rééchantillonnant les blocs
    paire × jour : chaque bloc porte ses compteurs, `f` les lit sommés."""
    valeurs = list(blocs.values())
    if len(valeurs) < 5:
        return None
    hasard = random.Random(GRAINE)
    sorties = []
    for _ in range(TIRAGES):
        tirage = hasard.choices(valeurs, k=len(valeurs))
        x = f([sum(col) for col in zip(*tirage)])
        if x is not None:
            sorties.append(x)
    if len(sorties) < TIRAGES // 2:
        return None
    sorties.sort()
    return [round(sorties[int(0.025 * len(sorties))], 4),
            round(sorties[int(0.975 * len(sorties)) - 1], 4)]


def _ecart_ev(n_a: float, g_a: float, n_b: float, g_b: float
              ) -> float | None:
    """Espérance de b moins espérance de a."""
    if not n_a or not n_b:
        return None
    return esperance(g_b / n_b) - esperance(g_a / n_a)


def _mcnemar(b: int, c: int) -> float | None:
    """p bilatéral exact du test de McNemar sur les paires discordantes."""
    n = b + c
    if not n:
        return None
    k = min(b, c)
    queue = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, 2 * queue), 4)


def _issue(e: Exemple, sec: int) -> bool | None:
    """L'issue de l'entrée confirmée à `sec` secondes."""
    return e.gagne if sec == ECHEANCE else e.issues.get(sec)


def _ecartee(valeur: float | None, exclusion: Sequence) -> bool:
    cle = exclusion[0]
    return bool(motifs_de_rejet({cle: valeur}, [exclusion]))


def strate(b: float | None, c: float | None, d: float | None) -> str:
    """La strate de la sélection d'échéance, dans l'ordre pré-inscrit."""
    if _ecartee(c, EXCLUSION_C):
        return "C"
    if _ecartee(d, EXCLUSION_D):
        return "D"
    if _ecartee(b, EXCLUSION_B):
        return "B"
    return "reste"


def _verdict(ecart: float | None, ic: Sequence[float] | None, n: int, *,
             n_min: int = N_MIN_VALIDATION, concu: bool = False,
             sens: int = 1) -> str:
    """soutenue / non soutenue / indéterminée, pour un écart attendu du
    signe de `sens`. `concu` : les données ont servi à concevoir la règle —
    elles peuvent la réfuter, pas la confirmer."""
    if ecart is None or n < n_min:
        return f"≈ indéterminée — {n} observations, il en faut {n_min}"
    if ecart * sens <= 0:
        return "❌ non soutenue — l'écart va dans l'autre sens"
    if concu:
        return ("≈ indéterminée — favorable sur les données qui ont "
                "inspiré la règle : à juger sur la fenêtre")
    if ic and (ic[0] > 0 if sens > 0 else ic[1] < 0):
        return "✅ soutenue — intervalle par blocs entièrement du bon côté"
    return "≈ indéterminée — l'intervalle par blocs touche 0"


# --------------------------------------------------------------------------- #
# Phase 0 — populations et disponibilité des variables
# --------------------------------------------------------------------------- #

def _confirme(e: Exemple, fenetre: int) -> bool:
    """Le signal a-t-il sa confirmation, comme `laboratoire.confirmes` ?"""
    if fenetre == 1:
        return e.contexte.get("retournement_suivant") == 1
    return 1 <= int(e.contexte.get("attente_confirmation", 0) or 0) <= fenetre


def _audit(exemples: Sequence[Exemple], signaux: Sequence[Exemple],
           base: Sequence[Exemple], en_direct: Callable[[Exemple], bool],
           fenetre: int,
           exclusions: Mapping[str, int] | None) -> dict:
    confirmes_signal = [e for e in signaux if _confirme(e, fenetre)]
    # Les deux règles de l'heure : au signal (l'entrée immédiate décide là)
    # et à l'entrée (la course décide après la confirmation). Chaque entrée
    # confirmée rejouée, rattachée à son signal.
    def signal_de(e: Exemple) -> tuple[str, int]:
        k = (1 if fenetre == 1
             else int(e.contexte.get("attente_confirmation", 1) or 1))
        return e.pair, e.ts_sec - 60 * k
    entrees = {signal_de(e): en_direct(e) for e in confirmes(exemples,
                                                              fenetre)}
    ids_signal = {(e.pair, e.ts_sec) for e in signaux}
    perdus_a_l_entree = sum(1 for e in confirmes_signal
                            if entrees.get((e.pair, e.ts_sec)) is False)
    gagnes_a_l_entree = sum(1 for cle, joue in entrees.items()
                            if joue and cle not in ids_signal)
    n = len(base)
    dispo = {}
    for lettre, cle, _excl in VARIABLES:
        dispo[lettre] = sum(1 for e in base if cle in e.contexte)
    libres = sum(1 for e in base
                 if e.contexte.get("espace_obstacle") == CHAMP_LIBRE)
    sans_c = [e for e in base if "part_parcourue" not in e.contexte]
    raisons_c = {
        "aucun obstacle confirmé devant (D = 99)": sum(
            1 for e in sans_c
            if e.contexte.get("espace_obstacle") == CHAMP_LIBRE),
        "pas de niveau de zone": sum(
            1 for e in sans_c if "distance_niveau" not in e.contexte)}
    raisons_c["obstacle en deçà de la zone"] = (
        len(sans_c) - sum(raisons_c.values()))
    bandes = {libelle: sum(1 for e in base
                           if bas <= e.contexte.get(cle, -1e9) < haut)
              for cle, bas, haut, libelle in AUTRES_LECTURES}
    attentes: dict[str, int] = {}
    for e in base:
        k = str(int(e.contexte.get("attente_confirmation", 0) or 0))
        attentes[k] = attentes.get(k, 0) + 1
    return {
        "signaux_zoneh1": len(exemples),
        "joues_au_signal": len(signaux),
        "confirmes_au_signal": len(confirmes_signal),
        "base": n, "fenetre": fenetre,
        "confirmes_ecartes_a_l_entree": perdus_a_l_entree,
        "joues_a_l_entree_seulement": gagnes_a_l_entree,
        "attentes": attentes,
        "disponibles": dispo, "champ_libre": libres,
        "sans_c": len(sans_c), "raisons_sans_c": raisons_c,
        "autres_lectures": bandes,
        "suivi_incomplet": sum(1 for e in base if "mfe_15" not in e.contexte),
        "temps_absents": sum(1 for e in base if "mfe_15" in e.contexte
                             and "t_pour" not in e.contexte),
        "sans_issue": {str(s // 60): sum(1 for e in base
                                         if _issue(e, s) is None)
                       for s in (TEMOIN, *HORIZONS)},
        "douteuses": sum(1 for e in base
                         if e.contexte.get("donnees_douteuses")),
        "exclusions_rejeu": dict(exclusions or {}),
    }


# --------------------------------------------------------------------------- #
# Étape 5 — couverture des données
# --------------------------------------------------------------------------- #

def _couverture(qualite, signaux: Sequence[Exemple],
                base: Sequence[Exemple], debut: int, fin: int) -> dict:
    """Paire, jour, heure ; paire-heures exploitables ; sensibilité."""
    heures = qualite.minutes_par_paire_heure() if qualite is not None else {}
    if not heures or fin <= debut:
        return {"note": "couverture non mesurée (qualité absente)"}
    h0, h1 = debut // 3600, fin // 3600
    total = max(1, h1 - h0)
    par_paire, exploitables = {}, set()
    for paire, hs in heures.items():
        minutes = sum(v for h, v in hs.items() if h0 <= h < h1)
        bonnes = {h for h, v in hs.items()
                  if h0 <= h < h1 and v >= HEURE_EXPLOITABLE}
        exploitables |= {(paire, h) for h in bonnes}
        par_paire[paire] = {"couverture": round(minutes / (60 * total), 3),
                            "heures": len(bonnes), "signaux": 0}
    dans, hors = 0, 0
    par_heure = {h: [0, 0, 0] for h in range(24)}    # minutes, heures, sig.
    for paire, hs in heures.items():
        for h, v in hs.items():
            if h0 <= h < h1:
                par_heure[h % 24][0] += v
                par_heure[h % 24][1] += int(v >= HEURE_EXPLOITABLE)
    for e in signaux:
        h = (e.ts_sec - 60) // 3600         # la minute du signal
        if (e.pair, h) in exploitables:
            dans += 1
            par_heure[h % 24][2] += 1
        else:
            hors += 1
        if e.pair in par_paire:
            par_paire[e.pair]["signaux"] += 1
    jours = max(1, total // 24)
    paires_suivies = max(1, len(heures))
    # La couverture de la paire-journée de chaque entrée jouée.
    par_jour: dict[tuple[str, int], int] = {}
    for paire, hs in heures.items():
        for h, v in hs.items():
            par_jour[(paire, h // 24)] = par_jour.get((paire, h // 24), 0) + v
    classes = {"élevée": [0, 0], "moyenne": [0, 0], "faible": [0, 0]}
    blocs: dict[tuple, list[int]] = {}
    for e in base:
        part = par_jour.get((e.pair, (e.ts_sec - 60) // 86400), 0) / 1440
        nom = ("élevée" if part >= COUVERTURE_ELEVEE else
               "faible" if part < COUVERTURE_FAIBLE else "moyenne")
        classes[nom][0] += 1
        classes[nom][1] += int(e.gagne)
        if nom != "moyenne":
            b = blocs.setdefault(_bloc(e), [0, 0, 0, 0])
            k = 0 if nom == "faible" else 2
            b[k] += 1
            b[k + 1] += int(e.gagne)
    lignes_classes = {k: {"n": n, "g": g, "ev": _ev(n, g)}
                      for k, (n, g) in classes.items()}
    hi, lo = classes["élevée"], classes["faible"]
    jours_couverture = [round(m / 1440 / paires_suivies, 3) for m in (
        sum(v for (p, j), v in par_jour.items() if j == jour)
        for jour in sorted({j for _p, j in par_jour}))]
    return {
        "paires": par_paire,
        "heures_calendrier": total,
        "paire_heures_exploitables": len(exploitables),
        "signaux_exploitables": dans, "signaux_hors": hors,
        "signaux_par_paire_heure": (round(dans / len(exploitables), 3)
                                    if exploitables else None),
        "par_heure": {str(h): {
            "couverture": round(m / (60 * jours * paires_suivies), 3),
            "exploitables": x, "signaux": s,
            "par_paire_heure": round(s / x, 3) if x else None}
            for h, (m, x, s) in par_heure.items()},
        "jours": _quartiles(jours_couverture),
        "jours_sous_moitie": sum(1 for x in jours_couverture if x < 0.5),
        "nombre_jours": len(jours_couverture),
        "sensibilite": lignes_classes,
        "ecart_elevee_faible": _rond(_ecart_ev(lo[0], lo[1], hi[0], hi[1]),
                                     4),
        "ic_ecart": _bootstrap(blocs, lambda s: _ecart_ev(*s)),
    }


# --------------------------------------------------------------------------- #
# Livrable A — entrée immédiate ou confirmée
# --------------------------------------------------------------------------- #

def _livrable_a(signaux: Sequence[Exemple], fenetre: int) -> dict:
    conf = [e for e in signaux if _confirme(e, fenetre)]
    non = [e for e in signaux if not _confirme(e, fenetre)]
    horizons = []
    for sec in (TEMOIN, *HORIZONS):
        cle = f"gagne_confirme_{sec}"
        tous = [e for e in signaux if e.issues.get(sec) is not None]
        paires = [(e, bool(e.issues[sec]), bool(e.contexte[cle]))
                  for e in conf
                  if e.issues.get(sec) is not None and cle in e.contexte]
        non_c = [e for e in non if e.issues.get(sec) is not None]
        table = [sum(1 for _e, i, c in paires if i and c),
                 sum(1 for _e, i, c in paires if i and not c),
                 sum(1 for _e, i, c in paires if not i and c),
                 sum(1 for _e, i, c in paires if not i and not c)]
        n = len(paires)
        g_imm, g_conf = table[0] + table[1], table[0] + table[2]
        g_tous = sum(int(e.issues[sec]) for e in tous)
        # [n tous, g tous, n appariés, g immédiate, g confirmée]
        blocs: dict[tuple, list[int]] = {}
        for e in tous:
            b = blocs.setdefault(_bloc(e), [0, 0, 0, 0, 0])
            b[0] += 1
            b[1] += int(e.issues[sec])
        for e, i, c in paires:
            b = blocs.setdefault(_bloc(e), [0, 0, 0, 0, 0])
            b[2] += 1
            b[3] += int(i)
            b[4] += int(c)
        selection = _ecart_ev(len(tous), g_tous, n, g_imm)
        retard = _ecart_ev(n, g_imm, n, g_conf)
        total = _ecart_ev(len(tous), g_tous, n, g_conf)
        horizons.append({
            "sec": sec, "table": table, "n": n,
            "immediate": _ev(n, g_imm), "confirmee": _ev(n, g_conf),
            "tous": [len(tous), g_tous],
            "non_confirmes": [len(non_c),
                              sum(int(e.issues[sec]) for e in non_c)],
            "mcnemar": _mcnemar(table[1], table[2]),
            "selection": _rond(selection, 4),
            "ic_selection": _bootstrap(
                blocs, lambda s: _ecart_ev(s[0], s[1], s[2], s[3])),
            "retard": _rond(retard, 4),
            "ic_retard": _bootstrap(
                blocs, lambda s: _ecart_ev(s[2], s[3], s[2], s[4])),
            "total": _rond(total, 4),
            "ic_total": _bootstrap(
                blocs, lambda s: _ecart_ev(s[0], s[1], s[2], s[4])),
        })
    # Le témoin du retard PUR : entrer une minute plus tard, sans attendre
    # de confirmation (la bougie suivante, quelle qu'elle soit).
    suivant = [(e, bool(e.gagne), bool(e.contexte["gagne_suivant"]))
               for e in signaux if "gagne_suivant" in e.contexte]
    blocs_s: dict[tuple, list[int]] = {}
    for e, i, s in suivant:
        b = blocs_s.setdefault(_bloc(e), [0, 0, 0])
        b[0] += 1
        b[1] += int(i)
        b[2] += int(s)
    n_s = len(suivant)
    par_attente = {}
    for e in conf:
        if "gagne_confirme" not in e.contexte:
            continue
        k = str(int(e.contexte.get("attente_confirmation", 0) or 0))
        x = par_attente.setdefault(k, [0, 0, 0])
        x[0] += 1
        x[1] += int(e.gagne)
        x[2] += int(e.contexte["gagne_confirme"])
    # Le prix d'entrée : la distance à la zone au signal et à l'entrée,
    # en amplitudes M1 — leur écart EST le déplacement du prix d'entrée.
    distances = [(e.contexte["distance_niveau"],
                  e.contexte["e_distance_niveau"]) for e in conf
                 if "distance_niveau" in e.contexte
                 and "e_distance_niveau" in e.contexte]
    blocs_d: dict[tuple, list[float]] = {}
    for e in conf:
        if "distance_niveau" in e.contexte and \
                "e_distance_niveau" in e.contexte:
            b = blocs_d.setdefault(_bloc(e), [0.0, 0.0])
            b[0] += 1
            b[1] += (e.contexte["e_distance_niveau"]
                     - e.contexte["distance_niveau"])
    ecarts = [b - a for a, b in distances]
    # M4 : combien d'entrées le retard pousse-t-il au-delà de 1,55 ?
    entrent_distance = sum(1 for a, b in distances
                           if not _ecartee(a, EXCLUSION_DISTANCE)
                           and _ecartee(b, EXCLUSION_DISTANCE))
    excursions = {}
    for nom in ("mfe_15", "mae_15", "t_mfe"):
        couples = [(e.contexte[f"imm_{nom}"], e.contexte[nom]) for e in conf
                   if f"imm_{nom}" in e.contexte and nom in e.contexte]
        excursions[nom] = {
            "n": len(couples),
            "immediate": _quartiles([a for a, _b in couples]),
            "confirmee": _quartiles([b for _a, b in couples]),
            "ecart": _quartiles([b - a for a, b in couples])}
    cassees = [(e.contexte["imm_zone_cassee"], e.contexte["zone_cassee"])
               for e in conf if "imm_zone_cassee" in e.contexte
               and "zone_cassee" in e.contexte]
    variables = []
    for lettre, cle, excl in VARIABLES:
        couples = [(e.contexte[cle], e.contexte[f"e_{cle}"]) for e in conf
                   if cle in e.contexte and f"e_{cle}" in e.contexte]
        hors_libre = [(a, b) for a, b in couples
                      if a != CHAMP_LIBRE and b != CHAMP_LIBRE]
        variables.append({
            "lettre": lettre, "n": len(couples),
            "signal": _mediane([a for a, _b in hors_libre]),
            "entree": _mediane([b for _a, b in hors_libre]),
            "entrent": sum(1 for a, b in couples
                           if not _ecartee(a, excl) and _ecartee(b, excl)),
            "sortent": sum(1 for a, b in couples
                           if _ecartee(a, excl) and not _ecartee(b, excl))})
    return {
        "signaux": len(signaux), "confirmes": len(conf),
        "horizons": horizons,
        "suivant": {"n": n_s,
                    "immediate": _ev(n_s, sum(i for _e, i, _s in suivant)),
                    "suivante": _ev(n_s, sum(s for _e, _i, s in suivant)),
                    "ic": _bootstrap(
                        blocs_s, lambda s: _ecart_ev(s[0], s[1], s[0], s[2]))},
        "par_attente": par_attente,
        "distance": {"n": len(distances),
                     "signal": _quartiles([a for a, _b in distances]),
                     "entree": _quartiles([b for _a, b in distances]),
                     "ecart": _quartiles(ecarts),
                     "plus_loin": sum(1 for x in ecarts if x > 0),
                     "entrent_m4": entrent_distance,
                     "moyenne": (round(sum(ecarts) / len(ecarts), 3)
                                 if ecarts else None),
                     "ic_moyenne": _bootstrap(
                         blocs_d, lambda s: s[1] / s[0] if s[0] else None)},
        "excursions": excursions,
        "zone_cassee": {"n": len(cassees),
                        "immediate": sum(int(a) for a, _b in cassees),
                        "confirmee": sum(int(b) for _a, b in cassees)},
        "variables": variables,
    }


# --------------------------------------------------------------------------- #
# Livrable B — M0, M1, M2, M3 (+ D, + C+D) sur les mêmes entrées
# --------------------------------------------------------------------------- #

def _blocs_modele(liste: Sequence, garde: Callable, cle: Callable,
                  gagne: Callable) -> dict[tuple, list[int]]:
    """{(paire, jour): [n réf., g réf., n gardées, g gardées]}."""
    blocs: dict[tuple, list[int]] = {}
    for x in liste:
        b = blocs.setdefault(cle(x), [0, 0, 0, 0])
        b[0] += 1
        b[1] += int(gagne(x))
        if garde(x):
            b[2] += 1
            b[3] += int(gagne(x))
    return blocs


def _livrable_b(base: Sequence[Exemple], coupure: int, debut: int,
                jours: float) -> dict:
    lignes = []
    for ident, libelle, exclusions in MODELES_R8:
        garde = [e for e in base if retenue(e, exclusions)]
        n, g = len(garde), sum(e.gagne for e in garde)
        blocs = _blocs_modele(base, lambda e, x=exclusions: retenue(e, x),
                              _bloc, lambda e: e.gagne)
        ic, ic_ecart = _intervalles(blocs)
        semaines: dict[int, list[int]] = {}
        paires: dict[str, list[int]] = {}
        for e in garde:
            s = semaines.setdefault((e.ts_sec - debut) // SEMAINE, [0, 0])
            s[0] += 1
            s[1] += int(e.gagne)
            p = paires.setdefault(e.pair, [0, 0])
            p[0] += 1
            p[1] += int(e.gagne)
        ligne = {"id": ident, "libelle": libelle, "n": n, "g": g,
                 "exclus": len(base) - n, "ev": _ev(n, g),
                 "wilson": [round(x, 4) for x in wilson(g, n, 1.96)]
                 if n else None,
                 "ic": ic, "periodes": _compte(garde, coupure),
                 "par_jour": round(n / jours, 2) if jours else None,
                 "semaines": [semaines.get(k, [0, 0])
                              for k in range(max(semaines) + 1)]
                 if semaines else [],
                 "paires": paires}
        if exclusions:
            ligne["ecart"] = _rond(_ecart(blocs.values()), 4)
            ligne["ic_ecart"] = ic_ecart
            ligne.update(_robustesse(blocs))
        lignes.append(ligne)
    # Le chevauchement des exclusions.
    exclus = {lettre: {id(e) for e in base
                       if _ecartee(e.contexte.get(cle), excl)}
              for lettre, cle, excl in VARIABLES}
    b, c, d = exclus["B"], exclus["C"], exclus["D"]
    par_id = {id(e): e for e in base}

    def compte(ids):
        return [len(ids), sum(int(par_id[i].gagne) for i in ids)]
    chevauchement = {
        "B": len(b), "C": len(c), "D": len(d),
        "B∩C": len(b & c), "B∩D": len(b & d), "C∩D": len(c & d),
        "B∩C∩D": len(b & c & d),
        "C_aussi_D": _rond(len(c & d) / len(c), 3) if c else None,
        "D_aussi_C": _rond(len(c & d) / len(d), 3) if d else None,
        "seulement": {"B": compte(b - c - d), "C": compte(c - b - d),
                      "D": compte(d - b - c)},
        "aucune": compte(set(par_id) - b - c - d)}
    # La valeur PRÉDICTIVE de chaque variable : écartées contre gardées.
    predictive = []
    for lettre, cle, excl in VARIABLES:
        blocs: dict[tuple, list[int]] = {}
        for e in base:
            bl = blocs.setdefault(_bloc(e), [0, 0, 0, 0])
            k = 0 if id(e) in exclus[lettre] else 2
            bl[k] += 1
            bl[k + 1] += int(e.gagne)
        ec, ga = compte(exclus[lettre]), compte(set(par_id) - exclus[lettre])
        predictive.append({
            "lettre": lettre, "ecartees": ec, "gardees": ga,
            "ecart": _rond(_ecart_ev(ec[0], ec[1], ga[0], ga[1]), 4),
            "ic": _bootstrap(blocs, lambda s: _ecart_ev(*s))})
    # La valeur AJOUTÉE dans E : E contre E privé d'une variable.
    ajoutee = []
    toutes = (EXCLUSION_B, EXCLUSION_C, EXCLUSION_D)
    for lettre, _cle, excl in VARIABLES:
        autres = tuple(x for x in toutes if x is not excl)
        blocs = _blocs_modele(
            [e for e in base if retenue(e, autres)],
            lambda e: retenue(e, toutes), _bloc, lambda e: e.gagne)
        n_a = sum(v[0] for v in blocs.values())
        g_a = sum(v[1] for v in blocs.values())
        n_e = sum(v[2] for v in blocs.values())
        g_e = sum(v[3] for v in blocs.values())
        ajoutee.append({
            "lettre": lettre, "sans": [n_a, g_a], "avec": [n_e, g_e],
            "retirees": [n_a - n_e, g_a - g_e],
            "ecart": _rond(_ecart_ev(n_a, g_a, n_e, g_e), 4),
            "ic": _bootstrap(blocs, lambda s: _ecart_ev(*s))})
    # M4, la distance à la zone : sa valeur prédictive, puis ce qu'elle
    # apporte AU-DELÀ du filtre B (B + distance contre B seul).
    loin = {id(e) for e in base
            if _ecartee(e.contexte.get("distance_niveau"), EXCLUSION_DISTANCE)}
    blocs = {}
    for e in base:
        bl = blocs.setdefault(_bloc(e), [0, 0, 0, 0])
        k = 0 if id(e) in loin else 2
        bl[k] += 1
        bl[k + 1] += int(e.gagne)
    ec, ga = compte(loin), compte(set(par_id) - loin)
    distance = {"ecartees": ec, "gardees": ga,
                "ecart": _rond(_ecart_ev(ec[0], ec[1], ga[0], ga[1]), 4),
                "ic": _bootstrap(blocs, lambda s: _ecart_ev(*s)),
                "aussi_b": len(loin & b)}
    blocs = _blocs_modele(
        [e for e in base if retenue(e, (EXCLUSION_B,))],
        lambda e: retenue(e, (EXCLUSION_B, EXCLUSION_DISTANCE)), _bloc,
        lambda e: e.gagne)
    n_a = sum(v[0] for v in blocs.values())
    g_a = sum(v[1] for v in blocs.values())
    n_e = sum(v[2] for v in blocs.values())
    g_e = sum(v[3] for v in blocs.values())
    distance["au_dela_de_b"] = {
        "retirees": [n_a - n_e, g_a - g_e],
        "ecart": _rond(_ecart_ev(n_a, g_a, n_e, g_e), 4),
        "ic": _bootstrap(blocs, lambda s: _ecart_ev(*s))}
    return {"modeles": lignes, "chevauchement": chevauchement,
            "predictive": predictive, "ajoutee": ajoutee,
            "distance": distance}


# --------------------------------------------------------------------------- #
# Livrable C — échéances, trajectoires, sélection d'échéance
# --------------------------------------------------------------------------- #

def _classe(e: Exemple) -> str | None:
    """La classe de trajectoire — une ISSUE, lue après l'entrée."""
    if "t_pour" not in e.contexte:
        return None
    p, c = int(e.contexte["t_pour"]), int(e.contexte["t_contre"])
    if not p and not c:
        return "sans_mouvement"
    if p and c and p == c:
        return "meme_minute"
    if c and (not p or c < p):
        return "invalidee"
    return "rapide" if p <= 4 else "lente"


def _valeurs_bcd(e: Exemple) -> tuple:
    return tuple(e.contexte.get(cle) for _l, cle, _x in VARIABLES)


def apprendre_echeances(entrees: Sequence[tuple[str, Mapping[int, bool]]]
                        ) -> dict[str, int]:
    """La règle d'échéance : dans chaque strate d'au moins `N_MIN_STRATE`
    entrées, l'horizon au meilleur taux (à égalité, le plus long) ; sinon
    15 min. `entrees` : (strate, {horizon: gagné})."""
    regle = {}
    for s in STRATES:
        sous = [issues for strate, issues in entrees if strate == s]
        if len(sous) < N_MIN_STRATE:
            regle[s] = ECHEANCE
            continue
        regle[s] = max(HORIZONS, key=lambda h: (
            sum(int(x[h]) for x in sous), h))
    return regle


def _livrable_c(base: Sequence[Exemple], coupure: int) -> dict:
    apparies = [e for e in base
                if all(_issue(e, s) is not None for s in HORIZONS)]
    horizons = []
    for sec in (TEMOIN, *HORIZONS):
        liste = [e for e in base if _issue(e, sec) is not None]
        n, g = len(liste), sum(int(_issue(e, sec)) for e in liste)
        horizons.append({"sec": sec, "n": n, "g": g, "ev": _ev(n, g),
                         "periodes": _compte(liste, coupure,
                                             lambda e, s=sec: _issue(e, s))})
    comparaisons = []
    for a, b in COMPARAISONS:
        table = [0, 0, 0, 0]
        blocs: dict[tuple, list[int]] = {}
        for e in apparies:
            ia, ib = bool(_issue(e, a)), bool(_issue(e, b))
            table[(0 if ia else 2) + (0 if ib else 1)] += 1
            bl = blocs.setdefault(_bloc(e), [0, 0, 0])
            bl[0] += 1
            bl[1] += int(ib)
            bl[2] += int(ia)
        n = len(apparies)
        comparaisons.append({
            "a": a, "b": b, "table": table,
            "ecart": _rond(_ecart_ev(n, table[0] + table[2],
                                     n, table[0] + table[1]), 4),
            "mcnemar": _mcnemar(table[1], table[2]),
            "ic": _bootstrap(blocs,
                             lambda s: _ecart_ev(s[0], s[1], s[0], s[2]))})
    # Les trajectoires.
    suivies = [e for e in base if _classe(e) is not None]
    classes = []
    for nom, libelle in CLASSES:
        sous = [e for e in suivies if _classe(e) == nom]
        classes.append({
            "classe": nom, "libelle": libelle, "n": len(sous),
            "issues": {str(s // 60): [
                sum(1 for e in sous if _issue(e, s) is not None),
                sum(int(bool(_issue(e, s))) for e in sous)]
                for s in HORIZONS}})
    trajectoire = {
        "n": len(suivies),
        "mfe": _quartiles([e.contexte["mfe_15"] for e in suivies]),
        "mae": _quartiles([e.contexte["mae_15"] for e in suivies]),
        "t_pour": _quartiles([e.contexte["t_pour"] for e in suivies
                              if e.contexte["t_pour"]]),
        "t_contre": _quartiles([e.contexte["t_contre"] for e in suivies
                                if e.contexte["t_contre"]]),
        "nets": {str(m): _quartiles([e.contexte[f"net_{m}"] for e in suivies
                                     if f"net_{m}" in e.contexte])
                 for m in (4, 10, 15)},
        "zone_cassee": sum(int(e.contexte.get("zone_cassee", 0))
                           for e in suivies)}
    # Les caractéristiques d'AVANT l'entrée, selon l'issue.
    groupes = {
        "gagnées": [e for e in base if e.gagne],
        "perdues": [e for e in base if not e.gagne],
        "perdues, zone cassée": [e for e in base if not e.gagne
                                 and e.contexte.get("zone_cassee") == 1],
        **{nom: [e for e in suivies if _classe(e) == nom]
           for nom in ("rapide", "lente", "invalidee")}}
    prealables = {}
    for nom, liste in groupes.items():
        valeurs = {}
        for cle, _libelle in PREALABLES:
            v = [e.contexte[cle] for e in liste if cle in e.contexte
                 and e.contexte[cle] != CHAMP_LIBRE]
            valeurs[cle] = _mediane(v)
        valeurs["d_libre"] = sum(
            1 for e in liste
            if e.contexte.get("espace_obstacle") == CHAMP_LIBRE)
        prealables[nom] = {"n": len(liste), "medianes": valeurs}
    # B, C, D à l'entrée : l'échéance qui marche dépend-elle d'eux ?
    par_variable = []
    for lettre, cle, excl in VARIABLES:
        for cote in ("écartée", "gardée"):
            sous = [e for e in apparies
                    if _ecartee(e.contexte.get(cle), excl)
                    == (cote == "écartée")]
            par_variable.append({
                "lettre": lettre, "cote": cote, "n": len(sous),
                "g": {str(s // 60): sum(int(bool(_issue(e, s)))
                                        for e in sous) for s in HORIZONS}})
    # La sélection d'échéance, apprise sur l'étalonnage, jugée sur les
    # 30 % récents qu'elle n'a pas vus.
    entrees = [(strate(*_valeurs_bcd(e)),
                {s: bool(_issue(e, s)) for s in HORIZONS}, e)
               for e in apparies]
    regle = apprendre_echeances([(s, i) for s, i, e in entrees
                                 if e.ts_sec < coupure])
    blocs: dict[tuple, list[int]] = {}
    for s, issues, e in entrees:
        if e.ts_sec < coupure:
            continue
        bl = blocs.setdefault(_bloc(e), [0, 0, 0])
        bl[0] += 1
        bl[1] += int(issues[ECHEANCE])
        bl[2] += int(issues[regle[s]])
    n = sum(v[0] for v in blocs.values())
    g15 = sum(v[1] for v in blocs.values())
    ga = sum(v[2] for v in blocs.values())
    strates = {s: sum(1 for x, _i, _e in entrees if x == s) for s in STRATES}
    return {
        "apparies": len(apparies), "horizons": horizons,
        "comparaisons": comparaisons, "classes": classes,
        "trajectoire": trajectoire, "prealables": prealables,
        "par_variable": par_variable,
        "adaptatif": {"regle": {s: h // 60 for s, h in regle.items()},
                      "strates": strates, "n": n, "fixe": _ev(n, g15),
                      "adaptee": _ev(n, ga),
                      "ecart": _rond(_ecart_ev(n, g15, n, ga), 4),
                      "ic": _bootstrap(
                          blocs, lambda s: _ecart_ev(s[0], s[1], s[0], s[2]))},
    }


# --------------------------------------------------------------------------- #
# La fenêtre indépendante (ouverte le 19/10)
# --------------------------------------------------------------------------- #

def _v(ctx: Mapping, cle: str) -> float | None:
    x = ctx.get(cle)
    return None if x is None else round(float(x), 4)


def _i(x) -> int | None:
    return None if x is None else int(bool(x))


def fiche(e: Exemple) -> list:
    """[entrée (s), paire, gagné à 15 min, B, C, D, douteuses, gagné à 4 min,
    gagné à 10 min, distance à la zone] : de quoi rejuger tous les modèles
    sans rejouer."""
    return [int(e.ts_sec), e.pair, int(bool(e.gagne)),
            _v(e.contexte, "taille_bougie"), _v(e.contexte, "part_parcourue"),
            _v(e.contexte, "espace_obstacle"),
            int(bool(e.contexte.get("donnees_douteuses"))),
            _i(e.issues.get(240)), _i(e.issues.get(600)),
            _v(e.contexte, "distance_niveau")]


def _valeurs_fiche(f: Sequence) -> dict:
    return {"taille_bougie": f[3], "part_parcourue": f[4],
            "espace_obstacle": f[5],
            "distance_niveau": f[9] if len(f) > 9 else None}


def _cumuler(ancien: Sequence[Sequence], nouveau: Sequence[Sequence]
             ) -> list[list]:
    """Le registre cumulé : une entrée garde sa première fiche — sauf si
    la nouvelle porte plus de mesures (la distance, ajoutée le 18/10 au
    soir) : mêmes bougies, même entrée, rien d'autre ne change."""
    fiches: dict[tuple[str, int], list] = {}
    for f in ancien:
        fiches.setdefault((f[1], int(f[0])), list(f))
    for f in nouveau:
        cle = (f[1], int(f[0]))
        if cle not in fiches or len(f) > len(fiches[cle]):
            fiches[cle] = list(f)
    return sorted(fiches.values(), key=lambda f: (f[0], f[1]))


def _gardee(f: Sequence, exclusions) -> bool:
    return not motifs_de_rejet(_valeurs_fiche(f), exclusions)


def _issues_fiche(f: Sequence) -> dict[int, int | None]:
    return {240: f[7], 600: f[8], 900: f[2]}


def _planifier(avant: Sequence[Sequence]) -> dict:
    """Ce que les entrées d'AVANT la fenêtre disent : taux de M0, part
    gardée par chaque modèle, effet de plan. Jamais un seuil."""
    n = len(avant)
    plan: dict = {"n": n}
    if n < 20:
        plan["note"] = f"seulement {n} entrées avant la fenêtre"
        return plan
    plan["p"] = round(sum(f[2] for f in avant) / n, 4)
    plan["effet_plan"] = _effet_de_plan(avant)
    plan["parts"] = {ident: round(sum(1 for f in avant
                                      if _gardee(f, excl)) / n, 4)
                     for ident, _l, excl in MODELES_R8 if excl}
    entrees = []
    for f in avant:
        issues = _issues_fiche(f)
        if all(x is not None for x in issues.values()):
            entrees.append((strate(f[3], f[4], f[5]),
                            {h: bool(x) for h, x in issues.items()}))
    plan["regle"] = {s: h for s, h in apprendre_echeances(entrees).items()}
    plan["entrees_regle"] = len(entrees)
    return plan


def _volume(plan: Mapping, ident: str, gain: float | None) -> int | None:
    if gain is None or "p" not in plan:
        return None
    return volume_requis(plan["p"], plan["parts"][ident], gain,
                         plan.get("effet_plan", 1.0))


def _modele_fenetre(fiches: Sequence[Sequence], exclusions,
                    jours: float) -> dict:
    blocs = _blocs_modele(fiches, lambda f: _gardee(f, exclusions),
                          lambda f: (f[1], int(f[0]) // 86400),
                          lambda f: f[2])
    gardees = [f for f in fiches if _gardee(f, exclusions)]
    n, g = len(gardees), sum(f[2] for f in gardees)
    ic, ic_ecart = _intervalles(blocs)
    sortie = {"n": n, "g": g, "ev": _ev(n, g), "ic": ic,
              "par_jour": round(n / jours, 2) if jours > 0 else None,
              "part_conservee": (round(n / len(fiches), 4)
                                 if fiches else None),
              "ecart": _rond(_ecart(blocs.values()), 4),
              "ic_ecart": ic_ecart}
    sortie.update(_robustesse(blocs))
    return sortie


def _fenetre(base: Sequence[Exemple], ancien: Mapping | None, fin: int,
             jours_collecte: float) -> dict:
    ouv = ouverture_r8()
    ancien = ancien or {}
    registre = _cumuler(ancien.get("registre") or (),
                        [fiche(e) for e in base if e.ts_sec >= ouv])
    registre = [f for f in registre if int(f[0]) >= ouv]
    avant = [fiche(e) for e in base if e.ts_sec < ouv]
    sortie: dict = {"ouverture": ouv, "n": len(registre)}
    # Le plan (volumes, règle d'échéance) est figé au premier rejeu qui
    # suit l'ouverture, sur les entrées d'avant ; le gain à sa 1re lecture.
    plan = ancien.get("plan")
    if not plan or not plan.get("fige_le"):
        plan = _planifier(avant)
        if fin >= ouv and "p" in plan:
            plan["fige_le"] = fin
    # Un modèle pré-inscrit après le gel du plan (M4) y reçoit sa part,
    # mesurée sur les mêmes entrées d'avant l'ouverture.
    if "parts" in plan:
        for ident, _l, excl in MODELES_R8:
            if excl and ident not in plan["parts"] and avant:
                plan["parts"][ident] = round(
                    sum(1 for f in avant if _gardee(f, excl)) / len(avant), 4)
    gain = plan.get("gain")
    if gain is None and plan.get("fige_le"):
        gain = gain_minimal()
        if gain is not None:
            plan["gain"] = gain
    sortie["plan"] = plan
    if "p" in plan:
        sortie["volumes_proposes"] = {
            ident: {str(gp): volume_requis(plan["p"], plan["parts"][ident],
                                           gp, plan["effet_plan"])
                    for gp in GAINS_PROPOSES}
            for ident in plan["parts"]}
    finals = dict(ancien.get("finals") or {})
    modeles = {}
    for ident, _libelle, exclusions in MODELES_R8:
        if not exclusions:
            continue
        if ident in finals:
            modeles[ident] = finals[ident]
            continue
        resultat = _modele_fenetre(registre, exclusions, jours_collecte)
        cible = _volume(plan, ident, gain) if plan.get("fige_le") else None
        resultat["cible"] = cible
        if cible is None:
            resultat["verdict"] = ("≈ indéterminée — volume cible non fixé "
                                   "(P2_GAIN_MIN_POINTS, ou plan pas encore "
                                   f"figé : fenêtre du {JOUR_R8})")
        elif len(registre) < cible:
            resultat["verdict"] = (f"≈ indéterminée — {len(registre)} "
                                   f"signaux de M0 sur {cible}")
        else:
            code = conclusion(resultat, True)
            resultat["criteres"] = _criteres(resultat)
            resultat["verdict"] = (
                "✅ soutenue — les cinq critères" if code.startswith("✅")
                else "❌ non soutenue — " + code[2:] if code.startswith("❌")
                else "≈ indéterminée — " + code[2:])
            resultat["fige_le"] = fin
            finals[ident] = resultat
        modeles[ident] = resultat
    sortie["modeles"] = modeles
    # R8-H : la règle d'échéance figée, contre 15 min fixe. Le verdict est
    # figé dès le volume atteint : le relire à chaque rejeu jusqu'à ce
    # qu'il plaise serait tricher.
    regle = {s: int(h) for s, h in (plan.get("regle") or {}).items()}
    if "R8-H" in finals:
        sortie["echeance"] = finals["R8-H"]
    elif regle and plan.get("fige_le"):
        blocs: dict[tuple, list[int]] = {}
        for f in registre:
            issues = _issues_fiche(f)
            choisie = issues.get(regle[strate(f[3], f[4], f[5])])
            if choisie is None:
                continue
            bl = blocs.setdefault((f[1], int(f[0]) // 86400), [0, 0, 0, 0])
            bl[0] += 1
            bl[1] += f[2]
            bl[2] += 1
            bl[3] += int(choisie)
        _ic, ic_ecart = _intervalles(blocs)
        n = sum(v[0] for v in blocs.values())
        ecart = _ecart(blocs.values())
        sortie["echeance"] = {
            "n": n, "ecart": _rond(ecart, 4), "ic": ic_ecart,
            "verdict": _verdict(ecart, ic_ecart, n, n_min=N_MIN_ECHEANCE)}
        if n >= N_MIN_ECHEANCE:
            sortie["echeance"]["fige_le"] = fin
            finals["R8-H"] = sortie["echeance"]
    sortie["finals"] = finals
    sortie["registre"] = registre
    return sortie


# --------------------------------------------------------------------------- #
# Le rapport
# --------------------------------------------------------------------------- #

def reference8(exemples: Sequence[Exemple],
               en_direct: Callable[[Exemple], bool], fenetre: int = 1, *,
               qualite=None, exclusions: Mapping[str, int] | None = None,
               ancien: Mapping | None = None, debut: int | None = None,
               fin: int | None = None,
               jours_collectes: Callable[[int, int], float] | None = None
               ) -> dict:
    """Le rapport complet, en JSON. `exemples` : ZoneH1 rejouée (signaux
    bruts) ; `en_direct` : la règle de l'heure en cours ; `ancien` : le
    rapport du rejeu précédent (registre de la fenêtre, plan figé)."""
    tous = sorted(exemples, key=lambda e: e.ts_sec)
    signaux = [e for e in tous if en_direct(e)]
    base = [e for e in sorted(confirmes(tous, fenetre),
                              key=lambda e: e.ts_sec) if en_direct(e)]
    precedente = (ancien or {}).get("fenetre")
    if len(base) < 2 * N_MIN_VALIDATION:
        # Le registre de la fenêtre survit à un rejeu trop maigre.
        return {"note": f"Seulement {len(base)} entrées confirmées "
                        f"rejouées : il en faut {2 * N_MIN_VALIDATION}.",
                **({"fenetre": precedente} if precedente else {})}
    coupure = base[int(len(base) * PART_ETALONNAGE)].ts_sec
    debut = base[0].ts_sec if debut is None else debut
    fin = base[-1].ts_sec if fin is None else fin
    d0, d1 = base[0].ts_sec, base[-1].ts_sec
    jours = max(1.0, (jours_collectes and jours_collectes(d0 - 60, d1))
                or (d1 - d0) / 86400)
    ouv = ouverture_r8()
    jours_fenetre = ((jours_collectes(ouv, fin) if jours_collectes
                      else (fin - ouv) / 86400) if fin > ouv else 0.0)
    return {
        "n": len(base), "coupure": coupure, "jours": round(jours, 1),
        "periode": [d0, d1],
        "audit": _audit(tous, signaux, base, en_direct, fenetre, exclusions),
        "couverture": _couverture(qualite, signaux, base, debut, fin),
        "a": _livrable_a(signaux, fenetre),
        "b": _livrable_b(base, coupure, d0, jours),
        "c": _livrable_c(base, coupure),
        "fenetre": _fenetre(base, precedente, fin, jours_fenetre),
    }


def hypotheses(r: Mapping | None) -> list[tuple[str, str, str]]:
    """(id, hypothèse, verdict) : une décision par hypothèse, pour le
    rapport et le journal de /protocole."""
    if not r or "note" in r:
        return []
    sortie = []
    a = next((h for h in r["a"]["horizons"] if h["sec"] == ECHEANCE), None)
    if a:
        n = a["n"]
        sortie += [
            ("R8-A1", "la confirmation M1 améliore ZoneH1 (effet total à "
                      "15 min, tous les signaux)",
             _verdict(a["total"], a["ic_total"], n)),
            ("R8-A2", "la confirmation sélectionne de meilleurs signaux "
                      "(entrés au signal)",
             _verdict(a["selection"], a["ic_selection"], n)),
            ("R8-A3", "attendre la confirmation coûte (mêmes signaux, "
                      "entrée plus tardive)",
             _verdict(a["retard"], a["ic_retard"], n, sens=-1))]
    d = r["a"]["distance"]
    sortie.append(("R8-A4", "la confirmation entre plus loin de la zone",
                   _verdict(d["moyenne"], d["ic_moyenne"], d["n"])))
    b = r["b"]
    for x in b["predictive"]:
        sortie.append((f"R8-B-{x['lettre']}",
                       f"{x['lettre']} prédit le résultat (les écartées "
                       f"gagnent moins que les gardées)",
                       _verdict(x["ecart"], x["ic"], x["ecartees"][0],
                                n_min=20, concu=True)))
    for x in b["ajoutee"]:
        sortie.append((f"R8-B+{x['lettre']}",
                       f"{x['lettre']} apporte une information au-delà des "
                       f"deux autres (E contre E sans {x['lettre']})",
                       _verdict(x["ecart"], x["ic"], x["retirees"][0],
                                n_min=20, concu=True)))
    dist = b.get("distance")
    if dist:
        sortie += [
            ("R8-B-dist", "la distance à la zone prédit le résultat (les "
                          "entrées à 1,55 ou plus gagnent moins)",
             _verdict(dist["ecart"], dist["ic"], dist["ecartees"][0],
                      n_min=20, concu=True)),
            ("R8-B+dist", "la distance apporte une information au-delà de "
                          "B (B + distance contre B seul)",
             _verdict(dist["au_dela_de_b"]["ecart"],
                      dist["au_dela_de_b"]["ic"],
                      dist["au_dela_de_b"]["retirees"][0],
                      n_min=20, concu=True))]
    for ident, libelle, exclusions in MODELES_R8:
        if not exclusions:
            continue
        m = r["fenetre"]["modeles"].get(ident, {})
        sortie.append((f"R8-{ident}",
                       f"{libelle} améliore M0 (fenêtre du {JOUR_R8})",
                       m.get("verdict") or "≈ indéterminée — fenêtre pas "
                                           "encore ouverte"))
    for x in r["c"]["comparaisons"]:
        if x["b"] != ECHEANCE:
            continue
        sortie.append((f"R8-C{x['a'] // 60}",
                       f"{x['a'] // 60} min fait mieux que 15 min sur les "
                       f"mêmes entrées",
                       _verdict(x["ecart"], x["ic"], r["c"]["apparies"],
                                concu=True)))
    h = r["fenetre"].get("echeance")
    sortie.append(("R8-H", "choisir l'échéance selon B, C et D bat 15 min "
                           "fixe (règle figée, fenêtre)",
                   h["verdict"] if h else "≈ indéterminée — règle pas "
                                          "encore figée"))
    cv = r["couverture"]
    if "note" not in cv:
        hi = cv["sensibilite"]["élevée"]["n"]
        lo = cv["sensibilite"]["faible"]["n"]
        ic = cv.get("ic_ecart")
        if min(hi, lo) < 20 or cv.get("ecart_elevee_faible") is None:
            v = f"≈ indéterminée — {min(hi, lo)} entrées dans la plus " \
                f"petite classe, il en faut 20"
        elif ic and (ic[0] > 0 or ic[1] < 0):
            v = "✅ soutenue — l'écart élevée / faible exclut 0"
        else:
            v = "≈ indéterminée — l'intervalle touche 0"
        sortie.append(("R8-Q", "le résultat dépend de la couverture des "
                               "données (paire-journée élevée contre faible)",
                       v))
    return sortie


# --------------------------------------------------------------------------- #
# Texte
# --------------------------------------------------------------------------- #

def _p(n: int, g: int) -> str:
    return f"{g / n:.0%}" if n else "—"


def _e(x: float | None) -> str:
    return "—" if x is None else f"{x:+.3f}"


def _ic(ic: Sequence[float] | None) -> str:
    return "—" if not ic else f"[{ic[0]:+.2f} ; {ic[1]:+.2f}]"


def _q(q: Sequence[float] | None) -> str:
    return "—" if not q else f"{q[1]:.2f} ({q[0]:.2f}–{q[2]:.2f})"


def _f2(x: float | None) -> str:
    return "—" if x is None else f"{x:.2f}"


def _pc(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def _gain(g: float | None) -> str:
    return ("non fixé (P2_GAIN_MIN_POINTS)" if g is None
            else f"{g:g} points")


def _qp(q: Sequence[float] | None) -> str:
    return "—" if not q else f"{q[1]:.0%} ({q[0]:.0%}–{q[2]:.0%})"


def _section_audit(r: Mapping) -> list[str]:
    a = r["audit"]
    n = a["base"]
    d = a["disponibles"]
    lignes = [
        "<b>0. Populations et variables</b> (définitions et divergences : "
        "docs/reference8_audit.md)",
        f"Signaux ZoneH1 {a['signaux_zoneh1']} → règle de l'heure au signal "
        f"{a['joues_au_signal']} → confirmés {a['confirmes_au_signal']} → "
        f"joués (règle à l'entrée) {n}. Confirmés écartés par la règle "
        f"lue à l'entrée : {a['confirmes_ecartes_a_l_entree']} ; joués que "
        f"la règle au signal aurait écartés : "
        f"{a['joues_a_l_entree_seulement']}. Confirmation sur "
        f"{a['fenetre']} bougie(s).",
        f"À l'entrée : B {d['B']}/{n}, C {d['C']}/{n}, D {d['D']}/{n} ; "
        f"D = 99 (champ libre, aucun sommet/creux confirmé devant) "
        f"{a['champ_libre']} — une CATÉGORIE, jamais écartée.",
        "C absente : " + ", ".join(f"{k} {v}" for k, v in
                                   a["raisons_sans_c"].items()) + ".",
        "Autres lectures des seuils : " + ", ".join(
            f"{k} : {v}" for k, v in a["autres_lectures"].items())
        + " entrée(s) changeraient de côté.",
        f"Suivi de 15 min incomplet {a['suivi_incomplet']} ; sans issue "
        + ", ".join(f"{k} min {v}" for k, v in a["sans_issue"].items())
        + f" ; données douteuses {a['douteuses']}."]
    if a["temps_absents"]:
        lignes.append(f"⚠ {a['temps_absents']} entrées sans temps de "
                      f"progression : rejeu d'une version antérieure.")
    return lignes


def _section_couverture(r: Mapping) -> list[str]:
    cv = r["couverture"]
    if "note" in cv:
        return ["<b>1. Couverture des données</b>", cv["note"]]
    lignes = [
        "<b>1. Couverture des données</b> (une absence de données n'est ni "
        "une absence de signal ni une absence de mouvement)",
        f"Paire-heures exploitables (≥ {HEURE_EXPLOITABLE} min sur 60) : "
        f"{cv['paire_heures_exploitables']} ; signaux joués au signal "
        f"dedans {cv['signaux_exploitables']}, hors "
        f"{cv['signaux_hors']} ; densité "
        f"{cv['signaux_par_paire_heure'] or 0:.3f} signal par paire-heure.",
        f"Jours : couverture moyenne par paire {_qp(cv['jours'])} ; "
        f"{cv['jours_sous_moitie']}/{cv['nombre_jours']} jours sous 50 %."]
    paires = sorted(cv["paires"].items(), key=lambda kv: kv[1]["couverture"])
    lignes.append("Par paire (couverture, heures exploitables, signaux) : "
                  + " · ".join(f"{p.replace('_otc', '')} "
                               f"{x['couverture']:.0%}/{x['heures']}/"
                               f"{x['signaux']}" for p, x in paires))
    rangs = []
    for h in range(24):
        x = cv["par_heure"][str(h)]
        densite = x["par_paire_heure"]
        rangs.append(f"{h:02d}h {x['couverture']:>4.0%} {_f2(densite):>5}")
    lignes.append("<pre>heure couv. sig/ph\n" + "\n".join(
        "  ".join(rangs[k:k + 3]) for k in range(0, 24, 3)) + "</pre>")
    s = cv["sensibilite"]
    lignes.append(
        "Sensibilité (couverture de la paire-journée de l'entrée) : "
        + " · ".join(f"{k} {_p(v['n'], v['g'])} sur {v['n']} "
                     f"(EV {_e(v['ev'])})" for k, v in s.items())
        + f" ; écart élevée − faible {_e(cv['ecart_elevee_faible'])} "
          f"{_ic(cv['ic_ecart'])}.")
    return lignes


def _section_a(r: Mapping) -> list[str]:
    a = r["a"]
    lignes = [
        "<b>2. Livrable A — entrée immédiate ou confirmée</b> (mêmes "
        f"signaux : {a['signaux']} joués au signal, {a['confirmes']} "
        "confirmés)",
        "<pre>éch.  GG  GP  PG  PP  imm.   conf.  McN.\n" + "\n".join(
            f"{h['sec'] // 60:>2}m {h['table'][0]:>4}{h['table'][1]:>4}"
            f"{h['table'][2]:>4}{h['table'][3]:>4} "
            f"{_e(h['immediate']):>6} {_e(h['confirmee']):>6} "
            f"{_f2(h['mcnemar']):>5}"
            for h in a["horizons"]) + "</pre>",
        "GP : la confirmation fait perdre un gagnant ; PG : elle sauve un "
        "perdant. Espérances par ordre (payout 92 %).",
        "Sélection ou retard (écart d'espérance, intervalle par blocs) :"]
    for h in a["horizons"]:
        if h["sec"] == TEMOIN:
            continue
        lignes.append(
            f"• {h['sec'] // 60} min : sélection {_e(h['selection'])} "
            f"{_ic(h['ic_selection'])} · retard {_e(h['retard'])} "
            f"{_ic(h['ic_retard'])} · total {_e(h['total'])} "
            f"{_ic(h['ic_total'])}")
    s = a["suivant"]
    lignes.append(
        f"Témoin du retard pur (entrer à la bougie suivante, sans "
        f"condition, {s['n']} signaux) : {_e(s['immediate'])} → "
        f"{_e(s['suivante'])} {_ic(s['ic'])}.")
    if len(a["par_attente"]) > 1:
        lignes.append("Par attente : " + " · ".join(
            f"{k} min {_p(x[0], x[1])} → {_p(x[0], x[2])} sur {x[0]}"
            for k, x in sorted(a["par_attente"].items())))
    d = a["distance"]
    lignes.append(
        f"Distance à la zone (amplitudes M1, médiane et quartiles) : signal "
        f"{_q(d['signal'])}, entrée {_q(d['entree'])}, écart "
        f"{_q(d['ecart'])} ; plus loin pour {d['plus_loin']}/{d['n']} ; "
        f"écart moyen {_ic(d['ic_moyenne'])} ; poussées au-delà de 1,55 "
        f"(seuil de M4) par le retard : {d.get('entrent_m4', 0)}.")
    x = a["excursions"]
    lignes.append(
        f"MFE 15 min : immédiate {_q(x['mfe_15']['immediate'])}, confirmée "
        f"{_q(x['mfe_15']['confirmee'])} ; MAE : immédiate "
        f"{_q(x['mae_15']['immediate'])}, confirmée "
        f"{_q(x['mae_15']['confirmee'])} ({x['mfe_15']['n']} paires).")
    z = a["zone_cassee"]
    if z["n"]:
        lignes.append(f"Zone cassée dans les 15 min : immédiate "
                      f"{_p(z['n'], z['immediate'])}, confirmée "
                      f"{_p(z['n'], z['confirmee'])}.")
    lignes.append("B, C, D du signal à l'entrée (médianes ; entrées qui "
                  "basculent DANS / HORS de l'exclusion) : " + " · ".join(
                      f"{v['lettre']} {_f2(v['signal'])} → {_f2(v['entree'])}"
                      f" (+{v['entrent']} / −{v['sortent']})"
                      for v in a["variables"]))
    return lignes


def _section_b(r: Mapping) -> list[str]:
    b = r["b"]
    lignes = [
        "<b>3. Livrable B — modèles gelés sur les mêmes entrées</b> "
        "(historique : il a inspiré les seuils, il ne les valide pas)",
        "<pre>mod.    n  exc.  taux  EV      ΔEV   IC ΔEV\n" + "\n".join(
            f"{m['id']:<4}{m['n']:>5}{m['exclus']:>5} {_p(m['n'], m['g']):>5} "
            f"{_e(m['ev']):>6} {_e(m.get('ecart')):>6} "
            f"{_ic(m.get('ic_ecart')) if m.get('ic_ecart') else ''}"
            for m in b["modeles"]) + "</pre>"]
    for m in b["modeles"]:
        p = m["periodes"]
        ligne = (f"{m['id']} : anciens {_p(p[0], p[1])} sur {p[0]}, récents "
                 f"{_p(p[2], p[3])} sur {p[2]} ; {m['par_jour']}/jour")
        if m.get("sans_deux_paires") is not None:
            ligne += (f" ; ΔEV sans 2 paires {_e(m['sans_deux_paires'])}, "
                      f"sans la meilleure journée "
                      f"{_e(m.get('sans_meilleur_jour'))}")
        lignes.append(ligne)
    semaines = max(len(m["semaines"]) for m in b["modeles"])
    if semaines:
        rangs = []
        for k in range(semaines):
            cases = []
            for m in b["modeles"][:4]:
                n, g = (m["semaines"][k] if k < len(m["semaines"])
                        else (0, 0))
                cases.append(f"{_p(n, g):>4}/{n:<3}")
            rangs.append(f"{k + 1:>4} " + " ".join(cases))
        lignes.append("<pre>sem. " + " ".join(
            f"{m['id']:>8}" for m in b["modeles"][:4]) + "\n"
            + "\n".join(rangs) + "</pre>")
    m0 = b["modeles"][0]["paires"]
    grandes = sorted(m0, key=lambda p: -m0[p][0])[:8]
    if grandes:
        lignes.append("Par paire (M0 → M3) : " + " · ".join(
            f"{p.replace('_otc', '')} {_p(*m0[p])}/{m0[p][0]} → "
            f"{_p(*b['modeles'][3]['paires'].get(p, [0, 0]))}/"
            f"{b['modeles'][3]['paires'].get(p, [0, 0])[0]}"
            for p in grandes))
    c = b["chevauchement"]
    lignes.append(
        f"Chevauchement des exclusions : B {c['B']}, C {c['C']}, D {c['D']} ;"
        f" B∩C {c['B∩C']}, B∩D {c['B∩D']}, C∩D {c['C∩D']}, les trois "
        f"{c['B∩C∩D']} ; part des exclusions de C aussi écartées par D "
        f"{_pc(c['C_aussi_D'])}, de D aussi par C {_pc(c['D_aussi_C'])}.")
    lignes.append("Écartées par UNE seule variable : " + " · ".join(
        f"{k} {_p(*v)} sur {v[0]}" for k, v in c["seulement"].items())
        + f" ; par aucune {_p(*c['aucune'])} sur {c['aucune'][0]}.")
    lignes.append("Valeur prédictive (gardées − écartées, EV) : " + " · ".join(
        f"{x['lettre']} {_p(*x['ecartees'])}/{x['ecartees'][0]} contre "
        f"{_p(*x['gardees'])} : {_e(x['ecart'])} {_ic(x['ic'])}"
        for x in b["predictive"]))
    dist = b.get("distance")
    if dist:
        ad = dist["au_dela_de_b"]
        lignes.append(
            f"Distance à la zone (M4) : à 1,55 ou plus {_p(*dist['ecartees'])}"
            f" sur {dist['ecartees'][0]} contre {_p(*dist['gardees'])} : "
            f"{_e(dist['ecart'])} {_ic(dist['ic'])} ; dont {dist['aussi_b']} "
            f"déjà écartées par B ; au-delà de B, elle retire "
            f"{ad['retirees'][0]} ({_p(*ad['retirees'])}) : {_e(ad['ecart'])}"
            f" {_ic(ad['ic'])}.")
    lignes.append("Valeur ajoutée dans E (E − E sans la variable) : "
                  + " · ".join(
                      f"{x['lettre']} retire {x['retirees'][0]} "
                      f"({_p(*x['retirees'])}) : {_e(x['ecart'])} "
                      f"{_ic(x['ic'])}" for x in b["ajoutee"]))
    return lignes


def _section_fenetre(r: Mapping) -> list[str]:
    f = r["fenetre"]
    lignes = [f"<b>4. Fenêtre indépendante</b> (ouverte le {JOUR_R8}, "
              f"{f['n']} entrées de M0)"]
    plan = f["plan"]
    if "p" in plan:
        etat = ("figé" if plan.get("fige_le") else "provisoire")
        lignes.append(
            f"Plan {etat} sur {plan['n']} entrées d'avant : M0 "
            f"{plan['p']:.0%}, effet de plan {plan['effet_plan']} ; gain "
            f"minimal {_gain(plan.get('gain'))} ; règle d'échéance "
            + ", ".join(
                f"{s} → {int(h) // 60} min"
                for s, h in plan["regle"].items()) + ".")
    if not f["n"]:
        lignes.append("Aucune entrée dans la fenêtre pour l'instant : les "
                      "modèles restent indéterminés (section 6).")
        return lignes
    for ident, m in f["modeles"].items():
        cible = f" / cible {m['cible']}" if m.get("cible") else ""
        lignes.append(
            f"{ident} : {_p(m['n'], m['g'])} sur {m['n']}{cible}, EV "
            f"{_e(m['ev'])}, ΔEV {_e(m['ecart'])} {_ic(m['ic_ecart'])}")
    if f.get("echeance"):
        h = f["echeance"]
        lignes.append(f"R8-H : ΔEV {_e(h['ecart'])} {_ic(h['ic'])} sur "
                      f"{h['n']} — {h['verdict']}")
    return lignes


def _section_c(r: Mapping) -> list[str]:
    c = r["c"]
    lignes = [
        "<b>5. Livrable C — échéances et trajectoires</b> (mêmes entrées : "
        f"{c['apparies']} jugées à 4, 10 et 15 min)",
        " · ".join(f"{h['sec'] // 60} min {_p(h['n'], h['g'])} sur {h['n']} "
                   f"(EV {_e(h['ev'])})" for h in c["horizons"])]
    for x in c["comparaisons"]:
        t = x["table"]
        lignes.append(
            f"{x['a'] // 60} contre {x['b'] // 60} min : les deux gagnent "
            f"{t[0]}, seul {x['a'] // 60} {t[1]}, seul {x['b'] // 60} {t[2]}, "
            f"aucun {t[3]} ; ΔEV {_e(x['ecart'])} {_ic(x['ic'])}, McNemar "
            f"{_f2(x['mcnemar'])}")
    t = c["trajectoire"]
    lignes.append(
        f"Trajectoire ({t['n']} suivies 15 min, amplitudes M1) : MFE "
        f"{_q(t['mfe'])}, MAE {_q(t['mae'])} ; premier +1 à "
        f"{_q(t['t_pour'])} min, premier −1 à {_q(t['t_contre'])} min ; net "
        + ", ".join(f"{m} min {_q(q)}" for m, q in t["nets"].items())
        + f" ; zone cassée {t['zone_cassee']}.")
    lignes.append("Classes (des ISSUES, pas des filtres) : " + " · ".join(
        f"{x['classe']} {x['n']} ("
        + ", ".join(f"{m} min {_p(*v)}" for m, v in x["issues"].items())
        + ")" for x in c["classes"] if x["n"]))
    noms = [cle for cle, _l in PREALABLES]
    entete = "groupe          n " + " ".join(f"{k[:6]:>6}" for k in noms)
    rangs = []
    for nom, g in c["prealables"].items():
        rangs.append(f"{nom[:14]:<14}{g['n']:>4} " + " ".join(
            f"{_f2(g['medianes'][k]):>6}" for k in noms))
    lignes.append("Avant l'entrée (médianes) :\n<pre>" + entete + "\n"
                  + "\n".join(rangs) + "</pre>")
    lignes.append("Échéance selon B, C, D (taux à 4 / 10 / 15 min) : "
                  + " · ".join(
                      f"{v['lettre']} {v['cote']} {v['n']} : "
                      + "/".join(_p(v["n"], v["g"][m]) for m in ("4", "10",
                                                                  "15"))
                      for v in c["par_variable"]))
    ad = c["adaptatif"]
    lignes.append(
        "Sélection d'échéance apprise sur l'étalonnage ("
        + ", ".join(f"{s} → {h} min ({ad['strates'][s]})"
                    for s, h in ad["regle"].items())
        + f"), jugée sur les {ad['n']} récentes : 15 fixe {_e(ad['fixe'])}, "
          f"choisie {_e(ad['adaptee'])}, ΔEV {_e(ad['ecart'])} "
          f"{_ic(ad['ic'])} — indicatif ; le verdict vient de la fenêtre.")
    return lignes


def texte(r: Mapping | None) -> str:
    """`/reference8`."""
    if not r:
        return ("🧭 Mission Reference 8 pas encore calculée : elle accompagne "
                "le rejeu (quelques minutes après un redémarrage).")
    if "note" in r:
        return f"🧭 {r['note']}"
    lignes = [
        "🧭 <b>Mission Reference 8</b> — audit de l'entrée, filtres gelés "
        f"(B 1,31 · C 0,46 · D 1,95 · distance 1,55), échéances. {r['n']} "
        f"entrées "
        f"confirmées jouées, {r['jours']} jours de collecte. Des mesures : "
        "aucune règle n'est modifiée.", ""]
    for section in (_section_audit, _section_couverture, _section_a,
                    _section_b, _section_fenetre, _section_c):
        lignes += section(r) + [""]
    lignes.append("<b>6. Décisions</b> (soutenue / non soutenue / "
                  "indéterminée)")
    for ident, enonce, verdict in hypotheses(r):
        lignes.append(f"• {ident} — {enonce} : {verdict}")
    lignes += ["", "⚠ Payout de 92 % supposé à toutes les échéances. Une "
                   "association n'est pas une cause ; un résultat favorable "
                   "sur l'historique désigne une piste, pas une règle."]
    return "\n".join(lignes)
