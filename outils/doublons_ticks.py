#!/usr/bin/env python
"""
Enquête doublons hors du bot : les politiques de déduplication comparées sur
les chemins déjà stockés, EN MÉMOIRE, en lecture seule.

    .venv313\\Scripts\\python.exe outils\\doublons_ticks.py --jours 7
    .venv313\\Scripts\\python.exe outils\\doublons_ticks.py --debut 1792343940 --fin 1792948740

Rien n'est écrit en base : la session est ouverte en LECTURE SEULE (comme
`outils/audit_ticks.py`), le collecteur n'est pas touché. Sorties dans
`--sortie` : doublons_ticks.json (résultat complet) et doublons_ticks.txt.

Méthode et limites : docs/enquete_doublons.md. ⚠ La politique
« expérimentale » (instant, prix) est une heuristique de comparaison, pas une
preuve : aucune donnée stockée ne distingue un tick reçu deux fois de deux
ticks légitimes identiques.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maxprofit.apprentissage.doublons import (                 # noqa: E402
    ComparaisonDoublons, texte)
from maxprofit.core.config import charger_env_local            # noqa: E402
from maxprofit.store.chemin_ticks import decoder                # noqa: E402
from maxprofit.store.market import MarketReader                # noqa: E402

from outils.audit_ticks import ouvrir_en_lecture_seule         # noqa: E402


def comparer_en_base(conn, debut: int, fin: int, paires=None) -> dict:
    lecteur = MarketReader(conn)
    if not paires:
        paires = [r[0] for r in conn.execute(
            "SELECT DISTINCT pair FROM tick_paths WHERE minute_sec >= ? AND "
            "minute_sec < ? ORDER BY pair", (debut, fin)).fetchall()]
    comparaison = ComparaisonDoublons()
    for paire in paires:
        bougies = {b.ts_sec: b for b in lecteur.candles(paire, 60, debut, fin)}
        for chemin in lecteur.tick_paths(paire, debut, fin):
            comparaison.ajouter_minute(paire, chemin.minute_sec,
                                       decoder(chemin),
                                       bougies.get(chemin.minute_sec))
    resultat = comparaison.resultat()
    resultat.update({"debut": debut, "fin": fin, "paires": list(paires)})
    return resultat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--jours", type=int, default=7)
    ap.add_argument("--debut", type=int, help="début, secondes UTC")
    ap.add_argument("--fin", type=int, help="fin, secondes UTC")
    ap.add_argument("--paires", default="")
    ap.add_argument("--sortie", default=".")
    args = ap.parse_args(argv)
    charger_env_local()
    fin = args.fin or int(time.time())
    debut = args.debut or fin - args.jours * 86400
    paires = [p.strip() for p in args.paires.split(",") if p.strip()]
    conn = ouvrir_en_lecture_seule()
    try:
        resultat = comparer_en_base(conn, debut, fin, paires)
    finally:
        conn.close()
    sortie = Path(args.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    (sortie / "doublons_ticks.json").write_text(
        json.dumps(resultat, ensure_ascii=False, indent=1), encoding="utf-8")
    lisible = texte(resultat)
    (sortie / "doublons_ticks.txt").write_text(lisible, encoding="utf-8")
    print(lisible)
    print(f"\nÉcrit dans {sortie.resolve()} : doublons_ticks.json, "
          f"doublons_ticks.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
