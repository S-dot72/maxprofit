"""
Le contexte d'un signal : ce qu'un trader regarde avant d'entrer.

Calculé sur la MÊME fenêtre de bougies que celle que la stratégie a vue, au
moment de la décision, et jamais recalculé après coup (SPEC §3.1) : c'est ce
qui rend identiques le contexte d'un signal rejoué et celui d'un signal réel.

Chaque grandeur est ORIENTÉE dans le sens du trade : positive, elle va dans
son sens ; négative, contre lui. Une même règle vaut donc pour un CALL et un
PUT. Les mouvements sont exprimés en AMPLITUDES M1 moyennes, pour qu'une règle
apprise sur EURUSD veuille dire la même chose sur USDJPY.
"""

from __future__ import annotations

import statistics
from typing import Mapping, Sequence

from maxprofit.core.types import Candle

#: Ce qui est mesuré, et comment le dire à un humain.
LIBELLES: dict[str, str] = {
    "heure_utc": "heure (UTC)",
    "tendance_h1": "tendance des 3 dernières heures dans le sens du trade",
    "elan_15m": "élan des 15 dernières minutes dans le sens du trade",
    "volatilite_relative": "volatilité des 15 dernières minutes / habituelle",
    "bougies_contre": "bougies consécutives contre le trade",
    "corps_signal": "corps de la bougie de signal (part de son amplitude)",
    "meche_rejet": "mèche de rejet de la bougie de signal",
    "position_bande": "position dans la bande de Bollinger (sens du trade)",
    "distance_niveau": "distance au niveau de la zone",
    "touches": "touches de la zone",
    "entrees_deja_offertes": "entrées déjà offertes par la zone",
}

#: Les caractéristiques apprises. `heure_utc` se découpe en tranches, les
#: autres en quantiles.
CARACTERISTIQUES: tuple[str, ...] = tuple(LIBELLES)

#: Bougies sur lesquelles le contexte est mesuré, en rejeu comme en direct.
FENETRE = 300


def _moyenne(valeurs: Sequence[float]) -> float:
    return sum(valeurs) / len(valeurs) if valeurs else 0.0


def contexte(bougies: Sequence[Candle], call: bool,
             features_signal: Mapping[str, float] | None = None
             ) -> dict[str, float]:
    """Le contexte du signal pris à la clôture de `bougies[-1]`."""
    # ⚠ UNE FENÊTRE FIXE. Le rejeu passe au plus `lookback` + 1 bougies, la
    # course parfois davantage : sans cette coupe, « volatilité habituelle »
    # ne serait pas mesurée sur la même durée, et une leçon apprise en rejeu
    # ne voudrait plus dire la même chose en direct.
    bougies = bougies[-FENETRE:]
    if len(bougies) < 20:
        return {}
    sens = 1.0 if call else -1.0
    der = bougies[-1]
    amplitude = _moyenne([c.high - c.low for c in bougies[-60:]]) or 1e-12

    # Les heures CLOSES seulement, comme la stratégie : l'heure en cours
    # n'a pas encore de clôture.
    fermetures: dict[int, float] = {}
    for c in bougies:
        fermetures[c.ts_sec // 3600] = c.close
    heures = sorted(fermetures)[:-1]
    tendance = 0.0
    if len(heures) >= 4:
        tendance = (fermetures[heures[-1]] - fermetures[heures[-4]]) * sens

    il_y_a_15 = bougies[-16].close if len(bougies) >= 16 else bougies[0].close
    contre = 0
    for c in reversed(bougies):
        if (c.close - c.open) * sens < 0:
            contre += 1
        else:
            break
    etendue = der.high - der.low
    if etendue > 0:
        corps = abs(der.close - der.open) / etendue
        meche = ((min(der.open, der.close) - der.low) if call
                 else (der.high - max(der.open, der.close))) / etendue
    else:
        corps = meche = 0.0
    closes = [c.close for c in bougies[-20:]]
    ecart = statistics.pstdev(closes)
    bande = ((der.close - _moyenne(closes)) / (2 * ecart) * sens
             if ecart > 0 else 0.0)

    ctx = {
        "heure_utc": float(((der.ts_sec + 60) // 3600) % 24),
        "tendance_h1": tendance / amplitude,
        "elan_15m": (der.close - il_y_a_15) * sens / amplitude,
        "volatilite_relative": (
            _moyenne([c.high - c.low for c in bougies[-15:]])
            / (_moyenne([c.high - c.low for c in bougies]) or 1e-12)),
        "bougies_contre": float(contre),
        "corps_signal": corps,
        "meche_rejet": meche,
        "position_bande": bande,
    }
    f = features_signal or {}
    if "niveau" in f:
        ctx["distance_niveau"] = (der.close - float(f["niveau"])) * sens / amplitude
    for nom in ("touches", "entrees_deja_offertes"):
        if nom in f:
            ctx[nom] = float(f[nom])
    return {k: round(v, 4) for k, v in ctx.items()}
