"""
Turso — le stockage durable quand la machine n'en a pas.

**Pourquoi.** Sur une offre d'hébergement gratuite, il n'y a pas de disque
persistant : le système de fichiers du conteneur est effacé à chaque
déploiement, à chaque redémarrage, et après chaque mise en veille. Une base
posée dessus repart vide sans un message — on croit collecter, on n'accumule
rien. C'est précisément le désastre silencieux que la §1.1 cherche à empêcher.

**Le mode réplique embarquée.** libSQL permet quelque chose de mieux qu'une base
distante : les écritures vont dans un fichier SQLite LOCAL, et `sync()` pousse
les changements vers Turso. Trois conséquences qui comptent ici :

- pas d'aller-retour réseau par tick. À deux ticks par seconde et par paire,
  sur trente paires, une base distante ordinaire passerait son temps en
  latence ;
- au démarrage, la connexion tire l'état distant : le conteneur repart avec
  toute la collecte, quel que soit ce qu'il est advenu de son disque ;
- le fichier local devient un CACHE. Sa perte n'est plus un incident, ce qui
  retire tout son sens à l'exigence d'un volume persistant.

**Ce que l'on perd, et qu'il faut regarder en face.** Entre deux `sync()`, les
données ne sont qu'en local. Une coupure brutale du conteneur perd cet
intervalle — c'est pourquoi la synchronisation suit le rythme d'écriture du
collecteur plutôt qu'un minuteur lâche, et pourquoi une dernière
synchronisation est faite à l'arrêt. Une donnée perdue de cette façon laisse un
trou dans `uptime` : le backtest l'écartera (§2.4) au lieu de raisonner dessus.

**Ce que Turso ne change pas.** Le schéma, les migrations, les requêtes : c'est
du SQLite. Le seul point qui a dû bouger est le versionnage de schéma, qui
passait par `PRAGMA user_version` — voir `store/version.py`.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from maxprofit.core.errors import BotError

log = logging.getLogger(__name__)

ENV_URL = "TURSO_DATABASE_URL"
ENV_JETON = "TURSO_AUTH_TOKEN"


#: Schémas d'URL acceptés. `libsql://` est la forme canonique ; `https://`
#: désigne la même base et fonctionne aussi. Tout autre schéma trahit une URL
#: copiée du mauvais endroit du tableau de bord.
SCHEMAS_VALIDES = ("libsql://", "https://", "http://", "wss://", "ws://")


class TursoIndisponible(BotError):
    """Turso est demandé mais inutilisable."""


def _verifier_url(url: str) -> None:
    if url.startswith(SCHEMAS_VALIDES):
        return
    raise TursoIndisponible(
        f"{ENV_URL} = « {url[:40]} » n'a pas un schéma attendu "
        f"({', '.join(SCHEMAS_VALIDES)}). Dans le tableau de bord Turso, c'est "
        f"l'URL de la base qu'il faut copier, pas le nom ni le chemin de "
        f"l'organisation."
    )


def configure() -> bool:
    """Turso est-il demandé ?

    L'URL seule suffit à en décider. Le jeton manquant est une ERREUR et non un
    repli silencieux vers le stockage local : ce repli donnerait exactement le
    comportement qu'on cherche à éviter — une collecte qui tourne et disparaît
    au redémarrage suivant.
    """
    url = os.environ.get(ENV_URL, "").strip()
    if not url:
        return False
    _verifier_url(url)
    if not os.environ.get(ENV_JETON, "").strip():
        raise TursoIndisponible(
            f"{ENV_URL} est défini mais pas {ENV_JETON}. Les deux vont "
            f"ensemble. On ne retombe pas en silence sur le stockage local : "
            f"sur un hébergement sans disque persistant, la collecte "
            f"disparaîtrait au redémarrage suivant sans un message."
        )
    return True


def chemin_cache() -> Path:
    """Où poser la réplique locale.

    `TRADING_DB_PATH` si elle est définie, sinon un fichier temporaire. Le
    contraste avec le stockage local est délibéré : là-bas, un chemin absolu
    dont le répertoire existe est EXIGÉ, parce qu'une faute de frappe y
    coûterait la collecte. Ici, le fichier n'est qu'un cache — le perdre ne
    coûte que le temps d'une resynchronisation — donc aucune de ces précautions
    n'a lieu d'être, et le répertoire peut être créé.

    C'est ce qui rend l'hébergement possible sans disque persistant : plus
    besoin d'un point de montage.
    """
    import tempfile

    brut = os.environ.get("TRADING_DB_PATH", "").strip()
    if brut:
        return Path(brut)
    return Path(tempfile.gettempdir()) / "maxprofit-replique.db"


#: Versions de CPython pour lesquelles `libsql` publie un binaire Windows.
#: Au-delà, pip tente de compiler du Rust et échoue sur le lieur — avec une
#: page d'erreurs de `link.exe` qui ne dit nulle part que le problème est la
#: version de Python.
PYTHON_MAX_MINEUR_WINDOWS = 13


def _diagnostic_libsql() -> str:
    """Dire POURQUOI le pilote manque, pas seulement qu'il manque.

    Le message d'origine renvoyait vers `requirements-broker.txt`, où libsql
    n'a jamais été : il est dans `requirements.txt`. Et sur un Python trop
    récent, l'installer ne peut pas marcher — inutile de le faire essayer.
    """
    trop_recent = (
        sys.platform == "win32"
        and sys.version_info[:2] > (3, PYTHON_MAX_MINEUR_WINDOWS)
    )
    if trop_recent:
        return (
            f"Python {sys.version_info[0]}.{sys.version_info[1]} sur "
            f"Windows : libsql ne publie de binaire que jusqu'à 3."
            f"{PYTHON_MAX_MINEUR_WINDOWS}. pip essaierait de le compiler "
            f"depuis Rust et échouerait sur le lieur. Créez un environnement "
            f"avec Python 3.13 ou 3.12 pour la collecte."
        )
    return "pip install -r requirements.txt"


def ouvrir(chemin_cache: Path):
    """Ouvre la réplique embarquée et tire l'état distant.

    `chemin_cache` est un fichier LOCAL. Il peut vivre sur un disque éphémère :
    la vérité est chez Turso, et la connexion la retire à l'ouverture.
    """
    url = os.environ.get(ENV_URL, "").strip()
    jeton = os.environ.get(ENV_JETON, "").strip()

    try:
        import libsql
    except ImportError as erreur:
        raise TursoIndisponible(
            f"Le pilote libsql n'est pas installé ({erreur}).\n"
            f"{_diagnostic_libsql()}\n"
            f"Ou retirez {ENV_URL} du .env pour collecter dans un fichier "
            f"local — les données seront bonnes, simplement pas partagées."
        ) from None

    chemin_cache.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = libsql.connect(str(chemin_cache), sync_url=url, auth_token=jeton)
    except Exception as erreur:                          # noqa: BLE001
        raise TursoIndisponible(
            f"Connexion à Turso impossible : {erreur}. Vérifiez {ENV_URL} et "
            f"{ENV_JETON} — le jeton se régénère avec `turso db tokens create`."
        ) from None

    # `connect` tire déjà l'état distant, mais on le demande explicitement :
    # démarrer sur une réplique périmée ferait rejouer des migrations déjà
    # appliquées et réécrire des données déjà collectées.
    synchroniser(conn, obligatoire=True)
    log.info("Réplique Turso ouverte (cache local : %s)", chemin_cache)
    return conn


def synchroniser(conn, *, obligatoire: bool = False) -> bool:
    """Pousse les écritures locales vers Turso. Retourne True si c'est fait.

    En fonctionnement normal, un échec de synchronisation n'arrête pas la
    collecte : le réseau revient, et les données sont toujours dans la réplique
    locale en attendant. Mais l'échec est journalisé en ERROR, parce qu'une
    réplique qui ne se synchronise plus est une collecte qui ne survivra pas au
    prochain redémarrage — et rien d'autre ne le signalerait.

    `obligatoire=True` à l'ouverture : là, échouer signifie qu'on ne sait pas
    ce que contient la base distante, et continuer serait travailler à l'aveugle.
    """
    sync = getattr(conn, "sync", None)
    if sync is None:
        return False
    try:
        sync()
        return True
    except Exception as erreur:                          # noqa: BLE001
        if obligatoire:
            raise TursoIndisponible(
                f"Synchronisation initiale impossible : {erreur}\n"
                f"\n"
                f"Cause la plus probable : la base a été créée avec l'option "
                f"« Run this database on TursoDB, the Rust rewrite of SQLite » "
                f"ACTIVÉE. TursoDB est un moteur différent, que le pilote "
                f"`libsql` et le mode réplique embarquée ne savent pas piloter. "
                f"Recréez la base avec cet interrupteur ÉTEINT.\n"
                f"\n"
                f"Sinon : jeton expiré ou révoqué, ou URL d'une autre base.\n"
                f"\n"
                f"On ne démarre pas sur une réplique dont on ignore l'état : "
                f"rejouer des migrations déjà appliquées détruirait des données."
            ) from None
        log.error(
            "Synchronisation Turso échouée : %s. Les données restent dans la "
            "réplique locale, mais elles seront perdues si le conteneur "
            "redémarre avant la prochaine synchronisation réussie.", erreur,
        )
        return False


def est_replique(conn) -> bool:
    """Cette connexion doit-elle être synchronisée ?

    Testé par la présence de `sync`, et non par un drapeau que l'appelant
    porterait : un drapeau se désynchronise de la réalité, une méthode non.
    """
    return callable(getattr(conn, "sync", None))
