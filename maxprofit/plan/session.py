"""
La session — le pont entre les signaux et le plan.

Une session déroule l'échelle sur des signaux successifs et rend un résultat à
`Journee`. Elle **ne décide de rien** : elle compte.

    ouvrir(solde)      -> la mise du pas 1
    enregistrer(True)  -> GAGNÉE, +gain visé
    enregistrer(False) -> la mise du pas 2, ou PERDUE si `pas_max` est atteint

--- ⚠ Ce qu'elle ne fait pas, et c'est le point à ne pas rater --------------

**Elle ne fabrique aucun signal.** Si aucun signal n'arrive pour le pas suivant,
la session reste ouverte — ou expire, et l'appelant le décide. Elle ne force
jamais une entrée pour « finir » la martingale.

Forcer serait exactement la façon dont une gestion de mise se met à décider à
la place de la stratégie : le deuxième pas coûte deux fois le premier, il est
donc tentant de le placer coûte que coûte pour récupérer, et l'on se retrouve à
prendre un trade que la stratégie n'a jamais demandé. Le backtest mesurerait
alors une stratégie qui n'existe pas.

C'est la même frontière que partout ailleurs ici : `Signal` ne porte pas de
mise, et ce module ne produit pas de `Signal`.

--- La session INTERROMPUE -------------------------------------------------

Une session peut se terminer sans être ni gagnée ni perdue : la journée
s'arrête — objectif atteint, perte maximale, sessions épuisées — alors qu'un
pas est engagé. Cet état existe et porte son nom, plutôt que d'être compté
comme une perte.

La différence n'est pas cosmétique : une session interrompue n'a coûté que les
pas déjà joués, pas l'exposition entière. La compter comme perdue surestimerait
la perte, et la compter comme gagnée l'effacerait.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from maxprofit.core.errors import BotError
from maxprofit.plan.progression import Echelle


class EtatSession(enum.Enum):
    """Où en est une session. Quatre états, et aucun « autre »."""

    OUVERTE = "ouverte"
    GAGNEE = "gagnée"
    PERDUE = "perdue"
    #: Arrêtée en cours par une garde de la journée.
    INTERROMPUE = "interrompue"

    @property
    def terminee(self) -> bool:
        return self is not EtatSession.OUVERTE

    def __str__(self) -> str:
        return self.value


@dataclass
class Session:
    """Une descente d'échelle. Mutable : c'est un état qui avance."""

    echelle: Echelle
    pas_joues: int = 0
    etat: EtatSession = EtatSession.OUVERTE
    #: Les mises réellement engagées, dans l'ordre. Conservées plutôt que
    #: recalculées : c'est ce qui permet de dire ce qu'une session a coûté même
    #: si elle a été interrompue.
    engagees: list[float] = field(default_factory=list)

    def mise_courante(self) -> float:
        """La mise du prochain pas. Lève si la session est terminée.

        Lève plutôt que de rendre zéro : un zéro se propagerait en silence dans
        un calcul de solde, et l'on chercherait longtemps pourquoi une session
        n'a rien coûté.
        """
        if self.etat.terminee:
            raise BotError(
                f"Session {self.etat} : il n'y a plus de mise à placer.")
        return self.echelle.mises()[self.pas_joues]

    def enregistrer(self, gagne: bool) -> EtatSession:
        """Comptabilise un pas résolu, et rend le nouvel état.

        Appelée UNIQUEMENT quand un trade réel s'est dénoué. Le nombre de pas
        joués vient donc du réel, jamais d'une simulation de ce qui aurait dû
        se produire.
        """
        if self.etat.terminee:
            raise BotError(
                f"Session {self.etat} : elle ne peut plus enregistrer de pas.")

        self.engagees.append(self.mise_courante())
        self.pas_joues += 1

        if gagne:
            self.etat = EtatSession.GAGNEE
        elif self.pas_joues >= self.echelle.pas_max:
            self.etat = EtatSession.PERDUE
        return self.etat

    def interrompre(self) -> EtatSession:
        """Arrête une session en cours, sans la compter comme perdue."""
        if self.etat.terminee:
            return self.etat
        self.etat = EtatSession.INTERROMPUE
        return self.etat

    @property
    def engage(self) -> float:
        """Ce qui a réellement été misé, pas ce qui aurait pu l'être."""
        return sum(self.engagees)

    @property
    def montant(self) -> float:
        """Le résultat signé de la session, pour `Journee.enregistrer`.

        Gagnée : le gain visé — l'échelle est construite pour que le pas
        gagnant rembourse tout le passé et le laisse.
        Perdue ou interrompue : l'opposé de ce qui a été engagé.
        """
        if self.etat is EtatSession.OUVERTE:
            raise BotError(
                "Session ouverte : son résultat n'est pas encore connu.")
        if self.etat is EtatSession.GAGNEE:
            return self.echelle.gain_vise
        return -self.engage
