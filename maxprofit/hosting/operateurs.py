"""
Qui a le droit de parler au bot, et de faire quoi.

Remplace la liste blanche à un seul identifiant codé dans la configuration. On
ne connaît plus personne d'avance : on s'inscrit auprès du bot avec un CODE.

--- Pourquoi des rôles, et pas seulement une liste -------------------------

« Plusieurs utilisateurs » sans rôles signifierait que quiconque s'inscrit peut
installer un jeton de session — c'est-à-dire détourner la collecte vers un autre
compte de courtier — ou lire l'état de l'infrastructure. Ce n'est pas une
précaution théorique : le code d'accès finit toujours par circuler, dans une
capture d'écran ou un message transféré.

    ADMIN         tout, y compris installer un jeton
    OBSERVATEUR   consulter l'état et les paires ; rien d'autre

Deux codes distincts, tous deux facultatifs. Sans code administrateur, personne
ne peut s'inscrire comme administrateur — le bot reste consultable et le jeton
ne se renouvelle plus que par `POST /session`.

--- Où c'est stocké, et ce que ça implique --------------------------------

Dans un fichier JSON, à côté du fichier de session. Sur un hébergement sans
disque, ce fichier est ÉPHÉMÈRE : après un redéploiement, les inscriptions sont
perdues et chacun doit renvoyer `/start <code>`.

C'est un choix délibéré plutôt qu'un oubli. L'alternative — stocker les
opérateurs dans la base Turso — obligerait le bot à ouvrir sa propre connexion
à la réplique locale, depuis un autre thread que le collecteur. Deux répliques
libSQL sur le même fichier, c'est un risque de corruption pour la seule
commodité de ne pas retaper une commande après un déploiement. La collecte, elle,
ne doit rien perdre.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("hosting.operateurs")

ENV_CODE_ADMIN = "TELEGRAM_ACCESS_CODE"
ENV_CODE_OBSERVATEUR = "TELEGRAM_VIEWER_CODE"

#: Ancien réglage : un identifiant unique en configuration. Toujours honoré —
#: il évite de devoir s'inscrire pour recevoir la première alerte — mais il
#: n'est plus obligatoire, et ce n'est plus le seul moyen.
ENV_CHAT_HISTORIQUE = "TELEGRAM_CHAT_ID"

NOM_FICHIER = "operateurs.json"


class Role(enum.Enum):
    ADMIN = "admin"
    OBSERVATEUR = "observateur"

    @property
    def peut_installer_jeton(self) -> bool:
        return self is Role.ADMIN

    def __str__(self) -> str:
        return self.value


class Annuaire:
    """Les opérateurs inscrits. Sûr entre threads : la boucle Telegram lit et
    écrit pendant que le superviseur diffuse des alertes."""

    def __init__(self, chemin: Path):
        self.chemin = chemin
        self._verrou = threading.Lock()
        self._inscrits: dict[str, dict] = {}
        self._charger()
        self._reprendre_configuration()

    # --- persistance --------------------------------------------------------

    def _charger(self) -> None:
        if not self.chemin.is_file():
            return
        try:
            donnees = json.loads(self.chemin.read_text(encoding="utf-8"))
        except (OSError, ValueError) as erreur:
            # Un annuaire illisible ne doit pas empêcher le bot de démarrer :
            # on repart à vide, et chacun se réinscrit. Perdre des inscriptions
            # est réparable en une commande ; ne plus recevoir d'alerte, non.
            log.warning("Annuaire illisible (%s) : %s", self.chemin, erreur)
            return
        if isinstance(donnees, dict):
            self._inscrits = {str(k): v for k, v in donnees.items()}

    def _enregistrer(self) -> None:
        try:
            self.chemin.parent.mkdir(parents=True, exist_ok=True)
            self.chemin.write_text(
                json.dumps(self._inscrits, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as erreur:
            log.error("Annuaire non enregistré (%s) : %s", self.chemin, erreur)

    def _reprendre_configuration(self) -> None:
        """`TELEGRAM_CHAT_ID` reste admin s'il est défini.

        Sans cela, une première alerte — celle qui dit que la collecte n'a pas
        démarré — n'aurait personne à qui parler tant que personne ne s'est
        inscrit. Or c'est précisément le moment où l'on a besoin d'être prévenu.
        """
        historique = os.environ.get(ENV_CHAT_HISTORIQUE, "").strip()
        if historique and historique not in self._inscrits:
            self._inscrits[historique] = {
                "role": Role.ADMIN.value,
                "nom": "configuration",
                "inscrit_ts_sec": int(time.time()),
            }
            self._enregistrer()

    # --- inscription --------------------------------------------------------

    def role_pour_code(self, code: str) -> Role | None:
        """Quel rôle ce code accorde-t-il ? `None` si aucun.

        La comparaison est en temps constant : sur un canal où l'on peut
        essayer des codes en boucle, une comparaison naïve laisse fuir la
        longueur du préfixe correct.
        """
        import hmac

        code = (code or "").strip()
        if not code:
            return None
        admin = os.environ.get(ENV_CODE_ADMIN, "").strip()
        if admin and hmac.compare_digest(code, admin):
            return Role.ADMIN
        observateur = os.environ.get(ENV_CODE_OBSERVATEUR, "").strip()
        if observateur and hmac.compare_digest(code, observateur):
            return Role.OBSERVATEUR
        return None

    def inscrire(self, chat_id: str, role: Role, nom: str = "") -> None:
        with self._verrou:
            self._inscrits[str(chat_id)] = {
                "role": role.value,
                "nom": nom,
                "inscrit_ts_sec": int(time.time()),
            }
            self._enregistrer()
        log.info("Opérateur inscrit : %s (%s, %s)", chat_id, nom, role)

    def revoquer(self, chat_id: str) -> bool:
        with self._verrou:
            retire = self._inscrits.pop(str(chat_id), None) is not None
            if retire:
                self._enregistrer()
        if retire:
            log.info("Opérateur révoqué : %s", chat_id)
        return retire

    # --- consultation -------------------------------------------------------

    def role(self, chat_id: str) -> Role | None:
        entree = self._inscrits.get(str(chat_id))
        if entree is None:
            return None
        try:
            return Role(entree.get("role"))
        except ValueError:
            return Role.OBSERVATEUR      # rôle inconnu : le moins permissif

    def est_inscrit(self, chat_id: str) -> bool:
        return self.role(chat_id) is not None

    def destinataires(self) -> list[str]:
        """Qui reçoit les alertes : tout le monde.

        Une panne de collecte n'est pas une information confidentielle pour qui
        a déjà le droit de consulter l'état. Restreindre les alertes aux
        administrateurs ferait manquer l'essentiel à ceux qui surveillent.
        """
        with self._verrou:
            return list(self._inscrits)

    def lister(self) -> list[tuple[str, Role, str]]:
        with self._verrou:
            return [
                (chat, self.role(chat) or Role.OBSERVATEUR, e.get("nom", ""))
                for chat, e in sorted(self._inscrits.items())
            ]

    def __len__(self) -> int:
        return len(self._inscrits)


def chemin_annuaire() -> Path:
    """À côté du fichier de session, donc au même endroit que le reste de
    l'état d'exploitation."""
    from maxprofit.collect.pocketoption import chemin_session

    return chemin_session().parent / NOM_FICHIER
