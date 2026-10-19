"""
/decision — où en est la décision de production, et quand tombera-t-elle ?

--- Pourquoi ce rapport ---------------------------------------------------

Le 19/10, après un mois d'expériences : « on perd du temps, je devais passer
en production en novembre et rien n'est concluant ». Le temps ne se perd pas
dans les tests mais dans les DONNÉES : un écart de 8 points de taux se
détecte sur environ 160 signaux indépendants, et chaque heure sans collecte
repousse d'autant la date. Ce rapport met les deux en face : pour chaque
question pré-inscrite, où en est son volume, à quel débit il avance, et à
quelle date tombera son verdict si la collecte tourne.

--- Les critères de production, écrits le 19/10 AVANT toute lecture ---------

Variante jouée : M0 (la référence 8), sauf si un modèle de la fenêtre
R8 est « soutenu » — alors celui dont l'EV par ordre est la plus haute.
Entrée : à la confirmation (T2), sauf si R8-T0 est « soutenue » — alors au
signal.

Passage en production (compte réel) si, sur la fenêtre indépendante :
  1. au moins 300 entrées de la variante jouée ;
  2. la borne basse de l'intervalle à 95 % (par blocs paire × jour) de son
     EV par ordre au-dessus de 0 ;
  3. son EV encore positive sans les deux paires, puis sans la journée,
     qui y contribuent le plus ;
  4. au moins 80 % du temps de la fenêtre réellement collecté ;
et, vérifiés à la main : le taux des ordres démo de la course (/bilan) pas
plus de 5 points sous le taux rejoué sur au moins 50 ordres, et le payout
réellement obtenu d'au moins 90 %.

1 à 4 remplis : ✅ production possible. Volume atteint mais 2 ou 3 non
remplis : ❌ pas de production sur cette variante. Sinon : ⏳ en attente,
avec la date estimée.

⚠ Ce rapport ne décide rien seul et ne change aucune règle : il dit quand
la décision deviendra possible et ce qui la retarde.
"""

from __future__ import annotations

import time
from typing import Mapping

from maxprofit.live.preinscriptions import (
    N_MIN_T, OUVERTURE_R8, ouverture_r8, ouverture_t)

N_MIN_PRODUCTION = 300
COLLECTE_MIN = 0.80
N_MIN_ECHEANCE = 100
JOUR = 86400


def _rythme(n_fenetre: int, jours_fenetre: float, n_hist: int | None,
            jours_hist: float | None) -> float | None:
    """Signaux par jour de collecte : ceux de la fenêtre dès qu'elle a
    une demi-journée de collecte, sinon ceux de l'historique."""
    if jours_fenetre >= 0.5 and n_fenetre:
        return n_fenetre / jours_fenetre
    if n_hist and jours_hist:
        return n_hist / jours_hist
    return None


def _eta(n: int, cible: int | None, rythme: float | None,
         maintenant: float) -> int | None:
    """La date (s UTC) où `cible` sera atteinte si la collecte tourne en
    continu au rythme mesuré."""
    if cible is None:
        return None
    if n >= cible:
        return int(maintenant)
    if not rythme:
        return None
    return int(maintenant + (cible - n) / rythme * JOUR)


def _question(ident: str, libelle: str, n: int, cible: int | None,
              rythme: float | None, verdict: str, maintenant: float) -> dict:
    return {"id": ident, "libelle": libelle, "n": n, "cible": cible,
            "verdict": verdict, "eta": _eta(n, cible, rythme, maintenant)}


def decision(laboratoire: Mapping | None, maintenant: float | None = None
             ) -> dict:
    """Le rapport, en JSON, à partir du laboratoire du dernier rejeu."""
    maintenant = time.time() if maintenant is None else maintenant
    lab = laboratoire or {}
    r8, en = lab.get("reference8") or {}, lab.get("entrees") or {}
    if "fenetre" not in r8:
        return {"note": "Mission Reference 8 pas encore calculée : il faut un "
                        "rejeu complet."}
    f8 = r8["fenetre"]
    jours8 = float(f8.get("jours_collecte") or 0.0)
    rythme8 = _rythme(f8.get("n", 0), jours8, r8.get("n"), r8.get("jours"))
    calendrier8 = max(0.0, (maintenant - ouverture_r8()) / JOUR)
    questions = []
    for ident, m in (f8.get("modeles") or {}).items():
        questions.append(_question(
            f"R8-{ident}", f"modèle {ident} contre M0", f8.get("n", 0),
            m.get("cible"), rythme8, m.get("verdict", ""), maintenant))
    h = f8.get("echeance") or {}
    questions.append(_question(
        "R8-H", "échéance choisie selon B, C, D", h.get("n", 0),
        N_MIN_ECHEANCE, rythme8, h.get("verdict", "≈ en attente"),
        maintenant))
    rythme_t = None
    if "fenetre" in en:
        ft = en["fenetre"]
        rythme_t = _rythme(ft.get("n", 0), float(ft.get("jours_collecte")
                                                 or 0.0),
                           en.get("n"), en.get("jours"))
        for ident, v in (ft.get("verdicts") or {}).items():
            # La fenêtre T ne compte qu'à partir de son ouverture.
            depart = max(maintenant, ouverture_t())
            q = _question(ident, "entrée " + ("au signal" if ident == "R8-T0"
                                              else "annulée si trop loin")
                          + " contre la confirmation", ft.get("n", 0),
                          N_MIN_T, rythme_t, v.get("verdict", ""), depart)
            questions.append(q)
    p2 = ((lab.get("protocole") or {}).get("exp004") or {}).get(
        "validation_p2") or {}
    gel = p2.get("gel") or {}
    if gel:
        n_a = ((p2.get("groupes") or {}).get("A") or {}).get("n", 0)
        debit = (gel.get("plan") or {}).get("debit_calendaire")
        questions.append(_question(
            "P2", "filtres C et D (fenêtre du 14/10)", n_a, gel.get("cible"),
            debit, p2.get("etape") or "", maintenant))

    # La production : variante jouée, puis les quatre critères.
    soutenus = [(m.get("ev") or -9, ident)
                for ident, m in (f8.get("modeles") or {}).items()
                if str(m.get("verdict", "")).startswith("✅")]
    variante = max(soutenus)[1] if soutenus else "M0"
    t0 = ((en.get("fenetre") or {}).get("verdicts") or {}).get("R8-T0", {})
    entree = ("au signal (T0)" if str(t0.get("verdict", "")).startswith("✅")
              else "à la confirmation (T2)")
    ev = (f8.get("ev_fenetre") or {}).get(variante) or {}
    n = ev.get("n", 0)
    ic = ev.get("ic")
    part = (jours8 / calendrier8) if calendrier8 > 0 else None
    criteres = {
        "volume": n >= N_MIN_PRODUCTION,
        "ev_positive": None if not ic else ic[0] > 0,
        "robuste": (None if ev.get("sans_deux_paires") is None
                    or ev.get("sans_meilleur_jour") is None
                    else ev["sans_deux_paires"] > 0
                    and ev["sans_meilleur_jour"] > 0),
        "collecte": None if part is None else part >= COLLECTE_MIN,
    }
    if all(criteres.values()):
        statut = "✅ production possible (critères 1 à 4 remplis)"
    elif criteres["volume"] and (criteres["ev_positive"] is False
                                 or criteres["robuste"] is False):
        statut = f"❌ pas de production sur {variante}"
    else:
        statut = "⏳ pas encore décidable"
    # La variante jouée en production compte les entrées de M0 qu'elle
    # garde : son débit est celui de M0 réduit d'autant.
    part_variante = ((ev.get("n") or 0) / f8["n"]) if f8.get("n") else None
    rythme_variante = (rythme8 * part_variante if rythme8 and part_variante
                       else rythme8)
    return {
        "maintenant": int(maintenant), "questions": questions,
        "rythme_r8": _rond(rythme8), "rythme_t": _rond(rythme_t),
        "collecte_fenetre": _rond(part, 3), "jours_collecte": jours8,
        "jours_calendrier": round(calendrier8, 2),
        "production": {
            "variante": variante, "entree": entree, "ev": ev,
            "criteres": criteres, "statut": statut,
            "eta": _eta(n, N_MIN_PRODUCTION, rythme_variante, maintenant)},
    }


def _rond(x: float | None, k: int = 2) -> float | None:
    return None if x is None else round(x, k)


# --------------------------------------------------------------------------- #
# Texte
# --------------------------------------------------------------------------- #

def _date(ts: int | None) -> str:
    return "—" if ts is None else time.strftime("%d/%m", time.gmtime(ts))


def _mark(x: bool | None) -> str:
    return "⏳" if x is None else ("✅" if x else "❌")


def texte(d: Mapping | None) -> str:
    """`/decision`."""
    if not d:
        return ("🎯 Décision pas encore calculable : elle lit le dernier "
                "rejeu.")
    if "note" in d:
        return f"🎯 {d['note']}"
    p = d["production"]
    ev = p["ev"]
    c = p["criteres"]
    collecte = d["collecte_fenetre"]
    lignes = [
        "🎯 <b>Décision de production — où en est-on ?</b>",
        f"Fenêtre indépendante ouverte le {OUVERTURE_R8[8:]}/"
        f"{OUVERTURE_R8[5:7]} : {d['jours_collecte']:.1f} jour(s) collectés "
        f"sur {d['jours_calendrier']:.1f} de calendrier"
        + (f" ({collecte:.0%})" if collecte is not None else "")
        + f". Débit : {d['rythme_r8'] or '—'} entrées jouées par jour de "
          f"collecte" + (f", {d['rythme_t']} signaux pour T0/T3"
                         if d.get("rythme_t") else "") + ".",
        "Les dates supposent une collecte CONTINUE : chaque heure sans "
        "collecte les repousse d'autant.", "",
        "<b>1. Production</b> (critères écrits le 19/10, avant lecture)",
        f"Variante jouée : {p['variante']}, entrée {p['entree']}.",
        f"{_mark(c['volume'])} 1. volume : {ev.get('n', 0)}/"
        f"{N_MIN_PRODUCTION} entrées sur la fenêtre",
        f"{_mark(c['ev_positive'])} 2. EV par ordre "
        + ("—" if ev.get("ev") is None else f"{ev['ev']:+.3f}")
        + (f", intervalle [{ev['ic'][0]:+.2f} ; {ev['ic'][1]:+.2f}] — borne "
           f"basse au-dessus de 0 ?" if ev.get("ic") else
           " — intervalle pas encore calculable"),
        f"{_mark(c['robuste'])} 3. EV sans les 2 paires qui y contribuent "
        f"le plus, puis sans la meilleure journée, encore positive",
        f"{_mark(c['collecte'])} 4. au moins 80 % du temps collecté",
        "À vérifier à la main : taux des ordres démo (/bilan) pas plus de "
        "5 points sous le taux rejoué sur 50 ordres ; payout obtenu d'au "
        "moins 90 %.",
        f"→ <b>{p['statut']}</b>"
        + ("" if p["statut"].startswith(("✅", "❌")) else
           f" — volume atteint vers le {_date(p['eta'])}"), "",
        "<b>2. Les questions en cours</b> (volume / cible → date estimée)"]
    for q in d["questions"]:
        cible = q["cible"] if q["cible"] is not None else "—"
        fait = str(q["verdict"]).startswith(("✅", "❌"))
        quand = "tranché" if fait else f"vers le {_date(q['eta'])}"
        lignes.append(f"• {q['id']} — {q['libelle']} : {q['n']}/{cible} → "
                      f"{quand}")
    lignes += [
        "", "<b>3. Ce qui raccourcit le délai</b>",
        "• la collecte en continu : c'est le seul levier qui double le "
        "débit sans toucher aux règles ;",
        "• les 20 paires souscrites en permanence (12 n'étaient couvertes "
        "qu'à 24 %) ;",
        "• aucune nouvelle hypothèse d'ici là : chacune en plus allonge la "
        "liste des faux positifs possibles, pas la fenêtre.",
        "", "⚠ Ce rapport ne décide rien seul et ne change aucune règle. "
            "Même ✅, la production commence à mise fixe réduite, sans "
            "martingale, avec un arrêt journalier."]
    return "\n".join(lignes)
