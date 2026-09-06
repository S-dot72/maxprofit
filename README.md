# maxprofit

Bot de signaux évolutif. La référence est [SPEC_bot_evolutif.md](SPEC_bot_evolutif.md) :
elle définit les invariants, ce fichier ne décrit que l'état d'avancement.

## Avancement (spec §4)

| # | Module | Critère de sortie | État |
|---|---|---|---|
| 0 | Frontières et contrats (§0) | Les couches sont séparées et la séparation est testée | **fait** |
| 1 | Persistance + migrations | Le test de survie au déploiement passe | **fait** |
| 2 | Collecteur | 14 jours de données, < 5 % de bougies écartées | **prêt à héberger** |
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
      store/        persistance : migrations, sauvegardes, lecture seule
        migrations.py   versionnées, en avant seulement (§1.3)
        db.py           PRAGMA user_version, mode=ro pour le backtest
        market.py       MarketWriter (collecte) / MarketReader (backtest)
        backup.py       VACUUM INTO + rétention 7j/4sem (§1.4)
      hosting/      processus hébergé : collecteur + sonde HTTP
      strategies/   le SEUL endroit où une Strategy peut être définie
      collect/      enregistre ; n'analyse ni ne décide
      backtest/     rejoue et mesure ; n'écrit pas dans les tables de marché
      live/         émet ; ne contient pas sa copie de la stratégie
    tests/

## Lancer

    python -m venv .venv
    .venv/Scripts/python -m pip install -e ".[dev]"     # Linux : .venv/bin/python
    .venv/Scripts/python -m pytest

Collecteur seul (source simulée, sans compte ni réseau) :

    export TRADING_DB_PATH=/chemin/absolu/trading_data/market.db
    .venv/Scripts/python -m maxprofit.collect.collector --source sim --min-payout 92

Processus hébergé (collecteur + sonde HTTP) :

    export TRADING_DB_PATH=/chemin/absolu/trading_data/market.db
    export MIN_PAYOUT_PCT=92
    .venv/Scripts/python -m maxprofit.hosting.service --source sim

## Hébergement

`Dockerfile`, `Procfile` et `render.yaml` déploient le COLLECTEUR — c'est
lui qui a besoin de tourner 24 h/24 pour l'étape 2. Le bot Telegram est
l'étape 8, conditionnée par une étape 7 concluante.

Deux routes :

- `/ping` — vivacité du processus. C'est celle à donner au health check de
  l'hébergeur.
- `/health` — santé réelle de la collecte : **503** si le dernier battement
  de coeur en base date de plus de 180 s. À brancher sur un moniteur externe
  (UptimeRobot, cron-job.org), qui maintient aussi l'instance éveillée sur
  les offres gratuites.

**Le disque doit être persistant.** `TRADING_DB_PATH` doit pointer vers un
volume monté (Render Disk, Railway Volume, Fly Volume). Sur un système de
fichiers de conteneur ordinaire, la base est effacée à chaque déploiement —
exactement le problème contre lequel toute la §1 de la spec est écrite. Le
service émet un avertissement au démarrage si la base se trouve dans le
répertoire de code.

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

`tests/test_persistance.py` porte le critère d'acceptation du §1 : une base
peuplée de 100 lignes survit à un déploiement qui ajoute une migration. Il
couvre aussi le rollback (code plus vieux que la base), la migration qui
échoue à mi-parcours, et la restauration d'une sauvegarde.

`tests/test_hosting.py` vérifie que la sonde ne ment pas : une collecte
arrêtée doit produire un 503, jamais un `status: ok`.
