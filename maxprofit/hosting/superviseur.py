"""
Superviseur du collecteur : le redémarrer quand un jeton neuf arrive.

Sans lui, une session expirée arrête la collecte jusqu'à une intervention
manuelle sur le serveur. Avec lui, l'intervention se réduit à envoyer un jeton
depuis un téléphone — le processus reprend seul.

--- Les trois façons dont le collecteur peut s'arrêter ---------------------

Elles appellent trois réponses différentes, et les confondre coûte cher :

    arrêt demandé        (SIGTERM, Ctrl+C)   -> on sort, c'est voulu
    session expirée      (SessionExpiree)    -> on ATTEND un jeton, on ne sort pas
    panne de programmation (autre exception) -> on sort, l'hébergeur redémarre

Le cas du milieu est le seul intéressant. Réessayer ne répare rien : le jeton
est mort, il faut un humain. Mais sortir non plus : l'hébergeur redémarrerait le
conteneur, qui repartirait avec le même jeton mort, en boucle, en consommant des
minutes de calcul. Le superviseur reste donc en vie, sonde au rouge, et attend.

--- Pourquoi la sonde reste rouge pendant l'attente ------------------------

C'est délibéré. Un processus vivant qui n'enregistre rien est exactement ce
contre quoi `/health` a été écrit : elle mesure la fraîcheur du dernier
battement de coeur en base, pas la survie du processus. Pendant l'attente d'un
jeton, la collecte est bel et bien arrêtée, et la sonde doit le dire.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

from maxprofit.collect.collector import Collector, Config
from maxprofit.collect.pocketoption import (
    ENV_FICHIER_SESSION,
    ENV_SSID,
    SessionExpiree,
    ecrire_session,
)
from maxprofit.core.errors import BotError

log = logging.getLogger("hosting.superviseur")

#: Un jeton valide commence par la trame d'authentification socket.io. Ce
#: contrôle ne prouve pas qu'il sera accepté — seul le broker le dira — mais il
#: attrape la faute la plus probable : un copier-coller tronqué ou décoré.
PREFIXE_JETON = '42["auth"'


class Superviseur:
    """Fait tourner le collecteur, l'arrête, le relance sur jeton neuf."""

    def __init__(self, fabriquer_source, cfg: Config, *, alerter=None):
        self._fabriquer_source = fabriquer_source
        self.cfg = cfg
        self._alerter = alerter
        self.collecteur: Collector | None = None
        self._thread: threading.Thread | None = None
        self._jeton_neuf = asyncio.Event()
        self._arret = asyncio.Event()
        self.attente_de_jeton = False
        self.derniere_erreur: str | None = None
        self.demarrages = 0

    # --- cycle de vie -------------------------------------------------------

    def arreter(self) -> None:
        self._arret.set()
        self._jeton_neuf.set()          # débloque une attente en cours
        if self.collecteur is not None:
            self.collecteur.stop()

    async def boucler(self) -> None:
        boucle = asyncio.get_running_loop()

        while not self._arret.is_set():
            fini = asyncio.Event()
            resultat: dict = {}

            def _tourner():
                try:
                    self.collecteur.run()
                except BaseException as erreur:          # noqa: BLE001
                    resultat["erreur"] = erreur
                finally:
                    boucle.call_soon_threadsafe(fini.set)

            self.collecteur = Collector(self._fabriquer_source(), self.cfg)
            self.demarrages += 1
            self._thread = threading.Thread(
                target=_tourner, name=f"collecteur-{self.demarrages}", daemon=True)
            self._thread.start()
            log.info("Collecteur démarré (démarrage n°%d).", self.demarrages)

            await fini.wait()
            self._thread.join(timeout=30)
            erreur = resultat.get("erreur")

            if self._arret.is_set():
                return
            if erreur is None:
                log.info("Le collecteur s'est arrêté de lui-même.")
                return
            if not isinstance(erreur, SessionExpiree):
                log.error("Collecteur arrêté sur une erreur non récupérable : %s",
                          erreur)
                self.derniere_erreur = str(erreur)
                await self._alerte(
                    "❌ <b>Collecte arrêtée</b>\n\n"
                    f"<code>{erreur}</code>\n\n"
                    "Le processus va s'arrêter ; l'hébergeur devrait le relancer."
                )
                raise erreur

            # Session expirée : on attend, on ne sort pas.
            self.derniere_erreur = str(erreur)
            self.attente_de_jeton = True
            log.error("Session expirée. En attente d'un nouveau jeton.")
            await self._alerte(
                "🔑 <b>Jeton de session expiré</b>\n\n"
                "La collecte est <b>arrêtée</b> et reprendra dès réception d'un "
                "jeton neuf.\n\n"
                "Sur votre ordinateur :\n"
                "<code>.\\.venv\\Scripts\\python.exe outils\\capturer_ssid.py "
                "--afficher</code>\n\n"
                "puis renvoyez-moi la valeur avec <code>/ssid …</code>"
            )

            self._jeton_neuf.clear()
            await self._jeton_neuf.wait()
            self.attente_de_jeton = False
            if self._arret.is_set():
                return
            log.info("Jeton reçu : redémarrage de la collecte.")

    # --- réception d'un jeton ----------------------------------------------

    async def installer_jeton(self, jeton: str) -> str:
        """Installe un SSID reçu de l'extérieur et relance la collecte.

        Le jeton va dans l'environnement ET dans le fichier de session :
        l'environnement est prioritaire et prend effet à la reconnexion
        suivante ; le fichier permet de survivre à un redémarrage du processus,
        à condition qu'il soit sur un volume persistant.
        """
        jeton = jeton.strip().strip("`").strip()
        if jeton.startswith(f"{ENV_SSID}="):
            jeton = jeton[len(ENV_SSID) + 1:].strip()

        if not jeton.startswith(PREFIXE_JETON):
            raise BotError(
                f"Un jeton commence par {PREFIXE_JETON} ; celui-ci commence par "
                f"« {jeton[:20]} ». Copiez la ligne entière affichée après "
                f"POCKET_OPTION_SSID=."
            )
        if '"session"' not in jeton:
            raise BotError("Jeton sans champ « session » : il est tronqué.")

        demo = '"isDemo":1' in jeton
        os.environ[ENV_SSID] = jeton
        chemin = ecrire_session(jeton, demo=demo, chemin=self._chemin_session())

        self._jeton_neuf.set()
        if self.collecteur is not None and not self.attente_de_jeton:
            # La collecte tourne encore : on la fait redémarrer pour qu'elle
            # prenne le nouveau jeton, plutôt que d'attendre qu'elle expire.
            self.collecteur.stop()

        compte = "DÉMO" if demo else "RÉEL"
        return (
            f"✅ <b>Jeton installé</b> (compte {compte})\n\n"
            f"Enregistré dans <code>{chemin}</code>\n"
            f"La collecte redémarre."
        )

    def _chemin_session(self) -> Path:
        """À côté de la base, donc sur le volume persistant.

        Le défaut de `chemin_session()` est la racine du projet, ce qui convient
        en local mais pas en conteneur : le système de fichiers d'une image est
        effacé à chaque déploiement, et le jeton serait à redemander à chaque
        mise à jour.
        """
        brut = os.environ.get(ENV_FICHIER_SESSION, "").strip()
        return Path(brut) if brut else self.cfg.db.parent / "session.json"

    # --- état ---------------------------------------------------------------

    def resume(self) -> str:
        if self.attente_de_jeton:
            return ("🔑 En attente d'un jeton de session.\n"
                    f"<code>{self.derniere_erreur}</code>")
        vivant = self._thread is not None and self._thread.is_alive()
        return ("🟢 Collecte en cours." if vivant
                else "🔴 Collecteur arrêté.")

    async def _alerte(self, texte: str) -> None:
        if self._alerter is None:
            return
        try:
            await self._alerter(texte)
        except Exception as erreur:                      # noqa: BLE001
            log.error("Alerte non transmise : %s", erreur)
