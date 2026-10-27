# Étape 2 — Rapport final : stockage, doublons, préparation de l'étude

Diagnostic et spécification seulement. Aucune migration, suppression,
compaction, déduplication ni modification d'abonnement ou de stratégie.
Statuts : **Vérifié**, **Probable**, **Hypothèse**, **Inconnu**.

Sources :
- `/stockage` du 26/10 09:20 UTC ;
- `/ticks` du 26/10 05:05 UTC (code f1aa617, 7 jours, 20 paires) ;
- le code ;
- livrables A à E (`docs/etape2_*.md`).

## 1. Stockage et délai avant saturation

- **Mesures** [Vérifié] :
  - base 423,5 Mo, toutes bases 438,5 Mo ;
  - chemins de ticks 208,9 Mo, payouts 102,7 Mo, bougies 102,7 Mo ;
  - lignes mortes quasi nulles ;
  - index : 153 Mo au total ;
  - relève : 409,2 Mo.
- **Limite** [Vérifié le 27/10, documentation de Neon] : **1 Go par
  projet** sur l'offre gratuite, et **5 Go de transfert réseau par mois**.
  Le 512 Mo de départ était une hypothèse, et 0,5 Go l'ancienne limite.
  Voir la mise à jour du livrable A.
- **Croissance** [Probable] : 21,4 à 22,7 Mo par jour, estimée sur les
  lignes. La mesure physique demande 24 h de relevés sans redémarrage, d'où
  aucun déploiement en attendant.
- **Délai** [Hypothèse, 512 Mo] : 3,2 à 4,1 jours selon que Neon compte
  toutes les bases ou la seule base.
- **Console Neon** (Free plan) [Vérifié] : Storage 464,83 Mo, History
  16,93 Mo, Network transfer 2,39 Go depuis le 19/10. Neon compte le total
  des bases [Probable]. Pire cas : limite de 500 Mo décimaux, soit **environ
  1,5 jour** ; 512 Mio, environ 3 jours. **Urgence : décider tout de suite.**
- **Transfert réseau** : le plafond de 5 Go par mois tomberait vers le
  3 novembre au rythme actuel. Il suspendrait le calcul jusqu'à la période
  suivante. C'est désormais le premier risque, devant le stockage (environ
  1 Go vers mi-novembre).

## 2. Payouts

[Vérifié] 183 lignes répétées sur 550 334 (0,03 %), toutes le 18/10. Le reste
sont de vrais changements : le payout bouge presque à chaque relevé de 5 min.

## 3. Conservation ou compaction

- **Payouts : aucune compaction.** Gain inférieur à 0,04 Mo, perte de dates
  d'observation (livrable B). Question close.
- **Leviers réels, non lancés, à décider** :
  - reconstruire les index (gain non mesuré, testable d'abord sur la relève) ;
  - fusionner l'index redondant des payouts (migration) ;
  - restreindre l'écriture des payouts aux paires utiles (collecteur) ;
  - offre Neon supérieure ;
  - conservation glissante des chemins de ticks, avec archive hors de Neon.

## 4. Audit des ticks disponible

- Compression PASS.
- Couverture des signaux 96,1 % (756/787).
- Issue au tick = issue M1 : 100 %.
- 6 concordances inexpliquées, toutes à la minute du déploiement du 20/10
  23:20.
- Exécutions : 25 ordres comparables, analyse descriptive seulement.

## 5. Origine des doublons

- [Vérifié] Copies exactes, toujours par deux, jamais trois ; aucun instant
  partagé à prix différent ; coïncidences dans un flux simple ≤ 2 par
  million. Deux sockets écrivent dans un même tampon (code).
- [Probable] Le second socket est le courtier.
- La règle (paire, instant, prix) a une borne d'erreur mesurée (livrable C).

## 6. Schéma du journal pré-entrée

Jeu de données calculé hors ligne, rien en base (livrable D) :
- garde de disponibilité G = 2 s, faute d'heure de réception ;
- ticks distincts ;
- fenêtres de 1, 3, 5 et 15 min ;
- qualité VALID / PARTIAL / INVALID fixée à l'avance.

## 7. Variables candidates

Familles A (approche), B (régime), C (retournements), D (zone) et E (qualité,
qui ne sert pas à prédire). Toutes ont une définition mathématique et ne
sont calculées qu'à partir de ticks antérieurs à la décision − G (livrable D).

## 8. Protocole statistique

- **Découpage** : chronologique en trois périodes ; la confirmation porte sur
  des jours collectés **après** le gel.
- **Métriques** : log-loss principale face à une référence connue à la
  décision ; Brier, calibration et EV secondaires.
- **Incertitude** : bootstrap par blocs paire × jour.
- **Comparaisons multiples** : Holm sur 4 hypothèses principales, BH sur le
  reste.
- **Volume minimal** : 500 signaux de confirmation (livrable E).

## 9. Limites et points non vérifiés

- **Non stockés** : heure de réception, ordre d'arrivée, socket d'origine.
- **Battements** : absents de la base avant le 24/10.
- **Trous** : 480 minutes de ticks sans bougie le 24/10 (02:02-02:25).
- **CADJPY** : revenue après le redémarrage du 26/10 (`/paires` : 19 suivies,
  plafond d'abonnements « aucun »). La paire épinglée absente est désormais
  EURJPY, **fermée par le broker** à 19:51 UTC, un cas légitime. L'audit
  distingue maintenant « paire fermée au broker » et trou de collecte,
  d'après `payouts.is_open`. Le prochain `/ticks` dira si CADJPY était fermée
  de 01:02 à 02:34, sans qu'on ait à supposer la cause.
- **Ordres** : l'étiquette « refusé par le broker » de l'audit était
  **fausse en partie** [Vérifié, code]. La course inscrit chaque ordre AVANT
  de l'envoyer, et une ligne non acceptée peut être :
  - une réservation en suspens ;
  - un ordre arrêté avant l'envoi (« non envoyé : … » : plafonds, prix,
    payout) ;
  - une réservation introuvable chez le broker ;
  - un vrai refus, avec motif et contexte.
  Le motif est dans la colonne `refus`, que l'audit lit désormais.
  - Cause établie par le commit 6df9d2f du 26/10 pour au moins trois d'entre
    eux : solde tombé vers 74 $, première mise de 0,98 $, sous le minimum du
    broker (« refus sans motif »).
  - Correctif : la mise est relevée au minimum de 1 $.
  - Le prochain `/ticks` donnera le motif enregistré de chacun des 5.
- **`/classement`** : il annonce une part du temps au plafond et mesure une
  part de relevés, avec environ 20 points d'écart en médiane.

## 10. Actions à valider, par priorité

1. **Lire la limite réelle** dans la console Neon. Aujourd'hui, c'est toi qui
   le fais.
2. **Faire une sauvegarde complète hors de Neon** (`pg_dump`) avant toute
   action sur les données.
3. **Décider de la continuité avant le 28/10** :
   - **offre supérieure** : simple, aucune perte ;
   - **ou conservation glissante des chemins avec archive** : outil à
     écrire et tester, suppression en base de données archivées ;
   - **ou, en dernier recours, `STOCKER_TICKS=0`** : perte de ticks à venir,
     qui casse la période de confirmation de l'étape 3.
4. **Mesurer le gain d'une reconstruction des index sur la relève** :
   maintenance, sans changement de données.
5. **Doublons** : option 4 tout de suite (couche d'analyse) ; option 3
   (agrégateur, réversible) à ta décision ; marquage du flux d'origine si tu
   veux la preuve du mécanisme.
6. **Payouts** : restreindre l'écriture aux paires utiles ; fusionner l'index
   redondant (migration).
7. **`/classement`** : corriger ce qu'il mesure, ou son libellé.
8. **Exploitation** : CADJPY et les 5 refus du broker.

## Passage à l'étape 3

**Réunies** :
- consommation et croissance estimées (la mesure physique arrive) ;
- gains sur les payouts chiffrés (nuls) ;
- risques de perte documentés ;
- doublons étudiés ;
- schéma et protocole écrits.

**Bloquant** : la continuité de la collecte. Sans elle, il n'y a pas de
période de confirmation. Le plan d'exécution de l'étape 3 sera présenté
avant tout code, une fois la décision de continuité prise.
