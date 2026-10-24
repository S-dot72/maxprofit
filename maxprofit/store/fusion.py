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

--- Ne verser qu'une partie, ou imposer la source ---------------------------

`tables` limite la fusion à quelques tables : rattraper `reglages` et
`plan_etat` ne demande pas de relire un million de bougies sur une base
gratuite au quota presque épuisé. `forcer` fait gagner la SOURCE même si la
ligne de la cible est plus récente — pour le cas où la cible a reçu, entre-
temps, une configuration écrite par erreur (un /arreter tapé sur la nouvelle
base avant l'arrivée de la vraie, par exemple). Le bilan de `reglages` dit
quelle campagne chaque base porte : on le lit à blanc avant de choisir.

--- Pourquoi un mode « à blanc » par défaut ----------------------------------

Une fusion écrit dans une base qui a déjà de la valeur. On veut voir, avant,
combien de lignes vont entrer et combien vont être remplacées — et pouvoir
s'arrêter si les chiffres surprennent.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Sequence

from maxprofit.core.errors import BotError
from maxprofit.store import postgres
from maxprofit.store.market import _inserer_en_lot

#: Tables jamais recopiées, et pourquoi (voir le docstring du module).
EXCLUES = frozenset({"_schema_version", "etat_broker", "ticks"})

#: Tables où une ligne de la source REMPLACE celle de la cible si elle est
#: « plus » sur cette colonne. Ailleurs, la cible garde la sienne.
REMPLACER_SI_PLUS = {
    "plan_etat": "maj_ts_sec",
    "tick_paths": "n_ticks",
    "reglages": "maj_ts_sec",
}

#: Clé de dédoublonnage des ordres. L'`id` ne vaut rien d'une base à l'autre.
CLE_EXECUTIONS = ("campagne", "pair", "sens", "signal_ts_ms")

#: Ordre de copie. Les petites tables de la CAMPAGNE d'abord, les grosses
#: séries ensuite. Le 24/10, une fusion floral-morning → autumn a passé des
#: heures sur les bougies et la base source a atteint son quota avant d'arriver
#: aux ordres : autumn a reçu 450 000 bougies, et pas un ordre ni l'état de la
#: campagne — la seule chose qui ne se recollecte pas. Entre elles, `plan_etat`
#: et `reglages` restent après `executions` : ce sont des résumés des ordres,
#: et c'est `reglages` qui fait basculer la course sur la campagne.
#:
#: ⚠ `reglages` a été ajoutée APRÈS l'écriture de cette fusion, et l'oubli
#: n'était pas anodin : c'est elle qui dit quelle campagne la course joue. Une
#: base reçue sans elle fait retomber la course sur le nom par défaut
#: `plan-demo-v2` — exactement la collision que la fusion existe pour éviter.
#: `test_toute_table_de_la_base_est_soit_copiee_soit_exclue` empêche qu'une
#: prochaine table soit oubliée de la même façon.
ORDRE = ("operateurs", "executions", "plan_etat", "reglages",
         "candles", "payouts", "tick_paths", "uptime")


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
        # `sqlite_sequence` & co : tables internes du moteur, pas des données.
        sql = ("SELECT name FROM sqlite_master WHERE type = 'table' "
               "AND name NOT LIKE 'sqlite_%'")
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

#: Tentatives par table avant d'abandonner, et attente entre deux.
REESSAIS_PAR_TABLE = 4
ATTENTE_REESSAI_SEC = (5, 20, 60)


def fusionner(source, cible, *, appliquer: bool = False,
              journal=print, attendre=None,
              tables: Sequence[str] | None = None,
              forcer: Sequence[str] = ()) -> list[Bilan]:
    """Verse `source` dans `cible`. À blanc tant que `appliquer` est faux.

    Rend un bilan par table : lignes à la source, déjà présentes à la cible,
    à insérer, à remplacer. Les chiffres valent pour les deux modes — à blanc,
    ils disent ce qui se produira ; appliqués, ce qui s'est produit.
    `tables` : seulement celles-ci ; `forcer` : la source l'emporte sur ces
    tables (parmi celles où une ligne peut être remplacée).
    """
    inconnues = sorted(set(tables or ()) - set(ORDRE))
    if inconnues:
        raise BotError(f"Tables inconnues : {', '.join(inconnues)} "
                       f"(fusionnables : {', '.join(ORDRE)}).")
    interdites = sorted(set(forcer) - set(REMPLACER_SI_PLUS))
    if interdites:
        raise BotError(f"--forcer ne vaut que pour "
                       f"{', '.join(REMPLACER_SI_PLUS)} : "
                       f"{', '.join(interdites)} refusée(s).")
    tables_source, tables_cible = _tables(source), _tables(cible)
    bilans: list[Bilan] = []
    for table in ORDRE:
        if table in EXCLUES:
            continue
        if tables is not None and table not in tables:
            continue
        if table not in tables_source:
            continue
        if table not in tables_cible:
            bilans.append(Bilan(table, note="absente de la cible : ignorée"))
            continue
        avant = None
        if table == "reglages":
            avant = (_configuration_lisible(source),
                     _configuration_lisible(cible))
        bilans.append(_avec_reessais(
            lambda t=table: _fusionner_table(source, cible, t, appliquer,
                                             forcer=t in forcer),
            table, journal, attendre))
        if avant is not None:
            bilans[-1].note = (f"configuration : source {avant[0]} ; cible "
                               f"{avant[1]}")
        journal(_ligne(bilans[-1], appliquer))
    return bilans


def _configuration_lisible(conn) -> str:
    """La campagne que porte la configuration d'une base, et sa date."""
    try:
        ligne = conn.execute("SELECT valeur, maj_ts_sec FROM reglages "
                             "WHERE cle = 'configuration'").fetchone()
    except Exception:                            # noqa: BLE001
        return "illisible"
    if ligne is None:
        return "aucune"
    try:
        valeur = json.loads(ligne[0])
    except ValueError:
        return "illisible"
    etat = "lancée" if valeur.get("lancee") else "arrêtée"
    quand = time.strftime("%d/%m %H:%M", time.gmtime(int(ligne[1] or 0)))
    return f"{valeur.get('campagne') or '—'} ({etat}, écrite le {quand} UTC)"


def _avec_reessais(faire, table: str, journal, attendre):
    """Rejoue une table qui a échoué, au lieu d'abandonner toute la fusion.

    ⚠ Mesuré : une fusion d'un million de lignes vers Neon a échoué au bout de
    quelques minutes sur « failed to resolve host » — une coupure DNS
    passagère, côté poste. Toute la fusion s'arrêtait, et la relancer relisait
    la base source en entier : de quoi consommer le quota d'une base gratuite
    déjà presque épuisée.

    Rejouer est sans risque parce que la fusion est idempotente : les lignes
    déjà versées sont reconnues à leur clé et ne sont pas réinsérées. On ne
    rejoue que la table qui a cédé.
    """
    import time as _t
    attendre = attendre or _t.sleep
    for essai in range(1, REESSAIS_PAR_TABLE + 1):
        try:
            return faire()
        except Exception as erreur:              # noqa: BLE001
            if essai == REESSAIS_PAR_TABLE:
                raise
            pause = ATTENTE_REESSAI_SEC[min(essai - 1,
                                             len(ATTENTE_REESSAI_SEC) - 1)]
            journal(f"  {table:<12} échec ({type(erreur).__name__}: "
                    f"{str(erreur).splitlines()[0][:90]}) — nouvel essai "
                    f"{essai + 1}/{REESSAIS_PAR_TABLE} dans {pause} s")
            attendre(pause)


def _ligne(b: Bilan, appliquer: bool) -> str:
    verbe = "insérées" if appliquer else "à insérer"
    rempl = f", {b.a_remplacer} remplacée(s)" if b.a_remplacer else ""
    return (f"  {b.table:<12} source {b.source:>8}  déjà là {b.deja:>8}  "
            f"{verbe} {b.a_inserer:>8}{rempl}{('  ' + b.note) if b.note else ''}")


def _fusionner_table(source, cible, table: str, appliquer: bool, *,
                     forcer: bool = False) -> Bilan:
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
        if forcer:
            plus_recentes.append(ligne)
        elif i_critere is not None:
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
        suite = f"ON CONFLICT ({', '.join(cle)}) DO UPDATE SET {maj}"
        if not forcer:
            suite += f" WHERE {table}.{critere} < excluded.{critere}"
        _inserer_en_lot(cible, entete, suite, plus_recentes)
    if hasattr(cible, "commit"):
        cible.commit()
    return bilan
