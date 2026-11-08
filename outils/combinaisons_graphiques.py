#!/usr/bin/env python
"""Les combinaisons de bougies au contact, comptées sur un lot de
/graphiques (bougies avant l'entrée + issues). Hors ligne, sans base :
    python3 -I outils/combinaisons_graphiques.py graphiques_bougies.csv \
        graphiques_signaux.csv [graphiques_issues.csv]

Lot 1 : l'issue est dans graphiques_signaux.csv. À partir du lot 2, elle est
dans graphiques_issues.csv, le troisième argument.
"""
import csv, sys
from collections import defaultdict
bougies_p, signaux_p = sys.argv[1:3]
issues_p = sys.argv[3] if len(sys.argv) > 3 else None
par = defaultdict(list)
for r in csv.DictReader(open(bougies_p, encoding="utf-8"), delimiter=";"):
    if r.get("apres_entree", "0") == "0":
        par[int(r["graphique"])].append(r)
sig = {int(r["graphique"]): r for r in csv.DictReader(open(signaux_p, encoding="utf-8"), delimiter=";")}
if issues_p:
    for r in csv.DictReader(open(issues_p, encoding="utf-8"), delimiter=";"):
        sig[int(r["graphique"])]["issue"] = r["issue"]
def bougie(r, sens, corps_moyen):
    o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
    e = h - l or 1e-12; corps = abs(c - o)
    d = (c - o) * sens
    meche_zone = (min(o, c) - l) if sens > 0 else (h - max(o, c))   # côté zone
    meche_loin = (h - max(o, c)) if sens > 0 else (min(o, c) - l)
    if corps <= 0.1 * e: genre = "doji"
    else: genre = "pour" if d > 0 else "contre"
    taille = corps / corps_moyen
    return {"genre": genre, "taille": taille, "corps": corps, "mz": meche_zone / e, "ml": meche_loin / e}
fiches = []
for g, rows in par.items():
    rows.sort(key=lambda r: int(r["ts"]))
    s = sig[g]; sens = 1 if s["sens"] == "call" else -1
    corps_moyen = sum(abs(float(r["close"]) - float(r["open"])) for r in rows[-50:]) / 50 or 1e-12
    b = [bougie(r, sens, corps_moyen) for r in rows[-4:]]
    fiches.append({"g": g, "gain": s["issue"] == "gain", "b": b})
def t(l):
    return f"{sum(f['gain'] for f in l):2d}/{len(l):2d} = {100*sum(f['gain'] for f in l)/len(l):3.0f} %" if l else "—"
def taille(x):
    return "grande" if x["taille"] >= 1.5 else "petite" if x["taille"] < 0.6 else "moyenne"
print("La bougie du signal (dernière avant l'entrée) :")
for k in ("pour", "contre", "doji"):
    for tl in ("grande", "moyenne", "petite"):
        l = [f for f in fiches if f["b"][-1]["genre"] == k and (k == "doji" or taille(f["b"][-1]) == tl)]
        if k == "doji" and tl != "grande": continue
        print(f"  {k:6s} {'' if k == 'doji' else tl:8s} {t(l)}")
print("Combinaison des deux dernières (avant-dernière → dernière) :")
combos = defaultdict(list)
for f in fiches:
    a, b = f["b"][-2], f["b"][-1]
    combos[f"{a['genre']}/{taille(a)} → {b['genre']}/{taille(b)}" if True else ""].append(f)
for k, l in sorted(combos.items(), key=lambda kv: -len(kv[1])):
    if len(l) >= 3: print(f"  {k:40s} {t(l)}")
print("Figures précises :")
eng = [f for f in fiches if f["b"][-2]["genre"] == "contre" and f["b"][-1]["genre"] == "pour" and f["b"][-1]["corps"] > f["b"][-2]["corps"]]
print(f"  englobante (contre puis pour, corps plus grand)  {t(eng)}")
pet = [f for f in fiches if f["b"][-2]["genre"] == "contre" and taille(f["b"][-2]) == "grande" and f["b"][-1]["genre"] == "pour" and taille(f["b"][-1]) == "petite"]
print(f"  grande contre puis petite pour                  {t(pet)}")
pin = [f for f in fiches if f["b"][-1]["mz"] >= 0.5]
print(f"  longue mèche côté zone (≥ 50 % de la bougie)    {t(pin)}")
mloin = [f for f in fiches if f["b"][-1]["ml"] >= 0.5]
print(f"  longue mèche côté opposé (≥ 50 %)               {t(mloin)}")
trois_contre = [f for f in fiches if all(x["genre"] == "contre" for x in f["b"][-4:-1]) and f["b"][-1]["genre"] == "pour"]
print(f"  3 contre puis 1 pour                            {t(trois_contre)}")
deux_grandes_contre = [f for f in fiches if sum(1 for x in f["b"][-4:-1] if x["genre"] == "contre" and taille(x) == "grande") >= 2]
print(f"  ≥ 2 grandes contre dans les 3 d'avant           {t(deux_grandes_contre)}")
doji_avant = [f for f in fiches if f["b"][-2]["genre"] == "doji" and f["b"][-1]["genre"] == "pour"]
print(f"  doji puis pour                                  {t(doji_avant)}")
print("Ensemble :", t(fiches))
