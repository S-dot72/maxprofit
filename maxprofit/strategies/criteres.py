"""
Les critères qui VALIDENT un trade — ils ne le décident jamais.

Précisé par l'utilisateur le 2026-10-03 : « le ZigZag ne doit pas
constituer une stratégie pour prendre des ordres ; de même pour les zones
cassées. Ce sont juste des critères permettant de mieux cerner le marché et
de valider le trade. »

Jusqu'ici, ZigZag et zones inversées avaient été essayés comme SOURCES de
signaux, et ils produisaient surtout du bruit. Ici, ZoneH1 décide (et la
confirmation M1 confirme) ; ces mesures disent seulement si le contexte du
marché soutient l'ordre :

- `zigzag_pivot_zone` : la zone du signal coïncide avec un vrai creux
  (achat) ou un vrai sommet (vente) du ZigZag — une vague y a réellement
  tourné, ce n'est pas un accident de deux minutes ;
- `zigzag_structure` : +1 si la structure va dans le sens du trade (creux
  montants pour un achat, sommets descendants pour une vente), −1 si elle
  va contre, 0 si on ne sait pas encore ;
- `niveau_inverse` : la zone est un ancien niveau cassé qui a changé de
  rôle (support devenu résistance pour une vente, et l'inverse) ;
- `obstacle_inverse` : à combien d'amplitudes M1 se trouve, dans le sens
  du trade, un niveau cassé qui joue CONTRE lui (99 s'il n'y en a pas).

Toutes causales : calculées sur les bougies que la stratégie a vues, les
pivots du ZigZag n'existant qu'une fois confirmés.
"""

from __future__ import annotations

from maxprofit.indicators.base import PivotKind
from maxprofit.strategies.zone_h1 import ZoneH1
from maxprofit.strategies.zones_zigzag import pivots

AUCUN_OBSTACLE = 99.0


def _niveaux_inverses(bougies, zones_de) -> list[tuple[float, int]]:
    """(prix, nouveau rôle) de chaque zone cassée par une clôture."""
    sortie = []
    for conf, prix, s in zones_de(bougies):
        marge = prix * 0.02 / 100
        for k in range(conf + 1, len(bougies)):
            c = bougies[k].close
            if (s > 0 and c < prix - marge) or (s < 0 and c > prix + marge):
                sortie.append((prix, -s))
                break
    return sortie


def criteres(bougies, call: bool, niveau: float | None) -> dict[str, float]:
    """Les critères de validation, pour un signal pris à `bougies[-1]`."""
    recentes = bougies[-60:]
    amplitude = (sum(b.high - b.low for b in recentes) / len(recentes)
                 if recentes else 0.0) or 1e-9
    sortie: dict[str, float] = {}
    pv = pivots(bougies)
    voulu = PivotKind.BAS if call else PivotKind.HAUT
    if niveau is not None:
        sortie["zigzag_pivot_zone"] = float(any(
            p.kind is voulu and abs(p.price - niveau) <= amplitude
            for p in pv))
    extremes = [p.price for p in pv if p.kind is voulu][-2:]
    if len(extremes) == 2:
        montant = extremes[1] > extremes[0]
        sortie["zigzag_structure"] = 1.0 if montant == call else -1.0
    else:
        sortie["zigzag_structure"] = 0.0
    inverses = _niveaux_inverses(bougies, ZoneH1()._zones)
    role = 1 if call else -1
    if niveau is not None:
        sortie["niveau_inverse"] = float(any(
            r == role and abs(prix - niveau) <= amplitude
            for prix, r in inverses))
    entree = bougies[-1].close
    distances = [(prix - entree) / amplitude if call
                 else (entree - prix) / amplitude
                 for prix, r in inverses if r == -role]
    devant = [d for d in distances if d > 0]
    sortie["obstacle_inverse"] = round(min(devant), 2) if devant \
        else AUCUN_OBSTACLE
    return sortie
