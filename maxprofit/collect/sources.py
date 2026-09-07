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
    def stream(self) -> Iterator[Tick | None]:
        """Générateur bloquant. Doit lever une exception en cas de perte de
        connexion : la boucle du collecteur gère le backoff et la reconnexion.

        Céder `None` signifie « rien pour l'instant » et rend la main au
        collecteur, qui en profite pour ses tâches à l'heure : battement de
        cœur, écriture, synchronisation. Une source qui bloque sans jamais
        rien céder les suspend toutes — et sur un marché calme, tout s'arrête
        sans qu'aucune erreur ne le dise."""

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

    def stream(self) -> Iterator[Tick | None]:
        while True:
            if not self._subscribed:
                time.sleep(0.2)
                yield None       # « rien pour l'instant », pas la fin du flux
                continue
            pair = self.rng.choice(self._subscribed)
            drift = self.rng.gauss(0, 1) * 0.00015
            self._price[pair] *= math.exp(drift)
            yield Tick(pair, int(time.time() * 1000), round(self._price[pair], 5))
            time.sleep(self._interval)


# --------------------------------------------------------------------------- #
# Pocket Option
# --------------------------------------------------------------------------- #
#
# L'adaptateur réel vit dans son propre module : il est volumineux et sa
# bibliothèque tierce (pandas, pywebview) n'a rien à faire dans le chemin
# d'import du backtest. `pocketoption.py` n'importe rien d'ici, donc pas de
# cycle ; `register` suffit à faire de la classe une MarketDataSource aux yeux
# d'`isinstance` sans l'obliger à hériter.

from maxprofit.collect.pocketoption import (  # noqa: E402
    ENV_SSID,
    PocketOptionSource,
    SourceIndisponible,
)

MarketDataSource.register(PocketOptionSource)

__all__ = [
    "ENV_SSID",
    "MarketDataSource",
    "PairInfo",
    "PocketOptionSource",
    "SimulatedSource",
    "SourceIndisponible",
    "Tick",
]
