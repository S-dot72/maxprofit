"""
Les exemples : la stratégie rejouée sur l'historique, avec contexte et issue.

Même évaluation que la course — fenêtre de `lookback` minutes, bougies closes,
garde de calibration, puis la stratégie — et même issue que le rejeu du plan :
entrée à la clôture de la bougie de signal, sortie à la clôture de la bougie
qui finit à l'échéance. Une égalité n'est ni gagnée ni perdue : elle n'est
pas un exemple.
"""

from __future__ import annotations

import bisect
import math
from typing import Callable, Mapping, Sequence

from maxprofit.apprentissage.contexte import contexte
from maxprofit.apprentissage.lecons import ECHEANCES_COMPAREES, Exemple
from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.types import Candle, Direction
from maxprofit.strategies.zones_zigzag import rythme_minutes


def exemples_historiques(
        bougies_par_paire: Mapping[str, Sequence[Candle]], strategie, *,
        echeance_sec: int,
        garde: Callable[[str, Sequence[Candle]], bool] = lambda p, f: True,
        respirer: Callable[[], None] | None = None,
        echeances: Sequence[int] = ECHEANCES_COMPAREES,
        avec_contexte: bool = True) -> list[Exemple]:
    """Chaque signal que la stratégie aurait émis, avec son contexte.

    `respirer`, appelée régulièrement, laisse la main aux autres fils du
    processus : le rejeu tourne à côté de la collecte et de la course.
    """
    lookback = strategie.p.lookback
    # Une stratégie à long recul (zones H1) tolère les trous de collecte ;
    # ZoneH1, elle, exige sa fenêtre entière, comme en direct.
    requis = math.ceil(lookback * getattr(strategie.p, "couverture_min", 1.0))
    sortie: list[Exemple] = []
    evaluees = 0
    for pair, serie in bougies_par_paire.items():
        completes = sorted((b for b in serie if b.complete),
                           key=lambda b: b.ts_sec)
        debuts = [b.ts_sec for b in completes]
        closes = {b.ts_sec: b.close for b in completes}
        if not completes:
            continue
        # UNE vue par paire, dont on avance le curseur — comme le moteur de
        # backtest. En recréer une à chaque minute revalidait toute la
        # fenêtre à chaque fois : 2 880 bougies par minute rejouée pour une
        # stratégie à zones H1. `candles(lookback)` rend la même fenêtre
        # que la tranche ci-dessous dès qu'elle est complète.
        vue = SequenceMarketView(pair, completes, 0)
        for i, bougie in enumerate(completes):
            j = bisect.bisect_left(debuts, bougie.ts_sec - lookback * 60)
            if i + 1 - j < requis:
                continue                  # la stratégie refuserait de trancher
            fenetre = completes[j:i + 1]
            if not garde(pair, fenetre):
                continue
            evaluees += 1
            if respirer is not None and evaluees % 200 == 0:
                respirer()
            while vue.index < i:
                vue.advance()
            signal = strategie.on_bar(vue)
            if signal is None:
                continue
            sortie_prix = closes.get(bougie.ts_sec + echeance_sec)
            if sortie_prix is None or sortie_prix == bougie.close:
                continue
            call = signal.direction is Direction.CALL
            # Le MÊME signal jugé à d'autres échéances : c'est ce qui rend
            # la comparaison juste — mêmes entrées, seule la sortie change.
            issues = {}
            for sec in echeances:
                prix = closes.get(bougie.ts_sec + sec)
                if prix is not None and prix != bougie.close:
                    issues[sec] = (prix > bougie.close) == call
            ctx = (contexte(fenetre, call, signal.features)
                   if avec_contexte else {})
            if avec_contexte:
                ctx.update(_au_dela_de_la_fenetre(
                    closes, bougie.ts_sec, call, signal.features))
                ctx.update(_confirmation_m1(completes, i, call, closes,
                                            echeance_sec))
                rythme = rythme_minutes(fenetre)
                if rythme is not None:
                    ctx["rythme_zigzag"] = round(rythme, 1)
            sortie.append(Exemple(
                ts_sec=bougie.ts_sec + 60, pair=pair, contexte=ctx,
                gagne=(sortie_prix > bougie.close) == call, issues=issues))
    sortie.sort(key=lambda e: e.ts_sec)
    return sortie


def _au_dela_de_la_fenetre(closes: Mapping[int, float], ts_sec: int,
                           call: bool, features) -> dict[str, float]:
    """Ce que le laboratoire mesure en plus du contexte de la stratégie.

    `tendance_4h` : du close d'il y a quatre heures closes au close de la
    dernière heure close, dans le sens du trade — plus loin que la fenêtre de
    cinq heures de la stratégie, d'où la lecture dans la série complète.
    `niveau` : le prix de la zone, pour reconnaître deux signaux sur la même.
    Hors de `CARACTERISTIQUES` : l'apprentissage des leçons ne les voit pas.
    """
    sortie: dict[str, float] = {}
    heure = (ts_sec + 60) // 3600 * 3600
    recent, ancien = closes.get(heure - 60), closes.get(heure - 4 * 3600 - 60)
    if recent is not None and ancien is not None:
        sortie["tendance_4h"] = round((recent - ancien) * (1 if call else -1)
                                      / (abs(ancien) or 1) * 1e4, 4)
    if features and "niveau" in features:
        sortie["niveau"] = float(features["niveau"])
    if features and "inversee" in features:
        sortie["inversee"] = float(features["inversee"])
    return sortie


def _retournement(call: bool, bougie, precedente) -> bool:
    if call:
        return bougie.close > bougie.open and bougie.close > precedente.high
    return bougie.close < bougie.open and bougie.close < precedente.low


def _confirmation_m1(completes, i: int, call: bool, closes,
                     echeance_sec: int) -> dict[str, float]:
    """Pour le laboratoire : le signal avait-il sa confirmation M1 ?

    `retournement_meme` : la bougie du signal va dans le sens du trade et
    casse l'extrême de la précédente. `retournement_suivant` : la bougie
    SUIVANTE le fait par rapport à celle du signal — on n'entre alors qu'à
    sa clôture, d'où `gagne_suivant`, l'issue d'une entrée une minute plus
    tard. Lire la bougie suivante n'est pas regarder l'avenir : la décision
    de cette variante se prend à sa clôture.
    """
    sortie: dict[str, float] = {}
    bougie = completes[i]
    if i >= 1 and completes[i - 1].ts_sec == bougie.ts_sec - 60:
        sortie["retournement_meme"] = float(
            _retournement(call, bougie, completes[i - 1]))
    if i + 1 < len(completes) and \
            completes[i + 1].ts_sec == bougie.ts_sec + 60:
        suivante = completes[i + 1]
        sortie["retournement_suivant"] = float(
            _retournement(call, suivante, bougie))
        sortie_prix = closes.get(suivante.ts_sec + echeance_sec)
        if sortie_prix is not None and sortie_prix != suivante.close:
            sortie["gagne_suivant"] = float(
                (sortie_prix > suivante.close) == call)
    return sortie
