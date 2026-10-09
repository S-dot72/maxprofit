"""
La persistance des bougies : après N bougies de même couleur, la suite ?

--- D'où elle vient --------------------------------------------------------

Hypothèse de l'utilisateur, le 2026-10-08, devenue une dimension du
laboratoire : « après 5 bougies vertes consécutives, la 6e a-t-elle une
probabilité particulière d'être verte ? » — et, symétriquement, pour les
rouges. Deux réponses sont possibles, et toutes deux exploitables :

- la PERSISTANCE : plus la série est longue, plus elle continue ;
- l'ESSOUFFLEMENT : passé un certain nombre, elle se retourne.

--- Ce qui est mesuré -----------------------------------------------------

Sur TOUTES les bougies M1 des paires rejouées, pas seulement nos signaux :
pour chaque bougie qui termine une série de N bougies de même couleur
(N = 1 à 8, « 8 » valant « 8 ou plus »), la position « dans le sens de la
série » prise à sa clôture est jugée à 1, 2, 3, 4, 5, 10 et 15 minutes.
Prédire la bougie suivante n'est pas gagner une option de 5 minutes : la
série peut continuer une minute puis se retourner.

Le taux de continuation au-dessus de 52,1 % rendrait le pari « dans le sens
de la série » rentable ; sous 47,9 %, c'est le pari inverse qui le serait.
Les 70 % les plus anciens de la période et les 30 % récents sont comptés à
part : un effet qui ne tient que sur une période est du bruit.

Causal par construction : la série est lue jusqu'à la bougie close, la
sortie strictement après. Une bougie sans corps interrompt la série, un
trou de collecte aussi.
"""

from __future__ import annotations

from typing import Callable, Sequence

from maxprofit.core.types import Candle

#: Longueur à partir de laquelle les séries sont regroupées.
N_MAX = 8
ECHEANCES: tuple[int, ...] = (60, 120, 180, 240, 300, 600, 900)


class Persistance:
    """Accumule, paire après paire, les issues des séries de bougies."""

    def __init__(self, coupure_ts: int, echeances: Sequence[int] = ECHEANCES,
                 n_max: int = N_MAX):
        self.coupure = int(coupure_ts)
        self.echeances = tuple(echeances)
        self.n_max = n_max
        #: {couleur: {N: {échéance: [anciens, gagnés, récents, gagnés]}}}
        self.comptes: dict[str, dict[int, dict[int, list[int]]]] = {
            "verte": {}, "rouge": {}}

    def ajouter(self, bougies: Sequence[Candle],
                respirer: Callable[[], None] | None = None) -> None:
        """`respirer`, appelée régulièrement, laisse la main à la course :
        ce calcul tourne dans le même processus qu'elle."""
        completes = sorted((b for b in bougies if b.complete),
                           key=lambda b: b.ts_sec)
        closes = {b.ts_sec: b.close for b in completes}
        serie, couleur, precedent = 0, 0, None
        for i, c in enumerate(completes):
            if respirer is not None and i and i % 5000 == 0:
                respirer()
            col = (c.close > c.open) - (c.close < c.open)
            contigu = precedent is not None and \
                precedent.ts_sec == c.ts_sec - 60
            if col and contigu and col == couleur:
                serie += 1
            else:
                serie = 1 if col else 0
            couleur, precedent = col, c
            if not serie:
                continue
            table = self.comptes["verte" if col > 0 else "rouge"].setdefault(
                min(serie, self.n_max), {})
            k = 0 if c.ts_sec < self.coupure else 2
            for sec in self.echeances:
                sortie = closes.get(c.ts_sec + sec)
                if sortie is None or sortie == c.close:
                    continue
                cellule = table.setdefault(sec, [0, 0, 0, 0])
                cellule[k] += 1
                cellule[k + 1] += int((sortie - c.close) * col > 0)

    def resultat(self) -> dict:
        """En JSON : les clés sont du texte."""
        return {"coupure": self.coupure, "n_max": self.n_max,
                "echeances": list(self.echeances),
                **{nom: {str(n): {str(sec): list(c)
                                  for sec, c in sorted(par_sec.items())}
                         for n, par_sec in sorted(table.items())}
                   for nom, table in self.comptes.items()}}
