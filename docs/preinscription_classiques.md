# Pré-inscription : six stratégies classiques sur l'OTC (`/classiques`, 17/11)

Demandé le 17/11 :

> « Je veux ce qui fonctionne sur otc […] tu vas tester une nouvelle
> stratégie sur les paires otc »

puis, avant tout lancement :

> « Les 6 stratégies que tu as choisies sont trop vagues […] tu as des
> données de ce qu'on a déjà testé qui montrait ce qu'il fallait pas faire,
> combine-les à ces stratégies »

Paramètres figés : `docs/preinscription_classiques.json`, empreinte
`1bb555d2bb13415943330489dd7d1d3c0d351af0aa15fd23f6c15db9f0c122f9`
(`etude_micro.empreinte`). La commande refuse de tourner si les paramètres
embarqués ne donnent plus cette empreinte.

Cette version remplace la première (`31e85658…`, commit eb32d4d). Le
remplacement a eu lieu avant tout lancement de `/classiques` et avant tout
résultat. Les six règles brutes n'ont pas changé ; seule la variante
prudente s'est ajoutée.

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

## Brut et prudent

Chaque famille est jugée deux fois : **brute**, puis **prudente**. La
version prudente applique toutes ensemble les quatre leçons de nos études
passées qui disaient quoi ne pas faire :

| Leçon | Règle | D'où elle vient | Ce qu'elle a donné ensuite |
|---|---|---|---|
| L1 tendance H1 | jamais contre la tendance de l'heure close (définition de ZoneH1, recul de 2 heures, heures complètes) | règle de l'utilisateur du 27/09 | lot 2 : 54,5 % avec la tendance, 37,5 % contre (8 cas) ; `/bougies` : 52,8 % avec, 56,1 % contre |
| L2 mèche contre | pas de mèche d'au moins la moitié de l'étendue du côté opposé au trade | K4 au lot 1 (33 %), marteau à l'ancre de `/bougies` (40,5 % puis 38,1 %) | lot 2 : 50 % |
| L3 incluses | moins de 6 bougies incluses sur les 50 dernières | H_IB : 51,1 % puis 45,0 % avec 6 ou plus | quarantaine : l'effet s'est inversé |
| L4 paires USD | seulement les 7 paires qui contiennent USD | étape 3 : les autres paires à 46,9 % en validation | piste non gelée par l'audit du 02/11 |

**Aucune de ces leçons ne s'est clairement répétée sur de nouvelles
données** : la dernière colonne le montre. Leur combinaison est donc une
hypothèse, jugée comme les autres. Par construction, L2 écarte toutes les
pin bars : F4 prudente est vide.

Leçons écartées :
- les créneaux horaires et les paires isolées : les filtres de l'étape 3 qui
  gagnaient en exploration se sont effondrés en validation ;
- l'heure 22 d'EURJPY : sa confirmation doit rester aveugle ;
- les caractéristiques de l'étude des pertes Engulfing : aucune ne s'est
  répétée.

## Ce qui compte

- **Exécution aux ticks.** Décision à la clôture de la bougie qui déclenche,
  entrée au dernier tick connu 2 s plus tard, sortie à l'échéance (1, 2, 3
  ou 5 minutes). Les deux ticks doivent dater de moins de 10 s, et chaque
  minute entre les deux doit avoir ses ticks.
- **Seules les décisions jouables comptent** : payout du flux ≥ 84, soit
  92 % appliqués et un point mort de 52,08 %. Une égalité est une perte.
- **Épisodes** : dans une famille et une variante, deux trades sur la même
  paire ne se chevauchent jamais (5 minutes au moins entre deux décisions
  retenues).
- **Le sens** : chaque case (famille × variante × échéance) choisit en
  exploration entre le sens conventionnel et l'inverse, et le garde en
  validation.

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

- La variante prudente garde peu de signaux : 7 paires sur 20, dans le sens
  de la tendance, hors longues mèches et hors marchés comprimés. Certaines
  de ses cases n'auront pas les 300 décisions de l'exploration et ne seront
  pas jugées. Un filtre ne change pas le taux de gain d'un signal qui ne
  prédit rien ; il réduit seulement le nombre de trades.
- Deux variantes doublent le nombre de cases (48), donc les chances qu'une
  case passe l'exploration par hasard ; Benjamini-Hochberg et la
  confirmation sur des jours futurs sont là pour cela.

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
- `classiques_cases.csv` : une ligne par famille × variante × échéance ×
  période, avec le sens choisi, le taux, l'IC et le verdict ;
- `classiques_details.csv` : les cases retenues, par paire et par sens ;
- `classiques_statuts.csv` : déclenchements, épisodes, statuts des issues ;
- `classiques_donnees.csv` : les données lues, par paire et par jour ;
- `classiques_signaux.csv.gz` : chaque signal retenu, ses variantes et ses
  issues.
