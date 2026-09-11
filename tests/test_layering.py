"""
Les frontières de la spec §0, vérifiées mécaniquement plutôt que promises.

| Couche   | Rôle                               | Ne fait jamais                    |
|----------|------------------------------------|-----------------------------------|
| Collecte | Enregistrer ticks, payouts, uptime | Analyser, filtrer, décider        |
| Backtest | Rejouer l'historique, mesurer      | Écrire dans les tables de marché  |
| Live     | Émettre des signaux                | Contenir sa copie de la stratégie |

Une frontière que seule la relecture protège finit toujours par être franchie,
et le franchissement ne se voit pas : le code marche. On l'analyse donc par AST
à chaque exécution des tests.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "maxprofit"

#: Pour chaque sous-paquet : les sous-paquets qu'il a le DROIT d'importer.
#: Toute entrée manquante fait échouer `test_tout_sous_paquet_est_declare` :
#: ajouter un paquet oblige à prendre position sur ses dépendances.
ALLOWED: dict[str, set[str]] = {
    # Le noyau ne connaît personne. C'est ce qui garantit qu'il n'existe
    # qu'UNE définition de Candle, Signal, MarketView et Strategy.
    "core": set(),
    # La persistance ne connaît que les types du domaine. Elle ignore qui la
    # lit, ce qui lui permet de servir une écriture au collecteur et une
    # lecture seule au backtest sans les faire se rencontrer.
    "store": {"core"},
    # La collecte enregistre. Elle ignore l'existence des stratégies : si elle
    # les connaissait, ses données seraient façonnées par les règles du moment
    # et le backtest deviendrait circulaire.
    "collect": {"core", "store"},
    # Indicateurs : fonctions pures d'une fenêtre de bougies. Ils ne
    # connaissent ni la persistance ni les stratégies, ce qui garantit
    # qu'ils ne peuvent pas aller chercher une bougie hors de la fenêtre
    # qu'on leur passe.
    "indicators": {"core"},
    # Le seul lieu de la logique de décision.
    "strategies": {"core", "indicators"},
    # Les deux moteurs importent LA MÊME stratégie. Ils ne se connaissent pas
    # l'un l'autre et ne passent pas par la couche Collecte : ils lisent les
    # données via `store`, sur un descripteur en lecture seule.
    "backtest": {"core", "store", "strategies", "indicators"},
    "live": {"core", "store", "strategies", "indicators"},
    # Couche d'exécution : démarre les processus et expose la sonde HTTP
    # attendue par l'hébergeur. Aucune logique métier — elle assemble.
    "hosting": {"core", "store", "collect"},
    # Note : `hosting` importe Telegram, mais UNIQUEMENT pour
    # l'exploitation — alertes et renouvellement du jeton de session.
    # Aucun signal, aucune stratégie : le bot de signaux reste
    # l'étape 8, conditionnée par une étape 7 concluante.
}


def modules():
    """(chemin, sous-paquet, arbre) pour chaque module du projet."""
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG)
        if len(rel.parts) < 2:  # maxprofit/__init__.py
            continue
        yield path, rel.parts[0], ast.parse(path.read_text(encoding="utf-8"), str(path))


def import_roots(node: ast.AST) -> list[str]:
    """Racine de chaque module importé par ce noeud (`a.b.c` -> `a`)."""
    if isinstance(node, ast.Import):
        return [a.name.split(".")[0] for a in node.names]
    if isinstance(node, ast.ImportFrom) and not node.level:
        return [(node.module or "").split(".")[0]]
    return []


def imported_subpackages(tree: ast.AST) -> set[str]:
    """Sous-paquets `maxprofit.X` importés par ce module."""
    found: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names = [node.module or ""]
        for name in names:
            parts = name.split(".")
            if parts[0] == "maxprofit" and len(parts) > 1:
                found.add(parts[1])
    return found


def test_tout_sous_paquet_est_declare():
    presents = {
        p.name for p in PKG.iterdir() if p.is_dir() and not p.name.startswith("__")
    }
    non_declares = presents - set(ALLOWED)
    assert not non_declares, (
        f"Sous-paquet(s) {sorted(non_declares)} sans règle de dépendance. "
        f"Ajoutez une entrée dans ALLOWED : une couche dont les droits ne sont "
        f"pas écrits est une couche qui n'en a plus."
    )


@pytest.mark.parametrize("sub", sorted(ALLOWED))
def test_dependances_entre_couches(sub):
    autorises = ALLOWED[sub] | {sub}
    for path, module_sub, tree in modules():
        if module_sub != sub:
            continue
        interdits = imported_subpackages(tree) - autorises
        assert not interdits, (
            f"{path.relative_to(ROOT)} importe {sorted(interdits)}, "
            f"interdit à la couche '{sub}' (autorisé : {sorted(autorises)})."
        )


def test_le_noyau_ne_depend_que_de_la_bibliotheque_standard():
    # Une dépendance tierce dans le noyau se propagerait aux trois couches et
    # au CI. Le noyau doit rester importable partout, sans rien installer.
    stdlib_ok = {
        "__future__", "abc", "ast", "collections", "dataclasses", "datetime",
        "enum", "json", "math", "os", "pathlib", "re", "types", "typing",
        "zoneinfo",
        "maxprofit",
    }
    for path, sub, tree in modules():
        if sub != "core":
            continue
        for node in ast.walk(tree):
            for racine in import_roots(node):
                assert racine in stdlib_ok, (
                    f"{path.relative_to(ROOT)} importe '{racine}', hors "
                    f"bibliothèque standard. Le noyau reste sans dépendance."
                )


def test_invariant_n1_aucune_strategie_hors_du_paquet_strategies():
    """Spec §0, invariant n°1 : une seule classe Strategy.

    « Deux implémentations divergeront, toujours, et le backtest deviendra un
    mensonge. » Il ne peut donc exister qu'un seul répertoire où l'on écrit une
    stratégie ; les deux moteurs y puisent le même objet.
    """
    coupables = []
    for path, sub, tree in modules():
        if path == PKG / "core" / "strategy.py":
            continue  # la classe de base elle-même
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                nom = (
                    base.attr
                    if isinstance(base, ast.Attribute)
                    else getattr(base, "id", None)
                )
                if nom == "Strategy" and sub != "strategies":
                    coupables.append(f"{path.relative_to(ROOT)}::{node.name}")
    assert not coupables, (
        f"Sous-classe(s) de Strategy hors de maxprofit/strategies : {coupables}. "
        f"Invariant n°1 : le code de stratégie est identique en backtest et en "
        f"live, donc il n'existe qu'à un seul endroit."
    )


def test_les_strategies_n_ont_acces_ni_a_l_horloge_ni_a_l_aleatoire():
    """Prérequis du test-oracle de déterminisme (spec §2.7.5).

    « Deux exécutions identiques produisent des résultats identiques au bit
    près. » Une lecture de l'horloge murale ou un tirage non graine dans une
    stratégie suffit à le casser — et la casse est intermittente, donc mise sur
    le compte de la malchance pendant des semaines.
    """
    interdits = {"time", "random", "datetime", "secrets", "socket", "requests"}
    for path, sub, tree in modules():
        if sub != "strategies":
            continue
        for node in ast.walk(tree):
            for racine in import_roots(node):
                assert racine not in interdits, (
                    f"{path.relative_to(ROOT)} importe '{racine}'. Une stratégie "
                    f"est une fonction pure de sa MarketView : l'heure vient de "
                    f"view.now_ms, jamais de l'horloge murale."
                )


def test_aucune_strategie_n_utilise_le_zigzag_repeignant():
    """Spec §2.1 : la version repeignante est un instrument de MESURE.

    Elle doit exister — c'est elle qui chiffre l'illusion en comparaison de la
    version honnête — mais elle ne doit jamais alimenter une décision. Un
    import depuis `strategies/` ou `live/` signifie que la stratégie lit
    l'avenir, et rien dans les résultats ne le dirait : ils seraient
    simplement excellents.
    """
    for path, sub, tree in modules():
        if sub not in {"strategies", "live"}:
            continue
        for node in ast.walk(tree):
            noms = set()
            if isinstance(node, ast.ImportFrom):
                noms = {a.name for a in node.names}
            elif isinstance(node, ast.Attribute):
                noms = {node.attr}
            assert "zigzag_repeignant" not in noms, (
                f"{path.relative_to(ROOT)} utilise zigzag_repeignant. Cette "
                f"fonction connaît l'avenir : elle ne sert qu'à mesurer l'écart "
                f"avec la version honnête, jamais à décider."
            )


#: Les tables que le §1.2 protège : la donnée COLLECTÉE, qu'aucune exécution ne
#: reconstitue. `operateurs` et `etat_broker` n'en font pas partie — ce sont des
#: états d'exploitation, refaits en une commande, et les modifier est une
#: opération normale : révoquer un accès EXIGE un DELETE.
TABLES_PROTEGEES = ("ticks", "candles", "payouts", "uptime")

DESTRUCTIF = re.compile(
    r"\bDROP\s+TABLE\b"
    r"|\bDROP\s+DATABASE\b"
    r"|\bTRUNCATE\b"
    r"|\bDELETE\s+FROM\s+(?:" + "|".join(TABLES_PROTEGEES) + r")\b"
    r"\s*(?![\w\s]*\bWHERE\b)",
    re.IGNORECASE,
)


def test_aucune_instruction_destructrice_dans_le_chemin_normal():
    """Spec §1.2 : `reset_db.py` est le seul fichier autorisé à détruire.

    Ces instructions ne sont pas dangereuses parce qu'on les exécute par
    erreur ; elles le sont parce qu'elles s'exécutent au DÉMARRAGE, sur un
    chemin que plus personne ne relit.

    Note pour plus tard : le §1.3 autorise la migration « copie table →
    nouvelle table → renommage », qui a besoin d'un `DROP TABLE` sur la table
    provisoire. Le jour où une telle migration devient nécessaire, ce test
    devra être assoupli explicitement, pour ce fichier de migration seulement —
    et cette exception se relira. C'est le but.
    """
    fichiers = sorted(PKG.rglob("*.py")) + sorted(ROOT.glob("*.py"))
    for path in fichiers:
        if path.name == "reset_db.py":
            continue
        trouve = DESTRUCTIF.search(path.read_text(encoding="utf-8"))
        assert not trouve, (
            f"{path.relative_to(ROOT)} contient une instruction destructrice "
            f"({trouve.group(0)!r}). Elle n'a le droit d'exister que dans "
            f"reset_db.py, jamais importé par le reste du projet (spec §1.2)."
        )


def test_le_backtest_ne_peut_pas_ecrire_dans_les_tables_de_marche():
    """Spec §0 : « Backtest — ne fait jamais : écrire dans les tables de marché. »

    La garantie de fond est le descripteur `mode=ro` de `store.open_read_only`.
    Ce test ajoute la ceinture : ni `MarketWriter` ni `open_read_write` ne
    doivent apparaître dans les imports du backtest. Sans cela, la violation
    resterait possible et ne se verrait qu'à l'exécution, sur une base de
    production.
    """
    interdits = {"MarketWriter", "open_read_write", "apply_migrations"}
    for path, sub, tree in modules():
        if sub != "backtest":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                noms = {a.name for a in node.names} & interdits
                assert not noms, (
                    f"{path.relative_to(ROOT)} importe {sorted(noms)}. Le "
                    f"backtest lit les tables de marché, il n'y écrit jamais."
                )


def test_la_regle_destructrice_protege_toujours_les_tables_de_marche():
    """L'assouplissement ne doit pas avoir ouvert la porte en grand.

    Precise le 2026-09-11 pour laisser `DELETE FROM operateurs` -- revoquer un
    acces l'exige. Les tables de marche, elles, restent intouchables.
    """
    for table in TABLES_PROTEGEES:
        assert DESTRUCTIF.search(f'conn.execute("DELETE FROM {table}")'), (
            f"{table} n'est plus protegee contre un DELETE sans WHERE"
        )
    assert DESTRUCTIF.search('conn.execute("DROP TABLE ticks")')
    assert DESTRUCTIF.search('conn.execute("TRUNCATE candles")')
    # Et ce qui doit passer.
    assert not DESTRUCTIF.search('conn.execute("DELETE FROM operateurs")')
    assert not DESTRUCTIF.search(
        'conn.execute("DELETE FROM ticks WHERE ts_ms < ?")')
