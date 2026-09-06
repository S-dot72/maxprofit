"""
Le moteur de backtest.

Il ne cherche pas à être juste — on ne prouve pas la justesse d'un moteur de
backtest. Il cherche à **échouer bruyamment** sur les erreurs classiques au lieu
de produire silencieusement des résultats flatteurs (spec §2). Ce que le moteur
garantit :

1. La stratégie ne reçoit qu'une `MarketView`. Elle ne peut pas voir le futur —
   pas « elle ne devrait pas » : elle ne le peut pas (§2.1).
2. Un signal daté autrement qu'à `view.now_ms` est REJETÉ, pas corrigé. Un
   moteur qui recale silencieusement un horodatage masque le bug qui l'a produit.
3. Aucun signal n'est généré sur des données douteuses, et chaque exclusion est
   comptée par motif (§2.4). Le rapport affiche le pourcentage écarté ; au-delà
   de 10 %, c'est la collecte qu'il faut réparer, pas la stratégie.
4. Le payout utilisé est celui en vigueur à l'instant du trade (§2.3).
5. Un trade non résolvable est exclu, jamais compté perdant (§2.2).

Ce que le moteur ne garantit pas, et qu'il faut vérifier autrement : qu'il ne
ment pas. C'est le rôle des cinq tests-oracles du §2.7, dans
`tests/test_oracles.py`. Aucun résultat de stratégie n'est crédible tant qu'ils
ne passent pas tous.
"""

from __future__ import annotations

import enum
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from maxprofit.core.errors import BotError
from maxprofit.core.market_view import SequenceMarketView, assert_no_look_ahead
from maxprofit.core.strategy import Strategy
from maxprofit.core.types import Candle
from maxprofit.backtest.execution import (
    ExecutionConfig,
    Resultat,
    SourcePayouts,
    SourceTicks,
    Trade,
    executer,
    payout_eligible,
)


class MotifExclusion(enum.Enum):
    """Pourquoi une bougie n'a pas pu produire de signal.

    Compter les exclusions par motif n'est pas de la statistique de confort :
    c'est ce qui permet de savoir si l'absence de trades vient de la stratégie
    (elle ne trouve rien) ou de la collecte (les données sont inutilisables).
    Les deux situations se ressemblent dans un rapport qui n'affiche que le
    nombre de trades.
    """

    HISTORIQUE_INSUFFISANT = "historique insuffisant"
    BOUGIE_INCOMPLETE = "bougie incomplète"
    TICKS_INSUFFISANTS = "moins de ticks que le minimum"
    TROU_UPTIME = "trou de connexion sur la fenêtre"
    PAIRE_NON_ELIGIBLE = "paire non éligible à cet instant"


@dataclass
class Rapport:
    strategie: str
    params: dict
    config: ExecutionConfig
    n_bougies: int = 0
    n_evaluations: int = 0
    n_signaux: int = 0
    trades: list[Trade] = field(default_factory=list)
    exclusions: Counter = field(default_factory=Counter)

    # --- comptages ----------------------------------------------------------

    @property
    def resolus(self) -> list[Trade]:
        return [t for t in self.trades if t.compte_dans_les_stats]

    @property
    def n_irresolus(self) -> int:
        return len(self.trades) - len(self.resolus)

    @property
    def n_gagnes(self) -> int:
        return sum(1 for t in self.trades if t.resultat is Resultat.GAGNE)

    @property
    def n_perdus(self) -> int:
        return sum(1 for t in self.trades if t.resultat is Resultat.PERDU)

    @property
    def n_egalites(self) -> int:
        return sum(1 for t in self.trades if t.resultat is Resultat.EGALITE)

    @property
    def taux_reussite(self) -> float | None:
        """Gagnés / (gagnés + perdus). Les ÉGALITÉS sont exclues du taux et
        comptées à part : les inclure au dénominateur ferait baisser le taux
        sans qu'aucun trade ait été perdu, et l'inclure au numérateur
        l'augmenterait sans qu'aucun ait été gagné."""
        decides = self.n_gagnes + self.n_perdus
        return None if decides == 0 else self.n_gagnes / decides

    @property
    def pnl_total(self) -> float:
        return sum(t.pnl for t in self.resolus if t.pnl is not None)

    @property
    def pnl_moyen(self) -> float | None:
        """Espérance par trade, en fraction de la mise. C'est le seul chiffre
        qui décide : un taux de réussite de 55 % à 80 % de payout perd de
        l'argent."""
        return self.pnl_total / len(self.resolus) if self.resolus else None

    @property
    def n_exclusions(self) -> int:
        return sum(self.exclusions.values())

    @property
    def pct_bougies_ecartees(self) -> float:
        """§2.4 : « Si ce pourcentage dépasse 10 %, la collecte est le problème,
        pas la stratégie. »"""
        if self.n_bougies == 0:
            return 0.0
        qualite = sum(
            self.exclusions[m] for m in (
                MotifExclusion.BOUGIE_INCOMPLETE,
                MotifExclusion.TICKS_INSUFFISANTS,
                MotifExclusion.TROU_UPTIME,
            )
        )
        return qualite / self.n_bougies * 100

    @property
    def collecte_suspecte(self) -> bool:
        return self.pct_bougies_ecartees > 10.0

    def resume(self) -> str:
        lignes = [
            f"Stratégie      : {self.strategie} {self.params}",
            f"Latence        : {self.config.latence_ms} ms   "
            f"Expiration : {self.config.expiry_sec} s   "
            f"Égalité : {self.config.regle_egalite.value}",
            f"Bougies        : {self.n_bougies:,}   Évaluations : {self.n_evaluations:,}",
            f"Signaux        : {self.n_signaux:,}",
            f"Trades résolus : {len(self.resolus):,}   Irrésolus (exclus) : {self.n_irresolus:,}",
            f"Gagnés/Perdus/Égalités : {self.n_gagnes}/{self.n_perdus}/{self.n_egalites}",
        ]
        if self.taux_reussite is not None:
            lignes.append(f"Taux de réussite : {self.taux_reussite * 100:.2f} %")
        if self.pnl_moyen is not None:
            lignes.append(f"P&L moyen par trade : {self.pnl_moyen * 100:+.2f} % de la mise")
        lignes.append(f"Bougies écartées pour qualité : {self.pct_bougies_ecartees:.1f} %")
        if self.collecte_suspecte:
            lignes.append(
                "  ATTENTION : au-delà de 10 %, c'est la collecte qu'il faut "
                "réparer, pas la stratégie (§2.4)."
            )
        for motif, n in self.exclusions.most_common():
            lignes.append(f"  exclusion {motif.value} : {n:,}")
        return "\n".join(lignes)


class BacktestEngine:
    def __init__(self, ticks: SourceTicks, payouts: SourcePayouts,
                 cfg: ExecutionConfig, *,
                 trous_uptime: Sequence[tuple[int, int]],
                 min_ticks_par_bougie: int = 5):
        """`trous_uptime` est OBLIGATOIRE, même vide.

        Le passer explicitement force à répondre à la question « ai-je vérifié
        les trous de connexion ? ». Une valeur par défaut reviendrait à
        supposer une collecte parfaite, ce qui est faux dès le premier
        redémarrage et se traduirait par des trades générés sur des fenêtres où
        le bot ne regardait pas le marché.
        """
        self.ticks = ticks
        self.payouts = payouts
        self.cfg = cfg
        self.trous = sorted(tuple(t) for t in trous_uptime)
        self.min_ticks = min_ticks_par_bougie

    def run(self, strategie: Strategy, candles: Sequence[Candle], *,
            lookback: int) -> Rapport:
        if lookback < 1:
            raise BotError(f"lookback doit valoir au moins 1, reçu {lookback}")
        serie = tuple(candles)
        if not serie:
            raise BotError("Backtest sur une série vide")

        strategie.reset()
        rapport = Rapport(
            strategie=strategie.name or type(strategie).__name__,
            params=dict(strategie.params), config=self.cfg,
            n_bougies=len(serie),
        )
        pair = serie[0].pair
        tf_sec = serie[0].tf_sec

        for i in range(len(serie)):
            motif = self._motif_exclusion(serie, i, lookback)
            if motif is not None:
                rapport.exclusions[motif] += 1
                continue

            vue = SequenceMarketView(pair, serie, index=i)
            assert_no_look_ahead(vue, tf_sec=tf_sec)
            rapport.n_evaluations += 1

            signal = strategie.on_bar(vue)
            if signal is None:
                continue

            self._verifier_signal(signal, vue)
            rapport.n_signaux += 1

            payout = self.payouts.payout_at(signal.pair, serie[i].close_ts_sec)
            if not payout_eligible(payout, self.cfg):
                # Pas un trade perdant : un trade qui n'aurait pas existé.
                rapport.exclusions[MotifExclusion.PAIRE_NON_ELIGIBLE] += 1
                continue

            rapport.trades.append(executer(signal, payout, self.ticks, self.cfg))

        return rapport

    # --- garde-fous ---------------------------------------------------------

    def _motif_exclusion(self, serie: Sequence[Candle], i: int,
                         lookback: int) -> MotifExclusion | None:
        """§2.4. La fenêtre examinée est celle des indicateurs, pas la seule
        bougie courante : une moyenne calculée sur une bougie incomplète est
        fausse même si la bougie courante est parfaite."""
        if i + 1 < lookback:
            return MotifExclusion.HISTORIQUE_INSUFFISANT

        fenetre = serie[i + 1 - lookback:i + 1]
        for c in fenetre:
            if not c.complete:
                return MotifExclusion.BOUGIE_INCOMPLETE
            if c.tick_count < self.min_ticks:
                return MotifExclusion.TICKS_INSUFFISANTS

        if self._trou_chevauche(fenetre[0].ts_sec, fenetre[-1].close_ts_sec):
            return MotifExclusion.TROU_UPTIME
        return None

    def _trou_chevauche(self, debut_sec: int, fin_sec: int) -> bool:
        for trou_debut, trou_fin in self.trous:
            if trou_debut < fin_sec and debut_sec < trou_fin:
                return True
        return False

    def _verifier_signal(self, signal, vue) -> None:
        """Un signal mal daté est rejeté, jamais recalé.

        Recaler silencieusement reviendrait à réparer le symptôme d'un bug qui
        continuerait ailleurs — et un signal daté avant `view.now_ms` est,
        littéralement, un ordre passé dans le passé.
        """
        if signal.decided_at_ms != vue.now_ms:
            raise BotError(
                f"Signal daté {signal.decided_at_ms} alors que la vue est à "
                f"{vue.now_ms}. Un signal porte l'instant de décision, qui est "
                f"`view.now_ms` — sans exception."
            )
        if signal.pair != vue.pair:
            raise BotError(
                f"Signal sur {signal.pair} produit par une vue de {vue.pair}"
            )
        if signal.expiry_sec != self.cfg.expiry_sec:
            raise BotError(
                f"Signal à {signal.expiry_sec} s d'expiration alors que le "
                f"moteur est configuré pour {self.cfg.expiry_sec} s. Le rapport "
                f"annoncerait une expiration qui n'est pas celle simulée."
            )
