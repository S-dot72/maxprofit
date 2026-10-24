"""
Plusieurs bases, une seule active, et la relève qui suit.

--- Le problème -------------------------------------------------------------

Une base gratuite Neon tient une dizaine de jours : la collecte écrit chaque
minute, la base ne se met jamais en veille, et le calcul gratuit s'épuise. Une
fois le quota atteint, Neon refuse TOUTE connexion — même en lecture. On ne
peut donc plus rien en extraire : fusionner « quand le quota est plein », c'est
déjà trop tard.

--- La réponse --------------------------------------------------------------

Toutes les URL `DATABASE_URL` et `DATABASE_URL_*` forment un POOL.

  au démarrage    la base active est la base JOIGNABLE la plus à jour (dernière
                  bougie la plus récente) : celle où la production écrivait.
                  Une base bloquée est écartée d'office.
  en continu      une autre base joignable sert de RELÈVE, et reçoit toutes les
                  20 minutes les lignes nouvelles de la base active.
  au quota        quand la base active refuse pour quota, le service redémarre
                  (`postgres.QUAND_QUOTA_EPUISE`) ; au démarrage suivant, la
                  base active bloquée est écartée et la relève, la plus à jour
                  des joignables, est choisie.

Ce qui peut être perdu : au plus les lignes écrites depuis la dernière
synchronisation — vingt minutes de collecte. Jamais l'état d'une campagne :
les tables du plan sont recopiées entières à chaque passe.

Aucune clé d'API n'est nécessaire : le quota n'est pas lu, il est constaté au
moment où la base refuse.

--- Ce qu'il faut côté hébergeur --------------------------------------------

Toutes les URL du pool doivent être déclarées dans l'environnement du service
(Render), pas seulement `DATABASE_URL`. Sans relève déclarée, rien ne change :
le service tourne sur `DATABASE_URL` comme avant.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from maxprofit.store import postgres
from maxprofit.store.fusion import (
    _avec_reessais, _colonnes, _cle_primaire, _fusionner_table, _lire,
    _tables)
from maxprofit.store.market import _inserer_en_lot

log = logging.getLogger(__name__)

#: Variable où le démarrage range l'URL de la relève choisie.
ENV_RELEVE = "DATABASE_URL_RELEVE"

#: Entre deux synchronisations de la relève. Vingt minutes : c'est ce qu'on
#: accepte de perdre au pire, et la relève peut dormir entre deux passes.
SYNCHRO_SEC = 1200

#: Fenêtre relue en arrière à chaque passe. Une bougie, un chemin de ticks
#: s'écrivent par morceaux pendant leur minute : relire la dernière heure
#: rattrape ce qui était incomplet à la passe précédente.
MARGE_SEC = 3600

#: Taille des tranches de lecture. Une première synchronisation recopie toute
#: une base : la lire d'un bloc tiendrait des centaines de milliers de lignes
#: en mémoire, sur un service qui n'a que 512 Mo.
TRANCHE_SEC = 86400

#: Séries temporelles : (table, colonne de temps, règle en cas de conflit).
#:   maj         la source fait foi : une bougie se complète pendant sa minute
#:   plus_grand  le chemin de ticks le plus complet l'emporte
#:   rien        une ligne écrite ne change plus
SERIES = (
    ("candles", "ts_sec", "maj"),
    ("payouts", "ts_sec", "rien"),
    ("uptime", "ts_sec", "rien"),
    ("tick_paths", "minute_sec", "plus_grand"),
)

#: Petites tables recopiées entières, la plus récente l'emportant.
PETITES = ("operateurs", "plan_etat", "reglages")

#: Les ordres changent APRÈS leur écriture (le résultat arrive au dénouement) :
#: on relit ceux des deux derniers jours et l'on met à jour ce qui a changé.
ORDRES_RECENTS_MS = 2 * 86400 * 1000
CLE_ORDRE = ("campagne", "pair", "sens", "signal_ts_ms")


# --- le pool ------------------------------------------------------------------

def bases_du_pool(env=None) -> list[tuple[str, str]]:
    """Les (nom, url) déclarées, sans doublon. `DATABASE_URL` d'abord."""
    env = os.environ if env is None else env
    noms = sorted(k for k in env
                  if k == "DATABASE_URL" or (k.startswith("DATABASE_URL_")
                                             and k != ENV_RELEVE))
    noms.sort(key=lambda k: k != "DATABASE_URL")
    vues, pool = set(), []
    for nom in noms:
        url = (env.get(nom) or "").strip()
        if url.startswith(("postgres://", "postgresql://")) and url not in vues:
            vues.add(url)
            pool.append((nom, url))
    return pool


@dataclass
class EtatBase:
    nom: str
    url: str
    joignable: bool
    quota_epuise: bool = False
    derniere_ts: int | None = None
    raison: str = ""

    @property
    def hote(self) -> str:
        return postgres._sans_secret(self.url).split(".")[0]


def sonder(nom: str, url: str, ouvrir=None) -> EtatBase:
    """Joignable ? Bloquée par quota ? Jusqu'où vont ses données ?"""
    ouvrir = ouvrir or postgres.ouvrir
    try:
        conn = ouvrir(url)
    except Exception as erreur:                  # noqa: BLE001
        texte = str(erreur)
        return EtatBase(nom, url, False, quota_epuise=est_quota(texte),
                        raison=texte.splitlines()[0][:160])
    try:
        derniere = None
        if "candles" in _tables(conn):
            ligne = conn.execute("SELECT MAX(ts_sec) FROM candles").fetchone()
            derniere = int(ligne[0]) if ligne and ligne[0] is not None else None
        return EtatBase(nom, url, True, derniere_ts=derniere)
    finally:
        try:
            conn.close()
        except Exception:                        # noqa: BLE001
            pass


def est_quota(texte: str) -> bool:
    return "exceeded the quota" in (texte or "")


def choisir(etats: list[EtatBase], actuelle: str = ""):
    """(active, relève). Chacune peut valoir `None`.

    Active : la joignable la plus à jour — c'est là que la production écrivait.
    À égalité, celle déjà désignée par `DATABASE_URL`, pour ne pas basculer
    sans raison. Relève : la plus à jour des autres joignables, pour que la
    rattraper coûte le moins possible.
    """
    joignables = [e for e in etats if e.joignable]
    if not joignables:
        return None, None
    rang = lambda e: (e.derniere_ts if e.derniere_ts is not None else -1,
                      e.url == actuelle)
    active = max(joignables, key=rang)
    autres = [e for e in joignables if e is not active]
    releve = max(autres, key=rang) if autres else None
    return active, releve


def choisir_au_demarrage(env=None, sonde=None) -> tuple[EtatBase | None,
                                                        EtatBase | None,
                                                        list[EtatBase]]:
    """Désigne la base active et la relève, et l'écrit dans l'environnement.

    Sans pool (une seule URL), ne touche à rien : le service tourne sur
    `DATABASE_URL` comme avant.
    """
    env = os.environ if env is None else env
    pool = bases_du_pool(env)
    if len(pool) < 2:
        return None, None, []
    sonde = sonde or sonder
    etats = [sonde(nom, url) for nom, url in pool]
    active, releve = choisir(etats, actuelle=env.get("DATABASE_URL", ""))
    for e in etats:
        etat = ("BLOQUÉE (quota)" if e.quota_epuise else
                "injoignable" if not e.joignable else
                f"dernière bougie {_date(e.derniere_ts)}")
        log.info("Pool : %-28s %-34s %s", e.nom, e.hote, etat)
    if active is None:
        log.error("Pool : aucune base joignable ; DATABASE_URL inchangée.")
        return None, None, etats
    if active.url != env.get("DATABASE_URL"):
        log.warning("Pool : bascule sur %s (%s), la plus à jour des bases "
                    "joignables.", active.nom, active.hote)
    env["DATABASE_URL"] = active.url
    if releve is not None:
        env[ENV_RELEVE] = releve.url
        log.info("Pool : relève = %s (%s)", releve.nom, releve.hote)
    else:
        env.pop(ENV_RELEVE, None)
        log.warning("Pool : aucune relève joignable — une base épuisée ne "
                    "pourra pas être remplacée.")
    return active, releve, etats


def _date(ts) -> str:
    return time.strftime("%d/%m %H:%M", time.gmtime(int(ts))) if ts else "aucune"


# --- la synchronisation -------------------------------------------------------

def synchroniser(source, cible, journal=log.info, attendre=None) -> dict:
    """Recopie dans `cible` ce que `source` a de nouveau. Idempotent.

    Une première passe vers une base vide recopie tout, par tranches d'un
    jour ; les suivantes ne relisent que la dernière heure et ce qui l'a suivie.
    """
    # La campagne D'ABORD : si la source tombe en cours de passe (quota), ce
    # qui ne se recollecte pas doit déjà être à l'abri. Les ordres avant leurs
    # résumés (`plan_etat`, `reglages`).
    copiees: dict[str, int] = {}
    tables_source, tables_cible = _tables(source), _tables(cible)
    if "executions" in tables_source and "executions" in tables_cible:
        copiees["executions"] = _avec_reessais(
            lambda: _synchroniser_ordres(source, cible), "executions",
            journal, attendre)
    for table in PETITES:
        if table in tables_source and table in tables_cible:
            bilan = _avec_reessais(
                lambda t=table: _fusionner_table(source, cible, t, True),
                table, journal, attendre)
            copiees[table] = bilan.a_inserer + bilan.a_remplacer
    for table, colonne, regle in SERIES:
        if table in tables_source and table in tables_cible:
            copiees[table] = _avec_reessais(
                lambda t=table, c=colonne, r=regle:
                    _synchroniser_serie(source, cible, t, c, r),
                table, journal, attendre)
    return copiees


def _synchroniser_serie(source, cible, table, colonne, regle) -> int:
    communes = [c for c in _colonnes(source, table)
                if c in set(_colonnes(cible, table))]
    cle = [c for c in _cle_primaire(cible, table) if c in communes]
    haut = source.execute(f"SELECT MAX({colonne}) FROM {table}").fetchone()[0]
    if haut is None:
        return 0
    deja = cible.execute(f"SELECT MAX({colonne}) FROM {table}").fetchone()[0]
    if deja is None:
        bas = source.execute(f"SELECT MIN({colonne}) FROM {table}").fetchone()[0]
    else:
        bas = int(deja) - MARGE_SEC
    entete = f"INSERT INTO {table} ({', '.join(communes)})"
    if regle == "rien" or not cle:
        suite = "ON CONFLICT DO NOTHING"
    else:
        autres = [c for c in communes if c not in cle]
        maj = ", ".join(f"{c} = excluded.{c}" for c in autres)
        suite = f"ON CONFLICT ({', '.join(cle)}) DO UPDATE SET {maj}"
        if regle == "plus_grand":
            suite += f" WHERE {table}.n_ticks < excluded.n_ticks"
    total = 0
    debut = int(bas)
    while debut <= int(haut):
        fin = debut + TRANCHE_SEC
        lignes = _lire_tranche(source, table, communes, colonne, debut, fin)
        if lignes:
            _inserer_en_lot(cible, entete, suite, lignes)
            total += len(lignes)
        debut = fin
    return total


def _lire_tranche(conn, table, colonnes, colonne, debut, fin) -> list[tuple]:
    sql = (f"SELECT {', '.join(colonnes)} FROM {table} "
           f"WHERE {colonne} >= ? AND {colonne} < ?")
    return [tuple(bytes(v) if isinstance(v, memoryview) else v for v in r)
            for r in conn.execute(sql, (debut, fin)).fetchall()]


def _synchroniser_ordres(source, cible) -> int:
    """Les ordres n'ont pas de clé naturelle en base, et ils CHANGENT : un ordre
    est écrit avant son départ, puis complété à son dénouement. On relit donc
    les récents, et l'on met à jour ceux que la relève a déjà."""
    communes = [c for c in _colonnes(source, "executions")
                if c in set(_colonnes(cible, "executions")) and c != "id"]
    cle = [c for c in CLE_ORDRE if c in communes]
    haut = cible.execute(
        "SELECT MAX(signal_ts_ms) FROM executions").fetchone()[0]
    depuis = 0 if haut is None else int(haut) - ORDRES_RECENTS_MS
    lignes = _lire_tranche(source, "executions", communes, "signal_ts_ms",
                           depuis, 2 ** 62)
    connus = {tuple(r) for r in _lire(cible, "executions", cle)}
    pos = [communes.index(c) for c in cle]
    nouvelles, a_maj = [], []
    for ligne in lignes:
        (a_maj if tuple(ligne[i] for i in pos) in connus else nouvelles
         ).append(ligne)
    if nouvelles:
        _inserer_en_lot(cible, f"INSERT INTO executions ({', '.join(communes)})",
                        "", nouvelles)
    mobiles = [c for c in communes if c not in cle]
    for ligne in a_maj:
        valeurs = [ligne[communes.index(c)] for c in mobiles]
        cles = [ligne[i] for i in pos]
        cible.execute(
            f"UPDATE executions SET "
            f"{', '.join(f'{c} = ?' for c in mobiles)} "
            f"WHERE {' AND '.join(f'{c} = ?' for c in cle)}",
            valeurs + cles)
    return len(nouvelles) + len(a_maj)
