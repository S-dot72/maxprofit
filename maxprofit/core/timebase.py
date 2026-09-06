"""
Base de temps — une seule convention, vérifiée mécaniquement.

Spec §5 : « Typer les horodatages sans ambiguïté : suffixer systématiquement
`_ms` ou `_sec`, tout en UTC. La confusion secondes/millisecondes est le bug le
plus fréquent de ce type de projet et il produit des backtests décalés d'un
facteur 1000 sans lever d'erreur. »

Conventions du projet, sans exception :

- Tout horodatage est un entier, epoch UTC. Jamais de `datetime` naïf, jamais
  d'heure locale, jamais de float pour un instant (le float perd la
  milliseconde au-delà de 2038 et rend les comparaisons instables).
- Le nom de toute variable, colonne, champ ou paramètre portant un instant se
  termine par `_ms` ou `_sec`. Un nom sans suffixe est un bug de revue.
- Les ticks sont en `_ms` (horloge SERVEUR du broker). Les bougies, payouts et
  battements de coeur sont en `_sec`.

Le rôle de ce module est de rendre la confusion détectable plutôt que
silencieuse : `ensure_ms` et `ensure_sec` refusent une valeur dont l'ordre de
grandeur ne correspond pas à l'unité annoncée.
"""

from __future__ import annotations

from datetime import datetime, timezone

from maxprofit.core.errors import TimebaseError

MS_PER_SEC = 1000

# Fenêtre de plausibilité : 2000-01-01 → 2100-01-01 UTC.
# Un epoch en secondes et le même epoch en millisecondes sont séparés d'un
# facteur 1000, donc les deux fenêtres ne se recouvrent pas. C'est exactement
# ce qui rend la confusion détectable.
MIN_PLAUSIBLE_SEC = 946_684_800        # 2000-01-01T00:00:00Z
MAX_PLAUSIBLE_SEC = 4_102_444_800      # 2100-01-01T00:00:00Z
MIN_PLAUSIBLE_MS = MIN_PLAUSIBLE_SEC * MS_PER_SEC
MAX_PLAUSIBLE_MS = MAX_PLAUSIBLE_SEC * MS_PER_SEC


def ensure_sec(ts_sec: int, *, what: str = "horodatage") -> int:
    """Valide un instant en secondes UTC. Lève si l'ordre de grandeur trahit
    des millisecondes (ou toute autre unité)."""
    if isinstance(ts_sec, bool) or not isinstance(ts_sec, int):
        raise TimebaseError(
            f"{what} : attendu un int en secondes UTC, reçu {type(ts_sec).__name__} "
            f"({ts_sec!r}). Les instants sont des entiers, jamais des float."
        )
    if MIN_PLAUSIBLE_MS <= ts_sec <= MAX_PLAUSIBLE_MS:
        raise TimebaseError(
            f"{what} = {ts_sec} est annoncé en secondes mais tombe dans la plage "
            f"des millisecondes. Confusion _sec / _ms : diviser par 1000."
        )
    if not (MIN_PLAUSIBLE_SEC <= ts_sec <= MAX_PLAUSIBLE_SEC):
        raise TimebaseError(
            f"{what} = {ts_sec} hors de la plage plausible en secondes UTC "
            f"[{MIN_PLAUSIBLE_SEC}, {MAX_PLAUSIBLE_SEC}] (2000 → 2100)."
        )
    return ts_sec


def ensure_ms(ts_ms: int, *, what: str = "horodatage") -> int:
    """Valide un instant en millisecondes UTC. Lève si l'ordre de grandeur
    trahit des secondes."""
    if isinstance(ts_ms, bool) or not isinstance(ts_ms, int):
        raise TimebaseError(
            f"{what} : attendu un int en millisecondes UTC, reçu "
            f"{type(ts_ms).__name__} ({ts_ms!r}). Les instants sont des entiers."
        )
    if MIN_PLAUSIBLE_SEC <= ts_ms <= MAX_PLAUSIBLE_SEC:
        raise TimebaseError(
            f"{what} = {ts_ms} est annoncé en millisecondes mais tombe dans la "
            f"plage des secondes. Confusion _ms / _sec : multiplier par 1000."
        )
    if not (MIN_PLAUSIBLE_MS <= ts_ms <= MAX_PLAUSIBLE_MS):
        raise TimebaseError(
            f"{what} = {ts_ms} hors de la plage plausible en millisecondes UTC "
            f"[{MIN_PLAUSIBLE_MS}, {MAX_PLAUSIBLE_MS}] (2000 → 2100)."
        )
    return ts_ms


def sec_to_ms(ts_sec: int) -> int:
    """Secondes UTC → millisecondes UTC, avec validation de l'entrée."""
    return ensure_sec(ts_sec) * MS_PER_SEC


def ms_to_sec(ts_ms: int) -> int:
    """Millisecondes UTC → secondes UTC (troncature vers le passé).

    La troncature est volontairement vers le bas, y compris pour les instants
    négatifs qui n'existent pas ici : un tick à 12:00:00.999 appartient à la
    seconde 12:00:00, jamais à la suivante.
    """
    return ensure_ms(ts_ms) // MS_PER_SEC


def floor_to_tf_sec(ts_sec: int, tf_sec: int) -> int:
    """Début de la bougie de timeframe `tf_sec` contenant `ts_sec`."""
    if tf_sec <= 0:
        raise TimebaseError(f"tf_sec doit être strictement positif, reçu {tf_sec}")
    return ensure_sec(ts_sec) // tf_sec * tf_sec


def bucket_of_ms(ts_ms: int, tf_sec: int) -> int:
    """Début (en secondes UTC) de la bougie contenant le tick `ts_ms`."""
    return floor_to_tf_sec(ms_to_sec(ts_ms), tf_sec)


def utc_from_sec(ts_sec: int) -> datetime:
    """`datetime` conscient du fuseau, toujours UTC. Pour l'affichage seulement :
    aucune logique métier ne manipule de `datetime`."""
    return datetime.fromtimestamp(ensure_sec(ts_sec), timezone.utc)


def utc_from_ms(ts_ms: int) -> datetime:
    """Idem, depuis des millisecondes. La milliseconde est conservée."""
    return datetime.fromtimestamp(ensure_ms(ts_ms) / MS_PER_SEC, timezone.utc)


def format_sec(ts_sec: int) -> str:
    return utc_from_sec(ts_sec).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_ms(ts_ms: int) -> str:
    return utc_from_ms(ts_ms).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC"


# --------------------------------------------------------------------------- #
# Heure locale — AFFICHAGE ET ANALYSE UNIQUEMENT
# --------------------------------------------------------------------------- #
#
# Rien de ce qui est stocké, comparé ou calculé n'utilise l'heure locale. Tout
# ce qui entre en base est en UTC, sans exception (§5). Ces fonctions servent à
# deux choses, et à rien d'autre :
#
#   - lire un horodatage dans les rapports sans faire le calcul de tête ;
#   - segmenter par heure de la journée telle qu'elle est VÉCUE (§3.2), parce
#     que si un edge horaire existe, il est lié aux sessions de marché et aux
#     habitudes, pas au méridien de Greenwich.
#
# Pourquoi un fuseau nommé et pas un décalage fixe de -5 : Haïti applique
# l'heure d'été. Le pays est à UTC-5 (EST) de novembre à mars, et à UTC-4 (EDT)
# de mars à novembre. Un décalage codé en dur serait donc faux la moitié de
# l'année, et l'erreur ne se verrait pas : elle décalerait simplement d'une
# heure la moitié des données dans une segmentation horaire, ce qui suffit à
# faire apparaître un edge à une heure où il n'y en a pas, ou à en effacer un.
# `zoneinfo` connaît les dates de transition et les applique par horodatage.

import os
from zoneinfo import ZoneInfo

#: Fuseau d'affichage. Peut être changé par la variable d'environnement, mais
#: n'a PAS besoin de l'être : contrairement au chemin de base ou au payout, se
#: tromper ici ne fausse aucun résultat, cela rend seulement un rapport moins
#: lisible. Un défaut est donc légitime (cf. §5, qui vise l'argent et les
#: données).
ENV_TIMEZONE = "TIMEZONE_AFFICHAGE"
TIMEZONE_DEFAUT = "America/Port-au-Prince"


def fuseau_affichage() -> ZoneInfo:
    nom = os.environ.get(ENV_TIMEZONE, "").strip() or TIMEZONE_DEFAUT
    try:
        return ZoneInfo(nom)
    except Exception as erreur:
        raise TimebaseError(
            f"Fuseau horaire inconnu : {nom!r} ({erreur}). Utilisez un nom IANA "
            f"comme 'America/Port-au-Prince'. Un décalage fixe ('UTC-5') est "
            f"refusé : il ignore l'heure d'été."
        ) from None


def local_from_sec(ts_sec: int) -> datetime:
    return utc_from_sec(ts_sec).astimezone(fuseau_affichage())


def local_from_ms(ts_ms: int) -> datetime:
    return utc_from_ms(ts_ms).astimezone(fuseau_affichage())


def heure_locale(ts_sec: int) -> int:
    """Heure de la journée vécue localement, 0-23.

    C'est cette valeur qu'il faut utiliser pour la segmentation horaire du
    §3.2 — mais la feature stockée dans `evaluations` reste `heure_utc`, brute
    et non ambiguë. On dérive l'heure locale au moment de l'analyse, jamais à
    l'enregistrement : une donnée stockée dans un fuseau qui change deux fois
    par an n'est plus interprétable une fois les règles de transition modifiées.
    """
    return local_from_sec(ts_sec).hour


def decalage_local_heures(ts_sec: int) -> float:
    """Décalage local à cet instant précis, en heures. -5 en hiver, -4 en été."""
    offset = local_from_sec(ts_sec).utcoffset()
    return offset.total_seconds() / 3600 if offset else 0.0


def format_local_sec(ts_sec: int) -> str:
    return local_from_sec(ts_sec).strftime("%Y-%m-%d %H:%M:%S %Z")


def format_local_ms(ts_ms: int) -> str:
    d = local_from_ms(ts_ms)
    return d.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + d.strftime(" %Z")
