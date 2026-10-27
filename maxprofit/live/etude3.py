"""
Étape 3 — `/micro_essai` : les contrôles préalables autorisés le 27/10,
en lecture seule, AVANT toute passe complète (docs/etape3_plan_execution.md
§11).

Trois contrôles, aucun lien variable–issue :
1. **Échantillon** d'une paire × un jour d'EXPLORATION : coût (lignes,
   octets, temps, mémoire), couverture, concordance, classes de fenêtre,
   part du niveau 2, valeurs manquantes, et le contrôle anti-fuite rejoué
   sur chaque vraie fenêtre. Les variables sont calculées ; les issues de
   l'échantillon ne sont PAS lues.
2. **Disponibilité sur les ordres réels** : le dernier prix que la course
   avait en main au moment de décider (`prix_attendu`, pris dans le tampon
   vivant) est-il dans le flux stocké, au même instant ? Et combien de
   temps après la frontière de minute la course décide-t-elle ?
3. **Puissance**, depuis les seules issues de l'exploration (avant le
   21/10) : taux de base, corrélation intra-bloc, effet minimal détectable
   à chaque regard, et seuils nominaux de la règle de poursuite.

Rien n'est écrit : la connexion est passée en lecture seule par l'appelant
(`diagnostic_stockage.lecture_seule`), et ce module ne lance que les
lectures de `MarketReader` et de `live.ticks`.
"""

from __future__ import annotations

import csv
import html
import io
import logging
import time
import tracemalloc
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

PAIRE_PAR_DEFAUT = "EURUSD_otc"
JOUR_PAR_DEFAUT = "2026-10-20"
#: Le recul de ZoneH1 (300 minutes) et une marge : les signaux du premier
#: quart d'heure du jour ont leur fenêtre entière.
AVANT_SEC = 6 * 3600
#: Les jours couverts par une passe complète de SÉLECTION (exploration et
#: validation) : du début des chemins au 28/10. Sert à l'extrapolation.
JOURS_SELECTION_DEFAUT = 18


class _Enregistreuse:
    """La stratégie de la course, dont on retient le `niveau` de chaque
    signal (il n'est pas dans le contexte du rejeu)."""

    def __init__(self, strategie) -> None:
        self._s = strategie
        self.p = strategie.p
        self.features: dict[int, dict] = {}

    def on_bar(self, vue):
        signal = self._s.on_bar(vue)
        if signal is not None:
            self.features[vue.now_ms // 1000] = dict(signal.features or {})
        return signal


def analyser(argument: str, paires: Sequence[str]) -> tuple[str, int]:
    """`[PAIRE] [AAAA-MM-JJ]` → (paire, début du jour UTC). Le jour doit
    être dans l'EXPLORATION : l'échantillon ne touche ni la validation ni ce
    qui suit."""
    paire, jour = PAIRE_PAR_DEFAUT, JOUR_PAR_DEFAUT
    for mot in argument.split():
        if mot[:4].isdigit() and "-" in mot:
            jour = mot
        else:
            paire = mot
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
    return paire, debut


# --------------------------------------------------------------------------- #
# 1. Échantillon
# --------------------------------------------------------------------------- #

def _signaux(course, bougies, paire: str, debut: int, fin: int):
    """(signaux du jour : (décision, call, features, issue), exclusions)."""
    from maxprofit.live.plan_demo import dans_la_plage_de_calibration
    strategie = _Enregistreuse(type(course.strategie)(course.strategie.p))
    tolerance = course.strategie.p.tolerance_pct
    sans_issue: list[dict] = []
    exemples = exemples_historiques(
        {paire: bougies}, strategie, echeance_sec=strategie.p.expiry_sec,
        garde=lambda p, f: dans_la_plage_de_calibration(tolerance, f),
        sans_issue=sans_issue)
    signaux = []
    for e in exemples:
        signaux.append((e.ts_sec, bool(e.contexte.get("sens_call")),
                        "avec issue"))
    for r in sans_issue:
        signaux.append((r["ts_sec"], r["call"], r["issue"]))
    return sorted((d, call, strategie.features.get(d, {}), motif)
                  for d, call, motif in signaux if debut <= d < fin)


def _calculer(course, paire: str, debut: int, bougies, chemins, ouvertures,
              temps: dict[str, float]) -> dict[str, Any]:
    """Décodage, concordance, signaux et variables, sur des lignes déjà
    lues ; `temps` reçoit la durée de chaque étape."""
    fin = debut + 86400
    t = time.monotonic()
    par_minute = {c.minute_sec: decoder(c) for c in chemins}
    temps["decodage"] = time.monotonic() - t

    t = time.monotonic()
    audit = AuditTicks(debut - mp.QUALITE_SEC, fin, decoder=decoder,
                       garder_classes=True)
    audit.ajouter_paire(paire, bougies, chemins)
    classes = (audit.classes or {}).get(paire, {})
    fermees = audit._fermees(ouvertures)
    temps["concordance"] = time.monotonic() - t

    t = time.monotonic()
    signaux = _signaux(course, bougies, paire, debut, fin)
    temps["signaux"] = time.monotonic() - t

    t = time.monotonic()
    doublees = {m for m, ticks in par_minute.items() if ticks and
                len(ticks) - len({(x.ts_ms, x.price) for x in ticks})
                >= DOUBLES_MIN * len(ticks)}
    delta = mp.pas_de_cotation(c.echelle for c in chemins)
    lignes, fiches, autotests = [], [], [0, 0]
    for d, call, features, motif in signaux:
        niveau = features.get("niveau")
        if niveau is None or delta is None:
            continue
        ctx = mp.Contexte(d_sec=d, s=1 if call else -1, niveau=float(niveau),
                          tolerance_pct=course.strategie.p.tolerance_pct,
                          delta=delta)
        q, rangs = mp.ligne_du_signal(par_minute, ctx, concordances=classes,
                                      fermees=fermees, doublees=doublees)
        for w in mp.FENETRES_SEC:
            autotests[0] += 1
            autotests[1] += mp.autotest_coupure(par_minute, ctx, paire, w)
        fiche = {"paire": paire, "decision": d,
                 "decision_utc": datetime.fromtimestamp(
                     d, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                 "sens": "call" if call else "put", "motif": motif, **q}
        fiches.append(fiche)
        lignes += [{**fiche, **r} for r in rangs]
    temps["variables"] = time.monotonic() - t
    return {"par_minute": par_minute, "audit": audit, "fermees": fermees,
            "doublees": doublees, "signaux": signaux, "fiches": fiches,
            "lignes": lignes, "autotests": autotests}


def echantillon(conn, course, paire: str, debut: int) -> dict[str, Any]:
    """L'échantillon d'une paire × un jour. Ne lit aucune issue : les
    signaux sont énumérés par le rejeu, mais seul leur motif d'exclusion
    (égalité, bougie absente) est gardé, pas leur gain.

    Deux passes sur les mêmes lignes lues : la première chronométrée, la
    seconde sous `tracemalloc` pour la mémoire (le suivi ralentit le calcul
    et fausserait le temps)."""
    from maxprofit.live.ticks import lire_ouvertures
    from maxprofit.store.market import MarketReader
    fin = debut + 86400
    lecteur = MarketReader(conn)
    temps: dict[str, float] = {}
    t = time.monotonic()
    bougies = lecteur.candles(paire, 60, debut - AVANT_SEC,
                              fin + em.HORIZON_SEC + 120)
    chemins = lecteur.tick_paths(paire, debut - mp.QUALITE_SEC, fin)
    ouvertures = lire_ouvertures(conn, paire, debut - mp.QUALITE_SEC, fin)
    temps["lecture"] = time.monotonic() - t
    c = _calculer(course, paire, debut, bougies, chemins, ouvertures, temps)
    tracemalloc.start()
    try:
        _calculer(course, paire, debut, bougies, chemins, ouvertures, {})
        _, pic = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    fiches, lignes = c["fiches"], c["lignes"]
    reference = [r for r in lignes if r["traitement"] == "distincts"
                 and r["k"] == 0 and r["w"] == 900]
    manquantes = {v: sum(1 for r in reference if r.get(v) is None)
                  for v in mp.VARIABLES}
    return {
        "paire": paire, "debut": debut, "temps": temps, "pic_octets": pic,
        "lignes_lues": {"bougies": len(bougies), "chemins": len(chemins),
                        "releves": len(ouvertures or [])},
        "octets_chemins": sum(len(x.octets) for x in chemins),
        "minutes": {"attendues": 1440 + mp.QUALITE_SEC // 60,
                    "presentes": len(c["par_minute"]),
                    "fermees": len(c["fermees"]),
                    "doublees": len(c["doublees"])},
        "concordance": dict(c["audit"].concordance),
        "signaux": len(c["signaux"]),
        "motifs": _compte(x[3] for x in c["signaux"]),
        "fiches": fiches, "lignes": lignes,
        "classes": _compte(f["classe"] for f in fiches),
        "niveau_2": sum(1 for f in fiches if f["E8_niveau"] == 2),
        "manquantes": manquantes, "autotests": c["autotests"],
    }


def _compte(valeurs) -> dict:
    sortie: dict = {}
    for v in valeurs:
        sortie[v] = sortie.get(v, 0) + 1
    return sortie


# --------------------------------------------------------------------------- #
# 2. Disponibilité sur les ordres réels
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
    return {
        "fiches": fiches, "n": len(fiches),
        "retard_ms": ((min(retards), median(retards), max(retards))
                      if retards else None),
        "trouve": {w: sum(1 for f in fiches if f[f"trouve_{w}ms"])
                   for w in FENETRES_PRIX_MS},
        "sans_tick": sum(1 for f in fiches if not f["ticks_autour"]),
    }


# --------------------------------------------------------------------------- #
# 3. Puissance, depuis l'exploration
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
            "part_niveau_2": part_niveau_2,
            "premier": min(e.ts_sec for e in gardes)}


def premiere_minute_de_ticks(conn) -> int | None:
    ligne = conn.execute("SELECT MIN(minute_sec) FROM tick_paths").fetchone()
    return None if not ligne or ligne[0] is None else int(ligne[0])


def exemples_exploration(conn, course, paires: Sequence[str],
                         debut: int) -> list:
    """Le rejeu de l'exploration, quand celui de la course n'est pas en
    mémoire : bougies de chaque paire de `debut` au 21/10."""
    from maxprofit.live.plan_demo import dans_la_plage_de_calibration
    from maxprofit.store.market import MarketReader
    lecteur = MarketReader(conn)
    strategie = type(course.strategie)(course.strategie.p)
    tolerance = course.strategie.p.tolerance_pct
    sortie = []
    for paire in paires:
        bougies = lecteur.candles(paire, 60, debut - AVANT_SEC,
                                  em.FIN_EXPLORATION + em.HORIZON_SEC + 120)
        sortie += [e for e in exemples_historiques(
            {paire: bougies}, strategie, echeance_sec=strategie.p.expiry_sec,
            garde=lambda p, f: dans_la_plage_de_calibration(tolerance, f),
            respirer=lambda: time.sleep(0.05))
            if debut <= e.ts_sec < em.FIN_EXPLORATION]
    return sortie


# --------------------------------------------------------------------------- #
# Le tout
# --------------------------------------------------------------------------- #

def essai(conn, course, argument: str = "") -> tuple[str, list]:
    """(rapport, fiches CSV). `conn` est déjà en lecture seule."""
    from maxprofit.live.ticks import lire_ordres
    from maxprofit.store.market import MarketReader
    paires = tuple(getattr(course, "paires_collectees", ()) or ())
    try:
        paire, debut = analyser(argument, paires)
    except ValueError as erreur:
        return f"🔬 {html.escape(str(erreur))}", []
    ech = echantillon(conn, course, paire, debut)
    lecteur = MarketReader(conn)
    ordres = ordres_reels(lire_ordres(conn), lecteur.ticks)
    part = (ech["niveau_2"] / len(ech["fiches"])) if ech["fiches"] else None
    premiere = premiere_minute_de_ticks(conn)
    memoire = getattr(course, "rejeu_en_memoire", None)
    if premiere is None:
        exemples, source = [], "aucun chemin de ticks"
    elif memoire:
        exemples, source = memoire[0], "rejeu en mémoire"
    else:
        t = time.monotonic()
        exemples = exemples_exploration(conn, course, paires, premiere)
        source = f"rejeu de l'exploration ({time.monotonic() - t:.0f} s)"
    puis = puissance_exploration(exemples, part, premiere or 0)
    return (texte(ech, ordres, puis, source, n_paires=len(paires) or 20),
            fichiers(ech, ordres))


def _s(x: float) -> str:
    return f"{x:.1f} s" if x >= 1 else f"{1000 * x:.0f} ms"


def _pc(n: int, total: int) -> str:
    return f"{n}/{total} ({100 * n / total:.0f} %)" if total else "—"


def texte(ech: dict, ordres: dict, puis: dict | None, source: str,
          jours_selection: int = JOURS_SELECTION_DEFAUT,
          n_paires: int = 20) -> str:
    jour = datetime.fromtimestamp(ech["debut"], timezone.utc).strftime(
        "%d/%m/%Y")
    total_temps = sum(ech["temps"].values())
    paires_jours = jours_selection * n_paires
    l = ["🔬 <b>Étape 3 — contrôles préalables</b> (lecture seule)",
         f"Échantillon : {html.escape(ech['paire'])}, {jour} UTC "
         f"(exploration). Aucune issue de l'échantillon n'est lue.", "",
         "<b>1. Coût</b>",
         f"- lu : {ech['lignes_lues']['bougies']} bougies, "
         f"{ech['lignes_lues']['chemins']} chemins "
         f"({ech['octets_chemins'] / 1e6:.2f} Mo compressés), "
         f"{ech['lignes_lues']['releves']} relevés de payout",
         "- temps : " + ", ".join(f"{k} {_s(v)}"
                                  for k, v in ech["temps"].items())
         + f" ; total {_s(total_temps)}",
         f"- mémoire Python au plus haut : {ech['pic_octets'] / 1e6:.0f} Mo",
         f"- extrapolation à {jours_selection} jours × {n_paires} paires "
         f"({paires_jours} paires-jours) : ~{total_temps * paires_jours / 60:.0f}"
         f" min de calcul, ~{ech['octets_chemins'] * paires_jours / 1e9:.2f} "
         f"Go de chemins à transférer (plus les bougies). Borne du plan : "
         f"30 min, 150 Mo.", "",
         "<b>2. Couverture et qualité</b>"]
    m = ech["minutes"]
    l.append(f"- minutes de ticks : {_pc(m['presentes'], m['attendues'])} ; "
             f"fermées chez le broker : {m['fermees']} ; à flux doublé : "
             f"{m['doublees']}")
    l.append("- concordance des minutes : " + ", ".join(
        f"{html.escape(str(k))} {v}" for k, v in
        sorted(ech["concordance"].items(), key=lambda kv: -kv[1])))
    n = len(ech["fiches"])
    l.append(f"- signaux du jour : {ech['signaux']} ("
             + ", ".join(f"{k} {v}" for k, v in ech["motifs"].items())
             + f") ; avec un niveau de zone : {n}")
    l.append("- fenêtres : " + ", ".join(
        f"{k} {_pc(v, n)}" for k, v in sorted(ech["classes"].items())))
    l.append(f"- niveau 2 : {_pc(ech['niveau_2'], n)}")
    lourdes = [f"{v} {c}" for v, c in ech["manquantes"].items()
               if n and c > 0.2 * n]
    l.append("- variables manquantes > 20 % (w = 900 s, distincts) : "
             + (", ".join(lourdes) if lourdes else "aucune"))
    a = ech["autotests"]
    l += ["", "<b>3. Anti-fuite sur les vraies fenêtres</b>",
          f"- ticks ajoutés après la coupure sans effet : "
          f"{_pc(a[1], a[0])}" + (" ✅" if a[0] and a[0] == a[1] else " ⚠️")]
    l += ["", "<b>4. Disponibilité : ordres réels face au flux stocké</b>"]
    if not ordres["n"]:
        l.append("- aucun ordre comparable.")
    else:
        bas, med, haut = ordres["retard_ms"]
        k = ordres["n"]
        l.append(f"- {k} ordres ; décision après la frontière de minute : "
                 f"{bas} / {med:.0f} / {haut} ms (min / médiane / max)")
        l.append(f"- prix vu par la course = dernier tick stocké au même "
                 f"instant : {_pc(ordres['trouve'][0], k)} ; présent dans le "
                 f"flux à ± 1 s : {_pc(ordres['trouve'][1000], k)}, ± 2 s : "
                 f"{_pc(ordres['trouve'][2000], k)}, ± 5 s : "
                 f"{_pc(ordres['trouve'][5000], k)}")
        l.append("- lecture : l'instant est ramené à l'horloge du broker par "
                 "le décalage mesuré ; une erreur de quelques centaines de "
                 "ms sur ce décalage suffit à changer le « dernier tick ». "
                 "Un prix absent à ± 5 s serait, lui, une divergence.")
    l += ["", "<b>5. Puissance, depuis l'exploration</b> "
          f"({html.escape(source, quote=False)})"]
    if puis is None:
        l.append("- aucun signal d'exploration disponible.")
    else:
        r = puis["paire_jour"]
        l.append(f"- {r['n']} signaux avec issue en {puis['jours']} jours "
                 f"(~{puis['par_jour']:.0f}/jour), {r['blocs']} blocs paire × "
                 f"jour ; taux de gain {100 * r['p']:.1f} %")
        l.append(f"- corrélation intra-bloc {r['icc']:.3f} (paire × jour), "
                 f"{puis['jour']['icc']:.3f} (jour) ; effet de plan "
                 f"{r['deff']:.2f}")
        part = puis["part_niveau_2"]
        l.append("- part du niveau 2 retenue : "
                 + (f"{100 * part:.0f} % (UN jour d'UNE paire : grossier)"
                    if part else "inconnue (100 % supposés)"))
        l.append("- effet minimal détectable (tercile contre le reste, "
                 "α = 0,0125 unilatéral, puissance 80 %) :")
        for x in r["lignes"]:
            jours = f", ~{x['jours']:.0f} jours" if x["jours"] else ""
            l.append(f"  · N = {x['N']} : {100 * x['effet_min']:.1f} points "
                     f"(demi-IC 95 % {100 * x['demi_ic95']:.1f}){jours}")
        jc, jr = r["jours_pour_confirmer"], r["jours_pour_rejeter"]
        l.append(f"- +2 points : {r['n_pour_confirmer']} signaux pour le "
                 f"détecter" + (f" (~{jc:.0f} jours)" if jc else "")
                 + f", {r['n_pour_rejeter']} pour l'exclure"
                 + (f" (~{jr:.0f} jours)" if jr else ""))
        l.append("- seuils de la règle de poursuite (famille de Holm) : "
                 + ", ".join(f"N {s['N']} → {s['nominal']:.2g}"
                             for s in em.seuils_des_regards()))
    l += ["", "Rien n'a été écrit. La passe complète n'est pas lancée : elle "
          "attend ton feu vert, après ces contrôles."]
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


def fichiers(ech: dict, ordres: dict) -> list[tuple[bytes, str, str]]:
    colonnes = (["paire", "decision", "decision_utc", "sens", "motif",
                 *mp.QUALITE, "classe",
                 "traitement", "k", "w", "n", *mp.VARIABLES])
    sortie = [(_csv(ech["lignes"], colonnes), "micro_essai.csv",
               "Échantillon : qualité et variables par signal × traitement "
               "× k × w (sans issue)")]
    if ordres["fiches"]:
        sortie.append((_csv(ordres["fiches"], list(ordres["fiches"][0])),
                       "ordres_disponibilite.csv",
                       "Ordres réels : prix vu par la course face au flux "
                       "stocké"))
    return sortie
