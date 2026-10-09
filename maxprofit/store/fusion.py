"""
Fusionner une base dans une autre qui contient déjà des données.

Le besoin : exporter la base Neon en cours et la verser dans l'ancienne, qui a
elle-même un historique. Un `pg_dump` / `pg_restore` ne convient pas — il
recrée des lignes, et la première clé déjà présente fait échouer la
restauration. Il faut FUSIONNER : ajouter ce qui manque, garder ce qui existe.

--- Les règles, table par table ---------------------------------------------

Rien n'est jamais effacé dans la base cible. Une ligne déjà présente y reste,
sauf dans deux cas où la source est objectivement plus à jour :

  plan_etat    la version dont `maj_ts_sec` est la plus récente l'emporte :
               c'est l'état d'une campagne, et la plus récente est la vraie.
  tick_paths   le chemin le plus complet (`n_ticks`) l'emporte : une minute
               partiellement observée d'un côté peut l'être entièrement de
               l'autre.

`executions` a une clé auto-incrémentée : les deux bases ont chacune leur
« ordre n°12 », et ce ne sont pas les mêmes ordres. L'identifiant n'est donc
pas copié — la cible en attribue un — et le dédoublonnage se fait sur ce qui
identifie réellement un ordre : campagne, paire, sens, instant du signal.

Ne sont PAS copiées :

  _schema_version   les migrations de la cible en décident ;
  etat_broker       l'état de pénalité du broker — le recopier ferait hériter
                    la base cible d'une attente qui ne la concerne pas ;
  ticks             facultatifs, volumineux, et déjà résumés par `tick_paths`.

Les colonnes copiées sont celles que les DEUX tables possèdent : une base plus
ancienne peut avoir un schéma en retard, et l'on ne veut ni planter ni inventer
une valeur.

--- Pourquoi un mode « à blanc » par défaut ----------------------------------

Une fusion écrit dans une base qui a déjà de la valeur. On veut voir, avant,
combien de lignes vont entrer et combien vont être remplacées — et pouvoir
s'arrêter si les chiffres surprennent.
"""

from __future__ import annotations

from dataclasses import dataclass

from maxprofit.store import postgres
from maxprofit.store.market import _inserer_en_lot

#: Tables jamais recopiées, et pourquoi (voir le docstring du module).
EXCLUES = frozenset({"_schema_version", "etat_broker", "ticks"})

#: Tables où une ligne de la source REMPLACE celle de la cible si elle est
#: « plus » sur cette colonne. Ailleurs, la cible garde la sienne.
REMPLACER_SI_PLUS = {
    "plan_etat": "maj_ts_sec",
    "tick_paths": "n_ticks",
}

#: Clé de dédoublonnage des ordres. L'`id` ne vaut rien d'une base à l'autre.
CLE_EXECUTIONS = ("campagne", "pair", "sens", "signal_ts_ms")

#: Ordre de copie. `plan_etat` en dernier : c'est un résumé de ce qui précède.
ORDRE = ("candles", "payouts", "tick_paths", "uptime", "operateurs",
         "executions", "plan_etat")


@dataclass
class Bilan:
    table: str
    source: int = 0
    deja: int = 0
    a_inserer: int = 0
    a_remplacer: int = 0
    note: str = ""


# --- l'introspection, des deux dialectes ---------------------------------

def _tables(conn) -> set[str]:
    if postgres.est_postgres(conn):
        sql = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    else:
        sql = "SELECT name FROM sqlite_master WHERE type = 'table'"
    return {r[0] for r in conn.execute(sql).fetchall()}


def _colonnes(conn, table: str) -> list[str]:
    if postgres.est_postgres(conn):
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ? "
            "ORDER BY ordinal_position", (table,)).fetchall()
        return [r[0] for r in rows]
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _cle_primaire(conn, table: str) -> list[str]:
    if postgres.est_postgres(conn):
        rows = conn.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = ?::regclass AND i.indisprimary",
            (table,)).fetchall()
        return [r[0] for r in rows]
    infos = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in sorted(infos, key=lambda r: r[5]) if r[5] > 0]


def _normaliser(valeur):
    """PostgreSQL rend un BYTEA en `memoryview` ; une clé doit être hachable,
    et un paramètre d'insertion le préfère en `bytes`."""
    return bytes(valeur) if isinstance(valeur, memoryview) else valeur


def _lire(conn, table: str, colonnes: list[str]) -> list[tuple]:
    sql = f"SELECT {', '.join(colonnes)} FROM {table}"
    return [tuple(_normaliser(v) for v in r)
            for r in conn.execute(sql).fetchall()]


# --- la fusion ------------------------------------------------------------

def fusionner(source, cible, *, appliquer: bool = False,
              journal=print) -> list[Bilan]:
    """Verse `source` dans `cible`. À blanc tant que `appliquer` est faux.

    Rend un bilan par table : lignes à la source, déjà présentes à la cible,
    à insérer, à remplacer. Les chiffres valent pour les deux modes — à blanc,
    ils disent ce qui se produira ; appliqués, ce qui s'est produit.
    """
    tables_source, tables_cible = _tables(source), _tables(cible)
    bilans: list[Bilan] = []
    for table in ORDRE:
        if table in EXCLUES:
            continue
        if table not in tables_source:
            continue
        if table not in tables_cible:
            bilans.append(Bilan(table, note="absente de la cible : ignorée"))
            continue
        bilans.append(_fusionner_table(source, cible, table, appliquer))
        journal(_ligne(bilans[-1], appliquer))
    return bilans


def _ligne(b: Bilan, appliquer: bool) -> str:
    verbe = "insérées" if appliquer else "à insérer"
    rempl = f", {b.a_remplacer} remplacée(s)" if b.a_remplacer else ""
    return (f"  {b.table:<12} source {b.source:>8}  déjà là {b.deja:>8}  "
            f"{verbe} {b.a_inserer:>8}{rempl}{('  ' + b.note) if b.note else ''}")


def _fusionner_table(source, cible, table: str, appliquer: bool) -> Bilan:
    communes = [c for c in _colonnes(source, table)
                if c in set(_colonnes(cible, table))]
    if table == "executions":
        communes = [c for c in communes if c != "id"]
        cle = [c for c in CLE_EXECUTIONS if c in communes]
    else:
        cle = [c for c in _cle_primaire(cible, table) if c in communes]
    # Sans clé connue, la ligne entière sert de clé : une table sans clé
    # primaire serait sinon dupliquée à chaque relance.
    cle = cle or communes
    lignes = _lire(source, table, communes)
    bilan = Bilan(table, source=len(lignes))

    position = [communes.index(c) for c in cle]
    existantes = {}
    critere = REMPLACER_SI_PLUS.get(table)
    colonnes_lues = cle + ([critere] if critere and critere in communes else [])
    for r in _lire(cible, table, colonnes_lues):
        existantes[tuple(r[:len(cle)])] = r[len(cle)] if len(r) > len(cle) else None

    nouvelles, plus_recentes = [], []
    i_critere = communes.index(critere) if critere in communes else None
    for ligne in lignes:
        k = tuple(ligne[i] for i in position)
        if k not in existantes:
            nouvelles.append(ligne)
            existantes[k] = ligne[i_critere] if i_critere is not None else None
            continue
        bilan.deja += 1
        if i_critere is not None:
            actuel = existantes[k]
            propose = ligne[i_critere]
            if propose is not None and (actuel is None or propose > actuel):
                plus_recentes.append(ligne)
    bilan.a_inserer = len(nouvelles)
    bilan.a_remplacer = len(plus_recentes)
    if not appliquer:
        return bilan

    entete = f"INSERT INTO {table} ({', '.join(communes)})"
    if nouvelles:
        _inserer_en_lot(cible, entete, "ON CONFLICT DO NOTHING", nouvelles)
    if plus_recentes:
        maj = ", ".join(f"{c} = excluded.{c}" for c in communes if c not in cle)
        suite = (f"ON CONFLICT ({', '.join(cle)}) DO UPDATE SET {maj} "
                 f"WHERE {table}.{critere} < excluded.{critere}")
        _inserer_en_lot(cible, entete, suite, plus_recentes)
    if hasattr(cible, "commit"):
        cible.commit()
    return bilan
