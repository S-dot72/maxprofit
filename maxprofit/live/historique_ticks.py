"""
`/historique_ticks [jours]` — les ticks de chaque paire, heure par heure, sur
les derniers jours (7 par défaut). Diagnostic autorisé le 31/10, en LECTURE
SEULE : il date le début de la dégradation de la collecte, et dit si elle
précède le 28/10.

--- Où l'on lit ---------------------------------------------------------------

Sur la RELÈVE (son propre quota de transfert), copie vérifiée heure par heure
sur l'active : pour chaque paire × heure, le serveur des deux bases résume les
chemins de ticks (minutes, ticks bruts), et chaque heure résume les battements.
Une heure dont un résumé diffère est reprise de l'active (résumés et minutes,
pas les ticks distincts) et marquée « copie divergente ».

--- Absence de ticks ou absence de données ------------------------------------

Le collecteur écrit un battement toutes les 30 s, ticks ou pas. Une minute
sans battement est une minute SANS COLLECTE : on n'y sait rien, ni des ticks
ni de leur absence. Une minute avec battement mais sans tick d'une paire est
un SILENCE de cette paire. Par heure et par paire :

  SANS COLLECTE  aucune minute de collecte dans l'heure
  SILENCE        collecte, mais aucun tick de la paire
  RARE           des ticks dans moins de 75 % des minutes de collecte
  ACTIVE         des ticks dans au moins 75 % des minutes de collecte

--- Ticks bruts et ticks distincts --------------------------------------------

Les chemins gardent chaque tick reçu, doublons de la double réception compris
(enquête du 25/10) : « bruts ». Les « distincts » comptent une seule fois un
même instant au même prix ; il faut décoder les chemins pour les compter,
d'où la lecture sur la relève.

--- La date de dégradation ----------------------------------------------------

Une heure est jugée si elle a au moins 30 minutes de collecte ; elle est
dégradée si moins de la moitié de ces minutes ont un tick de la paire.
« Dégradée en continu depuis » : le début de la dernière suite d'heures
jugées toutes dégradées, si elle va jusqu'à la fin de la fenêtre.
"""

from __future__ import annotations

import csv
import io
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.live.etude3 import ConnexionTracee, etat_lecture_seule
from maxprofit.store.chemin_ticks import decoder

JOURS_DEFAUT = 7
JOURS_MAX = 21
SILENCE_MIN = 15
PART_ACTIVE = 0.75
COLLECTE_MIN_JUGEE = 30
SEUIL_DEGRADEE = 0.5
#: Les deux dernières minutes ne sont pas encore toutes écrites.
RETARD_ECRITURE_SEC = 120
RESPIRATION_SEC = 0.02

SANS_COLLECTE, SILENCE, RARE, ACTIVE = (
    "SANS COLLECTE", "SILENCE", "RARE", "ACTIVE")

RESUME_CHEMINS = (
    "SELECT pair, minute_sec / 3600, COUNT(*), SUM(n_ticks) FROM tick_paths "
    "WHERE minute_sec >= ? AND minute_sec < ? GROUP BY pair, minute_sec / 3600")
RESUME_BATTEMENTS = (
    "SELECT ts_sec / 3600, COUNT(*), MAX(n_pairs) FROM uptime "
    "WHERE ts_sec >= ? AND ts_sec < ? GROUP BY ts_sec / 3600")
BATTEMENTS = ("SELECT ts_sec, n_pairs FROM uptime WHERE ts_sec >= ? AND "
              "ts_sec < ? ORDER BY ts_sec")
MINUTES_D_UNE_HEURE = ("SELECT minute_sec FROM tick_paths WHERE pair = ? AND "
                       "minute_sec >= ? AND minute_sec < ? ORDER BY minute_sec")


def _entier(v) -> int | None:
    return None if v is None else int(round(float(v)))


def _utc(ts: float | None, forme: str = "%d/%m %H:%M") -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime(forme)


def analyser_jours(argument: str) -> int:
    texte = (argument or "").strip()
    if not texte:
        return JOURS_DEFAUT
    jours = int(texte)
    if not 1 <= jours <= JOURS_MAX:
        raise ValueError(f"entre 1 et {JOURS_MAX} jours")
    return jours


# --------------------------------------------------------------------------- #
# Lectures
# --------------------------------------------------------------------------- #

def resumes(conn, debut: int, fin: int) -> tuple[dict, dict]:
    """(paire, heure) → (minutes, ticks bruts) ; heure → (battements, paires
    souscrites au plus). Calculés par le serveur."""
    chemins = {(str(p), int(h)): (_entier(n), _entier(t)) for p, h, n, t in
               conn.execute(RESUME_CHEMINS, (debut, fin)).fetchall()}
    battements = {int(h): (_entier(n), _entier(m)) for h, n, m in
                  conn.execute(RESUME_BATTEMENTS, (debut, fin)).fetchall()}
    return chemins, battements


def minutes_de_collecte(conn, debut: int, fin: int
                        ) -> tuple[set[int], dict[int, int]]:
    """Les minutes avec au moins un battement, et le plus grand nombre de
    paires souscrites annoncé dans chaque heure."""
    minutes, souscrites = set(), {}
    for ts, n in conn.execute(BATTEMENTS, (debut, fin)).fetchall():
        minutes.add(int(ts) // 60 * 60)
        h = int(ts) // 3600
        souscrites[h] = max(souscrites.get(h, 0), int(n or 0))
    return minutes, souscrites


# --------------------------------------------------------------------------- #
# L'enquête
# --------------------------------------------------------------------------- #

def historique(lue, active, paires: Sequence[str], *, maintenant: float,
               jours: int = JOURS_DEFAUT, nom_lecture: str = "relève",
               respirer=None) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers CSV). `lue` et `active` sont déjà en lecture
    seule ; `active` peut valoir None (copie non vérifiée)."""
    from maxprofit.store.market import MarketReader
    respirer = respirer or (lambda: time.sleep(RESPIRATION_SEC))
    t0 = time.monotonic()
    tracee = ConnexionTracee(lue)
    reference = ConnexionTracee(active) if active is not None else None
    fin = int(maintenant) // 60 * 60 - RETARD_ECRITURE_SEC
    debut = (fin // 3600 - jours * 24) * 3600
    lecture = {nom_lecture: etat_lecture_seule(tracee)}
    if reference is not None:
        lecture["active"] = etat_lecture_seule(reference)

    # La copie, vérifiée par paire × heure.
    chemins_l, battements_l = resumes(tracee, debut, fin)
    if reference is not None:
        chemins_a, battements_a = resumes(reference, debut, fin)
        heures_div = {h for h in set(battements_l) | set(battements_a)
                      if battements_l.get(h) != battements_a.get(h)}
        pj_div = {k for k in set(chemins_l) | set(chemins_a)
                  if chemins_l.get(k) != chemins_a.get(k)}
    else:
        chemins_a, battements_a, heures_div, pj_div = chemins_l, {}, set(), set()

    # Les minutes de collecte : relève, sauf les heures divergentes.
    collecte, souscrites = minutes_de_collecte(tracee, debut, fin)
    for h in sorted(heures_div):
        collecte = {m for m in collecte if m // 3600 != h}
        souscrites.pop(h, None)
        m_a, s_a = minutes_de_collecte(reference, h * 3600, (h + 1) * 3600)
        collecte |= m_a
        souscrites.update(s_a)

    par_heure_collecte: dict[int, int] = defaultdict(int)
    for m in collecte:
        if debut <= m < fin:
            par_heure_collecte[m // 3600] += 1
    toutes = sorted(set(paires) | {p for p, _ in chemins_l}
                    | {p for p, _ in chemins_a})
    lecteur = MarketReader(tracee)
    heures = list(range(debut // 3600, (fin + 3599) // 3600))
    lignes, silences, bilans = [], [], []
    octets = chemins_lus = 0
    for paire in toutes:
        par_heure: dict[int, dict[str, Any]] = defaultdict(
            lambda: {"minutes": 0, "bruts": 0, "distincts": 0,
                     "premier": None, "dernier": None, "copie": "identique"})
        cotees: set[int] = set()
        for jour in range(debut, fin, 86400):
            for c in lecteur.tick_paths(paire, jour, min(jour + 86400, fin)):
                h = c.minute_sec // 3600
                if (paire, h) in pj_div:
                    continue
                ticks = decoder(c)
                octets += len(c.octets)
                chemins_lus += 1
                x = par_heure[h]
                x["minutes"] += 1
                x["bruts"] += len(ticks)
                x["distincts"] += len({(t.ts_ms, t.price) for t in ticks})
                if ticks:
                    x["premier"] = min(x["premier"] or ticks[0].ts_ms,
                                       ticks[0].ts_ms)
                    x["dernier"] = max(x["dernier"] or 0, ticks[-1].ts_ms)
                cotees.add(c.minute_sec)
            respirer()
        for p, h in sorted(k for k in pj_div if k[0] == paire):
            minutes_a, bruts_a = chemins_a.get((p, h), (0, 0))
            liste = [int(r[0]) for r in reference.execute(
                MINUTES_D_UNE_HEURE, (p, h * 3600, (h + 1) * 3600)).fetchall()]
            par_heure[h] = {"minutes": minutes_a or 0, "bruts": bruts_a or 0,
                            "distincts": None,
                            "premier": (liste[0] * 1000 if liste else None),
                            "dernier": ((liste[-1] + 59) * 1000
                                        if liste else None),
                            "copie": "divergente (reprise de l'active)"}
            cotees |= set(liste)
        fiche = bilan_de_paire(paire, heures, par_heure, par_heure_collecte,
                               collecte, cotees, debut, fin)
        bilans.append(fiche)
        silences += fiche.pop("silences")
        for h in heures:
            x = par_heure.get(h, {"minutes": 0, "bruts": 0, "distincts": 0,
                                  "premier": None, "dernier": None,
                                  "copie": "identique"})
            n_collecte = par_heure_collecte.get(h, 0)
            lignes.append({
                "paire": paire, "heure_utc": _utc(h * 3600, "%Y-%m-%d %H:00"),
                "minutes_collecte": n_collecte,
                "paires_souscrites_max": souscrites.get(h),
                "minutes_avec_tick": x["minutes"],
                "ticks_bruts": x["bruts"], "ticks_distincts": x["distincts"],
                "premier_tick_utc": _utc(x["premier"] and x["premier"] / 1000,
                                         "%Y-%m-%d %H:%M:%S"),
                "dernier_tick_utc": _utc(x["dernier"] and x["dernier"] / 1000,
                                         "%Y-%m-%d %H:%M:%S"),
                "copie": x["copie"],
                "statut": statut_heure(n_collecte, x["minutes"])})
    resume = {
        "debut": debut, "fin": fin, "jours": jours, "lecture": lecture,
        "nom_lecture": nom_lecture, "verifiee": reference is not None,
        "heures_div": len(heures_div), "pj_div": len(pj_div),
        "octets": octets, "chemins": chemins_lus,
        "requetes": dict(tracee.requetes),
        "refusees": tracee.refusees + (reference.refusees if reference
                                       else 0),
        "heures_sans_collecte": sum(1 for h in heures
                                    if not par_heure_collecte.get(h)),
        "heures": len(heures), "duree": time.monotonic() - t0,
    }
    return texte(resume, bilans), fichiers(lignes, silences)


def statut_heure(minutes_collecte: int, minutes_avec_tick: int) -> str:
    if minutes_collecte == 0:
        return SANS_COLLECTE
    if minutes_avec_tick == 0:
        return SILENCE
    return (ACTIVE if minutes_avec_tick >= PART_ACTIVE * minutes_collecte
            else RARE)


def bilan_de_paire(paire: str, heures: Sequence[int],
                   par_heure: Mapping[int, Mapping[str, Any]],
                   par_heure_collecte: Mapping[int, int], collecte: set[int],
                   cotees: set[int], debut: int, fin: int) -> dict[str, Any]:
    """Les heures jugées, la date de dégradation, les silences et reprises."""
    ratios: list[tuple[int, float]] = []
    for h in heures:
        n_collecte = par_heure_collecte.get(h, 0)
        if n_collecte >= COLLECTE_MIN_JUGEE:
            n = (par_heure.get(h) or {}).get("minutes", 0)
            ratios.append((h, min(1.0, n / n_collecte)))
    degradees = [h for h, r in ratios if r < SEUIL_DEGRADEE]
    depuis = None
    if ratios and ratios[-1][1] < SEUIL_DEGRADEE:
        depuis = ratios[-1][0]
        for h, r in reversed(ratios):
            if r >= SEUIL_DEGRADEE:
                break
            depuis = h
    avant = [(h, r) for h, r in ratios if h * 3600 < em.FIN_VALIDATION]

    def part(jeu):
        return (sum(r for _, r in jeu) / len(jeu)) if jeu else None

    silences = trouver_silences(paire, collecte, cotees, debut, fin)
    return {
        "paire": paire,
        "premiere_activite": min(cotees) if cotees else None,
        "derniere_activite": (max(cotees) + 60) if cotees else None,
        "heures_jugees": len(ratios),
        "premiere_degradee": degradees[0] * 3600 if degradees else None,
        "degradee_depuis": depuis * 3600 if depuis is not None else None,
        "degradees_avant_28": sum(1 for _, r in avant if r < SEUIL_DEGRADEE),
        "jugees_avant_28": len(avant),
        "part_24_premieres": part(ratios[:24]),
        "part_24_dernieres": part(ratios[-24:]),
        "silences": silences,
    }


def trouver_silences(paire: str, collecte: set[int], cotees: set[int],
                     debut: int, fin: int) -> list[dict[str, Any]]:
    """Les suites d'au moins 15 minutes sans tick de la paire, avec la part
    de ces minutes où le collecteur tournait (battements)."""
    sortie, courant = [], None
    premiere = (debut + 59) // 60 * 60
    for m in range(premiere, fin, 60):
        if m in cotees:
            if courant is not None:
                sortie.append(courant)
                courant = None
            continue
        if courant is None:
            courant = {"debut": m, "fin": m + 60, "minutes": 0,
                       "sans_battement": 0}
        courant["fin"] = m + 60
        courant["minutes"] += 1
        courant["sans_battement"] += m not in collecte
    en_cours = courant
    if courant is not None:
        sortie.append(courant)
    resultat = []
    for s in sortie:
        if s["minutes"] < SILENCE_MIN:
            continue
        collecte_part = 1 - s["sans_battement"] / s["minutes"]
        resultat.append({
            "paire": paire, "debut_utc": _utc(s["debut"], "%Y-%m-%d %H:%M"),
            "fin_utc": _utc(s["fin"], "%Y-%m-%d %H:%M"),
            "duree_min": s["minutes"],
            "en_cours": s is en_cours,
            "depuis_le_debut_de_la_fenetre": s["debut"] == premiere,
            "minutes_sans_battement": s["sans_battement"],
            "nature": ("silence (collecte continue)" if collecte_part >= 0.95
                       else "sans collecte" if collecte_part <= 0.05
                       else "mixte : collecte interrompue en partie"),
            "reprise": "oui" if s is not en_cours else "non (en cours)",
            "_debut": s["debut"]})
    return resultat


# --------------------------------------------------------------------------- #
# Rapport et fichiers
# --------------------------------------------------------------------------- #

def _pc(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.0f} %"


def texte(resume: Mapping[str, Any], bilans: Sequence[Mapping[str, Any]]
          ) -> str:
    l = [f"📜 <b>Historique des ticks</b> (lecture seule, "
         f"{resume['jours']} jours : du {_utc(resume['debut'])} au "
         f"{_utc(resume['fin'])} UTC)",
         ("- copie de la relève vérifiée heure par heure sur l'active : "
          + ("identique ✅" if not (resume["heures_div"] or resume["pj_div"])
             else f"{resume['pj_div']} paire(s) × heure(s) et "
                  f"{resume['heures_div']} heure(s) de battements "
                  f"divergentes, reprises de l'active")
          if resume["verifiee"] else
          "- copie NON vérifiée : l'active n'a pas répondu ⚠️"),
         f"- lu sur la {resume['nom_lecture']} : {resume['chemins']} minutes "
         f"de chemins ({resume['octets'] / 1e6:.1f} Mo stockés, environ "
         f"{2 * resume['octets'] / 1e6:.0f} Mo sur le fil) ; "
         f"transaction_read_only : "
         + ", ".join(f"{k} {v}" for k, v in resume["lecture"].items())
         + f" ; requêtes refusées : {resume['refusees']} ; durée "
           f"{resume['duree']:.0f} s",
         f"- heures sans aucune collecte (aucun battement) : "
         f"{resume['heures_sans_collecte']}/{resume['heures']}", ""]
    apres, avant, saines = [], [], []
    for b in bilans:
        if b["degradee_depuis"] is None:
            saines.append(b)
        elif b["degradee_depuis"] >= em.FIN_VALIDATION:
            apres.append(b)
        else:
            avant.append(b)
    l.append(f"<b>Dégradation</b> (heure jugée dégradée : ticks dans moins de "
             f"{100 * SEUIL_DEGRADEE:.0f} % des minutes de collecte) : "
             f"{len(apres)} paire(s) dégradée(s) en continu depuis une date "
             f"APRÈS le 28/10, {len(avant)} depuis AVANT, {len(saines)} non "
             f"dégradée(s) à la fin de la fenêtre.")
    for b in sorted(bilans, key=lambda b: (b["degradee_depuis"] is None,
                                           b["degradee_depuis"] or 0,
                                           b["paire"])):
        ligne = f"- {b['paire']} : "
        if b["degradee_depuis"] is not None:
            ligne += (f"dégradée en continu depuis "
                      f"{_utc(b['degradee_depuis'])}")
        else:
            ligne += "active à la fin de la fenêtre"
        ligne += (f" ; minutes cotées : {_pc(b['part_24_premieres'])} sur les "
                  f"24 premières heures jugées, "
                  f"{_pc(b['part_24_dernieres'])} sur les 24 dernières")
        if b["premiere_degradee"] is not None and \
                b["premiere_degradee"] != b["degradee_depuis"]:
            ligne += f" ; 1re heure dégradée {_utc(b['premiere_degradee'])}"
        if b["jugees_avant_28"]:
            ligne += (f" ; avant le 28/10 : {b['degradees_avant_28']}/"
                      f"{b['jugees_avant_28']} heures dégradées")
        ligne += f" ; dernière activité {_utc(b['derniere_activite'])}"
        l.append(ligne)
    l += ["", "Fichiers : historique_ticks_heures.csv (paire × heure : "
          "minutes de collecte, minutes cotées, ticks bruts et distincts, "
          "premier et dernier tick, statut) et historique_ticks_silences.csv "
          "(silences ≥ 15 min, nature, reprise).",
          "Rien n'a été écrit en base."]
    return "\n".join(l)


def _csv(lignes: Iterable[Mapping[str, Any]], colonnes: Sequence[str]
         ) -> bytes:
    sortie = io.StringIO()
    w = csv.DictWriter(sortie, fieldnames=list(colonnes), delimiter=";",
                       extrasaction="ignore")
    w.writeheader()
    for r in lignes:
        w.writerow({c: "" if r.get(c) is None else r[c] for c in colonnes})
    return sortie.getvalue().encode("utf-8")


def fichiers(lignes: Sequence[Mapping[str, Any]],
             silences: Sequence[Mapping[str, Any]]
             ) -> list[tuple[bytes, str, str]]:
    colonnes_h = ["paire", "heure_utc", "minutes_collecte",
                  "paires_souscrites_max", "minutes_avec_tick", "ticks_bruts",
                  "ticks_distincts", "premier_tick_utc", "dernier_tick_utc",
                  "copie", "statut"]
    colonnes_s = ["paire", "debut_utc", "fin_utc", "duree_min", "nature",
                  "minutes_sans_battement", "reprise", "en_cours",
                  "depuis_le_debut_de_la_fenetre"]
    return [
        (_csv(lignes, colonnes_h), "historique_ticks_heures.csv",
         "Paire × heure : collecte, minutes cotées, ticks bruts et "
         "distincts, statut"),
        (_csv(sorted(silences, key=lambda s: (s["paire"], s["_debut"])),
              colonnes_s), "historique_ticks_silences.csv",
         "Silences ≥ 15 min par paire, nature et reprise"),
    ]
