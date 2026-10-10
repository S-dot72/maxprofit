"""
La configuration choisie par l'utilisateur : plan ou trading seul, démo ou
réel, et — en plan — capital, risque N/M et sessions par jour.

Le moteur du plan ne change pas : le risque N/M est le `Risque` qu'il
utilisait déjà (1/7 jusqu'ici, en dur). L'utilisateur le choisit désormais
lui-même, voit aussitôt ce qu'il donne — gain par session, objectif du jour,
capital à atteindre au bout des 30 jours, perte d'une session ratée — et
reçoit un avertissement quand ce risque est élevé.

Ce module calcule et formate ; il ne parle ni à Telegram ni au broker.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass, field, replace

from maxprofit.core.errors import BotError
from maxprofit.plan import Echelle, PlanCapital, Risque
from maxprofit.plan.projection import solde_projete
from maxprofit.store import reglages

CLE = "configuration"
PAYOUT_PCT = 92
JOURS = 30

#: Perte d'une session ratée, en % du capital, au-delà de laquelle le risque
#: est signalé : modéré, puis ÉLEVÉ (avertissement et confirmation).
SEUIL_MODERE_PCT = 2.5
SEUIL_ELEVE_PCT = 5.0

#: Trading seul : la mise de chaque ordre, l'objectif et la perte maximale du
#: jour, en % du solde d'ouverture de la journée. Sans martingale : c'est le
#: réglage au meilleur rapport gain/creux de la simulation de 30 jours.
TRADING_MISE_PCT = 1.0
TRADING_OBJECTIF_JOUR_PCT = 2.0
TRADING_PERTE_JOUR_PCT = 3.0

#: Trading seul : jours couverts par le plafond de mise. Les mises suivent
#: le solde ; le plafond doit le laisser grandir sans refuser d'ordre.
TRADING_JOURS_PLAFOND = 90
#: Trading seul : nombre de « jours » du plan. Il n'y a pas de fin de plan,
#: seulement un compteur de journées.
TRADING_JOURS = 365

#: Mise la plus petite que le broker accepte.
MISE_MINIMALE = 1.0

MODES = ("plan", "trading")
COMPTES = ("demo", "reel")


@dataclass
class Configuration:
    mode: str = "plan"
    compte: str = "demo"
    capital: float = 250.0
    gagnants: int = 1
    trades: int = 7
    sessions_par_jour: int = 6
    pas_max: int = 2
    #: Posés au lancement (`/demarrer`).
    lancee: bool = False
    campagne: str = ""
    lancee_ts: int = 0
    #: Le capital du dernier lancement : s'il n'a pas été retouché, une
    #: relance en cours de plan repart du solde atteint, pas de ce chiffre.
    capital_lance: float = 0.0
    #: Les campagnes jouées avant celle-ci, relues pour les taux par paire.
    campagnes_precedentes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Configuration":
        if not d:
            return cls()
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    # --- validation -----------------------------------------------------------

    def verifier(self) -> None:
        """Lève `BotError` avec un message lisible si un réglage est faux."""
        if self.mode not in MODES:
            raise BotError(f"mode inconnu : {self.mode}")
        if self.compte not in COMPTES:
            raise BotError(f"compte inconnu : {self.compte}")
        if not 10 <= self.capital <= 1_000_000:
            raise BotError("Le capital doit être entre 10 $ et 1 000 000 $.")
        if self.mode == "plan":
            if not 1 <= self.gagnants <= 20:
                raise BotError("Le nombre de trades gagnants (N) doit être "
                               "entre 1 et 20.")
            if not self.gagnants <= self.trades <= 30:
                raise BotError("Le nombre de trades (M) doit être au moins N "
                               "et au plus 30.")
            if not 1 <= self.sessions_par_jour <= 30:
                raise BotError("Le nombre de sessions par jour doit être "
                               "entre 1 et 30.")
            self.plan()                 # lève si le risque est impossible
            premiere = self.mises()[0]
            if premiere < MISE_MINIMALE:
                minimum = self.capital * MISE_MINIMALE / premiere
                raise BotError(
                    f"Avec ce risque, la première mise serait de "
                    f"{premiere:.2f} $, sous le minimum de "
                    f"{MISE_MINIMALE:.0f} $ du broker. Il faut au moins "
                    f"{minimum:.0f} $ de capital pour le risque "
                    f"{self.gagnants}/{self.trades}, ou un risque plus élevé.")
        elif TRADING_MISE_PCT / 100 * self.capital < MISE_MINIMALE:
            raise BotError(
                f"En trading seul, la mise est de {TRADING_MISE_PCT:.0f} % du "
                f"capital : il faut au moins "
                f"{100 * MISE_MINIMALE / TRADING_MISE_PCT:.0f} $ pour atteindre "
                f"la mise minimale de {MISE_MINIMALE:.0f} $ du broker.")


    # --- calculs ----------------------------------------------------------------

    def plan_de_course(self) -> PlanCapital:
        """Le plan que la course jouera, dans l'un ou l'autre mode."""
        if self.mode == "plan":
            return self.plan()
        gain = TRADING_MISE_PCT * PAYOUT_PCT / 100
        return PlanCapital(
            capital_initial=self.capital, gain_par_session_pct=gain,
            payout_pct=PAYOUT_PCT,
            sessions_par_jour=math.ceil(TRADING_OBJECTIF_JOUR_PCT / gain),
            jours=TRADING_JOURS, pas_max=1, sessions_perdues_max=3,
            perte_journaliere_max_pct=TRADING_PERTE_JOUR_PCT,
            objectif_journalier_pct=TRADING_OBJECTIF_JOUR_PCT,
            exposition_max_pct=100.0)

    def pas_de_course(self) -> int:
        """Pas de martingale : celui du réglage en plan, un seul en trading."""
        return self.pas_max if self.mode == "plan" else 1

    def plafond_de_mise(self) -> tuple[float, float]:
        """(capital visé, plafond de mise) pour la garde du courtier.

        La garde refuse toute mise au-delà du plafond. Les mises suivent le
        solde : le plafond est le dernier pas AU CAPITAL VISÉ, plus 10 %,
        pour que la croissance prévue ne fasse jamais refuser un ordre.
        """
        if self.mode == "plan":
            vise = self.capital_a_30_jours()
            gain = vise * self.plan().gain_par_session_pct / 100
            pire = Echelle(payout_pct=PAYOUT_PCT, gain_vise=gain,
                           pas_max=self.pas_max).mises()[-1]
        else:
            vise = self.capital * (1 + TRADING_OBJECTIF_JOUR_PCT / 100) \
                ** TRADING_JOURS_PLAFOND
            pire = vise * TRADING_MISE_PCT / 100
        return vise, round(pire * 1.1, 2)

    def plan(self, capital: float | None = None) -> PlanCapital:
        """Le plan que le moteur jouera, tel qu'il le calculait déjà.

        Le plafond d'exposition est levé ici : un risque élevé n'est plus
        REFUSÉ, il est SIGNALÉ (voir `niveau_de_risque`) — c'est l'utilisateur
        qui décide.
        """
        plan = PlanCapital.depuis_risque(
            capital_initial=capital or self.capital,
            risque=Risque(self.gagnants, self.trades),
            payout_pct=PAYOUT_PCT, sessions_par_jour=self.sessions_par_jour,
            jours=JOURS, sessions_perdues_max=2, pas_max=self.pas_max,
            exposition_max_pct=100.0)
        return replace(plan, objectif_journalier_pct=(
            plan.sessions_par_jour * plan.gain_par_session_pct))

    def mises(self) -> list[float]:
        gain = self.plan().gain_par_session_pct / 100 * self.capital
        return Echelle(payout_pct=PAYOUT_PCT, gain_vise=gain,
                       pas_max=self.pas_max).mises()

    def perte_session_pct(self) -> float:
        """Ce que coûte une session ratée, en % du capital."""
        return 100 * sum(self.mises()) / self.capital

    def niveau_de_risque(self) -> str:
        perte = (TRADING_MISE_PCT if self.mode == "trading"
                 else self.perte_session_pct())
        if perte > SEUIL_ELEVE_PCT:
            return "élevé"
        if perte > SEUIL_MODERE_PCT:
            return "modéré"
        return "normal"

    def capital_a_30_jours(self) -> float:
        return solde_projete(self.plan(), JOURS)

    # --- modifier --------------------------------------------------------------

    def modifiee(self, reglage: str, argument: str) -> "Configuration":
        """Une copie avec `reglage` changé d'après ce que l'utilisateur a tapé.

        Lève `BotError` si la valeur est illisible. Une valeur lisible mais
        qui rend le plan impossible est ACCEPTÉE : le résumé dit pourquoi et
        `/demarrer` refuse. La refuser ici bloquerait des changements qui ne
        se font que dans un ordre — baisser le capital avant de monter le
        risque, par exemple.
        """
        brut = argument.strip().lower()
        if reglage == "mode":
            if brut in ("plan",):
                return replace(self, mode="plan")
            if brut in ("trading", "trading seul", "seul", "auto"):
                return replace(self, mode="trading")
            raise BotError("Choisissez /mode plan ou /mode trading.")
        if reglage == "compte":
            if brut in ("demo", "démo"):
                return replace(self, compte="demo")
            if brut in ("reel", "réel"):
                return replace(self, compte="reel")
            raise BotError("Choisissez /compte demo ou /compte reel.")
        if reglage == "capital":
            nombre = brut.replace("$", "").replace(" ", "").replace(",", ".")
            try:
                capital = float(nombre)
            except ValueError:
                raise BotError("Indiquez le capital en dollars, par exemple "
                               "/capital 250.") from None
            if not 10 <= capital <= 1_000_000:
                raise BotError("Le capital doit être entre 10 $ et "
                               "1 000 000 $.")
            return replace(self, capital=round(capital, 2))
        if reglage == "risque":
            trouve = re.fullmatch(r"(\d+)\s*(?:/|sur|\s)\s*(\d+)", brut)
            if not trouve:
                raise BotError("Indiquez N trades gagnants sur M trades, par "
                               "exemple /risque 1/7.")
            n, m = int(trouve.group(1)), int(trouve.group(2))
            if not 1 <= n <= 20 or not n <= m <= 30:
                raise BotError("N doit être entre 1 et 20, et M entre N et "
                               "30.")
            return replace(self, gagnants=n, trades=m)
        if reglage == "sessions":
            try:
                n = int(brut)
            except ValueError:
                raise BotError("Indiquez un nombre de sessions par jour, par "
                               "exemple /sessions 6.") from None
            if not 1 <= n <= 30:
                raise BotError("Entre 1 et 30 sessions par jour.")
            return replace(self, sessions_par_jour=n)
        if reglage == "pas":
            # Le nombre de pas de la martingale : combien de mises d'affilée
            # une session engage avant d'être déclarée perdue. 1 = pas de
            # martingale, une seule mise par session.
            #
            # Une valeur lisible mais trop haute pour le capital est ACCEPTÉE
            # ici, comme partout dans ce fichier : le plan refuse un nombre de
            # pas qui atteindrait le pas où l'on engage tout le capital, et le
            # résumé le dit. `/demarrer` refuse ensuite de lancer.
            try:
                n = int(brut.replace("pas", "").strip())
            except ValueError:
                raise BotError("Indiquez un nombre de pas de martingale, par "
                               "exemple /pas 3.") from None
            if not 1 <= n <= 10:
                raise BotError("Entre 1 et 10 pas. 1 = une seule mise par "
                               "session, sans martingale.")
            return replace(self, pas_max=n)
        raise BotError(f"réglage inconnu : {reglage}")

    def erreur(self) -> str | None:
        """Pourquoi ce réglage ne peut pas être lancé, ou `None`."""
        try:
            self.verifier()
        except BotError as e:
            return str(e)
        return None

    # --- ce que l'utilisateur lit ---------------------------------------------

    def _entete(self) -> str:
        compte = ("💰 compte RÉEL" if self.compte == "reel"
                  else "🧪 compte démo")
        etat = (f"▶️ lancé le "
                f"{time.strftime('%d/%m %H:%M', time.gmtime(self.lancee_ts))} "
                f"UTC" if self.lancee else "⏸ pas lancé")
        nom = "Plan" if self.mode == "plan" else "Trading seul"
        return f"📋 <b>{nom}</b> · {compte} · {etat}"

    def resume(self) -> str:
        erreur = self.erreur()
        if self.mode == "trading":
            corps = self._resume_trading() if erreur is None else [
                f"Capital : <b>{self.capital:.2f} $</b>"]
            aide = "Modifier : /capital 300 · /compte demo ou reel · /mode plan"
        else:
            corps = self._resume_plan() if erreur is None else [
                f"Capital : <b>{self.capital:.2f} $</b> · risque "
                f"<b>{self.gagnants}/{self.trades}</b> · "
                f"{self.sessions_par_jour} sessions par jour"]
            aide = ("Modifier : /capital 300 · /risque 1/7 · /sessions 6 · "
                    "/pas 3 · /compte demo ou reel · /mode trading")
        lignes = [self._entete(), *corps]
        if erreur is not None:
            lignes.append(f"⛔ <b>Ce réglage ne peut pas être lancé</b> : "
                          f"{erreur}")
        if self.compte == "reel":
            lignes.append("💰 <b>Compte réel</b> : les ordres engageront de "
                          "l'argent véritable.")
        lignes += ["", aide, "Lancer : /demarrer · Arrêter : /arreter"]
        return "\n".join(lignes)

    def _resume_trading(self) -> list[str]:
        mise = TRADING_MISE_PCT / 100 * self.capital
        return [
            f"Capital : <b>{self.capital:.2f} $</b>",
            f"Le bot choisit les paires et les entrées. Mise de "
            f"{mise:.2f} $ par ordre ({TRADING_MISE_PCT:.0f} % du solde), "
            f"sans martingale : jamais de mise augmentée après une perte.",
            f"Objectif du jour : <b>+{TRADING_OBJECTIF_JOUR_PCT:.0f} %</b> "
            f"({TRADING_OBJECTIF_JOUR_PCT / 100 * self.capital:.2f} $). "
            f"Perte maximale du jour : −{TRADING_PERTE_JOUR_PCT:.0f} % "
            f"({TRADING_PERTE_JOUR_PCT / 100 * self.capital:.2f} $). Dès que "
            f"l'un des deux est atteint, le bot s'arrête jusqu'au lendemain.",
            f"🎯 <b>Si chaque journée atteint son objectif, en {JOURS} "
            f"jours : {self.capital_trading_a_30_jours():.2f} $</b>. Les "
            f"journées arrêtées sur perte retardent d'autant.",
            "Risque : <b>normal</b>",
        ]

    def capital_trading_a_30_jours(self) -> float:
        return self.capital * (1 + TRADING_OBJECTIF_JOUR_PCT / 100) ** JOURS

    def _resume_plan(self) -> list[str]:
        plan = self.plan()
        mises = self.mises()
        gain = plan.gain_par_session_pct / 100 * self.capital
        perte = sum(mises)
        niveau = self.niveau_de_risque()
        lignes = [
            f"Capital : <b>{self.capital:.2f} $</b> · risque "
            f"<b>{self.gagnants}/{self.trades}</b> · "
            f"{self.sessions_par_jour} sessions par jour · martingale "
            f"{self.pas_max} pas",
            "",
            f"Par session : gain visé {gain:.2f} $ "
            f"({plan.gain_par_session_pct:.2f} %) ; mises "
            + " puis ".join(f"{m:.2f} $" for m in mises)
            + f" ; une session ratée coûte {perte:.2f} $ "
              f"({self.perte_session_pct():.1f} % du capital).",
            f"Par jour : objectif +{plan.objectif_journalier_pct:.2f} % "
            f"({plan.objectif_journalier_pct / 100 * self.capital:.2f} $ le "
            f"1er jour).",
            f"🎯 <b>À atteindre en {JOURS} jours : "
            f"{self.capital_a_30_jours():.2f} $</b> — si chaque session est "
            f"gagnée ; chaque session ratée retarde le plan.",
            f"Risque : <b>{niveau}</b>",
        ]
        if niveau == "élevé":
            lignes.append(
                f"⚠️ <b>Risque élevé</b> : une seule session ratée coûte "
                f"{self.perte_session_pct():.1f} % du capital, deux d'affilée "
                f"{2 * self.perte_session_pct():.1f} %. Lancer demandera une "
                f"confirmation.")
        return lignes


# --- persistance ---------------------------------------------------------------

def charger(conn) -> Configuration | None:
    """La configuration enregistrée, ou `None` si personne n'en a fait."""
    return Configuration.from_dict(reglages.lire(conn, CLE)) \
        if reglages.lire(conn, CLE) is not None else None


def sauver(conn, configuration: Configuration) -> None:
    reglages.ecrire(conn, CLE, configuration.to_dict())
