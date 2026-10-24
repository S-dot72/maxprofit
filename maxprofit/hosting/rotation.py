"""
La rotation des bases Neon, vue du service : la relève tenue à jour, la
bascule quand la base active atteint son quota.

Le choix des bases et la recopie sont dans `store/pool.py` ; ici, on les fait
vivre — un fil qui synchronise toutes les `SYNCHRO_SEC`, un rappel posé sur
`postgres.QUAND_QUOTA_EPUISE`, une alerte Telegram, une ligne dans /etat.

--- La bascule ----------------------------------------------------------------

Quand la base ACTIVE refuse pour quota, on ne bascule pas « à chaud » : la
collecte, la course, le bot et la sonde ont chacun leur connexion, ouvertes
dans des fils différents. Les rouvrir toutes au bon endroit, sans en oublier
une, est exactement le genre de chose qui se rate. On ARRÊTE le service ;
l'hébergeur le relance ; au démarrage, `pool.choisir_au_demarrage` écarte la
base bloquée et désigne la relève, la plus à jour des joignables. Un seul
chemin, celui qu'emprunte déjà chaque déploiement.

On ne s'arrête que si une relève répond : sans elle, le redémarrage
retomberait sur la base bloquée et bouclerait.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from maxprofit.store import pool, postgres

log = logging.getLogger(__name__)

#: Le code de sortie d'une bascule : l'hébergeur relance, le journal dit
#: pourquoi.
CODE_BASCULE = 3

#: Si l'arrêt propre n'aboutit pas — une écriture pendue sur la base morte —
#: le processus est quitté de force après ce délai.
ARRET_FORCE_SEC = 90

#: La première passe attend un peu : au démarrage, la collecte et la course
#: ouvrent leurs connexions, inutile d'ajouter la recopie par-dessus.
PREMIERE_PASSE_SEC = 120


class Rotation:
    def __init__(self, active: pool.EtatBase, releve: pool.EtatBase | None,
                 *, basculee_depuis: str = "", sonde=None, ouvrir=None,
                 synchroniser=None, horloge=time.time):
        self.active = active
        self.releve = releve
        #: L'hôte que `DATABASE_URL` désignait avant le choix, s'il a changé.
        self.basculee_depuis = basculee_depuis
        self.alerter = None
        self.arreter = None
        self._sonde = sonde or pool.sonder
        self._ouvrir = ouvrir
        self._synchroniser = synchroniser or pool.synchroniser
        self._horloge = horloge
        self._verrou = threading.Lock()
        self._bascule_demandee = False
        self._releve_signalee = False
        self._stop = threading.Event()
        self.derniere_synchro: float | None = None
        self.derniere_copie: int = 0
        self.derniere_erreur: str = ""
        #: Schéma de la relève vérifié (migrations, réparation) : une fois
        #: par processus suffit.
        self._releve_preparee = False

    # --- au démarrage -----------------------------------------------------

    @classmethod
    def au_demarrage(cls, env=None, **kw) -> "Rotation | None":
        """Choisit les bases et rend la rotation, ou `None` sans pool."""
        env = os.environ if env is None else env
        avant = env.get("DATABASE_URL", "")
        active, releve, _ = pool.choisir_au_demarrage(env)
        if active is None:
            return None
        depuis = ""
        if avant and avant != active.url:
            depuis = pool.EtatBase("", avant, False).hote
        return cls(active, releve, basculee_depuis=depuis, **kw)

    def installer(self) -> None:
        postgres.QUAND_QUOTA_EPUISE = self.quota_epuise

    def message_de_demarrage(self) -> str | None:
        if not self.basculee_depuis:
            return None
        return (f"🔁 <b>Base basculée</b> : {self.basculee_depuis} → "
                f"<b>{self.active.hote}</b> (la plus à jour des bases "
                f"joignables). Les ~{pool.SYNCHRO_SEC // 60} dernières minutes "
                f"avant la bascule peuvent manquer ; vérifiez /etat."
                + ("" if self.releve else
                   "\n⚠️ Aucune relève joignable : la prochaine base épuisée "
                   "ne pourra pas être remplacée. Ajoutez une base."))

    # --- la synchronisation -------------------------------------------------

    def demarrer(self) -> None:
        if self.releve is None:
            return
        threading.Thread(target=self._boucler, name="releve",
                         daemon=True).start()

    def arreter_synchro(self) -> None:
        self._stop.set()

    def _boucler(self) -> None:
        attente = PREMIERE_PASSE_SEC
        while not self._stop.wait(attente):
            self.synchroniser_une_fois()
            attente = pool.SYNCHRO_SEC

    def synchroniser_une_fois(self) -> bool:
        """Une passe active → relève. Ne lève jamais."""
        if self.releve is None or self._bascule_demandee:
            return False
        ouvrir = self._ouvrir or _ouvrir_postgres
        debut = self._horloge()
        source = cible = None
        try:
            source = ouvrir(self.active.url, ecriture=False)
            cible = ouvrir(self.releve.url,
                           ecriture=not self._releve_preparee)
            self._releve_preparee = True
            copiees = self._synchroniser(source, cible, journal=log.warning)
        except Exception as erreur:                  # noqa: BLE001
            self.derniere_erreur = str(erreur).splitlines()[0][:160]
            log.warning("Relève %s : synchronisation impossible (%s)",
                        self.releve.hote, self.derniere_erreur)
            return False
        finally:
            for conn in (source, cible):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:                # noqa: BLE001
                        pass
        self.derniere_synchro = self._horloge()
        self.derniere_copie = sum(copiees.values())
        self.derniere_erreur = ""
        log.info("Relève %s synchronisée en %.0f s : %d ligne(s) (%s)",
                 self.releve.hote, self.derniere_synchro - debut,
                 self.derniere_copie,
                 ", ".join(f"{t} {n}" for t, n in copiees.items() if n)
                 or "rien de neuf")
        return True

    # --- le quota -----------------------------------------------------------

    def quota_epuise(self, url: str, texte: str) -> None:
        """Le rappel de `postgres` : une base refuse pour quota."""
        if url != self.active.url:
            self._releve_epuisee(url)
            return
        with self._verrou:
            if self._bascule_demandee:
                return
            self._bascule_demandee = True
        releve = self.releve
        etat = self._sonde(releve.nom, releve.url) if releve else None
        if etat is None or not etat.joignable:
            # Pas de bascule possible : on le dit, une fois, et on continue.
            # La collecte réessaie d'elle-même ; un redémarrage retomberait
            # sur la même base bloquée.
            log.error("Base active %s : QUOTA ÉPUISÉ, et aucune relève ne "
                      "répond. Pas de bascule.", self.active.hote)
            self._alerter(
                f"🛑 <b>Quota Neon épuisé</b> sur {self.active.hote}, et "
                f"aucune base de relève ne répond : la collecte est à "
                f"l'arrêt. Ajoutez une base (DATABASE_URL_…) sur Render.")
            return
        log.error("Base active %s : QUOTA ÉPUISÉ. Bascule sur %s : arrêt du "
                  "service, l'hébergeur le relance.", self.active.hote,
                  releve.hote)
        age = self.age_synchro_min()
        self._alerter(
            f"🔁 <b>Quota Neon épuisé</b> sur {self.active.hote}. Bascule sur "
            f"<b>{releve.hote}</b> : le service redémarre.\n"
            f"Dernière synchronisation de la relève : "
            + (f"il y a {age} min." if age is not None else "aucune ⚠️."))
        if self.arreter is not None:
            self.arreter()

    def _releve_epuisee(self, url: str) -> None:
        if self.releve is None or url != self.releve.url:
            return
        with self._verrou:
            if self._releve_signalee:
                return
            self._releve_signalee = True
        log.error("Relève %s : quota épuisé, elle n'est plus synchronisée.",
                  self.releve.hote)
        self._alerter(
            f"⚠️ <b>La relève {self.releve.hote} a atteint son quota</b> : "
            f"la base active n'a plus de remplaçante. Ajoutez une base "
            f"(DATABASE_URL_…) sur Render.")

    def _alerter(self, message: str) -> None:
        if self.alerter is None:
            return
        try:
            self.alerter(message)
        except Exception:                            # noqa: BLE001
            log.exception("Alerte de rotation non envoyée")

    # --- /etat --------------------------------------------------------------

    def age_synchro_min(self) -> int | None:
        if self.derniere_synchro is None:
            return None
        return int((self._horloge() - self.derniere_synchro) // 60)

    def resume(self) -> str:
        ligne = f"Base : {self.active.hote}"
        if self.releve is None:
            return ligne + " · ⚠ aucune relève"
        ligne += f" · relève {self.releve.hote}"
        age = self.age_synchro_min()
        if self.derniere_erreur:
            ligne += f", ⚠ synchro en échec ({self.derniere_erreur[:60]})"
        elif age is None:
            ligne += ", première synchro à venir"
        else:
            ligne += f", synchro il y a {age} min"
        return ligne


def _ouvrir_postgres(url: str, *, ecriture: bool):
    from maxprofit.store.db import open_postgres
    return open_postgres(url, ecriture=ecriture)
