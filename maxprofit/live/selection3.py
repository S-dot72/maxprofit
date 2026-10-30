"""
Étape 3 — `/micro_selection` : la passe de SÉLECTION (plan §6 et §13), en
lecture seule, autorisée le 30/10.

Elle calcule, sur les décisions d'avant le 28/10 00:00 UTC, tout ce que le
premier gel (`docs/etape3_preinscription.json`) détermine MÉCANIQUEMENT :
- la règle de retrait, sur l'exploration, sans issue ;
- les bornes des terciles de H1 et H2 (et de leurs variantes), sur
  l'exploration ;
- la préparation (médianes, moyennes, écarts), sur l'exploration ;
- les coefficients de M0, M_H1, M_H2, de leurs variantes et de M_H1H2, sur
  exploration + validation.

Ces paramètres forment le document du SECOND gel, rendu avec son empreinte
SHA-256. Rien n'est figé tant qu'il n'est pas commité ; la confirmation
s'ouvre au premier minuit UTC qui suit (option (a)).

En plus, et séparément : le criblage EXPLORATOIRE des 90 autres candidates
(H1 et H2 aux autres fenêtres comprises), ajustées sur l'exploration et
jugées sur la validation, en BH à 10 %. Une découverte y reste une piste,
jamais une preuve.

--- Où l'on lit ---------------------------------------------------------------

Sur la RELÈVE, qui reçoit toute la base active toutes les 20 minutes et a son
propre quota de transfert. Une copie n'est crue que vérifiée : pour chaque
paire et chaque jour, chemins de ticks, bougies et payouts sont résumés par
le serveur (comptes et sommes) sur les deux bases. Un paire-jour dont un
résumé diffère est lu sur l'active, et compté. Si l'active ne répond pas, la
copie est dite « non vérifiée ».

--- Ce qui arrête la passe (plan §10) -------------------------------------------

Un contrôle anti-fuite qui échoue, ou moins de 80 % de fenêtres VALID : le
rapport est rendu, le document du gel ne l'est pas.

Sorties en CSV et JSON uniquement. Aucune écriture en base.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.apprentissage import microstructure_pre as mp
from maxprofit.live import etude3
from maxprofit.live.etude3 import ConnexionTracee, etat_lecture_seule

log = logging.getLogger(__name__)

#: Les deux hypothèses principales du premier gel : (variable, fenêtre,
#: sens, tercile). Un test vérifie qu'elles sont celles du document gelé.
HYPOTHESES: dict[str, tuple[str, int, int, int]] = {
    "H1": ("A7_penetration", 300, -1, 2),
    "H2": ("D1_traversees", 300, -1, 2),
}
TRAITEMENT, K = "distincts", 0
#: Les variantes de mesure des sensibilités (traitement × k) ; la première
#: est la mesure principale.
MESURES: tuple[tuple[str, int], ...] = tuple(
    (t, k) for t in em.SENSIBILITES[0][1] for k in em.SENSIBILITES[1][1])
EGALITES: tuple[str, ...] = em.SENSIBILITES[3][1]
#: Le criblage exploratoire : l'échéance principale et les secondaires.
HORIZONS_EXPLORATOIRES: tuple[int, ...] = (em.HORIZON_SEC,
                                           *em.HORIZONS_SECONDAIRES)
VALID_MIN = 0.80
PLAFOND_SIGNAUX = em.REGARDS[-1]
#: Entre deux paires-jours : laisse respirer la course, qui partage le
#: processus.
RESPIRATION_SEC = 0.02

REQUETES_COPIE: dict[str, str] = {
    "tick_paths": (
        "SELECT pair, minute_sec / 86400, COUNT(*), SUM(n_ticks), "
        "SUM(LENGTH(chemin)) FROM tick_paths WHERE minute_sec >= ? AND "
        "minute_sec < ? GROUP BY pair, minute_sec / 86400"),
    "candles": (
        "SELECT pair, ts_sec / 86400, COUNT(*), SUM(tick_count), "
        "SUM(complete), SUM(ROUND(close * 100000)) FROM candles WHERE "
        "tf_sec = 60 AND ts_sec >= ? AND ts_sec < ? "
        "GROUP BY pair, ts_sec / 86400"),
    "payouts": (
        "SELECT pair, ts_sec / 86400, COUNT(*), SUM(payout_pct), "
        "SUM(is_open) FROM payouts WHERE ts_sec >= ? AND ts_sec < ? "
        "GROUP BY pair, ts_sec / 86400"),
}
PAYOUT_AVANT = ("SELECT ts_sec, payout_pct FROM payouts WHERE pair = ? AND "
                "ts_sec < ? ORDER BY ts_sec DESC LIMIT 1")
PAYOUTS_DU_JOUR = ("SELECT ts_sec, payout_pct FROM payouts WHERE pair = ? "
                   "AND ts_sec >= ? AND ts_sec < ? ORDER BY ts_sec")


def cle(variable: str, w: int, traitement: str = TRAITEMENT,
        k: int = K) -> str:
    """« A7_penetration@300 » pour la mesure principale ; les variantes
    portent leur traitement et leur troncature."""
    base = f"{variable}@{w}"
    return base if (traitement, k) == (TRAITEMENT, K) else \
        f"{base}|{traitement}|k{k}"


def _jour(ts: int) -> int:
    return int(ts) // 86400


def _utc(ts: int, forme: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime(forme)


# --------------------------------------------------------------------------- #
# La copie de la relève, vérifiée
# --------------------------------------------------------------------------- #

def resumes(conn, debut: int, fin: int) -> dict[tuple[str, str, int], tuple]:
    """(table, paire, jour) → comptes et sommes, calculés par le serveur."""
    sortie = {}
    for table, sql in REQUETES_COPIE.items():
        for ligne in conn.execute(sql, (int(debut), int(fin))).fetchall():
            valeurs = tuple(None if v is None else int(round(float(v)))
                            for v in ligne[2:])
            sortie[(table, str(ligne[0]), int(ligne[1]))] = valeurs
    return sortie


def divergences(releve: Mapping, active: Mapping) -> set[tuple[str, str, int]]:
    return {k for k in set(releve) | set(active)
            if releve.get(k) != active.get(k)}


def lu_sur_la_releve(divergents: set, paire: str, jour: int) -> bool:
    """Un paire-jour lit la veille (fenêtre de 15 min), le jour et le
    lendemain (bougie de sortie), et le dernier payout connu : tout cela
    doit être identique sur la relève."""
    for table in ("tick_paths", "candles"):
        if any((table, paire, j) in divergents
               for j in (jour - 1, jour, jour + 1)):
            return False
    return not any(t == "payouts" and p == paire and j <= jour + 1
                   for t, p, j in divergents)


# --------------------------------------------------------------------------- #
# Les signaux du rejeu en mémoire, issues comprises
# --------------------------------------------------------------------------- #

def signaux_de_la_periode(exemples, sans_issue, niveaux, debut: int,
                          fin: int) -> dict[tuple[str, int], dict]:
    """(paire, décision) → sens, issue, issues secondaires, contexte, niveau.
    Les signaux à issue décisive viennent de `exemples` ; les égalités et
    les sorties manquantes, de `sans_issue` (relevé passif du rejeu)."""
    sortie: dict[tuple[str, int], dict] = {}
    for e in exemples:
        if debut <= e.ts_sec < fin:
            sortie[(e.pair, e.ts_sec)] = {
                "call": bool(e.contexte.get("sens_call")),
                "issue": em.GAIN if e.gagne else em.PERTE,
                "issues": {int(h): bool(v) for h, v in dict(e.issues).items()},
                "contexte": e.contexte}
    for s in sans_issue or ():
        d = int(s["ts_sec"])
        if debut <= d < fin and (s["pair"], d) not in sortie:
            issue = (em.EGALITE if s["issue"] == "egalite" else em.MANQUANTE)
            sortie[(s["pair"], d)] = {
                "call": bool(s["call"]), "issue": issue, "issues": {},
                "contexte": {**(s.get("contexte") or {}),
                             "sens_call": float(bool(s["call"]))}}
    for (paire, d), s in sortie.items():
        s["niveau"] = niveaux.get((paire, d))
    return sortie


def payouts_du_jour(conn, paire: str, debut: int) -> list[tuple[int, int]]:
    avant = conn.execute(PAYOUT_AVANT, (paire, int(debut))).fetchone()
    lignes = conn.execute(PAYOUTS_DU_JOUR,
                          (paire, int(debut), int(debut) + 86400)).fetchall()
    sortie = [(int(avant[0]), int(avant[1]))] if avant else []
    return sortie + [(int(t), int(p)) for t, p in lignes]


def payout_a(releves: Sequence[tuple[int, int]], d: int) -> float | None:
    """Le dernier payout relevé STRICTEMENT avant la décision."""
    connu = None
    for t, p in releves:
        if t >= d:
            break
        connu = p
    return None if connu is None else float(connu)


# --------------------------------------------------------------------------- #
# Modèles
# --------------------------------------------------------------------------- #

def sous_preparation(prep: em.Preparation,
                     numeriques: Sequence[str]) -> em.Preparation:
    return em.Preparation(
        tuple(numeriques), dict(prep.categories),
        {n: prep.medianes[n] for n in numeriques},
        {n: prep.moyennes[n] for n in numeriques},
        {n: prep.ecarts[n] for n in numeriques})


def ajuster(prep: em.Preparation, lignes: Sequence[Mapping], y: str
            ) -> em.Logistique:
    return em.Logistique.ajuster([prep.vecteur(r) for r in lignes],
                                 [int(r[y]) for r in lignes])


def fiche_de_modele(prep: em.Preparation, modele: em.Logistique, n: int,
                    egalites: str) -> dict[str, Any]:
    return {"numeriques": list(prep.numeriques),
            "colonnes": ["interception", *prep.colonnes()],
            "coefficients": list(modele.coefficients), "n": n,
            "egalites": egalites}


def coefficient(fiche: Mapping[str, Any], colonne: str) -> float:
    return fiche["coefficients"][fiche["colonnes"].index(colonne)]


# --------------------------------------------------------------------------- #
# La passe
# --------------------------------------------------------------------------- #

ATTENDRE_LE_REJEU = (
    "🧪 Le rejeu quotidien n'est pas encore en mémoire avec les niveaux de "
    "zone et les signaux sans issue : il tourne quelques minutes après "
    "chaque démarrage. Relance /micro_selection quand /lecons annonce le "
    "nouvel apprentissage.")


def selection(releve, active, course, *, nom_lecture: str = "relève",
              maintenant: float | None = None, code: str | None = None,
              respirer=None) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers). `releve` : la base lue (déjà en lecture seule) ;
    `active` : la base de référence de la copie, ou None."""
    maintenant = time.time() if maintenant is None else maintenant
    respirer = respirer or (lambda: time.sleep(RESPIRATION_SEC))
    t0 = time.monotonic()
    memoire = getattr(course, "rejeu_en_memoire", None)
    niveaux = getattr(course, "niveaux_en_memoire", None)
    sans_issue = getattr(course, "sans_issue_en_memoire", None)
    if not memoire or niveaux is None or sans_issue is None:
        return ATTENDRE_LE_REJEU, []
    if int(course.strategie.p.expiry_sec) != em.HORIZON_SEC:
        return ("🧪 L'échéance de la stratégie n'est plus 900 s : la passe "
                "pré-inscrite ne s'applique pas."), []
    if not em.periode_figeable(em.FIN_VALIDATION, maintenant):
        return "🧪 Les issues de la validation ne sont pas encore mûres.", []

    lue = ConnexionTracee(releve)
    reference = ConnexionTracee(active) if active is not None else None
    lecture = {nom_lecture: etat_lecture_seule(lue)}
    if reference is not None:
        lecture["active"] = etat_lecture_seule(reference)
    # Le premier tick des DEUX bases : une relève qui commence plus tard
    # n'écourte pas la passe, ses jours manquants divergent et se lisent sur
    # l'active.
    premieres = [m for m in (etude3.premiere_minute_de_ticks(c) for c in
                             (lue, reference) if c is not None)
                 if m is not None]
    premiere = min(premieres) if premieres else None
    if premiere is None:
        return "🧪 Aucun chemin de ticks sur la base lue.", []
    debut = premiere + mp.QUALITE_SEC
    fin = em.FIN_VALIDATION

    # La copie, vérifiée par paire × jour.
    t = time.monotonic()
    copie: dict[str, Any] = {"verifiee": reference is not None}
    divergents: set = set()
    if reference is not None and active is not releve:
        bornes = (_jour(debut) * 86400 - 86400, fin + 2 * 86400)
        divergents = divergences(resumes(lue, *bornes),
                                 resumes(reference, *bornes))
    copie["divergences"] = sorted(divergents)
    copie["temps"] = time.monotonic() - t

    exemples = memoire[0]
    signaux = signaux_de_la_periode(exemples, sans_issue, niveaux,
                                    _jour(debut) * 86400, fin)
    avant_les_ticks = sum(1 for (_, d) in signaux if d < debut)
    signaux = {k: v for k, v in signaux.items() if k[1] >= debut}
    if len(signaux) > PLAFOND_SIGNAUX:
        return (f"🧪 {len(signaux)} signaux dans la période : au-delà du "
                f"plafond de {PLAFOND_SIGNAUX}. Passe non lancée."), []

    # Les paires-jours.
    par_pj: dict[tuple[str, int], list] = {}
    for (paire, d), s in signaux.items():
        par_pj.setdefault((paire, _jour(d)), []).append(
            (d, s["call"], s["niveau"]))
    resultats, lignes, lus = [], [], Counter()
    variantes = [m for m in MESURES if m != (TRAITEMENT, K)]
    t = time.monotonic()
    for paire, jour in sorted(par_pj):
        sur_releve = (reference is None or
                      lu_sur_la_releve(divergents, paire, jour))
        conn = lue if sur_releve else reference
        lus[nom_lecture if sur_releve else "active"] += 1
        r = etude3.paire_jour(conn, course, paire, jour * 86400,
                              sorted(par_pj[(paire, jour)]),
                              variantes=variantes,
                              fenetre_variantes=HYPOTHESES["H1"][1])
        r["jour"] = jour
        r["base"] = nom_lecture if sur_releve else "active"
        r["payouts"] = payouts_du_jour(conn, paire, jour * 86400)
        # Assemblé tout de suite : seules les lignes de signaux restent en
        # mémoire (le service n'a que 512 Mo).
        lignes += assembler([r], signaux)
        for lourd in ("lignes", "fiches", "variantes", "payouts"):
            r.pop(lourd, None)
        resultats.append(r)
        respirer()
    duree_lecture = time.monotonic() - t
    lignes.sort(key=lambda x: (x["decision"], x["paire"]))
    controles = controler(resultats, lignes)
    t = time.monotonic()
    calcul = calculer(lignes, respirer=respirer)
    duree_calcul = time.monotonic() - t

    entrees = hashlib.sha256(json.dumps(
        sorted([r["paire"], r["jour"], r["empreinte_entree"]]
               for r in resultats)).encode()).hexdigest()
    resume = {
        "debut": debut, "fin": fin, "premiere": premiere,
        "lecture": lecture, "copie": copie, "lus": dict(lus),
        "requetes": {nom_lecture: dict(lue.requetes),
                     **({"active": dict(reference.requetes)}
                        if reference is not None else {})},
        "refusees": lue.refusees + (reference.refusees if reference else 0),
        "avant_les_ticks": avant_les_ticks,
        "octets": sum(r["octets"] for r in resultats),
        "bougies": sum(r["bougies"] for r in resultats),
        "duree": time.monotonic() - t0, "duree_lecture": duree_lecture,
        "duree_calcul": duree_calcul, "entrees": entrees,
        "paires_jours": len(resultats), "maintenant": maintenant,
        "code": code, "version_regles": (str(memoire[3])
                                         if len(memoire) > 3 else None),
    }
    document = None
    if not controles["arrets"]:
        document = document_du_gel(resume, controles, calcul)
    return (texte(resume, controles, calcul, document),
            fichiers(lignes, calcul, document))


def assembler(resultats: Sequence[dict], signaux: Mapping) -> list[dict]:
    """Une ligne par signal : qualité, issue, référence, variables à toutes
    les fenêtres (mesure principale) et variantes de H1 et H2."""
    lignes = []
    for r in resultats:
        par_w: dict[int, dict] = {}
        for x in r["lignes"]:
            par_w.setdefault(x["decision"], {})[x["w"]] = x
        par_v: dict[int, list] = {}
        for x in r.get("variantes", ()):
            par_v.setdefault(x["decision"], []).append(x)
        for fiche in r["fiches"]:
            d = fiche["decision"]
            s = signaux[(r["paire"], d)]
            ctx = s["contexte"]
            ligne = {
                "paire": r["paire"], "decision": d,
                "decision_utc": fiche["decision_utc"],
                "periode": em.periode(d), "sens": fiche["sens"],
                "base": r["base"], "classe": fiche["classe"],
                "E8_niveau": fiche["E8_niveau"],
                "hors_niveau_2": fiche["hors_niveau_2"],
                "issue": s["issue"],
                "y": em.y_selon(s["issue"], "exclues"),
                "y_perdues": em.y_selon(s["issue"], "perdues"),
                "creneau": em.creneau(datetime.fromtimestamp(
                    d, timezone.utc).hour),
                "payout": payout_a(r["payouts"], d),
            }
            for h in em.HORIZONS_SECONDAIRES:
                v = s["issues"].get(h)
                ligne[f"y_{h}"] = None if v is None else int(v)
            ligne[f"y_{em.HORIZON_SEC}"] = ligne["y"]
            for nom in em.REFERENCE_NUMERIQUES:
                if nom != "payout":
                    v = ctx.get(nom)
                    ligne[nom] = None if v is None else float(v)
            for w, x in par_w.get(d, {}).items():
                for v in mp.VARIABLES:
                    ligne[cle(v, w)] = x.get(v)
            for x in par_v.get(d, ()):
                for variable, w, _, _ in HYPOTHESES.values():
                    ligne[cle(variable, w, x["traitement"], x["k"])] = \
                        x.get(variable)
            ligne["principale"] = (ligne["E8_niveau"] == 2
                                   and ligne["classe"] == "VALID")
            lignes.append(ligne)
    lignes.sort(key=lambda x: (x["decision"], x["paire"]))
    return lignes


def controler(resultats: Sequence[dict], lignes: Sequence[dict]
              ) -> dict[str, Any]:
    autotests = [sum(r["autotests"][0] for r in resultats),
                 sum(r["autotests"][1] for r in resultats)]
    classes = Counter(x["classe"] for x in lignes)
    part_valid = classes.get("VALID", 0) / len(lignes) if lignes else 0.0
    # A7 et D1 ne dépendent pas des copies exactes : bruts = distincts.
    ecarts_bruts = sum(
        1 for x in lignes for variable, w, _, _ in HYPOTHESES.values()
        for k in em.SENSIBILITES[1][1]
        if x.get(cle(variable, w, "bruts", k))
        != x.get(cle(variable, w, "distincts", k)))
    arrets = []
    if autotests[0] == 0 or autotests[0] != autotests[1]:
        arrets.append(f"anti-fuite : {autotests[1]}/{autotests[0]} "
                      f"contrôles sans effet")
    if part_valid < VALID_MIN:
        arrets.append(f"fenêtres VALID : {100 * part_valid:.0f} %, sous "
                      f"{100 * VALID_MIN:.0f} %")
    return {"autotests": autotests, "classes": dict(classes),
            "part_valid": part_valid, "ecarts_bruts": ecarts_bruts,
            "sans_niveau": sum(r["sans_niveau"] for r in resultats),
            "arrets": arrets}


def calculer(lignes: Sequence[dict], respirer=lambda: None
             ) -> dict[str, Any]:
    """Retrait, terciles, préparation, modèles du gel ; puis, à part, le
    criblage exploratoire."""
    exploration = [x for x in lignes if x["periode"] == em.EXPLORATION
                   and x["principale"]]
    selection_ = [x for x in lignes if x["periode"] in
                  (em.EXPLORATION, em.VALIDATION) and x["principale"]]
    validation = [x for x in selection_ if x["periode"] == em.VALIDATION]
    registre = em.Registre()

    # 1. Retrait, sur l'exploration, sans issue.
    candidates = [cle(v, w) for w in mp.FENETRES_SEC for v in mp.VARIABLES]
    trace = em.retrait_des_quasi_constantes(
        {c: [x.get(c) for x in exploration] for c in candidates})
    retenues = [t["candidate"] for t in trace if t["decision"] == "conservée"]

    # 2. Hypothèses : abandonnée si sa variable est retirée.
    hypotheses: dict[str, dict[str, Any]] = {}
    for h, (variable, w, sens, tercile) in HYPOTHESES.items():
        principale = cle(variable, w)
        hypotheses[h] = {
            "variable": principale, "sens_preinscrit": sens,
            "tercile": tercile,
            "statut": ("testée" if principale in retenues else
                       "abandonnée (variable retirée par la règle)"),
            "terciles": {f"{t}|k{k}": em.terciles(
                [x.get(cle(variable, w, t, k)) for x in exploration])
                for t, k in MESURES}}
    testees = [h for h in HYPOTHESES if hypotheses[h]["statut"] == "testée"]

    # 3. Préparation, sur l'exploration.
    numeriques_h = [cle(HYPOTHESES[h][0], HYPOTHESES[h][1], t, k)
                    for h in testees for t, k in MESURES]
    prep = em.Preparation.ajuster(
        exploration, [*em.REFERENCE_NUMERIQUES, *numeriques_h],
        categories=("paire", "creneau"))

    # 4. Modèles, sur exploration + validation, puis figés.
    modeles: dict[str, dict[str, Any]] = {}
    for egal in EGALITES:
        y = "y" if egal == "exclues" else "y_perdues"
        jeu = [x for x in selection_ if x[y] is not None]
        if not jeu:
            continue
        p0 = sous_preparation(prep, em.REFERENCE_NUMERIQUES)
        modeles[f"M0|{egal}"] = fiche_de_modele(p0, ajuster(p0, jeu, y),
                                                len(jeu), egal)
        respirer()
        for h in testees:
            variable, w, _, _ = HYPOTHESES[h]
            for t, k in MESURES:
                ph = sous_preparation(prep, [*em.REFERENCE_NUMERIQUES,
                                             cle(variable, w, t, k)])
                nom = f"M_{h}|{t}|k{k}|{egal}"
                modeles[nom] = fiche_de_modele(ph, ajuster(ph, jeu, y),
                                               len(jeu), egal)
                principal = (t, k, egal) == (TRAITEMENT, K, "exclues")
                registre.noter(
                    phase="validation",
                    nature="principale" if principal else "sensibilite",
                    hypothese=h, variable=variable, w=w, traitement=t, k=k,
                    horizon=em.HORIZON_SEC, modele=f"référence + {h}",
                    statistique="coefficient (standardisé), figé",
                    valeur=coefficient(modeles[nom], cle(variable, w, t, k)),
                    note=f"égalités {egal} ; n = {len(jeu)}")
                respirer()
    if len(testees) == 2:
        jeu = [x for x in selection_ if x["y"] is not None]
        p12 = sous_preparation(prep, [*em.REFERENCE_NUMERIQUES,
                                      *(hypotheses[h]["variable"]
                                        for h in testees)])
        modeles["M_H1H2|distincts|k0|exclues"] = fiche_de_modele(
            p12, ajuster(p12, jeu, "y"), len(jeu), "exclues")
    for h in testees:
        fiche = modeles.get(f"M_{h}|{TRAITEMENT}|k{K}|exclues")
        if fiche:
            c = coefficient(fiche, hypotheses[h]["variable"])
            hypotheses[h]["coefficient"] = c
            hypotheses[h]["signe_concordant"] = (
                c * hypotheses[h]["sens_preinscrit"] > 0)

    # 5. Criblage exploratoire : ajusté sur l'exploration, jugé sur la
    # validation. Les deux principales à 300 s n'y sont pas.
    principales = {hypotheses[h]["variable"] for h in HYPOTHESES}
    for t in trace:
        if t["decision"] != "conservée":
            n = registre.noter(phase="exploration", nature="exploratoire",
                               variable=t["candidate"], traitement=TRAITEMENT,
                               k=K, statistique="retrait (version 1)")
            registre.abandonner(n, t["raison"])
    prep_x = em.Preparation.ajuster(
        exploration, [*em.REFERENCE_NUMERIQUES, *retenues],
        categories=("paire", "creneau"))
    p0x = sous_preparation(prep_x, em.REFERENCE_NUMERIQUES)
    exploratoire, valeurs_p = [], {}
    for horizon in HORIZONS_EXPLORATOIRES:
        y = f"y_{horizon}"
        app = [x for x in exploration if x.get(y) is not None]
        jug = [x for x in validation if x.get(y) is not None]
        if len(app) < 30 or len(jug) < 30:
            continue
        m0 = ajuster(p0x, app, y)
        ref = [m0.proba(p0x.vecteur(x)) for x in jug]
        for c in retenues:
            if c in principales:
                continue
            pc = sous_preparation(prep_x, [*em.REFERENCE_NUMERIQUES, c])
            mc = ajuster(pc, app, y)
            comparees = [em.LigneComparee(
                ident=f"{x['paire']}|{x['decision']}",
                bloc=(x["paire"], _jour(x["decision"])),
                jour=_jour(x["decision"]), y=int(x[y]), p_ref=p,
                p_mod=mc.proba(pc.vecteur(x))) for x, p in zip(jug, ref)]
            bilan = em.comparer(comparees)
            par_bloc: dict = {}
            for r in comparees:
                par_bloc.setdefault(r.bloc, []).append(r.gain_log)
            boot = em.bootstrap_par_blocs(par_bloc)
            variable, _, w = c.partition("@")
            ligne = {"candidate": c, "variable": variable, "w": int(w),
                     "horizon": horizon, "n_apprentissage": len(app),
                     "n_validation": bilan["n"],
                     "gain_log": bilan["gain_log"], "bas": boot["bas"],
                     "haut": boot["haut"], "p": boot["p_unilaterale"],
                     "blocs": boot["blocs"],
                     "coefficient": coefficient(
                         fiche_de_modele(pc, mc, len(app), "exclues"), c),
                     "brier_ref": bilan["brier_ref"],
                     "brier_mod": bilan["brier_mod"]}
            ligne["registre"] = registre.noter(
                phase="validation", nature="exploratoire", variable=variable,
                w=int(w), traitement=TRAITEMENT, k=K, horizon=horizon,
                modele="référence + variable", statistique="Δℓ",
                valeur=bilan["gain_log"], p=boot["p_unilaterale"])
            exploratoire.append(ligne)
            valeurs_p[(c, horizon)] = boot["p_unilaterale"]
            respirer()
    decouvertes = em.benjamini_hochberg(valeurs_p) if valeurs_p else {}
    for ligne in exploratoire:
        ligne["bh_10"] = decouvertes.get((ligne["candidate"],
                                          ligne["horizon"]), False)
    exploratoire.sort(key=lambda x: (x["p"], -x["gain_log"]))

    def compte(jeu, cle_y="y"):
        return {"signaux": len(jeu),
                "avec_issue": sum(1 for x in jeu if x[cle_y] is not None)}
    population = {
        "toutes": Counter(x["periode"] for x in lignes),
        "niveau_2": Counter(x["periode"] for x in lignes
                            if x["E8_niveau"] == 2),
        "principale": Counter(x["periode"] for x in lignes
                              if x["principale"]),
        "issues": Counter(x["issue"] for x in lignes if x["principale"]),
        "exploration": compte(exploration),
        "validation": compte(validation),
        "partial_niveau_2": sum(1 for x in lignes if x["E8_niveau"] == 2
                                and x["classe"] == "PARTIAL"),
    }
    return {"trace": trace, "retenues": retenues, "hypotheses": hypotheses,
            "preparation": prep, "modeles": modeles,
            "exploratoire": exploratoire, "registre": registre,
            "population": population}


# --------------------------------------------------------------------------- #
# Le document du second gel
# --------------------------------------------------------------------------- #

def document_du_gel(resume: Mapping, controles: Mapping,
                    calcul: Mapping) -> dict[str, Any]:
    prep: em.Preparation = calcul["preparation"]
    pop = calcul["population"]
    doc = {
        "document": "Étape 3 — paramètres figés (second gel)",
        "gel_hypotheses": em.EMPREINTE_GEL_HYPOTHESES,
        "regles": ("déterminés mécaniquement par docs/etape3_preinscription"
                   ".json, sur les seules décisions d'avant "
                   "2026-10-28T00:00:00Z"),
        "calcule_le": _utc(int(resume["maintenant"]), "%Y-%m-%dT%H:%M:%SZ"),
        "code": resume.get("code"),
        "regles_du_rejeu": resume.get("version_regles"),
        "donnees": {
            "premiere_minute_de_ticks": resume["premiere"],
            "debut": resume["debut"], "fin": resume["fin"],
            "paires_jours": resume["paires_jours"],
            "lus_sur": resume["lus"],
            "copie_verifiee": resume["copie"]["verifiee"],
            "divergences_de_copie": len(resume["copie"]["divergences"]),
            "empreinte_des_entrees": resume["entrees"]},
        "population": {
            "principale": "niveau 2 et fenêtre VALID",
            "exploration": pop["exploration"],
            "validation": pop["validation"]},
        "controles": {"anti_fuite": controles["autotests"],
                      "part_valid": round(controles["part_valid"], 6)},
        "retrait": {"version": em.VERSION_RETRAIT,
                    "retenues": calcul["retenues"],
                    "retirees": [t["candidate"] for t in calcul["trace"]
                                 if t["decision"] != "conservée"]},
        "hypotheses": {
            h: {k: v for k, v in x.items()} for h, x in
            calcul["hypotheses"].items()},
        "preparation": {"numeriques": list(prep.numeriques),
                        "categories": {k: list(v) for k, v in
                                       prep.categories.items()},
                        "medianes": prep.medianes,
                        "moyennes": prep.moyennes, "ecarts": prep.ecarts},
        "modeles": calcul["modeles"],
        "regards": {"signaux_niveau_2": list(em.REGARDS),
                    "seuils_nominaux": {str(s["N"]): s["nominal"]
                                        for s in em.seuils_des_regards()}},
        "confirmation": ("option (a) : décisions à partir du premier minuit "
                         "UTC qui suit le commit de ce document"),
    }
    return doc


def contenu_du_document(document: Mapping) -> bytes:
    return (json.dumps(document, sort_keys=True, ensure_ascii=False,
                       indent=1) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# Rapport et fichiers
# --------------------------------------------------------------------------- #

def _s(x: float) -> str:
    return f"{x / 60:.1f} min" if x >= 60 else f"{x:.1f} s"


def _pc(n: int, total: int) -> str:
    return f"{n}/{total} ({100 * n / total:.0f} %)" if total else "—"


def texte(resume: Mapping, controles: Mapping, calcul: Mapping,
          document: Mapping | None) -> str:
    pop = calcul["population"]
    lus = ", ".join(f"{v} sur {html.escape(k)}" for k, v in
                    sorted(resume["lus"].items()))
    copie = resume["copie"]
    l = ["🧪 <b>Étape 3 — passe de sélection</b> (lecture seule)",
         f"Décisions du {_utc(resume['debut'], '%d/%m %H:%M')} au "
         f"{_utc(resume['fin'] - 60, '%d/%m %H:%M')} UTC : exploration "
         f"avant le 21/10, validation du 21 au 27/10.", "",
         "<b>Données</b>",
         f"- {resume['paires_jours']} paires-jours lus : {lus}",
         ("- copie de la relève vérifiée par paire × jour (chemins, "
          "bougies, payouts) : "
          + ("identique ✅" if not copie["divergences"] else
             f"{len(copie['divergences'])} résumé(s) différent(s), "
             f"paires-jours concernés lus sur l'active")
          if copie["verifiee"] else
          "- copie NON vérifiée : l'active n'a pas répondu ⚠️"),
         f"- lu : {resume['octets'] / 1e6:.1f} Mo de chemins, "
         f"{resume['bougies']} bougies ; transaction_read_only : "
         + ", ".join(f"{html.escape(k)} {html.escape(v)}"
                     for k, v in resume["lecture"].items())
         + f" ; requêtes refusées : {resume['refusees']}",
         f"- durée : {_s(resume['duree'])} (lecture et variables "
         f"{_s(resume['duree_lecture'])}, modèles "
         f"{_s(resume['duree_calcul'])})", "",
         "<b>Population</b> (une ligne par signal ZoneH1 du rejeu)"]
    toutes = pop["toutes"]
    for periode in (em.EXPLORATION, em.VALIDATION):
        l.append(f"- {periode} : {toutes.get(periode, 0)} signaux, niveau 2 "
                 f"{pop['niveau_2'].get(periode, 0)}, principale (niveau 2 "
                 f"et VALID) {pop['principale'].get(periode, 0)}, dont avec "
                 f"issue décisive {pop[periode]['avec_issue']}")
    classes = controles["classes"]
    n = sum(classes.values())
    l.append("- fenêtres : " + ", ".join(
        f"{k} {_pc(v, n)}" for k, v in sorted(classes.items())))
    l.append("- issues de la population principale : " + ", ".join(
        f"{k} {v}" for k, v in sorted(pop["issues"].items())))
    exclus = [f"{resume['avant_les_ticks']} avant les premiers ticks",
              f"{controles['sans_niveau']} sans niveau de zone",
              f"{pop['partial_niveau_2']} PARTIAL de niveau 2 (sensibilité "
              f"séparée)"]
    l.append("- hors population : " + ", ".join(exclus))
    a = controles["autotests"]
    l += ["", "<b>Contrôles</b>",
          f"- anti-fuite : {_pc(a[1], a[0])} sans effet "
          + ("✅" if a[0] and a[0] == a[1] else "⚠️"),
          f"- fenêtres VALID : {100 * controles['part_valid']:.0f} % "
          f"(seuil {100 * VALID_MIN:.0f} %) "
          + ("✅" if controles["part_valid"] >= VALID_MIN else "⚠️"),
          "- A7 et D1 identiques en ticks bruts et distincts : "
          + ("oui ✅" if not controles["ecarts_bruts"] else
             f"NON, {controles['ecarts_bruts']} écart(s) ⚠️")]
    retirees = [t for t in calcul["trace"] if t["decision"] != "conservée"]
    l += ["", "<b>Retrait des quasi constantes</b> (exploration, sans issue)",
          f"- {len(calcul['retenues'])} candidates conservées, "
          f"{len(retirees)} retirées"
          + (" : " + ", ".join(html.escape(t["candidate"])
                               for t in retirees[:12])
             + (" …" if len(retirees) > 12 else "") if retirees else "")]
    l += ["", "<b>Hypothèses principales</b> (gel 1, "
          f"{em.EMPREINTE_GEL_HYPOTHESES[:8]}…)"]
    for h, x in calcul["hypotheses"].items():
        bornes = x["terciles"].get(f"{TRAITEMENT}|k{K}")
        ligne = (f"- {h} {html.escape(x['variable'])} : {x['statut']}"
                 + (f" ; terciles d'exploration {bornes[0]:.3g} / "
                    f"{bornes[1]:.3g}" if bornes else ""))
        if "coefficient" in x:
            ligne += (f" ; coefficient figé {x['coefficient']:+.3f} "
                      + ("(dans le sens pré-inscrit)"
                         if x["signe_concordant"] else
                         "(OPPOSÉ au sens pré-inscrit : confirmation "
                         "seulement si l'effet pratique, dans le sens "
                         "pré-inscrit, atteint le seuil)"))
        l.append(ligne)
    l.append("- ce coefficient est estimé sur les données de sélection : "
             "il ne prouve rien. Seule la confirmation jugera H1 et H2.")
    l += ["", "<b>Criblage exploratoire</b> (ajusté sur l'exploration, jugé "
          "sur la validation, BH à 10 %)"]
    ex = calcul["exploratoire"]
    if not ex:
        l.append("- trop peu de signaux pour le criblage.")
    else:
        bh = [x for x in ex if x["bh_10"]]
        l.append(f"- {len(ex)} tests ({len({x['candidate'] for x in ex})} "
                 f"candidates × échéances) ; découvertes BH : {len(bh)}")
        for x in ex[:8]:
            l.append(f"  · {html.escape(x['candidate'])} à {x['horizon']} s"
                     f" : Δℓ {1000 * x['gain_log']:+.2f} mnats "
                     f"[{1000 * x['bas']:+.2f} ; {1000 * x['haut']:+.2f}], "
                     f"p {x['p']:.3f}, {x['blocs']} blocs, coefficient "
                     f"{x['coefficient']:+.2f}"
                     + (" — BH ✅" if x["bh_10"] else ""))
        l.append("- une piste exploratoire ne confirme rien : elle ne peut "
                 "devenir principale que pré-inscrite et testée sur des "
                 "données nouvelles.")
    l.append(f"- registre : {calcul['registre'].compte()} essais notés")
    l += ["", "<b>Second gel</b>"]
    if document is None:
        l.append("- NON produit : " + "; ".join(controles["arrets"])
                 + ". Diagnostiquer avant toute suite (plan §10).")
    else:
        l.append(f"- parametres_gel2.json, empreinte "
                 f"<code>{em.empreinte(document)}</code>")
        l.append("- rien n'est figé tant que ce document n'est pas commité. "
                 "La confirmation s'ouvre au premier minuit UTC qui suit le "
                 "commit (option (a)).")
    l += ["", "Rien n'a été écrit en base."]
    return "\n".join(l)


def fichiers(lignes: Sequence[dict], calcul: Mapping,
             document: Mapping | None) -> list[tuple[bytes, str, str]]:
    variantes = [cle(v, w, t, k) for v, w, _, _ in HYPOTHESES.values()
                 for t, k in MESURES if (t, k) != (TRAITEMENT, K)]
    colonnes = ["paire", "decision", "decision_utc", "periode", "sens",
                "base", "classe", "E8_niveau", "hors_niveau_2", "principale",
                "issue", "y", "y_perdues",
                *(f"y_{h}" for h in em.HORIZONS_SECONDAIRES), "creneau",
                *em.REFERENCE_NUMERIQUES,
                *(cle(v, w) for w in mp.FENETRES_SEC for v in mp.VARIABLES),
                *variantes]
    sortie = [
        (etude3._csv(lignes, colonnes), "selection_signaux.csv",
         "Passe de sélection : un signal par ligne, qualité, issue, "
         "référence, variables (distincts, k = 0) et variantes de H1/H2"),
        (etude3._csv(calcul["trace"], list(calcul["trace"][0])
                     if calcul["trace"] else ["candidate"]),
         "selection_retrait.csv",
         "Règle de retrait (version 1), sur l'exploration, sans issue"),
        (calcul["registre"].csv(), "selection_registre.csv",
         "Registre : chaque essai, abandonné compris"),
    ]
    if calcul["exploratoire"]:
        sortie.append((etude3._csv(calcul["exploratoire"],
                                   list(calcul["exploratoire"][0])),
                       "selection_exploratoire.csv",
                       "Criblage exploratoire (BH 10 %) : des pistes, pas "
                       "des preuves"))
    if document is not None:
        sortie.append((contenu_du_document(document), "parametres_gel2.json",
                       f"Second gel, empreinte {em.empreinte(document)} : à "
                       f"commiter tel quel"))
    return sortie
