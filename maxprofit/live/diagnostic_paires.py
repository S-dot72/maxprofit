"""
`/etat` — l'état de chaque paire collectée, lu dans la mémoire de la course
(diagnostic autorisé le 30/10).

STRICTEMENT DIAGNOSTIQUE : rien n'est lu en base, rien n'est écrit, aucun
abonnement, aucun signal, aucun réglage n'est touché. La course garde en
mémoire les bougies des `lookback` dernières minutes de chaque paire
collectée. Une bougie n'existe que pour une minute où le broker a transmis au
moins un tick, et `tick_count` dit combien.

--- Les statuts, dans cet ordre ------------------------------------------------

  MUETTE                  aucune bougie depuis au moins 15 minutes, ALORS QUE
                          d'autres paires cotent : rien ne nous arrive pour
                          elle. C'est un constat, pas une cause : le broker
                          peut ne plus la coter, ou ne plus nous la
                          transmettre.
  HISTORIQUE INSUFFISANT  elle cote, mais n'a pas assez de bougies complètes
                          sur la fenêtre pour la stratégie (26) : la course ne
                          peut pas l'évaluer.
  RALENTIE                évaluable, mais cotations rares : moins de 45 des
                          60 dernières minutes ont une bougie, ou ses ticks des
                          15 dernières minutes sont sous le quart de son propre
                          rythme habituel (médiane sur la fenêtre).
  ACTIVE                  sinon.

Une paire aux cotations simplement rares n'est donc pas « muette » : il faut
quinze minutes sans un seul tick pour l'être.

Les âges et les fenêtres se comptent depuis la bougie la plus récente de
TOUTES les paires. Si la collecte entière s'arrête, ou si la course ne lit
plus, aucune paire n'est déclarée muette pour autant : l'en-tête le dit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

MUETTE = "MUETTE"
INSUFFISANT = "HISTORIQUE INSUFFISANT"
RALENTIE = "RALENTIE"
ACTIVE = "ACTIVE"
ORDRE = (MUETTE, INSUFFISANT, RALENTIE, ACTIVE)
PASTILLE = {MUETTE: "🔴", INSUFFISANT: "🟠", RALENTIE: "🟡", ACTIVE: "🟢"}

SILENCE_SEC = 15 * 60
RECENTE_SEC = 15 * 60
HEURE_SEC = 3600
PART_MINUTES_MIN = 0.75
PART_RYTHME_MIN = 0.25
#: Au-delà, la bougie la plus récente de toutes les paires est elle-même
#: vieille : arrêt global, ou course qui ne lit plus.
FRAICHEUR_GLOBALE_SEC = 180
#: Une reprise suit un redémarrage du collecteur s'il a eu lieu pendant le
#: silence (ou dans les deux minutes de la première bougie revenue : il faut
#: le temps de se réabonner).
MARGE_REDEMARRAGE_SEC = 120


def diagnostiquer(cache: Mapping[str, Sequence[Any]], paires: Iterable[str],
                  *, maintenant: float, fenetre_min: int = 300,
                  requis: int = 26, plafond: Iterable[str] = (),
                  demarrage_collecteur: float | None = None
                  ) -> dict[str, Any]:
    """La fiche de chaque paire collectée, et la référence commune.

    `cache` : paire → bougies M1 (`ts_sec`, `complete`, `tick_count`), telles
    que la course les garde ; rien n'y est modifié."""
    paires = list(paires)
    plafond = set(plafond)
    fins = [b.ts_sec + 60 for p in paires for b in cache.get(p, ())[-1:]]
    reference = max(fins) if fins else None
    sortie: dict[str, Any] = {
        "reference": reference, "maintenant": maintenant,
        "fenetre_min": fenetre_min, "requis": requis,
        "demarrage_collecteur": demarrage_collecteur, "paires": []}
    if reference is None:
        return sortie
    sortie["globale_perimee"] = maintenant - reference > FRAICHEUR_GLOBALE_SEC
    debut = reference - fenetre_min * 60
    for paire in paires:
        bougies = sorted((b for b in cache.get(paire, ())
                          if b.ts_sec >= debut and b.ts_sec + 60 <= reference),
                         key=lambda b: b.ts_sec)
        fiche: dict[str, Any] = {
            "paire": paire, "plafond": paire in plafond,
            "completes": sum(1 for b in bougies if b.complete),
            "minutes_60": sum(1 for b in bougies
                              if b.ts_sec >= reference - HEURE_SEC),
            "ticks_15": sum(int(b.tick_count) for b in bougies
                            if b.ts_sec >= reference - RECENTE_SEC),
            "rythme": (median(int(b.tick_count) for b in bougies)
                       if bougies else None),
            "derniere_fin": (bougies[-1].ts_sec + 60 if bougies else None),
        }
        fiche["silence_sec"] = (reference - fiche["derniere_fin"]
                                if bougies else None)
        fiche["age_sec"] = (maintenant - fiche["derniere_fin"]
                            if bougies else None)
        trous = []
        for a, b in zip(bougies, bougies[1:]):
            vide = b.ts_sec - (a.ts_sec + 60)
            if vide >= SILENCE_SEC:
                trous.append({"debut": a.ts_sec + 60, "fin": b.ts_sec,
                              "duree_sec": vide})
        for t in trous:
            t["apres_redemarrage"] = (
                None if demarrage_collecteur is None else
                t["debut"] <= demarrage_collecteur
                <= t["fin"] + MARGE_REDEMARRAGE_SEC)
        fiche["trous"] = trous
        fiche["statut"] = statut(fiche, requis)
        sortie["paires"].append(fiche)
    sortie["paires"].sort(key=lambda f: (ORDRE.index(f["statut"]),
                                         not f["plafond"], f["paire"]))
    return sortie


def statut(fiche: Mapping[str, Any], requis: int) -> str:
    silence = fiche["silence_sec"]
    if silence is None or silence >= SILENCE_SEC:
        return MUETTE
    if fiche["completes"] < requis:
        return INSUFFISANT
    lent = fiche["minutes_60"] < PART_MINUTES_MIN * 60
    rythme = fiche["rythme"]
    if rythme and fiche["ticks_15"] / 15 < PART_RYTHME_MIN * rythme:
        lent = True
    return RALENTIE if lent else ACTIVE


def _duree(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = max(0, int(sec))
    if sec < 90:
        return f"{sec} s"
    if sec < 5400:
        return f"{round(sec / 60)} min"
    return f"{sec // 3600} h {sec % 3600 // 60:02d}"


def texte(diag: Mapping[str, Any], non_collectees: Sequence[str] = ()
          ) -> str:
    """Les lignes de `/etat` : un en-tête, puis une ligne par paire."""
    fiches = diag["paires"]
    if diag["reference"] is None:
        return ("<b>Paires</b> : aucune bougie en mémoire de la course pour "
                "l'instant (elle n'a pas encore lu, ou elle est arrêtée).")
    comptes = {s: sum(1 for f in fiches if f["statut"] == s) for s in ORDRE}
    l = [f"<b>Paires collectées</b> (mémoire de la course, "
         f"{diag['fenetre_min']} min ; ★ = au plafond de payout) : "
         + ", ".join(f"{PASTILLE[s]} {comptes[s]} {s.lower()}"
                     for s in ORDRE if comptes[s])]
    age_ref = diag["maintenant"] - diag["reference"]
    if diag.get("globale_perimee"):
        l.append(f"⚠ Aucune paire n'a de bougie depuis {_duree(age_ref)} : "
                 f"arrêt global de la collecte ou course qui ne lit plus. Les "
                 f"statuts sont relatifs à cette dernière bougie.")
    else:
        l.append(f"Bougie la plus récente, toutes paires : il y a "
                 f"{_duree(age_ref)}.")
    for f in fiches:
        ligne = (f"{PASTILLE[f['statut']]} {f['paire']}"
                 f"{'★' if f['plafond'] else ''} {f['statut']} · "
                 f"{f['completes']}/{diag['fenetre_min']} complètes · "
                 f"dernière il y a {_duree(f['age_sec'])} · "
                 f"{f['ticks_15']} ticks en 15 min"
                 + (f" (habituel {f['rythme']:.0f}/min)"
                    if f["rythme"] is not None else "")
                 + f" · {f['minutes_60']}/60 min cotées")
        if f["statut"] == MUETTE and f["silence_sec"] is not None:
            ligne += f" · silence en cours depuis {_duree(f['silence_sec'])}"
        elif f["statut"] == MUETTE:
            ligne += (f" · aucune bougie sur les {diag['fenetre_min']} "
                      f"dernières minutes")
        if f["trous"]:
            dernier = f["trous"][-1]
            ligne += (f" · {len(f['trous'])} silence(s) ≥ 15 min repris, le "
                      f"dernier de {_duree(dernier['duree_sec'])}, fini il "
                      f"y a {_duree(diag['maintenant'] - dernier['fin'])}")
            if dernier["apres_redemarrage"] is True:
                ligne += " (après un redémarrage du collecteur)"
            elif dernier["apres_redemarrage"] is False:
                ligne += " (sans redémarrage du collecteur)"
        l.append(ligne)
    if non_collectees:
        l.append("Au plafond mais non collectées (lues chez le broker, hors "
                 "diagnostic) : " + ", ".join(non_collectees))
    if diag.get("demarrage_collecteur") is not None:
        l.append(f"Dernier démarrage du collecteur : il y a "
                 f"{_duree(diag['maintenant'] - diag['demarrage_collecteur'])}"
                 f". Les reconnexions internes du socket n'apparaissent pas "
                 f"ici.")
    return "\n".join(l)


def depuis_la_course(course, *, maintenant: float,
                     demarrage_collecteur: float | None = None) -> str | None:
    """Le diagnostic à partir de la course VIVANTE, sans rien lui demander
    d'autre que de lire ses champs. None si elle n'a pas de mémoire."""
    cache = getattr(course, "_cache_bougies", None)
    if cache is None:
        return None
    paires = tuple(getattr(course, "paires_collectees", ()) or ())
    univers = tuple(getattr(course, "_univers", None) or ())
    p = getattr(getattr(course, "strategie", None), "p", None)
    fenetre = int(getattr(p, "lookback", 300) or 300)
    pique = getattr(p, "fenetre_pique", None)
    requis = 2 * int(pique) + 2 if pique else 26
    instantane = dict(cache)
    diag = diagnostiquer(instantane, paires, maintenant=maintenant,
                         fenetre_min=fenetre, requis=requis, plafond=univers,
                         demarrage_collecteur=demarrage_collecteur)
    return texte(diag, [u for u in univers if u not in set(paires)])


# --------------------------------------------------------------------------- #
# Messages du broker : reçus → transmis → enregistrés (diagnostic du 31/10)
# --------------------------------------------------------------------------- #

#: Les bougies de la minute en cours, et de la précédente, ne sont pas
#: encore toutes écrites : la comparaison s'arrête deux minutes avant.
RETARD_ECRITURE_SEC = 120
FENETRE_OBSERVATION_SEC = 3600


def fenetre_d_observation(maintenant: float, depuis: float
                          ) -> tuple[int, int]:
    """La dernière heure close, sans remonter avant le début de
    l'observation (première minute entière)."""
    fin = int(maintenant) // 60 * 60 - RETARD_ECRITURE_SEC
    premiere = (int(depuis) // 60 + 1) * 60
    return max(fin - FENETRE_OBSERVATION_SEC, premiere), fin


def enregistres(cache: Mapping[str, Sequence[Any]], paires: Iterable[str],
                debut: int, fin: int) -> dict[str, int]:
    """Ticks des bougies écrites, par paire, sur les minutes [debut, fin)."""
    return {p: sum(int(b.tick_count) for b in cache.get(p, ())
                   if debut <= b.ts_sec < fin) for p in paires}


def texte_observation(obs: Mapping[str, Any], ecrits: Mapping[str, int],
                      paires: Sequence[str]) -> str:
    debut, fin = obs["debut"], obs["fin"]
    if not obs["actif"]:
        entete = (f"⚠ Observation des messages DÉSACTIVÉE après une erreur "
                  f"({obs['erreur']}) : aucun compte n'est fiable.")
        return f"<b>Messages de ticks du broker</b>\n{entete}"
    if fin <= debut:
        return ("<b>Messages de ticks du broker</b> (observation passive) : "
                "moins d'une minute close depuis le démarrage, rien à "
                "comparer encore.")
    m = obs["messages"]
    n = m["messages"]

    def part(k: str) -> str:
        return f"{m[k]} ({100 * m[k] / n:.0f} %)" if n else "0"

    heure = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime(  # noqa: E731
        "%H:%M")
    l = [f"<b>Messages de ticks du broker</b> (observation passive, "
         f"{heure(debut)}–{heure(fin)} UTC, {(fin - debut) // 60} min, "
         f"{obs['sockets']} socket(s) additionnés)",
         f"- {n} messages ; plusieurs ticks : {part('plusieurs_ticks')} ; "
         f"plusieurs paires : {part('plusieurs_paires')} ; une même paire "
         f"répétée : {part('meme_paire_repetee')} ; éléments par message : "
         f"max {m['max_elements']}, moyenne "
         f"{(m['elements'] / n if n else 0):.2f}"
         + (f" ; hors drapeau (ignorés par la bibliothèque) : "
            f"{m['hors_drapeau']}" if m["hors_drapeau"] else "")
         + (f" ; sans élément retenu : {m['sans_tete_retenue']}"
            if m["sans_tete_retenue"] else "")
         + (f" ; éléments malformés : {m['malformes']}"
            if m["malformes"] else "")]
    lignes = []
    for p in paires:
        c = obs["paires"].get(p, {})
        r, a = c.get("recus", 0), c.get("transmis", 0)
        e = ecrits.get(p, 0)
        ligne = (f"- {p} : reçus {r} (dans {c.get('messages', 0)} messages, "
                 f"en tête {c.get('en_tete', 0)}) → transmis {a} → "
                 f"enregistrés {e}")
        if r > a:
            ligne += f" · ⚠ {r - a} reçus non transmis"
        if a != e:
            ligne += f" · écart transmis − enregistrés {a - e:+d}"
        if obs["remplacements"].get(p):
            ligne += (f" · tampon remplacé {obs['remplacements'][p]} fois "
                      f"(relu en entier)")
        lignes.append((r - a, p, ligne))
    l += [ligne for _, _, ligne in sorted(lignes, key=lambda x: (-x[0],
                                                                 x[1]))]
    autres = sorted(set(obs["paires"]) - set(paires))
    if autres:
        l.append(f"- reçues hors collecte (suivies par le courtier) : "
                 f"{len(autres)} paire(s)")
    l.append("Reçus et transmis sont comptés à la minute de réception, "
             "enregistrés à la minute du tick : quelques secondes de décalage "
             "aux bords. « En tête » = premier élément d'un message, le seul "
             "que la bibliothèque ajoute à son tampon.")
    return "\n".join(l)


def observation_depuis_la_course(course, observation, *, maintenant: float
                                 ) -> str | None:
    if observation is None:
        return None
    paires = tuple(getattr(course, "paires_collectees", ()) or ())
    cache = dict(getattr(course, "_cache_bougies", None) or {})
    debut, fin = fenetre_d_observation(maintenant, observation.depuis)
    obs = observation.instantane(debut, fin)
    return texte_observation(obs, enregistres(cache, paires, debut, fin),
                             paires)
