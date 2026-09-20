"""
La garde : ce module est le seul du projet qui puisse engager de l'argent.

--- ⚠ Il refuse par défaut ------------------------------------------------

Tout le reste du projet LIT. Ici on écrit chez le broker, et une erreur ne se
corrige pas par un correctif : l'ordre est parti.

La règle est donc inversée par rapport au reste du code. Ailleurs, une valeur
absente lève parce qu'on ne veut pas de défaut silencieux. Ici, une valeur
absente REFUSE : ne pas savoir si le compte est démo doit avoir exactement le
même effet que savoir qu'il est réel.

    isDemo absent du jeton   -> refus
    isDemo = 0               -> refus
    isDemo = 1               -> autorisé, et c'est le seul cas

--- ⚠ Pourquoi le jeton et pas la configuration ---------------------------

`PocketOptionSource(demo=True)` est une INTENTION. Le champ `isDemo` du jeton
est un FAIT : il vient de la session ouverte chez le broker. Les deux peuvent
diverger — on peut très bien passer `demo=True` avec un jeton capturé sur le
compte réel, et la bibliothèque se connectera au compte réel sans rien dire.

C'est arrivé dans une variante proche pendant la collecte : `_est_demo()` lisait
`global_value.DEMO`, qui vaut `None` avant construction, et l'on forçait la
mauvaise région sans le voir. La leçon est la même : on interroge la source de
vérité, pas la variable qu'on a soi-même posée.

--- Les plafonds -----------------------------------------------------------

Un compte démo se recharge, donc les plafonds ne protègent pas l'argent. Ils
protègent la MESURE : une sonde qui part en boucle et place mille ordres en
dix minutes fausse ce qu'elle mesure — le broker limite le débit, les
exécutions se dégradent, et l'on conclut à une latence qu'on a créée soi-même.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from maxprofit.core.errors import BotError

#: Mise maximale admise par ordre, quelle que soit la configuration. Un
#: garde-fou de dernier recours : sur un compte démo il ne protège rien, mais
#: il rend impossible qu'un zéro de trop passe inaperçu le jour où un jeton
#: réel se glisse malgré le reste.
MISE_MAX_ABSOLUE = 10.0


class CompteRefuse(BotError):
    """Le compte n'est pas démontré démo. Aucun ordre ne partira.

    Classe distincte pour qu'un appelant ne puisse pas l'attraper par mégarde
    avec un `except BotError` générique destiné aux erreurs de données.
    """


def est_demo(jeton: str) -> bool | None:
    """Lit `isDemo` dans le jeton. `None` si le champ est ABSENT.

    Trois valeurs de retour et non deux : « absent » n'est pas « faux ». Le
    distinguer permet à l'appelant de dire POURQUOI il refuse, et un refus
    dont on comprend la cause se répare ; un refus opaque se contourne.
    """
    trouve = re.search(r'"isDemo"\s*:\s*(\d+)', jeton or "")
    if trouve is None:
        return None
    return trouve.group(1) == "1"


def exiger_un_compte_demo(jeton: str) -> None:
    """Lève `CompteRefuse` sauf si le jeton prouve que le compte est démo."""
    marque = est_demo(jeton)
    if marque is True:
        return
    if marque is False:
        raise CompteRefuse(
            "Le jeton porte « isDemo: 0 » : c'est un compte RÉEL. Aucun ordre "
            "ne partira. Recapturez un jeton sur le compte démo avec "
            "outils/capturer_ssid.py.")
    raise CompteRefuse(
        "Le jeton ne porte aucun champ « isDemo ». Ne pas savoir si le compte "
        "est démo a exactement le même effet ici que savoir qu'il est réel : "
        "on refuse. Recapturez le jeton — un jeton complet porte ce champ.")


@dataclass(frozen=True)
class Plafonds:
    """Ce qu'une session de mesure a le droit de faire.

    Aucune valeur par défaut sur la mise (§5) : elle touche à l'argent, donc
    elle est fournie ou l'objet n'existe pas. Les deux autres en ont un, parce
    qu'ils bornent une DURÉE d'expérience et non un montant.
    """

    mise: float
    ordres_max: int = 200
    duree_max_sec: int = 6 * 3600

    def __post_init__(self) -> None:
        if not (0 < self.mise <= MISE_MAX_ABSOLUE):
            raise BotError(
                f"mise hors ]0,{MISE_MAX_ABSOLUE}] : {self.mise}. Le plafond "
                f"absolu ne protège pas un compte démo — il rend impossible "
                f"qu'un zéro de trop passe inaperçu si un jeton réel se glisse.")
        if not (1 <= self.ordres_max <= 2000):
            raise BotError(
                f"ordres_max hors [1,2000] : {self.ordres_max}. Au-delà, le "
                f"broker limite le débit et la sonde mesure une latence "
                f"qu'elle a créée elle-même.")
        if self.duree_max_sec < 60:
            raise BotError(
                f"duree_max_sec trop courte : {self.duree_max_sec}")

    @property
    def engagement_max(self) -> float:
        """Ce que la session peut engager au total, si tout est perdu."""
        return self.mise * self.ordres_max

    def resume(self) -> str:
        return (f"{self.ordres_max} ordres max à {self.mise:.2f} $ "
                f"({self.engagement_max:.2f} $ engagés au pire), "
                f"{self.duree_max_sec / 3600:.1f} h max")
