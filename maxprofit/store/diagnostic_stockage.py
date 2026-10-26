"""
Diagnostic du stockage, en LECTURE SEULE : où va la place, à quel rythme elle
se remplit, et ce que la table `payouts` contient de répété
(docs/etape2_stockage_diagnostic.md, docs/etape2_payouts_redondance.md).

Rien n'est écrit : des SELECT et des fonctions de catalogue seulement, sur une
connexion passée en lecture seule avant la première requête (`lecture_seule`).
Aucune compaction n'est faite ni préparée ici : les scénarios sont COMPTÉS,
jamais appliqués.

--- Ce que mesure chaque nombre ------------------------------------------------

- Taille PHYSIQUE (PostgreSQL) : `pg_relation_size` (table), `pg_indexes_size`
  (index), la table TOAST, et `pg_total_relation_size` (tout, cartes
  d'espace libre comprises). `pg_database_size` pour la base.
- Limite : `neon.max_cluster_size` quand Neon l'expose (en Mo). Sinon, elle
  est INCONNUE ici : seule la console Neon fait foi.
- Croissance : lignes par jour de données × octets physiques moyens par ligne
  de la table. C'est une ESTIMATION (la taille physique avance par pages et
  peut gonfler de lignes mortes) ; `Echantillons` donne, à côté, la croissance
  physique mesurée par le processus depuis son démarrage.
- Répétitions des payouts : une ligne est « répétée » quand son
  (payout_pct, is_open) est celui de la ligne précédente de la même paire.
  Toutes les requêtes de cette partie sont en SQL portable (SQLite ≥ 3.28 et
  PostgreSQL) : fonctions de fenêtre, division entière.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from maxprofit.core.payout import FLUX_POUR_LE_PLAFOND
from maxprofit.store import postgres

JOUR = 86400
#: Jours complets lus pour la croissance (plus la journée en cours, montrée
#: à part et jamais comptée dans les scénarios).
JOURS_CROISSANCE = 3
#: Tables dont on mesure la croissance : (table, colonne de temps, diviseur
#: pour l'avoir en secondes).
SERIES = (("tick_paths", "minute_sec", 1), ("candles", "ts_sec", 1),
          ("payouts", "ts_sec", 1), ("uptime", "ts_sec", 1),
          ("executions", "signal_ts_ms", 1000))
#: Casiers (secondes) des écarts entre deux relevés d'une même paire.
CASIERS_SEC = (60, 300, 900, 3600, 6 * 3600, JOUR)
#: Granularités des scénarios « un ancrage par période » (secondes).
ANCRAGES_SEC = {"heure": 3600, "jour": JOUR}
#: Fenêtre de /classement (collect.classement.JOURS_DE_CLASSEMENT).
JOURS_CLASSEMENT = 7


def lecture_seule(conn) -> None:
    """Toute écriture lèverait ensuite (PostgreSQL). SQLite : la connexion
    est déjà ouverte en `mode=ro` par `open_read_only`."""
    if postgres.est_postgres(conn):
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")


def _un(conn, sql: str, params: tuple = ()) -> Any:
    try:
        ligne = conn.execute(sql, params).fetchone()
    except Exception:                                    # noqa: BLE001
        return None
    return None if ligne is None else ligne[0]


# --------------------------------------------------------------------------- #
# Tailles
# --------------------------------------------------------------------------- #

def tailles(conn) -> dict:
    """Base, limite, et chaque table : table, index, TOAST, total, lignes."""
    if not postgres.est_postgres(conn):
        pages = _un(conn, "PRAGMA page_count") or 0
        page = _un(conn, "PRAGMA page_size") or 0
        return {"moteur": "sqlite", "base": int(pages) * int(page),
                "toutes_bases": None, "limite_mo": None,
                "source_limite": "inconnue (SQLite)", "taille_neon": None,
                "tables": {}}
    limite = _un(conn, "SELECT current_setting('neon.max_cluster_size', "
                       "true)")
    try:
        limite_mo = int(str(limite).strip()) if limite not in (None, "") \
            else None
    except ValueError:
        limite_mo = None
    lignes = conn.execute(
        "SELECT c.relname, pg_relation_size(c.oid), pg_indexes_size(c.oid), "
        "CASE WHEN c.reltoastrelid = 0 THEN 0 "
        "ELSE pg_total_relation_size(c.reltoastrelid) END, "
        "pg_total_relation_size(c.oid), c.reltuples "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r'").fetchall()
    # Lignes vivantes et mortes : la place des lignes mortes se récupère par
    # un nettoyage (VACUUM), sans toucher aux données.
    try:
        stats = {str(r[0]): (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT relname, n_live_tup, n_dead_tup, "
            "GREATEST(last_vacuum, last_autovacuum) "
            "FROM pg_stat_user_tables").fetchall()}
    except Exception:                                    # noqa: BLE001
        stats = {}
    tables = {}
    for nom, heap, index, toast, total, estimees in sorted(
            lignes, key=lambda x: -int(x[4] or 0)):
        tables[str(nom)] = {
            "table": int(heap or 0), "index": int(index or 0),
            "toast": int(toast or 0), "total": int(total or 0),
            "autres": max(0, int(total or 0) - int(heap or 0)
                          - int(index or 0) - int(toast or 0)),
            "lignes_estimees": (int(estimees) if estimees is not None
                                and float(estimees) >= 0 else None),
            "vivantes": stats.get(str(nom), (None,))[0],
            "mortes": stats.get(str(nom), (None, None))[1],
            "dernier_vacuum": (str(stats[str(nom)][2])[:16]
                               if stats.get(str(nom)) and stats[str(nom)][2]
                               else None)}
    return {
        "moteur": "postgresql",
        "base": int(_un(conn, "SELECT pg_database_size(current_database())")
                    or 0),
        "toutes_bases": _un(conn, "SELECT SUM(pg_database_size(datname)) "
                                  "FROM pg_database WHERE datallowconn"),
        "limite_mo": limite_mo,
        "source_limite": ("neon.max_cluster_size" if limite_mo is not None
                          else "inconnue : non exposée par le serveur"),
        "taille_neon": _un(conn, "SELECT neon.pg_cluster_size()"),
        "tables": tables,
    }


# --------------------------------------------------------------------------- #
# Croissance
# --------------------------------------------------------------------------- #

def croissance(conn, mesures: dict, maintenant: float | None = None) -> dict:
    """Lignes par jour de données sur les `JOURS_CROISSANCE` derniers jours
    complets, et l'estimation en octets (octets physiques moyens par ligne
    de chaque table). Trois scénarios : le plus bas, la moyenne et le plus
    haut des jours observés."""
    maintenant = int(maintenant if maintenant is not None else time.time())
    aujourd_hui = maintenant // JOUR * JOUR
    debut = aujourd_hui - JOURS_CROISSANCE * JOUR
    jours = [debut + k * JOUR for k in range(JOURS_CROISSANCE)]
    par_table: dict[str, dict] = {}
    for table, colonne, diviseur in SERIES:
        unite = JOUR * diviseur
        try:
            lignes = conn.execute(
                f"SELECT {colonne} / {unite}, COUNT(*) FROM {table} "
                f"WHERE {colonne} >= ? GROUP BY {colonne} / {unite}",
                (debut * diviseur,)).fetchall()
            total = int(_un(conn, f"SELECT COUNT(*) FROM {table}") or 0)
        except Exception:                                # noqa: BLE001
            continue
        comptes = {int(j) * JOUR: int(n) for j, n in lignes}
        octets_total = mesures.get("tables", {}).get(table, {}).get("total")
        par_ligne = octets_total / total if octets_total and total else None
        par_table[table] = {
            "lignes": total, "octets_par_ligne": par_ligne,
            "par_jour": {str(j): comptes.get(j, 0) for j in jours},
            "aujourd_hui": comptes.get(aujourd_hui, 0)}
    totaux = []
    for j in jours:
        octets = [v["par_jour"][str(j)] * v["octets_par_ligne"]
                  for v in par_table.values() if v["octets_par_ligne"]]
        totaux.append(sum(octets) if octets else None)
    connus = [x for x in totaux if x is not None]
    scenarios = ({"bas": min(connus), "central": sum(connus) / len(connus),
                  "haut": max(connus)} if connus else {})
    return {"jours": [str(j) for j in jours], "tables": par_table,
            "octets_par_jour": totaux, "scenarios": scenarios,
            "methode": "lignes par jour × octets physiques moyens par ligne"}


def delais(mesures: dict, scenarios: dict, limite_mo: float | None) -> dict:
    """Jours avant saturation, par scénario ; None si la limite est
    inconnue ou la croissance nulle."""
    if not limite_mo or not scenarios:
        return {}
    reste = limite_mo * 2 ** 20 - mesures.get("base", 0)
    return {nom: (reste / v if v and v > 0 else None)
            for nom, v in scenarios.items()} | {"reste_octets": reste}


class Echantillons:
    """La taille physique relevée au fil de l'eau, EN MÉMOIRE (perdue au
    redémarrage) : la croissance MESURÉE, à côté de l'estimation par les
    lignes. Rien n'est écrit en base."""

    def __init__(self, maximum: int = 200):
        self._points: deque = deque(maxlen=maximum)

    def noter(self, ts: float, mesures: dict) -> None:
        self._points.append((float(ts), int(mesures.get("base") or 0), {
            t: v["total"] for t, v in mesures.get("tables", {}).items()}))

    def croissance(self, maintenant: float) -> dict:
        """Octets gagnés sur les dernières 24, 48 et 72 h, quand le processus
        tourne depuis assez longtemps pour le dire."""
        if len(self._points) < 2:
            return {"depuis_sec": 0, "fenetres": {}}
        dernier = self._points[-1]
        sortie = {}
        for heures in (24, 48, 72):
            debut = maintenant - heures * 3600
            anciens = [p for p in self._points if p[0] <= debut + 1800]
            if not anciens:
                continue
            a = anciens[-1]
            sortie[str(heures)] = {
                "base": dernier[1] - a[1],
                "tables": {t: dernier[2].get(t, 0) - a[2].get(t, 0)
                           for t in dernier[2]},
                "duree_h": (dernier[0] - a[0]) / 3600}
        return {"depuis_sec": dernier[0] - self._points[0][0],
                "fenetres": sortie}


# --------------------------------------------------------------------------- #
# Payouts : répétitions, scénarios, reconstruction, consommateurs
# --------------------------------------------------------------------------- #

#: Chaque relevé, avec son précédent et son suivant pour la même paire.
_BASE = """
WITH s AS (
  SELECT pair, ts_sec, payout_pct, is_open,
         LAG(payout_pct) OVER w AS p_prec, LAG(is_open) OVER w AS o_prec,
         LAG(ts_sec) OVER w AS t_prec, LEAD(ts_sec) OVER w AS t_suiv
  FROM payouts WINDOW w AS (PARTITION BY pair ORDER BY ts_sec)
), f AS (
  SELECT pair, ts_sec, payout_pct, is_open, p_prec, o_prec, t_prec, t_suiv,
         CASE WHEN t_prec IS NOT NULL AND p_prec = payout_pct
                   AND o_prec = is_open THEN 1 ELSE 0 END AS repete
  FROM s
)"""


def _casier_sql(expr: str) -> str:
    morceaux = " ".join(f"WHEN {expr} <= {c} THEN {c}" for c in CASIERS_SEC)
    return f"CASE {morceaux} ELSE -1 END"


def payouts(conn, maintenant: float | None = None) -> dict:
    """Le diagnostic complet de la table `payouts`, sans rien modifier."""
    maintenant = int(maintenant if maintenant is not None else time.time())
    g = conn.execute(_BASE + """
SELECT COUNT(*), COALESCE(SUM(repete), 0), COUNT(DISTINCT pair),
       MIN(ts_sec), MAX(ts_sec),
       COALESCE(SUM(CASE WHEN t_prec IS NULL THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN repete = 0 AND t_prec IS NOT NULL
                          AND p_prec <> payout_pct AND o_prec = is_open
                         THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN repete = 0 AND t_prec IS NOT NULL
                          AND p_prec = payout_pct AND o_prec <> is_open
                         THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN repete = 0 AND t_prec IS NOT NULL
                          AND p_prec <> payout_pct AND o_prec <> is_open
                         THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN t_prec IS NOT NULL AND ts_sec - t_prec < 60
                         THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN repete = 1 AND ts_sec - t_prec < 60
                         THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN payout_pct < 0 OR payout_pct > 100
                          OR is_open NOT IN (0, 1) THEN 1 ELSE 0 END), 0)
FROM f""").fetchone()
    (total, repetes, paires, t_min, t_max, premiers, chg_payout, chg_ouv,
     chg_deux, rapproches, repetes_rapproches, invalides) = (
        int(x or 0) for x in g)
    sortie: dict[str, Any] = {
        "lignes": total, "repetes": repetes, "paires": paires,
        "periode": [t_min, t_max], "premiers": premiers,
        "changements": {"payout": chg_payout, "ouverture": chg_ouv,
                        "les_deux": chg_deux},
        # Catégories du cahier des charges (§4.2).
        "categories": {
            "1_repetes_horodatages_differents": repetes,
            # La clé primaire (ts_sec, pair) l'interdit : 0 par construction.
            "2_repetes_meme_horodatage": 0,
            "3_identiques_non_consecutifs": None,
            "4_doublons_de_cle_metier": 0,
            "4b_releves_a_moins_de_60s": rapproches,
            "4c_dont_repetes": repetes_rapproches,
            "5_validite_indeterminable": invalides},
    }
    # Identiques non consécutifs (A → B → A) : par paire, les lignes qui
    # changent la valeur moins les valeurs distinctes de la paire.
    par_paire = conn.execute(_BASE + """
SELECT pair, COUNT(*), SUM(repete), SUM(1 - repete),
       COUNT(DISTINCT payout_pct * 2 + is_open)
FROM f GROUP BY pair""").fetchall()
    non_consecutifs = sum(max(0, int(c) - int(d))
                          for _p, _n, _r, c, d in par_paire)
    sortie["categories"]["3_identiques_non_consecutifs"] = non_consecutifs
    sortie["par_paire"] = sorted(
        ([str(p), int(n), int(r)] for p, n, r, _c, _d in par_paire),
        key=lambda x: -x[2])
    sortie["par_jour"] = [
        [int(j) * JOUR, int(n), int(r)] for j, n, r in conn.execute(
            _BASE + f" SELECT ts_sec / {JOUR}, COUNT(*), SUM(repete) FROM f "
            f"GROUP BY ts_sec / {JOUR} ORDER BY 1").fetchall()]
    intervalles = conn.execute(
        _BASE + f" SELECT {_casier_sql('ts_sec - t_prec')}, repete, COUNT(*) "
        f"FROM f WHERE t_prec IS NOT NULL GROUP BY 1, 2").fetchall()
    sortie["intervalles"] = [[int(c), int(r), int(n)]
                             for c, r, n in intervalles]
    # Scénarios : points de changement stricts, puis un ancrage gardé par
    # paire et par heure / par jour (une période faite UNIQUEMENT de
    # répétitions garde sa première ligne).
    scenarios = {"changements_seuls": repetes}
    for nom, pas in ANCRAGES_SEC.items():
        ancres = int(_un(conn, _BASE + f"""
SELECT COUNT(*) FROM (SELECT pair, ts_sec / {pas} AS k, COUNT(*) AS n,
                             SUM(repete) AS r FROM f
                      GROUP BY pair, ts_sec / {pas}) x WHERE r = n""") or 0)
        scenarios[f"ancrage_par_{nom}"] = max(0, repetes - ancres)
    sortie["scenarios_lignes"] = scenarios
    sortie["reconstruction"] = _reconstruction(conn, maintenant)
    sortie["classement"] = _classement(conn, maintenant)
    sortie["rejeu"] = _rejeu(conn)
    return sortie


def _reconstruction(conn, maintenant: int) -> dict:
    """Points de changement seuls : la valeur EN VIGUEUR à chaque instant
    observé est-elle la même, et de quand date le relevé qui la porte ?"""
    v = conn.execute(_BASE + f"""
, r AS (
  SELECT pair, ts_sec, payout_pct, is_open,
         SUM(1 - repete) OVER (PARTITION BY pair ORDER BY ts_sec
                               ROWS UNBOUNDED PRECEDING) AS run
  FROM f
), v AS (
  SELECT pair, ts_sec, payout_pct, is_open,
         FIRST_VALUE(payout_pct) OVER x AS p_run,
         FIRST_VALUE(is_open) OVER x AS o_run,
         FIRST_VALUE(ts_sec) OVER x AS t_run
  FROM r WINDOW x AS (PARTITION BY pair, run ORDER BY ts_sec)
)
SELECT {_casier_sql('ts_sec - t_run')}, COUNT(*),
       SUM(CASE WHEN p_run <> payout_pct OR o_run <> is_open
                THEN 1 ELSE 0 END)
FROM v GROUP BY 1""").fetchall()
    ages = {int(c): int(n) for c, n, _d in v}
    differences = sum(int(d or 0) for _c, _n, d in v)
    etat = conn.execute(_BASE + """
SELECT pair, MAX(ts_sec), MAX(CASE WHEN repete = 0 THEN ts_sec END)
FROM f GROUP BY pair""").fetchall()
    retards = sorted(int(t) - int(c) for _p, t, c in etat
                     if t is not None and c is not None)
    return {
        "valeurs_differentes": differences,
        "age_du_releve_en_vigueur": {str(k): n for k, n in
                                     sorted(ages.items())},
        "paires": len(etat),
        "derniere_ligne_repetee": sum(1 for x in retards if x > 0),
        "recul_derniere_observation_sec": (
            [retards[len(retards) // 2], retards[-1]] if retards else None),
        "derniere_observation_age_sec": (
            maintenant - max(int(t) for _p, t, _c in etat) if etat else None),
    }


def _classement(conn, maintenant: int) -> dict:
    """Ce que /classement compte sur 7 jours — relevés et part au plafond —
    avec l'historique tel quel et en points de changement seuls, et la part
    du TEMPS au plafond, que /classement annonce."""
    depuis = maintenant - JOURS_CLASSEMENT * JOUR
    lignes = conn.execute(_BASE + """
SELECT pair, COUNT(*),
       SUM(CASE WHEN is_open = 1 AND payout_pct >= ? THEN 1 ELSE 0 END),
       SUM(1 - repete),
       SUM(CASE WHEN repete = 0 AND is_open = 1 AND payout_pct >= ?
                THEN 1 ELSE 0 END),
       SUM(CASE WHEN is_open = 1 AND payout_pct >= ?
                THEN COALESCE(t_suiv, ?) - ts_sec ELSE 0 END),
       SUM(COALESCE(t_suiv, ?) - ts_sec)
FROM f WHERE ts_sec >= ? GROUP BY pair""",
        (FLUX_POUR_LE_PLAFOND, FLUX_POUR_LE_PLAFOND, FLUX_POUR_LE_PLAFOND,
         maintenant, maintenant, depuis)).fetchall()
    return {"depuis": depuis, "plafond": FLUX_POUR_LE_PLAFOND,
            "paires": [[str(p), int(n), int(m or 0), int(c), int(cm or 0),
                        int(tp or 0), int(tt or 0)]
                       for p, n, m, c, cm, tp, tt in lignes]}


def _rejeu(conn) -> dict:
    """`outils/rejeu_plan.py` ne lit que le jour qui précède sa fenêtre :
    une valeur restée plus d'un jour sans relevé y devient « inconnue »."""
    plein = int(_un(conn, _BASE + f" SELECT COUNT(*) FROM f WHERE t_suiv "
                                  f"IS NOT NULL AND t_suiv - ts_sec > {JOUR}")
                or 0)
    changements = int(_un(conn, _BASE + f"""
, c AS (SELECT pair, ts_sec,
               LEAD(ts_sec) OVER (PARTITION BY pair ORDER BY ts_sec) AS t_n
        FROM f WHERE repete = 0)
SELECT COUNT(*) FROM c WHERE t_n IS NOT NULL AND t_n - ts_sec > {JOUR}""")
                      or 0)
    return {"ecarts_de_plus_d_un_jour": {"tel_quel": plein,
                                         "changements_seuls": changements}}
