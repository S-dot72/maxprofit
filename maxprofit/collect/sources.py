"""
Sources de données.

Le collecteur ne connaît QUE l'interface MarketDataSource. Cela isole la partie
fragile (l'API non officielle, qui change sans préavis) du reste du système.
Le jour où la connexion casse ou où vous changez de broker, seul ce fichier bouge.

Deux implémentations :
- SimulatedSource : marche immédiatement, sans compte ni réseau. Sert à valider
  la chaîne complète (agrégation, écriture, reprise après coupure) avant de
  brancher quoi que ce soit de réel.
- PocketOptionSource : squelette à compléter. Voir les notes en bas de fichier.
"""

from __future__ import annotations

import abc
import math
import random
import time
from typing import Iterator, List, Sequence

# Étape 0 : `Tick` et `PairInfo` sont définis UNE seule fois, dans le noyau.
# Ils se valident à la construction : une source qui renvoie un horodatage en
# secondes au lieu de millisecondes lève ici, au point d'entrée, au lieu de
# remplir la base d'un décalage d'un facteur 1000 (spec §5).
from maxprofit.core.types import PairInfo, Tick


class MarketDataSource(abc.ABC):
    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def list_pairs(self) -> List[PairInfo]: ...

    @abc.abstractmethod
    def subscribe(self, pairs: Sequence[str]) -> None: ...

    @abc.abstractmethod
    def stream(self) -> Iterator[Tick]:
        """Générateur bloquant. Doit lever une exception en cas de perte de
        connexion : la boucle du collecteur gère le backoff et la reconnexion."""

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Source simulée
# --------------------------------------------------------------------------- #

class SimulatedSource(MarketDataSource):
    """Marche aléatoire. Les prix n'ont aucun sens économique, c'est voulu :
    si votre stratégie trouve des signaux gagnants ici, elle trouve du bruit."""

    def __init__(self, n_pairs: int = 8, ticks_per_sec: float = 4.0, seed: int = 0):
        self.rng = random.Random(seed)
        self._pairs = [
            PairInfo(f"SIM{i}_otc", True, self.rng.choice([85, 88, 92, 92, 95]))
            for i in range(n_pairs)
        ]
        self._price = {p.name: 1.0 + self.rng.random() for p in self._pairs}
        self._subscribed: List[str] = []
        self._interval = 1.0 / ticks_per_sec

    def connect(self) -> None:
        pass

    def list_pairs(self) -> List[PairInfo]:
        # Les payouts bougent au fil de la journée : on le simule.
        return [
            PairInfo(p.name, True, max(70, min(96, p.payout_pct + self.rng.randint(-2, 2))))
            for p in self._pairs
        ]

    def subscribe(self, pairs: Sequence[str]) -> None:
        self._subscribed = list(pairs)

    def stream(self) -> Iterator[Tick]:
        while True:
            if not self._subscribed:
                time.sleep(0.2)
                continue
            pair = self.rng.choice(self._subscribed)
            drift = self.rng.gauss(0, 1) * 0.00015
            self._price[pair] *= math.exp(drift)
            yield Tick(pair, int(time.time() * 1000), round(self._price[pair], 5))
            time.sleep(self._interval)


# --------------------------------------------------------------------------- #
# Pocket Option
# --------------------------------------------------------------------------- #

class PocketOptionSource(MarketDataSource):
    """
    Squelette. À compléter avec la bibliothèque non officielle que vous retenez.

    Je ne code pas les appels à votre place ici, pour une raison précise : les
    signatures et le format des trames WebSocket de ces bibliothèques changent
    régulièrement et diffèrent d'un fork à l'autre. Du code écrit de mémoire
    aurait l'air correct et échouerait à l'exécution, ou pire, écrirait des
    données mal horodatées que vous ne découvririez qu'au backtest.

    Marche à suivre :
    1. Choisir une bibliothèque, ouvrir une session PyWebView, se connecter
       manuellement sur un compte DÉMO dédié, récupérer le SSID.
    2. Observer les trames WebSocket réelles pendant 2 minutes et noter :
       - le nom du champ horodatage et son unité (secondes ? millisecondes ?
         epoch ou offset ?). C'est le point à vérifier en priorité.
       - le format des messages de prix et de la liste des actifs.
    3. Remplir les quatre méthodes ci-dessous.

    Contrat à respecter :
    - stream() LÈVE une exception si le socket meurt. Ne jamais retourner
      silencieusement : le collecteur ne saurait pas qu'il y a un trou.
    - ts_ms provient du serveur. Si le broker n'envoie pas d'horodatage par tick,
      mesurer une fois l'offset horloge locale / horloge serveur et l'appliquer.
    """

    SESSION_FILE = "session.json"

    def __init__(self, demo: bool = True):
        self.demo = demo
        self._client = None

    def connect(self) -> None:
        # 1. charger SESSION_FILE si présent, sinon ouvrir PyWebView pour un login manuel
        # 2. instancier le client, vérifier que le socket répond
        # 3. persister le SSID
        raise NotImplementedError("Brancher la bibliothèque Pocket Option ici")

    def list_pairs(self) -> List[PairInfo]:
        # Retourner TOUTES les paires, ouvertes ou non, avec leur payout brut.
        # Le filtrage 92%+ se fait dans le collecteur, pas ici : on veut
        # l'historique complet des payouts en base.
        raise NotImplementedError

    def subscribe(self, pairs: Sequence[str]) -> None:
        raise NotImplementedError

    def stream(self) -> Iterator[Tick]:
        raise NotImplementedError

    def close(self) -> None:
        pass
