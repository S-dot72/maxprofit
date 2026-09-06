# maxprofit

Bot de signaux évolutif. La référence est [SPEC_bot_evolutif.md](SPEC_bot_evolutif.md) :
elle définit les invariants, ce fichier ne décrit que l'état d'avancement.

## Avancement (spec §4)

| # | Module | Critère de sortie | État |
|---|---|---|---|
| 0 | Frontières et contrats (§0) | Les couches sont séparées et la séparation est testée | **fait** |
| 1 | Persistance + migrations | Le test de survie au déploiement passe | à faire |
| 2 | Collecteur | 14 jours de données, < 5 % de bougies écartées | ébauche |
| 3 | Indicateurs sans repeint | ZigZag et fractales à latence de confirmation | à faire |
| 4 | Moteur de backtest | Les 5 tests-oracles passent | à faire |
| 5 | Stratégie + journalisation | 400+ évaluations avec features et contrefactuels | à faire |
| 6 | Analyse d'attribution | Rapport produit ; décision go/no-go honnête | à faire |
| 7 | Modèle + walk-forward | Avantage hors échantillon sur 3 fenêtres | à faire |
| 8 | Bot Telegram | Uniquement si 7 est concluant | à faire |
| 9 | Shadow mode en démo | 200 trades papier, écart backtest/live < 3 pts | à faire |

## Structure

    maxprofit/
      core/         contrats partagés — n'importe aucune autre couche
        errors.py       échouer bruyamment plutôt que se replier
        timebase.py     convention _ms / _sec, confusion détectée (§5)
        types.py        Tick, Candle, PairInfo, Signal, Direction
        market_view.py  garde-fou structurel anti-look-ahead (§2.1)
        strategy.py     Strategy.on_bar(view) -> Signal | None (§0)
        config.py       aucune valeur par défaut silencieuse (§5)
      strategies/   le SEUL endroit où une Strategy peut être définie
      collect/      enregistre ; n'analyse ni ne décide
      backtest/     rejoue et mesure ; n'écrit pas dans les tables de marché
      live/         émet ; ne contient pas sa copie de la stratégie
    tests/

## Lancer

    python -m venv .venv
    .venv/Scripts/python -m pip install -e ".[dev]"     # Linux : .venv/bin/python
    .venv/Scripts/python -m pytest

Collecteur (source simulée, sans compte ni réseau) :

    .venv/Scripts/python -m maxprofit.collect.collector --source sim --db /chemin/market.db

## Ce que les tests protègent

`tests/test_layering.py` analyse le code par AST à chaque exécution et refuse :

- qu'une couche importe une couche qu'elle n'a pas le droit de connaître ;
- qu'une sous-classe de `Strategy` existe hors de `maxprofit/strategies/`
  (invariant n°1 : le backtest et le live exécutent le même objet) ;
- qu'une stratégie importe `time`, `random` ou `datetime` (déterminisme, §2.7.5) ;
- qu'une instruction destructrice (`DROP TABLE`, `DELETE FROM` sans `WHERE`,
  `TRUNCATE`) apparaisse ailleurs que dans `reset_db.py` (§1.2) ;
- qu'un nouveau sous-paquet apparaisse sans que ses droits soient déclarés ;
- qu'une dépendance tierce entre dans le noyau.

Chacune de ces règles a été vérifiée en injectant la violation correspondante et
en constatant l'échec du test.
