#!/usr/bin/env python
"""
Audit INDÉPENDANT des résultats du gel 2 (02/11) : code neuf, aucune
importation du projet, à partir du seul selection_signaux.csv rendu par
/micro_selection et du document du gel. Résultats : docs/etape3_audit_gel2.md.

    python3 -I outils/audit_gel2.py selection_signaux.csv \
        docs/etape3_parametres_gel2.json

Rien n'est lu en base, rien n'est écrit.
"""

from __future__ import annotations

import csv
import itertools
import json
import math
import random
import sys
from collections import Counter, defaultdict
from statistics import NormalDist

FIN_EXPLO = 1792540800       # 2026-10-21T00:00:00Z
FIN_VALID = 1793145600       # 2026-10-28T00:00:00Z
OUVERTURE_CONF = 1794096000  # 2026-11-03T00:00:00Z
PAYOUT_NOMINAL = 0.92
POINT_MORT = 1 / (1 + PAYOUT_NOMINAL)
USD = lambda p: "USD" in p          # noqa: E731
GRAINE, TIRAGES = 7, 2000
N = NormalDist()


def f(x):
    return None if x in ("", None) else float(x)


def charger(chemin):
    with open(chemin, encoding="utf-8") as fichier:
        return list(csv.DictReader(fichier, delimiter=";"))


def titre(t):
    print(f"\n## {t}\n")


# --------------------------------------------------------------------------- #
def entonnoir(lignes):
    titre("A. Entonnoir et cohérence")
    cles = Counter((r["paire"], r["decision"]) for r in lignes)
    doublons = sum(1 for v in cles.values() if v > 1)
    print(f"- lignes : {len(lignes)} ; doublons (paire, décision) : {doublons}")
    for per in ("exploration", "validation"):
        jeu = [r for r in lignes if r["periode"] == per]
        n2 = [r for r in jeu if r["E8_niveau"] == "2"]
        valid = [r for r in n2 if r["classe"] == "VALID"]
        princ = [r for r in jeu if r["principale"] == "True"]
        decis = [r for r in princ if r["y"] in ("0", "1")]
        print(f"- {per} : {len(jeu)} signaux → niveau 2 : {len(n2)} → "
              f"VALID : {len(valid)} (colonne principale : {len(princ)}) → "
              f"issue décisive : {len(decis)} ; issues de la principale : "
              f"{dict(Counter(r['issue'] for r in princ))}")
    autres = Counter(r["periode"] for r in lignes
                     if r["periode"] not in ("exploration", "validation"))
    print(f"- autres périodes présentes : {dict(autres) or 'aucune'}")
    d = [int(r["decision"]) for r in lignes]
    print(f"- décisions : du {min(d)} au {max(d)} ; toutes multiples de 60 : "
          f"{all(x % 60 == 0 for x in d)}")
    mal = [r for r in lignes if (r["periode"] == "exploration"
                                 and int(r["decision"]) >= FIN_EXPLO)
           or (r["periode"] == "validation"
               and not FIN_EXPLO <= int(r["decision"]) < FIN_VALID)]
    print(f"- signaux mal classés par période : {len(mal)} ; décisions ≥ "
          f"28/10 : {sum(1 for x in d if x >= FIN_VALID)}")
    sortie = max(d) + 900
    print(f"- dernière bougie de sortie : {sortie} (< ouverture de la "
          f"confirmation {OUVERTURE_CONF} : {sortie < OUVERTURE_CONF})")
    incoherents = [r for r in lignes if r["principale"] == "True"
                   and not (r["E8_niveau"] == "2" and r["classe"] == "VALID")]
    print(f"- « principale » sans niveau 2 et VALID : {len(incoherents)}")
    y_faux = [r for r in lignes if r["y"] in ("0", "1") and
              ((r["issue"] == "gain") != (r["y"] == "1"))]
    print(f"- y incohérent avec l'issue : {len(y_faux)}")


def par_paire(gardees):
    titre("B. Signaux de la population principale avec issue, par paire")
    print("| paire | USD | exploration (n, gains) | validation (n, gains) |")
    print("|---|---|---|---|")
    for p in sorted({r["paire"] for r in gardees}):
        cel = []
        for per in ("exploration", "validation"):
            j = [r for r in gardees if r["paire"] == p and r["periode"] == per]
            g = sum(int(r["y"]) for r in j)
            cel.append(f"{len(j)}, {g} ({100 * g / len(j):.0f} %)" if j
                       else "0")
        print(f"| {p} | {'oui' if USD(p) else ''} | {cel[0]} | {cel[1]} |")
    for per in ("exploration", "validation"):
        print(f"- total {per} : "
              f"{sum(1 for r in gardees if r['periode'] == per)}")


# --------------------------------------------------------------------------- #
# Recalcul indépendant du gel 2
# --------------------------------------------------------------------------- #

REFERENCE = ["sens_call", "touches", "entrees_deja_offertes",
             "distance_niveau", "elan_15m", "volatilite_relative",
             "mouvement_heure", "depuis_contact", "alternances_10", "payout"]


def mediane(v):
    v = sorted(v)
    k = len(v) // 2
    return None if not v else (v[k] if len(v) % 2 else (v[k - 1] + v[k]) / 2)


def preparation(explo, numeriques):
    prep = {}
    for nom in numeriques:
        vals = [f(r[nom]) for r in explo]
        med = mediane([x for x in vals if x is not None])
        med = 0.0 if med is None else med
        comp = [med if x is None else x for x in vals]
        moy = sum(comp) / len(comp)
        sd = math.sqrt(sum((x - moy) ** 2 for x in comp) / len(comp)) or 1.0
        prep[nom] = (med, moy, sd)
    cats = {c: sorted({r[c] for r in explo})[1:] for c in ("paire", "creneau")}
    return prep, cats


def vecteur(r, prep, cats, numeriques):
    x = []
    for nom in numeriques:
        med, moy, sd = prep[nom]
        v = f(r[nom])
        x += [((med if v is None else v) - moy) / sd, float(v is None)]
    for c, mods in cats.items():
        x += [float(r[c] == m) for m in mods]
    return x


def logistique_l2(X, y, lam=1.0):
    """Newton, interception non pénalisée ; résolution par Gauss-Jordan."""
    p = len(X[0]) + 1
    beta = [0.0] * p
    for _ in range(100):
        g = [0.0] * p
        H = [[0.0] * p for _ in range(p)]
        for xi, yi in zip(X, y):
            z = [1.0] + xi
            u = sum(b * v for b, v in zip(beta, z))
            mu = 1 / (1 + math.exp(-u)) if u >= 0 else math.exp(u) / (1 + math.exp(u))
            w = mu * (1 - mu)
            for j in range(p):
                g[j] += (yi - mu) * z[j]
                for k in range(p):
                    H[j][k] += w * z[j] * z[k]
        for j in range(1, p):
            g[j] -= lam * beta[j]
            H[j][j] += lam
        # Gauss-Jordan sur [H | g]
        A = [H[i][:] + [g[i]] for i in range(p)]
        for c in range(p):
            piv = max(range(c, p), key=lambda i: abs(A[i][c]))
            A[c], A[piv] = A[piv], A[c]
            d = A[c][c]
            A[c] = [v / d for v in A[c]]
            for i in range(p):
                if i != c and A[i][c]:
                    fac = A[i][c]
                    A[i] = [a - fac * b for a, b in zip(A[i], A[c])]
        pas = [A[i][p] for i in range(p)]
        beta = [b + d for b, d in zip(beta, pas)]
        if max(abs(d) for d in pas) < 1e-10:
            break
    return beta


def recalcul_gel(gardees, princ_explo, doc):
    titre("C. Recalcul indépendant des paramètres du gel 2")
    for h, var in (("H1", "A7_penetration@300"), ("H2", "D1_traversees@300")):
        v = sorted(f(r[var]) for r in princ_explo if f(r[var]) is not None)
        bornes = (v[len(v) // 3], v[2 * len(v) // 3])
        attendu = doc["hypotheses"][h]["terciles"]["distincts|k0"]
        print(f"- terciles {h} recalculés {bornes[0]:.6f} / {bornes[1]:.6f} ; "
              f"gel {attendu[0]:.6f} / {attendu[1]:.6f} ; écart max "
              f"{max(abs(a - b) for a, b in zip(bornes, attendu)):.2e}")
    numeriques = REFERENCE
    prep, cats = preparation(princ_explo, numeriques)
    gel = doc["preparation"]
    ecart = max(max(abs(prep[n][0] - gel["medianes"][n]),
                    abs(prep[n][1] - gel["moyennes"][n]),
                    abs(prep[n][2] - gel["ecarts"][n])) for n in numeriques)
    print(f"- préparation (médianes, moyennes, écarts des 10 variables de "
          f"référence) : écart max au gel {ecart:.2e} (le CSV arrondit à "
          f"6 décimales)")
    jeu = [r for r in gardees]
    X = [vecteur(r, prep, cats, numeriques) for r in jeu]
    y = [int(r["y"]) for r in jeu]
    beta = logistique_l2(X, y)
    m0 = doc["modeles"]["M0|exclues"]
    colonnes = ["interception"] + [c for n in numeriques
                                   for c in (n, f"{n}?manque")] + \
        [f"{c}={m}" for c, mods in cats.items() for m in mods]
    assert colonnes == m0["colonnes"], "colonnes différentes du gel"
    diff = max(abs(a - b) for a, b in zip(beta, m0["coefficients"]))
    print(f"- M0 (référence) réajusté sur {len(jeu)} signaux (gel : "
          f"{m0['n']}) : écart max des {len(beta)} coefficients "
          f"{diff:.2e}")


# --------------------------------------------------------------------------- #
# Taux, rendement et incertitude
# --------------------------------------------------------------------------- #

def bloc_pj(r):
    return (r["paire"], int(r["decision"]) // 86400)


def bloc_jour(r):
    return int(r["decision"]) // 86400


def bloc_paire(r):
    return r["paire"]


def resample(jeu, cle, alea):
    blocs = defaultdict(list)
    for r in jeu:
        blocs[cle(r)].append(r)
    k = sorted(blocs, key=repr)
    out = []
    for _ in k:
        out += blocs[k[alea.randrange(len(k))]]
    return out


def taux(j):
    return sum(int(r["y"]) for r in j) / len(j) if j else float("nan")


def ic(stat, groupes, cle):
    """IC 95 % percentile ; chaque groupe est rééchantillonné à part."""
    alea = random.Random(GRAINE)
    tirs = sorted(stat(*[resample(g, cle, alea) for g in groupes])
                  for _ in range(TIRAGES))
    return tirs[int(0.025 * TIRAGES)], tirs[int(0.975 * TIRAGES) - 1], tirs


def rendement(j):
    """Rendement moyen par mise au payout OBSERVÉ de chaque signal."""
    vals = [(float(r["payout"]) / 100 if int(r["y"]) else -1.0)
            for r in j if r["payout"] not in ("",)]
    return sum(vals) / len(vals) if vals else float("nan")


def incertitude(gardees):
    titre("D. USD / non-USD : taux, rendement aux payouts observés, IC selon "
          "trois regroupements")
    pays = [float(r["payout"]) for r in gardees if r["payout"]]
    manquants = sum(1 for r in gardees if not r["payout"])
    print(f"- payouts observés : min {min(pays):.0f} %, médiane "
          f"{mediane(pays):.0f} %, max {max(pays):.0f} % ; manquants : "
          f"{manquants}")
    for per in ("exploration", "validation", "les deux"):
        j = [r for r in gardees if per == "les deux" or r["periode"] == per]
        u = [r for r in j if USD(r["paire"])]
        a = [r for r in j if not USD(r["paire"])]
        print(f"\n**{per}** — USD : {taux(u):.1%} (n={len(u)}), rendement "
              f"observé {rendement(u):+.1%} ; autres : {taux(a):.1%} "
              f"(n={len(a)}), rendement {rendement(a):+.1%} ; écart "
              f"{taux(u) - taux(a):+.1%}")
        for nom, cle in (("paire × jour", bloc_pj), ("jour", bloc_jour),
                         ("paire (grappes)", bloc_paire)):
            lo, hi, _ = ic(lambda x: taux(x), [u], cle)
            lo_d, hi_d, tirs = ic(lambda x, z: taux(x) - taux(z), [u, a], cle)
            lo_r, hi_r, tirs_r = ic(rendement, [u], cle)
            p_pm = (1 + sum(1 for t in ic(lambda x: taux(x), [u], cle)[2]
                            if t <= POINT_MORT)) / (TIRAGES + 1)
            print(f"  - blocs {nom} : taux USD IC {lo:.1%}–{hi:.1%} "
                  f"(P ≤ point mort {p_pm:.3f}) ; rendement USD IC "
                  f"{lo_r:+.1%}–{hi_r:+.1%} ; écart USD − autres IC "
                  f"{lo_d:+.1%}–{hi_d:+.1%}")


def permutation(gardees):
    titre("E. Test de permutation : les 7 paires USD face à TOUS les groupes "
          "de 7 paires parmi 20")
    paires = sorted({r["paire"] for r in gardees})
    usd = tuple(p for p in paires if USD(p))
    for per in ("exploration", "validation", "les deux"):
        j = [r for r in gardees if per == "les deux" or r["periode"] == per]
        n = {p: 0 for p in paires}
        g = {p: 0 for p in paires}
        for r in j:
            n[r["paire"]] += 1
            g[r["paire"]] += int(r["y"])
        N_, G_ = sum(n.values()), sum(g.values())

        def ecart(groupe):
            ng = sum(n[p] for p in groupe)
            gg = sum(g[p] for p in groupe)
            return gg / ng - (G_ - gg) / (N_ - ng)
        obs = ecart(usd)
        ecarts = [ecart(c) for c in itertools.combinations(paires, len(usd))]
        rang = sum(1 for e in ecarts if e >= obs - 1e-12)
        print(f"- {per} : écart USD − autres {obs:+.1%} ; groupes de 7 paires "
              f"au moins aussi favorables : {rang} sur {len(ecarts)} "
              f"(p = {rang / len(ecarts):.3f})")


def effet_de_plan(gardees):
    titre("F. Dépendance intra-bloc (paires USD, blocs paire × jour)")
    u = [r for r in gardees if USD(r["paire"])]
    blocs = defaultdict(list)
    for r in u:
        blocs[bloc_pj(r)].append(int(r["y"]))
    groupes = list(blocs.values())
    k, n = len(groupes), sum(len(b) for b in groupes)
    moy = sum(sum(b) for b in groupes) / n
    ssb = sum(len(b) * (sum(b) / len(b) - moy) ** 2 for b in groupes)
    ssw = sum(sum((y - sum(b) / len(b)) ** 2 for y in b) for b in groupes)
    msb, msw = ssb / (k - 1), ssw / (n - k)
    n0 = (n - sum(len(b) ** 2 for b in groupes) / n) / (k - 1)
    rho = max(0.0, (msb - msw) / (msb + (n0 - 1) * msw))
    m = sum(len(b) ** 2 for b in groupes) / n
    deff = 1 + (m - 1) * rho
    jours = len({bloc_jour(r) for r in u})
    print(f"- {n} signaux en {k} blocs ; taille pondérée {m:.2f} ; "
          f"corrélation intra-bloc {rho:.3f} ; effet de plan {deff:.2f}")
    print(f"- rythme : {n / jours:.1f} signaux USD de la population principale "
          f"par jour ({jours} jours)")
    return deff, n / jours


def puissance(deff, par_jour):
    titre("G. Calcul de puissance pour la pré-inscription (H0 : taux ≤ point "
          "mort 52,08 %)")
    za, zb = N.inv_cdf(0.95), N.inv_cdf(0.80)
    for deff_h in sorted({1.0, round(deff, 2)}):
        for p1 in (0.540, 0.545, 0.550, 0.555):
            d = p1 - POINT_MORT
            n = math.ceil((za + zb) ** 2 * p1 * (1 - p1) * deff_h / d ** 2)
            print(f"- effet de plan {deff_h:.2f}, taux réel {p1:.1%} : "
                  f"{n} signaux (≈ {n / par_jour:.0f} jours)")


def plafond(gardees):
    titre("H. Population jouable : signaux au payout maximum (≥ 92 %)")
    print("La course n'entre qu'au payout maximum : ce filtre est antérieur "
          "à toute analyse.")
    for per in ("exploration", "validation", "les deux"):
        j = [r for r in gardees if float(r["payout"]) >= 92
             and (per == "les deux" or r["periode"] == per)]
        u = [r for r in j if USD(r["paire"])]
        a = [r for r in j if not USD(r["paire"])]
        lo, hi, _ = ic(rendement, [u], bloc_pj)
        print(f"- {per} : toutes {taux(j):.1%} (n={len(j)}, rendement "
              f"{rendement(j):+.1%}) ; USD {taux(u):.1%} (n={len(u)}, "
              f"rendement {rendement(u):+.1%}, IC paire × jour "
              f"{lo:+.1%}–{hi:+.1%}) ; autres {taux(a):.1%} (n={len(a)}, "
              f"rendement {rendement(a):+.1%})")
    reste = [r for r in gardees if float(r["payout"]) < 92]
    print(f"- signaux sous le plafond : {taux(reste):.1%} (n={len(reste)}, "
          f"rendement observé {rendement(reste):+.1%})")
    jouables = [r for r in gardees if float(r["payout"]) >= 92]
    permutation(jouables)


def main(chemin_csv, chemin_doc):
    lignes = charger(chemin_csv)
    doc = json.load(open(chemin_doc, encoding="utf-8"))
    gardees = [r for r in lignes if r["principale"] == "True"
               and r["y"] in ("0", "1")]
    princ_explo = [r for r in lignes if r["principale"] == "True"
                   and r["periode"] == "exploration"]
    print("# Audit indépendant des résultats du gel 2")
    entonnoir(lignes)
    par_paire(gardees)
    recalcul_gel(gardees, princ_explo, doc)
    incertitude(gardees)
    permutation(gardees)
    deff, par_jour = effet_de_plan(gardees)
    puissance(deff, par_jour)
    plafond(gardees)
    print("\n# Mêmes calculs (F, G) sur la population jouable : paires USD "
          "au payout maximum")
    jouables = [r for r in gardees if float(r["payout"]) >= 92]
    deff_j, par_jour_j = effet_de_plan(jouables)
    puissance(deff_j, par_jour_j)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
