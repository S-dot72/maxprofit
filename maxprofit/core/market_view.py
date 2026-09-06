"""
MarketView — le garde-fou structurel contre le look-ahead (spec §2.1).

« La stratégie ne reçoit jamais un DataFrame complet. Elle reçoit un objet
MarketView qui n'expose que view.candles(n) et view.now. L'accès au futur
devient physiquement impossible, pas simplement déconseillé. »

Ce fichier est le point le plus important du noyau. Le raisonnement :

Un backtest qui passe un DataFrame complet à la stratégie repose sur la
discipline du développeur — ne pas écrire `df.iloc[t+1]`, ne pas appeler un
indicateur qui lit la série entière. Cette discipline échoue toujours, et elle
échoue silencieusement : le résultat est spectaculaire et faux. On remplace donc
la discipline par une impossibilité : la stratégie ne détient aucune référence
vers la série complète, seulement une fenêtre déjà tronquée.

Conséquences à respecter par tout moteur qui construit une vue :

- `candles(n)` retourne au plus n bougies, la dernière étant CLÔTURÉE à
  `now_ms`. La bougie en cours de formation n'est jamais visible : à l'instant
  où la stratégie décide, elle n'est pas encore terminée.
- Le tuple retourné est immuable et ne partage pas de structure mutable avec la
  vue : une stratégie ne peut ni corrompre l'historique ni le conserver pour
  reconstituer plus que sa fenêtre.
- `now_ms` est l'instant de décision. Il vaut exactement la clôture de la
  dernière bougie visible.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from maxprofit.core.errors import BotError, LookAheadError
from maxprofit.core.timebase import ensure_ms
from maxprofit.core.types import Candle


@runtime_checkable
class MarketView(Protocol):
    """Tout ce qu'une stratégie peut voir du marché. Rien d'autre n'existe.

    Ce protocole est délibérément minuscule. Chaque méthode ajoutée ici est une
    surface d'attaque supplémentaire pour le look-ahead : avant d'en ajouter
    une, se demander si elle peut retourner quoi que ce soit qui n'existait pas
    à `now_ms`.
    """

    @property
    def pair(self) -> str:
        """Paire observée. Une vue porte une seule paire."""
        ...

    @property
    def now_ms(self) -> int:
        """Instant de décision, en millisecondes UTC. Égal à la clôture de la
        dernière bougie visible."""
        ...

    def candles(self, n: int) -> tuple[Candle, ...]:
        """Les `n` dernières bougies clôturées, de la plus ancienne à la plus
        récente. Retourne moins de `n` éléments si l'historique est plus court :
        la stratégie DOIT vérifier la longueur avant de calculer un indicateur,
        et s'abstenir sinon."""
        ...


class SequenceMarketView:
    """Implémentation par curseur sur une série déjà chargée.

    Utilisée par le backtest, qui possède toute la série et avance le curseur
    bougie par bougie. La stratégie reçoit l'objet mais ne peut pas remonter à
    la série : `_all` n'est jamais exposé et `advance()` appartient au moteur,
    pas au protocole `MarketView`.

    En live, une autre implémentation (tampon circulaire alimenté par le flux)
    satisfera le même protocole. C'est ce qui permet à la stratégie d'être
    littéralement le même code des deux côtés (invariant n°1, spec §0).
    """

    __slots__ = ("_pair", "_all", "_i", "_tf_sec")

    def __init__(self, pair: str, candles: Sequence[Candle], index: int = -1):
        if not pair:
            raise BotError("SequenceMarketView sans paire")
        series = tuple(candles)
        if not series:
            raise BotError("SequenceMarketView sur une série vide")

        tf_sec = series[0].tf_sec
        prev_ts = None
        for c in series:
            if c.pair != pair:
                raise BotError(
                    f"SequenceMarketView({pair}) contient une bougie de {c.pair}. "
                    f"Une vue porte une seule paire."
                )
            if c.tf_sec != tf_sec:
                raise BotError(
                    f"SequenceMarketView({pair}) mélange des timeframes "
                    f"{tf_sec} et {c.tf_sec}."
                )
            if prev_ts is not None and c.ts_sec <= prev_ts:
                raise BotError(
                    f"SequenceMarketView({pair}) : série non strictement "
                    f"croissante ({prev_ts} puis {c.ts_sec}). Un moteur qui "
                    f"trie mal produit du look-ahead."
                )
            prev_ts = c.ts_sec

        self._pair = pair
        self._all = series
        self._tf_sec = tf_sec
        self._i = len(series) - 1 if index < 0 else index
        if not (0 <= self._i < len(series)):
            raise BotError(f"index {index} hors de la série ({len(series)} bougies)")

    # --- protocole MarketView ------------------------------------------------

    @property
    def pair(self) -> str:
        return self._pair

    @property
    def now_ms(self) -> int:
        return self._all[self._i].close_ts_ms

    def candles(self, n: int) -> tuple[Candle, ...]:
        if not isinstance(n, int) or isinstance(n, bool):
            raise BotError(f"candles(n) attend un int, reçu {type(n).__name__}")
        if n <= 0:
            raise BotError(f"candles(n) attend n > 0, reçu {n}")
        start = max(0, self._i + 1 - n)
        return self._all[start : self._i + 1]

    # --- réservé au moteur ---------------------------------------------------
    # Ces membres ne figurent PAS dans le protocole MarketView : une stratégie
    # typée contre MarketView ne les voit pas.

    def advance(self) -> bool:
        """Avance d'une bougie. Retourne False si la série est épuisée."""
        if self._i + 1 >= len(self._all):
            return False
        self._i += 1
        return True

    def at(self, index: int) -> "SequenceMarketView":
        """Nouvelle vue positionnée sur `index`, partageant la même série."""
        return SequenceMarketView(self._pair, self._all, index)

    @property
    def index(self) -> int:
        return self._i

    @property
    def length(self) -> int:
        return len(self._all)

    def __repr__(self) -> str:
        return (
            f"<SequenceMarketView {self._pair} tf={self._tf_sec}s "
            f"i={self._i}/{len(self._all) - 1} now_ms={self.now_ms}>"
        )


def assert_no_look_ahead(view: MarketView, *, tf_sec: int) -> None:
    """Vérifie qu'une vue est cohérente avant de la donner à une stratégie.

    Appelée par les moteurs, pas par les stratégies. Attrape une vue mal
    construite : dernière bougie non clôturée à `now_ms`, ou fenêtre contenant
    une bougie postérieure à l'instant de décision.
    """
    ensure_ms(view.now_ms, what="view.now_ms")
    window = view.candles(2)
    if not window:
        raise LookAheadError("Vue sans aucune bougie visible")
    last = window[-1]
    if last.close_ts_ms != view.now_ms:
        raise LookAheadError(
            f"Vue incohérente sur {last.pair} : la dernière bougie clôture à "
            f"{last.close_ts_ms} mais now_ms={view.now_ms}. La bougie en cours "
            f"de formation ne doit jamais être visible."
        )
    if last.tf_sec != tf_sec:
        raise LookAheadError(
            f"Vue en tf={last.tf_sec}s alors que le moteur attend {tf_sec}s"
        )
    if last.close_ts_ms > view.now_ms:
        raise LookAheadError("Bougie postérieure à l'instant de décision")
    if len(window) == 2 and window[0].close_ts_sec > window[1].ts_sec:
        raise LookAheadError("Bougies chevauchantes dans la fenêtre")
