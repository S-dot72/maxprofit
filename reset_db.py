#!/usr/bin/env python
"""
Suppression de la base — script isolé (spec §1.2).

C'est le SEUL fichier du projet autorisé à détruire des données. Il n'est
importé par rien : aucun `import reset_db` n'existe ailleurs, et il vit hors du
paquet `maxprofit/` pour qu'un import de paquet ne puisse pas le charger par
inadvertance.

Pour l'exécuter, il faut réunir quatre conditions indépendantes :

  1. passer `--i-understand-this-deletes-everything` ;
  2. être sur un terminal interactif — un script de déploiement, un cron ou un
     conteneur ne peuvent donc pas le déclencher, même en recopiant la ligne de
     commande complète ;
  3. retaper à la main le nom exact du fichier de base ;
  4. laisser le script écrire une dernière sauvegarde, qu'il ne supprime pas.

La quatrième condition n'est pas dans la spec, je l'ajoute parce que le coût
d'un instantané de plus est nul et que la seule raison d'exécuter ce script est
un moment où l'on est pressé et sûr de soi.

    python reset_db.py --i-understand-this-deletes-everything
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from maxprofit.core.config import backups_dir, db_path
from maxprofit.core.errors import BotError
from maxprofit.store.backup import creer_sauvegarde
from maxprofit.store.db import open_read_write

DRAPEAU = "--i-understand-this-deletes-everything"


def main(argv: list[str] | None = None) -> int:
    parseur = argparse.ArgumentParser(
        description="Supprime la base de données. Irréversible.",
    )
    parseur.add_argument(
        DRAPEAU, dest="confirme", action="store_true",
        help="Obligatoire. Sans ce drapeau, le script ne fait rien.",
    )
    args = parseur.parse_args(argv)

    if not args.confirme:
        print(f"Refus : {DRAPEAU} est obligatoire.", file=sys.stderr)
        return 2

    if not sys.stdin.isatty():
        print(
            "Refus : ce script exige un terminal interactif. Il ne peut pas "
            "être exécuté par un script de déploiement, un cron ou un "
            "conteneur — c'est délibéré.",
            file=sys.stderr,
        )
        return 2

    try:
        cible = db_path()
    except BotError as erreur:
        print(f"Refus : {erreur}", file=sys.stderr)
        return 2

    if not cible.is_file():
        print(f"Rien à supprimer : {cible} n'existe pas.")
        return 0

    taille_mo = cible.stat().st_size / 1e6
    print(f"Base      : {cible}")
    print(f"Taille    : {taille_mo:.1f} Mo")
    print(f"Sauvegardes conservées dans : {backups_dir()}")
    print()
    print(f"Retapez le nom du fichier pour confirmer ({cible.name}) :")
    saisie = input("> ").strip()

    if saisie != cible.name:
        print("Le nom ne correspond pas. Rien n'a été supprimé.", file=sys.stderr)
        return 1

    horodatage = datetime.now(timezone.utc)
    conn = open_read_write(cible)
    try:
        sauvegarde = creer_sauvegarde(conn, backups_dir(), now=horodatage)
    finally:
        conn.close()
    print(f"Dernière sauvegarde écrite : {sauvegarde}")

    for fichier in (cible,
                    cible.with_name(cible.name + "-wal"),
                    cible.with_name(cible.name + "-shm")):
        if fichier.is_file():
            fichier.unlink()
            print(f"Supprimé : {fichier}")

    print()
    print("Terminé. La base sera recréée vide au prochain démarrage du "
          "collecteur, et remigrée depuis la version 0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
