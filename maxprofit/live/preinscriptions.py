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

--- Amendement du 14/10 (P2 seulement), avant toute lecture de la fenêtre --

Le protocole de validation d'EXP-020 C et D (2026-10-14) a précisé P2 :

1. **Les filtres sont des EXCLUSIONS**, comme dans /experiences qui les a
   produits : C écarte une entrée dont la part parcourue est d'au moins
   0,46 ; D écarte une entrée dont la place restante est inférieure à
   1,95. Une entrée SANS valeur de C (aucun sommet ou creux confirmé
   devant elle, ou pas de niveau) n'est pas écartée par C — la version du
   13/10 l'écartait, ce qui n'était pas la formule mesurée.
2. **Quatre groupes sur les mêmes signaux** (référence, C, D, C + D) et un
   verdict sur cinq critères écrits d'avance (`live.validation_p2`), au
   lieu d'un taux comparé au seuil : espérance positive, gain d'espérance
   sur la référence établi par une comparaison appariée par blocs, gain
   qui ne tient ni à une ou deux paires ni à une seule journée, volume
   restant acceptable.
3. **La fenêtre se ferme sur un VOLUME** calculé d'avance (gain minimal à
   détecter, puissance 80 %, corrélation des signaux), et non au bout de
   quatorze jours : 90 signaux de référence ne détecteraient qu'un écart
   énorme. Une revue de contrôle a lieu à 100 signaux, sans verdict.

Les seuils 0,46 et 1,95, l'échéance, la référence et la date d'ouverture
ne changent pas. P4 garde sa pré-inscription du 13/10 telle quelle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from maxprofit.apprentissage.lecons import Exemple
from maxprofit.strategies.serie_rouge import PARAMETRES_PRE_INSCRITS

DECLAREES_LE = "2026-10-13"
AMENDEMENT_P2 = "2026-10-14"
#: Les noms partent en HTML vers Telegram : ni « < », ni « > », ni « & ».
#: La fenêtre aveugle telle qu'elle a été pré-inscrite : du gel par défaut
#: du protocole, pour quatorze jours (P4). P2 s'ouvre le même jour et se
#: ferme sur son volume (`live.validation_p2`).
FENETRE_DEBUT = "2026-10-14"
FENETRE_JOURS = 14
SEUIL_RENTABLE = 1 / 1.92

#: Les exclusions de P2 : (clé du contexte À L'ENTRÉE, comparaison, seuil).
#: C écarte le tiers haut de la part parcourue (« +0,46 et plus ») ; D
#: écarte le tiers bas de la place restante (« moins de +1,95 » ; le champ
#: libre vaut 99 et n'est jamais écarté).
EXCLUSION_C = ("part_parcourue", ">=", 0.46)
EXCLUSION_D = ("espace_obstacle", "<", 1.95)
LIBELLES_EXCLUSIONS = {"part_parcourue": "C ≥ 0,46",
                       "espace_obstacle": "D sous 1,95"}

CRITERE_P2 = (
    "sur la fenêtre (ouverte le 14/10, close au volume calculé d'avance) : "
    "1) espérance par ordre positive ; 2) gain d'espérance sur la référence, "
    "même signaux, intervalle apparié par blocs paire × jour au-dessus de "
    "0 ; 3) gain encore positif sans les deux paires qui y contribuent le "
    "plus ; 4) gain encore positif sans la meilleure journée ; 5) au moins "
    "la moitié des signaux de la référence conservés. Les cinq : piste "
    "confirmée provisoirement ; gain positif mais 2) non atteint, ou volume "
    "non atteint à la date de clôture : indécis ; sinon : rejetée.")


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
            "de l'heure en cours, échéance 15 min), écarter celles dont la "
            "part du chemin zone → prochain sommet/creux déjà parcourue est "
            "d'au moins 0,46 ET celles dont la place jusqu'au prochain "
            "sommet/creux est inférieure à 1,95 amplitude augmente "
            "l'espérance par ordre, sur la fenêtre aveugle."),
        univers="ZoneH1 + confirmation M1, paires de la course",
        echeance_sec=900, n_min=100, critere=CRITERE_P2,
        parametres={"exclusions": [EXCLUSION_C, EXCLUSION_D],
                    "manquant": "garde", "amendement": AMENDEMENT_P2}),
    PreInscription(
        ident="P2-C", famille="entree_tardive",
        nom="P2-C part du chemin parcourue sous 0,46",
        hypothese=(
            "Sur les entrées ZoneH1 confirmées jouées, écarter celles dont "
            "la part du chemin zone → prochain sommet/creux déjà parcourue "
            "est d'au moins 0,46 augmente l'espérance par ordre à 15 min, "
            "sur la fenêtre aveugle."),
        univers="ZoneH1 + confirmation M1, paires de la course",
        echeance_sec=900, n_min=100, critere=CRITERE_P2,
        parametres={"exclusions": [EXCLUSION_C], "manquant": "garde",
                    "amendement": AMENDEMENT_P2}),
    PreInscription(
        ident="P2-D", famille="entree_tardive",
        nom="P2-D place jusqu'au prochain sommet/creux d'au moins 1,95",
        hypothese=(
            "Sur les entrées ZoneH1 confirmées jouées, écarter celles dont "
            "la place jusqu'au prochain sommet/creux confirmé est inférieure "
            "à 1,95 amplitude (le champ libre n'est jamais écarté) augmente "
            "l'espérance par ordre à 15 min, sur la fenêtre aveugle."),
        univers="ZoneH1 + confirmation M1, paires de la course",
        echeance_sec=900, n_min=100, critere=CRITERE_P2,
        parametres={"exclusions": [EXCLUSION_D], "manquant": "garde",
                    "amendement": AMENDEMENT_P2}),
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

#: Les trois groupes filtrés de P2, dans l'ordre du tableau : B, C, D.
GROUPES_P2: tuple[str, ...] = ("P2-C", "P2-D", "P2-E")


def preinscription(ident: str) -> PreInscription:
    return next(p for p in PREINSCRIPTIONS if p.ident == ident)


def ouverture() -> int:
    """L'ouverture de la fenêtre aveugle, en secondes UTC."""
    return int(datetime.strptime(FENETRE_DEBUT, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp())


def fenetre_pre_inscrite() -> tuple[int, int]:
    """[début, fin) de la fenêtre aveugle de P4, en secondes UTC."""
    debut = ouverture()
    return debut, debut + FENETRE_JOURS * 86400


def _declenche(valeur: float, comparaison: str, seuil: float) -> bool:
    return valeur >= seuil if comparaison == ">=" else valeur < seuil


def motifs_de_rejet(valeurs: Mapping[str, float | None],
                    exclusions: Sequence[Sequence[Any]]) -> list[str]:
    """Les exclusions que l'entrée déclenche. Une valeur MANQUANTE n'en
    déclenche aucune : c'est la formule de /experiences, qui a produit
    l'hypothèse (« sans le pire tiers » garde les entrées non mesurées)."""
    motifs = []
    for cle, comparaison, seuil in exclusions:
        valeur = valeurs.get(cle)
        if valeur is not None and _declenche(float(valeur), comparaison,
                                             float(seuil)):
            motifs.append(LIBELLES_EXCLUSIONS.get(cle, cle))
    return motifs


def retenue(e: Exemple, exclusions: Sequence[Sequence[Any]]) -> bool:
    """L'entrée échappe-t-elle à TOUTES les exclusions ?"""
    return not motifs_de_rejet(e.contexte, exclusions)


def juger(n: int, g: int, ic, n_min: int,
          reference: Sequence[int] | None = None) -> str:
    """Le verdict de P4 sur la fenêtre aveugle, selon son critère écrit
    d'avance. (P2 a le sien : `live.validation_p2`.)"""
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
