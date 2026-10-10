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

#: `espace_obstacle` quand aucun sommet (achat) ou creux (vente) confirmé ne
#: barre la route : le champ est libre.
CHAMP_LIBRE = 99.0

#: Ce qui est mesuré, et comment le dire à un humain.
LIBELLES: dict[str, str] = {
    "heure_utc": "heure (UTC)",
    "tendance_h1": "tendance des 3 dernières heures dans le sens du trade",
    "elan_15m": "élan des 15 dernières minutes dans le sens du trade",
    "elan_30m": "élan des 30 dernières minutes dans le sens du trade",
    # ⚠ Ce que la stratégie NE VOIT PAS : sa tendance H1 ne lit que des
    # heures closes. Une chute de 1 % en vingt minutes dans l'heure en cours
    # (AUD/CAD, 2026-09-30, 21:16-21:36 UTC-4) lui est invisible, et elle
    # achète le rebond sur zone en la croyant dans le sens de la tendance.
    "mouvement_heure": "mouvement depuis le début de l'heure en cours "
                       "dans le sens du trade",
    "volatilite_relative": "volatilité des 15 dernières minutes / habituelle",
    "bougies_contre": "bougies consécutives contre le trade",
    "corps_signal": "corps de la bougie de signal (part de son amplitude)",
    "meche_rejet": "mèche de rejet de la bougie de signal",
    "position_bande": "position dans la bande de Bollinger (sens du trade)",
    # Mesurée À L'ENTRÉE (en direct, et au rejeu de la confirmation M1) :
    # c'est l'« entrée tardive » — CALL GBPUSD du 08/10 pris 5 amplitudes
    # au-dessus du support, une fois le rebond déjà consommé.
    "distance_niveau": "distance de l'entrée à la zone",
    # La fatigue de la zone, demandée le 08/10 (GBPAUD, 3e touche d'un
    # ancien plafond) : « sur les 15 dernières bougies, seulement 4 vertes,
    # les vendeurs ont le momentum » et « la précédente impulsion est
    # faible, celle qui suit sera encore plus faible ».
    "favorables_15": "part des 15 dernières bougies dans le sens du trade",
    "rebond_15": "plus fort rebond depuis la zone sur les 15 bougies "
                 "précédentes",
    # Les SÉQUENCES de bougies, demandées le 08/10 comme dimension à part
    # entière : la persistance (« après 5 vertes, la 6e ? »), l'amplitude de
    # la série, son rythme (accélère ou s'essouffle), et l'alternance.
    "serie_sens": "bougies consécutives de même couleur jusqu'à l'entrée "
                  "(+ dans le sens du trade, − contre)",
    "amplitude_serie": "chemin parcouru par cette série dans le sens du "
                       "trade",
    "rythme_bougies": "taille des 3 dernières bougies / des 3 précédentes",
    "alternances_10": "changements de couleur sur les 10 dernières bougies",
    # EXP-020, demandée le 10/10 (CALL CADJPY pris à 117,239 sur un
    # support à 117,019, près des sommets précédents) : la QUALITÉ DU PRIX
    # D'ENTRÉE, au-delà de la seule distance à la zone.
    "taille_bougie": "taille de la bougie d'entrée / médiane des 15 "
                     "précédentes",
    "espace_obstacle": "place jusqu'au prochain sommet (achat) ou creux "
                       "(vente) confirmé du ZigZag",
    "part_parcourue": "part du chemin zone → prochain sommet/creux déjà "
                      "parcourue à l'entrée",
    "depuis_contact": "bougies écoulées depuis le dernier contact avec la "
                      "zone",
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
    heure_en_cours = 0.0
    if heures:
        heure_en_cours = (der.close - fermetures[heures[-1]]) * sens
    il_y_a_30 = bougies[-31].close if len(bougies) >= 31 else bougies[0].close

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
        "elan_30m": (der.close - il_y_a_30) * sens / amplitude,
        "mouvement_heure": heure_en_cours / amplitude,
        "volatilite_relative": (
            _moyenne([c.high - c.low for c in bougies[-15:]])
            / (_moyenne([c.high - c.low for c in bougies]) or 1e-12)),
        "bougies_contre": float(contre),
        "corps_signal": corps,
        "meche_rejet": meche,
        "position_bande": bande,
    }
    # La série en cours : bougies consécutives de même couleur, la dernière
    # comprise. Une bougie sans corps l'interrompt.
    couleur = (der.close > der.open) - (der.close < der.open)
    serie, debut = 0, der
    if couleur:
        for c in reversed(bougies):
            if (c.close > c.open) - (c.close < c.open) != couleur:
                break
            serie += 1
            debut = c
    ctx["serie_sens"] = float(serie * couleur * (1 if call else -1))
    ctx["amplitude_serie"] = ((der.close - debut.open) * sens / amplitude
                              if serie else 0.0)
    if len(bougies) >= 6:
        recentes = _moyenne([c.high - c.low for c in bougies[-3:]])
        avant = _moyenne([c.high - c.low for c in bougies[-6:-3]])
        ctx["rythme_bougies"] = recentes / avant if avant > 0 else 1.0
    dix = [(c.close > c.open) - (c.close < c.open) for c in bougies[-10:]]
    ctx["alternances_10"] = float(sum(
        1 for a, b in zip(dix, dix[1:]) if a and b and a != b))
    precedentes = sorted(c.high - c.low for c in bougies[-16:-1])
    if precedentes:
        mediane = precedentes[len(precedentes) // 2]
        ctx["taille_bougie"] = ((der.high - der.low) / mediane
                                if mediane > 0 else 1.0)
    obstacle = _prochain_pivot(bougies, call, der.close)
    ctx["espace_obstacle"] = (abs(obstacle - der.close) / amplitude
                              if obstacle is not None else CHAMP_LIBRE)
    quinze = bougies[-15:]
    ctx["favorables_15"] = sum(
        1 for c in quinze if (c.close - c.open) * sens > 0) / len(quinze)
    f = features_signal or {}
    if "niveau" in f:
        niveau = float(f["niveau"])
        ctx["distance_niveau"] = (der.close - niveau) * sens / amplitude
        # Jusqu'où le prix s'est éloigné de la zone, dans le sens du trade,
        # AVANT la bougie d'entrée : un rebond qui ne décolle pas est une
        # zone qui fatigue.
        avant = bougies[-16:-1]
        if avant:
            loin = (max(c.high for c in avant) - niveau if call
                    else niveau - min(c.low for c in avant))
            ctx["rebond_15"] = max(0.0, loin) / amplitude
        if obstacle is not None and (obstacle - niveau) * sens > 0:
            ctx["part_parcourue"] = ((der.close - niveau) * sens
                                     / ((obstacle - niveau) * sens))
        # Le dernier contact : une bougie venue à moins d'un quart
        # d'amplitude de la zone (par le bas pour un support, par le haut
        # pour une résistance). 60 si aucune dans l'heure.
        contact = 60
        for k, c in enumerate(reversed(bougies[-61:])):
            if (call and c.low <= niveau + 0.25 * amplitude) or \
                    (not call and c.high >= niveau - 0.25 * amplitude):
                contact = k
                break
        ctx["depuis_contact"] = float(contact)
    for nom in ("touches", "entrees_deja_offertes"):
        if nom in f:
            ctx[nom] = float(f[nom])
    return {k: round(v, 4) for k, v in ctx.items()}


def _prochain_pivot(bougies: Sequence[Candle], call: bool,
                    prix: float) -> float | None:
    """Le sommet (achat) ou le creux (vente) CONFIRMÉ du ZigZag le plus
    proche, au-delà du prix d'entrée : l'obstacle que le trade doit
    franchir. `None` si aucun ne barre la route. Causal : un pivot n'existe
    qu'une fois confirmé par les bougies vues."""
    from maxprofit.strategies.zones_zigzag import pivots
    try:
        liste = pivots(list(bougies))
    except Exception:                             # noqa: BLE001
        return None
    if call:
        devant = [p.price for p in liste
                  if p.kind.name == "HAUT" and p.price > prix]
        return min(devant) if devant else None
    devant = [p.price for p in liste
              if p.kind.name != "HAUT" and p.price < prix]
    return max(devant) if devant else None
