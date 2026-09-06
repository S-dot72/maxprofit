"""
Exceptions du noyau.

Une règle unique gouverne ce fichier : tout ce qui touche à l'argent ou aux
données échoue par une exception, jamais par une valeur de repli. Un défaut
silencieux ne se voit pas dans les logs, il se voit dans un backtest faux
trois semaines plus tard (spec §5).
"""

from __future__ import annotations


class BotError(Exception):
    """Racine de toutes les erreurs du projet."""


class ConfigurationError(BotError):
    """Paramètre obligatoire absent, vide ou illisible.

    Levée au démarrage. Absence de configuration = arrêt, pas valeur par
    défaut (spec §5).
    """


class TimebaseError(BotError):
    """Horodatage dont l'unité est incohérente avec ce qui est attendu.

    Attrape la confusion secondes / millisecondes, qui décale un backtest
    d'un facteur 1000 sans rien lever ailleurs (spec §5).
    """


class LookAheadError(BotError):
    """Tentative d'accès à une information postérieure à l'instant de décision.

    Ne devrait jamais être levée en fonctionnement normal : la MarketView rend
    l'accès au futur structurellement impossible (spec §2.1). Cette exception
    existe pour les cas où un moteur construit une vue incohérente.
    """


class LayerViolation(BotError):
    """Une couche importe ou fait ce qu'elle n'a pas le droit de faire.

    Vérifiée mécaniquement par tests/test_layering.py (spec §0).
    """
