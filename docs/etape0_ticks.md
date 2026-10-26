# Étape 0 — audit forensique des ticks : inventaire technique et méthode

Cahier des charges v1.0 du 25/10/2026. Lecture seule. Rédigé à partir du code
du dépôt (commit de cette version), pas de sa documentation : chaque règle
ci-dessous renvoie au fichier et à la fonction qui l'appliquent.

Rapport : `/ticks` (Telegram), calculé au rejeu quotidien. Hors du bot :
`outils/audit_ticks.py`. Calculs : `maxprofit/apprentissage/audit_ticks.py`
(contrôles) et `maxprofit/apprentissage/microstructure.py` (analyses
exploratoires) ; lectures et mise en forme : `maxprofit/live/ticks.py`.

## 1. Inventaire — ce qui est réellement stocké

| Élément | Fichier, fonction | Champs | Règles observées |
|---|---|---|---|
| Réception des ticks | `collect/pocketoption.py`, `_vers_tick`, `_vers_ms`, `_caler_horloge` | `Tick(pair, ts_ms, price)` | `ts_ms` = horloge du **serveur du broker**, unité (s ou ms) détectée au premier tick, ramenée en UTC par un décalage **arrondi à l'heure**, revérifié toutes les 300 s (`INTERVALLE_VERIF_HORLOGE_SEC`) ; un tick daté du futur au-delà de la tolérance arrête le collecteur. L'heure de **réception** n'est pas conservée. |
| Agrégation M1 | `collect/collector.py`, `CandleAggregator.add`, `drain_closed`, `drain_ticks`, `drain_all` | bougie : open, high, low, close, tick_count, complete | minute = `bucket_of_ms(ts_ms, 60)` (`core/timebase.py`) : [m, m+60 s[ ; open = 1er tick reçu de la minute, close = dernier tick reçu, high/low = extrêmes ; un tick dont la minute est **antérieure** à la minute ouverte est **ignoré sans compteur** ; arrêt ou coupure : la minute en cours est écrite **incomplète** (`complete = 0`). Les ticks de la minute vont dans le même paquet que la bougie (un seul tampon). |
| Écriture des bougies | `store/market.py`, `MarketWriter.upsert_candles` | table `candles` (pair, tf_sec, ts_sec, open, high, low, close, tick_count, complete) | en conflit : open d'origine conservé, high = MAX, low = MIN, close = dernier écrit, tick_count = MAX, complete = MAX. Fusion de bases (`store/fusion.py`) : la bougie déjà présente reste. |
| Écriture des ticks | `store/chemin_ticks.py`, `encoder` ; `MarketWriter.insert_tick_paths` | table `tick_paths` (pair, minute_sec, n_ticks, echelle, chemin) | une ligne par (paire, minute) ; ticks **triés par horodatage** à l'écriture (l'ordre d'arrivée est perdu) ; deux ticks au même instant gardés ; prix convertis en entiers sur la plus petite grille décimale 10^-échelle (tolérance 1e-6 unité de grille, échelle ≤ 8) ; écarts en ms et en unités de grille (varints, zigzag), puis zlib niveau 6. En conflit, et à la fusion de bases : le chemin **le plus fourni** gagne. Jamais de chemin vide. Table `ticks` (une ligne par tick, v1) : présente, plus alimentée. |
| Lecture des ticks | `store/market.py`, `MarketReader.tick_paths`, `MarketReader.ticks` ; `store/chemin_ticks.decoder` | — | `ticks(pair, a, b)` élargit aux minutes couvrant [a, b[, décode, filtre à la ms ; ne comble pas les minutes absentes. `decoder` lève si zlib, échelle ou nombre de ticks sont incohérents. |
| Battements | `collect/collector.py`, `_taches_periodiques` ; `MarketWriter.heartbeat` | table `uptime` (ts_sec, n_pairs) | un battement toutes les 30 s (`heartbeat_sec`), horloge système. Ne dit pas QUELLES paires étaient souscrites. |
| Signaux ZoneH1 | `apprentissage/historique.py`, `exemples_historiques` | `Exemple(ts_sec, pair, contexte, gagne, issues)` | **aucune table** : les signaux sont recalculés à chaque rejeu depuis les bougies M1. `ts_sec` = clôture de la bougie du signal = instant de décision ; `contexte` porte `prix_signal`, `sens_call`, `niveau` (prix de la zone) ; `issues` = issues à 60…900 s par les clôtures M1 ; un signal sans bougie à 15 min ou à égalité à 15 min n'est pas produit ; une égalité à une autre échéance est absente de `issues`. Identifiant reproductible : `paire|ts_sec`. |
| Ordres réels | `execution/journal.py` (schéma), `execution/courtier.py` (remplissage) | table `executions` : signal_ts_ms, clic_ts_ms, accepte_ts_ms (horloge locale) ; ouverture_ts_ms, expiration_ts_ms (horloge broker, `openTimestamp` × 1000) ; decalage_broker_ms (horloge broker − locale, mesuré à l'ordre) ; prix_attendu (dernier prix vu par la **course**, sa propre connexion) ; prix_entree, prix_sortie (broker) ; sens, expiration_sec, payout_flux_pct, payout_broker_pct, accepte, refus, resultat, profit, brut | ordres rapatriés depuis le broker (`brut.rapatrie_du_broker`) : signal = clic = acceptation = ouverture, prix_attendu = prix d'entrée — inutilisables pour les délais et le prix attendu. Pas de prix au clic ni à l'acceptation. |
| Démarrages du collecteur | `hosting/superviseur.py` | compteur en mémoire | non persisté. |

### Ce qui peut être reconstruit

- Les ticks de chaque minute stockée, à la ms près, sur la grille décimale.
- Les bougies M1 depuis les chemins (même convention : open = 1er tick de
  la minute après tri, close = dernier).
- Les périodes d'écoute du collecteur (battements) et donc la distinction
  entre « collecteur arrêté » et « collecteur actif mais paire muette ».
- Le prix de NOTRE flux à tout instant couvert, y compris aux instants d'un
  ordre (signal, clic, acceptation via le reste du décalage d'horloge ;
  ouverture et clôture dans l'horloge du broker, heures entières ôtées).
- Les trajectoires de prix après chaque signal couvert (MFE, MAE,
  franchissements de la zone, issues à 4, 10 et 15 min).

### Ce qui est impossible à vérifier avec les données actuelles

- L'heure de réception des ticks, donc la latence du flux.
- L'ordre d'arrivée des ticks au sein d'une minute.
- Le nombre de ticks tardifs écartés (ni compteur ni trace).
- Ce que le broker calcule sans l'envoyer (échantillonnage du flux).
- Le prix au clic et à l'acceptation tels que le broker les voyait.
- Volume, prix d'achat et de vente séparés.
- Les paires lues au broker par la course sans être collectées.

## 2. Contrôles, statuts et seuils

Statuts : **PASS** (critère satisfait), **WARNING** (limite ou anomalie à
surveiller), **FAIL** (échec vérifié), **NOT TESTABLE** (données ou moyens
insuffisants). Les seuils sont dans `SEUILS` (`audit_ticks.py`), fixés avant
la première mesure ; leur justification est ci-dessous.

| Contrôle | Critère | Justification |
|---|---|---|
| Horodatages | FAIL : granularité ≥ 1 s, tick hors de sa minute, ordre décroissant ; WARNING : doublons exacts > 1 % des ticks ou gels ≥ 10 s sur > 1 % des minutes | Une étude sous la minute exige la ms ; un gel de 10 s représente ~20 ticks manquants au débit observé à la conception. |
| Compression | FAIL : chemin illisible, réencodage différent du stocké, ou `MarketReader.ticks()` différent du décodage, sur l'échantillon contrôlé | « Sans perte » doit être démontré sur les données, pas supposé. |
| Concordance M1 | FAIL : écarts inexpliqués > 0,1 % des minutes comparées ; WARNING : au moins un, une minute de ticks sans bougie, ou > 2 % d'écarts explicables | Bougie et chemin viennent des mêmes ticks : tout écart inexpliqué est une erreur de stockage. |
| Trous | sur les minutes où le collecteur battait : FAIL > 5 % sans ticks, WARNING > 1 % ; les minutes sans historique des battements (§2 bis) sortent du contrôle | Au-delà de 5 %, une fenêtre de 30 min a plus d'une chance sur deux d'être touchée. |
| Couverture des signaux | FAIL < 50 % de fenêtres complètes, WARNING < 80 % | En dessous de 50 %, l'échantillon exploitable cesse d'être représentatif. |
| Prix d'exécution | prix d'ouverture ET de clôture retrouvés dans le flux à ± 1 s : FAIL < 70 %, WARNING < 90 % ; NOT TESTABLE sous 10 ordres | Seul contrôle externe au flux. |
| Ticks tardifs | NOT TESTABLE | Aucun compteur persistant. |
| Heure de réception | NOT TESTABLE | Non stockée. |

## 2 bis. Règles ajoutées après le premier rapport réel (25/10)

Le premier `/ticks` réel (18/10 17:19 → 25/10 17:19, code c0a1fbd) a montré
trois classements faux. Les règles ci-dessous les corrigent ; elles sont
postérieures à cette mesure, et le disent.

- **Historique des battements absent ≠ collecteur arrêté.** Constat : 586 des
  596 exclusions de signaux (TIMESTAMP_UNCERTAIN) tombaient dans deux trous de
  la table `uptime` (13 h, puis 4,7 jours) pendant lesquels les 20 paires
  avaient leurs ticks sans interruption. Le collecteur écrit ticks et
  battements depuis la même boucle (`Collector._taches_periodiques`) : des
  ticks continus prouvent qu'il tournait, les battements manquent à la base.
  Règle : un trou de battements d'au moins 2 h (`HISTORIQUE_ABSENT_SEC`), les
  bords de la fenêtre comptant comme des battements, couvert par les ticks de
  la paire sur au moins 90 % de ses minutes (`HISTORIQUE_COUVERT`), rend ses
  minutes « sans historique » : le contrôle par les battements y est NOT
  TESTABLE (anomalie « historique des battements absent »), ni réussi ni
  échoué. Un trou plus court, ou peu couvert (cas d'un horodatage décalé
  d'une heure dans une vraie panne), reste « ticks sans battement ».
- **Ordre d'arrivée.** L'agrégateur prend l'ouverture et la clôture de la
  bougie dans l'ordre où les ticks ARRIVENT ; l'encodeur trie le chemin par
  horodatage. Nombre de ticks, haut et bas égaux, et une ouverture ou une
  clôture de bougie qui est un autre tick de la minute : écart explicable
  « ordre d'arrivée » (anomalie INFO, gardée au registre parce qu'elle
  montre que des ticks arrivent hors de l'ordre de leurs horodatages).
- **Doublons exacts.** 47,6 % des ticks avaient un jumeau (même instant,
  même prix). Les débits sont aussi donnés en ticks DISTINCTS, une minute
  est « à flux doublé » à 40 % de doublons ou plus (`DOUBLES_MIN`), et le
  seuil de la minute pauvre (`INSUFFICIENT_COVERAGE`) se lit en ticks
  distincts. Les prix, l'OHLC et les trajectoires n'en sont pas changés ;
  le `tick_count` des bougies les compte. Le débit anormal se juge aussi en
  ticks distincts.
- **Cause probable, correction suspendue.** Hypothèse (non prouvée sur des
  séquences brutes) : le collecteur et le courtier partagent le processus,
  chacun avec son socket abonné aux mêmes paires, et la bibliothèque empile
  les ticks des deux sockets dans un seul tampon global, celui que vide le
  collecteur. Une déduplication (instant, prix) dans `CandleAggregator` a été
  fusionnée le 25/10/2026 à 21:50 UTC (#89) puis RETIRÉE à 21:55 UTC
  (#90) : au plus quelques minutes en service. Elle ne doit revenir qu'après
  l'enquête (docs/enquete_doublons.md), dans une modification distincte.

- **Paire fermée au broker ≠ trou de collecte** (ajout du 26/10).
  - Constat : EURJPY fermée par le broker le 26/10 à 19:51, CADJPY absente
    de 01:02 à 02:34, alors que le collecteur tournait.
  - Le collecteur ne s'abonne pas à une paire que le broker déclare fermée
    (`Collector._paires_epinglees`). La table `payouts` garde cet état
    (`is_open`) à chaque changement.
  - Une minute sans ticks où le dernier relevé en vigueur dit la paire fermée
    est comptée « paire fermée au broker » (anomalie INFO), hors du contrôle
    des trous.
  - Requêtes `ouverture_avant` et `ouvertures`. Les bornes sont à 5 min près
    (relevés toutes les 5 min).

## 3. Règles de complétude d'une fenêtre de signal

Fenêtre = [décision − 15 min, décision + 15 min[, 30 minutes. Complète si,
dans cet ordre (le premier échec donne le code) :

| Code | Échec |
|---|---|
| NO_TICKS | aucune minute de ticks dans la fenêtre |
| DECOMPRESSION_ERROR | une minute de la fenêtre est illisible |
| INTERNAL_GAP | au moins une minute absente |
| INVALID_ORDER | un tick hors de sa minute ou un ordre décroissant |
| TIMESTAMP_UNCERTAIN | une minute de ticks sans battement du collecteur hors trou de l'historique des battements (§2 bis), ou horodatages à la seconde |
| START_BOUNDARY_MISSING | premier tick à plus de 5 s du début |
| END_BOUNDARY_MISSING | dernier tick à plus de 5 s de la fin |
| FEED_FREEZE | un écart de 10 s ou plus entre deux ticks |
| INSUFFICIENT_COVERAGE | une minute à moins de 5 ticks distincts |
| M1_MISMATCH | une minute dont la concordance M1 est inexpliquée |
| NO_TICK_AT_DECISION | aucun tick dans les 5 s qui précèdent la décision |

L'issue du contrat n'intervient jamais dans ce statut.

## 4. Conventions

- Prix d'entrée d'un signal : dernier tick avant la décision (= clôture de
  la bougie du signal). Sortie à l'échéance s : dernier tick avant
  décision + s (= clôture M1 de la minute qui finit à décision + s).
  Égalité : ni gagné ni perdu, exclu de l'échéance.
- Unités : « amplitude M1 » = moyenne de (haut − bas) des 60 bougies jusqu'à
  la bougie du signal (convention de `historique._excursions`) ; « point » =
  une unité de la grille décimale du prix.
- Écart d'exécution : **prix obtenu − prix de référence**, en points.
  Coût pour le pari = l'écart pour un CALL, son opposé pour un PUT : positif
  = défavorable (on a acheté plus cher, ou vendu moins cher).
- Franchissement de la zone : un tick de l'autre côté du `niveau` de la zone,
  contre le trade (la règle « zone cassée » de `historique._excursions`,
  lue au tick au lieu de la clôture M1). Aucun seuil en pips n'est ajouté.
- Intervalles : bootstrap par blocs paire × jour, 1 000 tirages, graine 7.

## 5. Reproduire

1. Lire, dans `/ticks`, la fenêtre `[début ; fin[` (secondes UTC) et
   l'empreinte des chemins.
2. `python outils/audit_ticks.py --debut <début> --fin <fin>` avec
   `DATABASE_URL` dans l'environnement (session ouverte en lecture seule).
3. Comparer l'empreinte : identique = mêmes données d'entrée. Sorties :
   `audit_ticks.json`, `audit_ticks.txt`, `anomalies_ticks.csv`,
   `signaux_ticks.csv`, `ordres_ticks.csv`.

Les requêtes exécutées (SELECT uniquement) sont dans `REQUETES`
(`audit_ticks.py`) et recopiées dans chaque rapport. Tout est recalculé à
chaque rejeu ; une valeur reprise d'un ancien rapport y est marquée comme
telle.
