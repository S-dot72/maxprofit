"""
Ce que le processus occupe en mémoire, pour le dire dans les messages.

Le serveur gratuit a 512 Mo pour tout : collecte, bot, course et études.
Au-delà, l'hébergeur tue le processus sans qu'aucune ligne de Python ne
s'exécute : une étude longue s'arrête alors en silence, et rien ne
distingue ce silence d'un calcul qui dure. Afficher la mémoire dans les
points d'avancement et dans `/etat` change ce silence en une tendance
lisible.

Rien ici n'est garanti : hors Linux, ou sans cgroup lisible, la valeur
manque et on le dit, plutôt que d'inventer un chiffre. On ne fait que
lire `/proc` et `/sys/fs/cgroup` : le noyau reste sans autre dépendance
que la bibliothèque standard qu'il s'autorise.
"""

from __future__ import annotations

_STATUT = "/proc/self/status"
#: (utilisée, limite) : cgroup v2, puis v1.
_CGROUPS = (
    ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
    ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
     "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
_MO = 1024 * 1024
#: En cgroup v1, « sans limite » s'écrit comme un nombre gigantesque.
_SANS_LIMITE = 1 << 50


def processus(chemin: str = _STATUT) -> tuple[float | None, float | None]:
    """(résidente, pic depuis le démarrage) du processus, en Mo."""
    rss = pic = None
    try:
        with open(chemin, encoding="ascii", errors="replace") as fichier:
            for ligne in fichier:
                if ligne.startswith("VmRSS:"):
                    rss = int(ligne.split()[1]) / 1024
                elif ligne.startswith("VmHWM:"):
                    pic = int(ligne.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return rss, pic


def residente(chemin: str = _STATUT) -> float | None:
    """La mémoire résidente seule, en Mo."""
    return processus(chemin)[0]


def conteneur(chemins=_CGROUPS) -> tuple[float | None, float | None]:
    """(utilisée, limite) du conteneur en Mo ; la limite est None si le
    conteneur n'en déclare pas. Le cache de fichiers y est compté : c'est
    une borne haute, que le noyau rogne avant de tuer."""
    for courant, limite in chemins:
        try:
            with open(courant, encoding="ascii") as fichier:
                utilisee = int(fichier.read().strip()) / _MO
        except (OSError, ValueError):
            continue
        plafond = None
        try:
            with open(limite, encoding="ascii") as fichier:
                brut = fichier.read().strip()
            if brut != "max" and int(brut) < _SANS_LIMITE:
                plafond = int(brut) / _MO
        except (OSError, ValueError):
            pass
        return utilisee, plafond
    return None, None


def resume(statut: str = _STATUT, cgroups=_CGROUPS) -> str:
    """« mémoire 281 Mo (pic 310 Mo) ; conteneur 330 / 512 Mo »."""
    rss, pic = processus(statut)
    if rss is None and pic is None:
        texte = "mémoire inconnue"
    elif rss is None:
        texte = f"mémoire : pic {pic:.0f} Mo"
    else:
        texte = f"mémoire {rss:.0f} Mo" + (
            f" (pic {pic:.0f} Mo)" if pic is not None else "")
    utilisee, plafond = conteneur(cgroups)
    if utilisee is not None:
        texte += f" ; conteneur {utilisee:.0f}" + (
            f" / {plafond:.0f} Mo" if plafond is not None else " Mo")
    return texte
