"""
La stabilité de la référence dans le temps (EXP-021), et le premier pas
(EXP-022).

--- D'où elles viennent ----------------------------------------------------

Demandées le 2026-10-12 : « est-ce que ZoneH1 conserve son avantage quand
le temps passe, que le marché change et qu'on la teste sur des signaux qui
n'ont pas servi à la concevoir ? » — ZoneH1 était passée de 64,0 % sur 214
signaux anciens à 51,5 % sur 33 récents. Et : « les premiers pas semblent
plus fragiles que les suivants » (5/9 au pas 1, 3/4 au pas 2 sur les ordres
réels).

--- EXP-021 : la stabilité --------------------------------------------------

1. Des FENÊTRES de `FENETRE_JOURS` jours, fixées avant de regarder les
   résultats, sans mélanger passé et futur : effectif, taux, intervalle de
   Wilson, espérance à mise fixe, plus longue série de pertes, signaux par
   jour, paires présentes.
2. Un intervalle par RÉÉCHANTILLONNAGE PAR BLOCS (une paire × un jour) :
   deux signaux de la même paire le même jour ne sont pas indépendants, et
   un intervalle qui les traite comme tels est trop optimiste.
3. Chaque paire dans une catégorie DESCRIPTIVE, jamais « bonne » ou
   « mauvaise » : données insuffisantes, à surveiller, stabilité à
   confirmer.
4. Une ALERTE quand la dernière fenêtre devient incompatible (test
   binomial unilatéral à 5 %) avec le taux des fenêtres précédentes. Elle
   demande une ENQUÊTE : aucune règle ne change d'elle-même.

--- EXP-022 : le premier pas ----------------------------------------------

Le pas 2 n'est pas une stratégie indépendante : il n'existe qu'après une
perte au pas 1, sur une AUTRE paire, au moins une échéance plus tard. Son
taux sur les ordres réels porte sur une sélection de situations — le
comparer au pas 1 sur 13 ordres ne dit rien. Ici, le plan est rejoué sur
toute la référence, et chaque entrée reçoit le rang qu'elle aurait eu :
un effet propre au rang se verrait sur des centaines d'entrées.

Et sur les MÊMES signaux de ZoneH1 : l'issue d'une entrée immédiate face à
celle de l'entrée confirmée (comparaison appariée), la part de signaux que
la confirmation fait perdre, et de combien elle dégrade le prix d'entrée.
"""

from __future__ import annotations

import math
import random
from typing import Callable, Mapping, Sequence

from maxprofit.apprentissage.lecons import SEUIL, Exemple, wilson
from maxprofit.live.laboratoire import confirmes

#: Taille des fenêtres, fixée AVANT de regarder les résultats.
FENETRE_JOURS = 7
#: Tirages du rééchantillonnage par blocs.
TIRAGES = 1000
#: En dessous, une paire n'a pas assez de signaux pour être jugée.
N_MIN_PAIRE = 20
#: Signaux minimaux dans une fenêtre pour qu'elle compte dans un verdict.
N_MIN_FENETRE = 5
#: Risque de fausse alerte du test de dégradation.
RISQUE_ALERTE = 0.05
PAYOUT = 0.92


def _esperance(p: float) -> float:
    return p * PAYOUT - (1 - p)


def _serie_max(liste: Sequence[Exemple]) -> int:
    meilleure = courante = 0
    for e in liste:
        courante = 0 if e.gagne else courante + 1
        meilleure = max(meilleure, courante)
    return meilleure


def _cdf_binomiale(k: int, n: int, p: float) -> float:
    """P(X ≤ k) pour X ~ Binomiale(n, p)."""
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i)
               for i in range(0, k + 1))


def intervalle_par_blocs(liste: Sequence[Exemple], tirages: int = TIRAGES,
                         graine: int = 7) -> tuple[float, float] | None:
    """L'intervalle à 95 % du taux, en rééchantillonnant des BLOCS
    (paire, jour) plutôt que des signaux isolés."""
    blocs: dict[tuple[str, int], list[int]] = {}
    for e in liste:
        blocs.setdefault((e.pair, e.ts_sec // 86400), []).append(int(e.gagne))
    valeurs = list(blocs.values())
    if len(valeurs) < 5:
        return None
    hasard = random.Random(graine)
    taux = []
    for _ in range(tirages):
        n = g = 0
        for _ in range(len(valeurs)):
            b = valeurs[hasard.randrange(len(valeurs))]
            n += len(b)
            g += sum(b)
        taux.append(g / n)
    taux.sort()
    return taux[int(0.025 * tirages)], taux[int(0.975 * tirages) - 1]


def _bloc_fenetre(liste: Sequence[Exemple], debut: int, jours: float) -> dict:
    n, g = len(liste), sum(e.gagne for e in liste)
    bas, haut = wilson(g, n, 1.96) if n else (0.0, 1.0)
    return {"debut": debut, "n": n, "g": g, "bas": round(bas, 4),
            "haut": round(haut, 4), "serie_max": _serie_max(liste),
            "par_jour": round(n / jours, 2),
            "paires": len({e.pair for e in liste})}


def _categorie_paire(liste: Sequence[Exemple],
                     par_fenetre: Sequence[tuple[int, int]]) -> str:
    n, g = len(liste), sum(e.gagne for e in liste)
    if n < N_MIN_PAIRE:
        return "données insuffisantes"
    moitie = len(liste) // 2
    avant, apres = liste[:moitie], liste[moitie:]
    t_avant = sum(e.gagne for e in avant) / max(1, len(avant))
    t_apres = sum(e.gagne for e in apres) / max(1, len(apres))
    haut_apres = wilson(sum(e.gagne for e in apres), len(apres), 1.96)[1]
    if t_avant - t_apres >= 0.15 or haut_apres < SEUIL:
        return "à surveiller"
    comptees = [(nf, gf) for nf, gf in par_fenetre if nf >= N_MIN_FENETRE]
    if len(comptees) >= 2 and all(gf / nf >= SEUIL for nf, gf in comptees):
        return "stabilité à confirmer"
    return "à surveiller"


def stabilite(exemples: Sequence[Exemple],
              en_direct: Callable[[Exemple], bool], fenetre: int = 1,
              pas_max: int = 2, echeance_sec: int = 900) -> dict:
    """EXP-021 et EXP-022 sur la référence jouée."""
    base = [e for e in sorted(confirmes(exemples, fenetre),
                              key=lambda e: e.ts_sec) if en_direct(e)]
    nom = "8" if fenetre == 1 else f"8f{fenetre}"
    if len(base) < 2 * N_MIN_FENETRE:
        return {"note": f"Seulement {len(base)} entrées confirmées rejouées "
                        f"({nom}) : trop peu pour juger une stabilité."}
    t0 = base[0].ts_sec
    duree = FENETRE_JOURS * 86400
    fenetres: dict[int, list[Exemple]] = {}
    for e in base:
        fenetres.setdefault((e.ts_sec - t0) // duree, []).append(e)
    derniere = max(fenetres)
    blocs = []
    for k in sorted(fenetres):
        # La dernière fenêtre est en cours : sa durée réelle compte pour le
        # débit, pas sept jours.
        jours = (FENETRE_JOURS if k < derniere else
                 max(1.0, (base[-1].ts_sec - (t0 + k * duree)) / 86400))
        blocs.append(_bloc_fenetre(fenetres[k], t0 + k * duree, jours))

    n, g = len(base), sum(e.gagne for e in base)
    sortie: dict = {
        "nom": nom, "fenetre_jours": FENETRE_JOURS, "fenetres": blocs,
        "global": {"n": n, "g": g, "wilson": list(wilson(g, n, 1.96)),
                   "blocs": intervalle_par_blocs(base),
                   "serie_max": _serie_max(base)},
    }

    # L'alerte : la dernière fenêtre face à toutes les précédentes.
    precedents = [e for k, l in fenetres.items() if k < derniere for e in l]
    dernieres = fenetres[derniere]
    if len(precedents) >= 30 and len(dernieres) >= N_MIN_FENETRE:
        p0 = sum(e.gagne for e in precedents) / len(precedents)
        k = sum(e.gagne for e in dernieres)
        p_valeur = _cdf_binomiale(k, len(dernieres), p0)
        sortie["alerte"] = {"attendu": round(p0, 4), "n": len(dernieres),
                            "g": k, "p": round(p_valeur, 4),
                            "declenchee": p_valeur < RISQUE_ALERTE}

    # Les paires, par catégorie descriptive.
    paires: dict[str, list[Exemple]] = {}
    for e in base:
        paires.setdefault(e.pair, []).append(e)
    sortie["paires"] = {}
    for p, liste in sorted(paires.items()):
        par_fenetre = []
        for k in sorted(fenetres):
            dans = [e for e in fenetres[k] if e.pair == p]
            par_fenetre.append((len(dans), sum(e.gagne for e in dans)))
        sortie["paires"][p] = {
            "n": len(liste), "g": sum(e.gagne for e in liste),
            "categorie": _categorie_paire(liste, par_fenetre)}

    sortie["premier_pas"] = _rangs(base, pas_max, echeance_sec)
    sortie["entree_retardee"] = _entree_retardee(
        [e for e in sorted(exemples, key=lambda e: e.ts_sec)
         if en_direct(e)], fenetre)
    return sortie


def _rangs(base: Sequence[Exemple], pas_max: int, echeance_sec: int) -> dict:
    """Le plan rejoué : chaque entrée prise reçoit son rang (1 = entrée de
    session, 2 = premier pas de martingale…). Mêmes règles qu'en direct :
    une entrée à la fois, le pas suivant sur une autre paire, au moins une
    échéance plus tard."""
    comptes = {r: [0, 0] for r in range(1, pas_max + 1)}
    libre_a, pas, derniere_paire = 0, 0, None
    for e in base:
        if e.ts_sec < libre_a:
            continue
        if pas and e.pair == derniere_paire:
            continue
        rang = pas + 1
        comptes[rang][0] += 1
        comptes[rang][1] += int(e.gagne)
        libre_a = e.ts_sec + echeance_sec
        if e.gagne or rang >= pas_max:
            pas, derniere_paire = 0, None
        else:
            pas, derniere_paire = rang, e.pair
    sortie = {"comptes": {str(r): c for r, c in comptes.items()}}
    (n1, g1), (n2, g2) = comptes[1], comptes.get(2, [0, 0])
    if n1 and n2:
        p = (g1 + g2) / (n1 + n2)
        ecart = (p * (1 - p) * (1 / n1 + 1 / n2)) ** 0.5
        sortie["z"] = round((g2 / n2 - g1 / n1) / ecart, 2) if ecart else 0.0
    return sortie


def _entree_retardee(signaux: Sequence[Exemple], fenetre: int) -> dict:
    """Sur les MÊMES signaux : entrée immédiate ou entrée confirmée."""
    confirmes_ = []
    for e in signaux:
        attente = int(e.contexte.get("attente_confirmation", 0) or 0)
        cle = "gagne_suivant" if fenetre == 1 else "gagne_confirme"
        ok = (e.contexte.get("retournement_suivant") == 1 if fenetre == 1
              else 1 <= attente <= fenetre)
        if ok and cle in e.contexte:
            confirmes_.append((e, bool(e.contexte[cle])))
    ids = {id(e) for e, _ in confirmes_}
    perdus = [e for e in signaux if id(e) not in ids]
    degradation = sorted(
        e.contexte["e_distance_niveau"] - e.contexte["distance_niveau"]
        for e, _ in confirmes_
        if "e_distance_niveau" in e.contexte and "distance_niveau" in e.contexte)
    return {
        "signaux": len(signaux),
        "immediat": [len(signaux), sum(e.gagne for e in signaux)],
        "confirmes": len(confirmes_),
        # Appariés : les signaux confirmés, entrés au signal ou à la
        # confirmation. Seule l'heure d'entrée change.
        "apparies_immediat": sum(e.gagne for e, _ in confirmes_),
        "apparies_confirme": sum(g for _, g in confirmes_),
        "perdus": [len(perdus), sum(e.gagne for e in perdus)],
        "degradation_mediane": (round(degradation[len(degradation) // 2], 2)
                                if degradation else None),
    }


def texte_alerte(resultat: Mapping | None) -> str | None:
    """Le message à envoyer quand l'alerte se déclenche, sinon None."""
    a = (resultat or {}).get("alerte")
    if not a or not a.get("declenchee"):
        return None
    return (f"🔔 <b>EXP-021 — dégradation à examiner</b> ({resultat['nom']})\n"
            f"Dernière fenêtre de {resultat['fenetre_jours']} jours : "
            f"{a['g']}/{a['n']} gagnés ({a['g'] / a['n']:.0%}), alors que les "
            f"fenêtres précédentes gagnaient {a['attendu']:.0%}. Une telle "
            f"baisse n'arrive par hasard que {a['p']:.1%} du temps.\n"
            f"Aucune règle n'est modifiée : c'est une demande d'enquête "
            f"(/stabilite, /audit).")


def texte(resultat: Mapping | None) -> str:
    """`/stabilite`."""
    import time
    if not resultat:
        return ("📈 Stabilité pas encore calculée : elle accompagne le rejeu "
                "(quelques minutes après un redémarrage).")
    if "note" in resultat:
        return f"📈 {resultat['note']}"
    jour = lambda ts: time.strftime("%d/%m", time.gmtime(ts))  # noqa: E731
    gl = resultat["global"]
    lignes = [f"📈 <b>EXP-021 — stabilité de la référence "
              f"{resultat['nom']}</b> (entrées confirmées rejouées, "
              f"fenêtres de {resultat['fenetre_jours']} jours fixées "
              f"d'avance)", ""]
    for f in resultat["fenetres"]:
        if not f["n"]:
            continue
        t = f["g"] / f["n"]
        peu = " (peu de signaux)" if f["n"] < N_MIN_FENETRE * 2 else ""
        lignes.append(
            f"  semaine du {jour(f['debut'])} : <b>{t:.0%}</b> sur {f['n']} "
            f"(95 % : {f['bas']:.0%}–{f['haut']:.0%}) · espérance "
            f"{_esperance(t):+.2f}/ordre · {f['par_jour']}/jour · "
            f"{f['paires']} paires · série de pertes max {f['serie_max']}"
            f"{peu}")
    t = gl["g"] / gl["n"]
    blocs = gl.get("blocs")
    lignes += ["", f"Tout : <b>{t:.1%}</b> sur {gl['n']} · Wilson "
                   f"{gl['wilson'][0]:.0%}–{gl['wilson'][1]:.0%}"
               + (f" · par blocs paire×jour {blocs[0]:.0%}–{blocs[1]:.0%} "
                  f"(plus honnête : les signaux d'une même journée se "
                  f"ressemblent)" if blocs else "")
               + f" · série de pertes max {gl['serie_max']}"]
    a = resultat.get("alerte")
    if a:
        etat = ("🔔 ALERTE — à examiner" if a["declenchee"] else
                "✅ compatible avec les fenêtres précédentes")
        lignes.append(f"Dernière fenêtre : {a['g']}/{a['n']} contre "
                      f"{a['attendu']:.0%} attendus (p = {a['p']:.2f}) → "
                      f"{etat}")
    categories: dict[str, list[str]] = {}
    for p, d in resultat["paires"].items():
        categories.setdefault(d["categorie"], []).append(
            f"{p.replace('_otc', '')} {d['g']}/{d['n']}")
    lignes += ["", "<b>Paires</b> (catégories descriptives, jamais un "
                   "verdict sur quelques ordres) :"]
    for cat in ("stabilité à confirmer", "à surveiller",
                "données insuffisantes"):
        if categories.get(cat):
            lignes.append(f"  {cat} : {', '.join(categories[cat])}")

    pp = resultat.get("premier_pas") or {}
    c = pp.get("comptes") or {}
    lignes += ["", "<b>EXP-022 — le premier pas</b> (plan rejoué sur toute "
                   "la référence, chaque entrée à son rang)"]
    for r, (n_r, g_r) in c.items():
        if n_r:
            bas, haut = wilson(g_r, n_r, 1.96)
            lignes.append(f"  pas {r} : {g_r}/{n_r} ({g_r / n_r:.0%}, 95 % : "
                          f"{bas:.0%}–{haut:.0%})")
    if "z" in pp:
        lignes.append(f"  écart pas 2 − pas 1 : z = {pp['z']:+.1f} → "
                      + ("effet du rang au-delà du hasard" if abs(pp["z"])
                         >= 1.96 else "pas d'effet du rang établi"))
    er = resultat.get("entree_retardee") or {}
    if er.get("confirmes"):
        nc = er["confirmes"]
        imm_n, imm_g = er["immediat"]
        pn, pg = er["perdus"]
        lignes += [
            "  <b>Entrée immédiate ou confirmée</b>, sur les mêmes signaux "
            "ZoneH1 :",
            f"  tous les signaux, entrée immédiate : {imm_g}/{imm_n} "
            f"({imm_g / imm_n:.0%})",
            f"  les {nc} confirmés ({nc / imm_n:.0%}) : immédiate "
            f"{er['apparies_immediat'] / nc:.0%} · confirmée "
            f"{er['apparies_confirme'] / nc:.0%} (seule l'heure d'entrée "
            f"change)",
            f"  les {pn} que la confirmation fait perdre gagnaient "
            f"{pg / max(1, pn):.0%} en entrée immédiate"]
        if er.get("degradation_mediane") is not None:
            lignes.append(f"  prix d'entrée : la confirmation entre en "
                          f"médiane {er['degradation_mediane']:+.2f} "
                          f"amplitude plus loin de la zone")
    lignes.append(
        "\nUne alerte ou un écart demande une enquête, pas un nouveau "
        "filtre : une règle changée sur du bruit ne se répare plus.")
    return "\n".join(lignes)
