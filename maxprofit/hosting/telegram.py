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

Le bot accepte PLUSIEURS opérateurs, et aucun identifiant n'est codé dans la
configuration : on s'inscrit avec un code, par `/start <code>`. Mais tous n'ont
pas les mêmes droits, et c'est essentiel — sans rôles, quiconque s'inscrit
pourrait installer un jeton de session, c'est-à-dire détourner la collecte vers
un autre compte de courtier. Voir `hosting/operateurs.py`.

ADMIN         tout, y compris installer un jeton
OBSERVATEUR   consulter l'état et les paires ; rien d'autre

Un message venant d'un inconnu reçoit la marche à suivre pour s'inscrire, et
rien d'autre : ni état, ni paires, ni indice sur ce que fait le bot.

Un SSID envoyé par Telegram transite par les serveurs de Telegram et reste dans
l'historique de la conversation. Le bot EFFACE donc le message dès qu'il l'a
traité. Cela ne l'efface pas des serveurs de Telegram, et il faut le savoir :
c'est une raison de plus pour n'utiliser qu'un compte de démonstration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from maxprofit.hosting.operateurs import Role, TropDAdmins

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
CLAVIER = [["📊 État", "📈 Paires"], ["🔑 Renouveler le jeton", "❓ Aide"]]

#: Commandes déclarées à Telegram par `setMyCommands`. C'est ce qui fait
#: apparaître le bouton ☰ Menu à gauche de la zone de saisie, et l'autocomplétion
#: quand on tape « / ». Sans cet appel, les commandes fonctionnent mais restent
#: invisibles — il faut les connaître pour s'en servir.
COMMANDES = [
    ("etat", "État de la collecte"),
    ("paires", "Paires actuellement suivies"),
    ("diag", "Ce que la bibliotheque recoit vraiment (admin)"),
    ("ssid", "Installer un nouveau jeton de session (admin)"),
    ("operateurs", "Qui a accès au bot (admin)"),
    ("revoquer", "Retirer un accès (admin)"),
    ("approuver", "Approuver une demande d'accès (admin)"),
    ("refuser", "Refuser une demande d'accès (admin)"),
    ("aide", "Comment ça marche"),
]

AIDE = (
    "<b>Bot d'exploitation</b>\n\n"
    "Il surveille une collecte de données. Il n'envoie aucun signal de trading "
    "et ne connaît aucune stratégie.\n\n"
    "<b>Commandes</b>\n"
    "/etat — état de la collecte\n"
    "/paires — paires actuellement suivies\n"
    "/aide — ce message\n\n"
    "<b>Administration</b>\n"
    "/ssid <i>jeton</i> — installer un nouveau jeton de session\n"
    "/operateurs — qui a accès\n"
    "/revoquer <i>id</i> — retirer un accès\n"
    "/approuver <i>id</i> — approuver une demande\n"
    "/refuser <i>id</i> — refuser une demande"
)

RESERVE_ADMIN = (
    "🔒 <b>Réservé aux administrateurs</b>\n\n"
    "Installer un jeton revient à choisir quel compte de courtier est "
    "collecté : cette commande n'est pas ouverte aux observateurs."
)

INSCRIPTION = (
    "🔒 <b>Accès réservé</b>\n\n"
    "Ce bot surveille une installation privée.\n\n"
    "Si vous avez un code d'accès :\n"
    "<code>/start votre-code</code>\n\n"
    "Sinon, envoyez <code>/start</code> : un administrateur "
    "recevra votre demande."
)

DEMANDE_DEPOSEE = (
    "📨 <b>Demande envoyée</b>\n\n"
    "Un administrateur doit approuver votre accès. Vous serez prévenu ici."
)

ATTENTE_APPROBATION = (
    "⏳ <b>Demande en attente</b>\n\n"
    "Votre accès n'a pas encore été approuvé."
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

    def __init__(self, client: ClientTelegram, annuaire, *,
                 etat: Callable[[], Awaitable[str]],
                 installer_jeton: Callable[[str], Awaitable[str]],
                 paires: Callable[[], Awaitable[str]] | None = None,
                 diagnostic: Callable[[], Awaitable[str]] | None = None):
        self.client = client
        self.annuaire = annuaire
        self._etat = etat
        self._installer_jeton = installer_jeton
        self._paires = paires
        self._diagnostic = diagnostic
        self._offset = 0
        self.actif = True

    async def alerter(self, texte: str) -> None:
        """Message poussé à TOUS les opérateurs. Le canal des pannes.

Un destinataire injoignable — bloqué, compte supprimé — ne doit pas
empêcher les autres d'être prévenus : chaque envoi est isolé.
"""
        destinataires = self.annuaire.destinataires()
        if not destinataires:
            log.warning(
                "Alerte sans destinataire : personne n'est inscrit. Envoyez "
                "/start <code> au bot pour recevoir les alertes. Message "
                "perdu : %s", texte.replace("\n", " ")[:120],
            )
            return
        for chat in destinataires:
            await self._alerter_un(chat, texte)

    async def _alerter_un(self, chat: str, texte: str) -> None:
        try:
            await self.client.envoyer(chat, texte, CLAVIER)
        except Exception as erreur:                      # noqa: BLE001
            # Une alerte qui ne part pas ne doit pas emporter le processus
            # qu'elle signale : ce serait remplacer une panne visible par une
            # panne muette.
            log.error("Alerte non envoyée à %s : %s", chat, erreur)
            if "can't send messages to the bot" in str(erreur):
                # Erreur fréquente et opaque : TELEGRAM_CHAT_ID contient
                # l'identifiant du BOT au lieu de celui de la conversation. Un
                # bot ne s'envoie pas de message à lui-même — donc AUCUNE alerte
                # n'arrivera jamais, y compris celle qui annoncera l'expiration
                # du jeton. La collecte s'arrêterait alors sans que personne ne
                # le sache.
                log.error(
                    "%s est l'identifiant du BOT, pas celui d'une conversation. "
                    "Un bot ne s'envoie pas de message à lui-même : aucune "
                    "alerte ne partira vers cet identifiant. Inscrivez-vous "
                    "plutôt par /start <code>, qui enregistre le bon.", chat,
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
                if "Conflict" in str(erreur):
                    # Deux processus interrogent le même bot. Telegram n'en
                    # sert qu'un, et ils se coupent la parole : les commandes
                    # arrivent au hasard chez l'un ou l'autre. Fréquent pendant
                    # un déploiement, quand l'ancienne instance survit quelques
                    # secondes ; persistant, cela signifie deux services actifs.
                    log.warning(
                        "Un autre processus interroge le même bot Telegram. "
                        "Normal pendant un déploiement ; si cela dure, deux "
                        "instances tournent et se disputent les commandes.")
                else:
                    log.warning(
                        "Telegram injoignable (%s). Nouvel essai dans %ds",
                        erreur, delai)
                await asyncio.sleep(delai)
                delai = min(delai * 2, BACKOFF_MAX_SEC)

    async def _traiter(self, message: dict) -> None:
        chat = str((message.get("chat") or {}).get("id", ""))
        texte = (message.get("text") or "").strip()
        if not texte:
            return

        expediteur = message.get("from") or {}
        nom = (expediteur.get("username")
               or expediteur.get("first_name") or "").strip()

        # L'inscription est le SEUL geste ouvert à un inconnu.
        if texte.startswith("/start"):
            await self._commande_start(chat, texte, nom,
                                       message.get("message_id"))
            return

        role = self.annuaire.role(chat)
        if role is not None and not role.peut_consulter:
            # Demande déposée, pas encore approuvée. Une demande n'est pas un
            # accès : ni état, ni paires.
            await self.client.envoyer(chat, ATTENTE_APPROBATION)
            return
        if role is None:
            # Ni état, ni paires, ni indice sur ce que fait le bot : un inconnu
            # n'apprend rien d'autre que la façon de demander l'accès.
            log.warning("Message ignoré : %s (%s) n'est pas inscrit.", chat, nom)
            await self.client.envoyer(chat, INSCRIPTION)
            return

        if texte.startswith("/diag"):
            # Reserve aux administrateurs : il expose l'interieur du client du
            # broker, pas l'etat de la collecte.
            if not role.peut_installer_jeton:
                await self.client.envoyer(chat, REFUS_ADMIN, CLAVIER)
            elif self._diagnostic is None:
                await self.client.envoyer(chat, "Diagnostic indisponible.",
                                          CLAVIER)
            else:
                await self.client.envoyer(chat, await self._diagnostic(),
                                          CLAVIER)
        elif texte.startswith("/ssid"):
            await self._commande_ssid(chat, texte, message.get("message_id"),
                                      role)
        elif texte.startswith("/operateurs") or texte.startswith("👥"):
            await self._commande_operateurs(chat, role)
        elif texte.startswith("/revoquer"):
            await self._commande_revoquer(chat, texte, role)
        elif texte.startswith("/approuver"):
            await self._commande_approuver(chat, texte, role)
        elif texte.startswith("/refuser"):
            await self._commande_refuser(chat, texte, role)
        elif texte.startswith("/etat") or texte.startswith("📊"):
            await self.client.envoyer(chat, await self._etat(), CLAVIER)
        elif texte.startswith("/paires") or texte.startswith("📈"):
            if self._paires is None:
                await self.client.envoyer(chat, "Information indisponible.", CLAVIER)
            else:
                await self.client.envoyer(chat, await self._paires(), CLAVIER)
        elif texte.startswith("🔑") or texte.startswith("/renouveler"):
            await self.client.envoyer(chat, INSTRUCTIONS_JETON, CLAVIER)
        else:
            await self.client.envoyer(chat, AIDE, CLAVIER)

    # --- inscription --------------------------------------------------------

    async def _commande_start(self, chat: str, texte: str, nom: str,
                              message_id: int | None) -> None:
        """`/start <code>` — la seule porte d'entrée.

Le message est effacé comme celui d'un jeton : un code d'accès qui
traîne dans un historique de conversation finit par être transféré.
"""
        code = texte[len("/start"):].strip()
        deja = self.annuaire.role(chat)

        if not code:
            if deja is not None and not deja.peut_consulter:
                await self.client.envoyer(chat, ATTENTE_APPROBATION)
                return
            if deja is None:
                # Sans code : demande d'accès, soumise à approbation.
                self.annuaire.demander_acces(chat, nom)
                log.info("Demande d'accès de %s (%s).", chat, nom)
                await self.client.envoyer(chat, DEMANDE_DEPOSEE)
                await self._prevenir_admins(chat, nom)
                return
            if deja is not None:
                await self.client.envoyer(
                    chat, f"Vous êtes inscrit comme <b>{deja}</b>.\n\n{AIDE}",
                    CLAVIER)
            else:
                await self.client.envoyer(chat, INSCRIPTION)
            return

        if message_id is not None:
            await self.client.effacer(chat, message_id)

        role = self.annuaire.role_pour_code(code)
        if role is None:
            log.warning("Code d'accès refusé pour %s (%s).", chat, nom)
            await self.client.envoyer(chat, "❌ Code invalide.")
            return

        try:
            self.annuaire.inscrire(chat, role, nom)
        except TropDAdmins as erreur:
            await self.client.envoyer(chat, f"❌ {erreur}")
            return
        await self.client.envoyer(
            chat,
            f"✅ Inscrit comme <b>{role}</b>.\n\n"
            f"Vous recevrez les alertes de la collecte ici.\n\n{AIDE}",
            CLAVIER,
        )

    async def _prevenir_admins(self, demandeur: str, nom: str) -> None:
        """Prévient les administrateurs qu'une demande attend.

        Sans cela, une demande resterait invisible jusqu'à ce qu'un
        administrateur pense à taper `/operateurs` — c'est-à-dire, en pratique,
        jamais.
        """
        admins = self.annuaire.administrateurs()
        if not admins:
            log.warning("Demande de %s sans administrateur pour l'approuver.",
                        demandeur)
            return
        texte = (
            f"👤 <b>Demande d'accès</b>\n\n"
            f"<code>{demandeur}</code>{(' — ' + nom) if nom else ''}\n\n"
            f"Approuver : <code>/approuver {demandeur}</code>\n"
            f"Refuser : <code>/refuser {demandeur}</code>"
        )
        for admin in admins:
            await self._alerter_un(admin, texte)

    # --- administration -----------------------------------------------------

    async def _commande_approuver(self, chat: str, texte: str, role) -> None:
        if not role.peut_installer_jeton:
            await self.client.envoyer(chat, RESERVE_ADMIN, CLAVIER)
            return
        cible = texte[len("/approuver"):].strip()
        if not cible:
            attente = self.annuaire.en_attente()
            if not attente:
                await self.client.envoyer(chat, "Aucune demande en attente.",
                                          CLAVIER)
                return
            lignes = ["<b>Demandes en attente</b>", ""]
            lignes += [f"• <code>{c}</code>{(' — ' + n) if n else ''}"
                       for c, n in attente]
            lignes.append("")
            lignes.append("<code>/approuver identifiant</code>")
            await self.client.envoyer(chat, "\n".join(lignes), CLAVIER)
            return

        if self.annuaire.role(cible) is None:
            await self.client.envoyer(
                chat, f"<code>{cible}</code> n'a pas déposé de demande.", CLAVIER)
            return

        self.annuaire.inscrire(cible, Role.OBSERVATEUR)
        await self.client.envoyer(
            chat, f"✅ <code>{cible}</code> est désormais observateur.", CLAVIER)
        # Prévenir l'intéressé : sinon il attend sans savoir.
        await self._alerter_un(
            cible,
            "✅ <b>Accès approuvé</b>\n\nVous pouvez consulter l'état de la "
            "collecte et recevrez les alertes.\n\n" + AIDE,
        )

    async def _commande_refuser(self, chat: str, texte: str, role) -> None:
        if not role.peut_installer_jeton:
            await self.client.envoyer(chat, RESERVE_ADMIN, CLAVIER)
            return
        cible = texte[len("/refuser"):].strip()
        if not cible:
            await self.client.envoyer(
                chat, "Usage : <code>/refuser identifiant</code>", CLAVIER)
            return
        if self.annuaire.revoquer(cible):
            await self.client.envoyer(chat, f"✅ Demande de <code>{cible}</code> "
                                            f"refusée.", CLAVIER)
        else:
            await self.client.envoyer(chat, f"<code>{cible}</code> n'est pas "
                                            f"inscrit.", CLAVIER)

    async def _commande_operateurs(self, chat: str, role) -> None:
        if not role.peut_installer_jeton:
            await self.client.envoyer(chat, RESERVE_ADMIN, CLAVIER)
            return
        inscrits = self.annuaire.lister()
        lignes = [f"<b>{len(inscrits)} opérateur(s)</b>", ""]
        for identifiant, r, nom in inscrits:
            marque = " ← vous" if identifiant == chat else ""
            lignes.append(f"• <code>{identifiant}</code> — {r}"
                          f"{' (' + nom + ')' if nom else ''}{marque}")
        lignes.append("")
        lignes.append("Retirer un accès : <code>/revoquer identifiant</code>")
        await self.client.envoyer(chat, "\n".join(lignes), CLAVIER)

    async def _commande_revoquer(self, chat: str, texte: str, role) -> None:
        if not role.peut_installer_jeton:
            await self.client.envoyer(chat, RESERVE_ADMIN, CLAVIER)
            return
        cible = texte[len("/revoquer"):].strip()
        if not cible:
            await self.client.envoyer(
                chat, "Usage : <code>/revoquer identifiant</code>", CLAVIER)
            return
        if cible == chat:
            # Se retirer soi-même laisserait peut-être le bot sans aucun
            # administrateur, donc sans personne pour renouveler le jeton.
            await self.client.envoyer(
                chat, "Vous ne pouvez pas révoquer votre propre accès.", CLAVIER)
            return
        if self.annuaire.revoquer(cible):
            await self.client.envoyer(chat, f"✅ Accès retiré à <code>{cible}</code>.",
                                      CLAVIER)
        else:
            await self.client.envoyer(chat, f"<code>{cible}</code> n'est pas inscrit.",
                                      CLAVIER)

    async def _commande_ssid(self, chat: str, texte: str,
                             message_id: int | None, role) -> None:
        if not role.peut_installer_jeton:
            # Le point qui justifie les rôles : installer un jeton, c'est
            # choisir quel compte de courtier est collecté.
            if message_id is not None:
                await self.client.effacer(chat, message_id)
            await self.client.envoyer(chat, RESERVE_ADMIN, CLAVIER)
            return
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
