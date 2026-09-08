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
        raise ConfigurationError(_message_repertoire_absent(parent))
    return path


def _dans_un_conteneur() -> bool:
    """Le conseil à donner n'est pas le même selon l'endroit.

    Sur un poste, un répertoire manquant se crée avec `mkdir`. Sur une
    plateforme d'hébergement il n'y a pas de shell, et le répertoire n'est pas
    à créer : c'est le POINT DE MONTAGE d'un disque. S'il manque, le disque
    n'est pas attaché — et créer le répertoire ferait écrire la collecte sur le
    système de fichiers du conteneur, effacé au déploiement suivant.
    """
    if Path("/.dockerenv").exists():
        return True
    return any(cle in os.environ for cle in
               ("RENDER", "RENDER_SERVICE_ID", "RAILWAY_ENVIRONMENT",
                "FLY_APP_NAME", "KUBERNETES_SERVICE_HOST"))


def _message_repertoire_absent(parent: Path) -> str:
    commun = (
        f"Le répertoire {parent} n'existe pas, et il n'est volontairement pas "
        f"créé automatiquement : une base créée à la volée dans un chemin "
        f"inattendu part vide et se perd au redémarrage suivant, ce qui est "
        f"précisément le problème que la spec §1.1 cherche à empêcher."
    )
    if not _dans_un_conteneur():
        return f"{commun} Créez-le : mkdir -p {parent}"
    return (
        f"{commun}\n"
        f"\n"
        f"Vous êtes dans un CONTENEUR, où ce répertoire n'est pas à créer : "
        f"c'est le point de montage d'un disque persistant, et son absence "
        f"signifie que le disque n'est pas attaché.\n"
        f"\n"
        f"  Render   : tableau de bord du service > Disks > Add Disk, avec "
        f"Mount Path = {parent}. Les disques ne sont pas disponibles sur "
        f"l'offre gratuite. Si le service a été créé à la main, le disque "
        f"déclaré dans render.yaml est ignoré : il faut soit l'ajouter ici, "
        f"soit recréer le service en Blueprint.\n"
        f"  Railway  : Volumes > New Volume, monté sur {parent}.\n"
        f"  Fly.io   : fly volumes create, puis [mounts] dans fly.toml.\n"
        f"\n"
        f"Créer ce répertoire dans l'image ne réglerait rien : la collecte "
        f"partirait sur le système de fichiers du conteneur et disparaîtrait "
        f"au déploiement suivant."
    )


def backups_dir() -> Path:
    """Répertoire des sauvegardes, à côté de la base (spec §1.4)."""
    return db_path().parent / "backups"


#: Base de RECHERCHE. Contrairement à `TRADING_DB_PATH`, elle a une valeur
#: dérivée par défaut, et c'est délibéré : cette base est entièrement
#: reconstructible en rejouant les backtests. La perdre coûte du temps de
#: calcul, pas des données. La règle « pas de défaut silencieux » du §5 vise ce
#: qu'on ne peut pas régénérer.
ENV_RESEARCH_DB_PATH = "RESEARCH_DB_PATH"


def research_db_path() -> Path:
    """`RESEARCH_DB_PATH` si définie, sinon `research.db` à côté de la base de
    marché — dans le même répertoire persistant, jamais dans le code."""
    brut = os.environ.get(ENV_RESEARCH_DB_PATH, "").strip()
    if not brut:
        return db_path().parent / "research.db"
    chemin = Path(brut)
    if not chemin.is_absolute():
        raise ConfigurationError(
            f"{ENV_RESEARCH_DB_PATH}={chemin} est un chemin relatif. Donnez un "
            f"chemin absolu, comme pour {ENV_DB_PATH}."
        )
    if not chemin.parent.is_dir():
        raise ConfigurationError(f"Le répertoire {chemin.parent} n'existe pas.")
    return chemin


# --------------------------------------------------------------------------- #
# Chargement de .env
# --------------------------------------------------------------------------- #

def charger_env_local(chemin: Path | None = None) -> list[str]:
    """Charge `.env` dans l'environnement. Retourne les clés définies.

    Appelée EXPLICITEMENT par les points d'entrée, jamais à l'import d'un
    module. Un fichier qui modifie l'environnement du seul fait qu'on importe
    une bibliothèque rend le comportement dépendant de l'ordre des imports, et
    les tests dépendants du répertoire courant.

    Les variables DÉJÀ définies dans l'environnement l'emportent : en
    hébergement, la plateforme injecte ses propres valeurs et un `.env` oublié
    dans l'image ne doit pas les écraser silencieusement.

    Volontairement minimal — pas de dépendance à python-dotenv pour lire des
    lignes `CLE=valeur`. Ni interpolation, ni export, ni multi-lignes : si un
    jour il en faut, ce sera un choix explicite.
    """
    chemin = chemin or (Path.cwd() / ".env")
    if not chemin.is_file():
        return []

    definies: list[str] = []
    vues: dict[str, int] = {}
    for numero, ligne in enumerate(
        chemin.read_text(encoding="utf-8").splitlines(), start=1
    ):
        nue = ligne.strip()
        if not nue or nue.startswith("#"):
            continue
        cle, separateur, valeur = nue.partition("=")
        if not separateur:
            raise ConfigurationError(
                f"{chemin}:{numero} : ligne sans '=' ({ligne!r}). Format "
                f"attendu : CLE=valeur."
            )
        cle = cle.strip()
        if not cle:
            raise ConfigurationError(f"{chemin}:{numero} : clé vide")
        if cle in vues:
            # Deux valeurs pour une clé : laquelle est la bonne ? Le fichier ne
            # le dit pas, et un chargeur qui tranche tout seul se trompera un
            # jour sur celle qui compte — un chemin de base, un payout minimal.
            # On refuse plutôt que d'appliquer une règle que personne n'a lue.
            raise ConfigurationError(
                f"{chemin} : {cle} est défini deux fois (lignes "
                f"{vues[cle]} et {numero}). Gardez-en un seul : rien ici ne "
                f"dit lequel devrait l'emporter."
            )
        vues[cle] = numero
        if cle in os.environ:
            continue          # l'environnement réel l'emporte
        valeur = valeur.strip()
        # Guillemets facultatifs, retirés seulement s'ils encadrent la valeur.
        if len(valeur) >= 2 and valeur[0] == valeur[-1] and valeur[0] in "\"'":
            valeur = valeur[1:-1]
        os.environ[cle] = valeur
        definies.append(cle)
    return definies
