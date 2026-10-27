"""
Étape 3 — `/micro_essai`, version 2 : l'essai technique de la passe
OPTIMISÉE, autorisé le 27/10, en lecture seule
(docs/etape3_plan_execution.md §11).

L'essai fait sur un jour d'EXPLORATION exactement ce que ferait la passe de
sélection, et mesure ce qu'elle coûterait :
- les signaux viennent du rejeu quotidien en mémoire, et leur niveau de zone
  du relevé passif de ce rejeu (`niveaux_en_memoire`) ;
- seules les fenêtres des signaux sont lues (chemins de [d − 900, d), bougies
  d'une minute de plus de chaque côté pour la concordance) ;
- chaque chemin n'est décodé qu'une fois (cache partagé avec l'audit) ;
- la sélection ne calcule que les ticks distincts à k = 0 ; les autres
  variantes ne serviront qu'aux deux hypothèses retenues.

Les vérifications demandées le 27/10 sont faites et publiées :
1. **Temps réel** : durée totale, paires-jours, octets de chemins, pic de
   mémoire (seconde passe sur le paire-jour le plus chargé, pour ne pas
   fausser le temps).
2. **Lecture seule** : toutes les requêtes passent par `ConnexionTracee`, qui
   les compte et refuse ce qui n'est pas une lecture ; sur PostgreSQL,
   `transaction_read_only` est lu au début et à la fin.
3. **Rejeu inchangé** : un paire-jour est rejoué ici avec la stratégie seule
   et comparé au rejeu en mémoire (mêmes signaux, mêmes issues, même
   contexte) ; et le relevé passif ne change rien au rejeu.
4. **Concordance** : une minute inexpliquée, absente ou réécrite exclut le
   signal du niveau 2. Aucune correction, aucune interpolation.
5. **Reproductibilité** : le paire-jour le plus chargé est relu et recalculé ;
   les empreintes des lignes lues et des variables doivent être identiques.

Aucune issue n'entre dans les variables ni dans la fiche CSV de
l'échantillon. Les issues de l'exploration ne servent qu'à la puissance.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import logging
import time
import tracemalloc
from collections import Counter
from datetime import datetime, timezone
from statistics import median
from typing import Any, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.apprentissage import microstructure_pre as mp
from maxprofit.apprentissage.audit_ticks import (
    DOUBLES_MIN, FENETRES_PRIX_MS, HEURE_MS, AuditTicks)
from maxprofit.apprentissage.historique import exemples_historiques
from maxprofit.store.chemin_ticks import decoder

log = logging.getLogger(__name__)

JOUR_PAR_DEFAUT = "2026-10-20"
#: Pour le contrôle du rejeu : le recul de ZoneH1 (300 minutes) et la
#: tendance sur 4 h avant ; les excursions et la confirmation après.
AVANT_SEC = 8 * 3600
APRES_SEC = 2 * 3600
#: Les jours d'une passe de SÉLECTION (exploration et validation) : du début
#: des chemins au 28/10. Sert à l'extrapolation.
JOURS_SELECTION_DEFAUT = 18
BORNE_MINUTES = 30
BORNE_MO = 150
#: Estimation du transfert d'une bougie (neuf colonnes et le protocole).
OCTETS_PAR_BOUGIE = 100
PAIRE_DU_CONTROLE = "EURUSD_otc"


class ConnexionTracee:
    """Toutes les requêtes de l'essai passent ici : comptées, et REFUSÉES si
    elles ne lisent pas. Une empreinte de fichier ne prouve la lecture seule
    que sur SQLite ; sur Neon, c'est ce registre plus `transaction_read_only`
    qui la montrent."""

    PERMIS = frozenset({"SELECT", "WITH", "SHOW"})

    def __init__(self, conn) -> None:
        self.brute = conn
        self.requetes: Counter = Counter()
        self.refusees = 0

    def execute(self, sql: str, *args):
        mot = (sql.lstrip().split(None, 1) or [""])[0].upper()
        if mot not in self.PERMIS:
            self.refusees += 1
            raise PermissionError(f"requête refusée (lecture seule) : {mot}")
        self.requetes[mot] += 1
        return self.brute.execute(sql, *args)


def etat_lecture_seule(conn: ConnexionTracee) -> str:
    """« on » / « off » sur PostgreSQL ; SQLite est ouvert en `mode=ro`."""
    from maxprofit.store import postgres
    if not postgres.est_postgres(conn.brute):
        return "SQLite (mode=ro)"
    ligne = conn.execute(
        "SELECT current_setting('transaction_read_only')").fetchone()
    return str(ligne[0]) if ligne else "inconnu"


def analyser(argument: str, paires: Sequence[str]
             ) -> tuple[tuple[str, ...], int]:
    """`[AAAA-MM-JJ] [PAIRE]` → (paires, début du jour UTC). Sans paire :
    toutes les paires collectées. Le jour doit être dans l'EXPLORATION."""
    jour, choisies = JOUR_PAR_DEFAUT, []
    for mot in argument.split():
        if mot[:4].isdigit() and "-" in mot:
            jour = mot
        else:
            choisies.append(mot)
    for paire in choisies:
        if paires and paire not in paires:
            raise ValueError(f"{paire} n'est pas une paire collectée.")
    try:
        debut = int(datetime.strptime(jour, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp())
    except ValueError as erreur:
        raise ValueError(f"date illisible : {jour}") from erreur
    if debut + 86400 > em.FIN_EXPLORATION:
        raise ValueError("le jour doit être entièrement avant le 21/10 "
                         "(exploration).")
    return tuple(choisies) or tuple(paires), debut


def intervalles(decisions: Sequence[int],
                avant_sec: int = mp.QUALITE_SEC) -> list[tuple[int, int]]:
    """Les fenêtres [d − 900, d) des signaux, fusionnées quand elles se
    touchent : chaque minute n'est lue qu'une fois."""
    sortie: list[list[int]] = []
    for d in sorted(decisions):
        a, b = d - avant_sec, d
        if sortie and a <= sortie[-1][1]:
            sortie[-1][1] = max(sortie[-1][1], b)
        else:
            sortie.append([a, b])
    return [(a, b) for a, b in sortie]


def _empreinte(objet: Any) -> str:
    return hashlib.sha256(json.dumps(objet, sort_keys=True, default=repr)
                          .encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Un paire-jour, comme la passe de sélection le traiterait
# --------------------------------------------------------------------------- #

def paire_jour(conn: ConnexionTracee, course, paire: str, debut: int,
               signaux: Sequence[tuple[int, bool, float | None]]
               ) -> dict[str, Any]:
    """`signaux` : (décision, call, niveau de zone) du rejeu en mémoire."""
    from maxprofit.live.ticks import lire_ouvertures
    from maxprofit.store.market import MarketReader
    lecteur = MarketReader(conn)
    temps: dict[str, float] = {}
    t = time.monotonic()
    chemins: dict[int, Any] = {}
    bougies: dict[int, Any] = {}
    for a, b in intervalles([d for d, _, _ in signaux]):
        for c in lecteur.tick_paths(paire, a, b):
            chemins[c.minute_sec] = c
        for x in lecteur.candles(paire, 60, a - 60, b + 60):
            bougies[x.ts_sec] = x
    ouvertures = lire_ouvertures(conn, paire, debut - mp.QUALITE_SEC,
                                 debut + 86400)
    temps["lecture"] = time.monotonic() - t
    chemins_l = [chemins[m] for m in sorted(chemins)]
    bougies_l = [bougies[m] for m in sorted(bougies)]

    cache: dict[int, Any] = {}

    def decoder_une_fois(c):
        if c.minute_sec not in cache:
            cache[c.minute_sec] = decoder(c)
        return cache[c.minute_sec]

    t = time.monotonic()
    par_minute = {c.minute_sec: decoder_une_fois(c) for c in chemins_l}
    temps["decodage"] = time.monotonic() - t

    t = time.monotonic()
    audit = AuditTicks(debut - mp.QUALITE_SEC, debut + 86400,
                       decoder=decoder_une_fois, garder_classes=True)
    audit.ajouter_paire(paire, bougies_l, chemins_l)
    classes = (audit.classes or {}).get(paire, {})
    fermees = audit._fermees(ouvertures)
    temps["concordance"] = time.monotonic() - t

    t = time.monotonic()
    doublees = {m for m, ticks in par_minute.items() if ticks and
                len(ticks) - len({(x.ts_ms, x.price) for x in ticks})
                >= DOUBLES_MIN * len(ticks)}
    delta = mp.pas_de_cotation(c.echelle for c in chemins_l)
    fiches, lignes, autotests, sans_niveau = [], [], [0, 0], 0
    for d, call, niveau in signaux:
        if niveau is None or delta is None:
            sans_niveau += 1
            continue
        ctx = mp.Contexte(d_sec=d, s=1 if call else -1, niveau=float(niveau),
                          tolerance_pct=course.strategie.p.tolerance_pct,
                          delta=delta)
        q, rangs = mp.ligne_du_signal(
            par_minute, ctx, concordances=classes, fermees=fermees,
            doublees=doublees, troncatures=(0,), traitements=("distincts",))
        for w in mp.FENETRES_SEC:
            autotests[0] += 1
            autotests[1] += mp.autotest_coupure(par_minute, ctx, paire, w)
        minutes = range(d - mp.QUALITE_SEC, d, 60)
        causes = sorted({("absente" if m not in par_minute
                          else str(classes.get(m)))
                         for m in minutes
                         if m not in par_minute or classes.get(m)
                         not in mp.CONCORDANCES_NIVEAU_2})
        fiche = {"paire": paire, "decision": d,
                 "decision_utc": datetime.fromtimestamp(
                     d, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                 "sens": "call" if call else "put",
                 "hors_niveau_2": ", ".join(causes), **q}
        fiches.append(fiche)
        lignes += [{**fiche, **r} for r in rangs]
    temps["variables"] = time.monotonic() - t
    entree = _empreinte(
        [[c.pair, c.minute_sec, c.n_ticks, c.echelle, c.octets.hex()]
         for c in chemins_l]
        + [[x.ts_sec, x.open, x.high, x.low, x.close, x.tick_count,
            x.complete] for x in bougies_l] + [list(o) for o in
                                               (ouvertures or [])])
    return {"paire": paire, "temps": temps, "fiches": fiches,
            "lignes": lignes, "autotests": autotests,
            "sans_niveau": sans_niveau, "chemins": len(chemins_l),
            "octets": sum(len(c.octets) for c in chemins_l),
            "bougies": len(bougies_l), "releves": len(ouvertures or []),
            "concordance": Counter(classes.values()),
            "fermees": len(fermees), "doublees": len(doublees),
            "empreinte_entree": entree,
            "empreinte_sortie": _empreinte(lignes)}


def signaux_du_jour(exemples, niveaux, paire: str, debut: int
                    ) -> list[tuple[int, bool, float | None]]:
    """(décision, call, niveau) des signaux du rejeu en mémoire. Seuls
    l'instant, le sens et le niveau sont lus : pas l'issue."""
    return sorted((e.ts_sec, bool(e.contexte.get("sens_call")),
                   niveaux.get((paire, e.ts_sec)))
                  for e in exemples
                  if e.pair == paire and debut <= e.ts_sec < debut + 86400)


def controle_du_rejeu(conn: ConnexionTracee, course, exemples, paire: str,
                      debut: int) -> dict[str, Any]:
    """Le rejeu d'un paire-jour refait ici, avec la stratégie seule puis avec
    le relevé passif, face au rejeu quotidien en mémoire."""
    from maxprofit.live.plan_demo import dans_la_plage_de_calibration
    from maxprofit.store.market import MarketReader
    t = time.monotonic()
    bougies = MarketReader(conn).candles(paire, 60, debut - AVANT_SEC,
                                         debut + 86400 + APRES_SEC)
    strategie = type(course.strategie)(course.strategie.p)
    tolerance = course.strategie.p.tolerance_pct

    def rejouer(niveaux=None):
        return [e for e in exemples_historiques(
            {paire: bougies}, strategie, echeance_sec=strategie.p.expiry_sec,
            garde=lambda p, f: dans_la_plage_de_calibration(tolerance, f),
            niveaux=niveaux) if debut <= e.ts_sec < debut + 86400]

    def cle(e):
        return (e.ts_sec, e.pair, e.gagne, dict(e.issues),
                dict(e.contexte))

    seul = rejouer()
    avec = rejouer({})
    memoire = sorted((e for e in exemples if e.pair == paire
                      and debut <= e.ts_sec < debut + 86400),
                     key=lambda e: e.ts_sec)
    ecarts = [e.ts_sec for e, m in zip(seul, memoire) if cle(e) != cle(m)]
    return {"paire": paire, "memoire": len(memoire), "recalcul": len(seul),
            "identique": len(seul) == len(memoire) and not ecarts,
            "ecarts": ecarts[:3],
            "passif_sans_effet": [cle(e) for e in seul]
            == [cle(e) for e in avec],
            "temps": time.monotonic() - t}


# --------------------------------------------------------------------------- #
# Disponibilité sur les ordres réels
# --------------------------------------------------------------------------- #

def ordres_reels(ordres: Sequence[dict], ticks_autour) -> dict[str, Any]:
    """Pour chaque ordre accepté, non rapatrié, au décalage d'horloge
    connu : l'instant de décision ramené à l'horloge du broker, son retard
    sur la frontière de minute, et le `prix_attendu` cherché dans le flux
    stocké (règle de l'audit : dernier tick, puis à ± 1, 2 et 5 s)."""
    fiches = []
    for i, o in enumerate(ordres, 1):
        if (not o.get("accepte") or o.get("decalage_broker_ms") is None
                or not o.get("prix_attendu") or o.get("signal_ts_ms") is None
                or "rapatrie_du_broker" in str(o.get("brut") or "")):
            continue
        decalage = int(o["decalage_broker_ms"])
        reste = decalage - round(decalage / HEURE_MS) * HEURE_MS
        instant = int(o["signal_ts_ms"]) + reste
        trouve = AuditTicks._retrouver(o["pair"], instant,
                                       float(o["prix_attendu"]), ticks_autour)
        frontiere = instant // 60_000 * 60_000
        entre = [t for t in ticks_autour(o["pair"], frontiere // 1000,
                                         instant // 1000 + 1)
                 if frontiere <= t.ts_ms < instant]
        fiches.append({
            "id": f"O{i:04d}", "paire": o["pair"],
            "apres_frontiere_ms": instant - frontiere,
            "ticks_depuis_frontiere": len(entre),
            "ticks_autour": trouve["ticks"],
            **{f"trouve_{w}ms": trouve["trouve"][k]
               for k, w in enumerate(FENETRES_PRIX_MS)},
            "ecart_points": trouve["ecart_points"],
            "proche_ms": trouve["proche_ms"]})
    retards = [f["apres_frontiere_ms"] for f in fiches]
    proches = [f["proche_ms"] for f in fiches if f["proche_ms"] is not None]
    return {
        "fiches": fiches, "n": len(fiches),
        "retard_ms": ((min(retards), median(retards), max(retards))
                      if retards else None),
        "proche_ms": median(proches) if proches else None,
        "trouve": {w: sum(1 for f in fiches if f[f"trouve_{w}ms"])
                   for w in FENETRES_PRIX_MS},
    }


# --------------------------------------------------------------------------- #
# Puissance, depuis l'exploration
# --------------------------------------------------------------------------- #

def puissance_exploration(exemples, part_niveau_2: float | None,
                          debut: int = 0) -> dict[str, Any] | None:
    """Issues T0 à 900 s des signaux de [debut, 21/10[ (debut : la première
    minute de ticks) : blocs paire × jour, et jour seul en sensibilité. Rien
    d'après l'exploration n'est lu."""
    gardes = [e for e in exemples
              if debut <= e.ts_sec < em.FIN_EXPLORATION]
    if not gardes:
        return None
    par_paire_jour: dict = {}
    par_jour: dict = {}
    for e in gardes:
        jour = e.ts_sec // 86400
        y = int(bool(e.gagne))
        par_paire_jour.setdefault((e.pair, jour), []).append(y)
        par_jour.setdefault(jour, []).append(y)
    jours = max(1, len(par_jour))
    part = part_niveau_2 if part_niveau_2 else 1.0
    return {"paire_jour": em.puissance(par_paire_jour,
                                       signaux_par_jour=len(gardes) / jours,
                                       part_niveau_2=part),
            "jour": em.puissance(par_jour,
                                 signaux_par_jour=len(gardes) / jours,
                                 part_niveau_2=part),
            "jours": jours, "par_jour": len(gardes) / jours,
            "part_niveau_2": part_niveau_2}


def premiere_minute_de_ticks(conn) -> int | None:
    ligne = conn.execute("SELECT MIN(minute_sec) FROM tick_paths").fetchone()
    return None if not ligne or ligne[0] is None else int(ligne[0])


# --------------------------------------------------------------------------- #
# L'essai
# --------------------------------------------------------------------------- #

ATTENDRE_LE_REJEU = (
    "🔬 Le rejeu quotidien n'est pas encore en mémoire avec les niveaux de "
    "zone : il tourne quelques minutes après chaque démarrage, puis toutes "
    "les 24 h. Relance /micro_essai quand /lecons annonce le nouvel "
    "apprentissage.")


def essai(conn, course, argument: str = "",
          jours_selection: int = JOURS_SELECTION_DEFAUT
          ) -> tuple[str, list]:
    """(rapport, fiches CSV). `conn` est déjà en lecture seule ; toutes les
    requêtes passent en plus par `ConnexionTracee`."""
    from maxprofit.live.ticks import lire_ordres
    from maxprofit.store.market import MarketReader
    tracee = ConnexionTracee(conn)
    paires = tuple(getattr(course, "paires_collectees", ()) or ())
    try:
        retenues, debut = analyser(argument, paires)
    except ValueError as erreur:
        return f"🔬 {html.escape(str(erreur))}", []
    memoire = getattr(course, "rejeu_en_memoire", None)
    niveaux = getattr(course, "niveaux_en_memoire", None)
    if not memoire or niveaux is None:
        return ATTENDRE_LE_REJEU, []
    exemples = memoire[0]
    lecture_debut = etat_lecture_seule(tracee)

    t0 = time.monotonic()
    resultats = [paire_jour(tracee, course, p, debut,
                            signaux_du_jour(exemples, niveaux, p, debut))
                 for p in retenues]
    duree = time.monotonic() - t0
    requetes_passe = sum(tracee.requetes.values())

    # Mémoire et reproductibilité : le paire-jour le plus chargé, relu et
    # recalculé sous `tracemalloc`.
    charge = max(resultats, key=lambda r: len(r["fiches"]))
    tracemalloc.start()
    try:
        relu = paire_jour(tracee, course, charge["paire"], debut,
                          signaux_du_jour(exemples, niveaux, charge["paire"],
                                          debut))
        _, pic = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    reproductible = (relu["empreinte_entree"] == charge["empreinte_entree"]
                     and relu["empreinte_sortie"]
                     == charge["empreinte_sortie"])

    paire_controle = (PAIRE_DU_CONTROLE if PAIRE_DU_CONTROLE in retenues
                      else charge["paire"])
    rejeu = controle_du_rejeu(tracee, course, exemples, paire_controle, debut)
    ordres = ordres_reels(lire_ordres(tracee), MarketReader(tracee).ticks)
    fiches = [f for r in resultats for f in r["fiches"]]
    part = (sum(1 for f in fiches if f["E8_niveau"] == 2) / len(fiches)
            if fiches else None)
    puis = puissance_exploration(exemples, part,
                                 premiere_minute_de_ticks(tracee) or 0)
    lecture_fin = etat_lecture_seule(tracee)
    resume = {
        "debut": debut, "paires_jours": len(resultats), "duree": duree,
        "requetes_passe": requetes_passe, "pic": pic,
        "charge": charge["paire"], "reproductible": reproductible,
        "empreintes": (charge["empreinte_entree"][:12],
                       charge["empreinte_sortie"][:12]),
        "lecture": (lecture_debut, lecture_fin),
        "requetes": dict(tracee.requetes), "refusees": tracee.refusees,
        "jours_selection": jours_selection, "n_paires": len(paires) or 20,
    }
    return (texte(resume, resultats, rejeu, ordres, puis),
            fichiers(resultats, ordres))


# --------------------------------------------------------------------------- #
# Rapport
# --------------------------------------------------------------------------- #

def _s(x: float) -> str:
    return f"{x:.1f} s" if x >= 1 else f"{1000 * x:.0f} ms"


def _pc(n: int, total: int) -> str:
    return f"{n}/{total} ({100 * n / total:.0f} %)" if total else "—"


def _ok(b: bool) -> str:
    return "✅" if b else "⚠️"


def texte(resume: dict, resultats: Sequence[dict], rejeu: dict,
          ordres: dict, puis: dict | None) -> str:
    jour = datetime.fromtimestamp(resume["debut"], timezone.utc).strftime(
        "%d/%m/%Y")
    n_pj = resume["paires_jours"]
    fiches = [f for r in resultats for f in r["fiches"]]
    n = len(fiches)
    temps: Counter = Counter()
    for r in resultats:
        temps.update(r["temps"])
    octets = sum(r["octets"] for r in resultats)
    bougies = sum(r["bougies"] for r in resultats)
    chemins = sum(r["chemins"] for r in resultats)
    a_faire = resume["jours_selection"] * resume["n_paires"]
    par_pj = resume["duree"] / n_pj if n_pj else 0.0
    minutes = par_pj * a_faire / 60
    transfert = (octets + bougies * OCTETS_PAR_BOUGIE) / n_pj * a_faire \
        if n_pj else 0
    pic_mo = resume["pic"] / 1e6
    dans_les_bornes = minutes <= BORNE_MINUTES and pic_mo <= BORNE_MO
    l = ["🔬 <b>Étape 3 — essai technique de la passe optimisée</b> "
         "(lecture seule)",
         f"Jour d'exploration : {jour} UTC, {n_pj} paire(s). Signaux et "
         f"niveaux de zone : rejeu quotidien en mémoire. Aucune issue lue "
         f"pour les variables.", "",
         "<b>1. Temps réel</b>",
         f"- durée totale : {_s(resume['duree'])} pour {n_pj} paires-jours, "
         f"{n} signaux ({_s(par_pj)} par paire-jour)",
         "- par étape : " + ", ".join(f"{k} {_s(v)}" for k, v in temps.items()),
         f"- lu : {chemins} chemins ({octets / 1e6:.2f} Mo exacts), "
         f"{bougies} bougies (~{bougies * OCTETS_PAR_BOUGIE / 1e6:.2f} Mo "
         f"estimés), {resume['requetes_passe']} requêtes",
         f"- mémoire au plus haut : {pic_mo:.0f} Mo (paire-jour le plus "
         f"chargé, {html.escape(resume['charge'])})",
         f"- extrapolation à {resume['jours_selection']} jours × "
         f"{resume['n_paires']} paires : ~{minutes:.0f} min, "
         f"~{transfert / 1e6:.0f} Mo de transfert. Bornes : "
         f"{BORNE_MINUTES} min, {BORNE_MO} Mo → "
         + ("dans les bornes ✅" if dans_les_bornes else
            "HORS BORNES ⚠️ : suspendre et diagnostiquer"), "",
         "<b>2. Lecture seule</b>",
         f"- transaction_read_only : {html.escape(resume['lecture'][0])} au "
         f"début, {html.escape(resume['lecture'][1])} à la fin",
         "- requêtes de tout l'essai : " + ", ".join(f"{v} {k}" for k, v in
                                     sorted(resume["requetes"].items()))
         + f" ; refusées : {resume['refusees']} "
         + _ok(not resume["refusees"]
               and set(resume["requetes"]) <= {"SELECT", "WITH", "SHOW"}),
         "", "<b>3. Rejeu inchangé</b>",
         f"- {html.escape(rejeu['paire'])} : {rejeu['memoire']} signaux en "
         f"mémoire, {rejeu['recalcul']} rejoués ici ; mêmes décisions, "
         f"issues et contexte : {'oui' if rejeu['identique'] else 'NON'} "
         f"{_ok(rejeu['identique'])}"
         + (f" (écarts : {rejeu['ecarts']})" if rejeu["ecarts"] else ""),
         f"- le relevé du niveau de zone ne change rien au rejeu : "
         f"{'oui' if rejeu['passif_sans_effet'] else 'NON'} "
         f"{_ok(rejeu['passif_sans_effet'])} (contrôle hors mesure : "
         f"{_s(rejeu['temps'])})", "",
         "<b>4. Concordance</b>"]
    conc: Counter = Counter()
    for r in resultats:
        conc.update(r["concordance"])
    l.append("- minutes lues : " + ", ".join(
        f"{html.escape(str(k))} {v}" for k, v in conc.most_common()))
    hors = [f for f in fiches if f["E8_niveau"] != 2]
    causes = Counter(c.strip() for f in hors
                     for c in f["hors_niveau_2"].split(",") if c.strip())
    l.append(f"- niveau 2 : {_pc(n - len(hors), n)}"
             + (" ; exclus pour : " + ", ".join(
                 f"{html.escape(k)} {v}" for k, v in causes.most_common())
                if hors else ""))
    l.append("- aucune correction, aucune interpolation : une minute "
             "inexpliquée, absente ou réécrite exclut le signal du niveau 2.")
    l += ["", "<b>5. Reproductibilité</b>",
          f"- {html.escape(resume['charge'])} relu et recalculé : lignes lues "
          f"et variables identiques : "
          f"{'oui' if resume['reproductible'] else 'NON'} "
          f"{_ok(resume['reproductible'])} (empreintes "
          f"{resume['empreintes'][0]} / {resume['empreintes'][1]})", "",
          "<b>6. Qualité et anti-fuite</b>"]
    classes = Counter(f["classe"] for f in fiches)
    l.append("- fenêtres : " + ", ".join(
        f"{k} {_pc(v, n)}" for k, v in sorted(classes.items())))
    sans = sum(r["sans_niveau"] for r in resultats)
    if sans:
        l.append(f"- signaux sans niveau de zone : {sans} (écartés)")
    a = [sum(r["autotests"][0] for r in resultats),
         sum(r["autotests"][1] for r in resultats)]
    l.append(f"- ticks ajoutés après la coupure sans effet : {_pc(a[1], a[0])}"
             f" {_ok(a[0] > 0 and a[0] == a[1])}")
    l += ["", "<b>7. Ordres réels face au flux stocké</b>"]
    if not ordres["n"]:
        l.append("- aucun ordre comparable.")
    else:
        k = ordres["n"]
        bas, med, haut = ordres["retard_ms"]
        l.append(f"- {k} ordres ; décision {bas} / {med:.0f} / {haut} ms après "
                 f"la frontière de minute ; prix vu = dernier tick stocké : "
                 f"{_pc(ordres['trouve'][0], k)} ; présent à ± 1 s : "
                 f"{_pc(ordres['trouve'][1000], k)} ; à ± 5 s : "
                 f"{_pc(ordres['trouve'][5000], k)} ; tick de même prix le "
                 f"plus proche : {ordres['proche_ms']:.0f} ms (médiane)")
    l += ["", "<b>8. Puissance</b> (2 hypothèses, α = 0,025 unilatéral, "
          "plafond de 4 000 signaux)"]
    if puis is None:
        l.append("- aucun signal d'exploration en mémoire.")
    else:
        r = puis["paire_jour"]
        part = puis["part_niveau_2"]
        l.append(f"- {r['n']} signaux avec issue en {puis['jours']} jours "
                 f"(~{puis['par_jour']:.0f}/jour), {r['blocs']} blocs ; "
                 f"corrélation intra-bloc {r['icc']:.3f}, effet de plan "
                 f"{r['deff']:.2f} ; part du niveau 2 "
                 + (f"{100 * part:.0f} % ({n} signaux)" if part else
                    "inconnue"))
        for x in r["lignes"]:
            jours = f", ~{x['jours']:.0f} jours" if x["jours"] else ""
            l.append(f"  · N = {x['N']} : effet détectable "
                     f"{100 * x['effet_min']:.1f} points (demi-IC 95 % "
                     f"{100 * x['demi_ic95']:.1f}){jours}")
        l.append("- seuils des regards : " + ", ".join(
            f"N {s['N']} → {s['nominal']:.2g}"
            for s in em.seuils_des_regards()))
    l += ["", "Rien n'a été écrit. La passe complète reste suspendue : coût "
          "réel, protocole pré-inscrit et continuité du stockage d'abord."]
    return "\n".join(l)


def _csv(lignes: Sequence[dict], colonnes: Sequence[str]) -> bytes:
    sortie = io.StringIO()
    w = csv.DictWriter(sortie, fieldnames=list(colonnes), delimiter=";",
                       extrasaction="ignore")
    w.writeheader()
    for r in lignes:
        w.writerow({c: ("" if r.get(c) is None else
                        (round(r[c], 6) if isinstance(r[c], float) else r[c]))
                    for c in colonnes})
    return sortie.getvalue().encode("utf-8")


def fichiers(resultats: Sequence[dict], ordres: dict
             ) -> list[tuple[bytes, str, str]]:
    colonnes = ["paire", "decision", "decision_utc", "sens", *mp.QUALITE,
                "classe", "hors_niveau_2", "traitement", "k", "w", "n",
                *mp.VARIABLES]
    lignes = [x for r in resultats for x in r["lignes"]]
    sortie = [(_csv(lignes, colonnes), "micro_essai.csv",
               "Essai : qualité et variables (distincts, k = 0) par signal × "
               "fenêtre, sans issue")]
    if ordres["fiches"]:
        sortie.append((_csv(ordres["fiches"], list(ordres["fiches"][0])),
                       "ordres_disponibilite.csv",
                       "Ordres réels : prix vu par la course face au flux "
                       "stocké"))
    return sortie
