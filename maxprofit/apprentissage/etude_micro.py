"""
Étape 3 — le protocole statistique (docs/etape3_plan_execution.md, §2 et
§5 à §10). Pur : aucune base, aucune horloge, aucun aléa non semé.

--- La règle d'issue, unique --------------------------------------------------

Le signal porte sur la bougie M (début `m`, en secondes), décision d = m + 60.
L'issue à h secondes est la clôture de la bougie COMPLÈTE qui commence à
m + h : elle couvre [d + h − 60, d + h) et clôt donc à d + h. Le prix
d'entrée est la clôture de M. C'est la règle de `historique` ; un test
vérifie que les deux rendent la même issue sur chaque signal.
- Bougie de sortie absente ou incomplète : MANQUANTE (aucune voisine ne la
  remplace).
- Prix égal : EGALITE, rapportée à part ; en sensibilité, comptée perdue.
- Deux bougies pour la même minute sont impossibles : la clé (paire, tf,
  début) les interdit.

--- Ce qu'une comparaison a le droit de comparer --------------------------------

La référence et le modèle enrichi sont jugés sur les MÊMES signaux : une
ligne porte les deux prédictions, et une ligne incomplète est refusée, pas
écartée. Une variable manquante est imputée (médiane de l'exploration) avec
une indicatrice ; elle n'exclut jamais un signal d'un seul des deux modèles.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Any, Callable, Hashable, Mapping, Sequence

GAIN, PERTE, EGALITE, MANQUANTE = "gain", "perte", "egalite", "manquante"
HORIZON_SEC = 900
#: Une issue n'est lue qu'une heure après son échéance : la bougie de sortie
#: d'une paire calme ne s'écrit qu'au tick suivant. Une période n'est figée
#: qu'une fois toutes ses issues mûres.
MARGE_MATURITE_SEC = 3600
HORIZONS_SECONDAIRES = (240, 600)

#: Le découpage, fixé avant de lire une issue (plan §6). La confirmation
#: s'ouvre au premier minuit UTC qui suit le gel formel ; ce qui tombe entre
#: la fin de la validation et cette ouverture est en QUARANTAINE : ni choisi,
#: ni confirmé.
FIN_EXPLORATION = int(datetime(2026, 10, 21, tzinfo=timezone.utc).timestamp())
FIN_VALIDATION = int(datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp())
EXPLORATION, VALIDATION, QUARANTAINE, CONFIRMATION = (
    "exploration", "validation", "quarantaine", "confirmation")

#: La référence sans microstructure : connue à la décision, figée (plan §5).
REFERENCE_NUMERIQUES: tuple[str, ...] = (
    "sens_call", "touches", "entrees_deja_offertes", "distance_niveau",
    "elan_15m", "volatilite_relative", "mouvement_heure", "depuis_contact",
    "alternances_10", "payout")
CRENEAU_H = 4
LAMBDA_L2 = 1.0

#: Verdicts (plan §9).
CONFIRMEE, REJETEE, INDETERMINEE = "confirmée", "rejetée", "indéterminée"
EFFET_PRATIQUE = 0.02
SIGNAUX_MIN = 500
ALPHA_FAMILLE = 0.05
#: Deux hypothèses principales au plus (décision du 27/10), choisies et
#: pré-inscrites AVANT tout examen de leur lien avec les issues.
HYPOTHESES_MAX = 2
BH_Q = 0.10
TIRAGES = 1000
GRAINE = 7
#: Les regards de la confirmation, en signaux de niveau 2 : la règle de
#: poursuite est fixée avant le premier (plan §7 bis). Plafond de 4 000
#: (décision du 27/10) : un plafond, pas une garantie de puissance.
REGARDS: tuple[int, ...] = (500, 1000, 2000, 4000)

#: Le PREMIER GEL (hypothèses H1 et H2 et règles communes) : l'empreinte de
#: `docs/etape3_preinscription.json`, notée le 29/10 avant toute consultation
#: des résultats prédictifs. Un test refuse toute modification du document.
EMPREINTE_GEL_HYPOTHESES = (
    "3e415576fd083a944f62a5051bc6f04df4e076fef98fe64a9899674b93a7a83e")

#: La règle de retrait des variables quasi constantes (plan §4 bis), fixée
#: avant toute lecture d'issue et appliquée sur l'EXPLORATION seule.
VERSION_RETRAIT = 1
PART_RARE_MIN = 0.05
N_RARE_MIN = 30
#: En ticks bruts, ces deux variables mesurent le doublement du flux (un
#: artefact de collecte) : jamais prédictives, diagnostic seulement.
ARTEFACTS_BRUTS: tuple[str, ...] = ("B3_nulles", "B6_concentration")

#: Les variantes de sensibilité de CHAQUE hypothèse principale : comptées
#: au registre, et une conclusion qui change de signe entre elles est
#: « fragile ».
SENSIBILITES: tuple[tuple[str, tuple[Any, ...]], ...] = (
    ("traitement", ("distincts", "bruts")),
    ("k", (0, 2, 5)),
    ("blocs", ("paire_jour", "jour")),
    ("egalites", ("exclues", "perdues")),
)


# --------------------------------------------------------------------------- #
# Issue et période
# --------------------------------------------------------------------------- #

def issue(closes_completes: Mapping[int, float], debut_signal_sec: int,
          call: bool, horizon_sec: int = HORIZON_SEC
          ) -> tuple[str, float | None]:
    """(issue, prix de sortie). `closes_completes` : début (s) → clôture des
    bougies COMPLÈTES de la paire."""
    if debut_signal_sec % 60 or horizon_sec % 60:
        raise ValueError("instants hors de la grille des minutes")
    entree = closes_completes.get(debut_signal_sec)
    if entree is None:
        raise ValueError("bougie du signal absente ou incomplète")
    sortie = closes_completes.get(debut_signal_sec + horizon_sec)
    if sortie is None:
        return MANQUANTE, None
    if sortie == entree:
        return EGALITE, sortie
    return (GAIN if (sortie > entree) == call else PERTE), sortie


def periode(decision_sec: int, gel_sec: int | None = None) -> str:
    """La période d'un signal. Sans gel enregistré, rien n'est en
    confirmation : tout ce qui suit la validation reste en quarantaine."""
    if decision_sec < FIN_EXPLORATION:
        return EXPLORATION
    if decision_sec < FIN_VALIDATION:
        return VALIDATION
    if gel_sec is None or decision_sec < ouverture_confirmation(gel_sec):
        return QUARANTAINE
    return CONFIRMATION


def ouverture_confirmation(gel_sec: int) -> int:
    """Le premier minuit UTC strictement après le gel, et jamais avant la
    fin de la validation."""
    return max(FIN_VALIDATION, (int(gel_sec) // 86400 + 1) * 86400)


def issue_mure(decision_sec: int, maintenant: float,
               horizon_sec: int = HORIZON_SEC) -> bool:
    """L'issue est-elle lisible sans risque d'être encore incomplète ?"""
    return maintenant >= decision_sec + horizon_sec + MARGE_MATURITE_SEC


def periode_figeable(fin_sec: int, maintenant: float,
                     horizon_sec: int = HORIZON_SEC) -> bool:
    """Une période [·, fin_sec[ ne se fige qu'une fois toutes ses issues
    mûres : son dernier signal peut décider juste avant `fin_sec`."""
    return issue_mure(fin_sec, maintenant, horizon_sec)


def y_selon(issue_: str, egalites: str = "exclues") -> int | None:
    """1 / 0 / None (signal hors de la métrique). Une issue manquante est
    toujours hors de la métrique."""
    if issue_ == GAIN:
        return 1
    if issue_ == PERTE:
        return 0
    if issue_ == EGALITE and egalites == "perdues":
        return 0
    return None


# --------------------------------------------------------------------------- #
# Préparation des variables : figée sur l'exploration
# --------------------------------------------------------------------------- #

@dataclass
class Preparation:
    """Imputation (médiane) et standardisation, calculées UNE fois sur
    l'exploration puis appliquées telles quelles."""

    numeriques: tuple[str, ...]
    categories: dict[str, tuple[str, ...]] = field(default_factory=dict)
    medianes: dict[str, float] = field(default_factory=dict)
    moyennes: dict[str, float] = field(default_factory=dict)
    ecarts: dict[str, float] = field(default_factory=dict)

    @classmethod
    def ajuster(cls, lignes: Sequence[Mapping[str, Any]],
                numeriques: Sequence[str],
                categories: Sequence[str] = ()) -> "Preparation":
        prep = cls(tuple(numeriques))
        for nom in numeriques:
            valeurs = sorted(float(r[nom]) for r in lignes
                             if r.get(nom) is not None)
            med = _mediane(valeurs)
            prep.medianes[nom] = 0.0 if med is None else med
            completes = [prep.medianes[nom] if r.get(nom) is None
                         else float(r[nom]) for r in lignes]
            moy = sum(completes) / len(completes) if completes else 0.0
            var = (sum((x - moy) ** 2 for x in completes) / len(completes)
                   if completes else 0.0)
            prep.moyennes[nom] = moy
            prep.ecarts[nom] = math.sqrt(var) or 1.0
        for nom in categories:
            # La première modalité est la référence (aucune colonne) :
            # sans cela, l'interception et les indicatrices seraient liées.
            prep.categories[nom] = tuple(sorted(
                {str(r.get(nom)) for r in lignes}))[1:]
        return prep

    def colonnes(self) -> list[str]:
        noms = []
        for nom in self.numeriques:
            noms += [nom, f"{nom}?manque"]
        for nom, modalites in self.categories.items():
            noms += [f"{nom}={m}" for m in modalites]
        return noms

    def vecteur(self, r: Mapping[str, Any]) -> list[float]:
        x = []
        for nom in self.numeriques:
            v = r.get(nom)
            manque = v is None
            v = self.medianes[nom] if manque else float(v)
            x += [(v - self.moyennes[nom]) / self.ecarts[nom], float(manque)]
        for nom, modalites in self.categories.items():
            x += [float(str(r.get(nom)) == m) for m in modalites]
        return x


def creneau(heure_utc: float) -> str:
    """Le créneau UTC de 4 h : « 0-4 », « 4-8 »…"""
    debut = int(heure_utc) // CRENEAU_H * CRENEAU_H
    return f"{debut}-{debut + CRENEAU_H}"


# --------------------------------------------------------------------------- #
# Régression logistique pénalisée, en Python pur
# --------------------------------------------------------------------------- #

def _sigmoide(u: float) -> float:
    if u >= 0:
        return 1 / (1 + math.exp(-u))
    e = math.exp(u)
    return e / (1 + e)


def _resoudre(a: list[list[float]], b: list[float]) -> list[float]:
    """Cholesky : `a` est symétrique définie positive (pénalité L2)."""
    n = len(b)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = a[i][j] - sum(low[i][k] * low[j][k] for k in range(j))
            if i == j:
                if s <= 0:
                    raise ValueError("système non défini positif")
                low[i][i] = math.sqrt(s)
            else:
                low[i][j] = s / low[j][j]
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(low[i][k] * y[k] for k in range(i))) / low[i][i]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (y[i] - sum(low[k][i] * x[k] for k in range(i + 1, n))) \
            / low[i][i]
    return x


@dataclass(frozen=True)
class Logistique:
    """β₀ + β·x ; l'interception n'est pas pénalisée."""

    coefficients: tuple[float, ...]

    @classmethod
    def ajuster(cls, x: Sequence[Sequence[float]], y: Sequence[int], *,
                lam: float = LAMBDA_L2, iterations: int = 50,
                tolerance: float = 1e-9) -> "Logistique":
        """Newton sur la log-vraisemblance pénalisée par λ/2·‖β‖²."""
        if len(x) != len(y) or not y:
            raise ValueError("données vides ou de tailles différentes")
        p = len(x[0]) + 1
        lignes = [[1.0, *xi] for xi in x]
        beta = [0.0] * p
        for _ in range(iterations):
            grad = [0.0] * p
            hess = [[0.0] * p for _ in range(p)]
            for xi, yi in zip(lignes, y):
                mu = _sigmoide(sum(b * v for b, v in zip(beta, xi)))
                w = mu * (1 - mu)
                r = yi - mu
                for j in range(p):
                    grad[j] += r * xi[j]
                    wj = w * xi[j]
                    row = hess[j]
                    for k in range(j + 1):
                        row[k] += wj * xi[k]
            for j in range(p):
                for k in range(j):
                    hess[k][j] = hess[j][k]
            for j in range(1, p):
                grad[j] -= lam * beta[j]
                hess[j][j] += lam
            hess[0][0] += 1e-9
            pas = _resoudre(hess, grad)
            beta = [b + d for b, d in zip(beta, pas)]
            if max(abs(d) for d in pas) < tolerance:
                break
        return cls(tuple(beta))

    def proba(self, x: Sequence[float]) -> float:
        b = self.coefficients
        return _sigmoide(b[0] + sum(c * v for c, v in zip(b[1:], x)))


def log_perte(p: float, y: int, eps: float = 1e-12) -> float:
    p = min(max(p, eps), 1 - eps)
    return -math.log(p) if y else -math.log(1 - p)


@dataclass(frozen=True)
class LigneComparee:
    """Un signal jugé par les deux modèles : c'est ce qui garantit que la
    comparaison porte sur les mêmes signaux."""

    ident: str
    bloc: Hashable
    jour: int
    y: int
    p_ref: float
    p_mod: float

    def __post_init__(self) -> None:
        if self.y not in (0, 1):
            raise ValueError(f"{self.ident} : issue hors de la métrique")
        for p in (self.p_ref, self.p_mod):
            if not (isinstance(p, float) and 0.0 <= p <= 1.0):
                raise ValueError(f"{self.ident} : prédiction invalide {p!r}")

    @property
    def gain_log(self) -> float:
        """Δℓ = ℓ_réf − ℓ_mod, en nats : positif quand le modèle fait mieux."""
        return log_perte(self.p_ref, self.y) - log_perte(self.p_mod, self.y)


def comparer(lignes: Sequence[LigneComparee]) -> dict[str, float]:
    """Δℓ moyen, Brier des deux modèles, sur des lignes uniques."""
    idents = [r.ident for r in lignes]
    if len(set(idents)) != len(idents):
        raise ValueError("un signal compté deux fois")
    if not lignes:
        raise ValueError("aucun signal")
    n = len(lignes)
    return {
        "n": n,
        "gain_log": sum(r.gain_log for r in lignes) / n,
        "brier_ref": sum((r.p_ref - r.y) ** 2 for r in lignes) / n,
        "brier_mod": sum((r.p_mod - r.y) ** 2 for r in lignes) / n,
    }


# --------------------------------------------------------------------------- #
# Incertitude : bootstrap par blocs
# --------------------------------------------------------------------------- #

def bootstrap_par_blocs(par_bloc: Mapping[Hashable, Sequence[float]], *,
                        tirages: int = TIRAGES, graine: int = GRAINE,
                        niveau: float = 0.95) -> dict[str, float | int]:
    """Moyenne par signal, ré-échantillonnée par BLOCS entiers, avec remise.

    Les blocs sont ceux qui ont au moins un signal admissible : une paire ×
    jour sans signal admissible n'existe pas comme bloc, elle n'a aucun poids
    (on conditionne sur l'ensemble observé ; le nombre de blocs est publié).
    p unilatérale = (1 + #{tirages ≤ 0}) / (tirages + 1) : un regard dont le
    seuil nominal est sous 1 / (tirages + 1) ne peut pas confirmer."""
    blocs = [list(v) for _, v in sorted(par_bloc.items(),
                                         key=lambda kv: repr(kv[0])) if v]
    if not blocs:
        raise ValueError("aucun bloc")
    sommes = [sum(b) for b in blocs]
    tailles = [len(b) for b in blocs]
    estimation = sum(sommes) / sum(tailles)
    alea = random.Random(graine)
    k = len(blocs)
    moyennes = []
    for _ in range(tirages):
        s = n = 0
        for _ in range(k):
            i = alea.randrange(k)
            s += sommes[i]
            n += tailles[i]
        moyennes.append(s / n)
    moyennes.sort()
    bas = moyennes[int(math.floor((1 - niveau) / 2 * tirages))]
    haut = moyennes[min(tirages - 1,
                        int(math.ceil((1 + niveau) / 2 * tirages)) - 1)]
    return {"estimation": estimation, "bas": bas, "haut": haut,
            "p_unilaterale": (1 + sum(1 for m in moyennes if m <= 0))
            / (tirages + 1),
            "blocs": k, "signaux": sum(tailles)}


def effet_tercile(lignes: Sequence[Mapping[str, Any]], variable: str,
                  bornes: tuple[float, float], tercile: int, *,
                  bloc: Callable[[Mapping[str, Any]], Hashable],
                  sens: int = 1, tirages: int = TIRAGES,
                  graine: int = GRAINE) -> dict[str, float | int]:
    """`sens` × (taux de gain du tercile pré-inscrit − taux des deux
    autres), avec un intervalle par blocs : positif dans le sens pré-inscrit
    (`sens` = −1 pour une hypothèse « moins de gains »). `bornes` : les
    terciles de l'EXPLORATION, figés ; `tercile` ∈ {0, 1, 2}. Une variable
    manquante tombe dans le tercile de la médiane d'exploration, comme dans
    le modèle."""
    if sens not in (1, -1):
        raise ValueError("sens : +1 ou −1")
    def classe(v: float) -> int:
        return 0 if v < bornes[0] else (1 if v < bornes[1] else 2)

    med = (bornes[0] + bornes[1]) / 2
    groupes: dict[Hashable, list[tuple[int, int]]] = {}
    for r in lignes:
        v = r.get(variable)
        dedans = classe(med if v is None else float(v)) == tercile
        groupes.setdefault(bloc(r), []).append((int(dedans), int(r["y"])))

    def effet(blocs: Sequence[Sequence[tuple[int, int]]]) -> float | None:
        a = [y for b in blocs for d, y in b if d]
        c = [y for b in blocs for d, y in b if not d]
        if not a or not c:
            return None
        return sens * (sum(a) / len(a) - sum(c) / len(c))

    ordonnes = [g for _, g in sorted(groupes.items(),
                                      key=lambda kv: repr(kv[0]))]
    estimation = effet(ordonnes)
    alea = random.Random(graine)
    tirs = []
    for _ in range(tirages):
        e = effet([ordonnes[alea.randrange(len(ordonnes))]
                   for _ in ordonnes])
        if e is not None:
            tirs.append(e)
    tirs.sort()
    if estimation is None or not tirs:
        return {"estimation": None, "bas": None, "haut": None,
                "blocs": len(ordonnes)}
    return {"estimation": estimation,
            "bas": tirs[int(0.025 * len(tirs))],
            "haut": tirs[min(len(tirs) - 1, int(math.ceil(0.975 * len(tirs)))
                             - 1)],
            "blocs": len(ordonnes)}


def retrait_des_quasi_constantes(
        valeurs: Mapping[str, Sequence[float | None]], *,
        part_min: float = PART_RARE_MIN, n_min: int = N_RARE_MIN
        ) -> list[dict[str, Any]]:
    """La trace de la règle de retrait, une ligne par candidate
    (« variable@w ») : calculée sur l'EXPLORATION, sans aucune issue, puis
    gelée avec le registre.

    - écart interquartile > 0 : conservée ;
    - écart interquartile nul : l'IQR ne suffit pas à juger une variable
      rare (un événement binaire peu fréquent a un IQR nul). Elle est
      conservée si au moins `n_min` valeurs ET au moins `part_min` des
      valeurs diffèrent du mode ; retirée sinon ;
    - moins de `n_min` valeurs mesurées : retirée."""
    trace = []
    for candidate, brutes in valeurs.items():
        v = sorted(float(x) for x in brutes if x is not None)
        n = len(v)
        ligne: dict[str, Any] = {
            "candidate": candidate, "valeurs": n,
            "manquantes": len(brutes) - n, "distinctes": len(set(v)),
            "q1": None, "q3": None, "iqr": None, "mode": None,
            "hors_mode": None, "part_hors_mode": None,
            "version": VERSION_RETRAIT}
        if n < n_min:
            ligne.update(decision="retirée",
                         raison=f"moins de {n_min} valeurs mesurées")
            trace.append(ligne)
            continue
        q1, q3 = v[n // 4], v[(3 * n) // 4]
        comptes: dict[float, int] = {}
        for x in v:
            comptes[x] = comptes.get(x, 0) + 1
        mode = max(comptes, key=lambda x: (comptes[x], -x))
        hors = n - comptes[mode]
        ligne.update(q1=q1, q3=q3, iqr=q3 - q1, mode=mode, hors_mode=hors,
                     part_hors_mode=hors / n)
        if q3 > q1:
            ligne.update(decision="conservée", raison="IQR > 0")
        elif hors >= n_min and hors / n >= part_min:
            ligne.update(decision="conservée",
                         raison=f"IQR nul mais {hors} valeurs "
                                f"({100 * hors / n:.1f} %) hors du mode")
        else:
            ligne.update(decision="retirée",
                         raison=f"quasi constante : IQR nul, {hors} valeurs "
                                f"({100 * hors / n:.1f} %) hors du mode, "
                                f"seuils {n_min} et {100 * part_min:.0f} %")
        trace.append(ligne)
    return trace


def terciles(valeurs: Sequence[float | None]) -> tuple[float, float] | None:
    """Les bornes des terciles, sur l'exploration, valeurs manquantes
    exclues."""
    v = sorted(float(x) for x in valeurs if x is not None)
    if len(v) < 3:
        return None
    return v[len(v) // 3], v[2 * len(v) // 3]


# --------------------------------------------------------------------------- #
# Comparaisons multiples
# --------------------------------------------------------------------------- #

def holm(p: Mapping[str, float], alpha: float = ALPHA_FAMILLE
         ) -> dict[str, bool]:
    """Rejet de H0 par Holm, au seuil de famille `alpha`."""
    ordre = sorted(p, key=lambda k: p[k])
    m = len(ordre)
    rejet = {k: False for k in p}
    for i, k in enumerate(ordre):
        if p[k] <= alpha / (m - i):
            rejet[k] = True
        else:
            break
    return rejet


def benjamini_hochberg(p: Mapping[str, float], q: float = BH_Q
                       ) -> dict[str, bool]:
    """Découvertes au taux de fausses découvertes `q`."""
    ordre = sorted(p, key=lambda k: p[k])
    m = len(ordre)
    dernier = -1
    for i, k in enumerate(ordre):
        if p[k] <= q * (i + 1) / m:
            dernier = i
    return {k: i <= dernier for i, k in enumerate(ordre)}


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #

def verdict(*, rejet_holm: bool, gain_log: Mapping[str, float],
            tercile: Mapping[str, float | None], tiers_positifs: int,
            signes_sensibilite: Mapping[str, int], n_niveau_2: int
            ) -> tuple[str, list[str]]:
    """Le verdict d'une hypothèse principale à un regard, et ses motifs.

    `gain_log` / `tercile` : estimation, bas, haut ; `tiers_positifs` :
    tiers chronologiques de la confirmation où Δℓ > 0 ; `signes_sensibilite`
    : variante → signe de Δℓ (+1, 0, −1)."""
    motifs: list[str] = []
    fragile = len(set(signes_sensibilite.values())) > 1
    if n_niveau_2 < SIGNAUX_MIN:
        motifs.append(f"moins de {SIGNAUX_MIN} signaux de niveau 2")
    if fragile:
        motifs.append("fragile : le signe change selon la variante")
    if motifs:
        return INDETERMINEE, motifs
    est = tercile.get("estimation")
    if (rejet_holm and est is not None and est >= EFFET_PRATIQUE
            and tiers_positifs >= 2):
        return CONFIRMEE, ["Holm, effet pratique et stabilité réunis"]
    haut_t = tercile.get("haut")
    if gain_log["haut"] <= 0:
        return REJETEE, ["borne haute de Δℓ ≤ 0"]
    if haut_t is not None and haut_t < EFFET_PRATIQUE:
        return REJETEE, [f"borne haute du tercile < +{100 * EFFET_PRATIQUE:.0f}"
                         f" points"]
    if not rejet_holm:
        motifs.append("Holm non atteint")
    if est is None or est < EFFET_PRATIQUE:
        motifs.append("effet pratique non atteint")
    if tiers_positifs < 2:
        motifs.append("instable entre les tiers")
    return INDETERMINEE, motifs


# --------------------------------------------------------------------------- #
# Puissance, et règle de poursuite
# --------------------------------------------------------------------------- #

_N = NormalDist()


def icc(blocs: Mapping[Hashable, Sequence[int]]) -> float:
    """Corrélation intra-bloc (estimateur ANOVA, tailles inégales), bornée
    à 0."""
    groupes = [list(v) for v in blocs.values() if v]
    k = len(groupes)
    n = sum(len(g) for g in groupes)
    if k < 2 or n <= k:
        return 0.0
    moyenne = sum(sum(g) for g in groupes) / n
    ssb = sum(len(g) * (sum(g) / len(g) - moyenne) ** 2 for g in groupes)
    ssw = sum(sum((y - sum(g) / len(g)) ** 2 for y in g) for g in groupes)
    msb, msw = ssb / (k - 1), ssw / (n - k)
    n0 = (n - sum(len(g) ** 2 for g in groupes) / n) / (k - 1)
    if msb + (n0 - 1) * msw <= 0:
        return 0.0
    return max(0.0, (msb - msw) / (msb + (n0 - 1) * msw))


def puissance(blocs: Mapping[Hashable, Sequence[int]], *,
              tailles: Sequence[int] = REGARDS,
              alpha: float = ALPHA_FAMILLE / HYPOTHESES_MAX,
              puissance_visee: float = 0.8, effet: float = EFFET_PRATIQUE,
              signaux_par_jour: float | None = None,
              part_niveau_2: float = 1.0) -> dict[str, Any]:
    """La précision attendue du contraste « tercile contre les deux
    autres », depuis les issues de l'EXPLORATION (aucune donnée de
    confirmation).

    Var ≈ p(1 − p)·(1/n₁ + 1/n₂)·DEFF, n₁ = N/3, n₂ = 2N/3, soit
    p(1 − p)·4,5/N·DEFF ; DEFF = 1 + (m − 1)·ρ, m la taille moyenne
    pondérée des blocs. `alpha` unilatéral au pas le plus sévère de Holm
    (0,05 / 4) ; l'effet minimal détectable vaut (z₁₋α + z_puissance)·σ."""
    tous = [y for v in blocs.values() for y in v]
    n = len(tous)
    if not n:
        return {"n": 0}
    p = sum(tous) / n
    rho = icc(blocs)
    m = sum(len(v) ** 2 for v in blocs.values()) / n
    deff = 1 + (m - 1) * rho
    za, zb = _N.inv_cdf(1 - alpha), _N.inv_cdf(puissance_visee)
    z95 = _N.inv_cdf(0.975)

    def sigma(taille: int) -> float:
        return math.sqrt(p * (1 - p) * 4.5 / taille * deff)

    def requis(z: float) -> int:
        return math.ceil((z + zb) ** 2 * p * (1 - p) * 4.5 * deff / effet ** 2)

    par_jour = (signaux_par_jour * part_niveau_2
                if signaux_par_jour else None)
    lignes = [{"N": t, "effet_min": (za + zb) * sigma(t),
               "demi_ic95": z95 * sigma(t),
               "jours": (t / par_jour if par_jour else None)}
              for t in tailles]
    n_conf, n_rejet = requis(za), requis(z95)
    return {"n": n, "blocs": len(blocs), "p": p, "icc": rho,
            "taille_bloc": m, "deff": deff, "alpha": alpha,
            "puissance": puissance_visee, "lignes": lignes,
            "n_pour_confirmer": n_conf, "n_pour_rejeter": n_rejet,
            "jours_pour_confirmer": (n_conf / par_jour if par_jour else None),
            "jours_pour_rejeter": (n_rejet / par_jour if par_jour else None)}


def depense_alpha(t: float, alpha: float = ALPHA_FAMILLE) -> float:
    """Dépense cumulée de type O'Brien-Fleming (Lan-DeMets) à la fraction
    d'information t ∈ ]0, 1]."""
    if t <= 0:
        return 0.0
    t = min(t, 1.0)
    return 2 * (1 - _N.cdf(_N.inv_cdf(1 - alpha / 2) / math.sqrt(t)))


def seuils_des_regards(regards: Sequence[int] = REGARDS,
                       alpha: float = ALPHA_FAMILLE) -> list[dict]:
    """Le seuil nominal de la famille à chaque regard : l'INCRÉMENT de
    dépense (borne de Bonferroni entre regards, conservatrice). Si la
    collecte s'arrête pour une raison étrangère aux résultats, le dernier
    regard reçoit tout le reste : alpha − dépense déjà faite."""
    n_max = regards[-1]
    sortie, deja = [], 0.0
    for n in regards:
        cumul = depense_alpha(n / n_max, alpha)
        sortie.append({"N": n, "cumul": cumul, "nominal": cumul - deja})
        deja = cumul
    return sortie


# --------------------------------------------------------------------------- #
# Registre et gel
# --------------------------------------------------------------------------- #

def empreinte(parametres: Mapping[str, Any]) -> str:
    """SHA-256 d'une pré-inscription : le gel est cette empreinte, notée au
    registre et dans le commit, AVANT l'ouverture de la confirmation."""
    texte = json.dumps(parametres, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), default=str)
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


def exiger_le_gel(parametres: Mapping[str, Any], empreinte_notee: str | None
                  ) -> None:
    """Refuse tout calcul de confirmation sans gel, ou sur des paramètres
    qui ne sont plus ceux du gel."""
    if not empreinte_notee:
        raise ValueError("aucun gel enregistré : la confirmation est fermée")
    if empreinte(parametres) != empreinte_notee:
        raise ValueError("paramètres différents de ceux du gel")


COLONNES_REGISTRE = ("n", "phase", "nature", "hypothese", "variable", "w",
                     "traitement", "k", "horizon", "modele", "statistique",
                     "valeur", "p", "statut", "note")
NATURES = ("controle", "exploratoire", "principale", "sensibilite")


@dataclass
class Registre:
    """Chaque essai, abandonné ou non, avec un numéro. Le nombre total
    d'essais est publié avec les résultats."""

    lignes: list[dict[str, Any]] = field(default_factory=list)

    def noter(self, **champs: Any) -> int:
        inconnus = set(champs) - set(COLONNES_REGISTRE)
        if inconnus:
            raise ValueError(f"champs inconnus : {sorted(inconnus)}")
        if champs.get("nature") not in NATURES:
            raise ValueError(f"nature inconnue : {champs.get('nature')}")
        ligne = {c: champs.get(c) for c in COLONNES_REGISTRE}
        ligne["n"] = len(self.lignes) + 1
        ligne["statut"] = ligne["statut"] or "calculé"
        self.lignes.append(ligne)
        return ligne["n"]

    def abandonner(self, n: int, note: str) -> None:
        """Un essai abandonné reste au registre, avec sa raison."""
        self.lignes[n - 1]["statut"] = "abandonné"
        self.lignes[n - 1]["note"] = note

    def compte(self, nature: str | None = None) -> int:
        return sum(1 for r in self.lignes
                   if nature is None or r["nature"] == nature)

    def csv(self) -> bytes:
        def champ(v: Any) -> str:
            return "" if v is None else str(v).replace(";", ",")
        texte = [";".join(COLONNES_REGISTRE)]
        texte += [";".join(champ(r[c]) for c in COLONNES_REGISTRE)
                  for r in self.lignes]
        return ("\n".join(texte) + "\n").encode("utf-8")


def variantes_de_sensibilite() -> list[dict[str, Any]]:
    """Toutes les variantes d'une hypothèse principale (2 × 3 × 2 × 2 =
    24), celle du calcul principal comprise (la première)."""
    variantes: list[dict[str, Any]] = [{}]
    for nom, valeurs in SENSIBILITES:
        variantes = [{**v, nom: x} for v in variantes for x in valeurs]
    return variantes


def _mediane(v: Sequence[float]) -> float | None:
    v = sorted(v)
    if not v:
        return None
    k = len(v) // 2
    return v[k] if len(v) % 2 else (v[k - 1] + v[k]) / 2
