"""
Régler et lancer la course depuis Telegram.

    /configuration           le réglage enregistré et sa projection
    /mode plan|trading       plan (capital, risque N/M, sessions) ou trading seul
    /compte demo|reel        l'interrupteur démo / réel
    /capital 250             le capital de départ
    /risque 1/7              N trades gagnants sur M trades
    /sessions 6              sessions par jour (plan)
    /demarrer                lance — ou relance — la course sur ce réglage
    /arreter                 arrête la course ; la collecte continue

Chaque modification est enregistrée et la projection réaffichée ; rien ne
change dans la course avant `/demarrer`. Une relance attend que la course
soit au repos : jamais au milieu d'une martingale.

--- Qui gouverne la course ---------------------------------------------------

Tant que personne n'a lancé de configuration, la course suit les variables
d'environnement (`PLAN_DEMO`, `PLAN_CAMPAGNE`…), comme avant. Dès le premier
`/demarrer` — ou `/arreter` —, c'est la configuration enregistrée qui décide,
redémarrages compris.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace

from maxprofit.core.errors import BotError
from maxprofit.live import configuration as config_mod
from maxprofit.live.configuration import Configuration

log = logging.getLogger(__name__)

REGLAGES = ("mode", "compte", "capital", "risque", "sessions")
COMMANDES = ("configuration", "configurer", *REGLAGES, "demarrer", "arreter")
#: Commandes qui ne modifient rien : ouvertes à tout opérateur approuvé.
LECTURE = ("configuration", "configurer")

GUIDE = (
    "⚙️ <b>Configurer le bot</b>\n"
    "1. /mode plan ou /mode trading (trading seul : le bot fait tout)\n"
    "2. /compte demo ou /compte reel\n"
    "3. /capital 250\n"
    "4. En plan : /risque N/M (N trades gagnants sur M trades, 1/7 par "
    "défaut) puis /sessions 6 (sessions par jour)\n"
    "Chaque changement réaffiche la projection. Quand elle vous convient : "
    "/demarrer.\n\n")


def signature(cfg: Configuration) -> str:
    """Ce qui fait le réglage, sans ce que le lancement y ajoute."""
    return (f"{cfg.mode}|{cfg.compte}|{cfg.capital:.2f}|{cfg.gagnants}/"
            f"{cfg.trades}|{cfg.sessions_par_jour}|{cfg.pas_max}")


class Pilote:
    """Le métier des commandes de configuration, hors de Telegram.

    `ouvrir()` rend une connexion en écriture, fermée après chaque commande :
    les commandes arrivent sur des fils différents. `superviseur()` rend le
    `SuperviseurCourse` du service, ou `None`.
    """

    def __init__(self, ouvrir, superviseur, campagne_env: str):
        self._ouvrir = ouvrir
        self._superviseur = superviseur
        self.campagne_env = campagne_env

    # --- accès à la base ------------------------------------------------------

    def charger(self) -> Configuration | None:
        conn = self._ouvrir()
        try:
            return config_mod.charger(conn)
        finally:
            _fermer(conn)

    def _sauver(self, cfg: Configuration) -> None:
        conn = self._ouvrir()
        try:
            config_mod.sauver(conn, cfg)
        finally:
            _fermer(conn)

    # --- la commande ----------------------------------------------------------

    def __call__(self, commande: str, argument: str = "") -> str:
        try:
            if commande in ("configuration", "configurer"):
                cfg = self.charger() or Configuration()
                return ((GUIDE if commande == "configurer" else "")
                        + self._apercu(cfg))
            if commande in REGLAGES:
                return self._modifier(commande, argument)
            if commande == "demarrer":
                return self._demarrer(argument.strip().lower())
            if commande == "arreter":
                return self._arreter()
        except BotError as erreur:
            return f"⛔ {erreur}"
        return f"Commande inconnue : /{commande}"

    def _modifier(self, reglage: str, argument: str) -> str:
        cfg = self.charger() or Configuration()
        if not argument.strip():
            return (f"Indiquez la valeur : {_EXEMPLES[reglage]}\n\n"
                    + self._apercu(cfg))
        nouvelle = cfg.modifiee(reglage, argument)
        self._sauver(nouvelle)
        suite = ""
        if nouvelle.lancee and signature(nouvelle) != signature(cfg):
            suite = ("\n\nℹ️ La course en cours garde l'ancien réglage : "
                     "/demarrer pour appliquer celui-ci.")
        return "✅ Enregistré.\n\n" + self._apercu(nouvelle) + suite

    def _apercu(self, cfg: Configuration) -> str:
        """Le résumé, recalculé sur le solde atteint si une relance en
        partirait."""
        capital = self._capital_de_relance(cfg)
        if capital == cfg.capital:
            return cfg.resume()
        return (replace(cfg, capital=capital).resume()
                + f"\n\n(calculé sur le solde atteint, {capital:.2f} $ : "
                  f"c'est de là qu'une relance repartirait)")

    def _capital_de_relance(self, cfg: Configuration) -> float:
        """Le capital d'une relance : le solde atteint, sauf si l'utilisateur
        a retouché le capital depuis le lancement."""
        if not cfg.lancee or abs(cfg.capital - cfg.capital_lance) > 1e-9:
            return cfg.capital
        course = getattr(self._superviseur(), "_course", None)
        solde = getattr(getattr(course, "etat", None), "solde", None)
        if not solde or getattr(course.journal, "campagne", None) \
                != cfg.campagne:
            return cfg.capital
        return round(float(solde), 2)

    def _demarrer(self, argument: str) -> str:
        enregistree = self.charger() or Configuration()
        sup = self._superviseur()
        if sup is None:
            return "⛔ La course n'est pas disponible sur ce service."
        cfg = replace(enregistree,
                      capital=self._capital_de_relance(enregistree))
        erreur = cfg.erreur()
        if erreur is not None:
            return f"⛔ <b>Lancement refusé</b> : {erreur}"
        if cfg.compte == "reel":
            return ("⛔ <b>Compte réel pas encore branché.</b> Le réglage est "
                    "enregistré, mais la course ne trade pour l'instant "
                    "qu'en démo : /compte demo pour lancer en démo.")
        if enregistree.lancee and sup.tourne() and \
                signature(cfg) == signature(enregistree) and \
                cfg.capital == enregistree.capital:
            return "▶️ Déjà lancé avec ce réglage.\n\n" + self._apercu(cfg)
        if cfg.mode == "plan" and cfg.niveau_de_risque() == "élevé" \
                and argument != "confirmer":
            return (f"⚠️ <b>Risque élevé</b> — une session ratée coûte "
                    f"{cfg.perte_session_pct():.1f} % du capital, deux "
                    f"d'affilée {2 * cfg.perte_session_pct():.1f} %.\n"
                    f"Pour lancer quand même : /demarrer confirmer\n\n"
                    + cfg.resume())
        precedente = enregistree.campagne or self.campagne_env
        precedentes = list(dict.fromkeys(
            [*enregistree.campagnes_precedentes, precedente]))
        maintenant = int(time.time())
        cfg = replace(
            cfg, lancee=True, lancee_ts=maintenant, capital_lance=cfg.capital,
            campagne=(f"{cfg.mode}-"
                      f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime(maintenant))}"),
            campagnes_precedentes=precedentes)
        self._sauver(cfg)
        log.info("Configuration lancée : %s (campagne %s)", signature(cfg),
                 cfg.campagne)
        return f"✅ <b>Lancé.</b> {sup.relancer()}\n\n" + cfg.resume()

    def _arreter(self) -> str:
        cfg = self.charger()
        if cfg is None:
            # La course suivait l'environnement : on enregistre qu'elle est
            # arrêtée, sinon le prochain redémarrage la relancerait.
            cfg = Configuration(campagne=self.campagne_env)
        cfg = replace(cfg, lancee=False,
                      campagne=cfg.campagne or self.campagne_env)
        self._sauver(cfg)
        sup = self._superviseur()
        message = sup.suspendre() if sup is not None else ""
        return f"⏹ <b>Arrêt demandé.</b> {message}"


_EXEMPLES = {
    "mode": "/mode plan ou /mode trading",
    "compte": "/compte demo ou /compte reel",
    "capital": "/capital 250",
    "risque": "/risque 1/7 (N trades gagnants sur M trades)",
    "sessions": "/sessions 6",
}


def gouverne(cfg: Configuration | None) -> bool:
    """La configuration décide-t-elle de la course ? Oui dès son premier
    lancement ou arrêt : avant, la course suit l'environnement."""
    return cfg is not None and bool(cfg.campagne)


def _fermer(conn) -> None:
    try:
        conn.close()
    except Exception:                            # noqa: BLE001
        pass
