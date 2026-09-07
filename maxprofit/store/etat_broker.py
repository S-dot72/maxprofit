"""
Attendre entre deux tentatives — y compris à travers un redémarrage.

**Le problème que cela résout.** Quand le broker est injoignable, le processus
meurt : c'est la seule façon d'arrêter le thread de la bibliothèque, qui compose
toutes les dix secondes tant qu'il vit. Mais l'hébergeur relance aussitôt, et
l'on rappelle le broker quarante secondes plus tard. Indéfiniment.

Si le refus vient d'une limitation de débit — et c'est le cas le plus probable
quand une adresse qui fonctionnait cesse de fonctionner — ce cycle empêche la
limitation d'expirer. On se maintient soi-même en pénitence, et l'on conclut au
bout de deux jours que « le broker bloque les hébergeurs », alors qu'il suffisait
de se taire une heure.

Un compteur en mémoire n'y changerait rien : il meurt avec le processus. L'état
est donc écrit en base, la seule chose qui survive à un redéploiement.

**L'attente a lieu AVANT de construire le client.** C'est essentiel : une fois
`PocketOption(...)` construit, son thread appelle le broker et rien ne l'arrête.
Attendre après coup ne ferait qu'ajouter du silence par-dessus du bruit.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

#: Attente après le n-ième échec consécutif, en secondes.
#:
#: Les trois premiers échecs ne coûtent RIEN. C'est délibéré : une coupure
#: réseau de dix secondes est le cas courant, et lui répondre par une minute de
#: silence creuserait dans les bougies un trou plus grand que la panne. Le
#: collecteur a déjà sa propre temporisation en mémoire (1 s, 2 s, 4 s…) qui
#: suffit largement pour ça.
#:
#: À partir du quatrième, la progression devient franche. Quatre refus d'affilée
#: ne sont plus un incident : c'est un refus. Continuer à composer ne fait
#: qu'entretenir la raison du refus.
PALIERS_SEC = (0, 0, 0, 0, 60, 180, 600, 1800, 3600, 7200, 21600)


def lire(conn) -> tuple[int, int | None, str | None]:
    """(échecs consécutifs, instant du dernier échec, dernière raison)."""
    try:
        ligne = conn.execute(
            "SELECT echecs_consecutifs, dernier_echec_ts_sec, derniere_raison "
            "FROM etat_broker WHERE id = 1"
        ).fetchone()
    except Exception as erreur:                          # noqa: BLE001
        # Table absente : base plus ancienne que ce code. Ne pas attendre est
        # le comportement le moins surprenant, et la migration la créera.
        log.debug("État broker illisible : %s", erreur)
        return 0, None, None
    if ligne is None:
        return 0, None, None
    return int(ligne[0] or 0), ligne[1], ligne[2]


def attente_requise(conn, maintenant: float | None = None) -> float:
    """Combien de secondes se taire avant de rappeler le broker.

    Zéro si la dernière tentative a réussi, ou si l'attente due est déjà
    écoulée — ce qui est le cas normal après un arrêt un peu long.
    """
    echecs, dernier_echec, _ = lire(conn)
    if not echecs or dernier_echec is None:
        return 0.0
    palier = PALIERS_SEC[min(echecs, len(PALIERS_SEC) - 1)]
    ecoule = (maintenant if maintenant is not None else time.time()) - dernier_echec
    return max(0.0, palier - ecoule)


def noter_echec(conn, raison: str) -> int:
    """Incrémente le compteur. Retourne le nombre d'échecs consécutifs."""
    echecs, _, _ = lire(conn)
    echecs += 1
    try:
        conn.execute(
            "UPDATE etat_broker SET echecs_consecutifs = ?, "
            "dernier_echec_ts_sec = ?, derniere_raison = ? WHERE id = 1",
            (echecs, int(time.time()), str(raison)[:500]),
        )
        conn.commit()
    except Exception as erreur:                          # noqa: BLE001
        # Ne pas pouvoir mémoriser l'échec est fâcheux mais pas fatal : la
        # collecte doit continuer d'essayer plutôt que de s'arrêter sur un
        # problème d'écriture.
        log.warning("Échec broker non mémorisé : %s", erreur)
    return echecs


def noter_succes(conn) -> None:
    """Remet le compteur à zéro.

    Une connexion réussie efface l'ardoise : les échecs qui l'ont précédée
    étaient passagers, et faire porter leur poids à la panne suivante
    imposerait des heures d'attente pour une coupure d'une minute.
    """
    try:
        conn.execute(
            "UPDATE etat_broker SET echecs_consecutifs = 0, "
            "dernier_succes_ts_sec = ? WHERE id = 1",
            (int(time.time()),),
        )
        conn.commit()
    except Exception as erreur:                          # noqa: BLE001
        log.warning("Succès broker non mémorisé : %s", erreur)


def resume(conn) -> str:
    echecs, dernier_echec, raison = lire(conn)
    if not echecs:
        return "Broker : aucune tentative en échec."
    attente = attente_requise(conn)
    texte = f"Broker : {echecs} échec(s) consécutif(s)"
    if attente > 0:
        texte += f", nouvelle tentative dans {int(attente)} s"
    if raison:
        texte += f"\n{raison[:200]}"
    return texte
