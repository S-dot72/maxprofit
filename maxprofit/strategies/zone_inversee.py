"""
Le support cassé devient résistance — et la résistance cassée, support.

Demandé le 2026-10-02, sur une vente USD/JPY perdue : « la zone a été
cassée ; un support cassé devient résistance ; une fois que le marché
revient dessus, il aura plus tendance à le faire plonger ».

ZoneH1 déclarait MORTE une zone qu'une clôture traversait, et l'oubliait.
156,40 avait servi de support de 09:09 à 09:27, avait été cassé à 09:30,
puis retesté et rejeté vers 10:15 — la vente évidente. La stratégie ne l'a
pas vue ; elle a vendu à 10:39 une autre résistance, une petite pique
récente déjà touchée plusieurs fois, et le marché est reparti à la hausse.

`ZoneInversee` garde toutes les zones de ZoneH1 et leur ajoute leur
INVERSE : quand une clôture casse une zone, son niveau renaît, au même
prix, dans le rôle opposé, à partir de la bougie de cassure. Il se trade
ensuite exactement comme une zone de ZoneH1 : touché, respecté depuis
l'autre côté, dans le sens de la tendance H1, et mort s'il est cassé à son
tour. Le signal porte `inversee = 1` quand il vient d'un niveau inversé,
pour que le laboratoire juge ces entrées à part.
"""

from __future__ import annotations

from dataclasses import replace

from maxprofit.strategies.zone_h1 import ZoneH1


class ZoneInversee(ZoneH1):
    """ZoneH1 plus les zones cassées, reprises dans le rôle inverse."""

    name = "zone_h1_inversee"

    def _zones(self, bougies) -> list[tuple[int, float, int]]:
        zones = super()._zones(bougies)
        self._inversees: set[tuple[float, int]] = set()
        sortie = list(zones)
        for conf, prix, s in zones:
            marge = prix * self.p.tolerance_pct / 100
            for k in range(conf + 1, len(bougies)):
                c = bougies[k].close
                if (s > 0 and c < prix - marge) or (s < 0 and c > prix + marge):
                    sortie.append((k, prix, -s))
                    self._inversees.add((prix, -s))
                    break
        return sortie

    def evaluer(self, view):
        evaluation = super().evaluer(view)
        signal = evaluation.signal
        if signal is None:
            return evaluation
        sens = 1 if signal.features.get("niveau") is not None and \
            signal.direction.name == "CALL" else -1
        inversee = (signal.features.get("niveau"), sens) in getattr(
            self, "_inversees", set())
        return replace(evaluation, signal=replace(
            signal, features={**signal.features,
                              "inversee": 1.0 if inversee else 0.0},
            reason=("support cassé devenu résistance" if sens < 0 else
                    "résistance cassée devenue support")
            if inversee else signal.reason))
