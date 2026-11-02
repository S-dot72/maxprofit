"""
`/rapprochement` : chaque signal ZoneH1 du rejeu, le PROXY des études, face à
l'ordre que la course a réellement passé. En LECTURE SEULE, toutes paires.

Demandé le 02/11 : « savoir si les performances que nous avons mesurées
représentent réellement ce que la course exécute ». Ni la course, ni ses
règles d'entrée, ni ses expirations ne sont touchées.

--- Pourquoi une empreinte, et pas l'heure ------------------------------------

Aucun identifiant n'est commun à un signal du rejeu, désigné par (paire,
décision), et à une ligne du journal :
- `signal_ts_ms` est l'instant du PLACEMENT (`courtier.placer`), pas la
  bougie du signal ;
- la fin de la bougie du signal n'est pas enregistrée.

La seule trace du signal dans l'ordre est son CONTEXTE, `brut["contexte"]`.
La course le mesure à la décision avec `apprentissage.contexte.contexte`. Le
rejeu applique la même fonction :
- sur la bougie du signal, pour l'entrée immédiate ;
- sur la bougie de confirmation, pour l'entrée confirmée (clés « e_ »).
Une vingtaine de grandeurs arrondies à 4 décimales forment une empreinte :
deux signaux différents ne la partagent pas.

Règle d'association, appliquée dans cet ordre (jamais la seule proximité) :
1. même paire et même sens ;
2. un ordre placé dans le CRÉNEAU qu'impose la course : de la fin de la
   bougie d'entrée à 90 s plus tard (fraîcheur), plus une marge de
   placement. La bougie d'entrée est celle du signal, ou la k-ième suivante
   si la confirmation M1 attend k bougies (k lu dans le rejeu) ;
3. l'empreinte : le contexte de l'ordre comparé à celui du rejeu sur la même
   bougie d'entrée.

Classes de correspondance :
- **exacte** : empreinte identique, créneau respecté, unique des deux côtés ;
- **approximative** : empreinte proche (les bougies du direct et de la base
  diffèrent un peu), ou identique mais hors du créneau ; unique ;
- **ambiguë** : plusieurs candidats vérifiés, ou des candidats sans
  empreinte. Un ordre refusé, non envoyé ou rapatrié n'a pas de contexte :
  la course ne l'écrit qu'à l'acceptation. Une ambiguïté n'est jamais
  forcée ;
- **absente** : aucun ordre candidat. Ces signaux restent dans le rapport,
  avec un motif PROBABLE tiré des règles de la course.

--- Ce qui est réel, ce qui est proxy -----------------------------------------

Entrée, règlement, payout et résultat réels viennent UNIQUEMENT des champs du
broker : `openPrice`, `closePrice`, `percentProfit`, `profit`, horodatages
d'ouverture et de clôture. Aucune bougie M1 n'en tient lieu. Un champ absent
est compté comme une limite, jamais reconstruit.

--- Bornes ---------------------------------------------------------------------

Les décisions s'arrêtent au 03/11 00:00 UTC, ouverture de la confirmation du
gel 2 : ses issues ne sont pas lues avant ses regards. Sorties en CSV
seulement, aucune écriture en base.
"""

from __future__ import annotations

import html
import json
import logging
import random
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.apprentissage import microstructure_pre as mp
from maxprofit.apprentissage.contexte import CARACTERISTIQUES
from maxprofit.apprentissage.historique import CONTEXTE_A_L_ENTREE
from maxprofit.core.payout import (
    FLUX_POUR_LE_PLAFOND, payout_applique_pct)
from maxprofit.execution.journal import RESERVE
from maxprofit.live import etude3
from maxprofit.live.etude3 import ConnexionTracee, etat_lecture_seule

log = logging.getLogger(__name__)

#: La fraîcheur de la course (`plan_demo.FRAICHEUR_MAX_SEC`, vérifiée par un
#: test) : une bougie plus vieille n'est pas jouée.
FRAICHEUR_SEC = 90
#: Entre l'évaluation de la bougie et `courtier.placer` (réservation au
#: journal, revérification du payout).
MARGE_PLACEMENT_SEC = 30
#: Avance tolérée de notre horloge sur celle des bougies.
AVANCE_HORLOGE_SEC = 5
#: Une empreinte IDENTIQUE est un identifiant : elle est cherchée aussi hors
#: du créneau, dans cette fenêtre, pour révéler un défaut d'horloge.
FENETRE_EMPREINTE_SEC = 900

#: Égales : à l'arrondi près (4 décimales des deux côtés).
TOLERANCE_EGALE = 1.5e-4
#: Proches : écart ≤ max(5 % de la valeur, 0,05).
TOLERANCE_PROCHE_REL, TOLERANCE_PROCHE_ABS = 0.05, 0.05
PART_PROCHE_MIN = 0.80
#: En dessous, l'empreinte ne prouve rien : « non vérifiable ».
CLES_MIN = 6
#: Grandeurs entières : proches seulement si égales.
CLES_ENTIERES = frozenset({
    "bougies_contre", "serie_sens", "alternances_10", "depuis_contact",
    "touches", "entrees_deja_offertes", "heure_utc"})
#: Propriétés de la ZONE du signal, identiques quelle que soit la bougie
#: d'entrée : deux signaux qui les ont différentes ne sont pas le même.
CLES_DE_LA_ZONE = ("touches", "entrees_deja_offertes")

EGALE, PROCHE, DISCORDANTE, NON_VERIFIABLE = (
    "égale", "proche", "discordante", "non vérifiable")
EXACTE, APPROXIMATIVE, AMBIGUE, ABSENTE = (
    "exacte", "approximative", "ambiguë", "absente")
CORRESPONDANCES = (EXACTE, APPROXIMATIVE, AMBIGUE, ABSENTE)

REGLE, NON_REGLE, INCONNUE, REJETE, NON_ENVOYE, RESERVE_SANS_SUITE = (
    "réglé", "exécuté, non réglé", "exécuté, issue inconnue", "rejeté",
    "non envoyé", "réservé sans suite")
ETATS = (REGLE, NON_REGLE, INCONNUE, REJETE, NON_ENVOYE, RESERVE_SANS_SUITE)
RESULTATS = {"win": "gain", "loose": "perte", "draw": "égalité"}

JOUABLE, SOUS_LE_PLAFOND, PAYOUT_INCONNU = (
    f"jouable (flux ≥ {FLUX_POUR_LE_PLAFOND})",
    f"sous le plafond (flux < {FLUX_POUR_LE_PLAFOND})", "inconnu")
ANTERIEURE = "antérieure"

#: Ouverture de la confirmation du gel 2 (`docs/etape3_registre.md` §5).
OUVERTURE_CONFIRMATION = int(datetime(2026, 11, 3,
                                      tzinfo=timezone.utc).timestamp())
HEURE_MS = 3_600_000
RESPIRATION_SEC = 0.01

COLONNES_ORDRES = (
    "id", "campagne", "pair", "sens", "mise", "signal_ts_ms", "clic_ts_ms",
    "accepte_ts_ms", "prix_attendu", "prix_entree", "prix_sortie",
    "payout_flux_pct", "payout_broker_pct", "expiration_sec",
    "ouverture_ts_ms", "expiration_ts_ms", "decalage_broker_ms", "accepte",
    "refus", "resultat", "profit", "order_id", "brut")
REQUETE_ORDRES = (f"SELECT {', '.join(COLONNES_ORDRES)} FROM executions "
                  f"ORDER BY id")
PAYOUT_AVANT = ("SELECT payout_pct FROM payouts WHERE pair = ? AND "
                "ts_sec < ? ORDER BY ts_sec DESC LIMIT 1")

ATTENDRE_LE_REJEU = (
    "🔗 Le rejeu quotidien n'est pas encore en mémoire : il tourne quelques "
    "minutes après chaque démarrage. Relance /rapprochement quand /lecons "
    "annonce le nouvel apprentissage.")


def _utc(ts: float | None, forme: str = "%Y-%m-%d %H:%M:%S") -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime(forme)


def _flottant(valeur) -> float | None:
    try:
        return None if valeur is None else float(valeur)
    except (TypeError, ValueError):
        return None


def periode(d: int, debut_etape3: int | None) -> str:
    """Le découpage de l'étape 3 ; « antérieure » avant ses premiers ticks."""
    if debut_etape3 is not None and d < debut_etape3:
        return ANTERIEURE
    return em.periode(d, None)


def classe_payout(flux: float | None) -> str:
    if flux is None:
        return PAYOUT_INCONNU
    return JOUABLE if flux >= FLUX_POUR_LE_PLAFOND else SOUS_LE_PLAFOND


# --------------------------------------------------------------------------- #
# Les ordres, tels que le journal les contient
# --------------------------------------------------------------------------- #

def lire_ordres(conn) -> list[dict]:
    return [dict(zip(COLONNES_ORDRES, ligne))
            for ligne in conn.execute(REQUETE_ORDRES).fetchall()]


def etat_ordre(o: Mapping) -> str:
    """Rejeté, non envoyé, réservé sans suite, exécuté (réglé ou non)."""
    refus = str(o.get("refus") or "")
    if o.get("accepte"):
        resultat = o.get("resultat")
        if resultat in RESULTATS:
            return REGLE
        return NON_REGLE if resultat is None else INCONNUE
    if refus == RESERVE:
        return RESERVE_SANS_SUITE
    if refus.startswith("non envoyé"):
        return NON_ENVOYE
    return REJETE


def _brut(o: Mapping) -> dict:
    brut = o.get("brut")
    if isinstance(brut, dict):
        return brut
    try:
        lu = json.loads(brut or "{}")
    except (TypeError, ValueError):
        return {}
    return lu if isinstance(lu, dict) else {}


def fiche_ordre(o: Mapping) -> dict:
    """L'ordre, ses champs broker et leurs limites. Les horodatages du
    broker sont ramenés en UTC par le décalage arrondi à l'heure, la
    convention de la collecte (celle des bougies)."""
    brut = _brut(o)
    rapatrie = brut.get("rapatrie_du_broker")
    contexte = brut.get("contexte") if isinstance(brut.get("contexte"),
                                                  dict) else None
    etat = etat_ordre(o)
    accepte = bool(o.get("accepte"))
    decalage = o.get("decalage_broker_ms")
    if isinstance(rapatrie, dict) and "decalage_broker_sec" in rapatrie:
        decalage_h = 0                    # déjà ramené en UTC au rapatriement
    elif decalage is not None:
        decalage_h = round(int(decalage) / HEURE_MS) * HEURE_MS
    else:
        decalage_h = None

    def en_utc(ts):
        if ts is None or decalage_h is None:
            return None
        return (int(ts) - decalage_h) / 1000

    ouverture, expiration = o.get("ouverture_ts_ms"), o.get("expiration_ts_ms")
    entree, sortie = _flottant(o.get("prix_entree")), _flottant(
        o.get("prix_sortie"))
    mise, profit = _flottant(o.get("mise")), _flottant(o.get("profit"))
    pct = _flottant(o.get("payout_broker_pct"))
    flux = _flottant(o.get("payout_flux_pct"))
    resultat = o.get("resultat")
    rendement = None
    if etat == REGLE and mise:
        if resultat == "draw":
            rendement = 0.0
        elif profit is not None:
            rendement = profit / mise
    applique = (100 * profit / mise if resultat == "win" and profit
                is not None and mise else None)
    attendu = None
    if entree is not None and sortie is not None and resultat in RESULTATS:
        if entree == sortie:
            attendu = "draw"
        else:
            attendu = "win" if (sortie > entree) == (o.get("sens") == "call") \
                else "loose"
    limites = []
    if accepte:
        if entree is None:
            limites.append("prix d'entrée broker absent")
        if etat == REGLE and sortie is None:
            limites.append("prix de règlement broker absent")
        if pct is None:
            limites.append("payout broker absent")
        if ouverture is None:
            limites.append("ouverture broker absente")
        if etat == REGLE and expiration is None:
            limites.append("expiration broker absente")
        if decalage_h is None and (ouverture is not None
                                   or expiration is not None):
            limites.append("décalage d'horloge inconnu : UTC non calculé")
        if etat == REGLE and "denouement" not in brut and not rapatrie:
            limites.append("message de dénouement du broker non conservé")
        if etat == REGLE and rendement is None:
            limites.append("profit absent")
    if contexte is None:
        limites.append("sans contexte du signal (aucune empreinte)")
    duree = ((int(expiration) - int(ouverture)) / 1000
             if ouverture is not None and expiration is not None else None)
    placement = o.get("signal_ts_ms")
    return {
        "id": o.get("id"), "campagne": o.get("campagne"),
        "paire": o.get("pair"), "sens": o.get("sens"), "mise": mise,
        "etat": etat, "origine": "rapatrié du broker" if rapatrie
        else "course",
        "placement_s": None if placement is None else int(placement) / 1000,
        "placement_utc": _utc(None if placement is None
                              else int(placement) / 1000),
        "order_id": o.get("order_id"), "refus": o.get("refus") or "",
        "prix_attendu_flux": _flottant(o.get("prix_attendu")),
        "ouverture_broker_ms": ouverture,
        "ouverture_utc_s": en_utc(ouverture),
        "ouverture_utc": _utc(en_utc(ouverture)),
        "prix_entree_broker": entree,
        "expiration_broker_ms": expiration,
        "expiration_utc_s": en_utc(expiration),
        "expiration_utc": _utc(en_utc(expiration)),
        "prix_reglement_broker": sortie,
        "duree_tenue_s": duree, "expiration_demandee_s": o.get(
            "expiration_sec"),
        "payout_flux_pct": flux, "payout_broker_pct": pct,
        # Un rapatrié n'a pas de flux : son « payout_flux » est l'appliqué.
        "payout_regle_pct": (payout_applique_pct(flux)
                             if flux and 0 < flux <= 100 and not rapatrie
                             else None),
        "payout_du_profit_pct": applique,
        "resultat": RESULTATS.get(resultat, "" if resultat is None
                                  else "inconnu"),
        "resultat_selon_prix": RESULTATS.get(attendu, ""),
        "coherence_prix_resultat": ("" if attendu is None else
                                    "oui" if attendu == resultat else "NON"),
        "profit": profit, "rendement": rendement,
        "contexte": contexte, "limites": " | ".join(limites),
        "classe_payout": classe_payout(flux) if flux else PAYOUT_INCONNU,
    }


# --------------------------------------------------------------------------- #
# Les signaux du rejeu, et leurs empreintes
# --------------------------------------------------------------------------- #

def signaux_du_rejeu(exemples, sans_issue, niveaux, debut: int,
                     fin: int) -> list[dict]:
    """Tous les signaux ZoneH1 du rejeu dont la décision tombe dans
    [debut, fin[, issue décisive ou non."""
    sortie: dict[tuple[str, int], dict] = {}
    for e in exemples:
        if debut <= e.ts_sec < fin:
            ctx = dict(e.contexte)
            sortie[(e.pair, int(e.ts_sec))] = {
                "paire": e.pair, "d": int(e.ts_sec),
                "call": bool(ctx.get("sens_call")),
                "issue": em.GAIN if e.gagne else em.PERTE, "contexte": ctx}
    for s in sans_issue or ():
        d = int(s["ts_sec"])
        if debut <= d < fin and (s["pair"], d) not in sortie:
            sortie[(s["pair"], d)] = {
                "paire": s["pair"], "d": d, "call": bool(s["call"]),
                "issue": (em.EGALITE if s["issue"] == "egalite"
                          else em.MANQUANTE),
                "contexte": dict(s.get("contexte") or {})}
    for (paire, d), s in sortie.items():
        s["niveau"] = (niveaux or {}).get((paire, d))
    return sorted(sortie.values(), key=lambda s: (s["d"], s["paire"]))


def references(s: Mapping) -> list[tuple[str, int, dict]]:
    """(mode d'entrée, k, empreinte attendue) : l'entrée immédiate sur la
    bougie du signal, et l'entrée confirmée sur la k-ième bougie suivante si
    le rejeu en a trouvé une."""
    ctx = s["contexte"]
    sortie = [("immédiate", 0, {c: float(ctx[c]) for c in CARACTERISTIQUES
                                if c in ctx})]
    k = ctx.get("attente_confirmation")
    if k is not None and int(k) >= 1:
        k = int(k)
        conf = {c: float(ctx[f"e_{c}"]) for c in CONTEXTE_A_L_ENTREE
                if f"e_{c}" in ctx}
        for c in CLES_DE_LA_ZONE:
            if c in ctx:
                conf[c] = float(ctx[c])
        conf["heure_utc"] = float(((s["d"] + 60 * k) // 3600) % 24)
        sortie.append((f"confirmée (k = {k})", k, conf))
    return sortie


def comparer(attendue: Mapping[str, float], contexte: Mapping | None
             ) -> tuple[str, int, int, int]:
    """(verdict, clés comparées, égales, proches)."""
    if not contexte:
        return NON_VERIFIABLE, 0, 0, 0
    communes = [c for c in attendue if _flottant(contexte.get(c)) is not None]
    if len(communes) < CLES_MIN:
        return NON_VERIFIABLE, len(communes), 0, 0
    egales = proches = 0
    zone_ok = True
    for c in communes:
        a, b = attendue[c], float(contexte[c])
        ecart = abs(a - b)
        if ecart <= TOLERANCE_EGALE:
            egales += 1
            proches += 1
        elif c not in CLES_ENTIERES and ecart <= max(
                TOLERANCE_PROCHE_ABS, TOLERANCE_PROCHE_REL * abs(a)):
            proches += 1
        elif c in CLES_DE_LA_ZONE:
            zone_ok = False
    n = len(communes)
    if egales == n:
        return EGALE, n, egales, proches
    if zone_ok and proches >= PART_PROCHE_MIN * n:
        return PROCHE, n, egales, proches
    return DISCORDANTE, n, egales, proches


def conforme_au_mode(mode: str, k: int) -> bool:
    """L'entrée sur la k-ième bougie (0 : celle du signal) est-elle possible
    au mode de confirmation ACTUEL de la course ?"""
    if mode in ("0", "meme"):
        return k == 0
    fenetre = int(mode) if mode.isdigit() else 1
    return 1 <= k <= fenetre


def candidats(s: Mapping, par_paire_sens: Mapping[tuple, list[dict]],
              mode: str = "suivante") -> list[dict]:
    """Les arêtes signal → ordre, empreinte discordante écartée."""
    aretes = []
    ordres = par_paire_sens.get((s["paire"], "call" if s["call"] else "put"),
                                ())
    for entree, k, attendue in references(s):
        fin = s["d"] + 60 * k
        bas, haut = fin - AVANCE_HORLOGE_SEC, \
            fin + FRAICHEUR_SEC + MARGE_PLACEMENT_SEC
        for o in ordres:
            t = o["placement_s"]
            if t is None or not (fin - FENETRE_EMPREINTE_SEC <= t
                                 <= fin + FENETRE_EMPREINTE_SEC):
                continue
            dans_le_creneau = bas <= t <= haut
            verdict, n, egales, proches = comparer(attendue, o["contexte"])
            # Pour le diagnostic d'un ordre resté sans signal : la meilleure
            # concordance vue, discordante comprise.
            if n and (o.get("_meilleure") is None
                      or (egales, proches) > o["_meilleure"][0]):
                o["_meilleure"] = ((egales, proches),
                                   f"{signal_id(s)} {entree} : {egales}/{n} "
                                   f"égales, {proches} proches ({verdict})")
            if verdict == DISCORDANTE:
                continue
            # Hors du créneau, seule une empreinte IDENTIQUE désigne l'ordre.
            if not dans_le_creneau and verdict != EGALE:
                continue
            aretes.append({"ordre": o, "mode": entree, "fin": fin,
                           "verdict": verdict, "creneau": dans_le_creneau,
                           "conforme": conforme_au_mode(mode, k),
                           "cles": n, "egales": egales, "proches": proches})
    return aretes


def _force(a: Mapping) -> tuple:
    return (a["verdict"] == EGALE, a["creneau"], a["conforme"], a["egales"],
            a["proches"])


def apparier(signaux: Sequence[dict], ordres: Sequence[dict],
             mode: str = "suivante") -> None:
    """Pose sur chaque signal et chaque ordre sa correspondance. Les
    identités sûres d'abord (empreinte égale dans le créneau), puis les
    proches ; ce qui reste en conflit, ou sans empreinte, est ambigu.

    Deux signaux consécutifs de la même zone peuvent partager la même bougie
    d'entrée : le premier confirmé par la bougie où le second signale. Leurs
    empreintes sont alors identiques. Seule la règle de la course les
    départage : à chaque niveau, les arêtes conformes au mode de
    confirmation actuel passent d'abord. Jamais l'heure."""
    par_paire_sens: dict[tuple, list[dict]] = defaultdict(list)
    for o in ordres:
        if o["placement_s"] is not None and (
                o["origine"] == "course" or o["ouverture_utc_s"] is not None):
            par_paire_sens[(o["paire"], o["sens"])].append(o)
    aretes = {id(s): candidats(s, par_paire_sens, mode) for s in signaux}
    pris_s: dict[int, tuple[dict, dict]] = {}
    pris_o: set[int] = set()

    def passe(accepte: Callable[[dict], bool], classe: str) -> None:
        par_ordre: dict[int, list] = defaultdict(list)
        for s in signaux:
            if id(s) in pris_s:
                continue
            for a in aretes[id(s)]:
                if accepte(a) and id(a["ordre"]) not in pris_o:
                    par_ordre[id(a["ordre"])].append((s, a))
        for s in signaux:
            if id(s) in pris_s:
                continue
            les_siennes = [a for a in aretes[id(s)] if accepte(a)
                           and id(a["ordre"]) not in pris_o]
            ordres_vus = {id(a["ordre"]) for a in les_siennes}
            if len(ordres_vus) != 1:
                continue
            (cle,) = ordres_vus
            if len({id(x) for x, _ in par_ordre[cle]}) != 1:
                continue
            a = max(les_siennes, key=_force)
            pris_s[id(s)] = (a, {"classe": classe})
            pris_o.add(cle)

    passe(lambda a: a["verdict"] == EGALE and a["creneau"] and a["conforme"],
          EXACTE)
    passe(lambda a: a["verdict"] == EGALE and a["creneau"], EXACTE)
    passe(lambda a: a["verdict"] in (EGALE, PROCHE) and a["conforme"],
          APPROXIMATIVE)
    passe(lambda a: a["verdict"] in (EGALE, PROCHE), APPROXIMATIVE)
    for o in ordres:
        o.update(correspondance="", signal_id="", candidats_signaux="")
    for s in signaux:
        s.update(correspondance=ABSENTE, ordre=None, arete=None,
                 candidats_ordres="")
        if id(s) in pris_s:
            a, info = pris_s[id(s)]
            s.update(correspondance=info["classe"], ordre=a["ordre"], arete=a)
            a["ordre"].update(correspondance=info["classe"],
                              signal_id=signal_id(s))
            continue
        restantes = [a for a in aretes[id(s)] if id(a["ordre"]) not in pris_o]
        if restantes:
            s["correspondance"] = AMBIGUE
            ids = sorted({str(a["ordre"]["id"]) for a in restantes})
            s["candidats_ordres"] = " ".join(ids)
            # Un même ordre peut tomber dans les deux créneaux du signal
            # (entrée immédiate et confirmée) : une seule mention.
            for o in {id(a["ordre"]): a["ordre"] for a in restantes}.values():
                o["correspondance"] = o["correspondance"] or AMBIGUE
                o["candidats_signaux"] = " ".join(filter(None, (
                    o["candidats_signaux"], signal_id(s))))


def signal_id(s: Mapping) -> str:
    return f"{s['paire']}@{s['d']}"


# --------------------------------------------------------------------------- #
# Le contexte de chaque signal : payout, confirmation, course occupée
# --------------------------------------------------------------------------- #

def confirme_selon(mode: str, ctx: Mapping) -> bool | None:
    """Le rejeu dit-il la confirmation M1 obtenue, au mode ACTUEL ?"""
    if mode == "0":
        return True
    if mode == "meme":
        v = ctx.get("retournement_meme")
        return None if v is None else bool(v)
    fenetre = 1 if mode == "suivante" else int(mode) if mode.isdigit() else 1
    k = ctx.get("attente_confirmation")
    return None if k is None else 1 <= int(k) <= fenetre


def motif_d_absence(s: Mapping, mode: str, occupee: bool) -> str:
    """Un motif PROBABLE, tiré des seules règles relues ici."""
    if confirme_selon(mode, s["contexte"]) is False:
        return f"non confirmé en M1 au rejeu (mode {mode})"
    if s["classe_payout"] == SOUS_LE_PLAFOND:
        return "payout du flux sous le plafond à la décision"
    if occupee:
        return "course occupée par un autre ordre (15 min)"
    return ("inexpliqué par les règles relues ici (filtres, leçons, paire "
            "écartée, course arrêtée…)")


def course_occupee(d: int, acceptes: Sequence[float]) -> bool:
    """Un ordre accepté placé dans les 15 minutes qui précèdent l'entrée :
    la course n'en place pas d'autre (un seul ordre, 15 min d'écart)."""
    return any(d - em.HORIZON_SEC < t <= d + 60 for t in acceptes)


# --------------------------------------------------------------------------- #
# Les mesures
# --------------------------------------------------------------------------- #

def ic_ratio(blocs: Mapping[Any, tuple[float, float]], tirages: int = em.TIRAGES,
             graine: int = em.GRAINE) -> tuple[float, float] | None:
    """IC 95 % percentile d'un rapport de sommes, par bootstrap de blocs."""
    valeurs = [v for v in blocs.values() if v[1] > 0 or v[0]]
    if len(valeurs) < 2 or sum(v[1] for v in valeurs) <= 0:
        return None
    nums = [v[0] for v in valeurs]
    dens = [v[1] for v in valeurs]
    k = len(valeurs)
    alea = random.Random(graine)
    tirs = []
    for _ in range(tirages):
        idx = alea.choices(range(k), k=k)
        den = sum(dens[i] for i in idx)
        if den > 0:
            tirs.append(sum(nums[i] for i in idx) / den)
    if len(tirs) < tirages // 2:
        return None
    tirs.sort()
    return tirs[int(0.025 * len(tirs))], tirs[int(0.975 * len(tirs)) - 1]


def _rapport(lignes: Sequence[dict], num: Callable, den: Callable, bloc
             ) -> tuple[int, float | None, tuple | None]:
    blocs: dict[Any, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in lignes:
        d = den(r)
        if d:
            b = blocs[bloc(r)]
            b[0] += num(r)
            b[1] += d
    total_d = sum(b[1] for b in blocs.values())
    if total_d <= 0:
        return 0, None, None
    total_n = sum(b[0] for b in blocs.values())
    return (int(total_d), total_n / total_d,
            ic_ratio({k: tuple(v) for k, v in blocs.items()}))


def _decisif(issue: str) -> bool:
    return issue in (em.GAIN, em.PERTE)


def mesures(signaux: Sequence[dict], ordres: Sequence[dict],
            respirer: Callable[[], None] | None = None) -> list[dict]:
    """Les quatre mesures, et l'écart apparié, par dimension."""
    respirer = respirer or (lambda: None)
    groupes: list[tuple[str, str, Callable, Callable]] = [
        ("ensemble", "toutes", lambda s: True, lambda o: True)]
    for p in sorted({s["periode"] for s in signaux}
                    | {o["periode"] for o in ordres if o.get("periode")}):
        groupes.append(("période", p, lambda s, p=p: s["periode"] == p,
                        lambda o, p=p: o.get("periode") == p))
    for c in (JOUABLE, SOUS_LE_PLAFOND, PAYOUT_INCONNU):
        groupes.append(("payout", c, lambda s, c=c: s["classe_payout"] == c,
                        lambda o, c=c: o["classe_payout"] == c))
    for p in sorted({s["paire"] for s in signaux}
                    | {o["paire"] for o in ordres}):
        groupes.append(("paire", p, lambda s, p=p: s["paire"] == p,
                        lambda o, p=p: o["paire"] == p))

    def bloc_s(s):
        return (s["paire"], s["d"] // 86400)

    def bloc_o(o):
        return (o["paire"], int(o["placement_s"]) // 86400)

    sortie = []
    for dimension, valeur, garde_s, garde_o in groupes:
        respirer()
        sg = [s for s in signaux if garde_s(s)]
        og = [o for o in ordres if o["etat"] == REGLE and o.get("periode")
              and garde_o(o)]
        if not sg and not og:
            continue
        compte = Counter(s["correspondance"] for s in sg)
        n_s, taux_c, ic_c = _rapport(
            sg, lambda s: s["correspondance"] in (EXACTE, APPROXIMATIVE),
            lambda s: 1, bloc_s)
        n_p, taux_p, ic_p = _rapport(
            sg, lambda s: s["issue"] == em.GAIN,
            lambda s: _decisif(s["issue"]), bloc_s)
        apparies = [s for s in sg if s["correspondance"] in
                    (EXACTE, APPROXIMATIVE) and s["ordre"]["etat"] == REGLE]
        n_ar, taux_ar, ic_ar = _rapport(
            apparies, lambda s: s["ordre"]["resultat"] == "gain",
            lambda s: s["ordre"]["resultat"] in ("gain", "perte"), bloc_s)
        n_er, rend_ar, ic_rend_ar = _rapport(
            apparies, lambda s: s["ordre"]["rendement"],
            lambda s: s["ordre"]["rendement"] is not None, bloc_s)
        paires_dec = [s for s in apparies if _decisif(s["issue"])
                      and s["ordre"]["resultat"] in ("gain", "perte")]
        n_pd, ecart, ic_ecart = _rapport(
            paires_dec, lambda s: (s["issue"] == em.GAIN)
            - (s["ordre"]["resultat"] == "gain"), lambda s: 1, bloc_s)
        _, accord, ic_accord = _rapport(
            paires_dec, lambda s: (s["issue"] == em.GAIN)
            == (s["ordre"]["resultat"] == "gain"), lambda s: 1, bloc_s)
        _, taux_pa, _ = _rapport(paires_dec, lambda s: s["issue"] == em.GAIN,
                                 lambda s: 1, bloc_s)
        n_or, taux_or, ic_or = _rapport(
            og, lambda o: o["resultat"] == "gain",
            lambda o: o["resultat"] in ("gain", "perte"), bloc_o)
        n_eo, rend_or, ic_rend_or = _rapport(
            og, lambda o: o["rendement"],
            lambda o: o["rendement"] is not None, bloc_o)
        sortie.append({
            "dimension": dimension, "valeur": valeur,
            "signaux": n_s, **{f"n_{c}": compte.get(c, 0)
                               for c in CORRESPONDANCES},
            "taux_correspondance": taux_c, "ic_correspondance": ic_c,
            "proxy_decisifs": n_p, "taux_proxy": taux_p, "ic_proxy": ic_p,
            "apparies_regles_decisifs": n_pd,
            "taux_proxy_apparies": taux_pa,
            "taux_reel_apparies": taux_ar, "n_reel_apparies": n_ar,
            "ic_reel_apparies": ic_ar,
            "rendement_reel_apparies": rend_ar, "n_rendement_apparies": n_er,
            "ic_rendement_apparies": ic_rend_ar,
            "ecart_proxy_moins_reel": ecart, "ic_ecart": ic_ecart,
            "accord_proxy_reel": accord, "ic_accord": ic_accord,
            "ordres_regles": n_or, "taux_reel_ordres": taux_or,
            "ic_reel_ordres": ic_or, "rendement_reel_ordres": rend_or,
            "n_rendement_ordres": n_eo, "ic_rendement_ordres": ic_rend_or,
            "blocs_signaux": len({bloc_s(s) for s in sg}),
            "blocs_ordres": len({bloc_o(o) for o in og}),
        })
    return sortie


# --------------------------------------------------------------------------- #
# La passe
# --------------------------------------------------------------------------- #

def rapprochement(conn, course, *, nom_lecture: str = "active",
                  maintenant: float | None = None,
                  respirer: Callable[[], None] | None = None
                  ) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers). `conn` est déjà en lecture seule."""
    maintenant = time.time() if maintenant is None else maintenant
    respirer = respirer or (lambda: time.sleep(RESPIRATION_SEC))
    t0 = time.monotonic()
    memoire = getattr(course, "rejeu_en_memoire", None)
    sans_issue = getattr(course, "sans_issue_en_memoire", None)
    if not memoire or sans_issue is None:
        return ATTENDRE_LE_REJEU, []
    exemples = memoire[0]
    niveaux = getattr(course, "niveaux_en_memoire", None) or {}
    mode = str(getattr(course, "confirmation_m1", "suivante") or "suivante")
    expiration = int(getattr(getattr(getattr(course, "strategie", None), "p",
                                     None), "expiry_sec", em.HORIZON_SEC))

    lue = ConnexionTracee(conn)
    lecture = etat_lecture_seule(lue)
    premiere = etude3.premiere_minute_de_ticks(lue)
    debut_etape3 = None if premiere is None else premiere + mp.QUALITE_SEC
    decisions = [int(e.ts_sec) for e in exemples] + [
        int(s["ts_sec"]) for s in sans_issue]
    if not decisions:
        return "🔗 Le rejeu en mémoire ne contient aucun signal.", []
    debut = min(decisions)
    mure = int(maintenant) - expiration - em.MARGE_MATURITE_SEC
    fin = min(mure, OUVERTURE_CONFIRMATION)
    signaux = signaux_du_rejeu(exemples, sans_issue, niveaux, debut, fin)

    # Les ordres : tout le journal, toutes campagnes, refus compris.
    brutes = lire_ordres(lue)
    ordres = [fiche_ordre(o) for o in brutes]
    for o in ordres:
        t = o["placement_s"]
        o["dans_le_rejeu"] = t is not None and debut <= t < fin + 600
        o["periode"] = (periode(int(t), debut_etape3) if o["dans_le_rejeu"]
                        else "")
    respirer()

    # Le payout du flux juste avant chaque décision.
    for rang, s in enumerate(signaux, 1):
        ligne = lue.execute(PAYOUT_AVANT, (s["paire"], s["d"])).fetchone()
        s["flux"] = None if not ligne or ligne[0] is None else float(ligne[0])
        s["classe_payout"] = classe_payout(s["flux"])
        s["periode"] = periode(s["d"], debut_etape3)
        if rang % 200 == 0:
            respirer()

    apparier(signaux, [o for o in ordres if o["dans_le_rejeu"]], mode)
    acceptes = sorted(o["placement_s"] for o in ordres
                      if o["etat"] in (REGLE, NON_REGLE, INCONNUE)
                      and o["placement_s"] is not None)
    for s in signaux:
        s["motif_absence"] = (motif_d_absence(
            s, mode, course_occupee(s["d"], acceptes))
            if s["correspondance"] == ABSENTE else "")
    for o in ordres:
        if not o["dans_le_rejeu"]:
            o["correspondance"] = "hors de la période du rejeu"
        elif not o["correspondance"]:
            o["correspondance"] = "aucun signal du rejeu"

    calcul = mesures(signaux, [o for o in ordres if o["dans_le_rejeu"]],
                     respirer)
    limites = limites_des_donnees(signaux, ordres)
    rapport = texte(signaux, ordres, calcul, limites, {
        "lecture": lecture, "nom": nom_lecture, "debut": debut, "fin": fin,
        "debut_etape3": debut_etape3, "mode": mode,
        "expiration": expiration, "requetes": dict(lue.requetes),
        "refusees": lue.refusees, "temps": time.monotonic() - t0,
        "coupe_confirmation": fin == OUVERTURE_CONFIRMATION})
    return rapport, fichiers(signaux, ordres, calcul)


def limites_des_donnees(signaux: Sequence[dict], ordres: Sequence[dict]
                        ) -> dict[str, Any]:
    """Ce que les données contiennent VRAIMENT, compté sur les ordres de la
    période du rejeu."""
    dans = [o for o in ordres if o["dans_le_rejeu"]]
    acceptes = [o for o in dans if o["etat"] in (REGLE, NON_REGLE, INCONNUE)]
    regles = [o for o in dans if o["etat"] == REGLE]
    gagnes = [o for o in regles if o["resultat"] == "gain"]

    def n(liste, garde):
        return sum(1 for o in liste if garde(o)), len(liste)

    return {
        "acceptes": len(acceptes), "regles": len(regles),
        "prix_entree": n(acceptes, lambda o: o["prix_entree_broker"]
                         is not None),
        "prix_reglement": n(regles, lambda o: o["prix_reglement_broker"]
                            is not None),
        "payout_broker": n(acceptes, lambda o: o["payout_broker_pct"]
                           is not None),
        "ouverture": n(acceptes, lambda o: o["ouverture_broker_ms"]
                       is not None),
        "expiration": n(regles, lambda o: o["expiration_broker_ms"]
                        is not None),
        "utc": n(acceptes, lambda o: o["ouverture_utc_s"] is not None),
        "denouement": n(regles, lambda o: "message de dénouement" not in
                        o["limites"]),
        "contexte": n(acceptes, lambda o: o["contexte"] is not None),
        "contexte_refus": n([o for o in dans if o["etat"] not in
                             (REGLE, NON_REGLE, INCONNUE)],
                            lambda o: o["contexte"] is not None),
        "profit": n(regles, lambda o: o["rendement"] is not None),
        "coherence": n([o for o in regles if o["coherence_prix_resultat"]],
                       lambda o: o["coherence_prix_resultat"] == "oui"),
        "payout_profit": n(
            [o for o in gagnes if o["payout_du_profit_pct"] is not None
             and o["payout_broker_pct"] is not None],
            lambda o: abs(o["payout_du_profit_pct"]
                          - o["payout_broker_pct"]) <= 1.0),
        "regle_broker": n(
            [o for o in acceptes if o["payout_regle_pct"] is not None
             and o["payout_broker_pct"] is not None],
            lambda o: abs(o["payout_regle_pct"] - o["payout_broker_pct"])
            < 0.5),
        "duree": n([o for o in regles if o["duree_tenue_s"] is not None],
                   lambda o: abs(o["duree_tenue_s"]
                                 - float(o["expiration_demandee_s"] or 0))
                   <= 2),
        "references": n(signaux, lambda s: any(
            len(r[2]) >= CLES_MIN for r in references(s))),
        "rapatries": sum(1 for o in dans if o["origine"] != "course"),
    }


# --------------------------------------------------------------------------- #
# Le rapport et les fichiers
# --------------------------------------------------------------------------- #

def _pct(x: float | None, signe: bool = False) -> str:
    if x is None:
        return "—"
    return f"{100 * x:+.1f} %" if signe else f"{100 * x:.1f} %"


def _ic(ic: tuple | None, signe: bool = False) -> str:
    if ic is None:
        return "IC —"
    a, b = ic
    if signe:
        return f"IC {100 * a:+.1f} à {100 * b:+.1f}"
    return f"IC {100 * a:.1f} à {100 * b:.1f}"


def _part(compte: tuple[int, int]) -> str:
    a, b = compte
    return f"{a}/{b}" + (f" ({100 * a / b:.0f} %)" if b else "")


def _ligne_mesure(m: Mapping) -> str:
    return (
        f"<b>{html.escape(str(m['valeur']))}</b> : {m['signaux']} signaux, "
        f"correspondance {_pct(m['taux_correspondance'])} "
        f"({_ic(m['ic_correspondance'])}) ; proxy "
        f"{_pct(m['taux_proxy'])} sur {m['proxy_decisifs']} "
        f"({_ic(m['ic_proxy'])}) ; réel réglé {_pct(m['taux_reel_ordres'])} "
        f"sur {m['ordres_regles']} ({_ic(m['ic_reel_ordres'])}), rendement "
        f"{_pct(m['rendement_reel_ordres'], True)} "
        f"({_ic(m['ic_rendement_ordres'], True)})")


def texte(signaux: Sequence[dict], ordres: Sequence[dict],
          calcul: Sequence[dict], limites: Mapping, meta: Mapping) -> str:
    dans = [o for o in ordres if o["dans_le_rejeu"]]
    etats = Counter(o["etat"] for o in dans)
    corresp = Counter(s["correspondance"] for s in signaux)
    motifs = Counter(s["motif_absence"] for s in signaux
                     if s["correspondance"] == ABSENTE)
    cote_ordres = Counter(o["correspondance"] for o in dans)
    par = {(m["dimension"], m["valeur"]): m for m in calcul}
    ens = par.get(("ensemble", "toutes"))
    l = ["🔗 <b>Rapprochement : signaux du rejeu (proxy) ↔ ordres réels</b>",
         f"Lecture seule sur la base {html.escape(meta['nom'])} "
         f"(transaction_read_only : {html.escape(str(meta['lecture']))}) ; "
         f"requêtes refusées : {meta['refusees']}. Rien n'a été écrit en "
         f"base.",
         f"Décisions du {_utc(meta['debut'], '%d/%m %H:%M')} au "
         f"{_utc(meta['fin'], '%d/%m %H:%M')} UTC (rejeu en mémoire"
         + (", arrêt à l'ouverture de la confirmation du gel 2, dont les "
            "issues ne sont pas lues avant ses regards" if
            meta["coupe_confirmation"] else ", issues mûres") + ").",
         f"Mode de confirmation M1 actuel : {html.escape(meta['mode'])} ; "
         f"échéance {meta['expiration']} s.",
         "",
         "<b>1. Ce que contiennent les données</b>",
         f"- prix d'entrée du broker (openPrice) : "
         f"{_part(limites['prix_entree'])} des ordres exécutés",
         f"- prix de règlement du broker (closePrice) : "
         f"{_part(limites['prix_reglement'])} des ordres réglés",
         f"- payout appliqué par le broker : "
         f"{_part(limites['payout_broker'])} ; profit : "
         f"{_part(limites['profit'])} des réglés",
         f"- ouverture et clôture (horloge broker) : "
         f"{_part(limites['ouverture'])} et {_part(limites['expiration'])} ; "
         f"ramenables en UTC : {_part(limites['utc'])}",
         f"- message de dénouement du broker conservé : "
         f"{_part(limites['denouement'])} des réglés",
         f"- contexte du signal (empreinte) : {_part(limites['contexte'])} "
         f"des exécutés ; {_part(limites['contexte_refus'])} des refusés, "
         f"non envoyés et réservés (jamais écrit pour eux)",
         f"- signaux du rejeu avec une empreinte de référence : "
         f"{_part(limites['references'])}",
         f"- contrôles : résultat cohérent avec les prix du broker "
         f"{_part(limites['coherence'])} ; payout du profit = payout broker "
         f"{_part(limites['payout_profit'])} ; règle min(flux + 8, 92) "
         f"{_part(limites['regle_broker'])} ; durée tenue = échéance "
         f"{_part(limites['duree'])}"]
    manque = []
    for cle, nom in (("prix_entree", "prix d'entrée"),
                     ("prix_reglement", "prix de règlement"),
                     ("payout_broker", "payout appliqué")):
        a, b = limites[cle]
        if b and a < b:
            manque.append(f"{nom} ({b - a})")
    if manque:
        l.append("⚠ <b>Limite</b> : il manque des champs broker pour "
                 + ", ".join(manque) + ". Ces ordres restent sans prix : "
                 "rien n'est reconstruit à partir des bougies M1. Leur "
                 "résultat compte s'il vient du broker (statut ou profit).")
    else:
        l.append("Les prix d'entrée et de règlement du broker sont présents "
                 "pour tous les ordres concernés.")
    l += ["",
          "<b>2. Les ordres de la période</b> (toutes campagnes)",
          "- " + " ; ".join(f"{e} : {etats.get(e, 0)}" for e in ETATS),
          f"- dont rapatriés du broker : {limites['rapatries']} ; hors de la "
          f"période du rejeu : {sum(1 for o in ordres if not o['dans_le_rejeu'])}",
          "- côté ordres : " + " ; ".join(
              f"{c} : {n}" for c, n in cote_ordres.most_common()),
          "",
          "<b>3. La correspondance</b> (empreinte du contexte, jamais la "
          "seule heure)",
          f"- {len(signaux)} signaux : " + " ; ".join(
              f"{c} {corresp.get(c, 0)}" for c in CORRESPONDANCES)]
    if motifs:
        l.append("- absents, motif probable : " + " ; ".join(
            f"{html.escape(m)} {n}" for m, n in motifs.most_common()))
    orphelins = [o for o in dans if o["correspondance"] ==
                 "aucun signal du rejeu" and o["contexte"] is not None]
    if orphelins:
        l.append(f"- ordres AVEC empreinte mais sans signal du rejeu : "
                 f"{len(orphelins)} (meilleure concordance vue : "
                 f"rapprochement_ordres.csv)")
    if ens:
        l += ["",
              "<b>4. Les quatre mesures</b> (IC 95 %, blocs paire × jour)",
              f"- taux de correspondance signal ↔ ordre : "
              f"{_pct(ens['taux_correspondance'])} "
              f"({_ic(ens['ic_correspondance'])})",
              f"- taux de gain du proxy : {_pct(ens['taux_proxy'])} sur "
              f"{ens['proxy_decisifs']} ({_ic(ens['ic_proxy'])})",
              f"- taux de gain des ordres réglés : "
              f"{_pct(ens['taux_reel_ordres'])} sur {ens['ordres_regles']} "
              f"({_ic(ens['ic_reel_ordres'])})",
              f"- rendement réel au payout appliqué : "
              f"{_pct(ens['rendement_reel_ordres'], True)} par mise "
              f"({_ic(ens['ic_rendement_ordres'], True)})",
              f"- sur les MÊMES signaux appariés et réglés "
              f"({ens['apparies_regles_decisifs']}) : proxy "
              f"{_pct(ens['taux_proxy_apparies'])}, réel "
              f"{_pct(ens['taux_reel_apparies'])} ; écart proxy − réel "
              f"{_pct(ens['ecart_proxy_moins_reel'], True)} "
              f"({_ic(ens['ic_ecart'], True)}) ; même issue dans "
              f"{_pct(ens['accord_proxy_reel'])} des cas ; rendement réel "
              f"{_pct(ens['rendement_reel_apparies'], True)}"]
        for dimension, titre in (("période", "Par période"),
                                 ("payout", "Par payout du flux")):
            lignes = [m for m in calcul if m["dimension"] == dimension]
            if lignes:
                l += ["", f"<i>{titre}</i>"] + [
                    "- " + _ligne_mesure(m) for m in lignes]
        l.append("Par paire : rapprochement_mesures.csv.")
    l += ["",
          "Aucune bougie M1 n'est prise pour un prix du broker. Le proxy "
          "reste un proxy ; un écart ici est un fait sur la course, pas une "
          "hypothèse à tester. Aucune règle de la course n'est changée.",
          f"Durée : {meta['temps']:.0f} s ; requêtes : "
          + ", ".join(f"{k} {v}" for k, v in sorted(meta["requetes"].items()))
          + "."]
    return "\n".join(l)


COLONNES_SIGNAUX = (
    "signal_id", "paire", "decision", "decision_utc", "periode", "sens",
    "E8_niveau", "payout_flux_decision", "classe_payout", "issue_proxy",
    "issue_proxy_entree_confirmee", "attente_confirmation_rejeu",
    "prix_signal_bougie_M1", "correspondance", "motif_absence",
    "mode_entree", "fin_bougie_entree_utc", "cles_comparees", "cles_egales",
    "cles_proches", "dans_le_creneau", "candidats_ordres", "ordre_id",
    "order_id_broker", "etat_ordre", "placement_utc", "retard_placement_s",
    "ouverture_utc", "retard_ouverture_s", "prix_entree_broker",
    "expiration_utc", "prix_reglement_broker", "duree_tenue_s",
    "payout_flux_ordre_pct", "payout_broker_pct", "payout_du_profit_pct",
    "resultat_reel", "profit", "rendement_reel", "coherence_prix_resultat",
    "limites_ordre")

COLONNES_FICHE_ORDRE = (
    "id", "campagne", "paire", "sens", "mise", "etat", "origine",
    "placement_utc", "order_id", "refus", "prix_attendu_flux",
    "ouverture_broker_ms", "ouverture_utc", "prix_entree_broker",
    "expiration_broker_ms", "expiration_utc", "prix_reglement_broker",
    "duree_tenue_s", "expiration_demandee_s", "payout_flux_pct",
    "payout_regle_pct", "payout_broker_pct", "payout_du_profit_pct",
    "resultat", "resultat_selon_prix", "coherence_prix_resultat", "profit",
    "rendement", "periode", "classe_payout", "correspondance", "signal_id",
    "candidats_signaux", "contexte_present", "meilleure_empreinte_vue",
    "limites")


def ligne_signal(s: Mapping) -> dict:
    ctx = s["contexte"]
    o, a = s.get("ordre"), s.get("arete")
    confirme = ctx.get("gagne_confirme")
    sortie = {
        "signal_id": signal_id(s), "paire": s["paire"], "decision": s["d"],
        "decision_utc": _utc(s["d"], "%Y-%m-%d %H:%M"),
        "periode": s["periode"], "sens": "call" if s["call"] else "put",
        "E8_niveau": s.get("niveau"), "payout_flux_decision": s.get("flux"),
        "classe_payout": s["classe_payout"], "issue_proxy": s["issue"],
        "issue_proxy_entree_confirmee": ("" if confirme is None else
                                         em.GAIN if confirme else em.PERTE),
        "attente_confirmation_rejeu": ctx.get("attente_confirmation"),
        "prix_signal_bougie_M1": ctx.get("prix_signal"),
        "correspondance": s["correspondance"],
        "motif_absence": s.get("motif_absence", ""),
        "candidats_ordres": s.get("candidats_ordres", "")}
    if a is not None:
        sortie.update({
            "mode_entree": a["mode"],
            "fin_bougie_entree_utc": _utc(a["fin"]),
            "cles_comparees": a["cles"], "cles_egales": a["egales"],
            "cles_proches": a["proches"],
            "dans_le_creneau": "oui" if a["creneau"] else "non"})
    if o is not None:
        sortie.update({
            "ordre_id": o["id"], "order_id_broker": o["order_id"],
            "etat_ordre": o["etat"], "placement_utc": o["placement_utc"],
            "retard_placement_s": (None if a is None or o["placement_s"] is None
                                   else round(o["placement_s"] - a["fin"], 1)),
            "ouverture_utc": o["ouverture_utc"],
            "retard_ouverture_s": (None if a is None or o["ouverture_utc_s"]
                                   is None else round(o["ouverture_utc_s"]
                                                      - a["fin"], 1)),
            "prix_entree_broker": o["prix_entree_broker"],
            "expiration_utc": o["expiration_utc"],
            "prix_reglement_broker": o["prix_reglement_broker"],
            "duree_tenue_s": o["duree_tenue_s"],
            "payout_flux_ordre_pct": o["payout_flux_pct"],
            "payout_broker_pct": o["payout_broker_pct"],
            "payout_du_profit_pct": o["payout_du_profit_pct"],
            "resultat_reel": o["resultat"], "profit": o["profit"],
            "rendement_reel": o["rendement"],
            "coherence_prix_resultat": o["coherence_prix_resultat"],
            "limites_ordre": o["limites"]})
    return sortie


def ligne_ordre(o: Mapping) -> dict:
    return {**{c: o.get(c) for c in COLONNES_FICHE_ORDRE},
            "contexte_present": "oui" if o["contexte"] is not None else "non",
            "meilleure_empreinte_vue": (o.get("_meilleure") or (None, ""))[1]}


def ligne_mesure(m: Mapping) -> dict:
    sortie = {}
    for c, v in m.items():
        if isinstance(v, tuple):
            sortie[f"{c}_bas"], sortie[f"{c}_haut"] = v
        elif c.startswith("ic_"):
            sortie[f"{c}_bas"] = sortie[f"{c}_haut"] = None
        else:
            sortie[c] = v
    return sortie


def fichiers(signaux: Sequence[dict], ordres: Sequence[dict],
             calcul: Sequence[dict]) -> list[tuple[bytes, str, str]]:
    mesures_csv = [ligne_mesure(m) for m in calcul]
    colonnes_mesures = list(mesures_csv[0]) if mesures_csv else ["dimension"]
    return [
        (etude3._csv([ligne_signal(s) for s in signaux], COLONNES_SIGNAUX),
         "rapprochement_signaux.csv",
         "Chaque signal du rejeu (proxy) : son ordre réel, ou son absence, "
         "et pourquoi"),
        (etude3._csv([ligne_ordre(o) for o in ordres], COLONNES_FICHE_ORDRE),
         "rapprochement_ordres.csv",
         "Chaque ordre du journal : état, champs broker, limites, signal"),
        (etude3._csv(mesures_csv, colonnes_mesures),
         "rapprochement_mesures.csv",
         "Les quatre mesures par paire, période et payout, IC paire × jour"),
    ]
