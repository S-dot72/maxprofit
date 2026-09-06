"""
Types du domaine — le vocabulaire partagé par les trois couches.

Ces objets sont immuables et se valident à la construction. Un `Tick` dont
l'horodatage est en secondes ne peut pas exister : il lève à la construction,
au moment où l'on sait encore d'où il vient, plutôt que de produire un backtest
décalé d'un facteur 1000 (spec §5).

Ils ne contiennent aucune logique de stratégie, aucun accès disque, aucun I/O.
C'est ce qui permet aux couches Collecte, Backtest et Live de partager
exactement les mêmes définitions sans se connaître (spec §0).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from maxprofit.core.errors import BotError
from maxprofit.core.timebase import MS_PER_SEC, ensure_ms, ensure_sec


class Direction(enum.Enum):
    """Sens d'une option binaire."""

    CALL = "CALL"
    PUT = "PUT"

    @property
    def opposite(self) -> "Direction":
        """Utilisé par le test-oracle de symétrie (spec §2.7.3)."""
        return Direction.PUT if self is Direction.CALL else Direction.CALL

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Tick:
    """Une cotation. `ts_ms` provient de l'horloge SERVEUR du broker : une
    expiration à 60 s est calée dessus, pas sur l'horloge locale."""

    pair: str
    ts_ms: int
    price: float

    def __post_init__(self) -> None:
        if not self.pair:
            raise BotError("Tick sans paire")
        ensure_ms(self.ts_ms, what=f"Tick({self.pair}).ts_ms")
        if not (self.price > 0):
            raise BotError(f"Tick({self.pair}) prix invalide : {self.price!r}")


@dataclass(frozen=True, slots=True)
class Candle:
    """Bougie agrégée. `ts_sec` est le DÉBUT de la bougie.

    `complete` = la minute a été observée du début à la fin sans coupure.
    `tick_count` < 5 signale une bougie peu fiable. Le backtest refuse de
    générer un signal si l'une des deux conditions manque dans la fenêtre
    d'indicateurs (spec §2.4) ; c'est `is_usable` qui porte cette règle.
    """

    pair: str
    tf_sec: int
    ts_sec: int
    open: float
    high: float
    low: float
    close: float
    tick_count: int
    complete: bool

    def __post_init__(self) -> None:
        if not self.pair:
            raise BotError("Candle sans paire")
        if self.tf_sec <= 0:
            raise BotError(f"Candle({self.pair}) tf_sec invalide : {self.tf_sec}")
        ensure_sec(self.ts_sec, what=f"Candle({self.pair}).ts_sec")
        if self.ts_sec % self.tf_sec != 0:
            raise BotError(
                f"Candle({self.pair}).ts_sec={self.ts_sec} n'est pas aligné sur "
                f"tf_sec={self.tf_sec}. Une bougie commence sur une frontière."
            )
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise BotError(
                f"Candle({self.pair}@{self.ts_sec}) OHLC incohérent : "
                f"O={self.open} H={self.high} L={self.low} C={self.close}"
            )
        if self.tick_count < 0:
            raise BotError(f"Candle({self.pair}) tick_count négatif")

    @property
    def close_ts_sec(self) -> int:
        """Instant de CLÔTURE : la bougie couvre [ts_sec, close_ts_sec)."""
        return self.ts_sec + self.tf_sec

    @property
    def close_ts_ms(self) -> int:
        return self.close_ts_sec * MS_PER_SEC

    def is_usable(self, *, min_ticks: int = 5) -> bool:
        """Critère de qualité de la spec §2.4. Le seuil est explicite et
        remonte dans le rapport de backtest, il n'est jamais implicite."""
        return self.complete and self.tick_count >= min_ticks


@dataclass(frozen=True, slots=True)
class PairInfo:
    """État d'une paire tel que relevé à un instant donné.

    Le payout est en points de pourcentage entiers (92 = 92 %). Il n'est jamais
    lu « au prix d'aujourd'hui » pour un trade passé : le backtest le rejoint
    par horodatage antérieur le plus proche (spec §2.3).
    """

    name: str
    is_open: bool
    payout_pct: int

    def __post_init__(self) -> None:
        if not self.name:
            raise BotError("PairInfo sans nom")
        if not (0 <= self.payout_pct <= 100):
            raise BotError(f"PairInfo({self.name}) payout hors [0,100] : {self.payout_pct}")

    @property
    def payout_ratio(self) -> float:
        """0.92 pour un payout de 92 %. C'est cette forme qu'utilise le calcul
        d'espérance `p × payout − (1−p)` (spec §3.3)."""
        return self.payout_pct / 100.0


_EMPTY_META: Mapping[str, float] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Signal:
    """Décision d'entrer en position, produite par `Strategy.on_bar`.

    Un signal ne porte PAS de prix d'entrée : la stratégie ne choisit pas son
    prix. Le prix d'entrée est le premier tick disponible à
    `decided_at_ms + latence`, déterminé par le moteur d'exécution, jamais la
    clôture de la bougie de signal (spec §2.2).

    `decided_at_ms` doit être égal à `view.now_ms`. Un signal daté autrement est
    soit du look-ahead, soit un décalage d'horloge : dans les deux cas le moteur
    doit le rejeter.

    `features` porte les valeurs numériques observées AU MOMENT DE LA DÉCISION.
    Elles sont journalisées telles quelles et ne sont jamais recalculées après
    coup — un recalcul sur du code modifié réintroduit du look-ahead (spec §3.1).
    """

    pair: str
    direction: Direction
    decided_at_ms: int
    expiry_sec: int
    features: Mapping[str, float] = field(default=_EMPTY_META)
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.pair:
            raise BotError("Signal sans paire")
        if not isinstance(self.direction, Direction):
            raise BotError(f"Signal.direction doit être un Direction, reçu {self.direction!r}")
        ensure_ms(self.decided_at_ms, what=f"Signal({self.pair}).decided_at_ms")
        if self.expiry_sec <= 0:
            raise BotError(
                f"Signal({self.pair}) expiry_sec doit être > 0, reçu {self.expiry_sec}. "
                f"Aucune valeur par défaut n'est appliquée (spec §5)."
            )
        # Gel des features : une stratégie ne doit pas pouvoir muter après coup
        # ce qui sera journalisé.
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))

    @property
    def expires_at_ms(self) -> int:
        """Échéance théorique, à partir de l'instant de DÉCISION. L'échéance
        réelle part de l'instant d'ENTRÉE (décision + latence) et c'est le
        moteur d'exécution qui la calcule."""
        return self.decided_at_ms + self.expiry_sec * MS_PER_SEC


@dataclass(frozen=True, slots=True)
class ConditionResult:
    """Le résultat d'UNE condition, avec la valeur brute qui l'a produit.

    Garder la valeur numérique et pas seulement le booléen est ce qui rend
    l'analyse d'attribution possible (§3.2). Un « stochastique : non » ne dit
    pas s'il manquait un point ou trente ; `valeur = 21.4` contre un seuil de 20
    dit que la condition a failli passer, et c'est cette information qui permet
    de savoir si un seuil est mal placé ou si la condition ne sert à rien.
    """

    nom: str
    validee: bool
    valeur: float | None = None

    def __post_init__(self) -> None:
        if not self.nom:
            raise BotError("ConditionResult sans nom")


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Ce qu'on enregistre à CHAQUE bougie évaluée, signal ou non (§3.1).

    « C'est le point le plus important de la spec. Sans les quasi-signaux et
    leur résultat contrefactuel, vous ne pouvez pas répondre à "qu'est-ce que
    le bot a raté". Avec eux, chaque condition devient mesurable. »

    `direction_envisagee` est renseignée même quand aucun signal n'est émis :
    c'est elle qui permet de calculer le résultat contrefactuel — ce qui serait
    arrivé si on avait pris le trade. Une évaluation sans direction envisagée
    ne produit pas de contrefactuel, et c'est un cas légitime (le marché
    n'oriente ni dans un sens ni dans l'autre).

    `features` est figée à la construction. Elles sont enregistrées AU MOMENT
    de la décision et jamais recalculées après coup : un recalcul ultérieur sur
    du code modifié réintroduit du look-ahead (§3.1).
    """

    pair: str
    ts_ms: int
    direction_envisagee: "Direction | None"
    conditions: tuple[ConditionResult, ...]
    features: Mapping[str, float]
    signal: "Signal | None" = None

    def __post_init__(self) -> None:
        if not self.pair:
            raise BotError("Evaluation sans paire")
        ensure_ms(self.ts_ms, what=f"Evaluation({self.pair}).ts_ms")
        if self.signal is not None:
            if self.signal.decided_at_ms != self.ts_ms:
                raise BotError(
                    f"Evaluation à {self.ts_ms} portant un signal daté "
                    f"{self.signal.decided_at_ms}"
                )
            if self.signal.direction is not self.direction_envisagee:
                raise BotError(
                    "Le signal émis ne va pas dans la direction envisagée : "
                    "l'enregistrement et la décision se contrediraient."
                )
            if not self.toutes_validees:
                raise BotError(
                    "Signal émis alors qu'une condition a échoué. La porte ET "
                    "et le signal doivent venir du même calcul."
                )
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
        object.__setattr__(self, "conditions", tuple(self.conditions))

    @property
    def signal_emis(self) -> bool:
        return self.signal is not None

    @property
    def toutes_validees(self) -> bool:
        return all(c.validee for c in self.conditions)

    @property
    def condition_bloquante(self) -> str | None:
        """La PREMIÈRE condition échouée, dans l'ordre d'évaluation.

        « Première » et non « toutes » parce que c'est ce que demande la spec,
        mais l'ordre compte donc : une condition placée en tête sera créditée
        de tous les blocages qu'une condition suivante aurait aussi provoqués.
        Le détail complet reste disponible dans `conditions` ; ce champ n'est
        qu'un raccourci pour les comptages rapides.
        """
        for c in self.conditions:
            if not c.validee:
                return c.nom
        return None
