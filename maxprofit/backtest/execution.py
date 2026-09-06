"""
Réalisme d'exécution (spec §2.2) : ce qui sépare un backtest d'une simulation
optimiste.

Les quatre points de la spec, et la raison de chacun :

**Latence.** Entre la clôture de la bougie qui déclenche le signal et le clic
réel, il se passe : l'agrégation, l'envoi Telegram, la notification, la lecture,
le clic. La spec demande de chronométrer vingt fois et de prendre la MÉDIANE,
pas le minimum — le minimum est ce qui arrive quand tout va bien, ce qui n'est
pas le cas moyen. Ce paramètre n'a pas de valeur par défaut et apparaît dans
chaque rapport.

**Prix d'entrée.** Premier tick disponible à `t_signal + latence`, jamais la
clôture de la bougie de signal. Utiliser la clôture revient à entrer dans le
passé, avec plusieurs secondes d'avance sur soi-même : c'est un look-ahead
déguisé en simplification.

**Prix de règlement.** Dernier tick à `t_entrée + expiration`, lu dans la table
des ticks. C'est la raison pour laquelle le collecteur enregistre les ticks et
pas seulement les bougies : une option à 60 s se règle sur le prix exact à la
seconde d'expiration, qu'aucune bougie M1 ne contient.

**Irrésolvable ≠ perdant.** Si aucun tick n'existe dans une fenêtre de ±2 s
autour de l'instant voulu, le trade est marqué irrésolvable et EXCLU des
statistiques. Le compter comme perdant introduirait un biais dont la direction
dépend de la qualité de la collecte : les trous surviennent pendant les
déconnexions, qui ne sont pas réparties au hasard dans la journée.

**Égalité.** Sur des paires peu volatiles cotées à cinq décimales, le prix de
règlement égale parfois exactement le prix d'entrée. La règle du broker doit
être codée explicitement, pas ignorée.
"""

from __future__ import annotations

import bisect
import enum
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence

from maxprofit.core.errors import BotError
from maxprofit.core.timebase import MS_PER_SEC, ms_to_sec
from maxprofit.core.types import Direction, PairInfo, Signal, Tick

#: Fenêtre de tolérance autour de l'instant voulu (spec §2.2). Ce n'est pas un
#: réglage : c'est la définition de « résolvable » donnée par la spec.
TOLERANCE_TICK_MS = 2 * MS_PER_SEC


class RegleEgalite(enum.Enum):
    """Ce que fait le broker quand le prix de règlement égale le prix d'entrée."""

    REMBOURSEMENT = "REMBOURSEMENT"   # la mise est rendue : P&L nul
    PERTE = "PERTE"                   # l'égalité compte comme une perte


class Resultat(enum.Enum):
    GAGNE = "GAGNE"
    PERDU = "PERDU"
    EGALITE = "EGALITE"
    IRRESOLU = "IRRESOLU"   # exclu des statistiques, jamais compté perdant


class MotifIrresolu(enum.Enum):
    AUCUN_TICK_ENTREE = "aucun tick à l'entrée"
    AUCUN_TICK_REGLEMENT = "aucun tick au règlement"


@dataclass(frozen=True)
class ExecutionConfig:
    """Aucune valeur par défaut sur les trois premiers champs (spec §5) : ils
    touchent directement au résultat monétaire. Un backtest lancé sans latence
    déclarée produirait un rapport, et le rapport serait faux."""

    latence_ms: int
    expiry_sec: int
    regle_egalite: RegleEgalite
    payout_min_pct: int
    tolerance_tick_ms: int = TOLERANCE_TICK_MS

    def __post_init__(self) -> None:
        if self.latence_ms < 0:
            raise BotError(f"latence_ms négative : {self.latence_ms}")
        if self.latence_ms == 0:
            raise BotError(
                "latence_ms = 0 signifie que vous cliquez à l'instant même de la "
                "clôture de bougie. Mesurez-la (médiane de 20 chronos, §2.2) : "
                "une latence nulle est le raccourci qui rend un backtest faux."
            )
        if self.expiry_sec <= 0:
            raise BotError(f"expiry_sec doit être > 0, reçu {self.expiry_sec}")
        if not isinstance(self.regle_egalite, RegleEgalite):
            raise BotError(
                "regle_egalite doit être explicite : vérifiez la règle de votre "
                "broker et codez-la (§2.2)."
            )
        if not (0 <= self.payout_min_pct <= 100):
            raise BotError(f"payout_min_pct hors [0,100] : {self.payout_min_pct}")


@dataclass(frozen=True)
class Trade:
    """Un trade simulé, avec tout ce qu'il faut pour le rejouer et le contester."""

    pair: str
    direction: Direction
    decided_at_ms: int
    entree_visee_ms: int
    reglement_vise_ms: int
    resultat: Resultat
    payout_pct: int
    entry_ts_ms: int | None = None
    entry_price: float | None = None
    settle_ts_ms: int | None = None
    settle_price: float | None = None
    pnl: float | None = None            # None si irrésolu : exclu, pas perdant
    motif_irresolu: MotifIrresolu | None = None
    features: Mapping[str, float] = field(default_factory=dict)

    @property
    def compte_dans_les_stats(self) -> bool:
        return self.resultat is not Resultat.IRRESOLU


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

class SourceTicks(Protocol):
    """Accès aux ticks. Deux questions seulement, celles du §2.2."""

    def premier_a_partir_de(self, pair: str, ts_ms: int,
                            tolerance_ms: int) -> Tick | None: ...

    def dernier_jusqu_a(self, pair: str, ts_ms: int,
                        tolerance_ms: int) -> Tick | None: ...


class SourcePayouts(Protocol):
    def payout_at(self, pair: str, ts_sec: int) -> PairInfo | None: ...


class TicksEnMemoire:
    """Source de ticks en mémoire, indexée par paire et triée.

    Utilisée par les tests-oracles, qui doivent tourner sans base de données :
    un moteur qu'on ne peut éprouver qu'avec quatorze jours de collecte n'est
    pas éprouvé.
    """

    def __init__(self, ticks: Sequence[Tick]):
        self._par_paire: dict[str, list[Tick]] = {}
        for tick in ticks:
            self._par_paire.setdefault(tick.pair, []).append(tick)
        for liste in self._par_paire.values():
            liste.sort(key=lambda t: t.ts_ms)
        self._instants = {
            pair: [t.ts_ms for t in liste] for pair, liste in self._par_paire.items()
        }

    def premier_a_partir_de(self, pair: str, ts_ms: int,
                            tolerance_ms: int) -> Tick | None:
        liste = self._par_paire.get(pair)
        if not liste:
            return None
        i = bisect.bisect_left(self._instants[pair], ts_ms)
        if i >= len(liste):
            return None
        tick = liste[i]
        return tick if tick.ts_ms - ts_ms <= tolerance_ms else None

    def dernier_jusqu_a(self, pair: str, ts_ms: int,
                        tolerance_ms: int) -> Tick | None:
        """Dernier tick à `ts_ms` ou avant, à défaut le premier juste après.

        L'ordre de préférence compte. Un tick antérieur est le prix qui avait
        cours à l'instant d'expiration : c'est celui sur lequel le broker règle.
        Un tick postérieur n'est utilisé que si aucun antérieur n'existe dans la
        fenêtre, et il reste dans la tolérance de ±2 s.
        """
        liste = self._par_paire.get(pair)
        if not liste:
            return None
        instants = self._instants[pair]
        i = bisect.bisect_right(instants, ts_ms) - 1
        if i >= 0 and ts_ms - liste[i].ts_ms <= tolerance_ms:
            return liste[i]
        j = bisect.bisect_left(instants, ts_ms)
        if j < len(liste) and liste[j].ts_ms - ts_ms <= tolerance_ms:
            return liste[j]
        return None


class PayoutsEnMemoire:
    """Relevés de payouts en mémoire, avec la même règle de jointure que la
    base : le relevé antérieur le plus proche (§2.3)."""

    def __init__(self, releves: Sequence[tuple[int, PairInfo]]):
        self._par_paire: dict[str, list[tuple[int, PairInfo]]] = {}
        for ts_sec, info in releves:
            self._par_paire.setdefault(info.name, []).append((ts_sec, info))
        for liste in self._par_paire.values():
            liste.sort(key=lambda x: x[0])

    def payout_at(self, pair: str, ts_sec: int) -> PairInfo | None:
        liste = self._par_paire.get(pair)
        if not liste:
            return None
        i = bisect.bisect_right([x[0] for x in liste], ts_sec) - 1
        return liste[i][1] if i >= 0 else None


# --------------------------------------------------------------------------- #
# Exécution
# --------------------------------------------------------------------------- #

def executer(signal: Signal, payout: PairInfo, ticks: SourceTicks,
             cfg: ExecutionConfig) -> Trade:
    """Transforme un signal en trade résolu, ou le marque irrésolvable.

    L'expiration court depuis l'instant d'entrée VISÉ (`décision + latence`) et
    non depuis l'horodatage du tick d'entrée réellement trouvé. C'est ce que
    fait un broker : le compte à rebours démarre au clic, pas à la cotation
    suivante. Faire courir l'expiration depuis le tick trouvé donnerait quelques
    dizaines de millisecondes gratuites, systématiquement du même côté.
    """
    entree_visee_ms = signal.decided_at_ms + cfg.latence_ms
    reglement_vise_ms = entree_visee_ms + cfg.expiry_sec * MS_PER_SEC

    base = dict(
        pair=signal.pair, direction=signal.direction,
        decided_at_ms=signal.decided_at_ms,
        entree_visee_ms=entree_visee_ms, reglement_vise_ms=reglement_vise_ms,
        payout_pct=payout.payout_pct, features=dict(signal.features),
    )

    tick_entree = ticks.premier_a_partir_de(
        signal.pair, entree_visee_ms, cfg.tolerance_tick_ms
    )
    if tick_entree is None:
        return Trade(**base, resultat=Resultat.IRRESOLU,
                     motif_irresolu=MotifIrresolu.AUCUN_TICK_ENTREE)

    tick_reglement = ticks.dernier_jusqu_a(
        signal.pair, reglement_vise_ms, cfg.tolerance_tick_ms
    )
    if tick_reglement is None:
        return Trade(**base, resultat=Resultat.IRRESOLU,
                     entry_ts_ms=tick_entree.ts_ms, entry_price=tick_entree.price,
                     motif_irresolu=MotifIrresolu.AUCUN_TICK_REGLEMENT)

    resultat = _juger(signal.direction, tick_entree.price, tick_reglement.price)
    return Trade(
        **base, resultat=resultat,
        entry_ts_ms=tick_entree.ts_ms, entry_price=tick_entree.price,
        settle_ts_ms=tick_reglement.ts_ms, settle_price=tick_reglement.price,
        pnl=pnl(resultat, payout.payout_ratio, cfg.regle_egalite),
    )


def _juger(direction: Direction, entree: float, reglement: float) -> Resultat:
    if reglement == entree:
        return Resultat.EGALITE
    monte = reglement > entree
    gagne = monte if direction is Direction.CALL else not monte
    return Resultat.GAGNE if gagne else Resultat.PERDU


def pnl(resultat: Resultat, payout_ratio: float, regle: RegleEgalite) -> float | None:
    """P&L en fraction de la mise. Une mise de 1 rapporte `payout_ratio` si elle
    gagne, et coûte 1 si elle perd — d'où l'espérance de `p × payout − (1−p)`."""
    if resultat is Resultat.GAGNE:
        return payout_ratio
    if resultat is Resultat.PERDU:
        return -1.0
    if resultat is Resultat.EGALITE:
        return 0.0 if regle is RegleEgalite.REMBOURSEMENT else -1.0
    return None


def payout_eligible(payout: PairInfo | None, cfg: ExecutionConfig) -> bool:
    """« Un trade sur une paire qui n'était pas éligible à cet instant n'est pas
    généré du tout » (§2.3). Pas estimé, pas approché : pas généré."""
    if payout is None:
        return False
    return payout.is_open and payout.payout_pct >= cfg.payout_min_pct


def instant_en_sec(ts_ms: int) -> int:
    return ms_to_sec(ts_ms)
