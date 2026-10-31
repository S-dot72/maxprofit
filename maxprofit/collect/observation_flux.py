"""
Observation PASSIVE des messages de ticks du broker (diagnostic autorisé le
31/10). Elle ne change rien à ce qui est reçu, transmis ou enregistré.

--- Ce qu'elle compte, paire par paire --------------------------------------

  1. REÇUS        les éléments des messages de ticks (`updateStream`) tels
                  qu'ils arrivent sur chaque socket, avant que la bibliothèque
                  ne les lise ;
  2. TRANSMIS     les éléments que notre adaptateur trouve dans le tampon de
                  la bibliothèque (`PocketOptionSource._drainer`) ;
  3. ENREGISTRÉS  les ticks des bougies écrites en base (`tick_count`) : /etat
                  les lit dans la mémoire de la course, pas ici.

Un message de ticks est une liste d'éléments `[actif, instant, prix]`. Trois
situations sont distinguées, car elles n'ont pas le même sens :
  - le message porte PLUSIEURS TICKS (plus d'un élément) ;
  - il porte PLUSIEURS PAIRES ;
  - il porte plusieurs ticks d'une MÊME paire.

La bibliothèque n'ajoutait à son tampon que le PREMIER élément d'un message,
et seulement s'il avait trois champs (pocketoptionapi `ws/client.py`
l. 215-222) : l'élément « en tête ». Mesuré le 31/10, c'était la cause des
paires muettes ; depuis, l'adaptateur lit le lot entier
(`pocketoption.lecture_du_lot_entier`), et « transmis » doit suivre « reçus ». Trouver plusieurs éléments dans un message ne
prouve pas une perte : il faut que des éléments REÇUS ne soient pas TRANSMIS.
La comparaison des niveaux 1 et 2 le montre, ou l'infirme.

--- Garanties ---------------------------------------------------------------

- Le message est remis à l'écouteur de la bibliothèque INCHANGÉ (le même
  objet), dans le même ordre : l'observation en décode une copie.
- Rien n'est consommé dans le tampon : l'adaptateur compte ce qu'il lit déjà.
- Coût : un décodage JSON d'un petit message, sous un verrou de quelques
  microsecondes.
- Toute erreur la DÉSACTIVE définitivement, sans jamais remonter dans la
  collecte ; /etat le dit.
- Mémoire bornée : soixante-dix minutes de compteurs par minute, plus les
  cumuls.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

#: Minutes de compteurs gardées (la comparaison porte sur la dernière heure
#: close).
MINUTES_GARDEES = 70
#: Indices des compteurs par paire.
RECUS, MESSAGES, EN_TETE, TRANSMIS, REPETES = range(5)
NIVEAUX = ("recus", "messages", "en_tete", "transmis", "repetes")
#: Compteurs des messages.
CLES_MESSAGES = ("messages", "plusieurs_ticks", "plusieurs_paires",
                 "meme_paire_repetee", "elements", "malformes",
                 "sans_tete_retenue", "hors_drapeau", "pas_une_liste")


def _vide_messages() -> dict[str, int]:
    return {k: 0 for k in CLES_MESSAGES} | {"max_elements": 0}


class ObservationFlux:
    def __init__(self, horloge=time.time) -> None:
        self._horloge = horloge
        self._verrou = threading.Lock()
        self.actif = True
        self.erreur: str | None = None
        self.depuis = horloge()
        self._minutes: dict[int, dict[str, Any]] = {}
        self._cumul_paires: dict[str, list[int]] = {}
        self._cumul_messages = _vide_messages()
        self._remplacements: dict[str, int] = {}
        self._sockets: dict[int, int] = {}
        #: Dernier (instant, prix) vu par (socket, paire) : un élément
        #: identique au précédent est une RÉPÉTITION (le serveur renvoie-t-il
        #: des ticks inchangés dans ses lots ?).
        self._derniers: dict[tuple[int, str], tuple] = {}
        self.exemple_malforme: str | None = None

    # --- ce que la collecte appelle -----------------------------------------

    def trame(self, client, message) -> None:
        """Un message reçu sur le socket de `client`, AVANT que l'écouteur de
        la bibliothèque ne le traite. N'en modifie rien, ne lève jamais."""
        if not self.actif or not isinstance(message, (bytes, bytearray)):
            return
        try:
            # Le drapeau de la bibliothèque, tel qu'elle va le lire : armé par
            # la trame texte `451-["updateStream",…]` qui précède.
            drapeau = bool(getattr(client, "updateStream", False))
            if not drapeau and not message.startswith(b"[["):
                return            # solde, ordres… : pas un message de ticks
            if getattr(client, "updateClosedDeals", False):
                return            # la bibliothèque le lira comme des ordres
            liste = json.loads(bytes(message).decode("utf-8"))
            self._compter(id(client), drapeau, liste)
        except Exception as erreur:                      # noqa: BLE001
            self._desactiver(erreur)

    def lus(self, paire: str, n: int) -> None:
        """`n` éléments que l'adaptateur vient de lire dans le tampon."""
        if not self.actif or n <= 0:
            return
        try:
            with self._verrou:
                self._paire(self._minute(), paire)[TRANSMIS] += n
                self._cumul(paire)[TRANSMIS] += n
        except Exception as erreur:                      # noqa: BLE001
            self._desactiver(erreur)

    def remplace(self, paire: str) -> None:
        """La bibliothèque a remplacé le tampon de la paire : l'adaptateur le
        relit depuis le début (ses « transmis » peuvent alors dépasser les
        « reçus »)."""
        if not self.actif:
            return
        try:
            with self._verrou:
                self._remplacements[paire] = (
                    self._remplacements.get(paire, 0) + 1)
        except Exception as erreur:                      # noqa: BLE001
            self._desactiver(erreur)

    # --- comptage -----------------------------------------------------------

    def _compter(self, socket: int, drapeau: bool, liste) -> None:
        with self._verrou:
            minute = self._minute()
            messages = self._minutes[minute]["messages"]
            self._sockets[socket] = self._sockets.get(socket, 0) + 1
            for m in (messages, self._cumul_messages):
                if not drapeau:
                    m["hors_drapeau"] += 1
            if not isinstance(liste, list):
                for m in (messages, self._cumul_messages):
                    m["pas_une_liste"] += 1
                return
            par_paire: dict[str, int] = {}
            repetes: dict[str, int] = {}
            malformes = 0
            for element in liste:
                if (isinstance(element, list) and len(element) >= 3
                        and isinstance(element[0], str)):
                    par_paire[element[0]] = par_paire.get(element[0], 0) + 1
                    cle = (socket, element[0])
                    valeur = (element[1], element[2])
                    if self._derniers.get(cle) == valeur:
                        repetes[element[0]] = repetes.get(element[0], 0) + 1
                    self._derniers[cle] = valeur
                else:
                    malformes += 1
                    if self.exemple_malforme is None:
                        self.exemple_malforme = repr(element)[:120]
            # Ce que la bibliothèque retiendra : le premier élément, s'il a
            # exactement trois champs, et seulement si son drapeau est armé.
            tete = None
            if (drapeau and liste and isinstance(liste[0], list)
                    and len(liste[0]) == 3 and isinstance(liste[0][0], str)):
                tete = liste[0][0]
            for m in (messages, self._cumul_messages):
                m["messages"] += 1
                m["elements"] += len(liste)
                m["malformes"] += malformes
                m["max_elements"] = max(m["max_elements"], len(liste))
                if len(liste) > 1:
                    m["plusieurs_ticks"] += 1
                if len(par_paire) > 1:
                    m["plusieurs_paires"] += 1
                if any(n > 1 for n in par_paire.values()):
                    m["meme_paire_repetee"] += 1
                if drapeau and tete is None:
                    m["sans_tete_retenue"] += 1
            for paire, n in par_paire.items():
                for compteur in (self._paire(minute, paire),
                                 self._cumul(paire)):
                    compteur[RECUS] += n
                    compteur[MESSAGES] += 1
                    compteur[REPETES] += repetes.get(paire, 0)
                    if paire == tete:
                        compteur[EN_TETE] += 1

    def _minute(self) -> int:
        minute = int(self._horloge()) // 60 * 60
        if minute not in self._minutes:
            self._minutes[minute] = {"messages": _vide_messages(),
                                     "paires": {}}
            for vieille in [m for m in self._minutes
                            if m < minute - MINUTES_GARDEES * 60]:
                del self._minutes[vieille]
        return minute

    def _paire(self, minute: int, paire: str) -> list[int]:
        return self._minutes[minute]["paires"].setdefault(paire, [0] * 5)

    def _cumul(self, paire: str) -> list[int]:
        return self._cumul_paires.setdefault(paire, [0] * 5)

    def _desactiver(self, erreur: Exception) -> None:
        self.actif = False
        self.erreur = f"{type(erreur).__name__}: {erreur}"[:200]
        log.warning("Observation des messages désactivée : %s", self.erreur)

    # --- ce que /etat lit ---------------------------------------------------

    def instantane(self, debut: int, fin: int) -> dict[str, Any]:
        """Les compteurs des minutes [debut, fin), et les cumuls."""
        with self._verrou:
            paires: dict[str, list[int]] = {}
            messages = _vide_messages()
            minutes = 0
            for minute, seau in self._minutes.items():
                if not debut <= minute < fin:
                    continue
                minutes += 1
                for k, v in seau["messages"].items():
                    messages[k] = (max(messages[k], v) if k == "max_elements"
                                   else messages[k] + v)
                for paire, c in seau["paires"].items():
                    total = paires.setdefault(paire, [0] * 5)
                    for i in range(5):
                        total[i] += c[i]
            return {
                "actif": self.actif, "erreur": self.erreur,
                "depuis": self.depuis, "debut": debut, "fin": fin,
                "minutes_observees": minutes,
                "paires": {p: dict(zip(NIVEAUX, c)) for p, c in paires.items()},
                "messages": messages,
                "cumul_paires": {p: dict(zip(NIVEAUX, c))
                                 for p, c in self._cumul_paires.items()},
                "cumul_messages": dict(self._cumul_messages),
                "remplacements": dict(self._remplacements),
                "exemple_malforme": self.exemple_malforme,
                "sockets": len(self._sockets),
            }


class TramesObservees:
    """Le socket tel que le voit l'écouteur de la bibliothèque : chaque
    message lui parvient INCHANGÉ (le même objet), dans le même ordre, et les
    erreurs du socket remontent telles quelles. Tout le reste est délégué."""

    def __init__(self, ws, client, observation: ObservationFlux) -> None:
        self._ws = ws
        self._client = client
        self._observation = observation
        self._iterateur = None

    def __getattr__(self, nom):
        return getattr(self._ws, nom)

    def __aiter__(self):
        self._iterateur = self._ws.__aiter__()
        return self

    async def __anext__(self):
        message = await self._iterateur.__anext__()
        self._observation.trame(self._client, message)
        return message


#: Une seule observation par processus : les deux sockets (collecteur et
#: courtier) écrivent dans le même tampon de la bibliothèque.
OBSERVATION = ObservationFlux()
