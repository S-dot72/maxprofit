"""
Sauvegardes automatiques (spec §1.4).

`VACUUM INTO` produit une copie COHÉRENTE sans arrêter le collecteur : SQLite
sérialise un instantané transactionnel dans un fichier neuf. C'est la raison de
préférer cette commande à une copie de fichier, qui attraperait la base au
milieu d'une écriture et donnerait une sauvegarde subtilement corrompue — que
l'on découvrirait le jour de la restauration.

Rétention : 7 quotidiennes + 4 hebdomadaires.

La purge mérite plus de prudence que sa taille ne le suggère : c'est le seul
endroit du projet qui supprime un fichier. Trois garde-fous, dans cet ordre :
le fichier doit se trouver dans le répertoire de sauvegardes, son nom doit
correspondre EXACTEMENT au motif produit par ce module, et il ne doit être ni
la base vive ni un de ses journaux. Un fichier qui échoue à l'un des trois est
laissé en place — un fichier inconnu en trop ne coûte rien, un fichier
supprimé à tort coûte la collecte.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from maxprofit.core.errors import BotError

log = logging.getLogger(__name__)

#: market_20240131_1830.db — la minute suffit, on sauvegarde toutes les 6 h.
NOM_SAUVEGARDE = re.compile(r"^market_(\d{8})_(\d{4})\.db$")
FORMAT_HORODATAGE = "%Y%m%d_%H%M"

JOURNALIERES_CONSERVEES = 7
HEBDOMADAIRES_CONSERVEES = 4


def creer_sauvegarde(conn: sqlite3.Connection, backups_dir: Path,
                     now: datetime | None = None) -> Path:
    """Écrit un instantané cohérent et retourne son chemin.

    `now` est injectable pour les tests : le reste du projet interdit la lecture
    de l'horloge murale dans les stratégies, ici elle est légitime mais reste
    paramétrable pour que les tests de rétention soient déterministes.
    """
    now = now or datetime.now(timezone.utc)
    backups_dir.mkdir(parents=True, exist_ok=True)
    cible = backups_dir / f"market_{now.strftime(FORMAT_HORODATAGE)}.db"

    if cible.exists():
        # VACUUM INTO refuse d'écraser. Deux sauvegardes dans la même minute
        # n'arrivent qu'en test ou après un redémarrage en boucle.
        log.warning("Sauvegarde %s déjà présente, ignorée", cible.name)
        return cible

    # Le chemin ne peut pas être un paramètre lié dans VACUUM INTO. Il est
    # construit ici à partir d'un horodatage, jamais d'une entrée externe.
    conn.execute(f"VACUUM INTO '{_echapper(cible)}'")
    log.info("Sauvegarde écrite : %s (%.1f Mo)",
             cible, cible.stat().st_size / 1e6)
    return cible


def _echapper(path: Path) -> str:
    texte = str(path)
    if "'" in texte:
        raise BotError(
            f"Le chemin de sauvegarde contient une apostrophe : {texte}. "
            f"Choisissez un répertoire sans apostrophe."
        )
    return texte


def _horodatage(fichier: Path) -> datetime | None:
    m = NOM_SAUVEGARDE.match(fichier.name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)}_{m.group(2)}",
                                 FORMAT_HORODATAGE).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def sauvegardes_a_conserver(fichiers: dict[Path, datetime]) -> set[Path]:
    """La plus récente de chacun des 7 derniers JOURS représentés, plus la plus
    récente de chacune des 4 dernières SEMAINES représentées.

    « Derniers jours représentés » et non « 7 derniers jours calendaires » : si
    le collecteur a été arrêté deux semaines, on garde quand même sept
    sauvegardes, au lieu de tout purger parce que rien n'est récent.
    """
    par_jour: dict[str, Path] = {}
    par_semaine: dict[str, Path] = {}
    for fichier, quand in sorted(fichiers.items(), key=lambda kv: kv[1]):
        par_jour[quand.strftime("%Y-%m-%d")] = fichier
        annee, semaine, _ = quand.isocalendar()
        par_semaine[f"{annee}-{semaine:02d}"] = fichier

    garder = {par_jour[k] for k in sorted(par_jour)[-JOURNALIERES_CONSERVEES:]}
    garder |= {par_semaine[k] for k in sorted(par_semaine)[-HEBDOMADAIRES_CONSERVEES:]}
    return garder


def purger(backups_dir: Path, db_path: Path) -> list[Path]:
    """Supprime les sauvegardes hors rétention. Retourne ce qui a été supprimé."""
    if not backups_dir.is_dir():
        return []

    proteges = {
        db_path.resolve(),
        db_path.with_name(db_path.name + "-wal").resolve(),
        db_path.with_name(db_path.name + "-shm").resolve(),
    }

    connues: dict[Path, datetime] = {}
    for fichier in backups_dir.iterdir():
        if not fichier.is_file():
            continue
        quand = _horodatage(fichier)
        if quand is None:
            # Nom inconnu : ce n'est pas une de nos sauvegardes, on n'y touche
            # pas. Le répertoire peut contenir une copie manuelle précieuse.
            continue
        connues[fichier] = quand

    garder = sauvegardes_a_conserver(connues)
    supprimes: list[Path] = []
    for fichier in sorted(connues):
        if fichier in garder:
            continue
        if fichier.resolve() in proteges:
            log.error("Refus de supprimer %s : c'est la base vive", fichier)
            continue
        if fichier.parent.resolve() != backups_dir.resolve():
            log.error("Refus de supprimer %s : hors du répertoire de sauvegardes",
                      fichier)
            continue
        fichier.unlink()
        supprimes.append(fichier)

    if supprimes:
        log.info("Purge : %d sauvegarde(s) supprimée(s), %d conservée(s)",
                 len(supprimes), len(garder))
    return supprimes


def sauvegarder_et_purger(conn: sqlite3.Connection, db_path: Path,
                          backups_dir: Path,
                          now: datetime | None = None) -> Path:
    cible = creer_sauvegarde(conn, backups_dir, now=now)
    purger(backups_dir, db_path)
    return cible
