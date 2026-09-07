"""
Quelle version tourne réellement.

On a perdu deux échanges à discuter d'un correctif en regardant le journal
d'une image antérieure. Le journal ne le disait pas, `/etat` non plus, et rien
dans le message d'erreur ne permettait de trancher. C'est une question qu'on se
pose à chaque déploiement raté, et il n'y a aucune raison d'y répondre à la main.

L'hébergeur connaît le commit déployé et le met dans l'environnement. On le
journalise au démarrage et on l'affiche dans `/etat` : deux endroits où l'on
regarde déjà quand quelque chose ne va pas.

Aucune de ces variables n'est garantie — en local il n'y en a aucune. L'absence
se dit alors telle quelle, plutôt que d'inventer un numéro.
"""

from __future__ import annotations

import os
import subprocess

#: Par ordre de préférence. Render définit la première ; les autres couvrent
#: les hébergeurs voisins, pour que déménager ne fasse pas retomber dans le
#: silence qu'on vient de corriger.
VARIABLES = (
    "RENDER_GIT_COMMIT",
    "RAILWAY_GIT_COMMIT_SHA",
    "FLY_MACHINE_VERSION",
    "SOURCE_COMMIT",
    "GIT_COMMIT",
)


def commit() -> str | None:
    """Le commit déployé, court. `None` si personne ne le dit."""
    for nom in VARIABLES:
        brut = os.environ.get(nom, "").strip()
        if brut:
            return brut[:7]
    return _commit_local()


def _commit_local() -> str | None:
    """En développement, git répond. En conteneur, il n'est pas installé —
    d'où le repli silencieux : ne pas savoir n'est pas une panne."""
    try:
        sortie = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    valeur = sortie.stdout.strip()
    return valeur or None


def resume() -> str:
    """Une ligne, toujours affichable."""
    reference = commit()
    return f"Version déployée : {reference}" if reference else (
        "Version déployée : inconnue (aucune variable de commit)"
    )
