"""
Le mode PLAN — capital, mises, et les gardes qui arrêtent.

--- Les deux modes, et pourquoi ils sont séparés ---------------------------

    mode TRADE SEUL   n'émet que des signaux. Il ignore le solde, les
                      positions ouvertes et le plan. C'est `Strategy`, inchangé.

    mode PLAN         consomme ces signaux, les dimensionne, et applique les
                      gardes. C'est ce module.

**Le dimensionnement ne doit JAMAIS modifier un signal**, et la séparation est
ce qui le garantit : `Signal` ne porte ni prix ni mise, et ce module ne connaît
aucune stratégie — `tests/test_layering.py` le refuse. Sans cette frontière, le
backtest ne mesurerait plus la stratégie mais la stratégie *plus* la mise, et
l'on ne saurait plus laquelle des deux gagne ou perd.

--- ⚠ Ce que ce module ne prétend pas faire --------------------------------

Il n'améliore pas les chances. Mesuré, et l'invariance est exacte :

    pari simple à 92 %   52,0833 %  de réussite nécessaire
    échelle à 2 pas      52,0833 %
    échelle à 7 pas      52,0833 %

Une gestion de capital **redistribue** les résultats ; elle n'en crée aucun. Ce
qu'elle décide, c'est la forme de la perte — étalée ou brutale — et c'est déjà
beaucoup.

> **Le chiffre qui doit rester sous les yeux.** À 49,11 % — le taux mesuré sur
> la collecte au 20 septembre — l'espérance est NÉGATIVE quelle que soit la
> progression. Le plan ne répare pas cela ; il borne ce que ça coûte.

--- Les gardes, et ce que chacune empêche ----------------------------------

| Garde | Empêche |
|---|---|
| `pas_max` (2) | qu'une session perdue emporte le capital — 1,96 % au lieu de 100 % |
| `sessions_perdues_max` | qu'une mauvaise série se poursuive dans la journée |
| `perte_journaliere_max_pct` | que le total du jour dépasse ce qu'on a décidé de risquer |
| `objectif_journalier_pct` | qu'un bon jour se transforme en mauvais — on s'arrête aussi sur un gain |

La dernière n'est pas une précaution de confort. Le plan vise 3,48 % par jour ;
continuer au-delà, c'est jouer un capital plus gros pour un objectif déjà
atteint.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from maxprofit.core.errors import BotError
from maxprofit.plan.progression import PAS_MAX_PAR_DEFAUT, Echelle

#: Marge de comparaison des pourcentages.
#:
#: Sans elle, un objectif de 3,48 % atteint exactement ne déclenchait PAS :
#: `258,70 − 250` vaut 8,699999999999989 en flottant, donc 3,4799999999999955 %,
#: donc strictement inférieur à 3,48. La garde restait muette au centième près —
#: exactement le genre de silence que ce projet traque.
TOLERANCE = 1e-9


class Mode(enum.Enum):
    """Ce que le système est autorisé à faire."""

    #: N'émet que des signaux. Aucun dimensionnement, aucun suivi de solde.
    TRADE_SEUL = "trade_seul"
    #: Dimensionne, compte, et arrête quand une garde le demande.
    PLAN = "plan"

    def __str__(self) -> str:
        return self.value


class Arret(enum.Enum):
    """Pourquoi la journée s'est arrêtée. Jamais « parce que »."""

    OBJECTIF_ATTEINT = "objectif journalier atteint"
    SESSIONS_EPUISEES = "toutes les sessions prévues ont été jouées"
    SESSIONS_PERDUES = "trop de sessions perdues d'affilée"
    PERTE_MAXIMALE = "perte journalière maximale atteinte"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PlanCapital:
    """La configuration du mode plan. Tout y est explicite.

    Aucune valeur par défaut sur `capital_initial`, `gain_par_session_pct` ni
    `payout_pct` : ils touchent à l'argent, donc ils sont fournis ou le plan
    n'existe pas (§5). Les gardes, elles, ont des défauts — parce qu'ils vont
    tous dans le sens sûr.
    """

    capital_initial: float
    #: Gain visé par session, en % du solde COURANT. La feuille dit 0,58 %.
    gain_par_session_pct: float
    payout_pct: int
    sessions_par_jour: int
    jours: int

    # --- les gardes ---------------------------------------------------------
    pas_max: int = PAS_MAX_PAR_DEFAUT
    #: Sessions perdues d'affilée avant d'arrêter la journée.
    #:
    #: À deux pas et 52 % de réussite, une session est perdue avec une
    #: probabilité de 23 % ; deux d'affilée arrivent donc 5,3 % du temps, soit
    #: environ une fois tous les dix-neuf tours. Mettre 2 ici arrête la journée
    #: plusieurs fois par jour — ce qui peut être voulu, mais doit être choisi
    #: en le sachant. Le défaut de 3 déclenche ~1,2 % du temps.
    sessions_perdues_max: int = 3
    #: Perte journalière maximale, en % du capital du début de journée.
    perte_journaliere_max_pct: float = 5.0
    #: Gain journalier au-delà duquel on s'arrête. `None` = pas d'arrêt au gain.
    objectif_journalier_pct: float | None = None
    #: Part maximale du solde qu'une session a le droit d'engager.
    #:
    #: **C'est le vrai stop loss, et il est vérifié à la CONSTRUCTION.** La
    #: feuille en affichait un — « Stop Loss 20 % » — que sa propre progression
    #: franchissait dès le 5ᵉ pas, puis anéantissait au 7ᵉ. Un stop qu'on
    #: dépasse n'est pas un stop, c'est une décoration.
    #:
    #: Vérifiable d'avance parce que la mise est proportionnelle au solde :
    #: l'exposition est donc une fraction CONSTANTE, fonction du seul triplet
    #: (gain %, payout, pas_max). Une échelle à 7 pas engage ~100 % du solde
    #: quel que soit ce solde — la refuser au moment de la configuration est
    #: la seule façon d'empêcher plutôt que de constater.
    exposition_max_pct: float = 10.0

    def __post_init__(self) -> None:
        if self.capital_initial <= 0:
            raise BotError(
                f"capital_initial doit être positif : {self.capital_initial}")
        if not (0 < self.gain_par_session_pct <= 100):
            raise BotError(
                f"gain_par_session_pct hors ]0,100] : "
                f"{self.gain_par_session_pct}")
        if not (1 <= self.payout_pct <= 100):
            raise BotError(f"payout_pct hors [1,100] : {self.payout_pct}")
        if self.sessions_par_jour < 1:
            raise BotError(
                f"sessions_par_jour doit valoir au moins 1 : "
                f"{self.sessions_par_jour}")
        if self.jours < 1:
            raise BotError(f"jours doit valoir au moins 1 : {self.jours}")
        if self.sessions_perdues_max < 1:
            raise BotError(
                f"sessions_perdues_max doit valoir au moins 1 : "
                f"{self.sessions_perdues_max}")
        if self.perte_journaliere_max_pct <= 0:
            raise BotError(
                f"perte_journaliere_max_pct doit être positive : "
                f"{self.perte_journaliere_max_pct}")
        if not (0 < self.exposition_max_pct <= 100):
            raise BotError(
                f"exposition_max_pct hors ]0,100] : {self.exposition_max_pct}")

        # Le stop loss, vérifié MAINTENANT et pas en cours de journée.
        part = self.echelle(self.capital_initial).part_du_capital(
            self.capital_initial)
        if part > self.exposition_max_pct:
            raise BotError(
                f"Une échelle à {self.pas_max} pas engage {part:.2f} % du "
                f"solde, au-delà du plafond de {self.exposition_max_pct:.2f} %. "
                f"C'est la configuration qui est refusée, pas une session : "
                f"l'exposition est une fraction constante du solde, donc ce "
                f"dépassement se produirait à CHAQUE session. Réduisez pas_max "
                f"— 2 engage 1,96 %, 7 en engage 100 — ou relevez "
                f"exposition_max_pct en sachant ce que cela autorise."
            )

    # --- ce qui se dérive, et ne se stocke donc pas -------------------------

    def gain_vise(self, solde: float) -> float:
        """Le gain d'une session, en monnaie, sur le solde COURANT.

        Sur le solde courant et non sur le capital initial : c'est ce que fait
        le bouton « COPY BALANCE » de la feuille, et c'est ce qui rend la
        progression réellement composée.
        """
        return solde * self.gain_par_session_pct / 100

    def echelle(self, solde: float) -> Echelle:
        return Echelle(
            payout_pct=self.payout_pct,
            gain_vise=self.gain_vise(solde),
            pas_max=self.pas_max,
        )

    def ratio_journalier_pct(self, compose: bool = False) -> float:
        """Le gain visé sur une journée.

        `compose=False` reproduit la feuille : 0,58 × 6 = 3,48 %. C'est une
        somme, pas une composition.

        `compose=True` donne ce qui se produit RÉELLEMENT, puisque chaque
        session mise sur le solde courant : (1,0058)^6 − 1 = 3,53 %. La feuille
        sous-estime donc de 0,05 point par jour — environ 1,5 % sur trente
        jours. L'écart est petit, mais il va dans le sens optimiste, et une
        projection qu'on compare à la réalité doit dire laquelle des deux elle
        calcule.
        """
        g = self.gain_par_session_pct / 100
        if compose:
            return 100 * ((1 + g) ** self.sessions_par_jour - 1)
        return self.gain_par_session_pct * self.sessions_par_jour

    def seuil_de_rentabilite_pct(self) -> float:
        return 100 / (1 + self.payout_pct / 100)


@dataclass
class Journee:
    """Une journée de trading, et les gardes qui peuvent l'arrêter.

    Mutable, contrairement au plan : c'est un état qui avance. La distinction
    est volontaire — on ne confond pas la règle et ce qu'elle a produit.
    """

    plan: PlanCapital
    solde: float
    sessions_jouees: int = 0
    sessions_perdues_daffilee: int = 0
    solde_ouverture: float = field(init=False)
    arret: Arret | None = None

    def __post_init__(self) -> None:
        self.solde_ouverture = self.solde

    # --- lecture ------------------------------------------------------------

    @property
    def resultat(self) -> float:
        return self.solde - self.solde_ouverture

    @property
    def resultat_pct(self) -> float:
        return 100 * self.resultat / self.solde_ouverture

    def peut_ouvrir_une_session(self) -> Arret | None:
        """`None` si l'on peut continuer, sinon la raison de s'arrêter.

        Interrogée AVANT d'engager quoi que ce soit. Une garde vérifiée après
        coup ne garde rien : elle constate.
        """
        if self.arret is not None:
            return self.arret
        if self.sessions_jouees >= self.plan.sessions_par_jour:
            return Arret.SESSIONS_EPUISEES
        if self.sessions_perdues_daffilee >= self.plan.sessions_perdues_max:
            return Arret.SESSIONS_PERDUES
        if -self.resultat_pct >= self.plan.perte_journaliere_max_pct - TOLERANCE:
            return Arret.PERTE_MAXIMALE
        objectif = self.plan.objectif_journalier_pct
        if objectif is not None and self.resultat_pct >= objectif - TOLERANCE:
            return Arret.OBJECTIF_ATTEINT
        return None

    # --- écriture -----------------------------------------------------------

    def enregistrer(self, gagnee: bool, montant: float) -> None:
        """Comptabilise une session terminée.

        `montant` est signé : le gain visé si elle est gagnée, l'exposition en
        négatif sinon. Il est passé plutôt que recalculé — le résultat réel
        peut différer du théorique (payout révisé, mise refusée), et recalculer
        écrirait dans le journal ce qu'on croit qu'il s'est passé.
        """
        self.solde += montant
        self.sessions_jouees += 1
        self.sessions_perdues_daffilee = (
            0 if gagnee else self.sessions_perdues_daffilee + 1)
        self.arret = self.peut_ouvrir_une_session()
