"""
/entrees — le retard de la confirmation M1 : T0, T1, T2 et T3 sur les MÊMES
signaux (analyse du 2026-10-18 ; pré-inscriptions R8-T0 et R8-T3).

--- La question --------------------------------------------------------------

La confirmation M1 sélectionne de meilleurs signaux, mais le retard qu'elle
impose en reprend presque tout le bénéfice. Faut-il supprimer ce retard, le
réduire, ou seulement le limiter ? Quatre entrées, une seule population :

  T0  au signal (clôture de la bougie du signal) ;
  T1  à la bougie suivante, sans condition : le coût de l'attente seule ;
  T2  à la confirmation M1 actuelle, règle de l'heure relue à l'entrée ;
  T3  comme T2, mais annulée si l'entrée se trouve à x amplitudes M1 ou
      plus de la zone (x = 1,00, 1,25, 1,55, fixés avant lecture).

La population : les signaux ZoneH1 qui passent la règle de l'heure AU
SIGNAL, dont l'issue est déterminable pour chaque entrée qui serait placée
(une égalité ou une bougie manquante écarte le signal, et il est compté).
Une entrée non placée (signal non confirmé, entrée annulée) ne gagne ni ne
perd : elle compte zéro. D'où la mesure principale, le GAIN PAR
OPPORTUNITÉ — gain total / signaux du pool —, qui tient compte du volume ;
l'espérance par ordre, le taux et le nombre d'ordres sont donnés à côté.

--- Les décompositions --------------------------------------------------------

  T1 − T0   le retard pur (une minute, sans rien choisir) ;
  T2 − T1   ce que la confirmation choisit, au prix différé ;
  T2 − T0   l'effet total de la confirmation actuelle ;
  T3 − T2   ce que l'annulation des entrées trop éloignées rapporte.

Avec la confirmation sur la bougie suivante seulement, T1 et T2 entrent au
même prix sur les signaux confirmés : T2 − T1 isole alors exactement la
sélection.

--- Les règles du jeu ---------------------------------------------------------

- Rien ne change dans la stratégie ni dans la course : des mesures.
- Le seuil de T3 est choisi sur l'étalonnage (70 % des signaux) pour le
  rapport, et sur les signaux d'avant le 20/10 pour la fenêtre, où il est
  figé ; le verdict de la fenêtre tombe à 300 signaux et ne bouge plus.
- Sur l'historique, l'idée même (« entrer plus tôt ») vient de ces données :
  un résultat favorable y reste indéterminé.
- La confirmation anticipée dans la bougie (ticks) et la réaction mesurable
  à la zone ne sont pas ici : elles demandent un critère écrit d'avance et
  une couverture sous la minute vérifiée.
- Intervalles à 95 % par blocs paire × jour (1 000 tirages, graine 7) ;
  payout de 92 % supposé à toutes les échéances.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping, Sequence

from maxprofit.apprentissage.lecons import Exemple
from maxprofit.live.laboratoire import PART_ETALONNAGE
from maxprofit.live.preinscriptions import (
    N_MIN_T, OUVERTURE_T, SEUILS_ANNULATION, ouverture_t)
from maxprofit.live.reference8 import (
    _bootstrap, _confirme, _mcnemar, _quartiles, _rond, _verdict)

PAYOUT = 0.92
ECHEANCE = 900
HORIZONS: tuple[int, ...] = (240, 600, 900)
#: Signaux du pool en deçà desquels le rapport ne conclut rien.
N_MIN = 60
JOUR_T = f"{OUVERTURE_T[8:]}/{OUVERTURE_T[5:7]}"


def _t3(x: float) -> str:
    return f"T3-{x:.2f}"


VARIANTES: tuple[str, ...] = ("T0", "T1", "T2",
                              *(_t3(x) for x in SEUILS_ANNULATION))
LIBELLES = {
    "T0": "au signal",
    "T1": "bougie suivante, sans condition",
    "T2": "confirmation M1 actuelle",
    **{_t3(x): f"T2 annulée à {x:.2f} ou plus de la zone"
       for x in SEUILS_ANNULATION},
}


# --------------------------------------------------------------------------- #
# Un signal, quatre entrées
# --------------------------------------------------------------------------- #

def _b(x) -> bool | None:
    return None if x is None else bool(x)


def _a_l_entree(e: Exemple) -> Exemple:
    """Le signal décrit à son entrée confirmée (clés « e_ »)."""
    ctx = dict(e.contexte)
    ctx.update({c[2:]: v for c, v in e.contexte.items() if c.startswith("e_")})
    return replace(e, contexte=ctx)


def _signal(e: Exemple, fenetre: int,
            en_direct: Callable[[Exemple], bool]) -> dict:
    """Ce que chaque entrée ferait de ce signal : {horizon: (T0, T1, T2)}
    (issues, None si indéterminable ou pas d'ordre), si T2 place un ordre,
    et la distance de l'entrée confirmée à la zone."""
    c = e.contexte
    confirme = _confirme(e, fenetre)
    t2 = confirme and en_direct(_a_l_entree(e))
    return {
        "ts": e.ts_sec, "pair": e.pair, "t2": t2, "confirme": confirme,
        "issues": {sec: (_b(e.issues.get(sec)),
                         _b(c.get(f"gagne_suivant_{sec}")),
                         _b(c.get(f"gagne_confirme_{sec}")) if t2 else None)
                   for sec in HORIZONS},
        "distance": c.get("e_distance_niveau") if t2 else None,
        "distances": (c.get("distance_niveau"), c.get("suiv_distance_niveau"),
                      c.get("e_distance_niveau") if t2 else None),
        "mfe": (c.get("imm_mfe_15"), c.get("suiv_mfe_15"),
                c.get("mfe_15") if t2 else None),
        "mae": (c.get("imm_mae_15"), c.get("suiv_mae_15"),
                c.get("mae_15") if t2 else None),
    }


def _dans_le_pool(s: Mapping, sec: int) -> bool:
    t0, t1, t2 = s["issues"][sec]
    return t0 is not None and t1 is not None and (not s["t2"]
                                                  or t2 is not None)


def _ordres(s: Mapping, sec: int) -> dict[str, bool | None]:
    """{variante: issue} pour les variantes qui placent un ordre."""
    t0, t1, t2 = s["issues"][sec]
    sortie = {"T0": t0, "T1": t1}
    if s["t2"]:
        sortie["T2"] = t2
        for x in SEUILS_ANNULATION:
            if s["distance"] is None or s["distance"] < x:
                sortie[_t3(x)] = t2
    return sortie


# --------------------------------------------------------------------------- #
# Les comptes : un vecteur par bloc paire × jour
# --------------------------------------------------------------------------- #
#
# [signaux, puis (ordres, gagnés) pour chaque variante dans l'ordre de
# VARIANTES]. Une fonction des sommes donne une mesure ; le bootstrap la
# rééchantillonne par blocs.

def _indice(v: str) -> int:
    return 1 + 2 * VARIANTES.index(v)


def _vecteurs(pool: Sequence[Mapping], sec: int,
              cle=lambda s: (s["pair"], s["ts"] // 86400)
              ) -> dict[tuple, list[int]]:
    blocs: dict[tuple, list[int]] = {}
    for s in pool:
        b = blocs.setdefault(cle(s), [0] * (1 + 2 * len(VARIANTES)))
        b[0] += 1
        for v, issue in _ordres(s, sec).items():
            b[_indice(v)] += 1
            b[_indice(v) + 1] += int(issue)
    return blocs


def _somme(blocs: Mapping[tuple, Sequence[int]]) -> list[int]:
    return [sum(col) for col in zip(*blocs.values())] if blocs else []


def _gain_opp(s: Sequence[float], v: str) -> float | None:
    """Gain par opportunité de `v` : (0,92 g − perdus) / signaux."""
    if not s or not s[0]:
        return None
    n, g = s[_indice(v)], s[_indice(v) + 1]
    return (PAYOUT * g - (n - g)) / s[0]


def _ev(s: Sequence[float], v: str) -> float | None:
    n, g = s[_indice(v)], s[_indice(v) + 1]
    return (PAYOUT * g - (n - g)) / n if n else None


def _delta(a: str, b: str) -> Callable[[Sequence[float]], float | None]:
    """Gain par opportunité de `a` moins celui de `b`."""
    def f(s):
        ga, gb = _gain_opp(s, a), _gain_opp(s, b)
        return None if ga is None or gb is None else ga - gb
    return f


def _robustesse(blocs: Mapping[tuple, Sequence[int]], f) -> dict:
    """L'écart sans les deux paires, puis sans la journée, qui y
    contribuent le plus."""
    def sans(retires):
        reste = {k: v for k, v in blocs.items()
                 if k[0] not in retires and k[1] not in retires}
        return f(_somme(reste)) if reste else None
    paires = sorted({k[0] for k in blocs},
                    key=lambda p: (sans({p}) if sans({p}) is not None
                                   else 0.0, p))
    jours = sorted({k[1] for k in blocs},
                   key=lambda j: (sans({j}) if sans({j}) is not None
                                  else 0.0, j))
    return {"sans_deux_paires": _rond(sans(set(paires[:2])), 4),
            "paires_retirees": paires[:2],
            "sans_meilleur_jour": _rond(sans(set(jours[:1])), 4)}


def _serie(pool: Sequence[Mapping], v: str, sec: int) -> dict:
    """Creux maximal (mises) et plus longue série de pertes, ordres pris
    dans l'ordre des signaux, mise fixe."""
    cumul = sommet = creux = 0.0
    serie = pire = 0
    for s in pool:
        issue = _ordres(s, sec).get(v)
        if issue is None:
            continue
        cumul += PAYOUT if issue else -1.0
        sommet = max(sommet, cumul)
        creux = max(creux, sommet - cumul)
        serie = 0 if issue else serie + 1
        pire = max(pire, serie)
    return {"creux": round(creux, 2), "serie_max": pire}


# --------------------------------------------------------------------------- #
# Le rapport historique
# --------------------------------------------------------------------------- #

def _tableau(pool: Sequence[Mapping], sec: int, jours: float) -> dict:
    blocs = _vecteurs(pool, sec)
    s = _somme(blocs)
    lignes = []
    for v in VARIANTES:
        n, g = (s[_indice(v)], s[_indice(v) + 1]) if s else (0, 0)
        gain = _gain_opp(s, v)
        lignes.append({
            "id": v, "ordres": n, "annules": (s[0] - n) if s else 0, "g": g,
            "ev": _rond(_ev(s, v), 4) if s else None,
            "ic_ev": _bootstrap(blocs, lambda x, v=v: _ev(x, v)),
            "gain_opp": _rond(gain, 4),
            "ev_jour": (round(gain * s[0] / jours, 2)
                        if gain is not None and jours else None)})
    ecarts = {}
    paires_d = [("T1", "T0"), ("T2", "T1"), ("T2", "T0"),
                *((_t3(x), "T2") for x in SEUILS_ANNULATION)]
    for a, b in paires_d:
        f = _delta(a, b)
        ecarts[f"{a}−{b}"] = {"ecart": _rond(f(s), 4) if s else None,
                              "ic": _bootstrap(blocs, f)}
    return {"n": s[0] if s else 0, "variantes": lignes, "ecarts": ecarts,
            "blocs": blocs}


def entrees(exemples: Sequence[Exemple],
            en_direct: Callable[[Exemple], bool], fenetre: int = 1, *,
            ancien: Mapping | None = None, fin: int | None = None,
            jours_collectes: Callable[[int, int], float] | None = None
            ) -> dict:
    """Le rapport complet, en JSON. `exemples` : ZoneH1 rejouée (signaux
    bruts) ; `ancien` : le rapport précédent (registre et plan de la
    fenêtre)."""
    signaux = [_signal(e, fenetre, en_direct)
               for e in sorted(exemples, key=lambda e: e.ts_sec)
               if en_direct(e)]
    pool = [s for s in signaux if _dans_le_pool(s, ECHEANCE)]
    precedente = (ancien or {}).get("fenetre")
    if len(pool) < N_MIN:
        return {"note": f"Seulement {len(pool)} signaux dans le pool : il "
                        f"en faut {N_MIN}.",
                **({"fenetre": precedente} if precedente else {})}
    d0, d1 = pool[0]["ts"], pool[-1]["ts"]
    fin = d1 if fin is None else fin
    jours = max(1.0, (jours_collectes and jours_collectes(d0 - 60, d1))
                or (d1 - d0) / 86400)
    tableaux = {}
    for sec in HORIZONS:
        t = _tableau([s for s in signaux if _dans_le_pool(s, sec)], sec,
                     jours)
        t.pop("blocs")
        tableaux[str(sec)] = t
    principal = _tableau(pool, ECHEANCE, jours)
    blocs = principal.pop("blocs")
    tableaux[str(ECHEANCE)] = principal
    # T0 contre T1, signal par signal.
    table = [0, 0, 0, 0]
    for s in pool:
        t0, t1, _t2 = s["issues"][ECHEANCE]
        table[(0 if t0 else 2) + (0 if t1 else 1)] += 1
    # Les prix d'entrée et les trajectoires.
    def colonne(nom, k):
        return _quartiles([s[nom][k] for s in pool if s[nom][k] is not None])
    prix = {nom: {v: colonne(nom, k) for k, v in enumerate(("T0", "T1",
                                                             "T2"))}
            for nom in ("distances", "mfe", "mae")}
    annulees = {}
    for x in SEUILS_ANNULATION:
        sous = [s for s in pool if s["t2"] and s["distance"] is not None
                and s["distance"] >= x]
        annulees[f"{x:.2f}"] = [len(sous), sum(
            int(s["issues"][ECHEANCE][2]) for s in sous)]
    # Étalonnage et récents ; le seuil de T3 choisi sur l'étalonnage.
    coupure = pool[int(len(pool) * PART_ETALONNAGE)]["ts"]
    anciens = [s for s in pool if s["ts"] < coupure]
    recents = [s for s in pool if s["ts"] >= coupure]
    sa = _somme(_vecteurs(anciens, ECHEANCE))
    sr = _somme(_vecteurs(recents, ECHEANCE))
    periodes = {v: {"anciens": [sa[_indice(v)], sa[_indice(v) + 1]],
                    "recents": [sr[_indice(v)], sr[_indice(v) + 1]],
                    "gain_anciens": _rond(_gain_opp(sa, v), 4),
                    "gain_recents": _rond(_gain_opp(sr, v), 4)}
                for v in VARIANTES} if sa and sr else {}
    choisi = choisir_seuil(sa) if sa else None
    hors = None
    if choisi is not None and sr:
        f = _delta(_t3(choisi), "T2")
        hors = {"seuil": choisi, "ecart": _rond(f(sr), 4),
                "ic": _bootstrap(_vecteurs(recents, ECHEANCE), f)}
    # Par paire, pour T0, T2 et T3 au seuil choisi.
    par_paire = {}
    for v in ("T0", "T2", _t3(choisi or SEUILS_ANNULATION[-1])):
        sp = {}
        for s in pool:
            issue = _ordres(s, ECHEANCE).get(v)
            if issue is None:
                continue
            x = sp.setdefault(s["pair"], [0, 0])
            x[0] += 1
            x[1] += int(issue)
        par_paire[v] = sp
    return {
        "n": len(pool), "signaux": len(signaux),
        "exclus": len(signaux) - len(pool), "jours": round(jours, 1),
        "periode": [d0, d1], "fenetre_confirmation": fenetre,
        "t2_ordres": sum(1 for s in pool if s["t2"]),
        "ecartes_a_l_entree": sum(1 for s in pool
                                  if s["confirme"] and not s["t2"]),
        "tableaux": tableaux,
        "t0_t1": {"table": table, "mcnemar": _mcnemar(table[1], table[2])},
        "prix": prix, "annulees": annulees,
        "robustesse": {
            "T0−T2": _robustesse(blocs, _delta("T0", "T2")),
            **{f"{_t3(x)}−T2": _robustesse(blocs, _delta(_t3(x), "T2"))
               for x in SEUILS_ANNULATION}},
        "series": {v: _serie(pool, v, ECHEANCE) for v in VARIANTES},
        "periodes": periodes, "seuil_etalonnage": hors,
        "par_paire": par_paire,
        "fenetre": _fenetre(signaux, precedente, fin,
                            (jours_collectes(ouverture_t(), fin)
                             if jours_collectes and fin > ouverture_t()
                             else 0.0)),
    }


def choisir_seuil(sommes: Sequence[float]) -> float | None:
    """Le seuil de T3 au meilleur gain par opportunité ; à égalité, le
    plus grand (celui qui annule le moins)."""
    candidats = [(g, x) for x in SEUILS_ANNULATION
                 if (g := _gain_opp(sommes, _t3(x))) is not None]
    return max(candidats)[1] if candidats else None


# --------------------------------------------------------------------------- #
# La fenêtre indépendante (ouverte le 20/10)
# --------------------------------------------------------------------------- #

def fiche(s: Mapping) -> list:
    """[signal (s), paire, T0, T1, T2 place un ordre, T2, distance] à
    15 min : de quoi rejuger T0 à T3 sans rejouer."""
    t0, t1, t2 = s["issues"][ECHEANCE]
    return [int(s["ts"]), s["pair"], _i(t0), _i(t1), int(bool(s["t2"])),
            _i(t2), None if s["distance"] is None
            else round(float(s["distance"]), 4)]


def _i(x) -> int | None:
    return None if x is None else int(bool(x))


def _depuis_fiche(f: Sequence) -> dict:
    return {"ts": int(f[0]), "pair": f[1], "t2": bool(f[4]),
            "issues": {ECHEANCE: (_b(f[2]), _b(f[3]), _b(f[5]))},
            "distance": f[6]}


def _cumuler(ancien: Sequence[Sequence], nouveau: Sequence[Sequence]
             ) -> list[list]:
    fiches: dict[tuple[str, int], list] = {}
    for f in (*ancien, *nouveau):
        fiches.setdefault((f[1], int(f[0])), list(f))
    return sorted(fiches.values(), key=lambda f: (f[0], f[1]))


def _juger(blocs: Mapping[tuple, Sequence[int]], variante: str) -> dict:
    s = _somme(blocs)
    f = _delta(variante, "T2")
    ecart = f(s) if s else None
    ic = _bootstrap(blocs, f)
    rob = _robustesse(blocs, f) if blocs else {}
    ev = _ev(s, variante) if s else None
    criteres = {
        "esperance_positive": None if ev is None else ev > 0,
        "gain_etabli": None if not ic else ic[0] > 0,
        "pas_deux_paires": (None if rob.get("sans_deux_paires") is None
                            else rob["sans_deux_paires"] > 0),
        "pas_une_journee": (None if rob.get("sans_meilleur_jour") is None
                            else rob["sans_meilleur_jour"] > 0)}
    if ecart is None or ecart <= 0:
        verdict = "❌ non soutenue — gain par opportunité pas au-dessus de T2"
    elif all(criteres.values()):
        verdict = "✅ soutenue — les trois critères"
    else:
        manque = ", ".join(k for k, v in criteres.items() if not v)
        verdict = f"≈ indéterminée — écart positif, manque : {manque}"
    return {"n": s[0] if s else 0, "ev": _rond(ev, 4),
            "ecart": _rond(ecart, 4), "ic": ic, **rob,
            "criteres": criteres, "verdict": verdict}


def _fenetre(signaux: Sequence[Mapping], ancien: Mapping | None,
             fin: int, jours_collecte: float = 0.0) -> dict:
    ouv = ouverture_t()
    ancien = ancien or {}
    nouveaux = [fiche(s) for s in signaux
                if s["ts"] >= ouv and _dans_le_pool(s, ECHEANCE)]
    registre = [f for f in _cumuler(ancien.get("registre") or (), nouveaux)
                if int(f[0]) >= ouv]
    sortie: dict = {"ouverture": ouv, "n": len(registre)}
    plan = dict(ancien.get("plan") or {})
    if not plan.get("fige_le"):
        avant = [s for s in signaux
                 if s["ts"] < ouv and _dans_le_pool(s, ECHEANCE)]
        sommes = _somme(_vecteurs(avant, ECHEANCE))
        plan = {"n": len(avant),
                "seuil": choisir_seuil(sommes) if sommes else None}
        if fin >= ouv and plan["seuil"] is not None:
            plan["fige_le"] = fin
    sortie["plan"] = plan
    finals = dict(ancien.get("finals") or {})
    verdicts = {}
    pool = [_depuis_fiche(f) for f in registre]
    blocs = _vecteurs(pool, ECHEANCE)
    for ident, variante in (("R8-T0", "T0"),
                            ("R8-T3", _t3(plan["seuil"])
                             if plan.get("seuil") is not None else None)):
        if ident in finals:
            verdicts[ident] = finals[ident]
            continue
        if variante is None or not plan.get("fige_le"):
            verdicts[ident] = {"n": len(pool), "verdict": (
                f"≈ indéterminée — fenêtre du {JOUR_T} pas encore ouverte "
                f"(seuil de T3 figé à son premier rejeu)")}
            continue
        jugement = _juger(blocs, variante)
        jugement["variante"] = variante
        if len(pool) < N_MIN_T:
            jugement["verdict"] = (f"≈ indéterminée — {len(pool)} signaux "
                                   f"sur {N_MIN_T}")
        else:
            jugement["fige_le"] = fin
            finals[ident] = jugement
        verdicts[ident] = jugement
    sortie["verdicts"] = verdicts
    sortie["finals"] = finals
    sortie["jours_collecte"] = round(jours_collecte, 2)
    sortie["registre"] = registre
    return sortie


# --------------------------------------------------------------------------- #
# Décisions et texte
# --------------------------------------------------------------------------- #

def hypotheses(r: Mapping | None) -> list[tuple[str, str, str]]:
    """(id, hypothèse, verdict), pour le rapport et le journal."""
    if not r or "note" in r:
        return []
    t = r["tableaux"][str(ECHEANCE)]
    e, n = t["ecarts"], t["n"]
    sortie = [
        ("T-retard", "attendre une minute sans condition coûte (T1 contre "
                     "T0, gain par opportunité)",
         _verdict(e["T1−T0"]["ecart"], e["T1−T0"]["ic"], n, sens=-1)),
        ("T-selection", "la confirmation choisit mieux que l'attente seule "
                        "(T2 contre T1)",
         _verdict(e["T2−T1"]["ecart"], e["T2−T1"]["ic"], n)),
        ("T0-hist", "entrer au signal bat la confirmation actuelle "
                    "(historique, T0 contre T2)",
         _verdict(-e["T2−T0"]["ecart"] if e["T2−T0"]["ecart"] is not None
                  else None,
                  [-e["T2−T0"]["ic"][1], -e["T2−T0"]["ic"][0]]
                  if e["T2−T0"]["ic"] else None, n, concu=True))]
    hors = r.get("seuil_etalonnage")
    if hors:
        sortie.append((
            "T3-hist", f"annuler à {hors['seuil']:.2f} ou plus de la zone "
                       f"bat T2 (seuil choisi sur l'étalonnage, jugé sur les "
                       f"récents)",
            _verdict(hors["ecart"], hors["ic"],
                     r["periodes"]["T0"]["recents"][0], concu=True)))
    for ident, v in r["fenetre"]["verdicts"].items():
        enonce = ("entrer au signal bat la confirmation (fenêtre du "
                  f"{JOUR_T})" if ident == "R8-T0" else
                  f"annuler l'entrée confirmée trop éloignée bat T2 (fenêtre "
                  f"du {JOUR_T})")
        sortie.append((ident, enonce, v["verdict"]))
    return sortie


def _p(n: int, g: int) -> str:
    return f"{g / n:.0%}" if n else "—"


def _e(x: float | None) -> str:
    return "—" if x is None else f"{x:+.3f}"


def _p3(x: float | None) -> str:
    return "—" if x is None else f"{x:.3f}"


def _e2(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}"


def _ic(ic: Sequence[float] | None) -> str:
    return "—" if not ic else f"[{ic[0]:+.2f} ; {ic[1]:+.2f}]"


def _q(q: Sequence[float] | None) -> str:
    return "—" if not q else f"{q[1]:.2f}"


def texte(r: Mapping | None) -> str:
    """`/entrees`."""
    if not r:
        return ("🚪 Comparaison des entrées pas encore calculée : elle "
                "accompagne le rejeu (quelques minutes après un "
                "redémarrage).")
    if "note" in r:
        return f"🚪 {r['note']}"
    t = r["tableaux"][str(ECHEANCE)]
    lignes = [
        "🚪 <b>Entrées T0 à T3 — le retard de la confirmation</b>",
        f"{r['n']} signaux ZoneH1 (règle de l'heure au signal), "
        f"{r['jours']} jours de collecte ; {r['exclus']} écartés faute "
        f"d'issue déterminable. T2 place {r['t2_ordres']} ordres "
        f"({r['ecartes_a_l_entree']} confirmés écartés par la règle de "
        f"l'heure relue à l'entrée). Un signal "
        f"sans ordre compte zéro : le gain par opportunité tient compte du "
        f"volume. Des mesures : aucune règle n'est modifiée.", "",
        "<b>1. À 15 minutes</b> (EV par ordre, payout 92 %)",
        "<pre>var.      ordres annul. taux  EV/ordre gain/opp EV/jour\n"
        + "\n".join(
            f"{v['id']:<9}{v['ordres']:>6}{v['annules']:>7} "
            f"{_p(v['ordres'], v['g']):>5} {_e(v['ev']):>8} "
            f"{_e(v['gain_opp']):>8} {_e2(v['ev_jour']):>7}"
            for v in t["variantes"]) + "</pre>",
        "IC de l'EV par ordre (par blocs) : " + " · ".join(
            f"{v['id']} {_ic(v['ic_ev'])}" for v in t["variantes"]), "",
        "<b>2. Décomposition</b> (écart de gain par opportunité, IC par "
        "blocs paire × jour)"]
    noms = {"T1−T0": "retard pur", "T2−T1": "sélection de la confirmation",
            "T2−T0": "effet total de la confirmation"}
    for cle, x in t["ecarts"].items():
        nom = noms.get(cle, "annulation")
        lignes.append(f"• {cle} ({nom}) : {_e(x['ecart'])} {_ic(x['ic'])}")
    for sec in (240, 600):
        u = r["tableaux"][str(sec)]
        lignes.append(f"À {sec // 60} min ({u['n']} signaux), gain par "
                      f"opportunité : " + " · ".join(
                          f"{v['id']} {_e(v['gain_opp'])}"
                          for v in u["variantes"]))
    tb = r["t0_t1"]["table"]
    lignes += [
        "", "<b>3. T0 contre T1</b>, signal par signal (15 min) : les deux "
        f"gagnent {tb[0]}, seul T0 {tb[1]}, seul T1 {tb[2]}, aucun {tb[3]} ; "
        f"McNemar (test apparié) {_p3(r['t0_t1']['mcnemar'])}.",
        "", "<b>4. Prix d'entrée et trajectoire</b> (médianes, amplitudes M1)"]
    for nom, libelle in (("distances", "distance à la zone"),
                         ("mfe", "MFE 15 min"), ("mae", "MAE 15 min")):
        q = r["prix"][nom]
        lignes.append(f"{libelle} : T0 {_q(q['T0'])} · T1 {_q(q['T1'])} · "
                      f"T2 {_q(q['T2'])}")
    lignes.append("Entrées confirmées annulées par T3 (ce qu'on perdrait) : "
                  + " · ".join(f"à {x} : {v[0]} ({_p(*v)} gagnées)"
                               for x, v in r["annulees"].items()))
    lignes += ["", "<b>5. Stabilité</b>"]
    for v, x in r["periodes"].items():
        lignes.append(
            f"{v} : anciens {_p(*x['anciens'])} sur {x['anciens'][0]} "
            f"(gain/opp {_e(x['gain_anciens'])}), récents "
            f"{_p(*x['recents'])} sur {x['recents'][0]} (gain/opp "
            f"{_e(x['gain_recents'])}) ; creux {r['series'][v]['creux']} "
            f"mises, série de pertes {r['series'][v]['serie_max']}")
    hors = r.get("seuil_etalonnage")
    if hors:
        lignes.append(
            f"Seuil de T3 choisi sur l'étalonnage : {hors['seuil']:.2f} ; sur "
            f"les récents, T3 − T2 = {_e(hors['ecart'])} {_ic(hors['ic'])} — "
            f"indicatif, le verdict vient de la fenêtre.")
    for cle, x in r["robustesse"].items():
        lignes.append(f"{cle} sans les 2 paires qui y contribuent le plus "
                      f"{_e(x['sans_deux_paires'])}, sans la meilleure "
                      f"journée {_e(x['sans_meilleur_jour'])}")
    pp = r["par_paire"]
    t0 = pp.get("T0", {})
    autres = [v for v in pp if v != "T0"]
    grandes = sorted(t0, key=lambda p: -t0[p][0])[:8]
    if grandes:
        lignes.append("Par paire (" + " → ".join(["T0", *autres]) + ") : "
                      + " · ".join(
                          f"{p.replace('_otc', '')} " + " → ".join(
                              f"{_p(*pp[v].get(p, [0, 0]))}/"
                              f"{pp[v].get(p, [0, 0])[0]}"
                              for v in ("T0", *autres))
                          for p in grandes))
    f = r["fenetre"]
    plan = f["plan"]
    lignes += ["", f"<b>6. Fenêtre indépendante</b> (ouverte le {JOUR_T}, "
                   f"{f['n']} signaux ; verdict à {N_MIN_T})"]
    if plan.get("seuil") is not None:
        etat = "figé" if plan.get("fige_le") else "provisoire"
        lignes.append(f"Seuil de T3 {etat} sur {plan['n']} signaux d'avant "
                      f": {plan['seuil']:.2f}")
    lignes += ["", "<b>7. Décisions</b> (soutenue / non soutenue / "
                   "indéterminée)"]
    for ident, enonce, verdict in hypotheses(r):
        lignes.append(f"• {ident} — {enonce} : {verdict}")
    lignes += ["", "⚠ Payout de 92 % supposé à toutes les échéances. La "
                   "confirmation anticipée (ticks) et la réaction mesurable à "
                   "la zone viendront ensuite, avec un critère écrit "
                   "d'avance."]
    return "\n".join(lignes)
