# Étude Engulfing OTC : architecture et sources de données (14/11)

Ce rapport précède tout code de l'étude. Il est tiré du code du dépôt, des
documents d'étape 0 et 2, et des mesures déjà reçues du bot. La présence
réelle des données sur la période étudiée est redite par la commande
elle-même à chaque lancement (`engulfing_donnees.csv`).

Protocole figé : `docs/preinscription_engulfing.json`, empreinte
`443c34106529632c470a055288dab4c6900cb011a0daa59c4146e320315d994f`.

## 1. Ce qui tourne

| Brique | Fichier | Rôle | Touchée par l'étude ? |
|---|---|---|---|
| Collecte | `collect/collector.py`, `collect/pocketoption.py` | reçoit les ticks du broker, agrège les bougies M1, écrit bougies, chemins de ticks, payouts, battements | non |
| Stockage | `store/market.py`, `store/pool.py` | base active (PostgreSQL Neon) et relève (copie qui sert les études) | lecture seule |
| Course | `live/plan_demo.py`, `execution/courtier.py` | stratégie de la course (ZoneH1 par défaut) et ordres | non, jamais importée |
| Rejeu | `apprentissage/historique.py` | signaux ZoneH1 recalculés chaque jour depuis les bougies M1 | non |
| ZoneH1 | `strategies/zone_h1.py` | zones (piques à deux bougies opposées, sur les corps) | son code est APPELÉ tel quel, jamais modifié |
| Études | `live/*.py`, `hosting/service._sur_la_releve` | bases ouvertes en lecture seule, `statement_timeout`, `ConnexionTracee` qui refuse toute requête autre qu'une lecture | même cadre |

## 2. Les tables

| Table | Contenu | Horodatage | Présence connue |
|---|---|---|---|
| `candles` | une bougie M1 par paire et par minute : OHLC, `tick_count`, `complete` | début de la minute, UTC | 20 paires ; les lots de `/graphiques` en montrent du 10/10 au 02/11 |
| `tick_paths` | les ticks d'une minute, à la ms, encodés sans perte | début de la minute ; ticks à la ms | code des chemins du 09/10. Semaine du 26/10 au 02/11 (`/historique_ticks`) : AUDCAD 100 %, EURUSD 95 %, AUDCHF 79 % des minutes de collecte ; les 17 autres paires 48 à 53 % ; 75 à 125 ticks distincts par minute |
| `payouts` | payout du flux et ouverture, à chaque changement | relevé toutes les 5 minutes | tout le catalogue (183 paires) |
| `uptime` | battement toutes les 30 s | horloge système | avec des trous d'historique connus |
| `executions` | les ordres de la course | horloges locale et broker | non utilisée ici |
| `ticks` | une ligne par tick (v1) | ms | présente, plus alimentée |

**Conséquence pour l'étude.** La plupart des paires n'ont des données que la
moitié du temps (rotation des abonnements, silences). Un motif exige deux
minutes consécutives, et une issue exige des minutes continues jusqu'à la
sortie. Les trous ne sont jamais comblés : ils écartent le signal, avec un
motif compté.

## 3. Les horodatages

- **Ticks** : horloge du serveur du broker, ramenée en UTC par un décalage
  arrondi à l'heure et revérifié toutes les 300 s. L'heure de réception
  n'est pas conservée.
- **Bougies** : minute [m, m + 60 s[ du tick. L'ouverture est le premier
  tick REÇU, la clôture le dernier reçu. Les chemins, eux, sont triés par
  horodatage : une ouverture ou une clôture peut donc différer d'un tick
  (écart « ordre d'arrivée », documenté à l'étape 0).
- **Payouts** : heure système du relevé, à 5 minutes près.
- **Ordres** : horloge locale et horloge du broker, avec leur décalage.

## 4. OTC, bougies M1 et ticks

- **OTC** : le prix est fabriqué par le broker, 24 h sur 24. Il ne suit pas
  le vrai marché : en octobre, les paires ont dérivé de 4 à 24 % en deux à
  trois semaines.
- **Bougie M1** : un résumé. L'ordre des mouvements dans la minute est
  inconnu, et une minute partiellement observée est marquée incomplète.
- **Ticks** : résolution à la ms. 47,6 % des ticks ont un jumeau exact
  (même instant, même prix). Ces jumeaux ne changent ni les prix ni les
  trajectoires. Une minute sans chemin est une minute non collectée, jamais
  un marché immobile.

## 5. Comment les anciennes expériences calculent leurs résultats

- **Issue** (`etude_micro.issue`) : entrée à la clôture de la bougie du
  signal, sortie à la clôture de la bougie complète qui commence 900 s plus
  tard. Une égalité est mise à part, puis exclue ou comptée perdue.
- **Payout** : appliqué = min(flux + 8, 92). Jouable si le flux vaut au
  moins 84, d'où un point mort de 52,08 %.
- **Fidélité au broker** (`/rapprochement`) : 86 ordres réels, accord de
  94 % entre l'issue calculée et le règlement du broker. Le prix d'entrée
  est la clôture de la bougie (écart médian nul).
- **Incertitude** : bootstrap par blocs paire × jour (1 000 tirages,
  graine 7) et correction BH à 10 %. Chaque protocole est figé par son
  empreinte avant toute donnée (`etude_micro.empreinte`,
  `exiger_le_gel`).

## 6. Les protections contre la lecture du futur

- ZoneH1 est causale : une pique n'existe qu'une fois les 12 bougies
  suivantes closes, et la tendance H1 se lit sur des heures closes. Les
  tests de `tests/test_causalite.py` le vérifient.
- Les périodes sont séparées (exploration, validation, confirmation), et le
  « sceau » de `research/decoupage.py` protège les périodes réservées.
- Les études vérifient leurs bornes de lecture dans leurs tests.
- **Piège constaté le 14/11.** Les graphiques du fichier des réponses
  montrent les 15 minutes qui suivent l'entrée. Une moyenne mobile calculée
  dessus fabrique un faux avantage : 21 trades « alignés » sur 21 gagnés,
  contre 22 sur 45 (48,9 %) sur les seules bougies d'avant l'entrée. Dans
  l'étude Engulfing, aucun filtre ne lit une donnée postérieure à la
  décision. Les mesures d'après l'entrée sont descriptives, jamais des
  filtres.

## 7. Ce que l'étude utilise, et ce qu'elle ne peut pas savoir

- **Utilisé** :
  - les bougies, pour le motif et ses caractéristiques ;
  - les chemins de ticks, pour l'atteinte du niveau et les issues ;
  - les payouts, pour savoir si un trade était jouable ;
  - le code de ZoneH1, pour les zones.
- **Impossible à savoir** :
  - l'heure de réception des ticks et l'ordre d'arrivée dans la minute ;
  - le volume, les prix d'achat et de vente ;
  - le payout propre à chaque échéance. Le même payout est supposé à 1,
    2, 5, 10 et 15 minutes, et il faudra le vérifier sur la plateforme.
