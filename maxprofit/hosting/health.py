"""
Sonde HTTP pour l'hébergeur (Render, Railway, Fly...).

Deux raisons d'exister, et une seule est technique :

1. Un hébergeur de type « web service » tue un processus qui n'écoute sur aucun
   port. Le collecteur n'a rien d'un serveur web : il faut donc lui adjoindre un
   socket qui écoute sur `$PORT`, sinon la plateforme le redémarre en boucle.
2. Sur les offres gratuites, l'instance s'endort faute de trafic. Un ping
   régulier sur `/health` (UptimeRobot, cron-job.org) la maintient éveillée.

**La sonde ne ment pas.** C'est le point sur lequel je m'écarte du dépôt qui
vous sert de modèle : sa fonction `health_check` renvoie `status: ok` en toutes
circonstances, y compris si la collecte est morte. Une sonde toujours verte est
pire que pas de sonde du tout — la plateforme maintient poliment en vie un
processus qui n'enregistre plus rien, et vous le découvrez quatorze jours plus
tard en constatant que la base est vide. Ici, `/health` interroge réellement
l'état de la collecte et répond **503** quand elle est en panne, ce qui permet à
l'hébergeur de redémarrer et à un moniteur d'alerter.

Le critère de fraîcheur est le battement de coeur écrit en base par le
collecteur (§1, table `uptime`) : c'est la seule preuve qu'il écrit VRAIMENT,
par opposition à « le thread est encore vivant », qu'un blocage sur socket
laisserait vrai indéfiniment.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from aiohttp import web

from maxprofit.store import postgres
from maxprofit.store.db import open_read_only, schema_version
from maxprofit.store.market import MarketReader

log = logging.getLogger("hosting.health")

#: Au-delà de ce silence, la collecte est considérée en panne. Le collecteur
#: bat toutes les 10 s ; 180 s laissent passer une reconnexion avec backoff
#: (qui plafonne à 60 s) sans crier au loup.
SEUIL_PANNE_SEC = 180

#: Le port par défaut n'est pas un défaut « silencieux » au sens du §5 : il ne
#: touche ni à l'argent ni aux données, et l'hébergeur injecte $PORT lui-même.
PORT_PAR_DEFAUT = 10000

#: Secret partagé protégeant `POST /session`. Sans lui, la route n'est pas
#: montée du tout : un point d'entrée qui accepte un jeton de session sans
#: authentification permettrait à quiconque connaît l'URL de détourner la
#: collecte vers un autre compte.
ENV_SECRET_ADMIN = "ADMIN_SECRET"


#: Clé typée : `app["etat"]` en chaîne libre est déprécié par aiohttp et
#: n'attrape pas les fautes de frappe.
ETAT: "web.AppKey[EtatCollecte]" = web.AppKey("etat")


class EtatCollecte:
    """Vue en lecture seule de l'état de la collecte, pour la sonde.

    La connexion est ouverte paresseusement : au tout premier démarrage, la
    base n'existe pas encore quand le serveur HTTP se lance. Elle est ouverte en
    `mode=ro`, donc la sonde ne peut pas modifier ce qu'elle observe.
    """

    def __init__(self, db: Path):
        self.db = db
        self._reader: MarketReader | None = None
        self.demarre_sec = int(time.time())

    def _lecteur(self) -> MarketReader | None:
        if self._reader is None:
            # `is_file()` est la bonne question pour SQLite et pour une
            # réplique, qui sont des fichiers. Elle n'a aucun sens pour
            # PostgreSQL : il n'y a rien sur le disque, et la sonde restait
            # rouge sur « démarrage » pendant que la collecte écrivait
            # normalement. L'hébergeur voyait un service en panne.
            if not postgres.configure() and not self.db.is_file():
                return None
            try:
                self._reader = MarketReader(open_read_only(self.db))
            except Exception as erreur:
                log.warning("Sonde : base illisible (%s)", erreur)
                return None
        return self._reader

    def rapport(self) -> tuple[bool, dict]:
        """(en_bonne_sante, détails). Ne lève jamais : une sonde qui plante
        prive l'hébergeur de la seule information qu'il sait lire."""
        maintenant = int(time.time())
        base = {
            "uptime_sec": maintenant - self.demarre_sec,
            "db": str(self.db),
            "seuil_panne_sec": SEUIL_PANNE_SEC,
        }

        lecteur = self._lecteur()
        if lecteur is None:
            return False, {**base, "status": "demarrage",
                           "detail": "base pas encore créée par le collecteur"}

        try:
            dernier_battement = lecteur.last_heartbeat_sec()
            compteurs = lecteur.counts()
            version = schema_version(lecteur.conn)
        except Exception as erreur:                      # noqa: BLE001
            # `sqlite3.Error` ne suffit plus depuis que la base peut être
            # PostgreSQL : une erreur psycopg passait au travers et remontait
            # jusqu'au serveur HTTP, qui répondait 500. Or une sonde qui plante
            # prive l'hébergeur de la seule information qu'il sait lire — et le
            # contrat de cette méthode, écrit deux lignes plus haut, est
            # justement de ne JAMAIS lever.
            #
            # La connexion est oubliée : sur une base distante, l'erreur est
            # souvent une connexion morte, et la garder ferait échouer toutes
            # les sondes suivantes de la même façon.
            self._reader = None
            return False, {**base, "status": "erreur", "detail": str(erreur)}

        if dernier_battement is None:
            return False, {**base, "status": "aucun_battement",
                           "detail": "le collecteur n'a encore rien enregistré",
                           "compteurs": compteurs}

        age = maintenant - dernier_battement
        sain = age <= SEUIL_PANNE_SEC
        return sain, {
            **base,
            "status": "ok" if sain else "collecte_arretee",
            "dernier_battement_sec": dernier_battement,
            "age_battement_sec": age,
            "schema_version": version,
            "compteurs": compteurs,
        }


async def health_check(request: web.Request) -> web.Response:
    """Répond 200 si la collecte est vivante, 503 sinon.

    Le 503 est le contrat que comprennent les hébergeurs et les moniteurs
    d'uptime : c'est lui qui déclenche un redémarrage ou une alerte. Renvoyer
    200 en toutes circonstances reviendrait à désactiver les deux.
    """
    etat = request.app[ETAT]
    sain, details = etat.rapport()
    return web.json_response(details, status=200 if sain else 503)


async def ping(request: web.Request) -> web.Response:
    """Sonde de vivacité nue : 200 tant que le processus répond.

    Séparée de `/health` volontairement. Certains hébergeurs redémarrent sur
    tout code != 200 ; si `/health` est branché sur leur health check, une
    collecte en panne provoquerait une boucle de redémarrages qui n'y changerait
    rien. Configurez l'hébergeur sur `/ping`, et le moniteur d'alerte sur
    `/health`.
    """
    return web.Response(text="pong")


INSTALLER = web.AppKey("installer_jeton")


async def poster_session(request: web.Request) -> web.Response:
    """Reçoit un SSID depuis le poste de l'utilisateur.

    C'est la réponse au problème du renouvellement : plutôt que de recopier un
    jeton à la main, l'outil de capture le POSTe directement ici et la collecte
    reprend seule. Le geste manuel se réduit à une commande locale, parce que
    la connexion au broker exige un navigateur et que rien ne peut y changer.

    La comparaison du secret est faite en temps constant. C'est peut-être
    excessif pour un service à trois requêtes par jour, mais une comparaison
    naïve laisse fuir la longueur du préfixe correct, et le coût de faire
    autrement est nul.
    """
    import hmac

    secret = os.environ.get(ENV_SECRET_ADMIN, "").strip()
    if not secret:
        return web.json_response({"erreur": "route désactivée"}, status=404)

    fourni = request.headers.get("X-Admin-Secret", "")
    if not hmac.compare_digest(fourni, secret):
        log.warning("POST /session refusé : secret invalide (depuis %s).",
                    request.remote)
        return web.json_response({"erreur": "non autorisé"}, status=401)

    installer = request.app.get(INSTALLER)
    if installer is None:
        return web.json_response({"erreur": "superviseur absent"}, status=503)

    try:
        corps = await request.json()
        jeton = (corps.get("ssid") or "").strip()
    except Exception:                                    # noqa: BLE001
        return web.json_response({"erreur": "corps JSON attendu"}, status=400)

    try:
        message = await installer(jeton)
    except Exception as erreur:                          # noqa: BLE001
        return web.json_response({"erreur": str(erreur)}, status=400)
    return web.json_response({"ok": True, "message": message})


def build_app(db: Path, installer=None) -> web.Application:
    """Construit l'application, sans l'écouter. Séparé de `start_http_server`
    pour que les tests puissent interroger les vraies routes sans ouvrir de
    port : une sonde testée sur autre chose que ses routes réelles ne prouve
    rien."""
    app = web.Application()
    app[ETAT] = EtatCollecte(db)
    if installer is not None:
        # Fourni à la CONSTRUCTION, pas après : aiohttp déprécie la
        # modification d'une application déjà démarrée, et le fera bientôt
        # échouer.
        app[INSTALLER] = installer
    app.router.add_get("/health", health_check)
    app.router.add_get("/ping", ping)
    app.router.add_get("/", health_check)
    app.router.add_post("/session", poster_session)
    return app


async def start_http_server(db: Path, port: int | None = None,
                            installer=None) -> web.AppRunner:
    """Démarre le serveur HTTP"""
    app = build_app(db, installer=installer)

    runner = web.AppRunner(app)
    await runner.setup()

    port = port if port is not None else int(os.getenv("PORT", PORT_PAR_DEFAUT))
    # 0.0.0.0 et non 127.0.0.1 : dans un conteneur, l'hébergeur teste le port
    # depuis l'extérieur du namespace réseau. Un bind sur localhost passe les
    # tests en local et échoue au déploiement, sans message clair.
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    log.info("Serveur HTTP à l'écoute sur :%d (/health, /ping)", port)
    return runner
