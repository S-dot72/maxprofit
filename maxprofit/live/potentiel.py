"""
/potentiel — le potentiel de la zone, l'expiration dynamique, et les
filtres d'entrée, comparés sur les MÊMES entrées confirmées.

--- D'où il vient ----------------------------------------------------------

Demandé le 2026-10-14 : choisir l'expiration selon la zone (15 minutes
seulement si les rebonds précédents durent assez, si rien ne barre la route
et si la zone tient), résoudre l'entrée tardive, et lire le Stochastic
(14, 3, 3) avec les lignes 10 et 100. Et un ordre de test : mesurer d'abord
chaque filtre seul, puis leurs combinaisons, sur les mêmes signaux ; ne rien
retenir qui n'améliore pas l'espérance avec un volume acceptable.

--- Les cinq blocs -----------------------------------------------------------

1. L'expiration : 15 min fixe (la référence), 5 min fixe, le rythme des
   rebonds seul, la règle dynamique complète, « 15 min si propice, sinon
   rien », « 15 min si propice, sinon 5 min ».
2. Le potentiel de zone : chaque catégorie (faible, intermédiaire, élevé,
   instable, indéterminé) jugée à chaque horizon, avec le meilleur et le
   pire point des 15 minutes.
3. Les filtres d'entrée tardive (B taille de la bougie, C part parcourue,
   D place restante) : la référence, +C, +D, +B+C, +C+D, +B+C+D.
4. Les indicateurs : EMA 20/50, Stochastic (14, 3, 3) lignes 10/100,
   chandeliers — seuls puis combinés, achats et ventes à part.
5. L'entrée au repli : garder la confirmation sans payer son prix d'entrée.

Même protocole que /experiences : 70 % d'étalonnage, 30 % récents ; un
verdict ✅ seulement si c'est mieux sur LES DEUX périodes, d'au moins 5
points sur la récente, avec au moins 30 signaux récents. Toutes les
hypothèses d'ici entrent au journal de /protocole.

⚠ L'espérance suppose un payout de 92 % à TOUS les horizons : à vérifier
sur la plateforme avant de croire une ligne à 2 ou 5 minutes.
⚠ Des mesures, pas des règles : la course ne lit rien d'ici.
"""

from __future__ import annotations

import statistics
from dataclasses import replace
from typing import Callable, Mapping, Sequence

from maxprofit.apprentissage.lecons import Exemple
from maxprofit.apprentissage.potentiel import (
    CATEGORIES, HORIZONS_MIN, MOTIFS, RAISONS, horizon_au_plus)
from maxprofit.live.experiences import _compte, _juger, esperance
from maxprofit.live.laboratoire import (
    N_MIN_VALIDATION, PART_ETALONNAGE, confirmes)

ECHEANCE = 900
#: Le filtre B : la bougie d'entrée au moins 1,27 fois la médiane des 15
#: précédentes (le tiers haut d'EXP-020 B) est écartée.
TAILLE_MAX = 1.27
PART_TARDIVE = 0.46
ESPACE_MIN = 1.95
SEMAINE = 7 * 86400


def _ecarte_b(e: Exemple) -> bool:
    v = e.contexte.get("taille_bougie")
    return v is not None and v >= TAILLE_MAX


def _ecarte_c(e: Exemple) -> bool:
    v = e.contexte.get("part_parcourue")
    return v is not None and v >= PART_TARDIVE


def _ecarte_d(e: Exemple) -> bool:
    v = e.contexte.get("espace_obstacle")
    return v is not None and v < ESPACE_MIN


def _ema(e: Exemple) -> bool:
    return e.contexte.get("tendance_ema") == 1


def _stoch(e: Exemple) -> bool:
    return bool(e.contexte.get("stoch_extreme")
                or e.contexte.get("stoch_retournement"))


def _chandelier(e: Exemple) -> bool:
    return bool(e.contexte.get("motif_favorable"))


#: (identifiant, libellé, garde-t-on l'entrée ?)
FILTRES: tuple[tuple[str, str, Callable[[Exemple], bool]], ...] = (
    ("A", "référence (ZoneH1 + confirmation)", lambda e: True),
    ("+C", "sans part parcourue ≥ 0,46", lambda e: not _ecarte_c(e)),
    ("+D", "sans place sous 1,95", lambda e: not _ecarte_d(e)),
    ("+B+C", "sans grande bougie (≥ 1,27) ni part ≥ 0,46",
     lambda e: not (_ecarte_b(e) or _ecarte_c(e))),
    ("+C+D", "sans part ≥ 0,46 ni place sous 1,95",
     lambda e: not (_ecarte_c(e) or _ecarte_d(e))),
    ("+B+C+D", "les trois exclusions",
     lambda e: not (_ecarte_b(e) or _ecarte_c(e) or _ecarte_d(e))),
)
INDICATEURS: tuple[tuple[str, str, Callable[[Exemple], bool]], ...] = (
    ("A", "référence", lambda e: True),
    ("EMA", "tendance EMA 20/50 dans le sens du trade", _ema),
    ("STO", "Stochastic : extrême au signal ou retournement", _stoch),
    ("CHA", "chandelier favorable (englobante, marteau, étoile)",
     _chandelier),
    ("EMA+STO", "EMA et Stochastic", lambda e: _ema(e) and _stoch(e)),
    ("EMA+CHA", "EMA et chandelier", lambda e: _ema(e) and _chandelier(e)),
    ("TOUS", "EMA, Stochastic et chandelier",
     lambda e: _ema(e) and _stoch(e) and _chandelier(e)),
)


def _issue_a(e: Exemple, sec: int) -> bool | None:
    """L'issue de l'entrée confirmée à `sec` secondes, `None` si inconnue
    (égalité, ou bougie manquante)."""
    if sec == ECHEANCE:
        return e.gagne
    return e.issues.get(sec)


def _horizon_rythme(e: Exemple) -> int | None:
    d = e.contexte.get("duree_rebond")
    return None if d is None else horizon_au_plus(d) * 60


def _horizon_dynamique(e: Exemple) -> int | None:
    h = e.contexte.get("expiration_dynamique")
    return None if h is None else int(h)


def _propice_ou_5(e: Exemple) -> int | None:
    p = e.contexte.get("propice_15")
    return None if p is None else (ECHEANCE if p else 300)


#: (identifiant, libellé, horizon choisi pour l'entrée — None : pas d'ordre)
EXPIRATIONS: tuple[tuple[str, str, Callable[[Exemple], int | None]], ...] = (
    ("15", "15 min fixe (référence)", lambda e: ECHEANCE),
    ("5", "5 min fixe", lambda e: 300),
    ("RYT", "rythme des rebonds précédents seul", _horizon_rythme),
    ("DYN", "expiration dynamique (règle complète)", _horizon_dynamique),
    ("P15", "15 min si propice, sinon pas d'ordre",
     lambda e: ECHEANCE if e.contexte.get("propice_15") == 1 else None),
    ("P15/5", "15 min si propice, sinon 5 min", _propice_ou_5),
)


def _compte_horizon(liste: Sequence[Exemple], coupure: int,
                    horizon: Callable[[Exemple], int | None]) -> list[int]:
    c = [0, 0, 0, 0]
    for e in liste:
        h = horizon(e)
        if h is None:
            continue
        issue = _issue_a(e, h)
        if issue is None:
            continue
        k = 0 if e.ts_sec < coupure else 2
        c[k] += 1
        c[k + 1] += int(issue)
    return c


def _serie_max(liste: Sequence[Exemple]) -> int:
    meilleure = courante = 0
    for e in liste:
        courante = 0 if e.gagne else courante + 1
        meilleure = max(meilleure, courante)
    return meilleure


def _semaines(liste: Sequence[Exemple], debut: int) -> list[list[int]]:
    semaines: dict[int, list[int]] = {}
    for e in liste:
        s = semaines.setdefault((e.ts_sec - debut) // SEMAINE, [0, 0])
        s[0] += 1
        s[1] += int(e.gagne)
    return [semaines.get(k, [0, 0]) for k in range(max(semaines) + 1)] \
        if semaines else []


def _mediane(valeurs: Sequence[float]) -> float | None:
    return round(statistics.median(valeurs), 2) if valeurs else None


def potentiel(exemples: Sequence[Exemple],
              en_direct: Callable[[Exemple], bool], fenetre: int = 1,
              jours_collectes: Callable[[int, int], float] | None = None
              ) -> dict:
    """Les cinq blocs de l'en-tête, en JSON."""
    base = [e for e in sorted(confirmes(exemples, fenetre),
                              key=lambda e: e.ts_sec) if en_direct(e)]
    if len(base) < 2 * N_MIN_VALIDATION:
        return {"note": f"Seulement {len(base)} entrées confirmées "
                        f"rejouées : il en faut {2 * N_MIN_VALIDATION}."}
    coupure = base[int(len(base) * PART_ETALONNAGE)].ts_sec
    debut, fin = base[0].ts_sec, base[-1].ts_sec
    jours = max(1.0, (jours_collectes and jours_collectes(debut - 60, fin))
                or (fin - debut) / 86400)
    ref = _compte(base, coupure)
    sortie: dict = {"n": len(base), "coupure": coupure, "jours": round(jours, 1),
                    "reference": ref}

    # 1. L'expiration.
    lignes = []
    for ident, libelle, horizon in EXPIRATIONS:
        c = _compte_horizon(base, coupure, horizon)
        lignes.append({"id": ident, "libelle": libelle, "compte": c,
                       "verdict": (None if ident == "15"
                                   else _juger(c, ref))})
    repartition: dict[str, int] = {}
    raisons: dict[str, int] = {}
    for e in base:
        h = e.contexte.get("expiration_dynamique")
        if h is not None:
            cle = str(int(h) // 60)
            repartition[cle] = repartition.get(cle, 0) + 1
        masque = int(e.contexte.get("raisons_expiration", 0) or 0)
        for bit, nom in RAISONS.items():
            if masque & bit:
                raisons[nom] = raisons.get(nom, 0) + 1
    sortie["expirations"] = lignes
    sortie["repartition"] = dict(sorted(repartition.items(),
                                        key=lambda kv: int(kv[0])))
    sortie["raisons"] = raisons

    # 2. Le potentiel de zone, horizon par horizon.
    categories = []
    for k, nom in enumerate(CATEGORIES):
        sous = [e for e in base
                if int(e.contexte.get("categorie_potentiel", 4)) == k]
        if not sous:
            continue
        horizons = {}
        for m in HORIZONS_MIN:
            issues = [_issue_a(e, m * 60) for e in sous]
            issues = [x for x in issues if x is not None]
            horizons[str(m)] = [len(issues), sum(issues)]
        categories.append({
            "categorie": nom, "compte": _compte(sous, coupure),
            "horizons": horizons,
            "mfe": _mediane([e.contexte["mfe_15"] for e in sous
                             if "mfe_15" in e.contexte]),
            "mae": _mediane([e.contexte["mae_15"] for e in sous
                             if "mae_15" in e.contexte]),
            "t_mfe": _mediane([e.contexte["t_mfe"] for e in sous
                               if "t_mfe" in e.contexte]),
            "verdict": _juger(_compte(sous, coupure), ref)})
    sortie["categories"] = categories

    # 3. Les filtres d'entrée tardive, sur les mêmes entrées.
    sortie["filtres"] = []
    for ident, libelle, garde in FILTRES:
        sous = [e for e in base if garde(e)]
        c = _compte(sous, coupure)
        sortie["filtres"].append({
            "id": ident, "libelle": libelle, "compte": c,
            "par_jour": round(len(sous) / jours, 2),
            "serie_max": _serie_max(sous),
            "semaines": _semaines(sous, debut),
            "verdict": None if ident == "A" else _juger(c, ref)})

    # 4. Les indicateurs, achats et ventes à part.
    sortie["indicateurs"] = []
    for ident, libelle, garde in INDICATEURS:
        sous = [e for e in base if garde(e)]
        achats = [e for e in sous if e.contexte.get("sens_call", 1.0)]
        ventes = [e for e in sous if not e.contexte.get("sens_call", 1.0)]
        c = _compte(sous, coupure)
        sortie["indicateurs"].append({
            "id": ident, "libelle": libelle, "compte": c,
            "achats": [len(achats), sum(e.gagne for e in achats)],
            "ventes": [len(ventes), sum(e.gagne for e in ventes)],
            "par_jour": round(len(sous) / jours, 2),
            "verdict": None if ident == "A" else _juger(c, ref)})
    stoch = [e.contexte["stoch_k_signal"] for e in base
             if "stoch_k_signal" in e.contexte]
    motifs: dict[str, int] = {}
    for e in base:
        code = int(e.contexte.get("motif_signal", 0) or 0)
        motifs[MOTIFS[code]] = motifs.get(MOTIFS[code], 0) + 1
    sortie["descriptif"] = {
        "stoch_mesures": len(stoch),
        "stoch_sous_10": sum(1 for k in stoch if k <= 10),
        "stoch_a_100": sum(1 for k in stoch if k >= 100),
        "tendance": {str(t): sum(1 for e in base
                                 if e.contexte.get("tendance_ema") == t)
                     for t in (-1, 0, 1)},
        "motifs_signal": motifs}

    # 5. L'entrée au repli.
    servies = [e for e in base if e.contexte.get("attente_repli", 0) >= 1
               and "gagne_repli" in e.contexte]
    sortie["repli"] = {
        "proposees": len(base), "servies": len(servies),
        "confirmee_memes": [len(servies), sum(e.gagne for e in servies)],
        "repli": [len(servies),
                  sum(int(e.contexte["gagne_repli"]) for e in servies)],
        "repli_ou_rien": _compte([_au_repli(e) for e in servies], coupure),
    }
    sortie["repli"]["verdict"] = _juger(sortie["repli"]["repli_ou_rien"],
                                        ref)
    return sortie


def _au_repli(e: Exemple) -> Exemple:
    """L'entrée au repli, jugée sur SON issue."""
    return replace(e, gagne=bool(e.contexte["gagne_repli"]))


def hypotheses(resultat: Mapping | None) -> list[tuple[str, str, str]]:
    """(id, libellé, verdict) pour le journal de /protocole."""
    if not resultat or "note" in resultat:
        return []
    sortie = []
    for bloc, prefixe in (("expirations", "EXP "), ("filtres", "FILTRE "),
                          ("indicateurs", "IND ")):
        for x in resultat.get(bloc, ()):
            if x.get("verdict"):
                sortie.append((prefixe + x["id"], x["libelle"],
                               x["verdict"]))
    for x in resultat.get("categories", ()):
        sortie.append((f"ZONE {x['categorie']}",
                       f"potentiel de zone {x['categorie']}", x["verdict"]))
    if resultat.get("repli", {}).get("verdict"):
        sortie.append(("REPLI", "entrée au repli, sinon pas d'ordre",
                       resultat["repli"]["verdict"]))
    return sortie


# --------------------------------------------------------------------------- #
# Texte
# --------------------------------------------------------------------------- #

def _pct(n: int, g: int) -> str:
    return f"{g / n:.0%}" if n else "—"


def _ligne(c: Sequence[int], jours: float | None = None) -> str:
    n, g = c[0] + c[2], c[1] + c[3]
    ev = esperance(g / n) if n else None
    debit = f" · {n / jours:.1f}/j" if jours and n else ""
    return (f"anciens {_pct(c[0], c[1])} ({c[0]}) · récents "
            f"<b>{_pct(c[2], c[3])}</b> ({c[2]}) · espérance "
            f"{'—' if ev is None else f'{ev:+.2f}'}/ordre{debit}")


def texte(resultat: Mapping | None) -> str:
    """`/potentiel`."""
    import time
    if not resultat:
        return ("🎯 Potentiel pas encore calculé : il accompagne le rejeu "
                "(quelques minutes après un redémarrage).")
    if "note" in resultat:
        return f"🎯 {resultat['note']}"
    r = resultat
    coupe = time.strftime("%d/%m", time.gmtime(r["coupure"]))
    jours = r["jours"]
    lignes = [
        f"🎯 <b>Potentiel de zone et expiration</b> — {r['n']} entrées "
        f"confirmées (référence 8, règle de l'heure), décrites à l'entrée ; "
        f"étalonnage jusqu'au {coupe}, jugement ensuite ; débits par jour "
        f"de collecte. Payout supposé 92 % à tous les horizons.", ""]

    lignes.append("<b>1. Expiration</b> (mêmes entrées, seule la sortie "
                  "change)")
    for x in r["expirations"]:
        verdict = f" → {x['verdict']}" if x.get("verdict") else ""
        lignes.append(f"  {x['libelle']} : {_ligne(x['compte'])}{verdict}")
    rep = " · ".join(f"{m} min : {n}" for m, n in r["repartition"].items())
    if rep:
        lignes.append(f"  expiration dynamique choisie : {rep}")
    if r["raisons"]:
        lignes.append("  raccourcie par : " + " · ".join(
            f"{k} {v}" for k, v in sorted(r["raisons"].items())))
    lignes.append("")

    lignes.append("<b>2. Potentiel de zone</b> (taux par horizon, "
                  "1 → 15 min ; meilleur et pire point des 15 min en "
                  "amplitudes)")
    for x in r["categories"]:
        horizons = " ".join(
            f"{m}′{_pct(*x['horizons'][str(m)])}" for m in HORIZONS_MIN
            if str(m) in x["horizons"])
        extra = (f" · meilleur point {x['mfe']} à {x['t_mfe']} min, pire "
                 f"{x['mae']}" if x.get("mfe") is not None else "")
        lignes.append(f"  {x['categorie']} : {_ligne(x['compte'])}")
        lignes.append(f"    {horizons}{extra}")
    lignes.append("")

    lignes.append("<b>3. Entrée tardive — filtres B, C, D</b>")
    for x in r["filtres"]:
        semaines = " ".join(_pct(n, g) for n, g in x["semaines"])
        verdict = f" → {x['verdict']}" if x.get("verdict") else ""
        lignes.append(f"  {x['id']} {x['libelle']} : "
                      f"{_ligne(x['compte'], jours)} · pertes d'affilée max "
                      f"{x['serie_max']} · semaines {semaines}{verdict}")
    lignes.append("")

    lignes.append("<b>4. EMA 20/50, Stochastic (14, 3, 3) 10/100, "
                  "chandeliers</b>")
    for x in r["indicateurs"]:
        verdict = f" → {x['verdict']}" if x.get("verdict") else ""
        lignes.append(
            f"  {x['id']} {x['libelle']} : {_ligne(x['compte'], jours)} · "
            f"achats {_pct(*x['achats'])} ({x['achats'][0]}) · ventes "
            f"{_pct(*x['ventes'])} ({x['ventes'][0]}){verdict}")
    d = r["descriptif"]
    lignes.append(
        f"  Stochastic au signal : {d['stoch_sous_10']} sous 10 et "
        f"{d['stoch_a_100']} à 100 sur {d['stoch_mesures']} mesurés · "
        f"tendance EMA contre / neutre / avec le trade : "
        f"{d['tendance']['-1']} / {d['tendance']['0']} / "
        f"{d['tendance']['1']} · figures au signal : " + ", ".join(
            f"{k} {v}" for k, v in sorted(d["motifs_signal"].items())))
    lignes.append("")

    rp = r["repli"]
    lignes.append("<b>5. Entrée au repli</b> (après la confirmation, "
                  "attendre 3 min au plus le retour au prix du signal)")
    lignes.append(
        f"  servie {rp['servies']} fois sur {rp['proposees']} · sur ces "
        f"signaux : entrée confirmée {_pct(*rp['confirmee_memes'])}, "
        f"entrée au repli {_pct(*rp['repli'])}")
    lignes.append(f"  repli, sinon pas d'ordre : "
                  f"{_ligne(rp['repli_ou_rien'], jours)} → {rp['verdict']}")
    lignes.append(
        "\nDes mesures, pas des règles : un ✅ désigne une piste à "
        "pré-inscrire et à juger à l'aveugle, pas un réglage à appliquer.")
    return "\n".join(lignes)
