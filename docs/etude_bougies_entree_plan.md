# Étude des bougies avant l'entrée — plan et passe `/bougies`

Demandé le 03/11 :

> « Il faut prendre en compte sur M1 la tendance haussière, baissière ou
> range. Une fois la tendance établie on analyse les 50 bougies précédant le
> point d'entrée ou la nouvelle touche sur le support ou résistance. On
> cherche s'il y a un ou plusieurs types de bougie en particulier qui se
> forment, on vérifie la taille des bougies, est-ce qu'elles sont vendeuses
> ou acheteuses, est-ce qu'avant l'entrée ce sont les vendeurs ou les
> acheteurs qui sont puissants à ce moment. »

Puis, le même jour :

> « Dans une tendance haussière le creux est plus haut que le creux
> précédent, on a des higher high et des higher low. Dans une tendance
> baissière on a des creux plus bas que le creux précédent, des lower high
> et lower low. Dans un range on n'a pas ces deux, le marché stagne. Les
> autres c'est à toi de les déterminer dans les données. »

**Statut** : la passe est écrite (`maxprofit/live/bougies_entree.py`,
commande `/bougies`, admin, en tâche de fond). Son issue ne vaut que si
`/rapprochement` montre que l'issue mesurée depuis l'entrée confirmée
s'accorde avec le règlement du broker.

Lecture seule ; la course, ses règles d'entrée et ses expirations ne
changent pas.

## 1. Ce qui diffère de l'étude du 01/11

L'étude du 01/11 mesurait 92 grandeurs de **ticks**, **au moment du
signal**, et jugeait l'issue depuis la bougie du signal. Celle-ci :
- mesure les **bougies M1**, dans leur **régime de tendance** ;
- se place **à l'entrée réelle** de la course (fin de la bougie de
  confirmation M1) et, en second, à la **touche** de la zone (bougie du
  signal) ;
- juge l'issue **depuis l'entrée réelle**.

## 2. La tendance M1 (définition de l'utilisateur, 03/11)

| Tendance | Sommets | Creux |
|---|---|---|
| haussière | plus hauts que le précédent (higher high) | plus hauts que le précédent (higher low) |
| baissière | plus bas (lower high) | plus bas (lower low) |
| range | tout autre cas : le marché stagne | |

- **Sommets et creux** : les deux derniers pivots CONFIRMÉS de chaque sorte
  du ZigZag de la stratégie (`zones_zigzag.pivots`). Un pivot n'existe qu'à
  partir de la bougie où le retournement l'a établi, ce qui garantit que
  rien n'est lu dans l'avenir.
- **Horizon** : les **300 bougies M1 (5 h)** qui finissent à l'entrée. Ce
  ZigZag trace environ huit pivots par tranche de cinq heures. Cet horizon
  donne donc deux sommets et deux creux à comparer, ce que 2 h ne
  donneraient pas.
- **Fenêtre insuffisante** : une fenêtre trouée de plus de 10 %, ou qui n'a
  pas deux sommets et deux creux, est « indéterminée ». Elle est comptée à
  part.
- **Orientation** : la tendance est rapportée au trade, AVEC, CONTRE ou
  RANGE.
- **Sensibilité** : EMA 20 face à EMA 50, neutre sous 0,05 % d'écart. Elle
  est publiée à côté, et ne remplace jamais la structure.

## 3. Les 50 bougies avant l'ancre

**La fenêtre** : les 50 dernières bougies M1 complètes, bougie d'entrée
comprise. Elle est sans trou : une fenêtre trouée est écartée et comptée.
Chaque mesure est aussi prise sur les **10** et les **5** dernières
bougies, car ce qui se passe juste avant l'entrée compte plus que le
début de la fenêtre.

Tout est orienté dans le sens du trade : « camp du trade » veut dire
acheteurs pour un CALL, vendeurs pour un PUT. Les tailles sont exprimées en
**amplitudes M1 moyennes** (moyenne des étendues des 50 bougies), pour être
comparables d'une paire à l'autre.

### 3.1 Les types de bougies (catalogue figé)

Les règles sont fixes, identiques pour toutes les paires. Les quatre
premières sont celles de `potentiel.motif`.

| Type | Règle |
|---|---|
| englobante | le corps englobe strictement celui de la précédente, de couleur opposée |
| marteau | corps non nul, mèche basse ≥ 2 corps, mèche haute ≤ 1 corps |
| étoile filante | l'inverse du marteau |
| doji | corps ≤ 10 % de l'étendue |
| marubozu | corps ≥ 80 % de l'étendue |
| toupie | corps ≤ 30 % de l'étendue, chaque mèche ≥ 25 % |
| bougie incluse (inside bar) | plus haut et plus bas à l'intérieur de la précédente |
| bougie de rejet | mèche du côté de la zone ≥ 60 % de l'étendue |

Chaque type est rangé selon le camp qu'il sert (du trade ou adverse). On
mesure :
- son nombre sur 50, sur 10 et sur 5 bougies ;
- sa présence sur la bougie d'entrée et sur la bougie de la touche.

### 3.2 La taille des bougies

- Étendue et corps moyens, dans le camp du trade et dans le camp adverse.
- Taille des 5 dernières face à la moyenne des 50 : accélération ou
  essoufflement.
- Plus grande bougie adverse des 10 dernières.

### 3.3 Le rapport de force acheteurs / vendeurs

- **Part des bougies du camp du trade**, sur 50, 10 et 5 bougies.
- **Pression des corps** : somme des corps du camp du trade moins somme des
  corps adverses, divisée par la somme des étendues (de −1 à +1).
- **Pression des mèches** : mèches de rejet favorables moins mèches
  défavorables, sur la même échelle.
- **Position de la clôture** dans la bougie, en moyenne : 0 au plus bas, 1
  au plus haut, orientée dans le sens du trade.
- **Série en cours** : bougies consécutives d'un même camp jusqu'à
  l'entrée.

Le nombre de ticks par bougie n'est pas utilisé : il est faussé par la
double réception.

## 4. Hypothèses : une seule a priori, le reste déterminé par les données

**P1, fixée à l'avance** : les trades AVEC la tendance gagnent plus que
les trades CONTRE elle. Elle est jugée sur l'exploration, sur la
validation et sur l'ensemble. La sensibilité EMA et les jouables sont
publiés à côté.

**Le reste est déterminé dans les données, sans tricher.** Les 127
variables (63 à l'entrée, 63 à la touche, plus `confirmation_fav`) suivent
le circuit de l'étape 3 :
1. **sur l'exploration (avant le 21/10)**, pour chaque variable, on fixe
   le groupe comparé et son sens :
   - le groupe est le tercile haut contre le tercile bas, ou, quand les
     terciles se confondent (figures rares), la présence contre l'absence ;
   - le sens est celui que montrent les données de l'exploration ;
2. **sur la validation (21 au 28/10)**, ce même groupe est jugé dans ce
   même sens. On publie l'écart, son IC par blocs paire × jour et sa p
   unilatérale. La p vient de l'approximation normale de l'écart-type
   bootstrap : 1 000 tirages ne descendent pas sous 0,001, alors que BH
   sur 127 tests demande moins ;
3. **Benjamini-Hochberg à 10 %** sur toutes les variables jugées. Un
   groupe de moins de 30 signaux, dans une période, n'est pas jugé.

Une **découverte** reste une piste. Elle ne devient une règle qu'après un
gel (groupe, sens, bornes) et une confirmation sur des données
postérieures.

Le rapport montre aussi ce que deviennent en validation les 5 plus forts
écarts de l'exploration. C'est là que se voit un effet qui tient, ou un
effet qui s'effondre.

## 5. Issue, données, découpage

**Issue principale.** Clôture de la bougie d'entrée face à la clôture de
la bougie qui commence 900 s plus tard. C'est `gagne_confirme` du rejeu.
Elle n'est retenue que si `/rapprochement` montre qu'elle s'accorde avec le
règlement du broker. Sinon, l'étude n'est pas lancée.

**Issue secondaire.** Le résultat réel des ordres appariés. Il est
descriptif : les ordres sont trop peu nombreux pour tester.

**Population.** Les signaux ZoneH1 du rejeu dont la confirmation M1 est
obtenue, toutes paires et tous payouts. Les figures de bougies ne
dépendent pas du payout, et c'est ce qui donne de la puissance. Le
rendement est rapporté à part sur les jouables (flux ≥ 84).

**Découpage.**
- Recherche sur octobre : exploration jusqu'au 21/10, validation du 21 au
  28/10, comme l'étape 3.
- Puis gel des paramètres, et confirmation sur des données postérieures au
  gel.

**Incertitude.** Bootstrap par blocs paire × jour, comme partout ailleurs.

**Puissance (ordre de grandeur).** Les hypothèses comparent deux groupes,
par exemple avec et contre la tendance. Avec deux moitiés égales, un test
bilatéral à 5 %, une puissance de 80 % et un effet de plan de 1,6 :

| Écart entre les deux groupes | Signaux nécessaires |
|---|---|
| 58 % contre 50 % (8 points) | ≈ 1 900 |
| 57 % contre 51 % (6 points) | ≈ 3 500 |
| 56 % contre 52 % (4 points) | ≈ 7 800 |

Octobre fournit au plus 1 800 signaux, et moins une fois la confirmation M1
exigée : le taux de confirmation reste à mesurer. Une recherche sur
octobre ne peut donc montrer qu'un écart d'environ 8 points ou plus. Un
écart plus petit ne se tranche que sur des semaines de données neuves.

## 6. La passe `/bougies`

- **Lecture seule**, sur la **relève** ; `/bougies active` lit sur l'active.
  - Elle lit les **bougies M1**, sans les ticks, une paire et un jour à la
    fois.
  - Chaque paire × jour est vérifié face à l'active (comptes et sommes
    calculés par le serveur). Un jour divergent est relu sur l'active, et
    compté.
  - Elle lit aussi le payout du flux avant chaque décision, sur l'active.
- **Signaux** : le rejeu quotidien en mémoire, confirmés en M1 au mode
  actuel de la course. Seules les décisions d'avant le 28/10 sont lues,
  car la confirmation du gel 2 est ouverte.
- **Issue** : recalculée sur les bougies, et contrôlée face au
  `gagne_confirme` du rejeu.
- **Sorties**, en CSV seulement :
  - `bougies_signaux.csv` : un signal par ligne, avec sa tendance, ses
    sommets et creux, son orientation et les mesures `e_` et `t_` ;
  - `bougies_tests.csv` : P1, puis chaque variable, avec son groupe, son
    sens, l'exploration, la validation et le verdict ;
  - `bougies_tendance.csv` : le taux de gain par orientation et par
    période, avec un IC.
- Aucune table dérivée, aucune écriture.
- Tests : `tests/test_bougies_entree.py`.

## 7. Résultat du 03/11 et suite : la piste H_IB

Résultats de `/bougies` du 03/11, sur 1 790 signaux du 10 au 28/10, en
mode 0 (la touche y est l'entrée) :
- **P1 n'est pas soutenue.** Avec la tendance : 52,8 % ; contre : 56,1 % ;
  range : 50,8 %. Les deux périodes se contredisent.
- **Une seule découverte** parmi 64 mesures distinctes : le nombre de
  **bougies incluses** sur les 50 bougies avant le signal. Moins de 5 : 58,2 %
  puis 60,5 % ; 6 ou plus : 51,1 % puis 45,0 % (p = 0,0007).
  - Elle tient à l'intérieur des paires et dans les trois régimes.
  - Elle n'est corrélée à aucune autre mesure.

La piste est **gelée** (`docs/preinscription_incluses.md`, empreinte
`ccc23575…3d36b1e`). L'épreuve se fait par `/incluses` (admin, lecture
seule, `maxprofit/live/incluses.py`) :
- réplication sur la quarantaine, calculée dès maintenant ;
- confirmation à partir du 04/11, regards à 300, 600 et 900 signaux
  favorables et jouables ;
- aucun résultat de la confirmation n'est montré avant le premier regard,
  ni dans le rapport ni dans les fichiers ;
- les signaux sont recalculés sur les bougies : l'épreuve ne dépend pas
  de la fenêtre de 30 jours du rejeu.

`/bougies` corrigé pour le mode 0 : les variables de la touche ne sont plus
testées en double, et le contrôle de l'issue compare à la même entrée.
