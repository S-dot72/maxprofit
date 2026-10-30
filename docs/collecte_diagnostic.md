# Collecte : paires muettes, diagnostic avant tout correctif

Décisions du 30 et du 31/10. **Aucun correctif du collecteur** tant qu'on n'a
pas établi où les ticks disparaissent. Refusés pour l'instant :
- le réabonnement automatique ;
- tout changement du traitement de `updateStream` ;
- la stratégie, les expirations, les séries historiques.

## 1. Ce qui a été constaté (`/etat`, 31/10, 13 h après un redémarrage)

Seules AUDCAD (202 ticks/min) et EURUSD (41/min) reçoivent leurs ticks
normalement.
- AUDCHF est ralentie, EURNZD a un historique insuffisant.
- 16 paires sont muettes, dont 12 sans une bougie depuis plus de 5 h.
- Des reprises brèves ont lieu SANS redémarrage du collecteur (AUDNZD,
  EURNZD).
- Le collecteur ne se réabonne que sur un silence de TOUTES les paires
  (`Collector._signaler_silence`) : une paire qui se tait seule n'est pas
  relancée.

## 2. Deux explications, non tranchées

- **(a) Regroupement** : la bibliothèque n'ajoute à son tampon que le premier
  élément de chaque message de ticks (pocketoptionapi `ws/client.py`
  l. 215-222, commit épinglé `c94c2b0`). Si le serveur regroupe désormais
  plusieurs paires par message, les autres sont perdues. L'enquête du 25/10
  décrivait des messages à un seul élément.
- **(b) Abonnements perdus** : le serveur cesse d'envoyer une partie des
  paires.

Les deux n'appellent pas la même correction.

## 3. Observation passive des messages (`/etat`)

Code : `maxprofit/collect/observation_flux.py`. Il compte trois niveaux, par
paire, sur la dernière heure close :

1. **reçus** : les éléments des messages de ticks, sur chaque socket, avant
   que la bibliothèque ne les lise ; avec le nombre de messages qui
   contiennent la paire et ceux où elle est en tête ;
2. **transmis** : ce que l'adaptateur trouve dans le tampon
   (`PocketOptionSource._drainer`) ;
3. **enregistrés** : les ticks des bougies écrites, lus dans la mémoire de la
   course.

Les messages sont comptés à part selon qu'ils portent plusieurs ticks,
plusieurs paires, ou plusieurs ticks d'une même paire.

**Garanties, testées** (`tests/test_observation_flux.py`) :
- l'écouteur de la bibliothèque reçoit les mêmes objets, dans le même ordre ;
  les erreurs du socket remontent telles quelles ;
- rien n'est retiré du tampon ;
- toute erreur désactive l'observation, sans remonter ;
- la mémoire est bornée à 70 minutes ;
- coût mesuré : environ 4 à 5 µs par message de ticks, 0,2 µs pour les
  autres.

Les tests de l'adaptateur restent à l'identique : 91 réussis, et les mêmes
10 échecs dus à la bibliothèque absente de l'environnement de test.

**Lecture** :
- des éléments reçus mais jamais transmis, alors que les messages portent
  plusieurs paires, confirment (a) ;
- des paires muettes qui n'apparaissent dans aucun message reçu orientent
  vers (b).

Plusieurs éléments dans un message ne suffisent pas, seuls, à prouver une
perte.

## 4. Historique des ticks : `/historique_ticks [jours]` (admin)

Code : `maxprofit/live/historique_ticks.py`, 7 jours par défaut et 21 au plus.

**Où il lit** : en lecture seule sur la relève. La copie est vérifiée heure
par heure sur l'active, par des résumés calculés par le serveur. Une heure
divergente est reprise de l'active et marquée comme telle.

**Ce qu'il donne**, par paire et par heure :
- minutes de collecte (battements) ;
- minutes avec au moins un tick ;
- ticks bruts et distincts ;
- premier et dernier tick ;
- statut : SANS COLLECTE / SILENCE / RARE / ACTIVE.

Il donne aussi les silences d'au moins 15 minutes (nature, reprise), et pour
chaque paire la date « dégradée en continu depuis », avant ou après le 28/10.

**Une heure sans battement est SANS COLLECTE** : on n'y sait rien, ce n'est
pas un silence.

**Volume** :
- sur la relève : environ le volume de l'audit quotidien des 7 jours de
  chemins (97 Mo mesurés le 29/10), moins ce que les paires muettes
  n'ont pas écrit ;
- sur l'active : les résumés seulement, environ 0,2 Mo.

Vérifié sur SQLite et sur un PostgreSQL local : résultat identique, et
`transaction_read_only` à on sur les deux bases.

## 5. Conséquences pour l'étape 3

- **Avant le 28/10** : période à auditer.
  - `/historique_ticks 21` couvre l'exploration.
  - La passe de sélection classe déjà chaque fenêtre réellement utilisée
    (VALID, PARTIAL ou INVALID ; niveau 1 ou 2).
- **Après le 28/10** : période potentiellement affectée, à ne pas intégrer
  automatiquement à la confirmation.
