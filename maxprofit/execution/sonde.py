"""
La sonde d'exécution : elle entre AU HASARD, et c'est le point.

--- ⚠ Pourquoi des entrées aléatoires, et pas une stratégie ---------------

Les quatre écarts mesurés — payout appliqué, prix d'entrée, latence, durée
réelle — ne dépendent en RIEN de la règle qui a décidé d'entrer. Le broker
n'applique pas un autre payout selon que le signal vient d'un RSI ou d'une
pièce lancée en l'air.

Trois conséquences, et la troisième est la vraie raison :

1. La mesure est possible AUJOURD'HUI, alors qu'aucune stratégie ne tient.
2. Elle se règle en quelques dizaines d'ordres, pas en milliers.
3. **Elle ne peut pas être confondue avec une mesure d'avantage.** Une sonde
   qui utiliserait une stratégie rendrait un taux de réussite, quelqu'un le
   lirait, et le coût d'exécution se mêlerait à l'avantage supposé sans qu'on
   puisse séparer les deux. L'aléatoire rend cette confusion impossible : son
   taux de réussite est 50 % par construction, et personne ne peut le prendre
   pour une découverte.

C'est aussi ce qu'impose `tests/test_layering.py`, qui refuse à `execution`
l'accès à `strategies`.

--- Ce que la sonde ne mesure pas ------------------------------------------

Elle ne dit rien de la rentabilité. Son espérance est négative par
construction — 50 % de réussite contre un seuil de 52,08 %. Voir le solde
descendre pendant une session de mesure est le comportement ATTENDU, pas un
symptôme.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Sequence

from maxprofit.core.errors import BotError
from maxprofit.execution.courtier import CourtierDemo
from maxprofit.execution.journal import Execution, JournalExecution

log = logging.getLogger(__name__)

SENS = ("call", "put")


@dataclass(frozen=True)
class Campagne:
    """Ce qu'une session de mesure va faire, déclaré avant de la lancer."""

    paires: tuple[str, ...]
    expiration_sec: int
    ordres: int
    #: Pause entre deux ordres. Pas un confort : deux ordres collés mesurent la
    #: file d'attente du broker plutôt que sa latence.
    pause_sec: float = 5.0
    graine: int = 20260920

    def __post_init__(self) -> None:
        if not self.paires:
            raise BotError("Une campagne sans paire ne mesure rien.")
        if self.expiration_sec <= 0:
            raise BotError(
                f"expiration_sec doit être positive : {self.expiration_sec}")
        if self.ordres < 1:
            raise BotError(f"ordres doit valoir au moins 1 : {self.ordres}")
        if self.pause_sec < 0:
            raise BotError(f"pause_sec négative : {self.pause_sec}")

    def resume(self) -> str:
        return (f"{self.ordres} ordres aléatoires sur "
                f"{', '.join(self.paires)}, échéance {self.expiration_sec} s, "
                f"pause {self.pause_sec:.0f} s")


def mesurer(courtier: CourtierDemo, journal: JournalExecution,
            campagne: Campagne) -> list[Execution]:
    """Place les ordres, attend chaque dénouement, écrit tout.

    Chaque ordre est écrit AVANT de passer au suivant, dénoué ou non. Une
    session interrompue à la trentième minute laisse donc trente minutes de
    mesures exploitables, et non un fichier vide.
    """
    alea = random.Random(campagne.graine)
    for pair in campagne.paires:
        courtier.suivre(pair)

    faits: list[Execution] = []
    for numero in range(1, campagne.ordres + 1):
        pair = alea.choice(campagne.paires)
        sens = alea.choice(SENS)
        try:
            execution = courtier.placer(pair, sens, campagne.expiration_sec)
        except BotError as erreur:
            log.error("Ordre %d/%d abandonné : %s", numero, campagne.ordres,
                      erreur)
            break
        if execution.accepte:
            execution = courtier.denouer(execution)
        journal.ecrire(execution)
        faits.append(execution)

        log.info("%d/%d %s %s -> %s", numero, campagne.ordres, pair, sens,
                 execution.resultat or execution.refus or "sans suite")
        if numero < campagne.ordres:
            time.sleep(campagne.pause_sec)
    return faits


def sigma_sur_horizon(closes: Sequence[float], pas: int) -> float:
    """Écart-type du mouvement de prix sur `pas` bougies, en unité de prix.

    C'est l'échelle qui convertit un glissement en points de taux de réussite.
    Elle est MESURÉE sur les données collectées et jamais supposée : la
    supposer reviendrait à choisir la réponse de la mesure E2.
    """
    if pas < 1:
        raise BotError(f"pas doit valoir au moins 1 : {pas}")
    ecarts = [closes[i + pas] - closes[i] for i in range(len(closes) - pas)]
    if len(ecarts) < 30:
        raise BotError(
            f"{len(ecarts)} écart(s) : trop peu pour une échelle fiable. "
            f"Sans elle, le glissement mesuré n'a pas d'unité interprétable.")
    moyenne = sum(ecarts) / len(ecarts)
    variance = sum((e - moyenne) ** 2 for e in ecarts) / len(ecarts)
    return variance ** 0.5
