"""
Validation de P2 — EXP-020 C et D, protocole du 2026-10-14.

--- Ce que l'on cherche ----------------------------------------------------

Les filtres « part du chemin parcourue » (C) et « place avant le prochain
obstacle » (D) améliorent-ils RÉELLEMENT ZoneH1, sans trop réduire le
nombre de signaux ? Rien n'est ajouté à la stratégie, rien n'est changé dans
la course : on mesure, sur des données nouvelles, des règles figées.

--- Les six phases, et où elles sont ---------------------------------------

1. Règles gelées : `live.preinscriptions` (seuils 0,46 et 1,95, exclusions,
   valeur manquante gardée) ; la référence (règles, confirmation, règle de
   l'heure, échéance, paires) est photographiée à l'ouverture, et toute
   modification ultérieure est signalée. C et D sont causaux : lus sur la
   fenêtre qui finit à la bougie d'entrée, pivots déjà confirmés
   (`tests/test_validation_p2.py` le vérifie sur le rejeu).
2. Quatre groupes sur les MÊMES signaux : A référence, B = A sans C ≥ 0,46,
   C = A sans D sous 1,95, D = les deux exclusions. Chaque entrée de la
   fenêtre est gardée au registre de la validation (valeurs de C et D,
   issue), y compris celles qu'un filtre écarte ; la fiche CSV de /audit
   donne aussi la décision et le motif de chaque filtre.
3. Étalonnage et test séparés : la fenêtre s'ouvre le 14/10 à 0 h UTC ; ce
   qui précède sert seulement à PLANIFIER (taux de référence, part conservée,
   corrélation des signaux, débit), jamais à choisir un seuil.
4. Critères écrits d'avance (`CRITERE_P2`) : espérance positive ; gain
   d'espérance sur la référence établi par une comparaison APPARIÉE (mêmes
   blocs paire × jour rééchantillonnés pour les deux groupes) ; gain qui
   tient sans les deux paires et sans la journée qui y contribuent le plus ;
   au moins la moitié des signaux conservés.
5. Volume : revue de contrôle à `REVUE_A` signaux de référence, sans verdict ;
   puis un volume CIBLE calculé avant le test pour le gain minimal choisi
   (`P2_GAIN_MIN_POINTS`, gelé à sa première lecture), et une date de
   clôture qui en découle.
6. Un tableau unique : référence, C, D, C + D.

--- Le calcul du volume ----------------------------------------------------

Un filtre qui garde la part q des signaux gagne Δ = (1 − q)(p_gardés −
p_écartés) sur la référence. Détecter Δ à 5 % (unilatéral) avec une
puissance de 80 % demande environ

    N = effet_de_plan × p(1 − p) × (1 − q) / q × ((1,645 + 0,842) / Δ)²

signaux de RÉFÉRENCE, où l'effet de plan mesure la corrélation des signaux
d'une même paire et d'un même jour (variance par blocs / variance binomiale).
À p = 0,55, q = 0,6 et des signaux indépendants : environ 1 130 signaux pour
3 points, 410 pour 5, 100 pour 10. Au débit actuel, quatorze jours ne
détectent qu'un écart énorme.

⚠ Des mesures, pas des règles : la course ne lit rien d'ici.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
from typing import Iterable, Mapping, Sequence

from maxprofit.apprentissage.lecons import Exemple, wilson
from maxprofit.live.preinscriptions import (
    AMENDEMENT_P2, CRITERE_P2, GROUPES_P2, motifs_de_rejet, ouverture,
    preinscription)

PAYOUT = 0.92
#: Signaux de référence de la revue de contrôle (étape A).
REVUE_A = 100
#: Part minimale des signaux de la référence qu'un filtre doit garder.
PART_MIN_CONSERVEE = 0.5
Z_ALPHA = 1.645                  # unilatéral, 5 %
Z_BETA = 0.842                   # puissance 80 %
#: Gains minimaux (points de taux de réussite) proposés pour le volume.
GAINS_PROPOSES: tuple[int, ...] = (3, 5, 8, 10)
ENV_GAIN = "P2_GAIN_MIN_POINTS"
TIRAGES = 1000
GRAINE = 7
NOMS = {"A": "Réf.", "P2-C": "C", "P2-D": "D", "P2-E": "C+D"}


def esperance(p: float) -> float:
    """Par ordre, en mises, au payout de 92 % : 1,92 p − 1."""
    return p * (1 + PAYOUT) - 1


def gain_minimal() -> float | None:
    """`P2_GAIN_MIN_POINTS` (points de taux) ; `None` s'il n'est pas fixé."""
    brut = os.environ.get(ENV_GAIN, "").strip().replace(",", ".")
    try:
        valeur = float(brut)
    except ValueError:
        return None
    return valeur if valeur > 0 else None


def volume_requis(p: float, q: float, gain_points: float,
                  effet_plan: float = 1.0) -> int | None:
    """Signaux de RÉFÉRENCE pour détecter `gain_points` de taux de réussite
    sur la référence (voir l'en-tête)."""
    if not (0 < p < 1) or not (0 < q < 1) or gain_points <= 0:
        return None
    gain = gain_points / 100
    return math.ceil(max(1.0, effet_plan) * p * (1 - p) * (1 - q) / q
                     * ((Z_ALPHA + Z_BETA) / gain) ** 2)


# --------------------------------------------------------------------------- #
# Le registre des entrées de la fenêtre
# --------------------------------------------------------------------------- #

def _valeur(ctx: Mapping, cle: str) -> float | None:
    v = ctx.get(cle)
    return None if v is None else round(float(v), 4)


def fiche(e: Exemple) -> list:
    """[entrée (s), paire, gagné, C, D, données douteuses] : de quoi
    recalculer les quatre groupes sans rejouer."""
    return [int(e.ts_sec), e.pair, int(bool(e.gagne)),
            _valeur(e.contexte, "part_parcourue"),
            _valeur(e.contexte, "espace_obstacle"),
            int(bool(e.contexte.get("donnees_douteuses")))]


def fusionner(ancien: Iterable[Sequence], nouveau: Iterable[Sequence]
              ) -> list[list]:
    """Le registre cumulé : une entrée mesurée garde sa PREMIÈRE fiche. Le
    rejeu ne couvre que ses derniers jours ; sans ce cumul, le début de la
    fenêtre sortirait du compte avant la clôture."""
    fiches: dict[tuple[str, int], list] = {}
    for f in ancien:
        fiches.setdefault((f[1], int(f[0])), list(f))
    for f in nouveau:
        fiches.setdefault((f[1], int(f[0])), list(f))
    return sorted(fiches.values(), key=lambda f: (f[0], f[1]))


def _gardee(f: Sequence, ident: str) -> bool:
    exclusions = preinscription(ident).parametres["exclusions"]
    return not motifs_de_rejet({"part_parcourue": f[3],
                                "espace_obstacle": f[4]}, exclusions)


# --------------------------------------------------------------------------- #
# Mesures
# --------------------------------------------------------------------------- #

def _blocs(fiches: Sequence[Sequence], ident: str | None
           ) -> dict[tuple[str, int], list[int]]:
    """{(paire, jour) : [n réf., gagnés réf., n groupe, gagnés groupe]}."""
    blocs: dict[tuple[str, int], list[int]] = {}
    for f in fiches:
        b = blocs.setdefault((f[1], int(f[0]) // 86400), [0, 0, 0, 0])
        b[0] += 1
        b[1] += f[2]
        if ident is None or _gardee(f, ident):
            b[2] += 1
            b[3] += f[2]
    return blocs


def _ecart(blocs: Iterable[Sequence[int]]) -> float | None:
    na = ga = nx = gx = 0
    for b in blocs:
        na, ga, nx, gx = na + b[0], ga + b[1], nx + b[2], gx + b[3]
    if not na or not nx:
        return None
    return esperance(gx / nx) - esperance(ga / na)


def _intervalles(blocs: Mapping[tuple[str, int], Sequence[int]]
                 ) -> tuple[list[float] | None, list[float] | None]:
    """Intervalles à 95 % par blocs paire × jour : le taux du groupe, et
    l'écart d'espérance au groupe de référence sur les MÊMES tirages."""
    valeurs = list(blocs.values())
    if len(valeurs) < 5:
        return None, None
    hasard = random.Random(GRAINE)
    taux, ecarts = [], []
    for _ in range(TIRAGES):
        tirage = [valeurs[hasard.randrange(len(valeurs))]
                  for _ in range(len(valeurs))]
        nx = sum(b[2] for b in tirage)
        if nx:
            taux.append(sum(b[3] for b in tirage) / nx)
        e = _ecart(tirage)
        if e is not None:
            ecarts.append(e)

    def borne(liste):
        if len(liste) < TIRAGES // 2:
            return None
        liste.sort()
        return [round(liste[int(0.025 * len(liste))], 4),
                round(liste[int(0.975 * len(liste)) - 1], 4)]
    return borne(taux), borne(ecarts)


def _sans(blocs, retires: set) -> float | None:
    """L'écart sans les paires (texte) et les jours (entier) de `retires`."""
    return _ecart(v for (paire, jour), v in blocs.items()
                  if paire not in retires and jour not in retires)


def _robustesse(blocs: Mapping[tuple[str, int], Sequence[int]]) -> dict:
    """L'écart d'espérance sans les deux paires, puis sans la journée, qui
    y contribuent le plus (celles dont le retrait le fait le plus baisser)."""
    paires = sorted({k[0] for k in blocs})
    jours = sorted({k[1] for k in blocs})
    par_paire = sorted(paires, key=lambda p: (_sans(blocs, {p}) or 0.0, p))
    par_jour = sorted(jours, key=lambda j: (_sans(blocs, {j}) or 0.0, j))
    deux = set(par_paire[:2])
    return {"sans_deux_paires": _sans(blocs, deux),
            "paires_retirees": sorted(deux),
            "sans_meilleur_jour": _sans(blocs, set(par_jour[:1])),
            "jour_retire": par_jour[0] * 86400 if par_jour else None}


def _groupe(fiches: Sequence[Sequence], ident: str | None,
            jours_collecte: float, n_reference: int) -> dict:
    gardees = [f for f in fiches if ident is None or _gardee(f, ident)]
    n, g = len(gardees), sum(f[2] for f in gardees)
    blocs = _blocs(fiches, ident)
    ic, ic_ecart = _intervalles(blocs)
    p = g / n if n else None
    sortie = {"n": n, "g": g,
              "wilson": list(wilson(g, n, 1.96)) if n else None,
              "ic": ic,
              "ev": round(esperance(p), 4) if p is not None else None,
              "par_jour": (round(n / jours_collecte, 2)
                           if jours_collecte > 0 else None),
              "part_conservee": (round(n / n_reference, 4)
                                 if n_reference else None)}
    if sortie["ev"] is not None and sortie["par_jour"] is not None:
        sortie["ev_jour"] = round(sortie["ev"] * sortie["par_jour"], 3)
    if ident is not None:
        sortie["ecart"] = _ecart(blocs.values())
        sortie["ic_ecart"] = ic_ecart
        sortie.update(_robustesse(blocs))
    return sortie


def _criteres(gr: Mapping) -> dict[str, bool | None]:
    def positif(x):
        return None if x is None else x > 0
    return {
        "esperance_positive": positif(gr.get("ev")),
        "gain_etabli": (None if not gr.get("ic_ecart")
                        else gr["ic_ecart"][0] > 0),
        "pas_deux_paires": positif(gr.get("sans_deux_paires")),
        "pas_une_journee": positif(gr.get("sans_meilleur_jour")),
        "volume": (None if gr.get("part_conservee") is None
                   else gr["part_conservee"] >= PART_MIN_CONSERVEE),
    }


def conclusion(gr: Mapping, volume_atteint: bool) -> str:
    """Les trois issues écrites d'avance (`CRITERE_P2`)."""
    c = _criteres(gr)
    if not volume_atteint:
        return "≈ résultat indécis : volume cible non atteint à la clôture"
    if all(c.values()):
        return "✅ piste confirmée provisoirement"
    if (gr.get("ecart") or 0) > 0 and c["esperance_positive"] \
            and c["pas_deux_paires"] and c["pas_une_journee"] \
            and c["volume"] and not c["gain_etabli"]:
        return "≈ résultat indécis : gain positif, précision insuffisante"
    return "❌ piste rejetée"


# --------------------------------------------------------------------------- #
# Planification et gel
# --------------------------------------------------------------------------- #

def _effet_de_plan(fiches: Sequence[Sequence]) -> float:
    """Variance du taux par blocs paire × jour / variance binomiale."""
    blocs = list(_blocs(fiches, None).values())
    n = sum(b[0] for b in blocs)
    if len(blocs) < 5 or not n:
        return 1.0
    p = sum(b[1] for b in blocs) / n
    if p in (0.0, 1.0):
        return 1.0
    hasard = random.Random(GRAINE)
    taux = []
    for _ in range(TIRAGES):
        tirage = [blocs[hasard.randrange(len(blocs))] for _ in blocs]
        nt = sum(b[0] for b in tirage)
        taux.append(sum(b[1] for b in tirage) / nt)
    moyenne = sum(taux) / len(taux)
    variance = sum((t - moyenne) ** 2 for t in taux) / (len(taux) - 1)
    return round(max(1.0, variance / (p * (1 - p) / n)), 2)


def planifier(avant: Sequence[Sequence], jours_calendrier: float) -> dict:
    """Ce que les données d'AVANT la fenêtre disent pour planifier : taux de
    référence, part gardée par chaque filtre, effet de plan, débit, et le
    volume requis pour chaque gain proposé. Jamais un seuil."""
    n = len(avant)
    plan: dict = {"n": n}
    if n < 20:
        plan["note"] = f"seulement {n} entrées avant la fenêtre"
        return plan
    p = sum(f[2] for f in avant) / n
    plan.update({"p": round(p, 4), "effet_plan": _effet_de_plan(avant),
                 "debit_calendaire": (round(n / jours_calendrier, 2)
                                      if jours_calendrier > 0 else None),
                 "parts": {}, "volumes": {}})
    for ident in GROUPES_P2:
        q = sum(1 for f in avant if _gardee(f, ident)) / n
        plan["parts"][ident] = round(q, 4)
        plan["volumes"][ident] = {
            str(g): volume_requis(p, q, g, plan["effet_plan"])
            for g in GAINS_PROPOSES}
    return plan


def _photo(reference: Mapping) -> dict:
    """Ce qui définit la référence ; comparé à chaque rejeu."""
    criteres = hashlib.sha1(
        (CRITERE_P2 + repr([preinscription(i).parametres
                            for i in GROUPES_P2])).encode()).hexdigest()[:12]
    return {**reference, "criteres": criteres}


def validation(base: Sequence[Exemple], ancien: Mapping | None, *,
               fin: int, jours_collecte: float, reference: Mapping,
               jours_avant: float, gain: float | None = None) -> dict:
    """L'état de la validation après ce rejeu, en JSON.

    `base` : les entrées confirmées jouées du rejeu (référence) ;
    `reference` : règles, confirmation, règle de l'heure, échéance et paires
    de la course ; `jours_collecte` : jours de collecte réelle depuis
    l'ouverture ; `jours_avant` : jours de calendrier rejoués avant elle.
    """
    debut = ouverture()
    ancien = dict(ancien or {})
    if ancien.get("final"):
        return ancien
    etat: dict = {"ouverture": debut, "amendement": AMENDEMENT_P2,
                  "revue_a": REVUE_A, "part_min": PART_MIN_CONSERVEE}
    if fin < debut:
        etat["note"] = "fenêtre pas encore ouverte"
        return etat
    photo = _photo(reference)
    gel = ancien.get("gel") or {
        "pris_le": fin, "reference": photo,
        "plan": planifier([fiche(e) for e in base if e.ts_sec < debut],
                          jours_avant)}
    alertes = [f"{cle} modifié pendant le test"
               for cle, valeur in photo.items()
               if gel["reference"].get(cle) != valeur]
    # Le gain minimal : gelé à sa première lecture, puis plus jamais relu.
    if gain is not None and "gain" not in gel:
        p = gel["plan"].get("p")
        volumes = [volume_requis(p, (gel["plan"].get("parts") or {}).get(i),
                                 gain, gel["plan"].get("effet_plan", 1.0))
                   if p else None for i in GROUPES_P2]
        cible = max((v for v in volumes if v), default=None)
        debit = gel["plan"].get("debit_calendaire")
        gel.update({"gain": gain, "gain_fixe_le": fin, "cible": cible,
                    "cloture": (debut + math.ceil(cible / debit) * 86400
                                if cible and debit else None)})
    elif gain is not None and gel.get("gain") != gain:
        alertes.append(f"gain minimal changé après son gel : on garde "
                       f"{gel['gain']:g} points")
    etat["gel"] = gel
    etat["alertes"] = alertes

    fiches = fusionner(ancien.get("registre", ()),
                       (fiche(e) for e in base if e.ts_sec >= debut))
    etat["registre"] = fiches
    n_a = len(fiches)
    groupes = {"A": _groupe(fiches, None, jours_collecte, n_a)}
    for ident in GROUPES_P2:
        groupes[ident] = _groupe(fiches, ident, jours_collecte, n_a)
    etat["groupes"] = groupes
    etat["jours_collecte"] = round(jours_collecte, 2)
    etat["jours"] = round((fin - debut) / 86400, 1)

    # La revue de contrôle (étape A) : qualité, exclusions, chevauchement.
    exclus = {i: {(f[1], f[0]) for f in fiches if not _gardee(f, i)}
              for i in ("P2-C", "P2-D")}
    union = exclus["P2-C"] | exclus["P2-D"]
    etat["revue"] = {
        "douteuses": sum(f[5] for f in fiches),
        "c_manquant": sum(1 for f in fiches if f[3] is None),
        "chevauchement": (round(len(exclus["P2-C"] & exclus["P2-D"])
                                / len(union), 3) if union else None)}

    cible, cloture = gel.get("cible"), gel.get("cloture")
    if n_a < REVUE_A:
        etat["etape"] = "A"
    elif not cible:
        etat["etape"] = "B à paramétrer"
    elif n_a >= cible or (cloture and fin >= cloture):
        etat["etape"] = "final"
        for ident in GROUPES_P2:
            groupes[ident]["conclusion"] = conclusion(groupes[ident],
                                                      n_a >= cible)
        etat["final"] = True
        etat["fige_le"] = fin
    else:
        etat["etape"] = "B"
    return etat


def hypotheses(etat: Mapping) -> list[tuple[str, str, str]]:
    """(id, libellé, verdict) pour le journal du protocole."""
    groupes = etat.get("groupes") or {}
    sortie = []
    for ident in GROUPES_P2:
        if ident not in groupes:
            continue
        verdict = groupes[ident].get("conclusion") or (
            f"⏳ en cours — étape {etat.get('etape', '?')}")
        sortie.append((ident, preinscription(ident).nom, verdict))
    return sortie


# --------------------------------------------------------------------------- #
# Texte
# --------------------------------------------------------------------------- #

def _p(x: float | None, signe: bool = False) -> str:
    if x is None:
        return "—"
    return f"{x:+.0%}" if signe else f"{x:.0%}"


def _e(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}"


def _ligne(libelle: str, valeurs: Sequence[str]) -> str:
    return f"{libelle:<13}" + "".join(f"{v:>9}" for v in valeurs)


def texte(etat: Mapping | None, jour) -> list[str]:
    """Les lignes de /protocole pour la validation P2 (HTML Telegram)."""
    if not etat:
        return []
    a = etat["amendement"]
    lignes = [f"<b>Validation P2 — EXP-020 C et D</b> (amendement du "
              f"{a[8:10]}/{a[5:7]} : 4 groupes sur les mêmes signaux, "
              f"critères écrits d'avance, fenêtre ouverte le "
              f"{jour(etat['ouverture'])})"]
    if etat.get("note"):
        return lignes + [f"  {etat['note']}."]
    gel = etat.get("gel") or {}
    plan = gel.get("plan") or {}
    for a in etat.get("alertes") or ():
        lignes.append(f"  ⚠ {a}")
    groupes = etat["groupes"]
    n_a = groupes["A"]["n"]
    etape = etat.get("etape")
    if etape == "A":
        lignes.append(f"  Étape A (revue de contrôle, sans verdict) : "
                      f"{n_a}/{etat['revue_a']} signaux de référence.")
    elif etape == "B à paramétrer":
        lignes.append(f"  Étape A atteinte ({n_a} signaux). Étape B à "
                      f"paramétrer : fixer {ENV_GAIN} (gain minimal à "
                      f"détecter, en points).")
    elif etape == "B":
        lignes.append(f"  Étape B : {n_a}/{gel['cible']} signaux de "
                      f"référence ; clôture au plus tard le "
                      f"{jour(gel['cloture']) if gel.get('cloture') else '—'}"
                      f" (gain minimal {gel['gain']:g} points, gelé le "
                      f"{jour(gel['gain_fixe_le'])}).")
    else:
        lignes.append(f"  Résultat FINAL ({n_a} signaux de référence, figé "
                      f"le {jour(etat['fige_le'])}).")
    cles = ["A", *GROUPES_P2]
    t = [_ligne("", [NOMS[c] for c in cles])]
    t.append(_ligne("signaux", [str(groupes[c]["n"]) for c in cles]))
    t.append(_ligne("réussite", [
        _p(groupes[c]["g"] / groupes[c]["n"]) if groupes[c]["n"] else "—"
        for c in cles]))
    t.append(_ligne("IC95 blocs", [
        f"{groupes[c]['ic'][0]:.0%}-{groupes[c]['ic'][1]:.0%}"
        if groupes[c].get("ic") else "—" for c in cles]))
    t.append(_ligne("EV/ordre", [_e(groupes[c].get("ev")) for c in cles]))
    t.append(_ligne("sig./jour", [
        "—" if groupes[c].get("par_jour") is None
        else f"{groupes[c]['par_jour']:.1f}" for c in cles]))
    t.append(_ligne("EV/jour", [_e(groupes[c].get("ev_jour")) for c in cles]))
    t.append(_ligne("éliminés", ["—"] + [
        _p(1 - groupes[c]["part_conservee"])
        if groupes[c].get("part_conservee") is not None else "—"
        for c in GROUPES_P2]))
    t.append(_ligne("ΔEV vs réf.", ["—"] + [
        _e(groupes[c].get("ecart")) for c in GROUPES_P2]))
    for rang, nom in ((0, "ΔEV IC bas"), (1, "ΔEV IC haut")):
        t.append(_ligne(nom, ["—"] + [
            _e(groupes[c]["ic_ecart"][rang]) if groupes[c].get("ic_ecart")
            else "—" for c in GROUPES_P2]))
    t.append(_ligne("Δ sans 2 p.", ["—"] + [
        _e(groupes[c].get("sans_deux_paires")) for c in GROUPES_P2]))
    t.append(_ligne("Δ sans 1 j.", ["—"] + [
        _e(groupes[c].get("sans_meilleur_jour")) for c in GROUPES_P2]))
    lignes.append("<pre>" + "\n".join(t) + "</pre>")
    lignes.append(f"  Fréquences par jour de COLLECTE réelle "
                  f"({etat['jours_collecte']} j collectés sur "
                  f"{etat['jours']} j de calendrier).")
    for ident in GROUPES_P2:
        if groupes[ident].get("conclusion"):
            lignes.append(f"  {NOMS[ident]} : {groupes[ident]['conclusion']}")
    r = etat.get("revue") or {}
    if n_a:
        chev = ("—" if r.get("chevauchement") is None
                else f"{r['chevauchement']:.0%}")
        lignes.append(
            f"  Revue : {r.get('douteuses', 0)} entrée(s) sur données "
            f"douteuses, {r.get('c_manquant', 0)} sans valeur de C (gardées "
            f"par C), chevauchement des exclusions C et D {chev}.")
    if plan.get("volumes"):
        meilleur = {g: max((plan["volumes"][i].get(str(g)) or 0)
                           for i in GROUPES_P2) for g in GAINS_PROPOSES}
        debit = plan.get("debit_calendaire")
        propositions = " · ".join(
            f"{g} pts : {v} sig." + (f" (~{math.ceil(v / debit)} j)"
                                     if debit and v else "")
            for g, v in meilleur.items())
        lignes.append(f"  Volume requis (puissance 80 %, taux de référence "
                      f"{plan['p']:.0%}, effet de plan "
                      f"{plan['effet_plan']:.2f}, planifié sur les "
                      f"{plan['n']} entrées d'avant) : {propositions}")
    elif plan.get("note"):
        lignes.append(f"  Planification impossible : {plan['note']}.")
    return lignes
