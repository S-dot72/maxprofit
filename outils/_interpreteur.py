"""
Vérifier l'interpréteur par ce qu'il SAIT FAIRE, pas par son chemin.

Les deux outils exigeaient `.venv/Scripts/python.exe`, en dur. C'était juste
tant qu'il n'existait qu'un environnement. Le jour où libsql a cessé de publier
un binaire pour le Python du projet, il a fallu un second venv en 3.13 — et les
outils l'ont refusé alors qu'il était le SEUL à pouvoir les faire tourner.

Un contrôle de chemin ne répond pas à la question posée. La question est : « ce
Python peut-il importer ce dont j'ai besoin ? » On la pose directement. Et si la
réponse est non, on cherche les environnements du projet qui, eux, le peuvent —
au lieu de nommer un chemin qui n'est peut-être plus le bon.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]


def _python_de(venv: Path) -> Path:
    return (venv / "Scripts" / "python.exe" if os.name == "nt"
            else venv / "bin" / "python")


def _venvs_du_projet() -> list[Path]:
    """Tout répertoire de la racine qui ressemble à un environnement."""
    trouves = []
    for entree in sorted(RACINE.iterdir()):
        if entree.is_dir() and _python_de(entree).is_file():
            trouves.append(_python_de(entree))
    return trouves


def _sait_importer(python: Path, module: str) -> bool:
    try:
        code = subprocess.run(
            [str(python), "-c", f"import {module}"],
            capture_output=True, timeout=60, check=False,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return False
    return code == 0


def exiger(module: str, commande: str) -> None:
    """Sortir avec un message utile si `module` n'est pas importable ici.

    Ne lance AUCUN sous-processus dans le cas normal : si le module s'importe,
    la fonction rend la main tout de suite. Les sous-processus ne servent qu'à
    composer le message d'erreur, où quelques secondes ne coûtent rien.
    """
    if importlib.util.find_spec(module) is not None:
        return

    lignes = [
        f"Le module « {module} » n'est pas disponible dans cet interpréteur.",
        f"  utilisé : {sys.executable}",
        "",
    ]
    capables = [p for p in _venvs_du_projet()
                if Path(p).resolve() != Path(sys.executable).resolve()
                and _sait_importer(p, module)]
    if capables:
        lignes.append("Un environnement du projet en est capable :")
        lignes += [f"    {p} {commande}" for p in capables]
    else:
        lignes.append("Aucun environnement du projet ne l'a. Installez-le :")
        lignes.append("    python -m pip install -r requirements-broker.txt")
    print("\n".join(lignes), file=sys.stderr)
    raise SystemExit(2)
