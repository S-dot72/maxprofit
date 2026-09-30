"""
Les leçons : les contextes qui perdent de façon ÉTABLIE, et rien d'autre.

--- Le protocole -----------------------------------------------------------

Les exemples (signal rejoué ou réel, son contexte, son issue) sont rangés
dans l'ordre du temps et coupés en deux :

- l'ÉTALONNAGE (les 70 % les plus anciens) propose des tranches de contexte —
  « élan des 15 dernières minutes sous −1,2 amplitude », « 3e entrée sur la
  zone ». Une tranche n'est candidate que si, sur au moins
  `N_MIN_ETALONNAGE` signaux, même la borne HAUTE de son intervalle de
  confiance à 99 % reste sous le seuil de rentabilité. Le 99 % et non le 95 % :
  une cinquantaine de tranches sont essayées, et à 95 % deux ou trois
  sortiraient par pur hasard ;
- la VALIDATION (les 30 % les plus récents, que l'étalonnage n'a pas vus)
  doit confirmer : au moins `N_MIN_VALIDATION` signaux, taux sous le seuil ;
- enfin chaque leçon doit AMÉLIORER le taux des signaux gardés sur la
  validation, et l'ensemble des leçons ne doit pas écarter plus de
  `COUVERTURE_MAX` des signaux : dix-huit sessions en douze heures
  demandent du débit, et un filtre qui ne laisse rien passer ne perd jamais.

Une tranche établie mais non confirmée reste CANDIDATE : affichée, jamais
appliquée.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Sequence

from maxprofit.apprentissage.contexte import CARACTERISTIQUES, LIBELLES

#: Le seuil de rentabilité d'une option à 92 % : 1 / 1,92.
SEUIL = 1 / 1.92
#: Quantile normal de la borne de confiance (99 %, bilatéral).
Z = 2.576
N_MIN_ETALONNAGE = 40
N_MIN_VALIDATION = 15
PART_VALIDATION = 0.30
COUVERTURE_MAX = 0.35
#: En dessous, on n'apprend rien : les tranches n'auraient pas 40 signaux.
N_MIN_EXEMPLES = 150
#: Effectif minimal d'une tranche citée dans une autopsie.
N_MIN_AUTOPSIE = 30
#: Les mouvements affichés par `/lecons`, du plus court au plus long : c'est
#: la question posée par l'opérateur, captures à l'appui — la stratégie
#: prend-elle position contre le mouvement de l'heure en cours ?
TENDANCES: tuple[str, ...] = ("mouvement_heure", "elan_30m", "tendance_h1")

#: Échéances comparées sur les mêmes signaux (secondes).
ECHEANCES_COMPAREES: tuple[int, ...] = (300, 600, 900)

#: Taux de réussite par pas mesuré sur l'historique de la stratégie (221
#: signaux, 12 jours) : sert à l'autopsie tant que rien n'a été appris.
TAUX_DE_REFERENCE = 0.579


@dataclass(frozen=True)
class Exemple:
    ts_sec: int
    pair: str
    contexte: Mapping[str, float]
    gagne: bool
    #: Issue du MÊME signal à d'autres échéances : {secondes: gagné}. Une
    #: échéance sans bougie de sortie, ou à égalité, est absente.
    issues: Mapping[int, bool] = field(default_factory=dict)


def wilson(gains: int, n: int, z: float = Z) -> tuple[float, float]:
    """L'intervalle de confiance de Wilson d'une proportion."""
    if n == 0:
        return 0.0, 1.0
    p = gains / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    demi = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
            / (1 + z * z / n))
    return max(0.0, centre - demi), min(1.0, centre + demi)


def _nombre(v: float, caracteristique: str) -> str:
    if caracteristique in ("heure_utc", "bougies_contre", "touches",
                           "entrees_deja_offertes"):
        return f"{v:.0f}"
    return f"{v:+.2f}" if caracteristique not in (
        "volatilite_relative", "corps_signal", "meche_rejet") else f"{v:.2f}"


@dataclass(frozen=True)
class Tranche:
    """[bas, haut[ sur une caractéristique ; `None` = sans borne."""

    caracteristique: str
    bas: float | None
    haut: float | None

    def contient(self, ctx: Mapping[str, float]) -> bool:
        v = ctx.get(self.caracteristique)
        if v is None:
            return False
        return ((self.bas is None or v >= self.bas)
                and (self.haut is None or v < self.haut))

    def libelle(self) -> str:
        c = self.caracteristique
        nom = LIBELLES.get(c, c)
        # « sous » et non « < » : les messages partent en HTML, où un « < »
        # nu fait rejeter le message entier par Telegram.
        if self.bas is None:
            return f"{nom} sous {_nombre(self.haut, c)}"
        if self.haut is None:
            return f"{nom} ≥ {_nombre(self.bas, c)}"
        if c == "heure_utc":
            return f"{nom} de {self.bas:.0f} h à {self.haut:.0f} h"
        return f"{nom} entre {_nombre(self.bas, c)} et {_nombre(self.haut, c)}"

    def to_dict(self) -> dict:
        return {"c": self.caracteristique, "bas": self.bas, "haut": self.haut}

    @classmethod
    def from_dict(cls, d: Mapping) -> "Tranche":
        return cls(str(d["c"]), d.get("bas"), d.get("haut"))


@dataclass(frozen=True)
class Regle:
    tranche: Tranche
    n: int
    taux: float
    n_validation: int
    taux_validation: float

    def texte(self) -> str:
        return (f"{self.tranche.libelle()} — {self.taux:.0%} sur {self.n} "
                f"signaux, confirmé {self.taux_validation:.0%} sur "
                f"{self.n_validation} ensuite")

    def to_dict(self) -> dict:
        return {"tranche": self.tranche.to_dict(), "n": self.n,
                "taux": self.taux, "n_val": self.n_validation,
                "taux_val": self.taux_validation}

    @classmethod
    def from_dict(cls, d: Mapping) -> "Regle":
        return cls(Tranche.from_dict(d["tranche"]), int(d["n"]),
                   float(d["taux"]), int(d["n_val"]), float(d["taux_val"]))


@dataclass(frozen=True)
class StatTranche:
    tranche: Tranche
    n: int
    taux: float

    def to_dict(self) -> dict:
        return {"tranche": self.tranche.to_dict(), "n": self.n,
                "taux": self.taux}

    @classmethod
    def from_dict(cls, d: Mapping) -> "StatTranche":
        return cls(Tranche.from_dict(d["tranche"]), int(d["n"]),
                   float(d["taux"]))


def taux_detectable(n_etalonnage: int) -> float | None:
    """Le taux de réussite qu'un contexte doit avoir, AU PLUS, pour que
    l'étalonnage puisse l'établir comme perdant.

    Une tranche typique couvre un cinquième des signaux (jamais moins de
    `N_MIN_ETALONNAGE`). Plus l'étalonnage est court, plus un contexte doit
    perdre franchement pour être vu : c'est ce qui distingue « aucun contexte
    ne perd » de « aucun contexte ne perd ASSEZ pour qu'on le voie ».
    """
    n = max(N_MIN_ETALONNAGE, n_etalonnage // 5)
    if n_etalonnage < N_MIN_ETALONNAGE:
        return None
    for gains in range(n, -1, -1):
        if wilson(gains, n)[1] < SEUIL:
            return gains / n
    return None


def _tiers(t: Tranche) -> str:
    c = t.caracteristique
    if t.bas is None:
        return f"le plus contre (sous {_nombre(t.haut, c)})"
    if t.haut is None:
        return f"le plus avec (≥ {_nombre(t.bas, c)})"
    return f"entre les deux ({_nombre(t.bas, c)} à {_nombre(t.haut, c)})"


def _taux(exemples: Sequence[Exemple]) -> float | None:
    return (sum(e.gagne for e in exemples) / len(exemples)
            if exemples else None)


def _quantile(tries: Sequence[float], q: float) -> float:
    return tries[min(len(tries) - 1, max(0, int(q * len(tries))))]


def _tranches(caracteristique: str, valeurs: Sequence[float],
              coupes=(0.2, 1 / 3, 2 / 3, 0.8)) -> list[Tranche]:
    """Les tranches essayées : des tranches de trois heures pour l'heure,
    les queues et le milieu de la distribution pour le reste."""
    if caracteristique == "heure_utc":
        return [Tranche(caracteristique, float(h), float(h + 3))
                for h in range(0, 24, 3)]
    tries = sorted(valeurs)
    if not tries:
        return []
    q = [_quantile(tries, x) for x in coupes]
    if len(coupes) == 2:
        bornes = [(None, q[0]), (q[0], q[1]), (q[1], None)]
    else:
        bornes = [(None, q[0]), (None, q[1]), (q[1], q[2]),
                  (q[2], None), (q[3], None)]
    vues, sortie = set(), []
    for bas, haut in bornes:
        if bas is not None and haut is not None and haut <= bas:
            continue
        if bas is not None and bas <= tries[0] and haut is None:
            continue                    # la tranche entière : rien à apprendre
        if haut is not None and haut <= tries[0]:
            continue                    # vide
        if (bas, haut) not in vues:
            vues.add((bas, haut))
            sortie.append(Tranche(caracteristique, bas, haut))
    return sortie


@dataclass
class Apprentissage:
    regles: tuple[Regle, ...] = ()
    candidates: tuple[Regle, ...] = ()
    statistiques: tuple[StatTranche, ...] = ()
    n: int = 0
    taux: float | None = None
    debut_sec: int = 0
    fin_sec: int = 0
    n_validation: int = 0
    taux_validation_avant: float | None = None
    taux_validation_apres: float | None = None
    part_ecartee: float = 0.0
    cree_ts: int = field(default_factory=lambda: int(time.time()))
    note: str = ""
    #: {échéance: (n, gagnés, n récents, gagnés récents)} sur les MÊMES
    #: signaux ; « récents » = la période de validation.
    echeances: Mapping[int, tuple[int, int, int, int]] = field(
        default_factory=dict)

    # --- ce que la course demande ------------------------------------------

    def ecarte(self, ctx: Mapping[str, float]) -> Regle | None:
        """La leçon qui écarte ce contexte, ou `None`."""
        for regle in self.regles:
            if regle.tranche.contient(ctx):
                return regle
        return None

    def defavorables(self, ctx: Mapping[str, float]) -> list[StatTranche]:
        """Les tranches de ce contexte qui perdent sur l'historique."""
        return [s for s in self.statistiques
                if s.n >= N_MIN_AUTOPSIE and s.taux < SEUIL
                and s.tranche.contient(ctx)]

    # --- ce que l'opérateur lit --------------------------------------------

    def resume(self) -> str:
        if not self.n:
            return ("🧠 <b>Apprentissage</b> — pas encore d'exemples. "
                    + (self.note or "Premier apprentissage en cours."))
        jour = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime(
            "%d/%m %H:%M")
        lignes = [
            f"🧠 <b>Apprentissage</b> — {self.n} signaux rejoués du "
            f"{jour(self.debut_sec)} au {jour(self.fin_sec)} UTC, "
            f"{self.taux:.1%} gagnants (seuil {SEUIL:.1%}).",
            f"appris le {jour(self.cree_ts)} UTC",
        ]
        if self.note:
            lignes.append(self.note)
        if self.regles:
            lignes.append(f"\n<b>Leçons ACTIVES ({len(self.regles)})</b> — "
                          f"ces contextes sont écartés :")
            lignes += [f"• {r.texte()}" for r in self.regles]
            lignes.append(
                f"\nEffet sur la période de validation ({self.n_validation} "
                f"signaux jamais vus à l'étalonnage) : "
                f"{self.taux_validation_avant:.1%} → "
                f"<b>{self.taux_validation_apres:.1%}</b>, "
                f"{self.part_ecartee:.0%} des signaux écartés.")
        else:
            detectable = taux_detectable(int(self.n * (1 - PART_VALIDATION)))
            lignes.append(
                "\nAucune leçon active : aucun contexte ne perd de façon "
                "établie ET confirmée.")
            if detectable is not None:
                # ⚠ « Rien de prouvé » n'est pas « rien de lié ». Le message
                # disait « c'est ce qu'il a mesuré » sur 276 signaux, où seul
                # un contexte perdant deux fois sur trois pouvait être vu.
                lignes.append(
                    f"⚠ Ce n'est PAS la preuve que les pertes sont sans lien : "
                    f"avec {self.n} signaux, seul un contexte gagnant moins de "
                    f"{detectable:.0%} du temps peut être établi. Un contexte "
                    f"à 45 % — perdant, mais moins franchement — passe "
                    f"inaperçu jusqu'à ce que l'historique grossisse.")
        tableau = self.tableau()
        if tableau:
            lignes.append("\n<b>Avec ou contre le mouvement</b> (négatif = "
                          "contre le trade ; ⚠ = perdant prouvé à 95 %)")
            lignes.append(tableau)
        if self.candidates:
            lignes.append(f"\nÀ surveiller, non confirmées "
                          f"({len(self.candidates)}) :")
            lignes += [f"• {r.tranche.libelle()} — {r.taux:.0%} sur {r.n}, "
                       f"puis {r.taux_validation:.0%} sur {r.n_validation}"
                       for r in self.candidates[:3]]
        return "\n".join(lignes)

    def tableau(self, caracteristiques: Sequence[str] = TENDANCES) -> str:
        """Le taux de réussite par tiers de chaque caractéristique.

        Répond à « trader CONTRE le mouvement de l'heure en cours perd-il
        plus ? » avec les signaux rejoués, au lieu d'une règle posée sur deux
        captures d'écran — dans un sens comme dans l'autre.
        """
        lignes = []
        for c in caracteristiques:
            tiers = [st for st in self.statistiques
                     if st.tranche.caracteristique == c]
            if not tiers:
                continue
            lignes.append(f"<b>{LIBELLES.get(c, c)}</b>")
            for st in tiers:
                bas, haut = wilson(round(st.taux * st.n), st.n, 1.96)
                marque = " ⚠" if haut < SEUIL else ""
                lignes.append(f"  {_tiers(st.tranche)} : {st.taux:.0%} sur "
                              f"{st.n} (95 % : {bas:.0%}–{haut:.0%}){marque}")
        return "\n".join(lignes)

    def texte_echeances(self) -> str:
        """Les mêmes signaux rejoués, jugés à chaque échéance."""
        if not self.echeances:
            return "Rejeu : comparaison pas encore calculée."
        lignes = [f"<b>Rejeu</b> — mêmes signaux, {self.n} au total "
                  f"(dont la période récente) :"]
        for sec in sorted(self.echeances):
            n, g, nr, gr = self.echeances[sec]
            if not n:
                continue
            bas, haut = wilson(g, n, 1.96)
            recent = f", récents {gr / nr:.0%} sur {nr}" if nr else ""
            lignes.append(f"{sec // 60:>2} min : <b>{g / n:.1%}</b> sur {n} "
                          f"(95 % : {bas:.0%}–{haut:.0%}){recent}")
        return "\n".join(lignes)

    def autopsie(self, pas: Sequence[tuple[str, str, Mapping[str, float]]]
                 ) -> str:
        return autopsie(pas, self)

    # --- persistance --------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "regles": [r.to_dict() for r in self.regles],
            "candidates": [r.to_dict() for r in self.candidates],
            "stats": [s.to_dict() for s in self.statistiques],
            "n": self.n, "taux": self.taux, "debut": self.debut_sec,
            "fin": self.fin_sec, "n_val": self.n_validation,
            "avant": self.taux_validation_avant,
            "apres": self.taux_validation_apres,
            "part": self.part_ecartee, "cree": self.cree_ts, "note": self.note,
            "echeances": {str(k): list(v) for k, v in self.echeances.items()},
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> "Apprentissage":
        return cls(
            regles=tuple(Regle.from_dict(x) for x in d.get("regles", ())),
            candidates=tuple(Regle.from_dict(x)
                             for x in d.get("candidates", ())),
            statistiques=tuple(StatTranche.from_dict(x)
                               for x in d.get("stats", ())),
            n=int(d.get("n", 0)), taux=d.get("taux"),
            debut_sec=int(d.get("debut", 0)), fin_sec=int(d.get("fin", 0)),
            n_validation=int(d.get("n_val", 0)),
            taux_validation_avant=d.get("avant"),
            taux_validation_apres=d.get("apres"),
            part_ecartee=float(d.get("part", 0.0)),
            cree_ts=int(d.get("cree", 0)), note=str(d.get("note", "")),
            echeances={int(k): tuple(int(x) for x in v)
                       for k, v in (d.get("echeances") or {}).items()})


def _statistiques(exemples: Sequence[Exemple]) -> tuple[StatTranche, ...]:
    """Terciles de chaque caractéristique, sur tous les exemples."""
    sortie = []
    for c in CARACTERISTIQUES:
        valeurs = [e.contexte[c] for e in exemples if c in e.contexte]
        for t in _tranches(c, valeurs, coupes=(1 / 3, 2 / 3)):
            sous = [e for e in exemples if t.contient(e.contexte)]
            if sous:
                sortie.append(StatTranche(t, len(sous), _taux(sous)))
    return tuple(sortie)


def _comparer_echeances(tries: Sequence[Exemple]
                        ) -> dict[int, tuple[int, int, int, int]]:
    coupure = int(len(tries) * (1 - PART_VALIDATION))
    sortie = {}
    for sec in sorted({s for e in tries for s in e.issues}):
        tous = [e.issues[sec] for e in tries if sec in e.issues]
        recents = [e.issues[sec] for e in tries[coupure:] if sec in e.issues]
        sortie[sec] = (len(tous), sum(tous), len(recents), sum(recents))
    return sortie


def apprendre(exemples: Sequence[Exemple]) -> Apprentissage:
    """Les leçons que ces exemples PROUVENT. Voir le protocole en tête."""
    tries = sorted(exemples, key=lambda e: e.ts_sec)
    if not tries:
        return Apprentissage(note="Aucun signal rejoué.")
    base = dict(n=len(tries), taux=_taux(tries), debut_sec=tries[0].ts_sec,
                fin_sec=tries[-1].ts_sec, statistiques=_statistiques(tries),
                echeances=_comparer_echeances(tries))
    if len(tries) < N_MIN_EXEMPLES:
        return Apprentissage(**base, note=(
            f"Seulement {len(tries)} signaux : il en faut {N_MIN_EXEMPLES} "
            f"avant de tirer la moindre leçon."))
    coupure = int(len(tries) * (1 - PART_VALIDATION))
    etalonnage, validation = tries[:coupure], tries[coupure:]

    confirmees, candidates = [], []
    for c in CARACTERISTIQUES:
        valeurs = [e.contexte[c] for e in etalonnage if c in e.contexte]
        for t in _tranches(c, valeurs):
            sous = [e for e in etalonnage if t.contient(e.contexte)]
            gains = sum(e.gagne for e in sous)
            if len(sous) < N_MIN_ETALONNAGE or \
                    wilson(gains, len(sous))[1] >= SEUIL:
                continue
            sous_v = [e for e in validation if t.contient(e.contexte)]
            regle = Regle(t, len(sous), gains / len(sous), len(sous_v),
                          _taux(sous_v) if sous_v else float("nan"))
            if len(sous_v) >= N_MIN_VALIDATION and \
                    regle.taux_validation < SEUIL:
                confirmees.append(regle)
            else:
                candidates.append(regle)

    # Les plus coûteuses d'abord : ce qu'une leçon épargne, c'est son
    # effectif fois son écart au seuil.
    confirmees.sort(key=lambda r: r.n * (SEUIL - r.taux), reverse=True)
    retenues: list[Regle] = []
    avant = _taux(validation)
    courant = avant
    for regle in confirmees:
        essai = retenues + [regle]
        couvre = lambda e: any(r.tranche.contient(e.contexte) for r in essai)
        if sum(map(couvre, tries)) / len(tries) > COUVERTURE_MAX:
            continue
        gardes = [e for e in validation if not couvre(e)]
        taux = _taux(gardes)
        if taux is None or taux <= courant:
            continue                  # n'améliore pas hors échantillon
        retenues.append(regle)
        courant = taux
    ecartes = [e for e in validation
               if any(r.tranche.contient(e.contexte) for r in retenues)]
    return Apprentissage(
        **base, regles=tuple(retenues),
        candidates=tuple(sorted(candidates, key=lambda r: r.taux)),
        n_validation=len(validation), taux_validation_avant=avant,
        taux_validation_apres=courant,
        part_ecartee=len(ecartes) / len(validation))


def autopsie(pas: Sequence[tuple[str, str, Mapping[str, float]]],
             apprentissage: Apprentissage | None) -> str:
    """Ce qu'une session perdue dit — et ce qu'elle ne dit pas.

    `pas` : (paire, sens, contexte) de chaque pas perdu, dans l'ordre.
    """
    lignes = [f"🧠 <b>Autopsie</b> — session perdue en {len(pas)} pas"]
    communs: dict[str, int] = {}
    couverts = 0
    for i, (paire, sens, ctx) in enumerate(pas, start=1):
        if not ctx:
            lignes.append(f"Pas {i} {paire} {sens.upper()} : contexte non "
                          f"enregistré.")
            continue
        mauvais = apprentissage.defavorables(ctx) if apprentissage else []
        if apprentissage and apprentissage.ecarte(ctx):
            couverts += 1
        for s in mauvais:
            communs[s.tranche.caracteristique] = \
                communs.get(s.tranche.caracteristique, 0) + 1
        detail = ("; ".join(f"{s.tranche.libelle()} ({s.taux:.0%} sur "
                            f"{s.n})" for s in mauvais)
                  or "aucun contexte connu pour perdre")
        # Les mouvements BRUTS, que l'opérateur compare à son graphique :
        # négatif = contre le trade.
        valeurs = ", ".join(
            f"{nom} {ctx[c]:+.1f}" for c, nom in (
                ("mouvement_heure", "heure en cours"), ("elan_30m", "30 min"),
                ("tendance_h1", "3 h closes")) if c in ctx)
        lignes.append(f"Pas {i} {paire} {sens.upper()} : {detail}"
                      + (f"\n   mouvements (amplitudes, − = contre) : "
                         f"{valeurs}" if valeurs else ""))

    p = (apprentissage.taux if apprentissage and apprentissage.taux
         else TAUX_DE_REFERENCE)
    hasard = (1 - p) ** len(pas)
    recurrents = [c for c, k in communs.items() if k >= 2]
    if couverts:
        lignes.append(
            f"\n⚠ Verdict : {couverts} pas relevaient d'une leçon ACTIVE — "
            f"ces contextes sont écartés désormais.")
    elif recurrents:
        noms = ", ".join(LIBELLES.get(c, c) for c in recurrents)
        lignes.append(
            f"\nVerdict : point commun défavorable — {noms}. Noté ; il ne "
            f"deviendra une leçon que s'il se CONFIRME sur des signaux que "
            f"l'étalonnage n'a pas vus (réapprentissage quotidien). Une règle "
            f"tirée d'une seule session serait du hasard habillé en leçon.")
    else:
        lignes.append(
            f"\nVerdict : aucune cause établie — c'est la variance. À "
            f"{p:.0%} par pas, {len(pas)} pertes d'affilée arrivent "
            f"{hasard:.1%} du temps (environ 1 session sur "
            f"{round(1 / hasard) if hasard else '∞'}).")
    return "\n".join(lignes)
