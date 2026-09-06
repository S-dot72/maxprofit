#!/usr/bin/env python
"""
Diagnostic de la source Pocket Option — à lancer UNE fois, avant de collecter.

    .venv\Scripts\python.exe outils\diagnostic_pocketoption.py --duree 90
    .venv/bin/python outils/diagnostic_pocketoption.py --duree 90   # Linux/mac

Lancez-le avec le Python du VENV du projet, pas celui du système : c'est
là que la bibliothèque broker est installée. Le script refuse de tourner
autrement, et affiche la commande exacte.

Deux questions ne se tranchent pas en lisant le code de la bibliothèque, et
elles décident toutes les deux si la collecte sera exploitable. Ce script y
répond en une minute et demie de connexion réelle.

**1. Quelle est la résolution des horodatages ?**

Le §5 de la spec désigne la confusion secondes/millisecondes comme le bug le
plus fréquent de ce type de projet. L'adaptateur détecte déjà l'unité tout
seul, donc ce n'est pas le risque. Le vrai risque est plus subtil : si le
broker envoie des secondes ENTIÈRES, plusieurs ticks d'une même seconde
partagent la clé primaire `(pair, ts_ms)` et un seul survit en base. Le
`tick_count` des bougies est alors sous-évalué, et le critère de qualité du
§2.4 — « au moins 5 ticks » — écarterait des bougies parfaitement valables.
On croirait à un problème de collecte là où il n'y aurait qu'un problème de
comptage.

**2. Combien de paires sont diffusées en même temps ?**

`change_symbol` pourrait ne garder qu'un symbole actif. Il faudrait alors faire
tourner l'abonnement, ce qui diviserait la densité de ticks par le nombre de
paires — et rendrait la plupart des bougies inexploitables. Autant le savoir
avant quatorze jours de collecte qu'après.

Ce script n'écrit rien en base. Il ne fait qu'observer et conclure.
"""

from __future__ import annotations

import argparse
import logging
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))


def _verifier_interpreteur() -> None:
    """Refuse de tourner sous le mauvais Python, avec la commande exacte.

    Le piège est facile : `python outils/diagnostic_pocketoption.py` lance
    l'interpréteur du système, où rien n'est installé. L'erreur qui en résulte
    (« No module named 'pocketoptionapi' ») ressemble à un problème
    d'installation de la bibliothèque, alors que le paquet est bien là — dans
    l'autre interpréteur.
    """
    if os.name == "nt":
        attendu = RACINE / ".venv" / "Scripts" / "python.exe"
        commande = ".\\.venv\\Scripts\\python.exe outils\\diagnostic_pocketoption.py"
    else:
        attendu = RACINE / ".venv" / "bin" / "python"
        commande = ".venv/bin/python outils/diagnostic_pocketoption.py"

    if not attendu.exists():
        return  # pas de venv de projet : l'utilisateur gère son environnement

    try:
        meme = Path(sys.executable).resolve() == attendu.resolve()
    except OSError:
        return
    if meme:
        return

    print(f"Mauvais interpréteur Python.\n"
          f"  utilisé  : {sys.executable}\n"
          f"  attendu  : {attendu}\n\n"
          f"La bibliothèque broker est installée dans le venv du projet, pas "
          f"dans le Python du système. Relancez depuis {RACINE} :\n\n"
          f"    {commande} --duree 90\n", file=sys.stderr)
    raise SystemExit(2)


_verifier_interpreteur()

from maxprofit.collect.pocketoption import ENV_SSID, PocketOptionSource  # noqa: E402
from maxprofit.core.errors import BotError  # noqa: E402
from maxprofit.core.timebase import format_local_ms, format_ms  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--duree", type=int, default=90,
                    help="Durée d'observation en secondes (défaut : 90)")
    ap.add_argument("--paires", type=int, default=4,
                    help="Nombre de paires à observer simultanément")
    ap.add_argument("--min-payout", type=int, default=92)
    ap.add_argument("--reel", action="store_true",
                    help="Compte RÉEL au lieu du compte démo. Déconseillé.")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    source = PocketOptionSource(demo=not args.reel)

    print("=" * 72)
    print("DIAGNOSTIC POCKET OPTION")
    print("=" * 72)
    if not (source._ssid or os.environ.get(ENV_SSID)):
        print(f"Aucun {ENV_SSID} : une fenêtre de connexion va s'ouvrir.")
        print("Connectez-vous sur votre compte DÉMO, puis laissez tourner.")
    print()

    try:
        source.connect()
    except BotError as erreur:
        print(f"ÉCHEC DE CONNEXION : {erreur}", file=sys.stderr)
        return 2

    try:
        return _observer(source, args)
    finally:
        source.close()


def _observer(source: PocketOptionSource, args) -> int:
    # --- 1. Les paires ------------------------------------------------------
    try:
        paires = source.list_pairs()
    except BotError as erreur:
        print(f"ÉCHEC list_pairs : {erreur}", file=sys.stderr)
        return 2

    ouvertes = [p for p in paires if p.is_open]
    eligibles = sorted(
        (p for p in ouvertes if p.payout_pct >= args.min_payout),
        key=lambda p: -p.payout_pct,
    )
    print(f"Paires reçues      : {len(paires)}")
    print(f"  ouvertes         : {len(ouvertes)}")
    print(f"  payout >= {args.min_payout}%   : {len(eligibles)}")
    if not eligibles:
        print()
        print("Aucune paire éligible. Si c'est le week-end, seules les paires "
              "OTC sont ouvertes ; vérifiez le seuil de payout.")
        return 1
    for p in eligibles[:8]:
        print(f"    {p.name:<20} {p.payout_pct}%")

    cibles = [p.name for p in eligibles[:args.paires]]
    print()
    print(f"Observation de {len(cibles)} paire(s) pendant {args.duree} s : "
          f"{', '.join(cibles)}")
    print()

    # --- 2. Le flux ---------------------------------------------------------
    source.subscribe(cibles)

    par_paire: dict[str, list] = defaultdict(list)
    bruts: dict[str, list] = defaultdict(list)
    debut = time.monotonic()
    premier_affiche = False

    flux = source.stream()
    try:
        while time.monotonic() - debut < args.duree:
            tick = next(flux)
            par_paire[tick.pair].append(tick)
            bruts[tick.pair].append(tick.ts_ms)
            if not premier_affiche:
                print(f"Premier tick : {tick.pair} @ {format_ms(tick.ts_ms)}")
                print(f"               soit {format_local_ms(tick.ts_ms)} chez vous")
                print()
                premier_affiche = True
    except StopIteration:
        pass
    except BotError as erreur:
        print(f"FLUX INTERROMPU : {erreur}", file=sys.stderr)
        print("(c'est le comportement attendu : l'adaptateur lève au lieu de "
              "se taire)")

    ecoule = time.monotonic() - debut
    return _conclure(source, cibles, par_paire, bruts, ecoule)


def _conclure(source, cibles, par_paire, bruts, ecoule) -> int:
    print("=" * 72)
    print(f"RÉSULTATS APRÈS {ecoule:.0f} s")
    print("=" * 72)

    total = sum(len(v) for v in par_paire.values())
    if total == 0:
        print("AUCUN TICK REÇU.")
        print()
        print("Causes possibles, par ordre de fréquence :")
        print("  - marché fermé (week-end : seules les paires OTC cotent) ;")
        print("  - SSID expiré : reconnectez-vous pour en obtenir un neuf ;")
        print("  - `change_symbol` n'a pas pris : le format de la bibliothèque "
              "a peut-être changé.")
        return 1

    print(f"{'Paire':<20}{'Ticks':>8}{'Ticks/s':>10}{'Δt médian':>12}"
          f"{'Instants distincts':>20}")
    print("-" * 72)
    for nom in cibles:
        ticks = par_paire.get(nom, [])
        if not ticks:
            print(f"{nom:<20}{0:>8}{'-':>10}{'-':>12}{'-':>20}")
            continue
        instants = sorted(t.ts_ms for t in ticks)
        ecarts = [b - a for a, b in zip(instants, instants[1:])] or [0]
        distincts = len(set(instants))
        print(f"{nom:<20}{len(ticks):>8}{len(ticks) / ecoule:>10.2f}"
              f"{statistics.median(ecarts):>10.0f}ms"
              f"{distincts:>13} / {len(ticks)}")

    # --- Question 1 : résolution -------------------------------------------
    print()
    print("--- 1. Résolution des horodatages ---")
    print(f"Unité détectée : {source._unite}")

    tous = [ts for liste in bruts.values() for ts in liste]
    a_la_ms = any(ts % 1000 != 0 for ts in tous)
    if a_la_ms:
        print("Résolution SOUS LA SECONDE : chaque tick a un instant distinct.")
        print("→ Le tick_count des bougies sera exact. Rien à changer.")
    else:
        print("Résolution à la SECONDE ENTIÈRE.")
        collisions = sum(
            n - 1 for liste in bruts.values() for n in Counter(liste).values()
        )
        print(f"→ {collisions} tick(s) sur {len(tous)} partagent leur instant "
              f"avec un autre ({collisions / len(tous) * 100:.0f} %).")
        print("→ Ils s'écraseront sur la clé primaire (pair, ts_ms) et le "
              "tick_count sera sous-évalué d'autant.")
        print("→ ACTION : abaisser le seuil `min_ticks_par_bougie` du moteur "
              "(§2.4), ou ajouter un compteur de séquence à la clé. Me le dire "
              "et je le fais.")

    # --- Question 2 : multi-paires -----------------------------------------
    print()
    print("--- 2. Diffusion simultanée ---")
    actives = [n for n in cibles if par_paire.get(n)]
    print(f"{len(actives)} paire(s) sur {len(cibles)} ont produit des ticks.")
    if len(actives) == len(cibles):
        print("→ La diffusion multi-paires fonctionne. Le collecteur peut "
              "s'abonner à toutes les paires éligibles d'un coup.")
    elif len(actives) <= 1:
        print("→ UNE SEULE paire diffuse à la fois : `change_symbol` remplace "
              "l'abonnement au lieu de l'ajouter.")
        print("→ ACTION : il faut une rotation des abonnements, qui divisera "
              "la densité de ticks par le nombre de paires. Me le dire et je "
              "l'ajoute — avec le calcul de ce que cela coûte au §2.4.")
    else:
        print("→ Diffusion PARTIELLE. Relancez avec --duree 300 pour voir si "
              "les autres démarrent plus lentement.")

    # --- Verdict ------------------------------------------------------------
    print()
    print("--- Bougies M1 exploitables ---")
    for nom in actives:
        par_minute = len(par_paire[nom]) / (ecoule / 60)
        distincts_par_minute = (
            len(set(bruts[nom])) / (ecoule / 60) if bruts[nom] else 0
        )
        verdict = "OK" if distincts_par_minute >= 5 else "INSUFFISANT (< 5)"
        print(f"  {nom:<20} ~{par_minute:5.1f} ticks/min, "
              f"{distincts_par_minute:5.1f} instants distincts/min  -> {verdict}")

    print()
    print("Le critère du §2.4 porte sur les instants DISTINCTS, puisque c'est "
          "ce qui survit en base.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
