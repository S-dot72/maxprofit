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

from maxprofit.apprentissage.contexte import FENETRE, contexte
from maxprofit.apprentissage.lecons import ECHEANCES_COMPAREES, Exemple
from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.types import Candle, Direction
from maxprofit.strategies.criteres import criteres
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
                                            echeance_sec, signal.features))
                ctx.update(criteres(fenetre, call,
                                    (signal.features or {}).get("niveau")))
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


#: Bougies M1 pendant lesquelles le rejeu cherche la confirmation.
ATTENTE_CONFIRMATION_MAX = 3

#: Le contexte remesuré À L'ENTRÉE confirmée, préfixé « e_ ». En direct, la
#: course mesure son contexte après la confirmation, sur la bougie
#: d'entrée ; le rejeu le mesurait sur la bougie du signal, une à trois
#: minutes plus tôt. L'« entrée tardive » (distance à la zone) n'existait
#: donc pas au rejeu : le CALL GBPUSD du 08/10, pris 5 amplitudes au-dessus
#: de son support, y aurait été noté au contact.
CONTEXTE_A_L_ENTREE: tuple[str, ...] = (
    "distance_niveau", "elan_15m", "elan_30m", "mouvement_heure",
    "volatilite_relative", "bougies_contre", "corps_signal", "meche_rejet",
    "position_bande", "favorables_15", "rebond_15")


def _confirmation_m1(completes, i: int, call: bool, closes,
                     echeance_sec: int, features=None) -> dict[str, float]:
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
        # La même entrée confirmée, jugée à d'autres échéances (3 et 5 min
        # demandées le 2026-10-03) : seule la sortie change.
        for sec in ECHEANCES_COMPAREES:
            prix = closes.get(suivante.ts_sec + sec)
            if prix is not None and prix != suivante.close:
                sortie[f"gagne_suivant_{sec}"] = float(
                    (prix > suivante.close) == call)
    # `attente_confirmation` : la première des ATTENTE_CONFIRMATION_MAX
    # bougies suivantes qui casse l'extrême de la bougie du signal dans le
    # sens du trade (1 = la suivante, comme `retournement_suivant`), 0 si
    # aucune ; `gagne_confirme`, l'issue d'une entrée à sa clôture. Mesure
    # le débit qu'on regagnerait en attendant la confirmation plus longtemps.
    for k in range(1, ATTENTE_CONFIRMATION_MAX + 1):
        j = i + k
        if j >= len(completes) or \
                completes[j].ts_sec != bougie.ts_sec + 60 * k:
            break
        if _retournement(call, completes[j], bougie):
            entree = completes[j]
            sortie["attente_confirmation"] = float(k)
            prix = closes.get(entree.ts_sec + echeance_sec)
            if prix is not None and prix != entree.close:
                sortie["gagne_confirme"] = float((prix > entree.close) == call)
            for sec in ECHEANCES_COMPAREES:
                prix = closes.get(entree.ts_sec + sec)
                if prix is not None and prix != entree.close:
                    sortie[f"gagne_confirme_{sec}"] = float(
                        (prix > entree.close) == call)
            a_l_entree = contexte(completes[max(0, j + 1 - FENETRE):j + 1],
                                  call, features)
            for cle in CONTEXTE_A_L_ENTREE:
                if cle in a_l_entree:
                    sortie[f"e_{cle}"] = a_l_entree[cle]
            break
        if k == ATTENTE_CONFIRMATION_MAX:
            sortie["attente_confirmation"] = 0.0
    return sortie
