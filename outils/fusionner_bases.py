#!/usr/bin/env python
r"""
Verser une base PostgreSQL dans une autre qui contient déjà des données.

    $env:SOURCE_DATABASE_URL = "postgresql://..."   # la base à exporter
    $env:CIBLE_DATABASE_URL  = "postgresql://..."   # la base qui reçoit
    .venv313\Scripts\python.exe outils\fusionner_bases.py              # à blanc
    .venv313\Scripts\python.exe outils\fusionner_bases.py --appliquer  # pour de vrai

Les règles de fusion sont dans `maxprofit/store/fusion.py`. En bref : rien
n'est effacé dans la cible ; ce qui manque est ajouté ; seuls l'état d'une
campagne (le plus récent gagne) et le chemin de ticks d'une minute (le plus
complet gagne) peuvent remplacer une ligne existante.

**À blanc par défaut.** La cible a déjà de la valeur : on regarde d'abord ce
qui va entrer, puis on applique.

**Relançable sans risque.** Chaque table est dédoublonnée sur sa clé : une
seconde passe n'ajoute rien de ce que la première a versé, et rattrape ce qui
aurait été écrit à la source entre-temps.

**Les URL ne passent pas en argument**, pour qu'un mot de passe n'aille pas se
loger dans l'historique du terminal. Elles ne sont jamais affichées en clair.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _interpreteur import exiger  # noqa: E402

exiger("psycopg", r"outils\fusionner_bases.py")

from maxprofit.store.db import open_postgres          # noqa: E402
from maxprofit.store.fusion import fusionner          # noqa: E402
from maxprofit.store.postgres import (                # noqa: E402
    PostgresIndisponible, _sans_secret)


def _url(nom: str) -> str:
    valeur = os.environ.get(nom, "").strip()
    if not valeur:
        sys.exit(f"{nom} n'est pas défini. Exemple, dans PowerShell :\n"
                 f'    $env:{nom} = "postgresql://..."')
    return valeur


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--appliquer", action="store_true",
                    help="Écrire pour de vrai. Sans lui, essai à blanc.")
    args = ap.parse_args()

    source_url = _url("SOURCE_DATABASE_URL")
    cible_url = _url("CIBLE_DATABASE_URL")
    if source_url == cible_url:
        sys.exit("La source et la cible sont la même base : rien à fusionner.")

    print(f"source : {_sans_secret(source_url)}")
    print(f"cible  : {_sans_secret(cible_url)}")
    print("mode   : " + ("ÉCRITURE" if args.appliquer else "à blanc — rien "
                         "ne sera écrit, relancez avec --appliquer"))
    print()
    try:
        source = open_postgres(source_url, ecriture=False)
    except PostgresIndisponible as erreur:
        sys.exit(f"Source injoignable : {erreur}")
    try:
        cible = open_postgres(cible_url, ecriture=args.appliquer)
    except PostgresIndisponible as erreur:
        sys.exit(f"Cible injoignable : {erreur}")

    debut = time.monotonic()
    bilans = fusionner(source, cible, appliquer=args.appliquer)
    source.close()
    cible.close()

    total = sum(b.a_inserer for b in bilans)
    remplacees = sum(b.a_remplacer for b in bilans)
    print()
    print(f"{'Inséré' if args.appliquer else 'À insérer'} : {total} ligne(s), "
          f"{'remplacé' if args.appliquer else 'à remplacer'} : "
          f"{remplacees}, en {time.monotonic() - debut:.0f} s.")
    if not args.appliquer and (total or remplacees):
        print("Rien n'a été écrit. Relancez avec --appliquer pour verser.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
