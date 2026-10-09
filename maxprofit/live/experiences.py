"""
Les expériences contrôlées : une référence fixe, et UNE variable à la fois.

--- D'où elles viennent ------------------------------------------------------

Demandé le 2026-10-08, après deux pertes que le bot n'a pas su expliquer :

- GBPAUD, CALL à la 3e touche d'un ancien plafond devenu support : « la
  précédente impulsion est faible, celle qui suit sera encore plus faible »,
  « sur les 15 dernières bougies, seulement 4 vertes » ;
- GBPUSD, CALL pris 5 amplitudes au-dessus de son support, le rebond déjà
  consommé — et l'autopsie concluait « aucune cause, c'est la variance ».

Et une méthode : ne plus empiler les filtres, mais garder la référence
(ce que la course joue : ZoneH1 + confirmation M1) comme contrôle permanent,
et lui opposer une variable à la fois.

--- Le protocole ---------------------------------------------------------------

1. La référence : les entrées confirmées, décrites À L'ENTRÉE (comme en
   direct), filtrées par la règle de l'heure en cours comme en direct.
2. Les 70 % les plus anciens servent à l'ÉTALONNAGE, les 30 % récents au
   JUGEMENT, jamais vus pour fixer quoi que ce soit.
3. Chaque variable est coupée en terciles sur l'étalonnage seul. Les
   coupes ne sont pas choisies pour faire joli : ce sont les tiers.
4. Le « pire tiers » est désigné sur l'étalonnage, puis on regarde ce que
   la référence aurait donné sans lui sur la période récente.
5. Verdict « à tester en démo » seulement si c'est mieux sur LES DEUX
   périodes, d'au moins `ECART_MIN_POINTS` sur la récente, avec au moins
   `N_MIN_VALIDATION` signaux récents. Comme au laboratoire.

⚠ Huit variables, quatre seuils d'élan, sept échéances : plus de vingt
comparaisons. Une sur vingt passe le seuil de 95 % par pur hasard. Un
verdict positif désigne un candidat pour la démo, pas une découverte.

Les grandeurs : le taux de réussite, et l'ESPÉRANCE par ordre en mises,
p × 0,92 − (1 − p), qui dit ce qu'un ordre rapporte en moyenne. Une
variante qui gagne plus souvent mais trois fois moins souvent peut
rapporter moins par jour : l'espérance par jour le montre.
"""

from __future__ import annotations

from typing import Callable, Mapping, Sequence

from maxprofit.apprentissage.lecons import (  # noqa: F401
    SEUIL, Exemple, situer, wilson)
from maxprofit.live.laboratoire import (
    ECART_MIN_POINTS, N_MIN_VALIDATION, PART_ETALONNAGE, confirmes)
from maxprofit.live.simulation import CAPITAL, simuler

PAYOUT = 0.92

#: (clé du contexte, ce qu'elle mesure). Toutes À L'ENTRÉE, orientées dans
#: le sens du trade, en amplitudes M1 sauf mention.
VARIABLES: tuple[tuple[str, str], ...] = (
    ("distance_niveau", "entrée tardive — distance de l'entrée à la zone"),
    ("rebond_15", "fatigue de la zone — plus fort rebond des 15 bougies "
                  "précédentes"),
    ("favorables_15", "momentum — part des 15 dernières bougies dans le "
                      "sens du trade"),
    ("elan_30m", "élan des 30 dernières minutes"),
    ("mouvement_heure", "mouvement depuis le début de l'heure"),
    ("volatilite_relative", "volatilité des 15 dernières minutes / "
                            "habituelle"),
    ("entrees_deja_offertes", "entrées déjà offertes par la zone"),
    ("rythme_zigzag", "durée des vagues du ZigZag (min)"),
    # Les séquences de bougies à l'entrée (séquence × zone vierge).
    ("serie_sens", "séquence — bougies consécutives de même couleur à "
                   "l'entrée (+ dans le sens du trade)"),
    ("amplitude_serie", "séquence — chemin parcouru par la série"),
    ("rythme_bougies", "séquence — taille des 3 dernières bougies / des 3 "
                       "précédentes (plus de 1 = accélère)"),
    ("alternances_10", "séquence — changements de couleur sur 10 bougies"),
)

#: Les variables dont chaque tiers est aussi jugé à chaque échéance : la
#: « matrice des expirations » (une échéance par contexte ?).
MATRICES: tuple[str, ...] = ("serie_sens", "volatilite_relative", "elan_30m")

#: Les seuils d'élan sur 30 minutes demandés (B1 à B4) : on écarte les
#: entrées dont l'élan va contre le trade de plus de |seuil| amplitudes.
SEUILS_ELAN: tuple[float, ...] = (-3.3, -2.5, -2.0, -1.5)

ECHEANCES: tuple[int, ...] = (60, 120, 180, 240, 300, 600, 900)

#: Effectif minimal d'un tiers, sur l'étalonnage, pour être désigné « pire ».
N_MIN_TIERS = 10


def esperance(p: float) -> float:
    """Ce que rapporte un ordre en moyenne, en mises, au payout de 92 %."""
    return p * PAYOUT - (1 - p)


def _compte(liste: Sequence[Exemple], coupure: int,
            issue: Callable[[Exemple], bool] = lambda e: e.gagne
            ) -> list[int]:
    """[anciens, gagnés, récents, gagnés]."""
    c = [0, 0, 0, 0]
    for e in liste:
        k = 0 if e.ts_sec < coupure else 2
        c[k] += 1
        c[k + 1] += int(issue(e))
    return c


def _taux(n: int, g: int) -> float | None:
    return g / n if n else None


def _juger(c: Sequence[int], ref: Sequence[int]) -> str:
    ta, tr = _taux(c[0], c[1]), _taux(c[2], c[3])
    ra, rr = _taux(ref[0], ref[1]), _taux(ref[2], ref[3])
    if ta is None or tr is None or ra is None or rr is None:
        return "indécise — trop peu de signaux"
    ecart_r, ecart_a = 100 * (tr - rr), 100 * (ta - ra)
    if ecart_r <= 0 or ecart_a <= 0:
        return "❌ pas mieux que la référence"
    if c[2] < N_MIN_VALIDATION:
        return (f"indécise — {c[2]} signaux récents, il en faut "
                f"{N_MIN_VALIDATION}")
    if ecart_r >= ECART_MIN_POINTS:
        return "✅ mieux sur les deux périodes : candidate pour la démo"
    return f"≈ devant de {ecart_r:+.1f} pts, dans le bruit"


def _series(liste: Sequence[Exemple]) -> dict:
    """Les pertes d'affilée de la référence, dans l'ordre du temps, face à
    ce que des pertes indépendantes donneraient."""
    n = len(liste)
    perdus = [not e.gagne for e in liste]
    q = sum(perdus) / n if n else 0.0
    series, courante = [], 0
    for x in perdus + [False]:
        if x:
            courante += 1
        elif courante:
            series.append(courante)
            courante = 0

    def attendu(k):
        return 0.0 if n < k else q ** k + (n - k) * (1 - q) * q ** k

    return {"n": n, "max": max(series, default=0),
            # Clés en texte : le résultat est stocké en JSON.
            "vues": {str(k): sum(1 for s in series if s >= k)
                     for k in (2, 3, 4)},
            "attendues": {str(k): round(attendu(k), 1) for k in (2, 3, 4)}}


def experiences(exemples: Sequence[Exemple],
                en_direct: Callable[[Exemple], bool],
                fenetre: int = 3, echeance_sec: int = 900) -> dict:
    """La référence et ses expériences. `exemples` : ZoneH1 rejouée ;
    `en_direct` : la règle de l'heure en cours ; `fenetre` : la
    confirmation M1 de la course (1 = bougie suivante, 2 ou 3)."""
    joues = sorted(confirmes(exemples, fenetre), key=lambda e: e.ts_sec)
    base = [e for e in joues if en_direct(e)]
    nom = "8" if fenetre == 1 else f"8f{fenetre}"
    if len(base) < 2 * N_MIN_VALIDATION:
        return {"note": f"Seulement {len(base)} entrées confirmées rejouées "
                        f"({nom}) : il en faut {2 * N_MIN_VALIDATION}."}
    coupure = base[int(len(base) * PART_ETALONNAGE)].ts_sec
    jours = max(1.0, (base[-1].ts_sec - base[0].ts_sec) / 86400)
    ref = _compte(base, coupure)
    plan = simuler(base, echeance_sec=echeance_sec, pas_max=2)
    sortie: dict = {
        "nom": nom, "fenetre": fenetre, "coupure": coupure,
        "jours": round(jours, 1), "reference": ref,
        "plan": [round(plan.solde - CAPITAL, 2), plan.creux],
        "series": _series(base), "variables": [], "elan": [],
        "delai": {}, "echeances": {},
    }
    etalonnage = [e for e in base if e.ts_sec < coupure]

    for cle, libelle in VARIABLES:
        valeurs = sorted(e.contexte[cle] for e in etalonnage
                         if cle in e.contexte)
        if len(valeurs) < 3 * N_MIN_TIERS:
            continue
        distinctes = sorted(set(valeurs))
        if len(distinctes) < 2:
            continue                      # rien à comparer
        if len(distinctes) <= 4:
            groupes = [(f"= {x:g}", (lambda x: lambda v: v == x)(x))
                       for x in distinctes]
            coupes = None
            valeurs_tiers = distinctes
        else:
            c1 = valeurs[len(valeurs) // 3]
            c2 = valeurs[2 * len(valeurs) // 3]
            groupes = [(f"moins de {c1:+.2f}", lambda v, c1=c1: v < c1),
                       (f"de {c1:+.2f} à {c2:+.2f}",
                        lambda v, c1=c1, c2=c2: c1 <= v < c2),
                       (f"{c2:+.2f} et plus", lambda v, c2=c2: v >= c2)]
            coupes = [c1, c2]
            valeurs_tiers = None
        avec = [e for e in base if cle in e.contexte]
        tiers = []
        for nom_t, dedans in groupes:
            sous = [e for e in avec if dedans(e.contexte[cle])]
            tiers.append({"libelle": nom_t, "compte": _compte(sous, coupure)})
        eligibles = [i for i, t in enumerate(tiers)
                     if t["compte"][0] >= N_MIN_TIERS]
        if not eligibles:
            continue
        pire = min(eligibles,
                   key=lambda i: tiers[i]["compte"][1] / tiers[i]["compte"][0])
        dedans_pire = groupes[pire][1]
        sans = [e for e in base
                if cle not in e.contexte or not dedans_pire(e.contexte[cle])]
        compte_sans = _compte(sans, coupure)
        # Un gradient qui va dans le même sens sur les deux périodes vaut
        # plus qu'un écart isolé : c'est ce qui distingue un effet du bruit.
        taux_a = [_taux(t["compte"][0], t["compte"][1]) for t in tiers]
        taux_r = [_taux(t["compte"][2], t["compte"][3]) for t in tiers]
        par_echeance = None
        if cle in MATRICES:
            par_echeance = [
                {str(sec): _compte([e for e in avec if dedans(e.contexte[cle])
                                    and sec in e.issues], coupure,
                                   lambda e, sec=sec: e.issues[sec])
                 for sec in ECHEANCES}
                for _nom, dedans in groupes]
        sortie["variables"].append({
            "cle": cle, "libelle": libelle, "coupes": coupes,
            "par_echeance": par_echeance,
            "valeurs": valeurs_tiers,
            "tiers": tiers, "pire": pire, "sans_pire": compte_sans,
            "gradient": coupes is not None and _monotone(taux_a)
            and _monotone(taux_r) and _sens(taux_a) == _sens(taux_r),
            "verdict": _juger(compte_sans, ref)})

    for seuil in SEUILS_ELAN:
        garde = [e for e in base if e.contexte.get("elan_30m", 0.0) >= seuil]
        c = _compte(garde, coupure)
        sortie["elan"].append({"seuil": seuil, "compte": c,
                               "verdict": _juger(c, ref)})

    if fenetre > 1:
        for k in range(1, fenetre + 1):
            sous = [e for e in base
                    if e.contexte.get("attente_confirmation") == k]
            if sous:
                sortie["delai"][str(k)] = _compte(sous, coupure)

    for sec in ECHEANCES:
        sous = [e for e in base if sec in e.issues]
        if sous:
            sortie["echeances"][str(sec)] = _compte(
                sous, coupure, lambda e, sec=sec: e.issues[sec])
    return sortie


def _monotone(taux: Sequence[float | None]) -> bool:
    t = [x for x in taux if x is not None]
    return len(t) == len(taux) and (
        all(a <= b for a, b in zip(t, t[1:]))
        or all(a >= b for a, b in zip(t, t[1:])))


def _sens(taux: Sequence[float | None]) -> int:
    t = [x for x in taux if x is not None]
    return 0 if len(t) < 2 else (1 if t[-1] > t[0] else -1)


def _ligne(c: Sequence[int], jours: float) -> str:
    n = c[0] + c[2]
    tr, ta = _taux(c[2], c[3]), _taux(c[0], c[1])
    tout = _taux(n, c[1] + c[3])
    bas = wilson(c[3], c[2], 1.96)[0] if c[2] else 0.0
    par_jour = n / jours
    ev = esperance(tout) if tout is not None else 0.0
    return (f"récents <b>{_pct(tr)}</b> sur {c[2]} (au moins {bas:.0%}) · "
            f"anciens {_pct(ta)} sur {c[0]} · {par_jour:.1f}/jour · "
            f"espérance {ev:+.3f}/ordre, {ev * par_jour:+.2f} mise/jour")


def _cellules(par_sec: Mapping[str, Sequence[int]]) -> str:
    """« 1m 55% · 2m 57% · … » sur les deux périodes réunies."""
    morceaux = []
    for sec, c in par_sec.items():
        n, g = c[0] + c[2], c[1] + c[3]
        if n:
            morceaux.append(f"{int(sec) // 60}m {g / n:.0%}")
    return " · ".join(morceaux) or "—"


def texte_persistance(resultat: Mapping | None) -> str:
    """La persistance des bougies, toutes paires, hors zones."""
    if not resultat:
        return ""
    lignes = ["<b>Persistance des bougies</b> — toutes les bougies M1 "
              "rejouées, hors zones : après N bougies de même couleur, "
              "position dans le sens de la série. ✅ = au-dessus de 52,1 % "
              "sur les DEUX périodes (suivre la série) ; 🔁 = sous 47,9 % sur "
              "les deux (la contrer)."]
    rentables = []
    for nom in ("verte", "rouge"):
        table = resultat.get(nom) or {}
        for n, par_sec in table.items():
            morceaux = []
            for sec, c in par_sec.items():
                if not (c[0] and c[2]):
                    continue
                ta, tr = c[1] / c[0], c[3] / c[2]
                tout = (c[1] + c[3]) / (c[0] + c[2])
                marque = ("✅" if min(ta, tr) > SEUIL else
                          "🔁" if max(ta, tr) < 1 - SEUIL else "")
                if marque:
                    rentables.append(f"{n} {nom}s, {int(sec) // 60} min : "
                                     f"{tout:.1%} ({marque}, anciens "
                                     f"{ta:.1%}, récents {tr:.1%}, "
                                     f"{c[0] + c[2]} cas)")
                morceaux.append(f"{int(sec) // 60}m {tout:.1%}{marque}")
            total = sum(c[0] + c[2] for c in par_sec.values()) // max(
                1, len(par_sec))
            plus = "+" if int(n) == resultat.get("n_max") else ""
            pluriel = "s" if int(n) > 1 else ""
            lignes.append(f"  {n}{plus} {nom}{pluriel} ({total} cas) : "
                          + " · ".join(morceaux))
    if rentables:
        lignes.append("Au-delà du seuil sur les deux périodes :")
        lignes += [f"  • {r}" for r in rentables[:12]]
    else:
        lignes.append("→ Aucune longueur de série ne donne un avantage "
                      "au-delà du seuil sur les deux périodes : la couleur "
                      "des bougies passées ne prédit pas la suite, seule.")
    return "\n".join(lignes)


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1%}"


def texte(resultat: Mapping | None) -> str:
    """`/experiences`."""
    if not resultat:
        return ("🔬 Expériences pas encore calculées : elles accompagnent le "
                "rejeu quotidien (quelques minutes après un redémarrage).")
    if "note" in resultat:
        return f"🔬 {resultat['note']}"
    import time
    coupe = time.strftime("%d/%m", time.gmtime(resultat["coupure"]))
    jours = resultat["jours"]
    ref = resultat["reference"]
    gain, creux = resultat["plan"]
    s = resultat["series"]
    lignes = [
        f"🔬 <b>Expériences — référence {resultat['nom']}</b> (ce que la "
        f"course joue, décrit à l'entrée)",
        f"Étalonnage jusqu'au {coupe}, <b>jugement depuis le {coupe}</b>. "
        f"Seuil de rentabilité {SEUIL:.1%}, espérance = p × 0,92 − (1 − p).",
        "",
        f"<b>Référence</b> : {_ligne(ref, jours)}",
        f"  plan 2 pas : {gain:+.2f} $, creux −{creux:.2f} $",
        f"  pertes d'affilée (tous signaux, dans l'ordre) : plus longue "
        f"{s['max']} ; séries de 2+ : {s['vues']['2']} (hasard "
        f"{s['attendues']['2']}), 3+ : {s['vues']['3']} (hasard "
        f"{s['attendues']['3']}), 4+ : {s['vues']['4']} (hasard "
        f"{s['attendues']['4']})",
    ]
    if resultat.get("echeances"):
        lignes += ["", "<b>Échéance</b> (mêmes entrées, seule la sortie "
                       "change ; ⚠ payout supposé identique) :"]
        for sec, c in resultat["echeances"].items():
            lignes.append(f"  {int(sec) // 60} min : {_ligne(c, jours)}")
    if resultat.get("delai"):
        lignes += ["", "<b>Délai de la confirmation</b> :"]
        for k, c in resultat["delai"].items():
            lignes.append(f"  bougie n°{k} : {_ligne(c, jours)}")
    for v in resultat["variables"]:
        lignes += ["", f"<b>{v['libelle']}</b>"
                   + (" — 📈 gradient constant sur les deux périodes"
                      if v["gradient"] else "")]
        for i, t in enumerate(v["tiers"]):
            marque = " ⬅ pire tiers à l'étalonnage" if i == v["pire"] else ""
            lignes.append(f"  {t['libelle']} : {_ligne(t['compte'], jours)}"
                          f"{marque}")
        lignes.append(f"  sans le pire tiers : {_ligne(v['sans_pire'], jours)}")
        lignes.append(f"  {v['verdict']}")
        for t, par_sec in zip(v["tiers"], v.get("par_echeance") or ()):
            lignes.append(f"  ⏱ {t['libelle']} : {_cellules(par_sec)}")
    if resultat.get("elan"):
        lignes += ["", "<b>Seuils d'élan sur 30 min</b> (on écarte les "
                       "entrées dont l'élan va plus loin contre le trade) :"]
        for x in resultat["elan"]:
            lignes.append(f"  élan au moins {x['seuil']:+.1f} : "
                          f"{_ligne(x['compte'], jours)}")
            lignes.append(f"    {x['verdict']}")
    persistance = texte_persistance(resultat.get("persistance"))
    if persistance:
        lignes += ["", persistance]
    lignes.append(
        "\n⚠ Plus de vingt comparaisons : une sur vingt passe par hasard. "
        "Un ✅ désigne une candidate pour la démo, pas une découverte.")
    return "\n".join(lignes)
