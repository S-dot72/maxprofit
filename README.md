# maxprofit

Bot de signaux évolutif. La référence est [SPEC_bot_evolutif.md](SPEC_bot_evolutif.md) :
elle définit les invariants, ce fichier ne décrit que l'état d'avancement.

## Avancement (spec §4)

| # | Module | Critère de sortie | État |
|---|---|---|---|
| 0 | Frontières et contrats (§0) | Les couches sont séparées et la séparation est testée | **fait** |
| 1 | Persistance + migrations | Le test de survie au déploiement passe | **fait** |
| 2 | Collecteur | 14 jours de données, < 5 % de bougies écartées | **branché, collecte à lancer** |
| 3 | Indicateurs sans repeint | ZigZag et fractales à latence de confirmation | à faire |
| 4 | Moteur de backtest | Les 5 tests-oracles passent | **fait** |
| 5 | Stratégie + journalisation | 400+ évaluations avec features et contrefactuels | **fait** |
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
        research.py     research.db : expériences, évaluations, contrefactuels
        backup.py       VACUUM INTO + rétention 7j/4sem (§1.4)
      indicators/   fonctions pures d'une fenêtre de bougies, sans repeint
        zigzag.py       automate causal, latence VARIABLE (§2.1)
        fractals.py     latence constante de 2 bougies
        oscillators.py  MA, Bollinger %B, stochastique, ATR à fenêtre finie
        geometry.py     corps et mèches
      strategies/   le SEUL endroit où une Strategy peut être définie
        six_conditions.py  la stratégie initiale, six hypothèses à mesurer
      backtest/     moteur : exécution réaliste, qualité, payout d'époque
        execution.py    latence, prix d'entrée/règlement, égalités, irrésolus
        engine.py       boucle, garde-fous §2.4, rapport
      hosting/      processus hébergé : collecteur + sonde HTTP
      strategies/   le SEUL endroit où une Strategy peut être définie
      collect/      enregistre ; n'analyse ni ne décide
        pocketoption.py  adaptateur broker : retraduit le silence en erreurs
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

## Deux bases de données

    market.db     collecte. Écrite par le collecteur seul. Irremplaçable :
                  quatorze jours de collecte ne se régénèrent pas.
    research.db   analyses. Écrite par le backtest. Entièrement
                  reconstructible en rejouant les backtests.

Le backtest ouvre `market.db` en lecture seule (`mode=ro`) : un bug
d'analyse ne peut pas corrompre les données de marché. `research.db` peut
être supprimée sans réfléchir ; l'autre, jamais. Son chemin est
`RESEARCH_DB_PATH`, ou par défaut `research.db` à côté de `market.db`.

Les expériences y sont **immuables** : des déclencheurs SQLite refusent
tout UPDATE et tout DELETE (§2.6). Un compteur d'hypothèses qu'on peut
nettoyer ne compte plus rien.

## Fuseau horaire

Tout ce qui est stocké est en UTC, sans exception (§5). L'heure locale est
dérivée à l'analyse, via `TIMEZONE_AFFICHAGE` (défaut :
`America/Port-au-Prince`). Haïti applique l'heure d'été — UTC−5 en hiver,
UTC−4 de mars à novembre — donc un décalage fixe serait faux la moitié de
l'année et décalerait d'une heure la segmentation horaire du §3.2. Un nom de
fuseau IANA est exigé ; « UTC-5 » est refusé.

## Source Pocket Option

Il n'existe aucune API officielle. L'adaptateur s'appuie sur
[PocketOptionAPI-v2](https://github.com/Mastaaa1987/PocketOptionAPI-v2), du
reverse-engineering maintenu par un tiers, qui peut cesser de fonctionner sans
préavis. **Compte démo dédié** : son usage viole probablement les conditions du
broker.

    pip install -e ".[pocketoption]"
    python outils/diagnostic_pocketoption.py --duree 90

Le diagnostic est à lancer UNE fois avant de collecter. Il répond aux deux
questions qui décident si la collecte sera exploitable, et qu'on ne peut pas
trancher en lisant du code :

1. **La résolution des horodatages.** Si le broker envoie des secondes
   entières, plusieurs ticks d'une même seconde s'écrasent sur la clé primaire
   `(pair, ts_ms)` et le `tick_count` des bougies est sous-évalué — le critère
   « au moins 5 ticks » du §2.4 écarterait alors des bougies valables.
2. **Le nombre de paires diffusées simultanément.** Si `change_symbol` ne garde
   qu'un symbole actif, il faut une rotation, qui divise la densité de ticks
   par le nombre de paires.

Le diagnostic affiche aussi le SSID à injecter par `POCKET_OPTION_SSID` en
hébergement : la bibliothèque sait l'obtenir en ouvrant une fenêtre de
connexion, ce qui n'a aucun sens dans un conteneur.

L'essentiel de l'adaptateur consiste à **retraduire le silence en exceptions**.
La bibliothèque avale toutes ses erreurs (`except: return None`) ; un collecteur
bâti dessus tel quel tournerait des jours sans rien enregistrer et sans une
seule erreur dans les logs.

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

`tests/test_oracles.py` porte les cinq tests-oracles du §2.7, critère de
sortie de l'étape 4. `tests/test_oracles_detectent.py` casse le moteur de
trois façons et montre quel oracle attrape quoi — deux des trois fautes
échappent à l'oracle des entrées aléatoires, ce qui est la meilleure raison
d'en avoir cinq.

`tests/test_causalite.py` vérifie qu'aucun indicateur ne repeint : un pivot
confirmé ne change plus jamais, aucun pivot n'est visible avant sa
confirmation, et la valeur d'un indicateur ne dépend pas de la longueur de
l'historique qu'on lui passe. Deux témoins négatifs prouvent que ces tests
détectent réellement quelque chose : `zigzag_repeignant` doit y échouer, et
un ATR de Wilder aussi.
