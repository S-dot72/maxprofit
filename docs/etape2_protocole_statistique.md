# Étape 2 — E. Protocole statistique de l'étude microstructurelle

À figer **avant** toute analyse confirmatoire. Toute modification après le gel
est datée et fait de l'hypothèse concernée une hypothèse exploratoire.

## 1. Population

- **Signaux** : ZoneH1 aux paramètres pré-inscrits (`PARAMETRES_PRE_INSCRITS`,
  échéance 900 s), recalculés par `exemples_historiques` sur les bougies
  collectées, identifiant `paire|ts_sec`. Les 20 paires collectées.
- **Inclusion** :
  - fenêtre de 15 min avant la décision VALID au sens du livrable D ;
  - décision à plus de 15 min d'un bord de la fenêtre de données ;
  - issue T0 à 900 s définie (pas d'égalité).
- **Exclusion**, par des règles indépendantes du résultat :
  - fenêtre INVALID ;
  - minutes en « divergence inexpliquée » ;
  - 30 min autour d'un déploiement connu ;
  - minutes sans bougie.
  Le nombre d'exclus est publié par motif. On n'exclut jamais une période
  parce que ses résultats sont mauvais.
- **Séparation** :
  - cette population n'est **pas** celle de T0/T2/T3 (`/entrees`) ni des
    expériences S ;
  - rien d'ici ne modifie ces expériences ;
  - toute comparaison chiffrée avec elles dit la différence de population,
    de période et de définition.

## 2. Résultat

- **Gain** : à 900 s après la décision, la clôture M1 est du bon côté du prix
  d'entrée. Le prix d'entrée est le dernier tick avant la décision, égal à
  la clôture de la bougie du signal. Égalité : exclue. Vérifié par l'audit :
  issue au tick = issue M1 dans 100 % des cas.
- **Payout** : celui en vigueur à la décision (`payout_at`). La course ne
  joue qu'au maximum du broker ; s'il manque, les résultats économiques
  passent en « provisoires ».

## 3. Découpage chronologique

Trois périodes, jamais mélangées : pas de tirage aléatoire de signaux
voisins.
1. **Exploration** : de la première journée de ticks exploitables jusqu'à J1.
   On y regarde les distributions et on formule les hypothèses.
2. **Validation** : de J1 à J2. On y sélectionne les candidates, avec au plus
   K hypothèses retenues.
3. **Confirmation** : des jours **collectés après le gel**, intacts jusqu'à
   la date de verdict.

J1 et J2 sont fixés au gel, sur les jours disponibles. La période de
confirmation exige que la collecte des ticks **continue**. Le stockage est
donc une condition de l'étude (livrable A).

## 4. Métriques

- **Principale** : amélioration de la **log-loss** hors échantillon d'un
  modèle « référence + famille » face à la référence seule.
  - Référence, connue à la décision : paire, heure UTC (créneaux de 4 h),
    sens, `touches`, `entrees_deja_offertes`, distance de la clôture de la
    bougie du signal au niveau (en marges), payout en vigueur.
  - Modèle : régression logistique régularisée, interprétable.
- **Secondaires** :
  - score de Brier ;
  - calibration (courbe et pente) ;
  - taux de gain par terciles de la variable, terciles **fixés sur
    l'exploration** ;
  - EV par ordre au payout réel ;
  - couverture : part des signaux où la variable existe.
- **Pertinence pratique** : une amélioration n'est retenue que si elle
  dépasse un seuil fixé au gel. Par exemple, +2 points de taux de gain dans
  le tercile retenu et une log-loss meilleure dans au moins 2 périodes
  hebdomadaires sur 3.

## 5. Incertitude

- **Méthode** : bootstrap par blocs paire × jour (convention des audits),
  1 000 tirages, graine 7. On publie les intervalles à 95 %.
- **Dépendance** : on la publie, par la corrélation des issues de signaux
  d'une même heure, toutes paires confondues.

## 6. Comparaisons multiples

- **Hypothèses principales** : 4 au plus, une par famille de marché (A à
  D), désignées au gel. Corrigées par Holm au seuil global de 5 %.
- **Autres variables** : exploratoires, avec un taux de fausses découvertes
  contrôlé (Benjamini-Hochberg) et le nombre total d'essais publié.
- **Interactions** : seulement après les familles seules, chacune avec son
  hypothèse écrite avant le calcul, et comptées dans les essais.

## 7. Puissance et volume minimal

- **Volume disponible** : environ 105 signaux par jour, en comptant ceux
  dont la fenêtre est incomplète.
- **Effet détectable en confirmation** :
  - avec 500 signaux, l'écart-type d'un taux de gain est d'environ 2,2
    points ;
  - un écart de 5 points entre terciles (environ 165 signaux chacun) n'est
    qu'à environ 1 écart-type ; il faut environ 8 points.
- **Règle** : sous 500 signaux de confirmation, verdict « données
  insuffisantes », jamais « validé ».

## 8. Robustesse, sur les seules candidates

- Sous-fenêtres voisines prédéfinies (1, 3, 5 et 15 min).
- Garde de disponibilité G = 0, 2 et 5 s.
- Avec et sans fenêtres PARTIAL.
- Ticks distincts / ticks bruts.
- Par paire et par créneau horaire : pour juger la stabilité, pas pour
  choisir.
- Avec et sans les jours à flux simple (20/10).
- Une variable qui disparaît après un changement raisonnable de méthode est
  publiée comme telle.

## 9. Verdicts possibles

- **Signal confirmé** : les critères principaux sont tenus en confirmation,
  après correction.
- **Prometteur, non confirmé** : l'association existe en exploration ou en
  validation, mais la confirmation est insuffisante.
- **Pas de preuve exploitable** : l'effet disparaît après contrôle.
- **Données insuffisantes**.

Une candidate confirmée devient une **pré-inscription**, jamais un filtre
appliqué directement à la course.
