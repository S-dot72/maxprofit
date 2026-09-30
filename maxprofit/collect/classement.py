"""
Les N meilleures paires à collecter et trader, classées sur ce qui est MESURÉ.

La course n'entre que lorsque le broker paie son maximum : une paire qui y est
rarement ne rapporte presque rien, quelle que soit sa qualité. Le critère est
donc la part des relevés de payout au plafond, sur les derniers jours — la
table `payouts` couvre TOUTES les paires du catalogue, collectées ou non.

Seules les paires entre deux des huit grandes devises flottantes concourent.
Le reste a déjà coûté : une crypto n'a pas d'échéance de 900 s (BTCUSD_otc),
une devise arrimée ne bouge pas (AEDCNY_otc : zéro signal sur 416 bougies),
et une action ou une matière première n'est pas ce que la stratégie a vu.

Le classement PROPOSE (`/classement`), il n'impose rien : la liste collectée
est `PAIRES_FIXES`, collée une fois et stable. Appliqué à chaque démarrage, il
faisait entrer et sortir des paires à chaque déploiement, et leurs séries se
fragmentaient.
"""

from __future__ import annotations

from typing import Sequence

from maxprofit.core.payout import FLUX_POUR_LE_PLAFOND

DEVISES_FLOTTANTES = frozenset(
    ("EUR", "USD", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"))
#: Relevés minimaux pour qu'une part au plafond veuille dire quelque chose.
RELEVES_MIN = 50
JOURS_DE_CLASSEMENT = 7


def est_une_paire_flottante(nom: str) -> bool:
    if not nom.endswith("_otc"):
        return False
    base = nom[:-len("_otc")]
    return (len(base) == 6 and base[:3] in DEVISES_FLOTTANTES
            and base[3:] in DEVISES_FLOTTANTES and base[:3] != base[3:])


def classer(conn, depuis_sec: int) -> list[tuple[str, float, int]]:
    """(paire, part des relevés au plafond, relevés), la meilleure d'abord."""
    lignes = conn.execute(
        "SELECT pair, COUNT(*), SUM(CASE WHEN is_open = 1 AND payout_pct >= ? "
        "THEN 1 ELSE 0 END) FROM payouts WHERE ts_sec >= ? GROUP BY pair",
        (FLUX_POUR_LE_PLAFOND, depuis_sec)).fetchall()
    classees = [(str(p), int(m or 0) / int(n), int(n)) for p, n, m in lignes
                if n and int(n) >= RELEVES_MIN and est_une_paire_flottante(str(p))]
    classees.sort(key=lambda x: (-x[1], x[0]))
    return classees


def completer(tete: Sequence[str], classement: Sequence[tuple[str, float, int]],
              total: int) -> tuple[str, ...]:
    """La tête, puis les mieux classées, jusqu'à `total` paires."""
    sortie = list(dict.fromkeys(tete))
    for pair, part, _ in classement:
        if len(sortie) >= total:
            break
        if pair not in sortie and part > 0:
            sortie.append(pair)
    return tuple(sortie[:max(total, len(tete))])
