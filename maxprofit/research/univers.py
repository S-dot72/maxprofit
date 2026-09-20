"""
Le périmètre d'une expérience : quels actifs, quelles échéances.

--- ⚠ La règle qui justifie ce fichier -------------------------------------

**On ne mélange jamais l'OTC et le non-OTC dans une même analyse.** Elle est
imposée ici par le code, pas laissée à la discipline, parce que la mesure dit
que ce sont deux objets différents :

    kurtosis des paires OTC        2,92 à 3,00   -> gaussien pur
    kurtosis du forex réel         5 à 20

Une analyse qui mêle les deux moyenne une marche au hasard sans mémoire avec
un processus qui en a. Le résultat n'appartient à aucun des deux marchés, et
rien dans les chiffres de sortie ne le signale.

--- Ce qu'un univers n'est pas ---------------------------------------------

Ce n'est pas une liste de préférences. C'est une DÉCLARATION, faite avant de
regarder les résultats, de ce sur quoi l'expérience portera. Elle part au
registre avec le reste : une expérience dont on élargit l'univers après avoir
vu les chiffres est une expérience différente, et elle doit compter comme
telle dans la correction du nombre de tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from maxprofit.core.errors import BotError

#: Suffixe qui désigne un actif synthétique chez ce broker.
SUFFIXE_OTC = "_otc"

#: Échéances proposées par Pocket Option, en secondes. L'expérience en choisit,
#: elle n'en invente pas : mesurer une échéance que la plateforme ne vend pas
#: produirait un avantage inexécutable.
ECHEANCES_CONNUES_SEC = (30, 60, 120, 180, 300, 900, 1800)


def est_otc(pair: str) -> bool:
    return pair.endswith(SUFFIXE_OTC)


@dataclass(frozen=True)
class Univers:
    """Les actifs et les échéances d'une expérience. Immuable et homogène."""

    paires: tuple[str, ...]
    echeances_sec: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.paires:
            raise BotError("Un univers sans paire ne mesure rien.")
        if len(set(self.paires)) != len(self.paires):
            raise BotError(f"Paire répétée dans l'univers : {self.paires}")
        familles = {est_otc(p) for p in self.paires}
        if len(familles) > 1:
            otc = sorted(p for p in self.paires if est_otc(p))
            reel = sorted(p for p in self.paires if not est_otc(p))
            raise BotError(
                f"Un univers ne mélange pas l'OTC et le non-OTC. "
                f"OTC : {otc} ; réel : {reel}. Les deux n'ont pas la même "
                f"loi — kurtosis 2,9 contre 5 à 20 — et les moyenner produit "
                f"un résultat qui n'appartient à aucun des deux marchés.")
        if not self.echeances_sec:
            raise BotError("Un univers sans échéance ne mesure rien.")
        if len(set(self.echeances_sec)) != len(self.echeances_sec):
            raise BotError(
                f"Échéance répétée : {self.echeances_sec}. Compter deux fois "
                f"la même durée fausserait la correction du nombre de tests.")
        inconnues = [e for e in self.echeances_sec
                     if e not in ECHEANCES_CONNUES_SEC]
        if inconnues:
            raise BotError(
                f"Échéance(s) que la plateforme ne vend pas : {inconnues}. "
                f"Connues : {list(ECHEANCES_CONNUES_SEC)}. Un avantage mesuré "
                f"sur une durée inexécutable n'est pas un avantage.")

    @property
    def otc(self) -> bool:
        """Vrai si l'univers est synthétique. Homogène par construction."""
        return est_otc(self.paires[0])

    @property
    def famille(self) -> str:
        return "OTC" if self.otc else "réel"

    def __str__(self) -> str:
        return (f"{len(self.paires)} paire(s) {self.famille}, "
                f"échéances {'/'.join(str(e) for e in self.echeances_sec)} s")

    def signature(self) -> str:
        """Une forme stable et triée, pour le registre. Deux univers identiques
        écrits dans un ordre différent doivent donner la MÊME signature, sinon
        la même expérience compterait deux fois."""
        return (",".join(sorted(self.paires)) + "|"
                + ",".join(str(e) for e in sorted(self.echeances_sec)))
