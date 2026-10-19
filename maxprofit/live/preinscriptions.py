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


# --------------------------------------------------------------------------- #
# Mission Reference 8 (2026-10-18) — `live.reference8`
# --------------------------------------------------------------------------- #
#
# Quatre modèles sur les mêmes entrées : M0 la référence, M1 = M0 + B,
# M2 = M0 + C, M3 = M0 + E (B + C + D, la combinaison d'EXP-020) ; D seul en
# analyse secondaire, et P2-E (C + D) gardée telle que pré-inscrite.
#
# Les seuils : C (0,46) et D (1,95) sont ceux du 13/10, déjà gelés. B n'avait
# jamais été pré-inscrit : il prend la valeur que cite le document de la
# mission, 1,31 (« la catégorie supérieure, taille relative d'au moins
# 1,31 »). Deux autres lectures du même tercile existent, 1,27 (lecture du
# 14/10, reprise par /potentiel) et, pour D, 1,94 (même document) : ce sont
# des lectures successives de terciles que /experiences recalcule à chaque
# rejeu. Elles ne sont pas retenues ; /reference8 compte les entrées que
# l'écart ferait changer de côté.
#
# Ajout du 18/10 au soir, AVANT l'ouverture : M4 = M0 sans les entrées
# prises à 1,55 amplitude ou plus de la zone (`distance_niveau` à l'entrée),
# la troisième priorité de l'analyse du 18/10 (« tester séparément la
# distance à la zone »). 1,55 est la coupe du tiers haut que cite cette
# analyse ; une autre expérience lisait 1,52 — non retenue, comptée.
#
# M2 est P2-C, D est P2-D : mêmes règles, déjà pré-inscrites le 13/10. Ici,
# tous les modèles sont jugés sur UNE fenêtre commune ouverte le 19/10 à
# 0 h UTC, pour que le tableau compare les mêmes signaux ; P2 garde sa
# propre fenêtre du 14/10 dans /protocole.

DECLAREES_R8 = "2026-10-18"
OUVERTURE_R8 = "2026-10-19"
#: Durée nominale inscrite au registre local ; la fenêtre se ferme sur un
#: VOLUME calculé d'avance, comme P2.
FENETRE_JOURS_R8 = 28
EXCLUSION_B = ("taille_bougie", ">=", 1.31)
LIBELLES_EXCLUSIONS["taille_bougie"] = "B ≥ 1,31"
EXCLUSION_DISTANCE = ("distance_niveau", ">=", 1.55)
LIBELLES_EXCLUSIONS["distance_niveau"] = "distance ≥ 1,55"
#: Les filtres de qualité de la course (`/filtres qualite`, 19/10) : B, et
#: le mouvement de l'heure déjà dans le sens du trade (seuil rond, 0).
EXCLUSION_HEURE = ("mouvement_heure", ">=", 0.0)
LIBELLES_EXCLUSIONS["mouvement_heure"] = "heure déjà dans le sens du trade"

#: (identifiant, libellé, exclusions) — l'ordre du tableau de /reference8.
MODELES_R8: tuple[tuple[str, str, tuple], ...] = (
    ("M0", "référence (ZoneH1 + confirmation M1)", ()),
    ("M1", "M0 sans bougie d'entrée ≥ 1,31", (EXCLUSION_B,)),
    ("M2", "M0 sans part parcourue ≥ 0,46", (EXCLUSION_C,)),
    ("M3", "M0 + E : sans B, C ni D", (EXCLUSION_B, EXCLUSION_C,
                                         EXCLUSION_D)),
    ("M4", "M0 sans entrée à 1,55 amplitude ou plus de la zone",
     (EXCLUSION_DISTANCE,)),
    ("Q", "filtres de qualité : sans bougie ≥ 1,31 ni heure déjà dans le "
          "sens du trade", (EXCLUSION_B, EXCLUSION_HEURE)),
    ("D", "secondaire : M0 sans place sous 1,95", (EXCLUSION_D,)),
    ("C+D", "P2-E pré-inscrite : sans C ni D", (EXCLUSION_C, EXCLUSION_D)),
)

CRITERE_R8 = (
    "sur la fenêtre ouverte le 19/10 (close au volume calculé d'avance sur "
    "les données antérieures) : les cinq critères de P2 — espérance "
    "positive ; gain d'espérance sur M0, mêmes signaux, intervalle apparié "
    "par blocs paire × jour au-dessus de 0 ; gain encore positif sans les "
    "deux paires et sans la journée qui y contribuent le plus ; au moins la "
    "moitié des signaux de M0 conservés. Les cinq : soutenue ; gain positif "
    "mais intervalle touchant 0, ou volume non atteint : indéterminée ; "
    "sinon : non soutenue.")

CRITERE_R8_H = (
    "règle apprise sur les entrées d'AVANT le 19/10, figée au premier rejeu "
    "qui suit : dans chaque strate (C ≥ 0,46 ; sinon D sous 1,95 ; sinon "
    "B ≥ 1,31 ; sinon le reste), l'échéance (4, 10 ou 15 min) au meilleur "
    "taux, 15 min si la strate a moins de 30 entrées. Jugée sur la fenêtre "
    "contre 15 min fixe, mêmes entrées : soutenue si l'intervalle apparié "
    "par blocs de l'écart d'espérance est au-dessus de 0 avec au moins 100 "
    "entrées ; non soutenue si l'écart est nul ou négatif ; sinon "
    "indéterminée.")

PREINSCRIPTIONS_R8: tuple[PreInscription, ...] = (
    PreInscription(
        ident="R8-M1", famille="entree_tardive",
        nom="R8-M1 bougie d'entrée sous 1,31 sur la référence 8",
        hypothese=(
            "Sur les entrées ZoneH1 confirmées jouées, écarter celles dont "
            "la bougie d'entrée mesure au moins 1,31 fois la médiane des 15 "
            "précédentes augmente l'espérance par ordre à 15 min, sur la "
            "fenêtre ouverte le 19/10."),
        univers="ZoneH1 + confirmation M1, paires de la course",
        echeance_sec=900, n_min=100, critere=CRITERE_R8,
        parametres={"exclusions": [EXCLUSION_B], "manquant": "garde"}),
    PreInscription(
        ident="R8-M3", famille="entree_tardive",
        nom="R8-M3 filtre E (B, C et D) sur la référence 8",
        hypothese=(
            "Sur les entrées ZoneH1 confirmées jouées, écarter celles dont "
            "la bougie d'entrée mesure au moins 1,31 fois la médiane des 15 "
            "précédentes, OU dont la part du chemin parcourue est d'au "
            "moins 0,46, OU dont la place jusqu'au prochain sommet/creux "
            "est inférieure à 1,95 amplitude, augmente l'espérance par "
            "ordre à 15 min, sur la fenêtre ouverte le 19/10."),
        univers="ZoneH1 + confirmation M1, paires de la course",
        echeance_sec=900, n_min=100, critere=CRITERE_R8,
        parametres={"exclusions": [EXCLUSION_B, EXCLUSION_C, EXCLUSION_D],
                    "manquant": "garde"}),
    PreInscription(
        ident="R8-M4", famille="entree_tardive",
        nom="R8-M4 entrée à moins de 1,55 amplitude de la zone",
        hypothese=(
            "Sur les entrées ZoneH1 confirmées jouées, écarter celles prises "
            "à 1,55 amplitude M1 ou plus de la zone (distance mesurée à "
            "l'entrée, dans le sens du trade) augmente l'espérance par "
            "ordre à 15 min, sur la fenêtre ouverte le 19/10 ; jugée seule, "
            "puis au-delà du filtre B."),
        univers="ZoneH1 + confirmation M1, paires de la course",
        echeance_sec=900, n_min=100, critere=CRITERE_R8,
        parametres={"exclusions": [EXCLUSION_DISTANCE], "manquant": "garde"}),
    PreInscription(
        ident="R8-Q", famille="qualite",
        nom="R8-Q filtres de qualité de la course (B et heure)",
        hypothese=(
            "Sur les entrées ZoneH1 confirmées jouées, écarter celles dont "
            "la bougie d'entrée mesure au moins 1,31 fois la médiane des 15 "
            "précédentes ET celles prises quand le mouvement depuis le début "
            "de l'heure va déjà dans le sens du trade (au moins 0 amplitude) "
            "augmente l'espérance par ordre à 15 min. Déclarée le 19/10 après "
            "l'ouverture de la fenêtre (une entrée de M0 dedans, non lue pour "
            "ces variables) ; ce sont aussi les filtres de /filtres qualite."),
        univers="ZoneH1 + confirmation M1, paires de la course",
        echeance_sec=900, n_min=100, critere=CRITERE_R8,
        parametres={"exclusions": [EXCLUSION_B, EXCLUSION_HEURE],
                    "manquant": "garde"}),
    PreInscription(
        ident="R8-H", famille="echeance",
        nom="R8-H échéance choisie selon B, C et D à l'entrée",
        hypothese=(
            "Choisir l'échéance (4, 10 ou 15 min) d'après la strate de "
            "l'entrée (C, D, B à l'entrée), selon une règle apprise avant "
            "le 19/10 et figée, donne une meilleure espérance par ordre que "
            "15 min fixe sur les mêmes entrées, sur la fenêtre."),
        univers="ZoneH1 + confirmation M1, paires de la course",
        echeance_sec=900, n_min=100, critere=CRITERE_R8_H,
        parametres={"horizons_sec": [240, 600, 900], "n_min_strate": 30}),
)


def ouverture_r8() -> int:
    """L'ouverture de la fenêtre de la mission Reference 8, en s UTC."""
    return int(datetime.strptime(OUVERTURE_R8, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp())


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


# --------------------------------------------------------------------------- #
# Le retard de la confirmation (2026-10-19) — `live.entrees`
# --------------------------------------------------------------------------- #
#
# L'analyse du 18/10 : la confirmation M1 SÉLECTIONNE de meilleurs signaux
# (+0,26 d'espérance à 15 min) mais le RETARD qu'elle impose en reprend
# presque autant (−0,24). Quatre entrées sur les mêmes signaux :
#
#   T0  au signal (clôture de la bougie du signal) ;
#   T1  à la bougie suivante, sans condition (le retard pur) ;
#   T2  à la confirmation M1 actuelle (règle de l'heure relue à l'entrée) ;
#   T3  comme T2, mais annulée si l'entrée est à `x` amplitudes ou plus de
#       la zone ; x parmi 1,00, 1,25 et 1,55, choisi sur les entrées
#       d'AVANT la fenêtre et figé.
#
# Un signal sans ordre (non confirmé, annulé) ne gagne ni ne perd : la
# comparaison porte sur le GAIN PAR OPPORTUNITÉ (gain total / signaux du
# pool), qui tient compte du volume, et non sur le seul taux.
#
# La fenêtre s'ouvre le 20/10 à 0 h UTC, après cette déclaration du 19/10 :
# la fenêtre du 19/10 était déjà ouverte quand ces hypothèses ont été
# écrites.

DECLAREES_T = "2026-10-19"
OUVERTURE_T = "2026-10-20"
FENETRE_JOURS_T = 21
SEUILS_ANNULATION: tuple[float, ...] = (1.00, 1.25, 1.55)
#: Signaux du pool dans la fenêtre avant un verdict (figé ensuite).
N_MIN_T = 300

CRITERE_T = (
    "sur la fenêtre ouverte le 20/10 à 0 h UTC, close à 300 signaux du pool "
    "(signaux ZoneH1 passant la règle de l'heure au signal, issues "
    "déterminables) : 1) espérance par ordre de la variante positive ; "
    "2) gain par opportunité supérieur à celui de T2 (confirmation "
    "actuelle), mêmes signaux, intervalle apparié par blocs paire × jour "
    "au-dessus de 0 ; 3) gain encore positif sans les deux paires et sans "
    "la journée qui y contribuent le plus. Les trois : soutenue ; écart "
    "positif sans les trois : indéterminée ; écart nul ou négatif : non "
    "soutenue.")

PREINSCRIPTIONS_T: tuple[PreInscription, ...] = (
    PreInscription(
        ident="R8-T0", famille="confirmation",
        nom="R8-T0 entrée au signal plutôt qu'à la confirmation M1",
        hypothese=(
            "Sur les mêmes signaux ZoneH1 (règle de l'heure au signal), "
            "entrer à la clôture de la bougie du signal (T0) donne un gain "
            "par opportunité à 15 min supérieur à celui de l'entrée à la "
            "confirmation M1 actuelle (T2), sur la fenêtre ouverte le "
            "20/10."),
        univers="ZoneH1, paires de la course",
        echeance_sec=900, n_min=N_MIN_T, critere=CRITERE_T,
        parametres={"variante": "T0", "reference": "T2"}),
    PreInscription(
        ident="R8-T3", famille="confirmation",
        nom="R8-T3 confirmation annulée au-delà d'une distance à la zone",
        hypothese=(
            "Sur les mêmes signaux, annuler l'entrée confirmée (T2) quand "
            "elle se trouve à x amplitudes M1 ou plus de la zone — x choisi "
            "parmi 1,00, 1,25 et 1,55 sur les entrées d'avant la fenêtre, "
            "puis figé — donne un gain par opportunité à 15 min supérieur à "
            "celui de T2, sur la fenêtre ouverte le 20/10."),
        univers="ZoneH1, paires de la course",
        echeance_sec=900, n_min=N_MIN_T, critere=CRITERE_T,
        parametres={"variante": "T3", "reference": "T2",
                    "seuils": list(SEUILS_ANNULATION)}),
)


def ouverture_t() -> int:
    """L'ouverture de la fenêtre des entrées T0 à T3, en s UTC."""
    return int(datetime.strptime(OUVERTURE_T, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp())
