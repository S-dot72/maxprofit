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

## 8. Lancer l'étude et lire les résultats

Code : `maxprofit/live/engulfing.py`, rangé avec les autres études en lecture
seule (la règle des couches, `tests/test_layering.py`, interdit à
`research` d'être appelé par le bot). Son isolation est vérifiée par
`tests/test_engulfing.py` : aucun import de l'exécution des ordres, de la
collecte, du dimensionnement, de la course ni de l'hébergement, et aucune
requête d'écriture.

- **`/engulfing essai`** : le contrôle technique, à lancer d'abord. Il
  prend une paire (la plus fournie en ticks) sur les deux derniers jours
  complets et passe par le même code, statistiques, rapport et fichiers
  compris. Il ne rend que des comptes, des durées, la mémoire, la version
  déployée et une projection de la passe complète. Aucun taux n'est montré,
  et l'essai ne compte pour rien.
- **`/engulfing`** : la passe complète, bougies et ticks. Compter 10 à 40
  minutes sur le serveur gratuit. Le premier point d'avancement arrive
  après 2 minutes, les suivants toutes les 10 minutes, avec la mémoire et
  une fin estimée. Une seconde `/engulfing` pendant la passe dit où elle en
  est. Le premier lancement complet fait foi.
- **`/engulfing sans_ticks`** : l'approximation M1 seule, plus rapide, si
  les ticks ne passent pas. Elle ne remplace pas l'exécution aux ticks.
- **`/engulfing active`** : tout sur la base active (son quota de
  transfert), si la relève ne répond pas.

| Livrable demandé | Où le lire |
|---|---|
| 1. Données effectivement utilisées | ce rapport ; `engulfing_donnees.csv` (minutes de bougies, de ticks, relevés de payout, par paire et par jour) |
| 2. Motifs détectés | rapport ; `engulfing_statuts.csv` (motifs haussiers et baissiers par période) |
| 3. Signaux admissibles | `engulfing_statuts.csv` (déclenchés, non déclenchés, données manquantes) ; `engulfing_signaux.csv.gz` |
| 4. Résultats à 1, 2, 5, 10 et 15 min | rapport ; `engulfing_cases.csv` |
| 5. Par série et par actif | `engulfing_details.csv` (paire, sens, semaine) ; plus longue série de pertes dans `engulfing_cases.csv` |
| 6. Gagnants contre perdants | `engulfing_pertes.csv` |
| 7. Filtres testés un par un | `engulfing_cases.csv` (V01 à V12, écart à V00 avec son IC) |
| 8. Erreurs et ambiguïtés | `engulfing_statuts.csv` ; statuts de chaque signal dans `engulfing_signaux.csv.gz` |
| 9. Tests automatisés | `tests/test_engulfing.py` |
| 10. Exploration ou validation | colonne `periode` partout ; verdicts « validée » ou « non reproduite » |

**Le moteur a été éprouvé sur des prix tirés au hasard** (marche aléatoire
au tick, 4 paires, 21 jours, 4 graines), avant tout contact avec les vraies
données :
- aucune variante n'est sortie « validée » ;
- quelques cases ont paru bonnes en exploration, puis sont sorties « non
  reproduites » en validation ;
- le scénario A invalide a donné 100 % à 1 minute et 0 % à 2 minutes, par
  construction ;
- une seule caractéristique a franchi la règle de l'étude des pertes, sur
  une graine sur quatre, par hasard.
Ce dernier point rappelle qu'une caractéristique « notée » reste une piste :
elle doit encore se confirmer sur des jours futurs.

## 9. Le premier lancement (14-15/11) : rien n'est revenu

Après deux heures, ni rapport ni message d'erreur. Une erreur pendant la
passe envoie un message « interruption », une commande inconnue reçoit
l'aide : le silence total désigne un processus arrêté de l'extérieur.

**Cause la plus probable : la mémoire.** Le service tourne sur l'offre
gratuite de Render (512 Mo, une fraction de processeur ; `render.yaml`). La
première version gardait tous les signaux en mémoire, puis les mettait en
forme d'un coup pour le fichier final. Sur un banc d'essai réaliste
(2 paires, 10 jours, ~120 ticks par minute), cela donnait 60 Mo et 11 s.
Rapporté à 20 paires, plus d'un mois et la vraie densité de ticks, on
arrivait à plusieurs centaines de Mo en fin de passe, après plus d'une heure
de calcul : de quoi faire tuer le processus sans qu'aucun message ne parte.

**Version allégée** (mêmes définitions, même protocole figé, même
empreinte) :
- chaque paire est agrégée, puis libérée, et le fichier des signaux
  s'écrit compressé au fil de l'eau ; la mémoire ne grandit plus qu'avec
  quelques octets par signal jouable ;
- une minute de ticks n'est décodée que si un prix y est lu, et la
  continuité se juge sur la présence des chemins, sans les décoder ;
- le décodage n'appelle plus de fonction par entier ;
- les tirages du bootstrap (1 000, graine 7, blocs paire × jour) sont faits
  une fois par période et partagés par toutes les cases ;
- un point d'avancement part toutes les 15 minutes.

Sur le même banc d'essai : 3,1 s au lieu de 10,7 s, et un pic de mémoire
qui ne dépend plus du nombre de signaux, avec des résultats identiques
(mêmes motifs, mêmes épisodes, mêmes taux).

## 10. Toujours rien après la version allégée (16/11) : rendre l'arrêt visible

La version allégée n'a toujours rien rendu, et le bot n'a rien dit. Deux
mesures avant de corriger :
- **Le calcul tient.** Sur un banc à l'échelle réelle (une paire entière
  en ticks, 41 jours, ~120 ticks par minute), la passe prend 3,7 s ici et
  monte la mémoire de 60 Mo, puis la rend avant la paire suivante. Les
  20 paires, dont la plupart n'ont des ticks que la moitié du temps,
  représentent environ une minute de calcul ici, soit 10 à 25 minutes sur
  une fraction de processeur, réseau compris.
- **Ce qui tue la passe ne laisse pas de trace.** Un déploiement (chaque
  fusion sur `main` en déclenche un), une mise en veille ou un manque de
  mémoire arrêtent le processus. Le calcul meurt avec lui, et la ligne qui
  aurait envoyé le rapport ne s'exécute jamais. Une fusion pendant la passe
  du 15/11 suffit à expliquer le silence.

Ce qui change (le protocole figé, l'empreinte et les résultats restent
identiques, fichier par fichier, sur le banc) :
- **à l'arrêt du service**, chaque commande longue encore en cours est dite
  « interrompue » à qui l'a lancée, avec la consigne de la relancer après
  « 🟢 Collecte démarrée ». Seul un arrêt brutal, faute de mémoire, peut
  encore passer sans message ;
- **les points d'avancement montrent la mémoire** du processus, son pic
  pendant l'étude et, si le conteneur le dit, son occupation sur 512 Mo.
  Une mémoire qui monte vers 512 Mo avant le silence désigne le manque de
  mémoire. `/etat` montre aussi la mémoire ;
- **le premier point arrive après 2 minutes**, avec la version déployée
  dans le message de lancement ;
- la veille qui envoie les points est gardée en référence, et un point qui
  ne part pas ne l'arrête plus ;
- un rapport refusé par le réseau est renvoyé une fois, et les fichiers
  partent quoi qu'il arrive au rapport ;
- **`/engulfing essai`** vérifie la chaîne entière en une à quelques
  minutes et prévoit la durée et la mémoire de la passe complète.


## 11. Résultats du 16/11 : l'Engulfing ne bat pas le point mort sur l'OTC

Rapport `/engulfing`, version bc01843. Il couvre 20 paires, du 06/10 au
15/11 : 905 040 minutes de bougies et 905 517 minutes de ticks. La passe a
pris 25,3 minutes sur le serveur, avec un pic de mémoire de 239 Mo. Les
correctifs de la section 10 ont tenu.

- **Volume.** 150 840 motifs (90 514 en exploration, 60 326 en
  validation) et 40 083 épisodes retenus sans filtre (V00).
- **V00 aux ticks** (entrée 2 s après l'atteinte, décisions jouables).
  Selon l'échéance, il gagne entre 49,1 % et 50,5 % en validation, sur
  environ 5 760 décisions chacune. La borne haute de l'intervalle de
  confiance à 95 % reste sous 52,08 % à toutes les échéances : 51,7 % au
  mieux, à 1 minute.
- **Même conclusion avec le jour pour unité.** Sur les 13 jours de
  validation, l'intervalle bootstrap des moyennes journalières vaut
  [49,0 % ; 51,9 %] à 1 minute et [48,6 % ; 50,4 %] à 15 minutes.
- **Variantes** (13 variantes × 5 échéances). Deux cases sont retenues en
  exploration, retouches 0-1 et 2-3 à 15 minutes. Aucune n'est validée.
- **Étude des pertes.** Aucune caractéristique connue à la décision ne
  sépare gagnants et perdants dans les deux périodes.
- **Contrôle hors protocole, par paire.** Sur 200 cases paire × sens ×
  échéance, 12 sont au-dessus du point mort dans les deux périodes. Le
  hasard seul en donnerait environ 16,6. Aucune paire ne se distingue.
- **L'approximation M1 flatte.** À 1 minute, elle donne 52,9 % contre
  50,5 % aux ticks, sur les mêmes signaux : un signal sur cinq change
  d'issue entre la bougie et l'exécution réelle. Une lecture de graphiques
  M1 surestime donc les entrées au retour du niveau.
- **Espérance au taux observé** : entre −3 et −6 cents par dollar misé.

**Verdict.** L'Engulfing, avec ou sans filtre, ne bat pas le point mort sur
l'OTC, à aucune échéance. Il n'y a rien à geler ni à confirmer.
