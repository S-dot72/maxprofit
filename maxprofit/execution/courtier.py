"""
Le courtier démo : la seule classe du projet qui appelle `buy()`.

--- ⚠ Pourquoi elle ne vit pas dans `collect` ------------------------------

L'adaptateur de collecte possède déjà un client connecté. Y ajouter une
méthode `passer_ordre()` aurait économisé cent lignes et créé le problème que
tout ce projet essaie d'éviter : le processus qui enregistre les données
serait aussi celui qui trade. Une erreur dans la sonde arrêterait la collecte,
et quatorze jours de série continue ne se rattrapent pas.

Ce sont donc deux clients, deux processus, deux responsabilités. Le coût est
une seconde connexion au broker ; le bénéfice est qu'aucun bug d'exécution ne
peut toucher la collecte.

--- ⚠ Ce qui est verrouillé et ne se configure pas -------------------------

    demo=True                 en dur dans l'appel, pas un paramètre
    exiger_un_compte_demo()   AVANT que le client n'existe

Le second contrôle est le seul qui compte : `demo=True` est une intention,
`isDemo` dans le jeton est un fait. Les deux peuvent diverger — un jeton
capturé sur le compte réel avec `demo=True` connecte au compte réel sans rien
dire. La vérification a lieu avant la construction du client, pour qu'aucun
socket ne s'ouvre sur un compte non démontré démo.
"""

from __future__ import annotations

import logging
import threading
import time
from types import TracebackType

from maxprofit.core.errors import BotError
# `_forcer_region` est privé et importé quand même, sciemment : sans lui la
# bibliothèque choisit une région au hasard parmi celles qu'elle connaît, et
# un client démo finit sur un point d'accès réel. Le dupliquer ici créerait
# deux vérités sur la même question.
from maxprofit.collect.pocketoption import (
    SessionExpiree,
    SourceIndisponible,
    _forcer_region,
    installer_boucle_asyncio,
    verifier_ssid,
)
from maxprofit.core.payout import au_plafond
from maxprofit.core.types import Candle
from maxprofit.execution.garde import Plafonds, exiger_un_compte_demo
from maxprofit.execution.journal import Execution

log = logging.getLogger(__name__)

#: Attente maximale de l'ouverture du socket.
DELAI_CONNEXION_SEC = 30.0
#: Attente maximale d'un premier tick après abonnement. Au-delà, l'actif est
#: muet et l'on préfère le dire que placer un ordre sur un prix inconnu.
DELAI_PREMIER_TICK_SEC = 20.0
#: Marge accordée au dénouement, au-delà de l'échéance elle-même.
MARGE_DENOUEMENT_SEC = 30.0

#: Limite de temps imposée à `get_candles`, qui n'en a aucune.
DELAI_HISTORIQUE_SEC = 15.0


def maintenant_ms() -> int:
    return int(time.time() * 1000)


class CourtierDemo:
    """Un client de trading, sur compte démo, et rien d'autre."""

    def __init__(self, ssid: str, plafonds: Plafonds, period_sec: int = 60):
        # Dans cet ordre : on refuse AVANT de construire quoi que ce soit.
        verifier_ssid(ssid)
        exiger_un_compte_demo(ssid)
        self.ssid = ssid
        self.plafonds = plafonds
        self.period_sec = period_sec
        self._client = None
        self._globals = None
        self._boucle = None
        self.ordres_places = 0
        self._debut_sec = time.monotonic()

    # --- cycle de vie -------------------------------------------------------

    def __enter__(self) -> "CourtierDemo":
        self.connecter()
        return self

    def __exit__(self, *_: object) -> None:
        self.fermer()

    def connecter(self) -> None:
        from pocketoptionapi import global_value
        from pocketoptionapi.stable_api import PocketOption

        self._globals = global_value
        # ⚠ AVANT de construire le client. La bibliothèque appelle
        # `asyncio.get_event_loop()` dans son constructeur, et un thread
        # secondaire n'en a pas : sans cette ligne, « There is no current
        # event loop in thread 'course-plan' ». En local ça passait, le thread
        # principal en possédant une — c'est le déplacement dans un thread qui
        # l'a révélé, en production.
        self._boucle = installer_boucle_asyncio(getattr(self, "_boucle", None))
        _forcer_region(demo=True)
        # `demo=True` en dur : ce n'est pas un paramètre de cette classe.
        self._client = PocketOption(demo=True, ssid=self.ssid)
        self._client.connect()

        limite = time.monotonic() + DELAI_CONNEXION_SEC
        while time.monotonic() < limite:
            if getattr(self._globals, "check_websocket_if_error", False):
                raise SourceIndisponible(
                    f"Erreur WebSocket à la connexion : "
                    f"{self._globals.websocket_error_reason}")
            if self._client.check_connect():
                break
            time.sleep(0.2)
        else:
            raise SourceIndisponible(
                f"Socket non ouvert après {DELAI_CONNEXION_SEC:.0f} s.")

        # Le solde est le SEUL signal d'authentification : le catalogue des
        # actifs est diffusé à tout le monde, authentifié ou non. Sans ce
        # contrôle, on placerait des ordres qui ne partent nulle part.
        limite = time.monotonic() + DELAI_CONNEXION_SEC
        while time.monotonic() < limite:
            if getattr(self._globals, "balance_updated", None) or \
                    getattr(self._globals, "balance", None) is not None:
                log.info("Courtier démo authentifié. Solde : %s",
                         self._globals.balance)
                return
            time.sleep(0.2)
        raise SessionExpiree(
            "Aucun solde reçu : le broker n'a pas authentifié la session. "
            "Le catalogue des actifs arrive quand même, donc tout aurait l'air "
            "normal — mais aucun ordre ne partirait. Recapturez le jeton.")

    def fermer(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception as erreur:      # noqa: BLE001
                log.warning("Fermeture du courtier : %s", erreur)
            self._client = None

    # --- lecture du marché --------------------------------------------------

    def suivre(self, pair: str) -> None:
        """S'abonne à un actif. Sans cela, `prix()` n'a rien à lire."""
        self._exiger_connecte()
        self._client.change_symbol(pair, self.period_sec)
        limite = time.monotonic() + DELAI_PREMIER_TICK_SEC
        while time.monotonic() < limite:
            if self._client.GetTicks(pair):
                return
            time.sleep(0.2)
        raise SourceIndisponible(
            f"{pair} n'a envoyé aucun tick en {DELAI_PREMIER_TICK_SEC:.0f} s. "
            f"Placer un ordre sur un prix inconnu fausserait la mesure du "
            f"glissement, qui est tout l'objet de la sonde.")

    def prix(self, pair: str) -> float:
        """Le dernier prix vu. Lève plutôt que de rendre une valeur périmée."""
        self._exiger_connecte()
        tampon = self._client.GetTicks(pair)
        if not tampon:
            raise SourceIndisponible(f"Aucun tick disponible sur {pair}.")
        try:
            return float(tampon[-1]["price"])
        except (KeyError, TypeError, ValueError) as erreur:
            raise SourceIndisponible(
                f"Dernier tick illisible sur {pair} : {tampon[-1]!r}"
            ) from erreur

    def payout(self, pair: str) -> float:
        """Le payout affiché à cet instant. Lève s'il est indisponible.

        Rendre un défaut ici fausserait E1 — la mesure qui compare précisément
        ce payout à celui que le broker applique.
        """
        self._exiger_connecte()
        valeur = self._client.GetPayout(pair)
        if valeur is None:
            raise SourceIndisponible(
                f"Payout indisponible sur {pair}. La bibliothèque avale ses "
                f"erreurs et rend None : impossible de dire si l'actif est "
                f"fermé ou si la trame a échoué.")
        return float(valeur)

    # --- l'univers : TOUTES les paires au plafond ---------------------------

    def paires_au_plafond(self) -> list[str]:
        """Les actifs OUVERTS qui paient le maximum, tous confondus.

        ⚠ Les quatre paires épinglées sont une décision de COLLECTE, pas de
        trading. S'y limiter pour chercher des signaux réduirait le champ à
        une poignée d'actifs alors que la plateforme en cote près de deux
        cents, dont plusieurs dizaines au plafond à tout instant.

        `GetPairs` lit le catalogue des payouts — diffusé à tout le monde, sans
        abonnement. Aucun coût réseau supplémentaire.
        """
        self._exiger_connecte()
        catalogue = self._client.GetPairs()
        if not catalogue:
            raise SourceIndisponible(
                "Catalogue des paires indisponible. La bibliothèque avale ses "
                "erreurs et rend None : impossible de dire si le socket est "
                "muet ou si la trame a échoué.")
        return sorted(
            nom for nom, info in catalogue.items()
            if info.get("active") and au_plafond(float(info.get("payout", 0))))

    def _decalage_horaire_sec(self) -> int:
        """Le décalage du broker, ARRONDI À L'HEURE — même règle que la collecte.

        Arrondir est ce qui rend la mesure robuste : un fuseau est un nombre
        entier d'heures, la latence réseau se compte en secondes. Sans cet
        arrondi, `get_server_timestamp()` traîne de ~3 s et l'on inscrirait ce
        retard dans chaque horodatage de bougie.
        """
        decalage = self._decalage_ms()
        if decalage is None:
            return 0
        return round(decalage / 3_600_000) * 3600

    def bougies(self, pair: str, count: int = 300) -> list[Candle]:
        """L'historique M1 d'un actif, demandé au broker.

        ⚠ Source DIFFÉRENTE de celle du backtest, et il faut le dire. Le
        backtest lit notre base, qui ne contient que les paires collectées ;
        la course doit pouvoir trader n'importe quel actif au plafond, et le
        broker est alors la seule source. Les deux devraient coïncider — même
        flux, même agrégation à la minute — mais ce n'est pas garanti, et une
        divergence rendrait le direct différent du backtest.

        Pour les quatre paires épinglées, la comparaison est possible : notre
        base a les mêmes minutes. C'est le contrôle à faire avant d'accorder
        du crédit à un résultat obtenu en direct.
        """
        self._exiger_connecte()
        if not self._get_candles_borne(pair):
            raise SourceIndisponible(f"Historique refusé sur {pair}.")
        brut = (self._globals.pairs.get(pair) or {}).get("history") or []
        decalage = self._decalage_horaire_sec()
        sortie: list[Candle] = []
        for ligne in brut:
            try:
                sortie.append(Candle(
                    pair=pair, tf_sec=60,
                    ts_sec=int(ligne["time"]) - decalage,
                    open=float(ligne["open"]), high=float(ligne["high"]),
                    low=float(ligne["low"]), close=float(ligne["close"]),
                    # Le broker ne dit pas combien de ticks composent sa
                    # bougie. On met 1 plutôt que 0 : zéro voudrait dire
                    # « bougie vide », ce qui est faux et ferait écarter la
                    # bougie par le critère de qualité du §2.4.
                    tick_count=1, complete=True))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(sortie, key=lambda c: c.ts_sec)

    def _get_candles_borne(self, pair: str) -> bool:
        """`get_candles` avec une limite de temps, parce qu'elle n'en a pas.

        ⚠ La fonction de la bibliothèque contient un `while True` sans
        condition de sortie : si l'historique n'arrive jamais, elle boucle
        INDÉFINIMENT. Ce n'est pas une exception, c'est un blocage — le thread
        appelant ne rend jamais la main, aucune erreur n'est levée, et un
        superviseur bâti pour rattraper des exceptions ne voit rien du tout.

        C'est arrivé en production : la course est restée verte, sans un seul
        échec au compteur, figée sur son message de démarrage.

        On l'exécute donc dans un thread sacrifiable. S'il ne rend pas la main
        à temps, on abandonne cet actif et on passe au suivant. Le thread
        resté dedans est un coût réel qu'on accepte faute de pouvoir
        l'interrompre — mais il est `daemon`, et le nombre d'actifs est fini.
        """
        fini = threading.Event()
        resultat: dict[str, bool] = {}

        def travailler():
            try:
                resultat["ok"] = bool(
                    self._client.get_candles(pair, 60, count_request=1))
            except Exception as erreur:          # noqa: BLE001
                log.debug("get_candles(%s) : %r", pair, erreur)
                resultat["ok"] = False
            finally:
                fini.set()

        threading.Thread(target=travailler, daemon=True,
                         name=f"historique-{pair}").start()
        if not fini.wait(DELAI_HISTORIQUE_SEC):
            log.warning(
                "Historique de %s non rendu en %.0f s : actif abandonné. La "
                "fonction de la bibliothèque boucle sans limite, on ne peut "
                "que la laisser derrière nous.", pair, DELAI_HISTORIQUE_SEC)
            return False
        return resultat.get("ok", False)

    # --- l'ordre ------------------------------------------------------------

    def placer(self, pair: str, sens: str, expiration_sec: int,
               mise: float | None = None) -> Execution:
        """Passe un ordre et rend son enregistrement, abouti ou refusé.

        Les trois horodatages sont pris ici et nulle part ailleurs : c'est le
        découpage signal -> clic -> acceptation qui est mesuré, et le déporter
        chez l'appelant le rendrait dépendant de ce que fait l'appelant entre
        les deux.
        """
        self._exiger_connecte()
        self._exiger_dans_les_plafonds()
        # ⚠ La mise est un ARGUMENT, et son absence signifie « le plafond ».
        #
        # La première version misait TOUJOURS `plafonds.mise`. La sonde
        # d'exécution, qui mise un montant fixe, n'y voyait rien ; la
        # martingale, elle, plaçait trois fois le même montant et son échelle
        # était purement décorative. Le bogue a abandonné une course en
        # production sans que rien d'autre ne le signale.
        mise = self.plafonds.mise if mise is None else float(mise)
        if not (0 < mise <= self.plafonds.mise):
            raise BotError(
                f"mise hors ]0,{self.plafonds.mise}] : {mise}. Le plafond est "
                f"calculé depuis le plan ; une mise au-dessus veut dire que "
                f"le dimensionnement a dérapé, et mieux vaut refuser l'ordre "
                f"que le placer.")

        signal_ts = maintenant_ms()
        prix_attendu = self.prix(pair)
        payout_flux = self.payout(pair)

        execution = Execution(
            pair=pair, sens=sens, mise=mise,
            signal_ts_ms=signal_ts, prix_attendu=prix_attendu,
            payout_flux_pct=payout_flux, expiration_sec=expiration_sec,
        )

        execution.clic_ts_ms = maintenant_ms()
        try:
            abouti, order_id = self._client.buy(
                mise, pair, sens, expiration_sec)
        except Exception as erreur:          # noqa: BLE001
            # La bibliothèque avale déjà presque tout ; ce qui remonte ici est
            # inattendu. On l'enregistre comme un refus plutôt que de perdre
            # l'ordre : un refus non compté fausse le nombre de trades.
            execution.refus = f"exception : {erreur!r}"
            log.error("Ordre %s %s : %r", pair, sens, erreur)
            return execution

        self.ordres_places += 1
        execution.accepte_ts_ms = maintenant_ms()
        brut = getattr(self._globals, "order_data", None) or {}
        execution.brut = dict(brut) if isinstance(brut, dict) else {"brut": brut}

        if not abouti or order_id is None:
            execution.refus = str(
                execution.brut.get("error") or "refus sans motif")
            execution.accepte_ts_ms = None
            return execution

        execution.accepte = True
        execution.order_id = str(order_id)
        execution.prix_entree = _flottant(execution.brut, "openPrice", "open")
        execution.payout_broker_pct = _flottant(
            execution.brut, "percentProfit", "profit_percent")
        ouverture = _flottant(execution.brut, "openTimestamp")
        if ouverture is not None:
            execution.ouverture_ts_ms = int(ouverture * 1000)
        execution.decalage_broker_ms = self._decalage_ms()
        return execution

    def _decalage_ms(self) -> int | None:
        """De combien l'horloge du broker avance sur la nôtre, maintenant.

        Mesuré à chaque ordre, et non une fois pour toutes : c'est la seule
        façon de comparer `openTimestamp` (horloge broker) à notre horodatage
        d'acceptation. Sans lui, le premier ordre passé annonçait « +7202 s
        d'écart d'expiration » sur un contrat de 60 s — l'horloge du broker
        avance de deux heures.

        `None` si la bibliothèque ne le donne pas : une valeur devinée ferait
        passer un décalage d'horloge pour un délai d'exécution.
        """
        try:
            serveur = self._client.get_server_timestamp()
        except Exception as erreur:          # noqa: BLE001
            log.debug("Horodatage serveur indisponible : %r", erreur)
            return None
        if serveur is None:
            return None
        try:
            return int(float(serveur) * 1000) - maintenant_ms()
        except (TypeError, ValueError):
            return None

    def denouer(self, execution: Execution) -> Execution:
        """Attend le dénouement et complète l'enregistrement.

        Bloque jusqu'à l'échéance plus une marge. La bibliothèque rend
        `(None, "unknown")` au bout de soixante secondes sans réponse : on
        enregistre « unknown » tel quel plutôt que de deviner, parce qu'un
        résultat deviné contaminerait exactement la mesure qu'on cherche.
        """
        if not execution.accepte or execution.order_id is None:
            return execution
        self._exiger_connecte()

        # ⚠ ATTENDRE L'ÉCHÉANCE AVANT D'INTERROGER.
        #
        # `check_win` de la bibliothèque abandonne au bout de SOIXANTE
        # SECONDES et rend « unknown ». Sur une option de 15 minutes, elle
        # rendrait donc « unknown » à tous les coups : chaque session serait
        # interrompue dès le premier pas, la martingale ne descendrait jamais
        # son échelle, et chaque mise serait perdue — sans une seule erreur.
        #
        # Les 26 ordres d'essai étaient à 60 s d'échéance : la limite tenait
        # tout juste, et le défaut est resté invisible.
        self._attendre_l_echeance(execution)
        profit, statut = self._client.check_win(execution.order_id)
        execution.resultat = statut
        execution.profit = None if profit is None else float(profit)

        deal = None
        # ⚠ `check_win` et le détail de l'ordre ne parlent pas de la même
        # chose. Mesuré au premier ordre perdu : `check_win` a rendu 0, le
        # détail portait -1. Le premier est ce que la position RAPPORTE
        # (rien), le second ce qu'elle a COÛTÉ net (la mise). Le second est
        # celui qu'on veut — il se somme directement en variation de solde —
        # et il écrase le premier quelques lignes plus bas.
        try:
            deal = self._client.get_async_order(execution.order_id)
        except Exception as erreur:          # noqa: BLE001
            log.debug("Détail d'ordre indisponible : %r", erreur)
        if isinstance(deal, dict):
            execution.brut = {**execution.brut, "denouement": deal}
            execution.prix_sortie = _flottant(deal, "closePrice", "close")
            if execution.prix_entree is None:
                execution.prix_entree = _flottant(deal, "openPrice", "open")
            if execution.payout_broker_pct is None:
                execution.payout_broker_pct = _flottant(deal, "percentProfit")
            net = _flottant(deal, "profit")
            if net is not None:
                execution.profit = net
            ferme = _flottant(deal, "closeTimestamp")
            if ferme is not None:
                # La bibliothèque parle en secondes ; on stocke en ms, et le
                # suffixe du champ le dit (§5). Horloge BROKER.
                execution.expiration_ts_ms = int(ferme * 1000)
            ouverture = _flottant(deal, "openTimestamp")
            if ouverture is not None and execution.ouverture_ts_ms is None:
                execution.ouverture_ts_ms = int(ouverture * 1000)
        return execution

    def _attendre_l_echeance(self, execution: Execution) -> None:
        """Dort jusqu'à l'expiration, plus une marge, par petits pas.

        Par petits pas et non d'un bloc : le thread doit pouvoir être
        interrompu, et un sommeil de quinze minutes rendrait un arrêt de
        service muet pendant tout ce temps.
        """
        if execution.accepte_ts_ms is None:
            return
        cible = (execution.accepte_ts_ms / 1000
                 + execution.expiration_sec + MARGE_DENOUEMENT_SEC)
        restant = cible - time.time()
        if restant > 0:
            log.info("Attente du dénouement : %.0f s (échéance %d s).",
                     restant, execution.expiration_sec)
        while True:
            restant = cible - time.time()
            if restant <= 0:
                return
            time.sleep(min(5.0, restant))

    # --- gardes -------------------------------------------------------------

    def _exiger_connecte(self) -> None:
        if self._client is None:
            raise BotError(
                "Courtier non connecté : appelez connecter(), ou utilisez-le "
                "comme gestionnaire de contexte.")

    def _exiger_dans_les_plafonds(self) -> None:
        if self.ordres_places >= self.plafonds.ordres_max:
            raise BotError(
                f"Plafond atteint : {self.ordres_places} ordres. Au-delà, le "
                f"broker limite le débit et la sonde mesurerait une latence "
                f"qu'elle a créée elle-même.")
        ecoule = time.monotonic() - self._debut_sec
        if ecoule >= self.plafonds.duree_max_sec:
            raise BotError(
                f"Durée maximale atteinte : {ecoule / 3600:.1f} h.")


def _flottant(source: dict, *noms: str) -> float | None:
    """Le premier champ présent et convertible, sinon `None`.

    Plusieurs noms parce que cette API n'est pas documentée : mon
    interprétation est une hypothèse. Le champ `brut` du journal garde la
    charge utile entière, donc une lecture ratée ici se répare sans replacer
    un seul ordre.
    """
    for nom in noms:
        if nom in source and source[nom] is not None:
            try:
                return float(source[nom])
            except (TypeError, ValueError):
                continue
    return None
