"""
Le fil qui réapprend : une fois par jour, la stratégie rejouée sur les
derniers jours collectés, et les leçons recalculées.

Une fois par JOUR, et pas après chaque perte (SPEC §3.4) : un bot qui
réapprend sur ses résultats de l'heure et agit aussitôt poursuit du bruit.

Il tourne à côté de la collecte et de la course, dans le même processus :
- sa propre connexion à la base (une connexion ne se partage pas entre fils) ;
- une paire à la fois, pour ne pas charger dix jours de douze paires en
  mémoire d'un coup ;
- une pause régulière, pour laisser la main aux autres fils.
Le résultat est posé sur l'état de la course d'une seule affectation : la
course lit l'ancien ou le nouveau, jamais un mélange.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from maxprofit.apprentissage.historique import exemples_historiques
from maxprofit.apprentissage.lecons import apprendre
from maxprofit.store.market import MarketReader

log = logging.getLogger(__name__)

PERIODE_SEC = 24 * 3600
#: Pause du rejeu toutes les 200 bougies. Elle valait 2 ms : le rejeu, qui
#: rejoue désormais plusieurs stratégies, occupait le processeur et la course
#: évaluait ses bougies jusqu'à une minute en retard. 100 ms laissent la main
#: à la course ; le rejeu, quotidien, prend simplement plus longtemps.
RESPIRATION_SEC = 0.1
#: Attente avant le premier rejeu d'une course sans apprentissage : laisser
#: la collecte et la course se connecter d'abord.
DELAI_INITIAL_SEC = 120


def jours_d_apprentissage() -> int:
    """Trente jours par défaut, soit tout l'historique collecté.

    Dix jours n'ont donné que 276 signaux : trop peu pour établir autre
    chose qu'un contexte perdant deux fois sur trois. Le coût est un rejeu
    trois fois plus long, une fois par jour.
    """
    try:
        return max(2, int(os.environ.get("JOURS_APPRENTISSAGE", "30")))
    except ValueError:
        return 30


class Apprenti:
    """Le fil de réapprentissage. Un seul par processus : voir `demarrer`."""

    _actif: "Apprenti | None" = None
    _verrou = threading.Lock()

    def __init__(self, course, ouvrir_la_base, paires):
        self.course = course
        self._ouvrir = ouvrir_la_base
        self.paires = tuple(paires)
        self._arret = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def demarrer(cls, course, ouvrir_la_base, paires) -> "Apprenti":
        """Démarre le fil, ou le rattache à la nouvelle course.

        Une course refabriquée après un échec en démarrerait sinon un de
        plus à chaque fois — autant de rejeux concurrents.
        """
        with cls._verrou:
            if cls._actif is not None and cls._actif._thread is not None \
                    and cls._actif._thread.is_alive():
                # Une course relancée sur un nouveau réglage repart d'une
                # campagne neuve, donc sans apprentissage : elle hérite de
                # celui de la précédente plutôt que d'attendre un rejeu.
                if course.etat.apprentissage is None:
                    course.etat.apprentissage = \
                        cls._actif.course.etat.apprentissage
                cls._actif.course = course
                cls._actif.paires = tuple(paires)
                return cls._actif
            apprenti = cls(course, ouvrir_la_base, paires)
            apprenti._thread = threading.Thread(
                target=apprenti._boucle, name="apprentissage", daemon=True)
            apprenti._thread.start()
            cls._actif = apprenti
            return apprenti

    def arreter(self) -> None:
        self._arret.set()

    def _prochaine_attente(self) -> float:
        actuel = self.course.etat.apprentissage
        # Un apprentissage d'avant la comparaison des échéances est refait
        # tout de suite : /echeances resterait sinon muet jusqu'à un jour.
        # Et d'avant le taux par paire : /lecons le montrerait vide.
        if actuel is None or not actuel.n or not actuel.echeances \
                or not actuel.par_paire or not actuel.simulations \
                or not actuel.laboratoire \
                or not (_avec_zone_confirmee(actuel.laboratoire)
                        or "note" in actuel.laboratoire) \
                or not _mesure_tout(actuel):
            return DELAI_INITIAL_SEC
        return max(DELAI_INITIAL_SEC,
                   actuel.cree_ts + PERIODE_SEC - time.time())

    def _boucle(self) -> None:
        while not self._arret.wait(self._prochaine_attente()):
            try:
                self.apprendre_une_fois()
            except Exception:                    # noqa: BLE001
                # Un rejeu raté n'arrête ni la course ni la collecte : les
                # leçons d'hier restent en place, on réessaiera demain.
                log.exception("Apprentissage en échec")
                self._arret.wait(3600)

    def apprendre_une_fois(self) -> None:
        course = self.course
        strategie = type(course.strategie)(course.strategie.p)
        tolerance = course.strategie.p.tolerance_pct
        from maxprofit.live.plan_demo import dans_la_plage_de_calibration

        jours = jours_d_apprentissage()
        fin = int(time.time())
        debut = fin - jours * 86400
        debut_calcul = time.monotonic()
        # Les stratégies candidates (zones H1, ZigZag, prise de liquidité,
        # supports inversés) ont toutes été jugées au laboratoire le
        # 2026-10-03 sans faire mieux que ZoneH1 : leurs rejeux, qui
        # occupaient le processeur de la course, sont retirés. Leur code
        # reste, et STRATEGIE peut encore les mettre en course.
        etendus = None
        conn = self._ouvrir()
        try:
            lecteur = MarketReader(conn)
            exemples = []
            # La persistance des bougies se mesure sur les mêmes bougies,
            # pendant qu'elles sont en mémoire : aucune lecture de plus. De
            # même la qualité des données (EXP-001) et les indicateurs sur
            # toutes les bougies (EXP-002) du protocole OTC.
            from maxprofit.apprentissage.indicateurs import Indicateurs
            from maxprofit.apprentissage.persistance import Persistance
            from maxprofit.apprentissage.qualite import Qualite
            from maxprofit.apprentissage.simples import StrategiesSimples
            from maxprofit.live.preinscriptions import ouverture_s
            from maxprofit.live.protocole import date_de_gel
            from maxprofit.strategies.serie_rouge import SerieRouge
            # P4, pré-inscrite : la stratégie elle-même, rejouée sur les
            # mêmes bougies que ZoneH1 pour passer le même test aveugle.
            serie_rouge = SerieRouge()
            serie: list = []
            coupure_temps = debut + int(0.7 * (fin - debut))
            gel = date_de_gel()
            persistance = Persistance(coupure_temps)
            qualite = Qualite(debut, fin, coupure_temps)
            indicateurs = Indicateurs(debut, coupure_temps, gel)
            # EXP-030 (/simples) : momentum de 7 bougies, croisement de
            # moyennes, retour vers la médiane de Bollinger.
            strategies_simples = StrategiesSimples(debut, coupure_temps,
                                                   ouverture_s())
            exclusions: dict[str, int] = {}
            respirer = lambda: time.sleep(RESPIRATION_SEC)  # noqa: E731
            # L'étape 0 des ticks (/ticks), en lecture seule, sur les
            # derniers jours : elle réutilise les bougies et les signaux
            # déjà en mémoire. Une panne de l'audit n'arrête pas le rejeu.
            audit_ticks, erreur_ticks = self._audit_des_ticks(conn, fin)
            for paire in self.paires:
                bougies = lecteur.candles(paire, 60, debut, fin)
                persistance.ajouter(bougies, respirer=respirer)
                qualite.ajouter(bougies, respirer=respirer)
                indicateurs.ajouter(bougies, respirer=respirer)
                strategies_simples.ajouter(bougies, respirer=respirer)
                de_la_paire = exemples_historiques(
                    {paire: bougies}, strategie,
                    echeance_sec=strategie.p.expiry_sec,
                    garde=lambda p, f: dans_la_plage_de_calibration(
                        tolerance, f),
                    respirer=respirer, exclusions=exclusions)
                exemples += de_la_paire
                if audit_ticks is not None:
                    try:
                        audit_ticks.ajouter_paire(
                            paire, bougies, lecteur.tick_paths(
                                paire, audit_ticks.debut, audit_ticks.fin),
                            de_la_paire, respirer=respirer,
                            relire=lecteur.ticks)
                    except Exception as erreur:      # noqa: BLE001
                        log.exception("Audit des ticks en échec sur %s",
                                      paire)
                        audit_ticks, erreur_ticks = None, repr(erreur)
                serie += exemples_historiques(
                    {paire: bougies}, serie_rouge,
                    echeance_sec=serie_rouge.p.expiry_sec,
                    echeances=(serie_rouge.p.expiry_sec,),
                    respirer=respirer, avec_contexte=False)
            try:
                couverture = lecteur.couverture(fenetre_sec=fin - debut,
                                                maintenant=fin)
            except Exception:                    # noqa: BLE001
                couverture = None             # une mesure, pas une panne
            resultat_ticks = self._fin_de_l_audit(audit_ticks, erreur_ticks,
                                                  conn, lecteur)
        finally:
            try:
                conn.close()
            except Exception:                    # noqa: BLE001
                pass
        ancien = course.etat.apprentissage
        # Le drapeau de qualité suit chaque signal jusqu'à son entrée
        # confirmée : à poser AVANT tout calcul sur les confirmés.
        from maxprofit.live.protocole import marquer_qualite
        marquer_qualite(exemples, qualite, strategie.p.expiry_sec)
        # ⚠ L'APPRENTISSAGE PORTE SUR CE QUE LA COURSE JOUE.
        #
        # Le taux par paire décide des paires écartées « au taux perdant »,
        # les leçons des contextes écartés, l'autopsie de ce qui est
        # « défavorable ». Tout cela était mesuré sur ZoneH1 SANS
        # confirmation, et sur la bougie du SIGNAL, alors que la course joue
        # ZoneH1 PLUS la confirmation M1 et décide sur la bougie d'ENTRÉE :
        # - le 03/10, 41 % des bougies tombaient sur des paires écartées
        #   d'après des signaux que la course ne prend plus ;
        # - le 08/10, un CALL GBPUSD pris 5 amplitudes au-dessus de son
        #   support recevait « aucun contexte connu pour perdre » : au
        #   rejeu, la distance était mesurée au contact de la zone.
        # Les entrées confirmées, décrites à l'entrée, sont la population
        # jouée : c'est sur elle qu'on apprend.
        mode = getattr(course, "confirmation_m1", "0")
        fenetre = {"suivante": 1, "2": 2, "3": 3}.get(mode)
        if fenetre is not None:
            from maxprofit.live.laboratoire import confirmes
            nouveau = apprendre(confirmes(exemples, fenetre))
        else:
            nouveau = apprendre(exemples)
        nouveau.jours = jours
        nouveau.simulations = _simulations(
            exemples, strategie.p.expiry_sec, course.seuil_contre_heure,
            _echeances_courtes(exemples, course.seuil_contre_heure))
        from maxprofit.live.laboratoire import laboratoire
        seuil = course.seuil_contre_heure
        nouveau.laboratoire = laboratoire(
            exemples, etendus,
            (lambda e: True) if seuil is None else
            (lambda e: e.contexte.get("mouvement_heure", 0.0) >= -seuil),
            strategie.p.expiry_sec, jours_collectes=qualite.jours_collectes)
        nouveau.laboratoire["paires_jugees_sur"] = mode
        nouveau.laboratoire["version_experiences"] = VERSION_EXPERIENCES
        from maxprofit.live.experiences import experiences
        nouveau.laboratoire["experiences"] = experiences(
            exemples,
            (lambda e: True) if seuil is None else
            (lambda e: e.contexte.get("mouvement_heure", 0.0) >= -seuil),
            fenetre or 3, strategie.p.expiry_sec,
            jours_collectes=qualite.jours_collectes)
        nouveau.laboratoire["experiences"]["persistance"] = {
            **persistance.resultat(), "fin": fin}
        # L'audit : son résumé est gardé avec le laboratoire ; les signaux
        # eux-mêmes restent en MÉMOIRE seulement (la fiche CSV de /audit) —
        # les sauver avec l'état toutes les dix minutes pèserait des
        # centaines de kilo-octets pour rien.
        from maxprofit.live.audit import audit, version_des_regles
        en_direct = ((lambda e: True) if seuil is None else
                     (lambda e: e.contexte.get("mouvement_heure", 0.0)
                      >= -seuil))
        nouveau.laboratoire["audit"] = audit(exemples, en_direct,
                                             fenetre or 1)
        # Le potentiel de zone et l'expiration dynamique (/potentiel).
        from maxprofit.live.potentiel import potentiel
        nouveau.laboratoire["potentiel"] = potentiel(
            exemples, en_direct, fenetre or 1,
            jours_collectes=qualite.jours_collectes)
        # La mission Reference 8 (/reference8) : entrée, modèles gelés,
        # échéances, couverture ; le registre de sa fenêtre vient du rejeu
        # précédent.
        from maxprofit.live.reference8 import reference8
        nouveau.laboratoire["reference8"] = reference8(
            exemples, en_direct, fenetre or 1, qualite=qualite,
            exclusions=exclusions,
            ancien=((getattr(ancien, "laboratoire", None) or {})
                    .get("reference8")),
            debut=debut, fin=fin, jours_collectes=qualite.jours_collectes)
        # T0 à T3 sur les mêmes signaux (/entrees) : le retard de la
        # confirmation ; le registre de sa fenêtre vient du rejeu précédent.
        from maxprofit.live.entrees import entrees
        nouveau.laboratoire["entrees"] = entrees(
            exemples, en_direct, fenetre or 1,
            ancien=((getattr(ancien, "laboratoire", None) or {})
                    .get("entrees")),
            fin=fin, jours_collectes=qualite.jours_collectes)
        # L'étape 0 des ticks : le résumé est gardé avec le laboratoire, les
        # fiches CSV (anomalies, signaux, ordres) en mémoire seulement.
        if resultat_ticks is not None:
            nouveau.laboratoire["ticks"] = resultat_ticks[0]
            course.fichiers_ticks = resultat_ticks[1]
        # Les trois stratégies simples (/simples) : le choix figé de leur
        # fenêtre vient du rejeu précédent.
        from maxprofit.live.simples import simples
        nouveau.laboratoire["simples"] = simples(
            strategies_simples.resultat(),
            ancien=((getattr(ancien, "laboratoire", None) or {})
                    .get("simples")),
            fin=fin, jours_collectes=qualite.jours_collectes)
        course.rejeu_en_memoire = (exemples, en_direct, fenetre or 1,
                                   version_des_regles(strategie))
        # EXP-021 / EXP-022 : la stabilité dans le temps et le premier pas.
        # Une alerte de dégradation PRÉVIENT ; elle ne change aucune règle.
        from maxprofit.live.stabilite import stabilite, texte_alerte
        nouveau.laboratoire["stabilite"] = stabilite(
            exemples, en_direct, fenetre or 1,
            int(getattr(course, "pas_max", 2) or 2), strategie.p.expiry_sec,
            jours_collectes=qualite.jours_collectes)
        alerte = texte_alerte(nouveau.laboratoire["stabilite"])
        # Le protocole de simulation OTC (EXP-001 à EXP-004) : le gel et le
        # journal des hypothèses viennent du rejeu précédent.
        from maxprofit.live.protocole import protocole
        nouveau.laboratoire["protocole"] = protocole(
            exemples, en_direct, fenetre or 1, strategie.p.expiry_sec,
            qualite=qualite, indicateurs=indicateurs, exclusions=exclusions,
            couverture=couverture,
            experiences=nouveau.laboratoire["experiences"],
            ancien=((getattr(ancien, "laboratoire", None) or {})
                    .get("protocole")),
            version_regles=version_des_regles(strategie),
            version_experiences=VERSION_EXPERIENCES,
            debut=debut, fin=fin, gel=gel, jours=jours,
            serie=sorted(serie, key=lambda e: e.ts_sec),
            paires=self.paires, regle_heure=seuil,
            potentiel=nouveau.laboratoire["potentiel"],
            reference8=nouveau.laboratoire["reference8"],
            entrees=nouveau.laboratoire["entrees"],
            simples=nouveau.laboratoire["simples"])
        course.etat.apprentissage = nouveau
        log.info("Apprentissage : %d signaux rejoués sur %d jours en %.0f s, "
                 "%d leçon(s) active(s).", nouveau.n, jours,
                 time.monotonic() - debut_calcul, len(nouveau.regles))
        course._prevenir(_annonce(ancien, nouveau))
        if alerte:
            course._prevenir(alerte)

    def _audit_des_ticks(self, conn, fin: int):
        """(audit prêt à recevoir les paires, erreur) ; (None, None) si
        `AUDIT_TICKS_JOURS=0`."""
        from maxprofit.live.ticks import (
            jours_audites, lire_battements, nouvel_audit)
        jours = jours_audites()
        if not jours:
            return None, None
        seuil = getattr(self.course, "seuil_contre_heure", None)
        try:
            audit = nouvel_audit(
                fin - jours * 86400, fin,
                joue=None if seuil is None else (
                    lambda e: e.contexte.get("mouvement_heure", 0.0)
                    >= -seuil))
            audit.noter_battements(lire_battements(conn, audit.debut,
                                                   audit.fin))
            return audit, None
        except Exception as erreur:              # noqa: BLE001
            log.exception("Audit des ticks impossible")
            return None, repr(erreur)

    def _fin_de_l_audit(self, audit, erreur, conn, lecteur):
        """Les ordres réels comparés au flux, puis (résumé, fiches CSV)."""
        from maxprofit.live.protocole import commit
        from maxprofit.live.ticks import fichiers, jours_audites, lire_ordres
        if audit is None:
            return None if erreur is None else (
                {"erreur": erreur}, [])
        try:
            audit.comparer_ordres(lire_ordres(conn), lecteur.ticks)
            return (audit.resultat(commit=commit() or "",
                                   parametres_extra={"jours":
                                                     jours_audites()}),
                    fichiers(audit))
        except Exception as erreur:              # noqa: BLE001
            log.exception("Audit des ticks en échec")
            return {"erreur": repr(erreur)}, []


#: Version du contenu des expériences. La changer force un rejeu au
#: démarrage suivant, au lieu d'attendre le rejeu quotidien.
VERSION_EXPERIENCES = 16


def _avec_zone_confirmee(laboratoire) -> bool:
    """Faux pour un laboratoire calculé avant que les paires soient jugées
    sur ce que la course joue (confirmation M1 comprise) : il est alors
    refait au démarrage, pas dans vingt-quatre heures."""
    return laboratoire.get("version_experiences") == VERSION_EXPERIENCES


def _echeances_courtes(exemples, seuil_contre_heure) -> dict:
    """Le plan à 3 et 5 min, demandé le 2026-10-03 : ZoneH1 et la
    confirmation M1, comme en direct (règle de l'heure en cours comprise),
    mêmes entrées, seule la sortie change. La confirmation à 15 min sert de
    repère."""
    from maxprofit.live.laboratoire import a_l_echeance, confirmes
    joues = [e for e in exemples if seuil_contre_heure is None
             or e.contexte.get("mouvement_heure", 0.0) >= -seuil_contre_heure]
    sortie = {"Confirmation M1 (15 min), comme en direct":
              (confirmes(joues), 900)}
    for sec in (180, 300):
        m = sec // 60
        sortie[f"ZoneH1 ({m} min), comme en direct"] = (
            a_l_echeance(joues, sec), sec)
        sortie[f"Confirmation M1 ({m} min), comme en direct"] = (
            confirmes(joues, echeance_sec=sec), sec)
    return sortie


def _simulations(exemples, echeance_sec, seuil_contre_heure, autres) -> dict:
    """Le plan simulé : la stratégie jouée (telle qu'en direct, puis sans la
    règle de l'heure en cours), et chacune des autres."""
    from maxprofit.live.simulation import comparer

    def bloc(liste, echeance, garder=lambda e: True):
        return {"signaux": sum(1 for e in liste if garder(e)),
                "resultats": [r.to_dict()
                              for r in comparer(liste, echeance, garder)]}

    sortie = {}
    if seuil_contre_heure is not None:
        sortie["ZoneH1 (15 min), comme en direct"] = bloc(
            exemples, echeance_sec,
            lambda e: e.contexte.get("mouvement_heure", 0.0)
            >= -seuil_contre_heure)
    sortie["ZoneH1 (15 min), sans la règle de l'heure"] = bloc(
        exemples, echeance_sec)
    for nom, (liste, echeance) in autres.items():
        sortie[nom] = bloc(liste, echeance)
    return sortie


def _mesure_tout(apprentissage) -> bool:
    """Faux si l'apprentissage date d'avant une caractéristique ajoutée
    depuis : il est alors refait au démarrage, pas dans vingt-quatre heures."""
    vues = {s.tranche.caracteristique for s in apprentissage.statistiques}
    return {"elan_30m", "mouvement_heure"} <= vues


def _annonce(ancien, nouveau) -> str:
    """Ce qui a changé depuis le dernier apprentissage, puis le résumé."""
    avant = {r.tranche.libelle() for r in (ancien.regles if ancien else ())}
    apres = {r.tranche.libelle() for r in nouveau.regles}
    changements = []
    if apres - avant:
        changements.append("🆕 " + " · ".join(sorted(apres - avant)))
    if avant - apres:
        changements.append("🗑 retirée(s), plus confirmée(s) : "
                           + " · ".join(sorted(avant - apres)))
    tete = "\n".join(changements)
    return (tete + "\n\n" if tete else "") + nouveau.resume()
