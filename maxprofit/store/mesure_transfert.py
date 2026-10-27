"""
La mesure PASSIVE du transfert sortant de PostgreSQL (Neon), par
consommateur — demandée le 27/10 avant toute modification.

--- Ce que c'est, et ce que ce n'est pas ------------------------------------

Un compteur en mémoire, branché sur le curseur de `store.postgres` :
- il ne change aucune requête, aucun résultat, aucun ordre d'exécution ;
- il n'écrit rien, ni en base ni sur disque ; il repart de zéro à chaque
  démarrage ;
- une erreur de comptage le désactive au lieu de remonter.

**Ce qu'il mesure.** Pour chaque lecture (SELECT, WITH), quand l'appelant
récupère les lignes :
- le consommateur : la première fonction du projet hors de la couche de
  stockage ;
- l'opération : la méthode de `MarketReader`, sinon le premier mot et la
  table ;
- la base : active, relève ou autre ;
- les requêtes, les lignes, et les octets **estimés sur le fil** (voir
  ci-dessous) ;
- la portée dans le temps des lectures bornées, et les (paire, jour) lus,
  pour compter les journées relues plusieurs fois.

**L'estimation des octets.** psycopg reçoit les résultats en format TEXTE :
- un nombre arrive comme son écriture décimale ;
- un BYTEA comme son écriture hexadécimale, soit 2 caractères par octet plus
  « \\x » : un chemin de ticks de 400 octets en coûte environ 800 ;
- chaque ligne porte un en-tête (7 octets) et une longueur par champ
  (4 octets).

Ni le chiffrement TLS, ni les messages de protocole hors lignes ne sont
comptés : c'est une **borne basse** du transfert facturé, à rapprocher du
compteur de la console de Neon pour connaître le facteur.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Iterable

log = logging.getLogger(__name__)

OCTETS_PAR_LIGNE = 7
OCTETS_PAR_CHAMP = 4
#: Les fichiers de la couche de stockage, traversés pour trouver l'appelant.
COUCHE = ("store/postgres.py", "store/mesure_transfert.py", "store/db.py",
          "store/market.py", "store/dialecte.py")
#: Le dossier du paquet `maxprofit`, barre finale comprise : seules ses
#: fonctions sont des consommateurs.
_RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).replace(
    os.sep, "/") + "/"
_TABLE = re.compile(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
#: Instants plausibles (s ou ms), pour reconnaître les bornes d'une lecture.
_SEC = (1_000_000_000, 3_000_000_000)
_MS = (1_000_000_000_000, 3_000_000_000_000)
PORTEES = ((3600, "fenêtre < 1 h"), (86400, "< 1 jour"),
           (7 * 86400, "1 à 7 jours"), (None, "≥ 7 jours"))
SANS_BORNES = "sans bornes de temps"


class Mesure:
    """Les compteurs d'un processus. Une seule instance (`MESURE`)."""

    def __init__(self) -> None:
        self.debut = time.time()
        self.actif = True
        self.erreur: str | None = None
        self._verrou = threading.Lock()
        #: (base, consommateur, opération, table) → [requêtes, lignes, octets]
        self.par_cle: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0])
        #: (base, consommateur, table) → {(paire, jour): [lectures, octets]}
        self.jours: dict[tuple, dict] = defaultdict(
            lambda: defaultdict(lambda: [0, 0.0]))
        #: (base, consommateur, table) → {portée: octets}
        self.portees: dict[tuple, Counter] = defaultdict(Counter)

    # --- enregistrement ----------------------------------------------------

    def lecture(self, sql: str, params, base: str) -> "Lecture | None":
        """Une lecture qui commence : rend l'objet qui comptera ses lignes,
        ou None (écriture, ou mesure désactivée)."""
        if not self.actif:
            return None
        try:
            mot = (sql.lstrip().split(None, 1) or [""])[0].upper()
            if mot not in ("SELECT", "WITH"):
                return None
            consommateur, operation = appelant()
            table = (_TABLE.search(sql) or [None, "?"])[1].lower()
            if operation is None:
                operation = f"{mot} {table}"
            lecture = Lecture(self, (base, consommateur, operation, table),
                              bornes(params))
            with self._verrou:
                self.par_cle[lecture.cle][0] += 1
                for cle_jour in lecture.jours:
                    self.jours[lecture.cle_jours][cle_jour][0] += 1
            return lecture
        except Exception as erreur:                      # noqa: BLE001
            self._desactiver(erreur)
            return None

    def ajouter(self, lecture: "Lecture", lignes: int, octets: int) -> None:
        """Des lignes récupérées par l'appelant : comptées tout de suite,
        sans attendre la fin du curseur (un `fetchone` n'a pas de fin)."""
        if not lignes:
            return
        try:
            with self._verrou:
                c = self.par_cle[lecture.cle]
                c[1] += lignes
                c[2] += octets
                self.portees[lecture.cle_jours][lecture.portee] += octets
                if lecture.jours:
                    part = octets / len(lecture.jours)
                    for cle_jour in lecture.jours:
                        self.jours[lecture.cle_jours][cle_jour][1] += part
        except Exception as erreur:                      # noqa: BLE001
            self._desactiver(erreur)

    def _desactiver(self, erreur: Exception) -> None:
        self.actif = False
        self.erreur = repr(erreur)[:300]
        log.warning("Mesure du transfert désactivée : %s", self.erreur)

    # --- lecture des compteurs ---------------------------------------------

    def instantane(self) -> dict[str, Any]:
        """Une copie cohérente des compteurs, pour le rapport."""
        with self._verrou:
            par_cle = {k: list(v) for k, v in self.par_cle.items()}
            relues = {}
            for cle, jours in self.jours.items():
                lectures = sum(v[0] for v in jours.values())
                en_trop = sum(v[0] - 1 for v in jours.values() if v[0] > 1)
                octets_en_trop = sum(v[1] * (v[0] - 1) / v[0]
                                     for v in jours.values() if v[0] > 1)
                relues[cle] = {"paires_jours": len(jours),
                               "lectures": lectures,
                               "relectures": en_trop,
                               "octets_relus": octets_en_trop}
            portees = {k: dict(v) for k, v in self.portees.items()}
        return {"debut": self.debut, "maintenant": time.time(),
                "actif": self.actif, "erreur": self.erreur,
                "par_cle": par_cle, "relues": relues, "portees": portees}


class Lecture:
    """Les lignes d'UNE requête, comptées au fil de leur récupération."""

    __slots__ = ("mesure", "cle", "cle_jours", "portee", "jours")

    def __init__(self, mesure: Mesure, cle: tuple, bornes_) -> None:
        self.mesure = mesure
        self.cle = cle
        base, consommateur, _, table = cle
        self.cle_jours = (base, consommateur, table)
        if bornes_ is None:
            self.portee, self.jours = SANS_BORNES, ()
        else:
            (debut, fin), paire = bornes_
            duree = max(0, fin - debut)
            self.portee = next(nom for borne, nom in PORTEES
                               if borne is None or duree < borne)
            self.jours = tuple(
                (paire, j) for j in
                range(debut // 86400, max(debut, fin - 1) // 86400 + 1))

    def lignes_lues(self, lignes: Iterable[Any]) -> None:
        try:
            n = o = 0
            for ligne in lignes:
                if ligne is None:
                    continue
                n += 1
                o += octets_ligne(ligne)
        except Exception as erreur:                      # noqa: BLE001
            self.mesure._desactiver(erreur)
            return
        self.mesure.ajouter(self, n, o)


def octets_valeur(v: Any) -> int:
    """Taille d'une valeur dans le format texte du protocole PostgreSQL."""
    if v is None:
        return 0
    if isinstance(v, (bytes, bytearray, memoryview)):
        return 2 * len(v) + 2
    if isinstance(v, str):
        return len(v.encode("utf-8"))
    if isinstance(v, bool):
        return 1
    if isinstance(v, (int, float)):
        return len(repr(v))
    return len(str(v))


def octets_ligne(ligne: Any) -> int:
    if ligne is None:
        return 0
    try:
        valeurs = tuple(ligne)
    except TypeError:
        valeurs = (ligne,)
    return (OCTETS_PAR_LIGNE + OCTETS_PAR_CHAMP * len(valeurs)
            + sum(octets_valeur(v) for v in valeurs))


def bornes(params) -> tuple[tuple[int, int], str | None] | None:
    """((début, fin) en secondes, paire) d'une lecture bornée, sinon None :
    au moins deux paramètres qui ressemblent à des instants."""
    if not params:
        return None
    instants = []
    paire = None
    for p in params:
        if isinstance(p, bool):
            continue
        if isinstance(p, int):
            if _SEC[0] <= p < _SEC[1]:
                instants.append(p)
            elif _MS[0] <= p < _MS[1]:
                instants.append(p // 1000)
        elif isinstance(p, str) and paire is None and "_" in p:
            paire = p
    if len(instants) < 2:
        return None
    return (min(instants), max(instants)), paire


def appelant() -> tuple[str, str | None]:
    """(consommateur, opération) : la première fonction du projet hors de
    la couche de stockage, et la méthode de `MarketReader` traversée."""
    operation = None
    cadre = sys._getframe(2)
    while cadre is not None:
        fichier = cadre.f_code.co_filename.replace(os.sep, "/")
        if fichier.startswith(_RACINE):
            relatif = fichier[len(_RACINE):]
            if relatif in COUCHE:
                if relatif == "store/market.py" and operation is None:
                    operation = f"market.{cadre.f_code.co_name}"
            else:
                return f"{relatif}:{cadre.f_code.co_name}", operation
        cadre = cadre.f_back
    return "hors du projet", operation


def nom_de_base(url: str) -> str:
    """« active », « relève », ou « autre », d'après les variables du pool —
    jamais l'adresse elle-même."""
    if url and url == os.environ.get("DATABASE_URL", ""):
        return "active"
    for nom, valeur in os.environ.items():
        if nom.startswith("DATABASE_URL_") and valeur and valeur == url:
            return "relève" if "RELEVE" in nom else nom[len("DATABASE_URL_"):]
    return "autre"


MESURE = Mesure()
