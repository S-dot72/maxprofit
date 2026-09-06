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
from pathlib import Path

from maxprofit.core.errors import BotError

log = logging.getLogger(__name__)

ENV_URL = "TURSO_DATABASE_URL"
ENV_JETON = "TURSO_AUTH_TOKEN"


class TursoIndisponible(BotError):
    """Turso est demandé mais inutilisable."""


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
            f"Le pilote libsql n'est pas installé ({erreur}). "
            f"pip install -r requirements-broker.txt, ou retirez {ENV_URL} "
            f"pour revenir au stockage local."
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
                f"Synchronisation initiale impossible : {erreur}. On ne démarre "
                f"pas sur une réplique dont on ignore l'état."
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
