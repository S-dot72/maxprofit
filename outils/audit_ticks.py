#!/usr/bin/env python
"""
L'étape 0 des ticks hors du bot : les mêmes calculs que /ticks, en lecture
seule, sur la fenêtre qu'on lui donne.

    .venv313\\Scripts\\python.exe outils\\audit_ticks.py --jours 7
    .venv313\\Scripts\\python.exe outils\\audit_ticks.py --debut 1792800000 --fin 1793404800

`--debut` et `--fin` (secondes UTC) sont ceux qu'affiche /ticks sous
« Reproduire » : avec les mêmes données en base, l'empreinte des chemins et
les comptes sont identiques. La base est celle de DATABASE_URL (PostgreSQL),
lue dans l'environnement ou le fichier .env, ou la base SQLite locale ; la
session est ouverte en LECTURE SEULE (`SET SESSION CHARACTERISTICS AS
TRANSACTION READ ONLY` sur PostgreSQL, `mode=ro` sur SQLite) — une écriture
lèverait.

Sorties, dans `--sortie` (par défaut le dossier courant) :
  audit_ticks.json       le résultat complet, comme le garde le bot ;
  audit_ticks.txt        le rapport lisible ;
  anomalies_ticks.csv    le registre des anomalies, la plus grave d'abord ;
  signaux_ticks.csv      chaque signal : couverture et motif d'exclusion ;
  ordres_ticks.csv       chaque ordre réel face au flux.

Méthode, inventaire, codes et conventions : docs/etape0_ticks.md.

Les signaux ZoneH1 sont rejoués avec la stratégie par défaut et la garde de
calibration de la course, sur les bougies des trois jours qui précèdent la
fenêtre et de la fenêtre elle-même. ⚠ Si la course tourne avec d'autres
paramètres (STRATEGIE, réglages), la couverture des signaux peut différer de
celle du bot ; les quatre autres contrôles, non.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxprofit.core.config import charger_env_local            # noqa: E402
from maxprofit.live.ticks import (                             # noqa: E402
    fichiers, lire_battements, lire_ordres, lire_ouvertures, nouvel_audit)
from maxprofit.store.market import MarketReader                # noqa: E402

#: Historique lu avant la fenêtre pour que ZoneH1 y trouve ses zones H1.
AVANT_SIGNAUX_SEC = 3 * 86400


def ouvrir_en_lecture_seule():
    from maxprofit.store import postgres
    from maxprofit.store.db import open_read_only
    conn = open_read_only(Path("lecture"))
    if postgres.configure():
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    return conn


def auditer(conn, debut: int, fin: int, paires=None):
    """(résultat, audit) : le résultat tel que le garde le bot, et l'audit
    lui-même pour ses fiches."""
    from maxprofit.apprentissage.historique import exemples_historiques
    from maxprofit.live.plan_demo import (
        dans_la_plage_de_calibration, seuil_contre_heure)
    from maxprofit.live.protocole import commit
    from maxprofit.strategies.zone_h1 import ZoneH1

    lecteur = MarketReader(conn)
    if not paires:
        paires = [r[0] for r in conn.execute(
            "SELECT DISTINCT pair FROM tick_paths WHERE minute_sec >= ? AND "
            "minute_sec < ? ORDER BY pair", (debut, fin)).fetchall()]
    seuil = seuil_contre_heure()
    audit = nouvel_audit(debut, fin, joue=None if seuil is None else (
        lambda e: e.contexte.get("mouvement_heure", 0.0) >= -seuil))
    audit.noter_battements(lire_battements(conn, audit.debut, audit.fin))
    strategie = ZoneH1()
    for paire in paires:
        bougies = lecteur.candles(paire, 60, debut - AVANT_SIGNAUX_SEC, fin)
        signaux = exemples_historiques(
            {paire: bougies}, strategie, echeance_sec=strategie.p.expiry_sec,
            garde=lambda p, f: dans_la_plage_de_calibration(
                strategie.p.tolerance_pct, f))
        audit.ajouter_paire(paire, bougies,
                            lecteur.tick_paths(paire, audit.debut, audit.fin),
                            signaux, relire=lecteur.ticks,
                            ouvertures=lire_ouvertures(
                                conn, paire, audit.debut, audit.fin))
    audit.comparer_ordres(lire_ordres(conn), lecteur.ticks)
    return (audit.resultat(commit=commit() or "local",
                           parametres_extra={"outil": "outils/audit_ticks.py"}),
            audit)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--jours", type=int, default=7,
                    help="jours audités jusqu'à maintenant (défaut 7)")
    ap.add_argument("--debut", type=int, help="début, secondes UTC")
    ap.add_argument("--fin", type=int, help="fin, secondes UTC")
    ap.add_argument("--paires", default="",
                    help="EURUSD_otc,GBPAUD_otc… (défaut : toutes celles "
                         "qui ont des ticks dans la fenêtre)")
    ap.add_argument("--sortie", default=".", help="dossier des fichiers")
    args = ap.parse_args(argv)
    charger_env_local()
    fin = args.fin or int(time.time())
    debut = args.debut or fin - args.jours * 86400
    paires = [p.strip() for p in args.paires.split(",") if p.strip()]
    conn = ouvrir_en_lecture_seule()
    try:
        resultat, audit = auditer(conn, debut, fin, paires)
    finally:
        conn.close()
    from maxprofit.live.ticks import texte
    sortie = Path(args.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    (sortie / "audit_ticks.json").write_text(
        json.dumps(resultat, ensure_ascii=False, indent=1), encoding="utf-8")
    lisible = html.unescape(
        re.sub(r"</?(b|pre|code)>", "", texte(resultat)))
    (sortie / "audit_ticks.txt").write_text(lisible, encoding="utf-8")
    noms = ["audit_ticks.json", "audit_ticks.txt"]
    for contenu, nom, _legende in fichiers(audit):
        (sortie / nom).write_bytes(contenu)
        noms.append(nom)
    print(lisible)
    print(f"\nÉcrit dans {sortie.resolve()} : {', '.join(noms)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
