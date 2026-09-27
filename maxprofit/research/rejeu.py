"""
Rejouer le PLAN — pas seulement la stratégie — sur les bougies collectées.

Le backtest dit si un signal gagne. Il ne dit pas combien de SESSIONS un jour
peut contenir, parce que ce nombre dépend de règles qui n'existent qu'au
niveau du plan :

- un seul ordre à la fois : pendant l'échéance, les autres signaux sont
  perdus ;
- la règle d'indépendance (#68) : le pas suivant d'une martingale se joue sur
  un AUTRE actif, au moins `independance_sec` après l'entrée du précédent ;
- une session qui attend un pas plus de `attente_max_sec` est interrompue ;
- une session s'arrête au premier pas gagné, ou après `pas_max` pertes.

C'est la seule façon de répondre à « 18 sessions en 12 h sont-elles
possibles, et à quel taux de réussite » avant de toucher à la production.

⚠ Ce que ce rejeu simplifie, et dans quel sens. L'entrée est prise à la
clôture de la bougie de signal, la sortie à la clôture de la bougie qui finit
à l'échéance : c'est l'approximation M1 d'un ordre placé quelques secondes
après la clôture. Un signal né pendant un ordre en vol est perdu, même si sa
bougie est la dernière au dénouement : le direct en rattraperait quelques-uns,
le rejeu est donc légèrement PESSIMISTE sur le débit.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.payout import au_plafond
from maxprofit.core.types import Candle, Direction

#: Secondes dans une fenêtre de débit. Douze heures : la question posée.
FENETRE_SEC = 12 * 3600


@dataclass(frozen=True)
class SignalRejoue:
    """Un signal tel que le direct l'aurait vu, à la clôture de sa bougie."""

    fin_sec: int          # clôture de la bougie de signal = instant de décision
    pair: str
    call: bool
    ts_bougie: int        # début de la bougie de signal


def generer_signaux(bougies_par_paire: dict[str, Sequence[Candle]],
                    strategie,
                    dans_la_calibration: Callable[[str, list[Candle]], bool],
                    progression: Callable[[str], None] | None = None,
                    ) -> list[SignalRejoue]:
    """Évalue chaque bougie comme le direct : fenêtre des `lookback` dernières
    minutes, bougies complètes seulement, garde de calibration, puis
    historique minimal, puis la stratégie."""
    p = strategie.p
    minimum = 2 * p.fenetre_pique + 2
    sortie: list[SignalRejoue] = []
    for pair, serie in bougies_par_paire.items():
        if progression is not None:
            progression(pair)
        completes = sorted((b for b in serie if b.complete),
                           key=lambda b: b.ts_sec)
        debuts = [b.ts_sec for b in completes]
        for i, bougie in enumerate(completes):
            j = bisect.bisect_left(debuts, bougie.ts_sec - p.lookback * 60)
            fenetre = completes[j:i + 1]
            if not dans_la_calibration(pair, fenetre):
                continue
            if len(fenetre) < minimum:
                continue
            signal = strategie.on_bar(SequenceMarketView(pair, fenetre))
            if signal is None:
                continue
            sortie.append(SignalRejoue(
                fin_sec=bougie.ts_sec + 60, pair=pair,
                call=signal.direction is Direction.CALL,
                ts_bougie=bougie.ts_sec))
    sortie.sort(key=lambda s: (s.fin_sec, s.pair))
    return sortie


class IndexBougies:
    """Clôtures par (paire, début de bougie), pour résoudre un trade."""

    def __init__(self, bougies_par_paire: dict[str, Sequence[Candle]]):
        self._close = {(b.pair, b.ts_sec): b.close
                       for serie in bougies_par_paire.values() for b in serie}

    def resultat(self, s: SignalRejoue, echeance_sec: int) -> str | None:
        """« win », « loose » ou « draw » ; `None` si une bougie manque."""
        entree = self._close.get((s.pair, s.ts_bougie))
        sortie = self._close.get((s.pair, s.ts_bougie + echeance_sec))
        if entree is None or sortie is None:
            return None
        if sortie == entree:
            return "draw"
        return "win" if (sortie > entree) == s.call else "loose"


class IndexPayouts:
    """Le relevé antérieur le plus proche (§2.3), en temps logarithmique."""

    def __init__(self, releves: Iterable[tuple[int, str, int, bool]]):
        par_paire: dict[str, list[tuple[int, int, bool]]] = {}
        for ts, pair, pct, ouvert in releves:
            par_paire.setdefault(pair, []).append((int(ts), int(pct), bool(ouvert)))
        self._ts: dict[str, list[int]] = {}
        self._val: dict[str, list[tuple[int, bool]]] = {}
        for pair, liste in par_paire.items():
            liste.sort()
            self._ts[pair] = [x[0] for x in liste]
            self._val[pair] = [(x[1], x[2]) for x in liste]

    def au_maximum(self, pair: str, ts_sec: int) -> bool:
        ts = self._ts.get(pair)
        if not ts:
            return False
        i = bisect.bisect_right(ts, ts_sec) - 1
        if i < 0:
            return False
        pct, ouvert = self._val[pair][i]
        return ouvert and au_plafond(pct)


@dataclass
class Rejeu:
    echeance_sec: int
    signaux: int = 0
    hors_payout: int = 0
    pendant_un_ordre: int = 0
    sautes_independance: int = 0
    irresolus: int = 0
    #: (instant d'entrée, paire, rang du pas, résultat)
    trades: list[tuple[int, str, int, str]] = field(default_factory=list)
    #: (instant d'ouverture, issue : « gagnee », « perdue », « interrompue »)
    sessions: list[tuple[int, str]] = field(default_factory=list)

    def precision(self, pas: int | None = None) -> float | None:
        vus = [r for _, _, k, r in self.trades if pas is None or k == pas]
        return sum(r == "win" for r in vus) / len(vus) if vus else None

    def par_fenetre(self, debut: int, fin: int,
                    fenetres_valides: set[int] | None = None) -> list[int]:
        """Sessions OUVERTES par fenêtre de 12 h, de `debut` à `fin`."""
        n = max(1, (fin - debut) // FENETRE_SEC)
        comptes = [0] * n
        for ts, _ in self.sessions:
            k = (ts - debut) // FENETRE_SEC
            if 0 <= k < n:
                comptes[k] += 1
        if fenetres_valides is None:
            return comptes
        return [c for k, c in enumerate(comptes) if k in fenetres_valides]


def rejouer(signaux: Sequence[SignalRejoue], bougies: IndexBougies,
            payouts: IndexPayouts, *, echeance_sec: int,
            independance_sec: int = 900, attente_max_sec: int = 7200,
            pas_max: int = 3) -> Rejeu:
    """Joue le plan, signal après signal, dans l'ordre du temps."""
    r = Rejeu(echeance_sec=echeance_sec, signaux=len(signaux))
    libre_a = 0
    session_debut: int | None = None
    pas = 0
    dernier: tuple[str, int] | None = None     # (paire, entrée) du pas précédent

    def clore(issue: str) -> None:
        nonlocal session_debut, pas, dernier
        r.sessions.append((session_debut, issue))
        session_debut, pas, dernier = None, 0, None

    for s in signaux:
        t = s.fin_sec
        if session_debut is not None and dernier is not None \
                and t - dernier[1] > attente_max_sec:
            clore("interrompue")
        if t < libre_a:
            r.pendant_un_ordre += 1
            continue
        if not payouts.au_maximum(s.pair, t):
            r.hors_payout += 1
            continue
        if dernier is not None and (s.pair == dernier[0]
                                    or t - dernier[1] < independance_sec):
            r.sautes_independance += 1
            continue
        issue = bougies.resultat(s, echeance_sec)
        if issue is None:
            r.irresolus += 1
            continue
        if session_debut is None:
            session_debut = t
        pas += 1
        r.trades.append((t, s.pair, pas, issue))
        libre_a = t + echeance_sec
        dernier = (s.pair, t)
        # Une égalité ne rapporte rien : comme en direct, elle compte comme
        # un pas perdu pour l'échelle.
        if issue == "win":
            clore("gagnee")
        elif pas >= pas_max:
            clore("perdue")
    if session_debut is not None:
        clore("interrompue")
    return r
