"""
Adaptateur Pocket Option, au-dessus de PocketOptionAPI-v2.

    https://github.com/Mastaaa1987/PocketOptionAPI-v2

Il n'existe aucune API officielle Pocket Option. Cette bibliothèque est du
reverse-engineering du WebSocket de l'interface web, maintenue par un tiers.
Elle peut cesser de fonctionner sans préavis, et son usage viole probablement
les conditions d'utilisation du broker : compte DÉMO dédié, jamais le compte
principal.

--- Ce que fait cet adaptateur, et pourquoi -------------------------------

La bibliothèque est écrite dans un style qui s'oppose frontalement au principe
directeur de ce projet : elle avale toutes ses exceptions (`except: return
None`) et signale les pannes par des valeurs de retour nulles. Un collecteur
bâti dessus tel quel tournerait des jours en n'enregistrant rien, sans une
seule erreur dans les logs. Cet adaptateur est donc, pour l'essentiel, une
couche qui RETRADUIT le silence en exceptions :

  bibliothèque                        adaptateur
  ----------------------------------  ------------------------------------
  GetPairs() -> None en cas d'erreur  lève SourceIndisponible
  websocket_is_connected passe à 0    stream() lève, le collecteur reconnecte
  check_websocket_if_error = True     stream() lève avec la raison

C'est le contrat exigé par `MarketDataSource` : « stream() LÈVE une exception
si le socket meurt. Ne jamais retourner silencieusement : le collecteur ne
saurait pas qu'il y a un trou. »

--- L'unité des horodatages, détectée et non supposée ----------------------

Le §5 désigne la confusion secondes/millisecondes comme le bug le plus
fréquent de ce type de projet. La bibliothèque manipule des secondes
(`pd.to_datetime(..., unit='s')`), mais rien ne garantit que ce soit vrai de
chaque champ ni que cela le reste.

Plutôt que de supposer, l'adaptateur DÉTERMINE l'unité à partir de l'ordre de
grandeur : un epoch en secondes vaut ~1,7 × 10⁹ et le même en millisecondes
~1,7 × 10¹², deux plages disjointes. La détection est faite sur le premier tick,
journalisée, puis VÉRIFIÉE sur chacun des suivants — un changement d'unité en
cours de flux signalerait un mélange de sources et lève.

--- Le fuseau du broker : mesuré, pas supposé ------------------------------

Pocket Option n'envoie PAS de l'UTC. Sa dernière mesure donne +2 h. Rien ne le
signale : l'horodatage reste un epoch parfaitement plausible, simplement faux de
deux heures, et `ensure_ms` ne peut pas l'attraper.

Ce n'est pas un problème d'affichage. Le collecteur horodate `payouts` et
`uptime` avec l'horloge SYSTÈME, donc en vrai UTC. Des ticks à l'heure du broker
feraient chercher, pour un trade donné, un payout relevé jusqu'à deux heures
APRÈS — du look-ahead sur les payouts, exactement ce que le §2.3 interdit ; et
les trous d'`uptime` seraient détectés au mauvais endroit.

Le décalage est donc MESURÉ contre l'horloge du poste, arrondi à l'heure
entière, et re-vérifié toutes les cinq minutes — si l'horloge du broker suit
l'heure d'été européenne, elle passera de +2 h à +1 h fin octobre, au milieu
d'une collecte de quatorze jours.

--- Ce qui a été vérifié sur une connexion réelle --------------------------

`outils/diagnostic_pocketoption.py` a tranché les deux questions ouvertes, et
les deux réponses sont favorables :

1. Résolution des horodatages : SOUS LA SECONDE (…358.523). Chaque tick a un
   instant distinct, donc aucun écrasement sur la clé primaire (pair, ts_ms) et
   un `tick_count` exact pour le critère de qualité du §2.4.
2. Diffusion simultanée : les quatre paires observées ont émis en parallèle,
   à ~2 ticks/s chacune. Aucune rotation d'abonnement n'est nécessaire.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Iterator, List, Sequence

import json
from pathlib import Path

from maxprofit.core.errors import BotError
from maxprofit.core.timebase import (
    MAX_PLAUSIBLE_MS,
    MAX_PLAUSIBLE_SEC,
    MIN_PLAUSIBLE_MS,
    MIN_PLAUSIBLE_SEC,
)
from maxprofit.core.types import PairInfo, Tick

log = logging.getLogger("collect.pocketoption")

ENV_SSID = "POCKET_OPTION_SSID"
ENV_FICHIER_SESSION = "POCKET_OPTION_SESSION_FILE"

#: Fichier où le SSID est persisté après capture, pour ne pas avoir à le
#: recopier. Il est dans `.gitignore` — c'est un jeton de session complet.
NOM_FICHIER_SESSION = "session.json"

#: Au-delà, on considère que le socket ne répondra pas.
DELAI_CONNEXION_SEC = 30

#: Délai d'attente du catalogue des actifs, poussé par le serveur APRÈS
#: l'ouverture du socket. Généreux à dessein : le confondre avec une panne
#: ferait boucler le collecteur sur son propre démarrage.
DELAI_PAYOUTS_SEC = 25

#: Au-delà de ce nombre de ticks accumulés pour une paire, on compacte le
#: tampon de la bibliothèque : elle y empile sans jamais purger.
SEUIL_COMPACTAGE = 5_000

#: Écart résiduel toléré après retrait des heures entières, lors de la mesure
#: du décalage d'horloge du broker. Couvre la latence réseau et une dérive
#: normale du poste ; au-delà, on ne comprend plus l'horloge et on refuse.
TOLERANCE_HORLOGE_SEC = 90

#: Le décalage est re-vérifié à cet intervalle. Ce n'est pas de la paranoïa :
#: si l'horloge du broker suit l'heure d'été européenne, elle passe de +2 h à
#: +1 h fin octobre. Un décalage mesuré une seule fois au démarrage deviendrait
#: faux d'une heure du jour au lendemain, sur une collecte de quatorze jours.
INTERVALLE_VERIF_HORLOGE_SEC = 300

#: Budget laissé au client EXISTANT pour se rétablir seul après une coupure.
#: Généreux : sa boucle interne réessaie sans qu'on ait rien à faire, et la
#: seule alternative est de tuer le processus. Au-delà, on considère que la
#: session ou le réseau ne reviendront pas d'eux-mêmes.
DELAI_RETABLISSEMENT_SEC = 180

#: Décalage maximal admissible, en heures. Le fuseau le plus extrême de la
#: planète est UTC+14 (Kiribati). Au-delà, ce n'est plus un fuseau : c'est une
#: horloge fausse, un horodatage périmé rejoué, ou un champ mal interprété. Sans
#: cette borne, un arrondi à l'heure entière accepte n'importe quoi — un écart
#: d'un an tombe à 77 secondes de résidu et passerait la seule tolérance.
DECALAGE_MAX_HEURES = 14

#: Nombre de ticks consécutifs rejetés au-delà duquel on considère que le format
#: du flux a changé. Sans ce compteur, un flux devenu illisible ferait tourner le
#: collecteur indéfiniment en ne produisant que des avertissements — vivant aux
#: yeux de la sonde, et sans une ligne en base.
REJETS_AVANT_ALERTE = 100

#: Croissance du nombre de threads au-delà de laquelle on signale une fuite.
#: `PocketOption.connect()` démarre un thread WebSocket à chaque appel sans en
#: garder la référence, et son `disconnect()` échoue à le rejoindre. Le
#: collecteur appelant `connect()` à chaque coupure réseau, les threads
#: s'accumuleraient sur quatorze jours — chacun conservant un socket et écrivant
#: dans les mêmes tampons globaux.
CROISSANCE_THREADS_SUSPECTE = 3


def chemin_session() -> Path:
    """Où vit le SSID capturé.

    Ordre : `POCKET_OPTION_SESSION_FILE`, sinon `session.json` à la racine du
    projet. En hébergement, on passe plutôt par `POCKET_OPTION_SSID` : le
    système de fichiers d'un conteneur est éphémère, un fichier écrit à
    l'exécution disparaît au prochain déploiement.
    """
    brut = os.environ.get(ENV_FICHIER_SESSION, "").strip()
    if brut:
        return Path(brut)
    return Path(__file__).resolve().parents[2] / NOM_FICHIER_SESSION


def lire_session(demo: bool, chemin: Path | None = None) -> str | None:
    """Relit un SSID capturé. `None` si absent, illisible ou pour l'autre type
    de compte.

    Le contrôle du type de compte n'est pas une politesse : un SSID de compte
    réel utilisé en croyant être en démo ferait passer de vrais ordres. Le SSID
    porte lui-même `isDemo`, donc l'erreur est détectable — autant la détecter.
    """
    chemin = chemin or chemin_session()
    if not chemin.is_file():
        return None
    try:
        donnees = json.loads(chemin.read_text(encoding="utf-8"))
        ssid = donnees["ssid"]
        enregistre_demo = bool(donnees["demo"])
    except (OSError, ValueError, KeyError, TypeError) as erreur:
        log.warning("Fichier de session illisible (%s) : %s", chemin, erreur)
        return None
    if enregistre_demo != demo:
        log.warning(
            "Session enregistrée pour un compte %s alors que %s est demandé : "
            "ignorée. Relancez outils/capturer_ssid.py.",
            "démo" if enregistre_demo else "RÉEL",
            "démo" if demo else "RÉEL",
        )
        return None
    return ssid


def ecrire_session(ssid: str, demo: bool, uid: str | None = None,
                   chemin: Path | None = None) -> Path:
    """Persiste le SSID pour que plus personne n'ait à le recopier."""
    chemin = chemin or chemin_session()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps({
        "ssid": ssid,
        "demo": demo,
        "uid": uid,
        "capture_ts_sec": int(time.time()),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        chemin.chmod(0o600)     # sans effet utile sur Windows, correct ailleurs
    except OSError:
        pass
    return chemin


def resoudre_ssid(demo: bool, explicite: str | None = None) -> str | None:
    """D'où vient le SSID, une fois pour toutes.

    Ordre : argument explicite, puis `POCKET_OPTION_SSID`, puis le fichier de
    session écrit par `outils/capturer_ssid.py`.

    Cette fonction existe pour qu'il n'y ait qu'UN endroit qui réponde à la
    question. Le diagnostic avait sa propre version, qui ne regardait que
    l'environnement : il annonçait « aucun SSID » alors que l'adaptateur, lui,
    l'aurait trouvé dans le fichier. C'est l'invariant n°1 en miniature — deux
    implémentations de la même règle divergent toujours.

    L'environnement l'emporte sur le fichier : en hébergement, la plateforme
    injecte le jeton et un `session.json` resté dans l'image ne doit pas le
    remplacer par un périmé.
    """
    return (
        (explicite or "").strip()
        or os.environ.get(ENV_SSID, "").strip()
        or lire_session(demo)
        or None
    )


class HorlogeIncoherente(BotError):
    """L'horloge du broker ne peut pas être rapportée à l'UTC.

    Distincte des autres erreurs parce qu'elle ne doit PAS être traitée comme
    un tick malformé. Un tick illisible isolé se saute ; une horloge
    incompréhensible touche tous les ticks, et les sauter un par un ferait
    tourner le collecteur indéfiniment sans rien enregistrer, en n'écrivant que
    des avertissements. C'est précisément le mode de défaillance que ce projet
    cherche à rendre impossible.
    """


class BrokerInjoignable(BotError):
    """Aucune poignée de main n'aboutit depuis ce processus.

    Volontairement FATALE, et distincte d'une coupure : réessayer ne répare pas
    une adresse bloquée, et la bibliothèque continue de composer toutes les dix
    secondes tant que le processus vit — ce qui ne peut qu'aggraver une
    limitation de débit.

    Le symptôme est caractéristique : « timed out during opening handshake » dès
    la PREMIÈRE tentative d'un processus neuf, sans qu'aucun thread hérité ne
    soit en cause. Mesuré le 6 septembre 2026 : depuis un centre de données, tous
    les essais expiraient ; depuis une connexion résidentielle, quatre minutes
    plus tard, avec le même jeton et le même code, la connexion aboutissait en
    3,5 secondes et les ticks arrivaient.

    Un courtier qui ne souhaite pas être moissonné bloque les plages d'adresses
    des hébergeurs. Aucune quantité de code n'y changera rien.
    """


class RedemarrageRequis(BotError):
    """Une reconnexion exige un processus neuf. Volontairement FATALE.

    `PocketOptionAPI.start_websocket()` se termine par `loop.run_forever()` :
    le thread qu'il occupe ne revient JAMAIS, et il porte sa propre boucle de
    reconnexion. `disconnect()` tente d'arrêter une autre boucle — celle du
    thread principal — donc l'ancienne continue d'appeler le broker
    indéfiniment.

    Chaque reconnexion dans le même processus ajoute donc un thread qui
    n'arrêtera plus jamais de composer. Observé en production : au bout de
    quelques cycles, une dizaine de tentatives simultanées, et le broker ne
    répond plus à personne — « timed out during opening handshake » en boucle.
    Une panne réseau d'une seconde suffisait à condamner la collecte.

    On refuse donc de reconnecter en interne. Le processus meurt, l'hébergeur le
    relance, et l'on repart avec un seul thread. Trente secondes d'arrêt valent
    mieux qu'une spirale dont on ne sort pas.
    """


class SourceIndisponible(BotError):
    """Le broker ou la bibliothèque ne répond pas.

    Le collecteur la traite comme une perte de connexion — à réessayer avec
    backoff — et non comme une erreur de programmation. Elle est donc rattrapée
    AVANT `FATALES` dans la boucle du collecteur : sans cela, héritant de
    `BotError`, elle y serait comprise et tuerait la collecte à la première
    indisponibilité passagère du broker.
    """


class SessionExpiree(SourceIndisponible):
    """Le SSID n'est plus accepté : il faut un humain, pas une nouvelle tentative.

    Distincte d'une coupure réseau, et la distinction est opérationnelle. Une
    coupure se répare toute seule en réessayant ; une session expirée non. Un
    collecteur qui réessaie indéfiniment avec un jeton mort tourne des jours
    sans rien enregistrer, en journalisant « connexion perdue » toutes les
    minutes — vivant aux yeux de l'hébergeur, et inutile.

    Le symptôme observé est particulier : le socket s'ouvre et se déclare
    connecté, mais le catalogue des actifs n'arrive jamais. Le serveur accepte
    la poignée de main et refuse les données.
    """


class PocketOptionSource:
    """Source de ticks réelle. Implémente `MarketDataSource`.

    `period_sec` est le timeframe demandé au broker pour l'abonnement. Il ne
    change pas ce qu'on enregistre — on stocke les TICKS, et les bougies sont
    agrégées chez nous (§2.2 : une option à 60 s se règle sur le prix exact à
    la seconde d'expiration, qu'aucune bougie ne contient).
    """

    def __init__(self, demo: bool = True, ssid: str | None = None,
                 period_sec: int = 60, intervalle_lecture_sec: float = 0.25,
                 delai_payouts_sec: float = DELAI_PAYOUTS_SEC):
        if not demo:
            log.warning(
                "Compte RÉEL demandé. Cette bibliothèque est non officielle et "
                "son usage viole probablement les conditions du broker : "
                "utilisez un compte démo dédié."
            )
        self.demo = demo
        self._ssid = ssid
        self.period_sec = period_sec
        self.intervalle = intervalle_lecture_sec
        self.delai_payouts_sec = delai_payouts_sec
        self._client = None
        self._globals = None
        self._souscrites: List[str] = []
        self._vus: dict[str, int] = {}
        self._unite: str | None = None   # "sec" ou "ms", détectée au 1er tick
        self._decalage_sec: int | None = None    # horloge broker - UTC vrai
        self._rejets_consecutifs = 0
        self._threads_au_repos: int | None = None
        self._prochaine_verif_horloge = 0.0
        self._boucle: asyncio.AbstractEventLoop | None = None

    # --- connexion ----------------------------------------------------------

    def connect(self) -> None:
        # Import différé : `maxprofit` doit rester installable et testable sans
        # pandas ni pywebview, qui n'ont rien à faire dans le noyau ni dans le
        # backtest. Seul ce chemin d'exécution en a besoin.
        try:
            from pocketoptionapi import global_value
            from pocketoptionapi.stable_api import PocketOption
        except ImportError as erreur:
            # Deux causes distinctes, et le message doit permettre de les
            # distinguer : soit la bibliothèque manque, soit elle est là mais
            # une de ses dépendances ne s'importe pas. `stable_api` fait
            # `import webview` au niveau module ; dans un conteneur sans
            # bibliothèques graphiques, c'est CET import qui échoue, alors même
            # que le SSID vient de l'environnement et qu'aucune fenêtre ne
            # serait jamais ouverte.
            raise SourceIndisponible(
                f"Import de PocketOptionAPI-v2 impossible : {erreur}. "
                f"Si la bibliothèque manque : pip install -e '.[pocketoption]'. "
                f"  - si c'est 'webview' ou une autre dépendance qui échoue, "
                f"c'est un problème d'image : elle est importée au niveau "
                f"module même quand le SSID vient de l'environnement."
            ) from None

        self._globals = global_value
        ssid = resoudre_ssid(self.demo, self._ssid)

        if ssid is None:
            # Le SSID est OBLIGATOIRE, y compris en session interactive.
            #
            # La bibliothèque sait ouvrir une fenêtre de connexion quand il
            # manque, mais ce chemin se bloque indéfiniment : son `read_cookies`
            # exige sept cookies simultanés, dont six traceurs tiers (Snapchat,
            # TikTok, Twitter, AppsFlyer). S'il en manque un, la boucle renonce
            # au bout de 250 s SANS fermer la fenêtre, et `webview.start()` ne
            # rend jamais la main. Le processus reste figé, sans un message.
            #
            # Un blocage silencieux est le pire mode de défaillance possible
            # pour un collecteur : on le croit en train de travailler. On refuse
            # donc d'emprunter ce chemin, et la capture du SSID est un geste
            # explicite, fait une fois, par un outil dédié.
            raise SourceIndisponible(
                f"Aucun SSID. Lancez outils/capturer_ssid.py une fois : il "
                f"l'enregistre dans {chemin_session()} et tout le reste le "
                f"relira de là, sans copier-coller. En hébergement, passez "
                f"plutôt par {ENV_SSID}. La connexion par fenêtre intégrée de "
                f"la bibliothèque n'est pas utilisée : elle se bloque sans "
                f"message quand un cookie de traceur manque."
            )

        if self._client is not None:
            # NE PAS créer un second client. Son thread tourne sur une boucle
            # `run_forever()` que `disconnect()` n'arrête pas — il vise une
            # autre boucle — et cette boucle RECONNECTE toute seule. En ouvrir
            # un deuxième n'ajouterait donc pas une connexion : cela ajouterait
            # un concurrent, puis un troisième, jusqu'à ce que le broker cesse
            # de répondre à tout le monde.
            #
            # On attend simplement que le client existant se rétablisse. C'est
            # le contraire de l'intuition — ne rien faire est ici l'action utile.
            self._attendre_retablissement()
            return

        if self._threads_au_repos is None:
            self._threads_au_repos = threading.active_count()

        self._installer_boucle_asyncio()
        self._client = PocketOption(demo=self.demo, ssid=ssid)
        self._client.connect()

        limite = time.monotonic() + DELAI_CONNEXION_SEC
        while time.monotonic() < limite:
            if self._globals.check_websocket_if_error:
                raise SourceIndisponible(
                    f"Erreur WebSocket à la connexion : "
                    f"{self._globals.websocket_error_reason}"
                )
            if self._client.check_connect():
                log.info("Socket ouvert (démo=%s). Attente du catalogue des "
                         "actifs...", self.demo)
                self._vus.clear()
                # Une reconnexion peut enjamber un changement d'heure côté
                # broker : on remesure plutôt que de reconduire l'ancien.
                self._decalage_sec = None
                self._prochaine_verif_horloge = 0.0
                # « Connecté » ne suffit pas : sans le catalogue des actifs, la
                # source ne sait rien faire. On attend donc ici plutôt que de
                # laisser le premier appel échouer.
                paires = self._paires_brutes(self.delai_payouts_sec)
                log.info("Connecté (démo=%s), %d actifs au catalogue.",
                         self.demo, len(paires))
                self._verifier_fuite_de_threads()
                return
            time.sleep(0.5)

        raise BrokerInjoignable(
            f"Aucune connexion au broker après {DELAI_CONNEXION_SEC} s, dès la "
            f"première tentative de ce processus.\n"
            f"\n"
            f"Si le journal montre « timed out during opening handshake » à "
            f"chaque essai, ce n'est pas le jeton : c'est l'adresse. Les "
            f"courtiers bloquent les plages des hébergeurs, et la même "
            f"configuration fonctionne depuis une connexion résidentielle.\n"
            f"\n"
            f"Pour trancher en une minute, lancez depuis chez vous :\n"
            f"    outils/diagnostic_pocketoption.py --duree 25\n"
            f"Si cela marche là et pas ici, aucun changement de code n'y fera "
            f"rien.\n"
            f"\n"
            f"Autres causes possibles, moins probables : SSID expiré "
            f"(recapturez-le), ou panne du broker."
        )

    def _installer_boucle_asyncio(self) -> None:
        """Compatibilité Python 3.12+ : installer une boucle avant la construction.

        `PocketOptionAPI.__init__` et `WebsocketClient.__init__` appellent
        `asyncio.get_event_loop()`. Jusqu'à Python 3.10, cet appel CRÉAIT une
        boucle quand le thread n'en avait pas. Depuis, il ne le fait plus, et
        depuis 3.12 il lève `RuntimeError: There is no current event loop`. La
        bibliothèque a été écrite avant ce changement.

        On installe donc la boucle nous-mêmes, dans le thread qui va construire
        le client. Le thread WebSocket que la bibliothèque démarre ensuite crée
        correctement la sienne (`asyncio.new_event_loop()` dans api.py), donc il
        n'y a rien à faire de ce côté.

        Le cas d'une boucle DÉJÀ EN COURS dans ce thread est refusé plutôt que
        contourné : cela signifierait qu'on appelle ce `connect()` bloquant
        depuis une coroutine, et remplacer la boucle en place casserait le
        serveur HTTP de `maxprofit.hosting.service`.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass                      # cas normal : aucun événement en cours
        else:
            raise SourceIndisponible(
                "connect() a été appelé depuis un thread où une boucle asyncio "
                "tourne déjà. Le collecteur doit tourner dans son propre thread "
                "— c'est ce que fait maxprofit.hosting.service."
            )

        if self._boucle is None or self._boucle.is_closed():
            self._boucle = asyncio.new_event_loop()
        asyncio.set_event_loop(self._boucle)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception as erreur:      # noqa: BLE001 - fermeture au mieux
                log.debug("Fermeture imparfaite : %s", erreur)
            # `self._client` n'est PAS remis à None : son thread survit de toute
            # façon, et l'oublier ferait croire à `connect()` qu'aucun client
            # n'existe — il en ouvrirait un second, ce que tout ce mécanisme
            # cherche à empêcher.
        if self._boucle is not None and not self._boucle.is_closed():
            # La boucle que NOUS avons installée. Ne pas la fermer laisserait un
            # descripteur ouvert à chaque reconnexion du collecteur, et il y en a
            # une par coupure réseau sur quatorze jours.
            self._boucle.close()
            self._boucle = None

    # --- paires -------------------------------------------------------------

    def _paires_brutes(self, delai_sec: float) -> dict:
        """Attend que le relevé de payouts soit disponible.

        L'ouverture du socket et l'arrivée des données de payout sont deux
        événements DISTINCTS : le serveur pousse le catalogue des actifs peu
        après la poignée de main, de façon asynchrone. Entre les deux,
        `GetPayoutData()` renvoie None, `json.loads(None)` lève, et le bare
        `except` de `GetPairs()` transforme cela en None.

        Sans cette attente, le collecteur interroge trop tôt à chaque démarrage
        et conclut que le broker est en panne. Comme il redémarre avec backoff,
        il ne dépasserait jamais cette étape : une course perdue au démarrage
        deviendrait une panne permanente.
        """
        limite = time.monotonic() + delai_sec
        while True:
            self._verifier_connexion()
            brut = self._client.GetPairs()
            if brut:
                return brut
            if time.monotonic() >= limite:
                raise SessionExpiree(
                    f"Socket connecté mais aucune donnée de payout après "
                    f"{delai_sec:.0f} s. Le serveur accepte la poignée de main "
                    f"et refuse les données : le SSID est expiré. Recapturez-le "
                    f"avec outils/capturer_ssid.py, ou envoyez-en un nouveau au "
                    f"bot Telegram avec /ssid."
                )
            time.sleep(0.5)

    def list_pairs(self) -> List[PairInfo]:
        """TOUTES les paires, ouvertes ou non, avec leur payout brut.

        Le filtrage par payout minimal appartient au collecteur : on veut
        l'historique COMPLET en base pour pouvoir rejouer l'éligibilité telle
        qu'elle était à l'instant T (§2.3).
        """
        self._verifier_connexion()
        brut = self._paires_brutes(self.delai_payouts_sec)

        paires: List[PairInfo] = []
        for nom, info in brut.items():
            try:
                paires.append(PairInfo(
                    name=str(nom),
                    is_open=bool(info["active"]),
                    payout_pct=int(info["payout"]),
                ))
            except (KeyError, TypeError, ValueError, BotError) as erreur:
                # Une paire mal formée n'invalide pas le relevé, mais elle est
                # signalée : si le format change, on veut le voir tout de suite
                # plutôt que de collecter un historique incomplet en silence.
                log.warning("Paire ignorée (%s) : %r", erreur, {nom: info})
        if not paires:
            raise SourceIndisponible(
                f"Aucune paire exploitable dans {len(brut)} entrées : le format "
                f"de la bibliothèque a probablement changé."
            )
        return paires

    def subscribe(self, pairs: Sequence[str]) -> None:
        self._verifier_connexion()
        self._souscrites = list(pairs)
        for nom in self._souscrites:
            self._client.change_symbol(nom, self.period_sec)
        log.info("Abonné à %d paire(s).", len(self._souscrites))

    # --- flux ---------------------------------------------------------------

    def stream(self) -> Iterator[Tick]:
        """Générateur bloquant. LÈVE en cas de perte de connexion.

        La bibliothèque n'offre aucun callback : elle empile les ticks dans
        `global_value.pairs[nom]['ticks']` depuis son propre thread. On draine
        ce tampon à intervalle régulier.
        """
        while True:
            self._verifier_connexion()
            produit = False
            for nom in self._souscrites:
                for tick in self._drainer(nom):
                    produit = True
                    yield tick
            if not produit:
                time.sleep(self.intervalle)

    def _drainer(self, nom: str) -> Iterator[Tick]:
        tampon = self._client.GetTicks(nom)
        if not tampon:
            return

        # `tampon` est la liste vive de la bibliothèque, alimentée par le thread
        # WebSocket. On en lit la longueur une fois, puis on ne regarde que
        # cette tranche : un `append` concurrent ajoute APRÈS, donc ne peut ni
        # décaler ni faire disparaître ce qu'on lit.
        vus = self._vus.get(nom, 0)
        fin = len(tampon)
        if fin <= vus:
            return

        for brut in tampon[vus:fin]:
            tick = self._vers_tick(nom, brut)
            if tick is not None:
                yield tick
        self._vus[nom] = fin

        if fin >= SEUIL_COMPACTAGE:
            # La bibliothèque n'purge jamais ce tampon : sans cela, la mémoire
            # croît indéfiniment sur un collecteur qui tourne quatorze jours.
            # `del` d'une tranche s'exécute sans céder le GIL, donc aucun tick
            # ajouté entre-temps n'est perdu — on ne supprime que ce qu'on a lu.
            del tampon[:fin]
            self._vus[nom] = 0

    def _vers_tick(self, nom: str, brut) -> Tick | None:
        try:
            instant, prix = brut["time"], float(brut["price"])
        except (KeyError, TypeError, ValueError) as erreur:
            log.warning("Tick illisible sur %s (%s) : %r", nom, erreur, brut)
            return None
        try:
            tick = Tick(pair=nom, ts_ms=self._vers_ms(instant), price=prix)
        except HorlogeIncoherente:
            # Ne se rattrape pas : elle concerne TOUS les ticks. La laisser
            # remonter arrête le collecteur bruyamment, au lieu de le laisser
            # tourner à vide en n'écrivant que des avertissements.
            raise
        except BotError as erreur:
            log.warning("Tick rejeté sur %s : %s", nom, erreur)
            self._rejets_consecutifs += 1
            if self._rejets_consecutifs >= REJETS_AVANT_ALERTE:
                raise SourceIndisponible(
                    f"{self._rejets_consecutifs} ticks consécutifs rejetés. "
                    f"Le format du flux a probablement changé : le collecteur "
                    f"tournerait sans rien enregistrer. Dernière erreur : "
                    f"{erreur}"
                ) from None
            return None
        self._rejets_consecutifs = 0
        self._threads_au_repos: int | None = None
        return tick

    def _vers_ms(self, brut) -> int:
        """Horodatage serveur -> millisecondes UTC VRAI.

        Deux corrections, toutes deux mesurées et non supposées :

        1. L'UNITÉ. Les deux plages plausibles sont disjointes d'un facteur
           1000, ce qui rend la détection non ambiguë. Mémorisée au premier
           tick ; si elle change ensuite, deux sources se mélangent et cela lève.

        2. LE FUSEAU. Pocket Option n'envoie pas de l'UTC : son horloge serveur
           est décalée (+2 h à la mesure). Rien ne le signale — l'horodatage
           reste un epoch parfaitement plausible, simplement faux de deux
           heures. `ensure_ms` ne peut pas l'attraper.

        Pourquoi ce n'est pas cosmétique. Le collecteur horodate `payouts` et
        `uptime` avec l'horloge SYSTÈME, donc en vrai UTC. Si les ticks
        portaient l'heure du broker, la jointure du §2.3 irait chercher, pour
        un trade donné, un payout relevé jusqu'à deux heures APRÈS — du
        look-ahead sur les payouts, exactement le biais que ce paragraphe
        interdit. La détection des trous d'`uptime` serait décalée d'autant.
        """
        valeur = float(brut)

        if MIN_PLAUSIBLE_SEC <= valeur <= MAX_PLAUSIBLE_SEC:
            unite = "sec"
        elif MIN_PLAUSIBLE_MS <= valeur <= MAX_PLAUSIBLE_MS:
            unite = "ms"
        else:
            raise BotError(
                f"Horodatage {brut!r} hors de toute plage plausible : ni "
                f"secondes ni millisecondes epoch entre 2000 et 2100."
            )

        if self._unite is None:
            self._unite = unite
            log.info(
                "Horodatages du broker détectés en %s (exemple : %r). "
                "Résolution sous-la-seconde : %s.",
                unite, brut, "oui" if valeur != int(valeur) else "NON",
            )
            if unite == "sec" and valeur == int(valeur):
                log.warning(
                    "Les horodatages sont en secondes ENTIÈRES. Plusieurs ticks "
                    "d'une même seconde partageront la clé (pair, ts_ms) et un "
                    "seul survivra : le tick_count des bougies sera sous-évalué "
                    "et le critère de qualité du §2.4 trop sévère."
                )
        elif self._unite != unite:
            raise BotError(
                f"L'unité des horodatages est passée de {self._unite} à {unite} "
                f"(valeur {brut!r}). Deux sources se mélangent."
            )

        secondes = valeur if unite == "sec" else valeur / 1000
        self._caler_horloge(secondes)
        return round((secondes - self._decalage_sec) * 1000)

    def _caler_horloge(self, secondes_broker: float) -> None:
        """Mesure le décalage entre l'horloge du broker et l'UTC vrai.

        Le décalage est arrondi à l'heure ENTIÈRE. C'est ce qui rend la mesure
        robuste : un fuseau est un nombre entier d'heures (ou de demi-heures,
        qu'on refuserait ici), tandis que la latence réseau et la dérive du
        poste se comptent en secondes. Arrondir évite d'inscrire dans les
        données le hasard du premier tick reçu.

        Si le résidu dépasse la tolérance, on refuse : soit l'horloge du poste
        est fausse, soit le broker fait autre chose que ce qu'on croit. Dans les
        deux cas, écrire quand même produirait un historique décalé dont rien
        ne signalerait l'erreur — quatorze jours plus tard, il serait trop tard.
        """
        maintenant = time.monotonic()
        if self._decalage_sec is not None and maintenant < self._prochaine_verif_horloge:
            return

        ecart = secondes_broker - time.time()
        heures = round(ecart / 3600)
        residu = ecart - heures * 3600

        if abs(heures) > DECALAGE_MAX_HEURES:
            raise HorlogeIncoherente(
                f"Décalage d'horloge de {heures:+d} h : impossible. Le fuseau "
                f"le plus extrême est UTC+14. Un tel écart signifie une horloge "
                f"fausse, un horodatage périmé rejoué, ou un champ mal "
                f"interprété — pas un fuseau. Horodatage reçu : "
                f"{secondes_broker:.3f}."
            )

        if abs(residu) > TOLERANCE_HORLOGE_SEC:
            raise HorlogeIncoherente(
                f"Horloge incompréhensible : le broker est à {ecart:+.0f} s de "
                f"l'UTC de ce poste, soit {heures:+d} h et {residu:+.0f} s de "
                f"résidu. Un fuseau est un nombre entier d'heures ; un résidu de "
                f"cette taille signifie que l'horloge de ce poste est fausse, ou "
                f"que le broker n'envoie pas ce qu'on croit. Vérifiez la "
                f"synchronisation horaire avant de collecter."
            )

        nouveau = heures * 3600
        if self._decalage_sec is None:
            self._decalage_sec = nouveau
            if nouveau:
                log.warning(
                    "L'horloge du broker est décalée de %+d h par rapport à "
                    "l'UTC (résidu %+.1f s). Les horodatages sont ramenés en "
                    "UTC vrai avant enregistrement : sans cela, la jointure des "
                    "payouts (§2.3) irait chercher des relevés postérieurs au "
                    "trade.", heures, residu,
                )
            else:
                log.info("Horloge du broker alignée sur l'UTC (résidu %+.1f s).",
                         residu)
        elif nouveau != self._decalage_sec:
            log.warning(
                "Le décalage d'horloge du broker est passé de %+d h à %+d h. "
                "Probable changement d'heure côté serveur. Les ticks suivants "
                "sont corrigés avec la nouvelle valeur.",
                self._decalage_sec // 3600, heures,
            )
            self._decalage_sec = nouveau

        self._prochaine_verif_horloge = maintenant + INTERVALLE_VERIF_HORLOGE_SEC

    def _attendre_retablissement(self) -> None:
        """Attend que le client existant retrouve le broker.

        La bibliothèque reconnecte d'elle-même dans son thread. Notre travail se
        borne donc à patienter et à constater — pas à ouvrir une connexion de
        plus, qui se disputerait le broker avec la précédente.

        Passé le budget, on lève une erreur FATALE plutôt que de boucler : à ce
        stade seul un processus neuf peut repartir sur des bases saines, et
        l'hébergeur sait relancer un processus.
        """
        log.info("Coupure : attente du rétablissement (le client reconnecte "
                 "de lui-même, aucun second client n'est ouvert).")
        limite = time.monotonic() + DELAI_RETABLISSEMENT_SEC

        while time.monotonic() < limite:
            # Le drapeau d'erreur est consommé : il reflète la tentative
            # précédente, pas l'état courant, et le laisser ferait échouer
            # toutes les vérifications suivantes.
            self._globals.check_websocket_if_error = False
            if self._client.check_connect():
                paires = self._paires_brutes(self.delai_payouts_sec)
                log.info("Rétabli (%d actifs au catalogue).", len(paires))
                self._vus.clear()
                self._decalage_sec = None
                self._prochaine_verif_horloge = 0.0
                return
            time.sleep(2.0)

        raise RedemarrageRequis(
            f"Toujours pas de connexion au broker après "
            f"{DELAI_RETABLISSEMENT_SEC} s d'attente. Ouvrir un second client "
            f"n'y changerait rien : la bibliothèque ne sait pas arrêter le "
            f"thread du premier, qui continuerait d'appeler le broker en "
            f"parallèle jusqu'à ce qu'il ne réponde plus à personne. Le "
            f"processus s'arrête ; l'hébergeur le relancera propre."
        )

    def _verifier_fuite_de_threads(self) -> None:
        """Signale l'accumulation de threads WebSocket.

        On ne peut pas l'empêcher depuis l'extérieur : la bibliothèque ne garde
        pas de référence sur le thread qu'elle démarre, et son `disconnect()`
        échoue à le rejoindre (« 'NoneType' object has no attribute 'join' »).
        Fermer avant de rouvrir limite la casse ; ce contrôle rend le reste
        visible plutôt que de le laisser ronger la mémoire en silence.

        Le risque n'est pas seulement la mémoire : `PocketOptionAPI.connect()`
        se termine par une attente active (`while True` avec `except: pass`) du
        premier horodatage serveur. Un thread resté bloqué là consomme un coeur
        entier — sur une petite instance hébergée, deux suffisent à tout figer.
        """
        if self._threads_au_repos is None:
            return
        croissance = threading.active_count() - self._threads_au_repos
        if croissance >= CROISSANCE_THREADS_SUSPECTE:
            log.warning(
                "%d threads de plus qu'au démarrage. La bibliothèque n'arrête "
                "pas ses threads WebSocket à la déconnexion ; certains attendent "
                "activement et consomment du CPU. Si cela continue de croître, "
                "redémarrez le processus — l'hébergeur le fera de toute façon "
                "quand la sonde /health passera au rouge.", croissance,
            )

    @property
    def decalage_horloge_heures(self) -> float | None:
        """Décalage mesuré, en heures. `None` tant qu'aucun tick n'est arrivé."""
        return None if self._decalage_sec is None else self._decalage_sec / 3600

    # --- santé --------------------------------------------------------------

    def _verifier_connexion(self) -> None:
        if self._client is None:
            raise SourceIndisponible("connect() n'a pas été appelé")
        if self._globals.check_websocket_if_error:
            raison = self._globals.websocket_error_reason
            self._globals.check_websocket_if_error = False
            raise SourceIndisponible(f"Erreur WebSocket : {raison}")
        if not self._client.check_connect():
            raise SourceIndisponible("Socket fermé par le broker")
