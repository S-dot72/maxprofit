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
    verifier_ssid,
)
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
