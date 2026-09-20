"""
Le sceau : une période qu'on ne regarde pas, et qui sert une seule fois.

--- ⚠ Pourquoi c'est du code et pas une règle de conduite ------------------

La phase 9 du protocole dit : bloquer une période entière, ne pas la regarder
pendant la recherche, et l'utiliser UNE FOIS à la fin. C'est la seule épreuve
qui ne puisse pas être trichée par accident.

Mais une règle de conduite ne résiste pas à six semaines. On se dit « je jette
juste un œil pour vérifier que le chargement marche », et la période scellée
est brûlée sans que personne ne l'ait décidé. Ce qui est brûlé ne se répare
pas : on ne peut pas oublier un chiffre qu'on a vu.

Alors le sceau est un objet. Y accéder demande un appel nommé, une raison
écrite, et laisse une trace. On peut toujours contourner — c'est du Python,
pas un coffre-fort. Mais on ne peut plus le faire SANS LE SAVOIR, et c'est
exactement la différence qui compte.

--- Ce que le sceau ne protège pas -----------------------------------------

Il ne protège pas contre une recherche recommencée après un échec en période
scellée. Si l'on brûle le sceau, qu'on n'aime pas le résultat, qu'on retourne
chercher et qu'on revient : la deuxième mesure ne vaut plus rien, et aucun
code ne peut l'empêcher.

La seule défense est le REGISTRE, qui garde la trace de l'ouverture. Le nombre
d'ouvertures est une donnée de l'expérience, au même titre que le nombre de
signaux.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from maxprofit.core.errors import BotError


def _iso(ts_sec: int) -> str:
    return datetime.fromtimestamp(ts_sec, timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


@dataclass(frozen=True)
class Periode:
    """Un intervalle `[debut, fin[` en secondes UTC."""

    debut_sec: int
    fin_sec: int

    def __post_init__(self) -> None:
        if self.fin_sec <= self.debut_sec:
            raise BotError(
                f"Période vide ou inversée : {self.debut_sec} -> "
                f"{self.fin_sec}")

    @property
    def duree_sec(self) -> int:
        return self.fin_sec - self.debut_sec

    @property
    def jours(self) -> float:
        return self.duree_sec / 86400

    def contient(self, ts_sec: int) -> bool:
        return self.debut_sec <= ts_sec < self.fin_sec

    def chevauche(self, autre: "Periode") -> bool:
        return (self.debut_sec < autre.fin_sec
                and autre.debut_sec < self.fin_sec)

    def __str__(self) -> str:
        return (f"{_iso(self.debut_sec)} -> {_iso(self.fin_sec)} "
                f"({self.jours:.1f} j)")


@dataclass
class Decoupage:
    """Une période de recherche, une période scellée, et la trace des accès.

    Mutable, et c'est voulu : le compteur d'ouvertures est un fait de
    l'expérience qui s'incrémente. Le figer obligerait à le tenir ailleurs, et
    un compteur tenu ailleurs finit par ne plus être tenu.
    """

    recherche: Periode
    scelle: Periode
    #: Les raisons données à chaque ouverture, dans l'ordre. Leur NOMBRE est ce
    #: qui compte : au-delà de un, le sceau n'a plus de valeur probante.
    ouvertures: list[str]

    def __init__(self, recherche: Periode, scelle: Periode):
        if recherche.chevauche(scelle):
            raise BotError(
                f"La période de recherche et la période scellée se "
                f"chevauchent : {recherche} / {scelle}. Le chevauchement rend "
                f"la validation sans valeur, et il ne se voit pas dans les "
                f"chiffres de sortie.")
        if scelle.debut_sec < recherche.fin_sec:
            raise BotError(
                f"La période scellée précède la recherche : {scelle} avant "
                f"{recherche}. Valider sur le PASSÉ d'une règle mise au point "
                f"sur le futur est un look-ahead à l'échelle de l'expérience.")
        self.recherche = recherche
        self.scelle = scelle
        self.ouvertures = []

    @classmethod
    def depuis_la_fin(cls, debut_sec: int, fin_sec: int,
                      part_scellee: float = 0.25) -> "Decoupage":
        """Scelle la DERNIÈRE part de la période disponible.

        La dernière et non une au hasard : une stratégie se déploie vers
        l'avant, et la seule question qui compte est « aurait-elle tenu sur ce
        qui est venu APRÈS ? ». Un bloc scellé pris au milieu répondrait à une
        question qu'on ne se pose jamais en trading.
        """
        if not (0 < part_scellee < 1):
            raise BotError(f"part_scellee hors ]0,1[ : {part_scellee}")
        total = fin_sec - debut_sec
        if total <= 0:
            raise BotError(f"Période vide : {debut_sec} -> {fin_sec}")
        coupure = fin_sec - int(total * part_scellee)
        if coupure <= debut_sec:
            raise BotError(
                f"Rien ne resterait pour la recherche : {part_scellee:.0%} de "
                f"{total / 86400:.1f} jours.")
        return cls(Periode(debut_sec, coupure), Periode(coupure, fin_sec))

    @property
    def scelle_intact(self) -> bool:
        return not self.ouvertures

    def ouvrir_le_sceau(self, raison: str) -> Periode:
        """Rend la période scellée. À n'appeler qu'une fois, à la toute fin.

        La raison est obligatoire et n'est pas décorative : c'est elle qu'on
        relira pour savoir si l'ouverture était l'examen final ou un « juste
        pour vérifier » qui a coûté la validité de l'expérience.
        """
        if not raison or not raison.strip():
            raise BotError(
                "Ouvrir le sceau sans raison écrite n'est pas permis. La "
                "raison est ce qui permettra de dire, plus tard, si cette "
                "ouverture était l'examen final ou une curiosité.")
        self.ouvertures.append(raison.strip())
        return self.scelle

    def avertissement(self) -> str | None:
        """Ce qu'un rapport doit afficher si le sceau a déjà servi."""
        if self.scelle_intact:
            return None
        n = len(self.ouvertures)
        if n == 1:
            return (f"⚠ Sceau ouvert une fois : « {self.ouvertures[0]} ». "
                    f"Le résultat en période scellée ne vaut que s'il "
                    f"s'agissait de l'examen final.")
        return (
            f"⚠ SCEAU OUVERT {n} FOIS. La période scellée n'a plus de valeur "
            f"probante : une règle mise au point en la consultant est ajustée "
            f"dessus, qu'on l'ait voulu ou non. Raisons : "
            + " | ".join(self.ouvertures))

    def resume(self) -> str:
        etat = "intact" if self.scelle_intact else \
            f"OUVERT {len(self.ouvertures)}x"
        return (f"recherche : {self.recherche}\n"
                f"scellé    : {self.scelle}  [{etat}]")
