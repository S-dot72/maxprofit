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

    .venv\Scripts\python -m pip install -e ".[pocketoption]"
    .venv\Scripts\python outils\capturer_ssid.py            # une seule fois
    .venv\Scripts\python outils\diagnostic_pocketoption.py --duree 90

Utilisez le Python du venv, pas celui du système : c'est là que la
bibliothèque est installée. Le diagnostic refuse de tourner autrement et
affiche la commande exacte.

Le diagnostic est à lancer UNE fois avant de collecter. Il répond aux deux
questions qui décident si la collecte sera exploitable, et qu'on ne peut pas
trancher en lisant du code :

1. **La résolution des horodatages** — mesurée : sous la seconde. Chaque tick a
   un instant distinct, donc aucun écrasement sur la clé `(pair, ts_ms)` et un
   `tick_count` exact pour le §2.4.
2. **Le nombre de paires diffusées simultanément** — mesuré : toutes, à
   ~2 ticks/s chacune. Aucune rotation d'abonnement n'est nécessaire.

Il a aussi révélé un troisième point, qui n'était pas dans mes prévisions :

3. **Le broker n'envoie pas de l'UTC.** Son horloge est décalée (+2 h à la
   mesure). L'horodatage reste un epoch parfaitement plausible, simplement faux
   de deux heures, et aucune validation de type ne peut l'attraper. Comme le
   collecteur horodate `payouts` et `uptime` avec l'horloge système, en vrai
   UTC, des ticks à l'heure du broker feraient chercher pour chaque trade un
   payout relevé jusqu'à deux heures **après** — du look-ahead sur les payouts,
   exactement ce que le §2.3 interdit. Le décalage est donc mesuré, arrondi à
   l'heure entière, appliqué, et re-vérifié toutes les cinq minutes : si
   l'horloge du broker suit l'heure d'été européenne, elle passera de +2 h à
   +1 h fin octobre, au milieu d'une collecte de quatorze jours.

Le SSID est OBLIGATOIRE. `capturer_ssid.py` l'obtient une fois et
l'ENREGISTRE dans `session.json` : il n'y a rien à recopier, le collecteur
et le diagnostic le relisent de là. Si la fenêtre se referme aussitôt, c'est
qu'une session valide existait déjà — `--nouvelle-session` en ouvre une sans
cookies. En hébergement, `--afficher` donne le jeton à mettre dans
`POCKET_OPTION_SSID`, le disque d'un conteneur étant éphémère. La connexion intégrée de la
bibliothèque n'est pas utilisée — elle exige sept cookies simultanés dont six
traceurs tiers, et quand l'un manque elle se bloque indéfiniment sans un
message.

`.env` est lu par les points d'entrée (collecteur, service, diagnostic), pas à
l'import. L'environnement réel l'emporte toujours sur le fichier : en
hébergement, la plateforme injecte ses valeurs et un `.env` oublié dans
l'image ne doit pas les écraser.

La bibliothèque a été écrite avant Python 3.12 : elle appelle
`asyncio.get_event_loop()` en comptant sur l'ancien comportement, qui créait
une boucle quand le thread n'en avait pas. Depuis 3.12 cet appel lève, et la
bibliothèque est inutilisable telle quelle sur un Python récent. L'adaptateur
installe la boucle lui-même avant de construire le client.

L'essentiel de l'adaptateur consiste à **retraduire le silence en exceptions**.
La bibliothèque avale toutes ses erreurs (`except: return None`) ; un collecteur
bâti dessus tel quel tournerait des jours sans rien enregistrer et sans une
seule erreur dans les logs.

## Où faire tourner la collecte

Il n'existe pas, en 2026, d'hébergement gratuit offrant un disque persistant
adapté à quatorze jours de collecte. Render réserve les disques aux offres
payantes et endort les instances gratuites ; Fly.io n'a plus de tier gratuit ;
Railway suspend ses services quand le crédit d'essai est épuisé. Sur un disque
éphémère, la base repart vide à chaque redémarrage — on croirait collecter sans
rien accumuler, ce qui est exactement le désastre silencieux que la §1.1 vise.

**Collecter depuis son poste ne coûte rien et fait presque aussi bien** :

    .\outils\collecter.ps1

Le lanceur empêche la mise en veille — sans quoi la collecte s'arrête au premier
écran noir — relance le service s'il tombe, et journalise à côté de la base.
Les alertes Telegram fonctionnent : elles n'ont besoin que d'une sortie HTTPS.
Le renouvellement du jeton devient d'ailleurs immédiat, capture et collecte
étant sur la même machine.

Le seul inconvénient est l'uptime, et c'est précisément ce que la table `uptime`
mesure : le backtest refuse de générer un signal sur une fenêtre chevauchant un
trou de connexion (§2.4). Une interruption est une donnée manquante déclarée,
pas une donnée fausse.

Pour héberger malgré tout, il faut un disque persistant — Render Starter, ou une
petite VPS. `render.yaml` et le `Dockerfile` sont prêts.

## Déploiement

Avant tout, en local, avec la configuration de production :

    .venv\Scripts\python outilserifier_deploiement.py

Il contrôle ce qu'aucun test ne peut couvrir, parce que cela dépend de comptes
réels : que le jeton Telegram est valide, et surtout que `TELEGRAM_CHAT_ID`
désigne bien VOTRE conversation — il vous envoie un message pour le prouver.
L'erreur la plus fréquente est d'y mettre l'identifiant du bot ; un bot ne
s'envoie pas de message à lui-même, et aucune alerte ne parviendrait jamais.

Déployer d'abord et déboguer ensuite dans le visualiseur de journaux d'un
hébergeur coûte plusieurs minutes par aller-retour, souvent avec une erreur
tronquée. Ce contrôle prend quinze secondes.

### Renouvellement du jeton, sans copier-coller

Le SSID expire. Quand cela arrive :

1. le service **arrête** la collecte, garde le processus en vie et vous alerte
   sur Telegram — la sonde `/health` passe au rouge, car un processus vivant
   qui n'enregistre rien est exactement ce qu'elle dénonce ;
2. chez vous, une commande capture ET envoie le jeton :

       .venv\Scripts\python outils\capturer_ssid.py --envoyer https://votre-service

3. le service l'installe sur le volume persistant et reprend seul.

La capture exige un navigateur — les cookies naissent sur la machine qui se
connecte, jamais sur le serveur. Aucune fenêtre « ouverte à travers Telegram »
n'y changerait rien. Mais rien n'oblige un humain à faire le transport.

`POST /session` répond 404 tant qu'`ADMIN_SECRET` n'est pas défini : un point
d'entrée acceptant un jeton de session sans authentification permettrait à
quiconque connaît l'URL de détourner la collecte vers un autre compte.

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
