"""
Les hypothèses pré-inscrites pour le test aveugle du protocole OTC.

--- D'où elles viennent ----------------------------------------------------

Le verdict du 2026-10-13, après lecture de /protocole, /experiences, /audit
et /stabilite : l'avantage de septembre ne tient pas en octobre, les pertes
viennent surtout de zones qui cassent (81 %), et la faille la plus
corrigible est l'entrée tardive. Deux pistes ont été retenues pour la
fenêtre aveugle, et seulement elles :

- **P2** — les deux filtres ✅ de l'entrée tardive, C (part du chemin déjà
  parcourue) et D (place jusqu'au prochain sommet/creux), et leur réunion E
  (60 % récents / 66 % anciens, mais 20 signaux récents seulement) ;
- **P4** — le retournement après une longue série rouge
  (`strategies.serie_rouge`).

--- Pourquoi dans le code ---------------------------------------------------

Le registre (`research.registre`) est une base locale, `research.db`. La
même liste est écrite ici, dans le dépôt : le commit qui l'introduit DATE la
pré-inscription — le 13/10, avant l'ouverture de la fenêtre aveugle le 14/10
à 0 h UTC — et personne ne peut la réécrire sans que l'historique le montre.
`outils/preinscrire.py` la recopie dans le registre local, et /protocole
la juge chaque jour sur la fenêtre aveugle.

⚠ Les seuils sont ceux lus le 13/10 (coupes des terciles, à deux
décimales). Ils ne se recalculent pas : une autre valeur est une autre
hypothèse, à pré-inscrire à son tour — et à compter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from maxprofit.apprentissage.lecons import Exemple
from maxprofit.strategies.serie_rouge import PARAMETRES_PRE_INSCRITS

DECLAREES_LE = "2026-10-13"
#: Les noms partent en HTML vers Telegram : ni « < », ni « > », ni « & ».
#: La fenêtre aveugle telle qu'elle a été pré-inscrite : du gel par défaut
#: du protocole, pour quatorze jours.
FENETRE_DEBUT = "2026-10-14"
FENETRE_JOURS = 14
SEUIL_RENTABLE = 1 / 1.92

#: Les filtres de P2 : (clé du contexte À L'ENTRÉE, comparaison, seuil).
#: C garde le tiers bas de la part parcourue (« moins de +0,46 ») ; D garde
#: le tiers haut de la place restante (« +1,95 et plus », champ libre
#: compris).
FILTRE_C = ("part_parcourue", "<", 0.46)
FILTRE_D = ("espace_obstacle", ">=", 1.95)


@dataclass(frozen=True)
class PreInscription:
    ident: str
    famille: str
    nom: str
    hypothese: str
    univers: str
    echeance_sec: int
    #: Observations minimales dans la fenêtre pour trancher.
    n_min: int
    critere: str
    parametres: Mapping[str, Any] = field(default_factory=dict)


PREINSCRIPTIONS: tuple[PreInscription, ...] = (
    PreInscription(
        ident="P2-E", famille="entree_tardive",
        nom="P2-E filtre C et D réunis sur la référence 8",
        hypothese=(
            "Sur les entrées ZoneH1 confirmées jouées (référence 8, règle "
            "de l'heure en cours), celles dont la part du chemin zone → "
            "prochain sommet/creux déjà parcourue est inférieure à 0,46 ET "
            "dont la place jusqu'au prochain sommet/creux est d'au moins "
            "1,95 amplitude gagnent à 15 min plus souvent que la référence, "
            "et plus de 52,1 % du temps, sur la fenêtre aveugle."),
        univers="ZoneH1 + confirmation M1, paires de la course",
        echeance_sec=900, n_min=30,
        critere=("taux du sous-ensemble > taux de la référence ET > 52,1 % ; "
                 "établi si la borne basse par blocs dépasse 52,1 % ; "
                 "indécis sous 30 entrées"),
        parametres={"filtres": [FILTRE_C, FILTRE_D], "reunion": "et"}),
    PreInscription(
        ident="P2-C", famille="entree_tardive",
        nom="P2-C part du chemin parcourue sous 0,46",
        hypothese=(
            "Sur les entrées ZoneH1 confirmées jouées, celles dont la part du "
            "chemin zone → prochain sommet/creux déjà parcourue est "
            "inférieure à 0,46 gagnent à 15 min plus souvent que la "
            "référence, et plus de 52,1 % du temps, sur la fenêtre aveugle."),
        univers="ZoneH1 + confirmation M1, paires de la course",
        echeance_sec=900, n_min=30,
        critere=("taux du sous-ensemble > taux de la référence ET > 52,1 % ; "
                 "établi si la borne basse par blocs dépasse 52,1 % ; "
                 "indécis sous 30 entrées"),
        parametres={"filtres": [FILTRE_C], "reunion": "et"}),
    PreInscription(
        ident="P2-D", famille="entree_tardive",
        nom="P2-D place jusqu'au prochain sommet/creux d'au moins 1,95",
        hypothese=(
            "Sur les entrées ZoneH1 confirmées jouées, celles dont la place "
            "jusqu'au prochain sommet/creux confirmé est d'au moins 1,95 "
            "amplitude (champ libre compris) gagnent à 15 min plus souvent "
            "que la référence, et plus de 52,1 % du temps, sur la fenêtre "
            "aveugle."),
        univers="ZoneH1 + confirmation M1, paires de la course",
        echeance_sec=900, n_min=30,
        critere=("taux du sous-ensemble > taux de la référence ET > 52,1 % ; "
                 "établi si la borne basse par blocs dépasse 52,1 % ; "
                 "indécis sous 30 entrées"),
        parametres={"filtres": [FILTRE_D], "reunion": "et"}),
    PreInscription(
        ident="P4", famille="serie_bougies",
        nom="P4 retournement après 8 rouges, CALL 2 min",
        hypothese=(
            f"Après au moins {PARAMETRES_PRE_INSCRITS.n_min} bougies M1 "
            f"rouges consécutives (minutes contiguës, sans doji), un CALL "
            f"pris à la clôture de chaque rouge de la série à partir de la "
            f"{PARAMETRES_PRE_INSCRITS.n_min}e gagne à "
            f"{PARAMETRES_PRE_INSCRITS.expiry_sec // 60} min plus de 52,1 % "
            f"du temps (attendu : 55,4 %), sur la fenêtre aveugle, toutes "
            f"paires rejouées."),
        univers="toutes les paires rejouées",
        echeance_sec=PARAMETRES_PRE_INSCRITS.expiry_sec, n_min=100,
        critere=("taux > 52,1 % ; établi si la borne basse par blocs "
                 "paire × jour dépasse 52,1 % ; indécis sous 100 signaux"),
        parametres={"strategie": "serie_rouge",
                    "n_min": PARAMETRES_PRE_INSCRITS.n_min,
                    "expiry_sec": PARAMETRES_PRE_INSCRITS.expiry_sec}),
)


def fenetre_pre_inscrite() -> tuple[int, int]:
    """[début, fin) de la fenêtre aveugle pré-inscrite, en secondes UTC."""
    debut = int(datetime.strptime(FENETRE_DEBUT, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp())
    return debut, debut + FENETRE_JOURS * 86400


def _passe(valeur: float, comparaison: str, seuil: float) -> bool:
    return valeur < seuil if comparaison == "<" else valeur >= seuil


def retenue(e: Exemple, filtres: Sequence[Sequence[Any]]) -> bool:
    """L'entrée passe-t-elle TOUS les filtres ? Une valeur manquante ne
    passe pas : on ne devine pas ce qu'on n'a pas mesuré."""
    for cle, comparaison, seuil in filtres:
        if cle not in e.contexte or not _passe(
                float(e.contexte[cle]), comparaison, float(seuil)):
            return False
    return True


def juger(n: int, g: int, ic, n_min: int,
          reference: Sequence[int] | None = None) -> str:
    """Le verdict d'une pré-inscription sur la fenêtre aveugle, selon son
    critère écrit d'avance."""
    if n < n_min:
        return f"indécise — {n} observations, il en faut {n_min}"
    taux = g / n
    if taux <= SEUIL_RENTABLE:
        return "❌ réfutée : pas au-dessus de 52,1 %"
    if reference is not None and reference[0] and \
            taux <= reference[1] / reference[0]:
        return "❌ réfutée : pas mieux que la référence"
    if ic and ic[0] > SEUIL_RENTABLE:
        return "✅ établie : borne basse au-dessus de 52,1 %"
    return "≈ compatible, non établie (intervalle sous 52,1 %)"
