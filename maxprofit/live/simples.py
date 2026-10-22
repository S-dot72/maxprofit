"""
/simples — trois stratégies simples sur toutes les bougies M1 (EXP-030,
2026-10-22) : momentum de 7 bougies, croisement de deux moyennes mobiles du
prix, repli jusqu'à la médiane de Bollinger en tendance haussière.

Les règles et leurs raisons sont dans `apprentissage.simples` ; les
pré-inscriptions S-MOM7, S-EMA et S-BB dans `live.preinscriptions`.

--- Deux lectures, à ne pas confondre -----------------------------------------

1. **L'exploration** : chaque règle à chaque échéance, anciens (70 % de la
   période) et récents (30 %). ✅ au-dessus de 52,1 % sur les DEUX périodes,
   🔁 sous 47,9 % sur les deux (c'est alors le pari inverse qui paierait).
   Quarante-deux cases : le rapport dit combien le hasard seul en
   marquerait, et une case marquée est une PISTE.
2. **La fenêtre indépendante**, ouverte le 23/10 : la seule qui juge. S-MOM7
   y est fixée ; pour S-EMA et S-BB, la case (couple ou variante, échéance)
   est choisie sur les signaux d'avant la fenêtre, figée au premier rejeu qui
   suit l'ouverture et jamais rechoisie.

Des mesures : la course ne lit rien d'ici.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Callable, Mapping

from maxprofit.apprentissage.simples import FAMILLES, REGLES
from maxprofit.live.preinscriptions import (
    DECLAREES_S, JOURS_MIN_S, N_MIN_CHOIX_S, N_MIN_S, PREINSCRIPTIONS_S,
    SEUIL_RENTABLE, juger, ouverture_s)
from maxprofit.live.stabilite import intervalle_des_blocs

SEUIL_INVERSE = 1 - SEUIL_RENTABLE
_LIBELLES = {cle: libelle for cle, _f, libelle in REGLES}


def _ev(n: int, g: int) -> float | None:
    """Espérance par ordre à 92 % : p × 0,92 − (1 − p)."""
    return 1.92 * g / n - 1 if n else None


def _au_dela_par_hasard(n: int, seuil: float) -> float:
    """P(taux observé au-delà de `seuil` | taux réel 50 %), approximation
    normale."""
    if not n:
        return 0.0
    z = abs(seuil - 0.5) * 2 * math.sqrt(n)
    return 0.5 * math.erfc(z / math.sqrt(2))


def _marque(an: int, ag: int, rn: int, rg: int) -> str:
    if not an or not rn:
        return ""
    if ag / an > SEUIL_RENTABLE and rg / rn > SEUIL_RENTABLE:
        return "✅"
    if ag / an < SEUIL_INVERSE and rg / rn < SEUIL_INVERSE:
        return "🔁"
    return ""


def simples(res: Mapping, *, ancien: Mapping | None, fin: int,
            jours_collectes: Callable[[int, int], float] | None = None
            ) -> dict:
    """Le rapport, en JSON, à partir de `StrategiesSimples.resultat()`.
    Les blocs (paire, jour) n'y sont plus : réduits à des intervalles.
    `ancien` : le rapport du rejeu précédent, d'où vient le choix figé."""
    debut = res["debut"]
    jours = (jours_collectes(debut, fin) if jours_collectes else None) \
        or max(1.0, (fin - debut) / 86400)
    regles: dict = {}
    marquees, attendues = 0, 0.0
    for cle, _famille, _libelle in REGLES:
        regles[cle] = {}
        for sec, c in res["regles"][cle].items():
            an, ag, rn, rg = c["periodes"]
            marque = _marque(an, ag, rn, rg)
            ic = intervalle_des_blocs(c["blocs"]) if marque else None
            attendues += 2 * (_au_dela_par_hasard(an, SEUIL_RENTABLE)
                              * _au_dela_par_hasard(rn, SEUIL_RENTABLE))
            marquees += bool(marque)
            regles[cle][sec] = {
                "periodes": list(c["periodes"]), "avant": list(c["avant"]),
                "marque": marque, "ic": list(ic) if ic else None}
    return {"debut": debut, "fin": fin, "coupure": res["coupure"],
            "jours": round(jours, 2), "paires": len(res["paires"]),
            "parametres": res["parametres"], "regles": regles,
            "cases": [marquees, round(attendues, 1),
                      sum(len(v) for v in regles.values())],
            "fenetre": _fenetre(res, ancien, fin)}


def _choisir(res: Mapping) -> dict:
    """La case de chaque pré-inscription, sur les signaux d'AVANT la
    fenêtre : la meilleure espérance parmi celles qui ont au moins
    `N_MIN_CHOIX_S` signaux (à égalité, la première : couple le plus
    rapide, échéance la plus courte). Une pré-inscription à case unique
    (S-MOM7) la garde quoi qu'il arrive."""
    choix = {}
    for p in PREINSCRIPTIONS_S:
        candidates = [(cle, sec) for cle in p.parametres["regles"]
                      for sec in p.parametres["echeances"]]
        meilleure = None
        for cle, sec in candidates:
            n, g = res["regles"][cle][str(sec)]["avant"]
            if len(candidates) > 1 and n < N_MIN_CHOIX_S:
                continue
            ev = _ev(n, g)
            if meilleure is None or (ev is not None and (
                    meilleure["ev"] is None or ev > meilleure["ev"])):
                meilleure = {"regle": cle, "echeance": sec, "avant": [n, g],
                             "ev": ev}
        choix[p.ident] = meilleure
    return choix


def _fenetre(res: Mapping, ancien: Mapping | None, fin: int) -> dict:
    ouverture = ouverture_s()
    sortie: dict = {"ouverture": ouverture, "declarees_le": DECLAREES_S,
                    "n_min": N_MIN_S}
    if fin < ouverture:
        sortie["note"] = "pas encore ouverte"
        return sortie
    precedente = (ancien or {}).get("fenetre") or {}
    choix = precedente.get("choix") or _choisir(res)
    verdicts = {}
    for p in PREINSCRIPTIONS_S:
        deja = (precedente.get("verdicts") or {}).get(p.ident)
        if deja and deja.get("final"):
            verdicts[p.ident] = deja
            continue
        case = choix.get(p.ident)
        if not case:
            verdicts[p.ident] = {
                "n": 0, "g": 0, "ic": None,
                "verdict": f"indécise — aucune case n'avait "
                           f"{N_MIN_CHOIX_S} signaux avant la fenêtre"}
            continue
        blocs = res["regles"][case["regle"]][str(case["echeance"])][
            "blocs_fenetre"]
        n, g = sum(b[0] for b in blocs), sum(b[1] for b in blocs)
        ic = intervalle_des_blocs(blocs) if n else None
        v = {"n": n, "g": g, "ic": list(ic) if ic else None,
             "verdict": juger(n, g, ic, N_MIN_S)}
        if n >= N_MIN_S and fin - ouverture < JOURS_MIN_S * 86400:
            v["verdict"] = (f"indécise — {n} signaux, verdict au plus tôt "
                            f"le {_date(ouverture + JOURS_MIN_S * 86400)}")
        elif n >= N_MIN_S:
            v["final"] = True
            v["fige_le"] = fin
        verdicts[p.ident] = v
    sortie["choix"] = choix
    sortie["verdicts"] = verdicts
    return sortie


def hypotheses(r: Mapping | None) -> list[tuple[str, str, str]]:
    """(id, hypothèse, verdict) pour le journal du protocole : l'exploration
    de chaque règle (✅ si une de ses échéances l'est), puis la fenêtre."""
    if not r or "regles" not in r:
        return []
    sortie = []
    for cle, _f, libelle in REGLES:
        marques = [r["regles"][cle][s]["marque"] for s in r["regles"][cle]]
        verdict = ("✅ au-delà du seuil sur les deux périodes"
                   if any(marques) else "❌ dans le bruit à toutes les "
                                         "échéances")
        sortie.append((f"EXP-030 {cle}", libelle, verdict))
    for p in PREINSCRIPTIONS_S:
        v = (r.get("fenetre", {}).get("verdicts") or {}).get(p.ident)
        if v:
            sortie.append((p.ident, p.nom, v["verdict"]))
    return sortie


# --------------------------------------------------------------------------- #
# Texte
# --------------------------------------------------------------------------- #

def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d/%m")


def _pc(n: int, g: int) -> str:
    return f"{g / n:.1%}" if n else "—"


def _case(cle: str, sec: str) -> str:
    return f"{_LIBELLES[cle]}, {int(sec) // 60} min"


def texte(r: Mapping | None) -> str:
    if not r or "regles" not in r:
        return ("🧪 Stratégies simples : pas encore rejouées — le prochain "
                "rejeu du laboratoire les mesurera.")
    lignes = [
        f"🧪 <b>Trois stratégies simples</b> — EXP-030, toutes les bougies "
        f"M1 rejouées du {_date(r['debut'])} au {_date(r['fin'])} "
        f"({r['paires']} paires, {r['jours']:.1f} jours de collecte) ; "
        f"étalonnage jusqu'au {_date(r['coupure'])}, récents ensuite.",
        "Position prise à la clôture de la bougie, jugée à la clôture de "
        "l'échéance ; seuil 52,1 % (payout 92 % supposé). ✅ au-dessus sur "
        "les DEUX périodes ; 🔁 sous 47,9 % sur les deux (le pari inverse "
        "paierait).",
        "⚠ Surtout à 1 min : un ordre réel s'ouvre 2 à 3 s après la "
        "clôture, et le payout des échéances courtes est souvent sous "
        "92 %.", ""]
    remarquables = []
    for k, (famille, titre) in enumerate(FAMILLES, 1):
        lignes.append(f"<b>{k}. {titre}</b>")
        for cle, fam, libelle in REGLES:
            if fam != famille:
                continue
            cases = r["regles"][cle]
            premiere = next(iter(cases.values()))["periodes"]
            n = premiere[0] + premiere[2]
            lignes.append(f"{libelle} — {n} signaux, "
                          f"{n / r['jours']:.1f}/jour")
            morceaux = []
            for sec, c in cases.items():
                an, ag, rn, rg = c["periodes"]
                morceaux.append(f"{int(sec) // 60}m "
                                f"{_pc(an + rn, ag + rg)}{c['marque']}")
                if c["marque"]:
                    remarquables.append((cle, sec, c))
            lignes.append("  " + " · ".join(morceaux))
        lignes.append("")
    if remarquables:
        lignes.append("Au-delà du seuil sur les deux périodes :")
        for cle, sec, c in remarquables:
            an, ag, rn, rg = c["periodes"]
            ic = (f" · par blocs {c['ic'][0]:.0%}–{c['ic'][1]:.0%}"
                  if c["ic"] else "")
            n, g = an + rn, ag + rg
            if c["marque"] == "🔁":
                # Le pari inverse : c'est lui qui paierait.
                an, ag, rn, rg, g = an, an - ag, rn, rn - rg, n - g
                ic = (f" · par blocs {1 - c['ic'][1]:.0%}–"
                      f"{1 - c['ic'][0]:.0%}" if c["ic"] else "")
            lignes.append(
                f"  • {_case(cle, sec)}"
                f"{' — À L’ENVERS' if c['marque'] == '🔁' else ''} : "
                f"{_pc(n, g)} sur {n} (anciens {_pc(an, ag)} sur {an}, "
                f"récents {_pc(rn, rg)} sur {rn}){ic} · espérance "
                f"{_ev(n, g):+.3f}/ordre")
    marquees, attendues, total = r["cases"]
    lignes.append(
        f"Cases ✅ ou 🔁 : {marquees} sur {total} ; le hasard seul en "
        f"marquerait environ {attendues:.1f} — au moins, car des signaux "
        f"voisins se ressemblent.")
    lignes += ["", *_texte_fenetre(r["fenetre"]), "",
               "Des mesures, pas des règles : la course ne lit rien d'ici. "
               "Une case ✅ est une piste ; seule la fenêtre juge."]
    return "\n".join(lignes)


def _texte_fenetre(f: Mapping) -> list[str]:
    tete = (f"<b>Fenêtre indépendante</b> (pré-inscrite le "
            f"{f['declarees_le'][8:10]}/{f['declarees_le'][5:7]}, ouverte le "
            f"{_date(f['ouverture'])} à 0 h UTC ; verdict à {f['n_min']} "
            f"signaux de la règle)")
    if "note" in f:
        return [tete, "Pas encore ouverte. Le choix de S-EMA et S-BB sera "
                      "figé au premier rejeu qui suit l'ouverture, sur les "
                      "signaux d'avant."]
    lignes = [tete]
    for p in PREINSCRIPTIONS_S:
        case = f["choix"].get(p.ident)
        v = f["verdicts"].get(p.ident, {})
        if not case:
            lignes.append(f"• {p.nom} : {v.get('verdict', '—')}")
            continue
        n, g = v.get("n", 0), v.get("g", 0)
        ic = (f", par blocs {v['ic'][0]:.0%}–{v['ic'][1]:.0%}"
              if v.get("ic") else "")
        an, ag = case["avant"]
        lignes.append(
            f"• {p.ident} — {_case(case['regle'], str(case['echeance']))} "
            f"(avant : {_pc(an, ag)} sur {an}) → fenêtre {_pc(n, g)} sur "
            f"{n}/{f['n_min']}{ic} : {v.get('verdict', '—')}")
    return lignes
