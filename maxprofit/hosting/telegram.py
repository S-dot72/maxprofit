"""
Bot Telegram d'EXPLOITATION — surveillance et renouvellement du jeton.

**Ce n'est pas l'étape 8.** Le §4 conditionne le bot de signaux à une étape 7
concluante, et cette règle est intacte : ce bot n'émet aucun signal, ne connaît
aucune stratégie et n'a accès à aucun résultat de backtest. Il surveille un
processus et transporte un jeton de session. Le §0 est respecté à la lettre —
« le module Telegram n'a aucune logique métier : il formate et envoie ».

--- Ce qui est automatisable, et ce qui ne l'est pas -----------------------

La capture d'un SSID passe par une connexion navigateur : identifiants, parfois
un CAPTCHA. Dans un conteneur sans écran, c'est impossible. « Renouvellement
automatique » ne peut donc pas vouloir dire que le serveur se reconnecte seul —
sauf à stocker le mot de passe du broker sur la plateforme d'hébergement, ce qui
échangerait un inconvénient de dix secondes contre une exposition permanente, et
casserait au premier CAPTCHA.

Ce que ce bot automatise, c'est tout le reste :

    le serveur détecte l'expiration  -> alerte poussée sur votre téléphone
    vous capturez le jeton en local  -> dix secondes, une commande
    vous l'envoyez par /ssid         -> le serveur reprend seul la collecte

Plus de redéploiement, plus de variable d'environnement à éditer dans un tableau
de bord, plus de surveillance de terminal.

--- Pas de python-telegram-bot --------------------------------------------

L'API Bot de Telegram est du REST : deux points d'entrée suffisent ici,
`getUpdates` en longue attente et `sendMessage`. `aiohttp` est déjà une
dépendance du projet pour la sonde HTTP. Ajouter `python-telegram-bot` et ses
dépendances transitives pour deux appels irait contre la même logique que le
`requirements.txt` : n'installer que ce qui est réellement importé.

--- Sécurité --------------------------------------------------------------

Deux points, tous deux nécessaires plutôt que prudents.

`TELEGRAM_CHAT_ID` est une LISTE BLANCHE, pas une simple destination. Sans elle,
n'importe qui découvrant le bot pourrait lui injecter un SSID — c'est-à-dire
détourner la collecte vers un autre compte — ou lire l'état de votre
infrastructure. Toute commande venant d'un autre chat est ignorée et
journalisée.

Un SSID envoyé par Telegram transite par les serveurs de Telegram et reste dans
l'historique de la conversation. Le bot EFFACE donc le message dès qu'il l'a
traité. Cela ne l'efface pas des serveurs de Telegram, et il faut le savoir :
c'est une raison de plus pour n'utiliser qu'un compte de démonstration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("hosting.telegram")

API = "https://api.telegram.org/bot{jeton}/{methode}"

#: Durée de la longue attente sur `getUpdates`. Telegram garde la requête
#: ouverte jusqu'à ce qu'un message arrive : c'est du temps réel sans sondage,
#: et sans consommer de CPU.
ATTENTE_LONGUE_SEC = 50

#: Après une erreur réseau, on ne réessaie pas immédiatement — le bot doit
#: survivre à une coupure sans marteler l'API.
BACKOFF_MAX_SEC = 60


class ClientTelegram:
    """Le strict nécessaire de l'API Bot : envoyer, recevoir, effacer."""

    def __init__(self, jeton: str, session: aiohttp.ClientSession):
        self._jeton = jeton
        self._session = session

    async def _appeler(self, methode: str, **params):
        url = API.format(jeton=self._jeton, methode=methode)
        delai = aiohttp.ClientTimeout(total=ATTENTE_LONGUE_SEC + 15)
        async with self._session.post(url, json=params, timeout=delai) as reponse:
            donnees = await reponse.json()
        if not donnees.get("ok"):
            raise RuntimeError(
                f"Telegram a refusé {methode} : "
                f"{donnees.get('description', donnees)}"
            )
        return donnees.get("result")

    async def envoyer(self, chat_id: str, texte: str,
                      clavier: list[list[str]] | None = None) -> None:
        params = {"chat_id": chat_id, "text": texte, "parse_mode": "HTML"}
        if clavier:
            params["reply_markup"] = {
                "keyboard": [[{"text": b} for b in ligne] for ligne in clavier],
                "resize_keyboard": True,
            }
        await self._appeler("sendMessage", **params)

    async def effacer(self, chat_id: str, message_id: int) -> None:
        """Efface un message. Utilisé sur ceux qui portent un SSID.

        Échoue silencieusement : un message trop ancien ou déjà effacé n'est pas
        un incident, et le faire remonter interromprait le traitement du jeton
        justement en train d'être installé.
        """
        try:
            await self._appeler("deleteMessage", chat_id=chat_id,
                                message_id=message_id)
        except Exception as erreur:                      # noqa: BLE001
            log.debug("Message %s non effacé : %s", message_id, erreur)

    async def declarer_commandes(self, commandes) -> None:
        """Fait apparaître le bouton ☰ Menu dans le client Telegram.

        Échoue sans conséquence : un menu absent est un désagrément, pas une
        panne, et le bot doit démarrer même si Telegram est momentanément
        injoignable — c'est justement quand tout va mal qu'on a besoin de ses
        alertes.
        """
        try:
            await self._appeler("setMyCommands", commands=[
                {"command": nom, "description": texte} for nom, texte in commandes
            ])
        except Exception as erreur:                      # noqa: BLE001
            log.warning("Menu non déclaré : %s", erreur)

    async def recevoir(self, depuis: int) -> list[dict]:
        return await self._appeler(
            "getUpdates", offset=depuis, timeout=ATTENTE_LONGUE_SEC,
            allowed_updates=["message"],
        )


#: Clavier affiché sous la zone de saisie. Des libellés, pas des commandes :
#: on tape rarement « /etat » au téléphone.
CLAVIER = [["📊 État", "🔑 Renouveler le jeton"], ["📈 Paires", "❓ Aide"]]

#: Commandes déclarées à Telegram par `setMyCommands`. C'est ce qui fait
#: apparaître le bouton ☰ Menu à gauche de la zone de saisie, et l'autocomplétion
#: quand on tape « / ». Sans cet appel, les commandes fonctionnent mais restent
#: invisibles — il faut les connaître pour s'en servir.
COMMANDES = [
    ("etat", "État de la collecte"),
    ("paires", "Paires actuellement suivies"),
    ("ssid", "Installer un nouveau jeton de session"),
    ("aide", "Comment ça marche"),
]

AIDE = (
    "<b>Bot d'exploitation</b>\n\n"
    "Il surveille la collecte de données. Il n'envoie aucun signal de trading "
    "et ne connaît aucune stratégie.\n\n"
    "<b>Commandes</b>\n"
    "/etat — état de la collecte\n"
    "/ssid <i>jeton</i> — installer un nouveau jeton de session\n"
    "/aide — ce message"
)

INSTRUCTIONS_JETON = (
    "<b>Renouveler le jeton</b>\n\n"
    "Sur votre ordinateur, dans le dossier du projet :\n\n"
    "<code>.\\.venv\\Scripts\\python.exe outils\\capturer_ssid.py --afficher</code>\n\n"
    "Copiez la ligne affichée après <code>POCKET_OPTION_SSID=</code> et "
    "renvoyez-la ici précédée de <code>/ssid</code> :\n\n"
    "<code>/ssid 42[\"auth\",{...}]</code>\n\n"
    "J'effacerai votre message aussitôt et la collecte reprendra seule."
)


class BotExploitation:
    """Boucle de réception. Une seule conversation autorisée.

    Les actions sont injectées plutôt que codées ici : ce module formate et
    envoie, il ne décide de rien. C'est ce qui lui permet d'être testé sans
    réseau, et de ne jamais devenir l'endroit où une règle métier se glisse.
    """

    def __init__(self, client: ClientTelegram, chat_autorise: str, *,
                 etat: Callable[[], Awaitable[str]],
                 installer_jeton: Callable[[str], Awaitable[str]],
                 paires: Callable[[], Awaitable[str]] | None = None):
        self.client = client
        self.chat_autorise = str(chat_autorise)
        self._etat = etat
        self._installer_jeton = installer_jeton
        self._paires = paires
        self._offset = 0
        self.actif = True

    async def alerter(self, texte: str) -> None:
        """Message poussé, sans qu'on ait rien demandé. Le canal des pannes."""
        try:
            await self.client.envoyer(self.chat_autorise, texte, CLAVIER)
        except Exception as erreur:                      # noqa: BLE001
            # Une alerte qui ne part pas ne doit pas emporter le processus
            # qu'elle signale : ce serait remplacer une panne visible par une
            # panne muette.
            log.error("Alerte Telegram non envoyée : %s", erreur)
            if "can't send messages to the bot" in str(erreur):
                # Erreur fréquente et opaque : TELEGRAM_CHAT_ID contient
                # l'identifiant du BOT au lieu de celui de la conversation. Un
                # bot ne s'envoie pas de message à lui-même — donc AUCUNE alerte
                # n'arrivera jamais, y compris celle qui annoncera l'expiration
                # du jeton. La collecte s'arrêterait alors sans que personne ne
                # le sache.
                log.error(
                    "TELEGRAM_CHAT_ID = %s est l'identifiant du BOT, pas celui "
                    "de votre conversation. Écrivez à @userinfobot pour obtenir "
                    "le vôtre. En l'état, aucune alerte ne vous parviendra.",
                    self.chat_autorise,
                )

    async def boucler(self) -> None:
        await self.client.declarer_commandes(COMMANDES)
        delai = 1
        while self.actif:
            try:
                for maj in await self.client.recevoir(self._offset):
                    self._offset = maj["update_id"] + 1
                    await self._traiter(maj.get("message") or {})
                delai = 1
            except asyncio.CancelledError:
                raise
            except Exception as erreur:                  # noqa: BLE001
                log.warning("Telegram injoignable (%s). Nouvel essai dans %ds",
                            erreur, delai)
                await asyncio.sleep(delai)
                delai = min(delai * 2, BACKOFF_MAX_SEC)

    async def _traiter(self, message: dict) -> None:
        chat = str((message.get("chat") or {}).get("id", ""))
        texte = (message.get("text") or "").strip()
        if not texte:
            return

        if chat != self.chat_autorise:
            # Liste blanche, pas simple destination : sans elle, quiconque
            # trouve le bot pourrait détourner la collecte vers un autre compte.
            log.warning("Message ignoré : chat %s non autorisé.", chat)
            return

        if texte.startswith("/ssid"):
            await self._commande_ssid(chat, texte, message.get("message_id"))
        elif texte.startswith("/etat") or texte.startswith("📊"):
            await self.client.envoyer(chat, await self._etat(), CLAVIER)
        elif texte.startswith("/paires") or texte.startswith("📈"):
            if self._paires is None:
                await self.client.envoyer(chat, "Information indisponible.", CLAVIER)
            else:
                await self.client.envoyer(chat, await self._paires(), CLAVIER)
        elif texte.startswith("🔑") or texte.startswith("/renouveler"):
            await self.client.envoyer(chat, INSTRUCTIONS_JETON, CLAVIER)
        elif texte.startswith("/start"):
            await self.client.envoyer(
                chat, "Bot d'exploitation en service.\n\n" + AIDE, CLAVIER)
        else:
            await self.client.envoyer(chat, AIDE, CLAVIER)

    async def _commande_ssid(self, chat: str, texte: str,
                             message_id: int | None) -> None:
        jeton = texte[len("/ssid"):].strip()

        # Effacer AVANT de répondre : le jeton ne doit pas rester affiché plus
        # longtemps que nécessaire dans la conversation.
        if message_id is not None:
            await self.client.effacer(chat, message_id)

        if not jeton:
            await self.client.envoyer(chat, INSTRUCTIONS_JETON, CLAVIER)
            return

        try:
            resultat = await self._installer_jeton(jeton)
        except Exception as erreur:                      # noqa: BLE001
            await self.client.envoyer(
                chat, f"❌ Jeton refusé.\n\n<code>{erreur}</code>", CLAVIER)
            return
        await self.client.envoyer(chat, resultat, CLAVIER)
