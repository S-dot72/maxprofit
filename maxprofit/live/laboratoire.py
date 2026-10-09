"""
Le laboratoire : des variantes de ZoneH1 jugées sur des jours qu'elles n'ont
jamais vus.

Demandé le 2026-10-01 : « peux-tu trouver une stratégie encore meilleure ? ».
ZoneH1 est la seule rescapée de 150 expériences ; plutôt que repartir de zéro,
on teste quatre façons de l'améliorer, DÉCLARÉES ICI AVANT d'avoir vu un seul
résultat :

1. Bougie de rejet — la bougie du signal se referme dans le sens du trade.
   C'est le cas AUD/CAD et USD/CAD montré par l'utilisateur : le prix touche
   la zone mais rien n'indique un retournement, et l'on achète quand même.
2. Rejet avec mèche — la même, avec une mèche d'au moins un quart de son
   amplitude du côté de la zone : le marché est allé chercher le niveau et a
   été repoussé.
3. Tendance 4 h alignée — le mouvement des quatre dernières heures closes va
   dans le sens du trade, en plus de la tendance H1.
4. Heures rentables — les tranches de 4 heures dont le taux, mesuré sur la
   période d'étalonnage, est sous le seuil de rentabilité sont écartées.
5. Seconde chance — une zone déjà jouée 3 ou 4 fois reste jouable si le
   dernier signal qu'elle a donné a GAGNÉ. C'est la seule variante qui
   AJOUTE des signaux : un plan qui attend moins avance plus vite.

--- ⚠ Le protocole, et pourquoi il est strict ------------------------------

Plus on essaie de variantes, plus l'une d'elles gagne PAR HASARD. Avec ~500
signaux sur 30 jours, trois points d'écart sont dans le bruit. D'où :

- toutes partent de ZoneH1 TELLE QU'EN DIRECT (règle de l'heure en cours
  comprise) : c'est la référence à battre, pas une version idéale ;
- les seuils sont fixés ici, a priori ; seule la variante 4 apprend quelque
  chose, et seulement sur les 70 % les plus anciens ;
- le verdict se lit sur les 30 % les plus RÉCENTS, que rien n'a vus ;
- « prometteuse » exige d'être devant sur les DEUX périodes, de 5 points au
  moins sur la récente, avec assez de signaux pour que ça veuille dire
  quelque chose. Même alors, elle passe en test démo avant de remplacer
  quoi que ce soit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Callable, Mapping, Sequence

from maxprofit.apprentissage.lecons import SEUIL, Exemple, wilson
from maxprofit.live.simulation import CAPITAL, simuler

#: Part la plus ancienne des signaux, servant à l'étalonnage.
PART_ETALONNAGE = 0.7
#: Écart minimal, en points, sur la période récente.
ECART_MIN_POINTS = 5.0
#: Signaux minimaux sur la période récente pour trancher.
N_MIN_VALIDATION = 30
#: Mèche de rejet minimale (part de l'amplitude de la bougie).
MECHE_MIN = 0.25
#: Tranche horaire de la variante 4.
TRANCHE_H = 4
#: Signaux minimaux dans une tranche horaire pour l'écarter.
N_MIN_TRANCHE = 10
#: Entrées déjà offertes que ZoneH1 tolère (`entrees_max_par_zone`).
ENTREES_MAX = 3


@dataclass(frozen=True)
class Mesure:
    nom: str
    description: str
    n_etalonnage: int
    gagnes_etalonnage: int
    n_validation: int
    gagnes_validation: int
    jours: float
    gain_plan: float            # plan 2 pas sur toute la période, en $
    creux_plan: float
    verdict: str = ""

    @property
    def taux_etalonnage(self) -> float | None:
        return (self.gagnes_etalonnage / self.n_etalonnage
                if self.n_etalonnage else None)

    @property
    def taux_validation(self) -> float | None:
        return (self.gagnes_validation / self.n_validation
                if self.n_validation else None)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping) -> "Mesure":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def _heure_tranche(e: Exemple) -> int | None:
    h = e.contexte.get("heure_utc")
    return None if h is None else int(h) // TRANCHE_H


def _heures_perdantes(etalonnage: Sequence[Exemple]) -> set[int]:
    """Les tranches horaires sous le seuil, mesurées sur l'étalonnage seul."""
    comptes: dict[int, list[int]] = {}
    for e in etalonnage:
        t = _heure_tranche(e)
        if t is not None:
            c = comptes.setdefault(t, [0, 0])
            c[0] += 1
            c[1] += int(e.gagne)
    return {t for t, (n, g) in comptes.items()
            if n >= N_MIN_TRANCHE and g / n < SEUIL}


def _seconde_chance(etendus: Sequence[Exemple]) -> list[Exemple]:
    """Les signaux de ZoneH1 élargie : usée (3 ou 4 entrées) seulement si le
    dernier signal de la MÊME zone a gagné, et que son issue était connue."""
    gardes: list[Exemple] = []
    derniers: dict[tuple[str, float], Exemple] = {}
    for e in sorted(etendus, key=lambda x: x.ts_sec):
        entrees = e.contexte.get("entrees_deja_offertes", 0.0)
        niveau = e.contexte.get("niveau")
        cle = (e.pair, niveau) if niveau is not None else None
        precedent = derniers.get(cle) if cle else None
        if entrees < ENTREES_MAX:
            gardes.append(e)
        elif entrees < ENTREES_MAX + 2 and precedent is not None \
                and precedent.gagne \
                and precedent.ts_sec + 900 <= e.ts_sec:
            gardes.append(e)
        if cle is not None:
            derniers[cle] = e
    return gardes


def a_l_echeance(exemples: Sequence[Exemple], sec: int) -> list[Exemple]:
    """Les mêmes signaux de ZoneH1, jugés à l'échéance `sec`."""
    return [replace(e, gagne=bool(e.issues[sec]))
            for e in exemples if sec in e.issues]


def _a_l_entree(e: Exemple, k: int, gagne: bool) -> Exemple:
    """Le signal `e` daté, jugé et décrit à son entrée confirmée, `k`
    bougies après le signal.

    Le contexte mesuré à l'entrée (clés « e_ » du rejeu) remplace celui de
    la bougie du signal : c'est celui que la course voit en direct, et
    l'« entrée tardive » n'existe qu'à cet instant-là. Les issues aux autres
    échéances sont celles de la même entrée.
    """
    ctx = dict(e.contexte)
    ctx.update({c[2:]: v for c, v in e.contexte.items() if c.startswith("e_")})
    issues = {int(c.rsplit("_", 1)[1]): bool(v)
              for c, v in e.contexte.items()
              if c.startswith("gagne_confirme_")}
    return replace(e, ts_sec=e.ts_sec + 60 * k, gagne=gagne, contexte=ctx,
                   issues=issues)


def confirmes(exemples: Sequence[Exemple], fenetre: int = 1,
              echeance_sec: int | None = None) -> list[Exemple]:
    """Les signaux que la confirmation M1 laisse passer, datés, jugés et
    décrits à leur entrée : la clôture de la bougie qui confirme.

    `fenetre` : 1 pour la bougie suivante seulement (CONFIRMATION_M1=
    suivante), 2 ou 3 pour une confirmation qui peut venir plus tard.
    `echeance_sec` : jugés à une autre échéance que celle jouée (bougie
    suivante seulement).
    """
    if echeance_sec is not None:
        cle = f"gagne_suivant_{echeance_sec}"
        return [_a_l_entree(e, 1, bool(e.contexte[cle]))
                for e in exemples
                if e.contexte.get("retournement_suivant") == 1
                and cle in e.contexte]
    if fenetre == 1:
        return [_a_l_entree(e, 1, bool(e.contexte["gagne_suivant"]))
                for e in exemples
                if e.contexte.get("retournement_suivant") == 1
                and "gagne_suivant" in e.contexte]
    return [_a_l_entree(e, int(e.contexte["attente_confirmation"]),
                        bool(e.contexte["gagne_confirme"]))
            for e in exemples
            if 1 <= e.contexte.get("attente_confirmation", 0) <= fenetre
            and "gagne_confirme" in e.contexte]


def laboratoire(exemples: Sequence[Exemple],
                etendus: Sequence[Exemple] | None,
                en_direct: Callable[[Exemple], bool],
                echeance_sec: int = 900,
                autres: Mapping[str, tuple[str, Sequence[Exemple]]]
                | None = None) -> dict:
    """Les variantes mesurées, et leur verdict face à ZoneH1 en direct.

    `exemples` : ZoneH1 rejouée. `etendus` : ZoneH1 rejouée avec deux
    entrées par zone de plus (variante 5), ou `None`. `en_direct` : le filtre
    que la course applique déjà (heure en cours).
    """
    base = sorted((e for e in exemples if en_direct(e)), key=lambda e: e.ts_sec)
    if len(base) < 2 * N_MIN_VALIDATION:
        return {"note": f"Seulement {len(base)} signaux rejoués : il en faut "
                        f"{2 * N_MIN_VALIDATION} pour juger une variante."}
    coupure = base[int(len(base) * PART_ETALONNAGE)].ts_sec
    debut, fin = base[0].ts_sec, base[-1].ts_sec
    jours = max(1.0, (fin - debut) / 86400)
    heures_ko = _heures_perdantes([e for e in base if e.ts_sec < coupure])

    variantes: list[tuple[str, str, list[Exemple]]] = [
        ("ZoneH1 en direct", "la référence : ce que la course joue", base),
        ("1. Bougie de rejet",
         "la bougie du signal se referme dans le sens du trade",
         [e for e in base if e.contexte.get("bougies_contre", 1) == 0]),
        ("2. Rejet avec mèche",
         f"la même, avec une mèche d'au moins {MECHE_MIN:.0%} côté zone",
         [e for e in base if e.contexte.get("bougies_contre", 1) == 0
          and e.contexte.get("meche_rejet", 0) >= MECHE_MIN]),
        ("3. Tendance 4 h alignée",
         "les 4 dernières heures closes vont dans le sens du trade",
         [e for e in base if e.contexte.get("tendance_4h", 0) > 0]),
        ("4. Heures rentables",
         "sans les tranches de 4 h perdantes à l'étalonnage"
         + (" (" + ", ".join(f"{t * TRANCHE_H}-{t * TRANCHE_H + TRANCHE_H} h"
                             for t in sorted(heures_ko)) + " UTC)"
            if heures_ko else " (aucune écartée)"),
         [e for e in base if _heure_tranche(e) not in heures_ko]),
    ]
    if etendus:
        variantes.append((
            "5. Seconde chance",
            "zone usée rejouée si son dernier signal a gagné",
            _seconde_chance([e for e in etendus if en_direct(e)])))
    for nom, (description, liste) in (autres or {}).items():
        variantes.append((nom, description,
                          sorted((e for e in liste if en_direct(e)),
                                 key=lambda e: e.ts_sec)))
    variantes += [
        ("7. Confirmation M1, même bougie",
         "la bougie du signal va dans le sens du trade et casse l'extrême "
         "de la précédente",
         [e for e in base if e.contexte.get("retournement_meme") == 1]),
        ("8. Confirmation M1, bougie suivante",
         "on attend la bougie suivante : entrée à sa clôture si elle casse "
         "l'extrême de celle du signal (CONFIRMATION_M1=suivante)",
         confirmes(base)),
    ]
    # Le DÉBIT de la confirmation. Demandé le 2026-10-03 : 2,1 sessions par
    # jour en direct, le plan n'avance plus. Trois façons d'en regagner, à
    # juger contre ZoneH1 comme la variante 8, et à comparer à elle.
    for k in (2, 3):
        variantes.append((
            f"8f{k}. Confirmation M1 dans les {k} bougies suivantes",
            f"comme 8, mais la confirmation peut venir jusqu'à {k} bougies "
            f"après le signal ; entrée à la clôture de la première qui casse "
            f"l'extrême de la bougie du signal (CONFIRMATION_M1={k})",
            confirmes(base, k)))
    variantes.append((
        "8h. Confirmation M1, sans la règle de l'heure en cours",
        "la variante 8 sur TOUS les signaux de ZoneH1, même ceux que la "
        "règle de l'heure en cours écarte",
        confirmes(sorted(exemples, key=lambda e: e.ts_sec))))
    # L'élan des 30 dernières minutes, en plus de l'heure en cours : une
    # montée partie juste avant le début de l'heure échappe à la règle
    # actuelle (vente EUR/CHF du 02/10).
    variantes.append((
        "17. ZoneH1, élan 30 min pas fortement contre",
        "ZoneH1 en direct, sans les signaux dont l'élan des 30 dernières "
        "minutes va contre le trade de plus de 3,3 amplitudes M1",
        [e for e in base if e.contexte.get("elan_30m", 0.0) >= -3.3]))
    # Les niveaux inversés seuls : les entrées que ZoneH1 ne prenait pas.
    inversees = next((liste for n, _d, liste in variantes
                      if n.startswith("15.")), None)
    if inversees is not None:
        variantes.append((
            "16. Supports/résistances inversés seulement",
            "seulement les entrées sur un niveau cassé puis retesté dans le "
            "rôle inverse", [e for e in inversees
                             if e.contexte.get("inversee") == 1]))
    # Les critères de VALIDATION, sur ce que la course joue : ZoneH1 puis la
    # confirmation M1 (variante 8). Demandé le 2026-10-03 : le ZigZag et les
    # zones cassées ne décident pas des ordres, ils les valident. Jugés
    # contre la variante 8, pas contre ZoneH1 seule.
    huit = next((liste for n, _d, liste in variantes
                 if n.startswith("8. ")), [])
    for nom, description, garder in (
            ("8a. + zone sur un sommet/creux du ZigZag",
             "la zone coïncide avec un vrai creux (achat) ou sommet (vente) "
             "du ZigZag", lambda c: c.get("zigzag_pivot_zone") == 1),
            ("8b. + structure ZigZag dans le sens du trade",
             "creux montants pour un achat, sommets descendants pour une "
             "vente", lambda c: c.get("zigzag_structure") == 1),
            ("8c. + vagues de 15 min ou plus",
             "les dernières vagues du ZigZag durent au moins l'échéance",
             lambda c: c.get("rythme_zigzag", 0) >= echeance_sec / 60),
            ("8d. + ancien niveau cassé qui a changé de rôle",
             "support cassé devenu résistance pour une vente, et l'inverse",
             lambda c: c.get("niveau_inverse") == 1),
            ("8e. + pas de niveau cassé contre le trade à moins de 3 "
             "amplitudes", "aucun ancien niveau inversé ne barre la route "
             "dans le sens du trade",
             lambda c: c.get("obstacle_inverse", 99) >= 3)):
        variantes.append((nom, description + " — jugé contre la variante 8",
                          [e for e in huit if garder(e.contexte)]))
    # Les vagues du marché sont-elles assez longues pour une option de
    # 15 minutes ? Demandé le 2026-10-02 : un creux du ZigZag repris en six
    # minutes ne tient pas jusqu'à l'échéance.
    minutes = echeance_sec / 60

    def vagues_longues(liste):
        return [e for e in liste
                if e.contexte.get("rythme_zigzag", 0) >= minutes]

    variantes.append((
        "12. ZoneH1, vagues de 15 min ou plus",
        f"ZoneH1 en direct, seulement quand les dernières vagues du ZigZag "
        f"durent au moins {minutes:.0f} min : le creux a le temps de tenir "
        f"jusqu'à l'échéance", vagues_longues(base)))
    for prefixe, nom in (("10.", "13. ZoneH1 ZigZag, vagues de 15 min ou plus"),
                         ("11.", "14. Prise ZigZag, vagues de 15 min ou plus")):
        source = next((liste for n, _d, liste in variantes
                       if n.startswith(prefixe)), None)
        if source is not None:
            variantes.append((
                nom, f"variante {prefixe[:-1]}, seulement quand les "
                f"dernières vagues du ZigZag durent au moins "
                f"{minutes:.0f} min", vagues_longues(source)))

    # Les échéances COURTES, demandées le 2026-10-03 : mêmes entrées, seule
    # la sortie change. Le plan simulé espace ses pas de l'échéance jouée.
    # ⚠ Le seuil de rentabilité suppose le même payout (92 %) qu'à 15 min.
    echeance_de: dict[str, int] = {}
    for sec in (180, 300):
        m = sec // 60
        for nom, description, liste in (
                (f"E{m}. ZoneH1 en direct, échéance {m} min",
                 f"les mêmes signaux, sortie {m} min après l'entrée",
                 a_l_echeance(base, sec)),
                (f"E{m}c. Confirmation M1 (8), échéance {m} min",
                 f"la variante 8, sortie {m} min après l'entrée",
                 confirmes(base, echeance_sec=sec))):
            variantes.append((nom, description, liste))
            echeance_de[nom] = sec

    mesures = [_mesurer(nom, desc, liste, coupure, jours,
                        echeance_de.get(nom, echeance_sec))
               for nom, desc, liste in variantes]
    ref = mesures[0]
    ref_huit = next((m for m in mesures if m.nom.startswith("8. ")), ref)
    jugees = [ref] + [_juger(m, ref_huit if m.nom[:2] in ("8a", "8b", "8c",
                                                          "8d", "8e")
                             else ref) for m in mesures[1:]]
    return {"debut": debut, "fin": fin, "coupure": coupure,
            "mesures": [m.to_dict() for m in jugees],
            "paires": _par_paire(base, coupure),
            "paires_huit": _par_paire(huit, coupure)}


def _par_paire(base: Sequence[Exemple], coupure: int) -> dict:
    """{paire: [signaux anciens, gagnés, signaux récents, gagnés]}.

    Sert à dire si une baisse récente vient de TOUTES les paires — le
    marché a changé — ou de celles ajoutées récemment, qui n'ont aucun
    signal dans la période ancienne et pèsent donc seulement sur la récente.
    """
    comptes: dict[str, list[int]] = {}
    for e in base:
        c = comptes.setdefault(e.pair, [0, 0, 0, 0])
        i = 0 if e.ts_sec < coupure else 2
        c[i] += 1
        c[i + 1] += int(e.gagne)
    return comptes


def _groupe(paires: Mapping[str, Sequence[int]], nouvelles: bool
            ) -> tuple[int, int, int, int]:
    """Totaux des paires anciennes (`nouvelles=False`) ou nouvelles."""
    total = [0, 0, 0, 0]
    for c in paires.values():
        if (c[0] == 0) == nouvelles:
            total = [a + b for a, b in zip(total, c)]
    return tuple(total)


def _mesurer(nom, description, liste, coupure, jours, echeance_sec) -> Mesure:
    cal = [e for e in liste if e.ts_sec < coupure]
    val = [e for e in liste if e.ts_sec >= coupure]
    plan = simuler(liste, echeance_sec=echeance_sec, pas_max=2)
    return Mesure(nom, description, len(cal), sum(e.gagne for e in cal),
                  len(val), sum(e.gagne for e in val), round(jours, 1),
                  round(plan.solde - CAPITAL, 2), plan.creux)


def _juger(m: Mesure, ref: Mesure) -> Mesure:
    if not m.n_validation or not ref.n_validation:
        return replace(m, verdict="indécise — aucun signal récent")
    ecart_val = 100 * (m.taux_validation - ref.taux_validation)
    ecart_cal = 100 * ((m.taux_etalonnage or 0) - (ref.taux_etalonnage or 0))
    if ecart_val <= 0 or ecart_cal <= 0:
        verdict = "❌ pas mieux que la référence"
    elif m.n_validation < N_MIN_VALIDATION:
        verdict = (f"indécise — {m.n_validation} signaux récents, il en faut "
                   f"{N_MIN_VALIDATION}")
    elif ecart_val >= ECART_MIN_POINTS:
        verdict = ("✅ prometteuse — devant sur les deux périodes : à "
                   "tester en démo")
    else:
        verdict = (f"≈ légèrement devant ({ecart_val:+.1f} pts), dans le "
                   f"bruit")
    return replace(m, verdict=verdict)


def texte(resultat: Mapping | None) -> str:
    """`/laboratoire`."""
    if not resultat:
        return ("🧪 Laboratoire pas encore calculé : il accompagne le rejeu "
                "quotidien de l'apprentissage (quelques minutes après un "
                "redémarrage).")
    if "note" in resultat:
        return f"🧪 {resultat['note']}"
    import time
    coupe = time.strftime("%d/%m", time.gmtime(resultat["coupure"]))
    lignes = [
        "🧪 <b>Laboratoire — variantes de ZoneH1</b>",
        f"Étalonnage : signaux jusqu'au {coupe} · <b>jugement : signaux "
        f"depuis le {coupe}</b>, jamais vus par les variantes.",
        f"Seuil de rentabilité : {SEUIL:.1%}.",
    ]
    for d in resultat["mesures"]:
        m = Mesure.from_dict(d)
        tv = m.taux_validation
        tc = m.taux_etalonnage
        bas = wilson(m.gagnes_validation, m.n_validation, 1.96)[0] \
            if m.n_validation else 0.0
        par_jour = (m.n_etalonnage + m.n_validation) / m.jours
        lignes += [
            "",
            f"<b>{m.nom}</b> — {m.description}",
            f"  récents : <b>{_pct(tv)}</b> sur {m.n_validation} "
            f"(au moins {bas:.0%} à 95 %) · anciens : {_pct(tc)} sur "
            f"{m.n_etalonnage}",
            f"  {par_jour:.1f} signaux/jour · plan 2 pas : "
            f"{m.gain_plan:+.2f} $, creux −{m.creux_plan:.2f} $",
        ]
        if m.verdict:
            lignes.append(f"  {m.verdict}")
    lignes.append(
        "\nUne variante « prometteuse » n'est pas adoptée d'office : elle "
        "passe d'abord en test démo.")
    if resultat.get("paires"):
        lignes += ["", *_texte_paires(resultat["paires"], coupe)]
    if resultat.get("paires_huit"):
        lignes += ["", *_texte_paires_huit(resultat["paires_huit"])]
    return "\n".join(lignes)


def _texte_paires_huit(paires: Mapping[str, Sequence[int]]) -> list[str]:
    """Chaque paire sous la variante 8, ce que la course joue.

    Les paires exclues (PAIRES_EXCLUES, et celles au taux perdant) l'ont été
    sur ZoneH1 SANS confirmation. Sous confirmation, leur taux peut avoir
    changé : c'est ici qu'on voit si elles méritent toujours de l'être.
    """
    lignes = ["🔎 <b>Par paire sous la variante 8</b> (tout le mois ; ⚠ = "
              "perdante PROUVÉE à 95 %, ✅ = gagnante prouvée)"]
    totaux = {p: (c[0] + c[2], c[1] + c[3]) for p, c in paires.items()}
    for p, (n, g) in sorted(totaux.items(),
                            key=lambda x: -(x[1][1] / x[1][0]) if x[1][0]
                            else 0.0):
        if not n:
            continue
        bas, haut = wilson(g, n, 1.96)
        marque = " ⚠" if haut < SEUIL else " ✅" if bas > SEUIL else ""
        lignes.append(f"  {p.replace('_otc', '')} : {g / n:.0%} sur {n} "
                      f"(entre {bas:.0%} et {haut:.0%}){marque}")
    return lignes


def _texte_paires(paires: Mapping[str, Sequence[int]], coupe: str) -> list[str]:
    """D'où vient l'écart entre la période ancienne et la récente."""
    def taux(n, g):
        return f"{g / n:.1%} sur {n}" if n else "—"

    na, ga, nr, gr = _groupe(paires, nouvelles=False)
    _, _, nr2, gr2 = _groupe(paires, nouvelles=True)
    nouvelles = sorted(p.replace("_otc", "") for p, c in paires.items()
                       if c[0] == 0)
    lignes = [
        "🔎 <b>D'où vient la baisse récente ?</b> (ZoneH1 en direct)",
        f"Paires présentes avant le {coupe} : avant <b>{taux(na, ga)}</b> → "
        f"depuis <b>{taux(nr, gr)}</b>",
        f"Paires sans signal avant le {coupe} (nouvelles) : depuis "
        f"<b>{taux(nr2, gr2)}</b>"
        + (f" — {', '.join(nouvelles)}" if nouvelles else ""),
    ]
    # ⚠ Les paires d'origine se comparent à LEUR passé, pas au seuil. La
    # première version concluait « ce sont les nouvelles » dès que les
    # anciennes restaient au-dessus de 52,1 % : 64,3 % → 52,8 % passait pour
    # une tenue, alors que c'était l'essentiel de la baisse.
    if na and nr:
        baisse = 100 * (ga / na - gr / nr)
        if baisse >= ECART_MIN_POINTS:
            lignes.append(
                f"→ Les paires d'origine ont baissé elles aussi "
                f"({-baisse:+.1f} pts) : la baisse ne vient PAS seulement "
                f"des nouvelles paires.")
        elif nr2 and gr2 / nr2 < SEUIL:
            lignes.append("→ Les paires d'origine tiennent : ce sont les "
                          "NOUVELLES paires qui tirent le taux vers le bas.")
    lignes.append("Par paire, depuis le " + coupe
                  + " (avant entre parenthèses) :")
    for p, (n0, g0, n1, g1) in sorted(
            paires.items(), key=lambda x: (x[1][3] / x[1][2]) if x[1][2]
            else 2.0):
        if not n1:
            continue
        marque = " 🆕" if n0 == 0 else ""
        avant = f" ({g0 / n0:.0%} sur {n0})" if n0 else ""
        lignes.append(f"  {p.replace('_otc', '')}{marque} : {g1 / n1:.0%} "
                      f"sur {n1}{avant}")
    return lignes


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1%}"
