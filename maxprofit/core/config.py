"""
Configuration — sans valeur par défaut silencieuse.

Spec §5 : « Interdire les valeurs par défaut silencieuses sur tout ce qui
touche à l'argent ou aux données : chemin de base, payout, latence, expiration.
Absence de configuration = arrêt, pas valeur par défaut. »

Le raisonnement, qui vaut d'être écrit parce qu'il est contre-intuitif : une
valeur par défaut raisonnable est pire qu'une absence de valeur. Si la latence
retombe à 0 s parce que la variable n'est pas lue, le backtest tourne, produit
un rapport, et le rapport est faux — sans le moindre message d'erreur. Un
`ConfigurationError` au démarrage coûte trente secondes ; un backtest
silencieusement optimiste coûte des semaines.

Ce module ne fournit donc QUE des accesseurs obligatoires. Il n'y a
volontairement pas de `get_env(name, default=...)` : la fonction n'existe pas,
donc personne ne l'appelle par réflexe.
"""

from __future__ import annotations

import os
from pathlib import Path

from maxprofit.core.errors import ConfigurationError

#: Chemin du fichier de base de données. La spec §1.1 impose qu'il vive HORS du
#: répertoire de code, pour qu'un déploiement ne puisse pas l'écraser.
ENV_DB_PATH = "TRADING_DB_PATH"


def require_env(name: str, *, why: str = "") -> str:
    """Lit une variable d'environnement obligatoire. Lève si absente ou vide.

    Une chaîne vide est traitée comme absente : `EXPORT X=` est presque
    toujours une erreur de script, pas une intention.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        suffix = f" {why}" if why else ""
        raise ConfigurationError(
            f"Variable d'environnement {name} absente ou vide.{suffix} "
            f"Aucune valeur par défaut n'est appliquée (spec §5) : définissez-la "
            f"puis relancez."
        )
    return raw.strip()


def require_int_env(name: str, *, why: str = "", minimum: int | None = None,
                    maximum: int | None = None) -> int:
    raw = require_env(name, why=why)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigurationError(
            f"{name}={raw!r} n'est pas un entier."
        ) from None
    _check_bounds(name, value, minimum, maximum)
    return value


def require_float_env(name: str, *, why: str = "", minimum: float | None = None,
                      maximum: float | None = None) -> float:
    raw = require_env(name, why=why)
    try:
        value = float(raw)
    except ValueError:
        raise ConfigurationError(
            f"{name}={raw!r} n'est pas un nombre."
        ) from None
    _check_bounds(name, value, minimum, maximum)
    return value


def _check_bounds(name, value, minimum, maximum) -> None:
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name}={value} est inférieur au minimum {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name}={value} est supérieur au maximum {maximum}")


def db_path() -> Path:
    """Chemin de la base, lu depuis `TRADING_DB_PATH`, sans repli.

    Trois vérifications, toutes issues de la spec §1.1 :

    - la variable existe (sinon arrêt) ;
    - le chemin est ABSOLU — un chemin relatif dépend du répertoire courant, et
      c'est précisément ce qui fait qu'un `cd` malheureux crée une base neuve à
      côté du code au lieu d'ouvrir l'ancienne ;
    - le répertoire parent existe déjà — le créer à la volée masquerait une
      faute de frappe dans le chemin, qui se traduirait par une base vide.
    """
    path = Path(require_env(
        ENV_DB_PATH,
        why="Elle porte le chemin ABSOLU du fichier de base, qui doit vivre "
            "hors du répertoire de code (ex. ~/trading_data/market.db).",
    ))
    if not path.is_absolute():
        raise ConfigurationError(
            f"{ENV_DB_PATH}={path} est un chemin relatif. Un chemin relatif "
            f"dépend du répertoire courant : au premier lancement depuis un "
            f"autre dossier, vous ouvrez une base vide et croyez avoir tout "
            f"perdu. Donnez un chemin absolu."
        )
    parent = path.parent
    if not parent.is_dir():
        raise ConfigurationError(
            f"Le répertoire {parent} n'existe pas. Il n'est volontairement pas "
            f"créé automatiquement : une faute de frappe dans {ENV_DB_PATH} "
            f"produirait alors une base vide au lieu d'une erreur. Créez-le "
            f"vous-même si le chemin est correct."
        )
    return path


def backups_dir() -> Path:
    """Répertoire des sauvegardes, à côté de la base (spec §1.4)."""
    return db_path().parent / "backups"
