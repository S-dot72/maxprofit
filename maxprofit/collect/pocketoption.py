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

--- Ce qui reste à vérifier sur une connexion réelle -----------------------

Deux questions ne se tranchent pas en lisant le code, et `outils/
diagnostic_pocketoption.py` y répond en une minute de connexion :

1. La résolution des horodatages. S'ils sont en secondes ENTIÈRES, plusieurs
   ticks d'une même seconde s'écrasent sur la clé primaire (pair, ts_ms) et le
   `tick_count` des bougies est sous-évalué — ce qui ferait échouer à tort le
   critère de qualité « au moins 5 ticks » du §2.4.
2. Le nombre de paires diffusées simultanément. `change_symbol` pourrait ne
   garder qu'un symbole actif à la fois ; il faudrait alors une rotation, qui
   diviserait la densité de ticks par le nombre de paires.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Iterator, List, Sequence

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

#: Au-delà, on considère que le socket ne répondra pas.
DELAI_CONNEXION_SEC = 30

#: Au-delà de ce nombre de ticks accumulés pour une paire, on compacte le
#: tampon de la bibliothèque : elle y empile sans jamais purger.
SEUIL_COMPACTAGE = 5_000


class SourceIndisponible(BotError):
    """Le broker ou la bibliothèque ne répond pas.

    Distincte de `BotError` parce que le collecteur doit la traiter comme une
    perte de connexion (à réessayer avec backoff) et non comme une erreur de
    programmation (fatale). Voir `FATALES` dans collector.py.
    """


class PocketOptionSource:
    """Source de ticks réelle. Implémente `MarketDataSource`.

    `period_sec` est le timeframe demandé au broker pour l'abonnement. Il ne
    change pas ce qu'on enregistre — on stocke les TICKS, et les bougies sont
    agrégées chez nous (§2.2 : une option à 60 s se règle sur le prix exact à
    la seconde d'expiration, qu'aucune bougie ne contient).
    """

    def __init__(self, demo: bool = True, ssid: str | None = None,
                 period_sec: int = 60, intervalle_lecture_sec: float = 0.25):
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
        self._client = None
        self._globals = None
        self._souscrites: List[str] = []
        self._vus: dict[str, int] = {}
        self._unite: str | None = None   # "sec" ou "ms", détectée au 1er tick
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
        ssid = self._ssid or os.environ.get(ENV_SSID, "").strip() or None

        if ssid is None:
            # Sans SSID, la bibliothèque ouvre une fenêtre PyWebView pour un
            # login manuel. Cela ne peut pas fonctionner dans un conteneur ni
            # sous un service : autant le dire ici plutôt que de laisser le
            # processus se bloquer sur une fenêtre que personne ne verra.
            if not _session_interactive():
                raise SourceIndisponible(
                    f"Aucun SSID fourni et pas de session interactive. En "
                    f"hébergement, récupérez le SSID une fois en local puis "
                    f"passez-le par {ENV_SSID}. Voir "
                    f"outils/diagnostic_pocketoption.py."
                )
            log.info("Aucun SSID : ouverture d'une fenêtre de connexion manuelle.")

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
                log.info("Connecté (démo=%s).", self.demo)
                self._vus.clear()
                return
            time.sleep(0.5)

        raise SourceIndisponible(
            f"Pas de connexion après {DELAI_CONNEXION_SEC} s. SSID expiré ou "
            f"broker injoignable."
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
            self._client = None
        if self._boucle is not None and not self._boucle.is_closed():
            # La boucle que NOUS avons installée. Ne pas la fermer laisserait un
            # descripteur ouvert à chaque reconnexion du collecteur, et il y en a
            # une par coupure réseau sur quatorze jours.
            self._boucle.close()
            self._boucle = None

    # --- paires -------------------------------------------------------------

    def list_pairs(self) -> List[PairInfo]:
        """TOUTES les paires, ouvertes ou non, avec leur payout brut.

        Le filtrage par payout minimal appartient au collecteur : on veut
        l'historique COMPLET en base pour pouvoir rejouer l'éligibilité telle
        qu'elle était à l'instant T (§2.3).
        """
        self._verifier_connexion()
        brut = self._client.GetPairs()
        if brut is None:
            # GetPairs avale ses exceptions et renvoie None. Sans cette
            # traduction, une panne ressemblerait à « aucune paire ouverte » et
            # le collecteur se croirait simplement en week-end.
            raise SourceIndisponible(
                "GetPairs() a renvoyé None : la bibliothèque a rencontré une "
                "erreur qu'elle n'a pas remontée (données de payout absentes "
                "ou socket mort)."
            )

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
            return Tick(pair=nom, ts_ms=self._vers_ms(instant), price=prix)
        except BotError as erreur:
            log.warning("Tick rejeté sur %s : %s", nom, erreur)
            return None

    def _vers_ms(self, brut) -> int:
        """Horodatage serveur -> millisecondes UTC, unité DÉTECTÉE.

        Les deux plages plausibles sont disjointes d'un facteur 1000, ce qui
        rend la détection non ambiguë. L'unité est mémorisée au premier tick :
        si elle change ensuite, c'est que deux sources se mélangent, et cela
        lève plutôt que de produire un historique décalé.
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

        return round(valeur * 1000) if unite == "sec" else round(valeur)

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


def _session_interactive() -> bool:
    import sys
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False
