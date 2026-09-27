"""
Contrôle et réparation du schéma PostgreSQL, à chaque ouverture en écriture.

⚠ LES MIGRATIONS NE REGARDENT QUE LE NUMÉRO DE VERSION.

Une base réimportée depuis un dump porte le numéro 9 et passe donc pour à
jour, quels que soient les types et les contraintes que le dump lui a donnés.
C'est arrivé en production le 2026-09-27 : `candles.open` en `json`, et plus
aucune clé primaire. Chaque écriture de bougies échouait (« column "open" is
of type json », puis « no unique or exclusion constraint matching the ON
CONFLICT specification »), et les prix relus revenaient en texte — d'où
« unsupported operand type(s) for -: 'str' and 'str' » dans la course.

Ici, on compare donc la base à ce que le code attend, colonne par colonne, et
l'on répare ce qui s'en écarte. Rien n'est supprimé hormis des lignes en
double ou sans clé, qui empêcheraient de rétablir la clé primaire.

Les types sont comparés en Python et non en SQL : `dialecte.vers_postgres`
réécrit les mots des types SQLite partout, y compris dans les littéraux.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_T = "text"
_E = "bigint"
_R = "double precision"
_O = "bytea"

#: table -> (colonnes et leur type attendu, clé primaire). Reflète les
#: migrations 1 à 9 telles que `dialecte.vers_postgres` les traduit.
SCHEMA_ATTENDU: dict[str, tuple[dict[str, str], tuple[str, ...]]] = {
    "ticks": ({"pair": _T, "ts_ms": _E, "price": _R}, ("pair", "ts_ms")),
    "candles": ({"pair": _T, "tf_sec": _E, "ts_sec": _E, "open": _R,
                 "high": _R, "low": _R, "close": _R, "tick_count": _E,
                 "complete": _E}, ("pair", "tf_sec", "ts_sec")),
    "payouts": ({"ts_sec": _E, "pair": _T, "payout_pct": _E, "is_open": _E},
                ("ts_sec", "pair")),
    "uptime": ({"ts_sec": _E, "n_pairs": _E}, ("ts_sec",)),
    "etat_broker": ({"id": _E, "echecs_consecutifs": _E,
                     "dernier_echec_ts_sec": _E, "dernier_succes_ts_sec": _E,
                     "derniere_raison": _T}, ("id",)),
    "operateurs": ({"chat_id": _T, "role": _T, "nom": _T,
                    "inscrit_ts_sec": _E}, ("chat_id",)),
    "tick_paths": ({"pair": _T, "minute_sec": _E, "n_ticks": _E,
                    "echelle": _E, "chemin": _O}, ("pair", "minute_sec")),
    "executions": ({"id": _E, "campagne": _T, "pair": _T, "sens": _T,
                    "mise": _R, "signal_ts_ms": _E, "clic_ts_ms": _E,
                    "accepte_ts_ms": _E, "prix_attendu": _R, "prix_entree": _R,
                    "prix_sortie": _R, "payout_flux_pct": _R,
                    "payout_broker_pct": _R, "expiration_sec": _E,
                    "ouverture_ts_ms": _E, "expiration_ts_ms": _E,
                    "decalage_broker_ms": _E, "accepte": _E, "refus": _T,
                    "resultat": _T, "profit": _R, "order_id": _T,
                    "brut": _T}, ("id",)),
    "plan_etat": ({"campagne": _T, "maj_ts_sec": _E, "jour": _E, "solde": _R,
                   "solde_ouverture": _R, "sessions_jouees": _E,
                   "sessions_perdues_daffilee": _E, "jour_utc": _E,
                   "reancrages": _T, "derniere_bougie": _T,
                   "session_pas_joues": _E, "session_engagees": _T,
                   "session_gain_vise": _R, "dernier_trade_pair": _T,
                   "dernier_trade_ts_sec": _E, "solde_broker_ancre": _R,
                   "compteurs": _T, "demarre_ts": _E}, ("campagne",)),
}

_COMPATIBLES = {
    _T: {"text", "character varying", "character"},
    _R: {"double precision"},
    _O: {"bytea"},
}


def _compatible(colonne: str, actuel: str, attendu: str) -> bool:
    if attendu == _E:
        # Un `integer` 32 bits suffit à des secondes, pas à des millisecondes
        # (« integer out of range », migration 7).
        return actuel == "bigint" or (
            actuel in ("integer", "smallint") and not colonne.endswith("_ms"))
    return actuel in _COMPATIBLES[attendu]


def _conversion(colonne: str, actuel: str, attendu: str) -> str:
    c = f'"{colonne}"'
    if actuel in ("json", "jsonb"):
        # `#>> '{}'` rend la valeur en texte brut : 1.25 comme "1.25".
        source = f"({c} #>> '{{}}')"
    elif actuel == "boolean":
        source = f"(CASE WHEN {c} THEN '1' ELSE '0' END)"
    else:
        source = f"({c}::text)"
    if attendu == _T:
        return source
    if attendu == _E:
        return f"{source}::numeric::bigint"
    return f"{source}::{attendu}"


def _index(nom: str, table: str, colonnes: str):
    return (f"index {nom} recréé",
            (("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() "
              "AND indexname = ?", (nom,)),
             f"CREATE INDEX IF NOT EXISTS {nom} ON {table}({colonnes})"))


#: Ce qu'un dump peut perdre sans qu'aucune erreur ne le dise.
#:
#: La ligne unique d'`etat_broker` : sans elle, chaque `UPDATE ... WHERE id =
#: 1` ne touche aucune ligne, en silence, et le compteur d'échecs du broker
#: n'est jamais écrit. Les index : sans eux rien ne casse, mais chaque lecture
#: de payouts ou de bougies parcourt toute la table.
_ELEMENTS_INDISPENSABLES = (
    ("etat_broker : ligne unique recréée",
     (("SELECT 1 FROM etat_broker WHERE id = 1", ()),
      "INSERT INTO etat_broker (id, echecs_consecutifs) VALUES (1, 0) "
      "ON CONFLICT DO NOTHING")),
    _index("idx_ticks_ts", "ticks", "ts_ms"),
    _index("idx_candles_ts", "candles", "ts_sec"),
    _index("idx_payouts_pair", "payouts", "pair, ts_sec"),
    _index("idx_tick_paths_minute", "tick_paths", "minute_sec"),
    _index("idx_exec_campagne", "executions", "campagne"),
)


def planifier(colonnes: dict[str, dict[str, str]],
              uniques: dict[str, list[frozenset[str]]],
              defaut_id_executions: bool) -> list[tuple[str, list[str]]]:
    """Les réparations à faire, sans rien exécuter : (description, SQL)."""
    plan: list[tuple[str, list[str]]] = []
    for table, (attendues, cle) in SCHEMA_ATTENDU.items():
        reelles = colonnes.get(table)
        if not reelles:
            continue                    # table absente : les migrations la créent
        for colonne, attendu in attendues.items():
            actuel = reelles.get(colonne)
            if actuel is None or _compatible(colonne, actuel, attendu):
                continue
            if attendu == _O and actuel in _COMPATIBLES[_T]:
                continue                # voir `convertir_les_chemins`
            if attendu == _O:
                log.error("%s.%s est en %s au lieu de bytea : conversion "
                          "impossible sans connaître l'encodage, à réparer à "
                          "la main.", table, colonne, actuel)
                continue
            plan.append((
                f"{table}.{colonne} : {actuel} -> {attendu}",
                [f'ALTER TABLE {table} ALTER COLUMN "{colonne}" TYPE {attendu} '
                 f"USING {_conversion(colonne, actuel, attendu)}"]))
        if frozenset(cle) not in uniques.get(table, []):
            egal = " AND ".join(f'a."{k}" = b."{k}"' for k in cle)
            nul = " OR ".join(f'"{k}" IS NULL' for k in cle)
            liste = ", ".join(f'"{k}"' for k in cle)
            plan.append((
                f"{table} : clé primaire ({liste}) rétablie",
                [f"DELETE FROM {table} WHERE {nul}",
                 f"DELETE FROM {table} a USING {table} b "
                 f"WHERE a.ctid < b.ctid AND {egal}",
                 f"ALTER TABLE {table} ADD PRIMARY KEY ({liste})"]))
    if "executions" in colonnes and not defaut_id_executions:
        plan.append((
            "executions.id : numérotation automatique rétablie",
            ["CREATE SEQUENCE IF NOT EXISTS executions_id_seq",
             "SELECT setval('executions_id_seq', "
             "(SELECT coalesce(max(id), 0) + 1 FROM executions), false)",
             "ALTER TABLE executions ALTER COLUMN id "
             "SET DEFAULT nextval('executions_id_seq')",
             "ALTER SEQUENCE executions_id_seq OWNED BY executions.id"]))
    return plan


def _lire_la_base(conn):
    colonnes: dict[str, dict[str, str]] = {}
    defaut_id = True
    for table, colonne, type_, defaut, identite in conn.execute(
            "SELECT table_name, column_name, data_type, column_default, "
            "is_identity FROM information_schema.columns "
            "WHERE table_schema = current_schema()").fetchall():
        colonnes.setdefault(table, {})[colonne] = type_
        if table == "executions" and colonne == "id":
            defaut_id = defaut is not None or identite == "YES"
    uniques: dict[str, list[frozenset[str]]] = {}
    for table, cle in conn.execute(
            "SELECT c.relname, array_agg(a.attname::text) "
            "FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) "
            "WHERE n.nspname = current_schema() AND i.indisunique "
            "GROUP BY c.relname, i.indexrelid").fetchall():
        uniques.setdefault(table, []).append(frozenset(cle))
    return colonnes, uniques, defaut_id


def reparer_le_schema(conn) -> list[str]:
    """Aligne une base PostgreSQL sur le schéma attendu. Rend ce qui a été
    fait, échecs compris. Ne lève pas : une réparation ratée ne doit pas
    empêcher d'ouvrir une base qui marchait peut-être par ailleurs."""
    try:
        plan = planifier(*_lire_la_base(conn))
    except Exception as erreur:                          # noqa: BLE001
        log.error("Contrôle du schéma impossible : %s", erreur)
        return [f"contrôle impossible : {erreur}"]
    bilan = []
    for description, instructions in plan:
        try:
            for sql in instructions:
                conn.execute(sql)
            log.warning("Schéma réparé — %s", description)
            bilan.append(description)
        except Exception as erreur:                      # noqa: BLE001
            log.error("Réparation du schéma échouée — %s : %s",
                      description, erreur)
            bilan.append(f"ÉCHEC {description} : {erreur}")
    for description, sql in _ELEMENTS_INDISPENSABLES:
        try:
            if conn.execute(*sql[0]).fetchone() is None:
                conn.execute(sql[1])
                log.warning("Schéma réparé — %s", description)
                bilan.append(description)
        except Exception as erreur:                      # noqa: BLE001
            log.error("Réparation du schéma échouée — %s : %s",
                      description, erreur)
            bilan.append(f"ÉCHEC {description} : {erreur}")
    try:
        converti = convertir_les_chemins(conn)
    except Exception as erreur:                          # noqa: BLE001
        converti = f"ÉCHEC tick_paths.chemin : {erreur}"
        log.error("Réparation du schéma échouée — %s", converti)
    if converti:
        bilan.append(converti)
    try:
        recale = recaler_la_numerotation(conn)
    except Exception as erreur:                          # noqa: BLE001
        log.error("Numérotation des ordres non vérifiée : %s", erreur)
        recale = None
    if recale:
        log.warning("Schéma réparé — %s", recale)
        bilan.append(recale)
    return bilan


def convertir_les_chemins(conn) -> str | None:
    """`tick_paths.chemin` en texte : le rendre binaire, si c'est sûr.

    Écrire des octets dans une colonne texte ne lève pas — PostgreSQL les
    range sous leur forme hexadécimale « \\x… » —, mais ils reviennent en
    texte à la lecture et le chemin ne se décode plus. On ne convertit que
    si TOUTES les lignes ont cette forme ; sinon l'encodage est inconnu, et
    deviner détruirait des données.
    """
    ligne = conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'tick_paths' "
        "AND column_name = 'chemin'").fetchone()
    if ligne is None or ligne[0] not in _COMPATIBLES[_T]:
        return None
    illisibles = conn.execute(
        "SELECT COUNT(*) FROM tick_paths WHERE chemin IS NOT NULL "
        "AND chemin !~ '^\\\\x([0-9a-fA-F][0-9a-fA-F])*$'").fetchone()[0]
    if illisibles:
        log.error("tick_paths.chemin en texte, %d ligne(s) hors du format "
                  "hexadécimal : conversion refusée, à examiner à la main.",
                  illisibles)
        return f"ÉCHEC tick_paths.chemin : {illisibles} ligne(s) illisible(s)"
    conn.execute("ALTER TABLE tick_paths ALTER COLUMN chemin TYPE bytea "
                 "USING decode(substr(chemin, 3), 'hex')")
    log.warning("Schéma réparé — tick_paths.chemin : text -> bytea")
    return "tick_paths.chemin : text -> bytea"


def recaler_la_numerotation(conn) -> str | None:
    """Remet le compteur des ordres AU-DESSUS du plus grand id existant.

    ⚠ UN COMPTEUR PRÉSENT MAIS EN RETARD. Après le réimport, `executions`
    avait gardé ses lignes (ids 1 à 3) mais sa séquence était repartie de 1 :
    le premier ordre suivant a reçu l'id 3 et « duplicate key value violates
    unique constraint executions_pkey » a fait tomber la course — APRÈS que
    l'ordre était parti chez le broker, donc sans trace dans le journal.

    Rend la description du recalage, ou None s'il n'y avait rien à faire.
    """
    ligne = conn.execute(
        "SELECT pg_get_serial_sequence('executions', 'id')").fetchone()
    sequence = ligne[0] if ligne else None
    if not sequence:
        return None
    plus_grand = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM executions").fetchone()[0]
    dernier, appele = conn.execute(
        f"SELECT last_value, is_called FROM {sequence}").fetchone()
    prochain = dernier + 1 if appele else dernier
    if prochain > plus_grand:
        return None
    conn.execute("SELECT setval(?, ?, true)", (sequence, int(plus_grand)))
    return (f"executions.id : le compteur proposait {prochain} alors que "
            f"l'id {plus_grand} existe, recalé à {plus_grand + 1}")
