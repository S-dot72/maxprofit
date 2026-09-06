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
