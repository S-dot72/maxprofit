# Pré-inscription : six stratégies classiques sur l'OTC (`/classiques`, 17/11)

Demandé le 17/11 :

> « Je veux ce qui fonctionne sur otc […] tu vas tester une nouvelle
> stratégie sur les paires otc »

Paramètres figés : `docs/preinscription_classiques.json`, empreinte
`31e8565808605ceda03db67bd7f6cba2dec96c7dee62392b2d4d997b3fd85889`
(`etude_micro.empreinte`). La commande refuse de tourner si les paramètres
embarqués ne donnent plus cette empreinte.

## Pourquoi ces six-là

Ce sont des stratégies répandues chez les traders d'options binaires, que
nos études n'avaient pas encore testées telles quelles. Elles sont prises
avec les réglages standards des plateformes, sans aucun réglage fin : un
réglage choisi sur nos données serait une découverte du hasard.

| Famille | Règle | Sens conventionnel |
|---|---|---|
| F1 RSI, sortie de zone | RSI(14) repasse sous 70, ou au-dessus de 30 | retournement |
| F2 Bollinger + RSI | clôture hors de la bande (20, 2) avec RSI(14) extrême | retournement |
| F3 stochastique | croisement %K / %D (14, 3, 3) au-delà de 80 ou en deçà de 20 | retournement |
| F4 pin bar | longue mèche (≥ 2/3 de l'étendue) au plus haut ou au plus bas de 20 bougies | retournement |
| F5 chiffre rond | premier contact d'un niveau rond (0,005 ou 0,5) en 60 bougies, rejeté | rebond |
| F6 cassure de canal | clôture au-delà du plus haut ou du plus bas des 20 bougies précédentes | continuation |

Déjà testé ailleurs, donc absent ici : RSI, MACD et ATR seuls (EXP-002),
momentum de 7 bougies, croisement de moyennes et repli sur la médiane de
Bollinger (`/simples`), les six conditions de retour à la moyenne, la série
rouge, la prise de liquidité, les bougies incluses, ZoneH1, les familles de
`/terrain` et l'Engulfing.

## Ce qui compte

- **Exécution aux ticks.** Décision à la clôture de la bougie qui déclenche,
  entrée au dernier tick connu 2 s plus tard, sortie à l'échéance (1, 2, 3
  ou 5 minutes). Les deux ticks doivent dater de moins de 10 s, et chaque
  minute entre les deux doit avoir ses ticks.
- **Seules les décisions jouables comptent** : payout du flux ≥ 84, soit
  92 % appliqués et un point mort de 52,08 %. Une égalité est une perte.
- **Épisodes** : dans une famille, deux trades sur la même paire ne se
  chevauchent jamais (5 minutes au moins entre deux décisions retenues).
- **Le sens** : chaque case (famille × échéance) choisit en exploration
  entre le sens conventionnel et l'inverse, et le garde en validation.

## Les trois temps

1. **Exploration**, du 06/10 au 02/11 : une case est retenue si son meilleur
   sens atteint 52,08 % sur au moins 300 décisions.
2. **Validation**, du 03/11 à la veille du lancement : chaque case retenue est
   rejugée dans son sens (au moins 100 décisions), avec un bootstrap par
   blocs paire × jour et une correction de Benjamini-Hochberg à 10 %.
3. **Confirmation** : une case validée est gelée, puis confirmée sur des
   jours postérieurs au lancement, avec le jour pour unité, avant tout essai
   en démo.

## Limites connues, écrites avant le résultat

- Les jours de validation ont déjà servi de validation à d'autres études
  (`/terrain`, l'Engulfing). Ils restent hors échantillon pour CES règles,
  mais à force d'études sur les mêmes jours, une case peut passer par
  hasard : c'est pourquoi la confirmation sur des jours futurs est
  obligatoire.
- Deux semaines de validation détectent un vrai écart de l'ordre de 2 à 3
  points. Un avantage plus fin passerait inaperçu.
- Le rapport ne ventile rien par heure : la confirmation de la piste
  EURJPY 22 h doit rester aveugle.
- C'est une étude, pas un changement : la course, la collecte, la
  stratégie et les échéances restent telles quelles.

## Verdicts possibles

- **Piste** : une case validée. Elle est gelée, puis confirmée sur des jours
  futurs.
- **Rien** : aucune case validée. Ces six stratégies ne battent pas le point
  mort sur l'OTC.

## Lancer l'étude

Code : `maxprofit/live/classiques.py`. Il reprend la lecture des bases, le
décodage des ticks et le bootstrap de l'étude Engulfing. Tests :
`tests/test_classiques.py`. Ils vérifient les indicateurs valeur par
valeur contre `maxprofit.indicators`, chaque déclencheur, les fenêtres
continues, les issues aux ticks, l'absence de lecture du futur et la
lecture seule.

- **`/classiques essai`** : à lancer d'abord. C'est le contrôle technique
  sur une paire et deux jours, qui passe par toute la chaîne mais ne
  montre aucun taux. Il donne la durée et la mémoire prévues pour la passe
  complète.
- **`/classiques`** : la passe complète, à lancer une seule fois. Elle
  prend 10 à 40 minutes ; un point d'avancement arrive après 2 minutes,
  puis toutes les 10 minutes. Le premier lancement complet fait foi.
- **`/classiques active`** : tout sur la base active, si la relève ne
  répond pas.

Elle rend un rapport et cinq fichiers :
- `classiques_cases.csv` : une ligne par famille × échéance × période,
  avec le sens choisi, le taux, l'IC et le verdict ;
- `classiques_details.csv` : les cases retenues, par paire et par sens ;
- `classiques_statuts.csv` : déclenchements, épisodes, statuts des issues ;
- `classiques_donnees.csv` : les données lues, par paire et par jour ;
- `classiques_signaux.csv.gz` : chaque signal retenu et ses issues.
