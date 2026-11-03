# Étude des bougies avant l'entrée — plan (BROUILLON, non gelé)

Demandé le 03/11 :

> « Il faut prendre en compte sur M1 la tendance haussière, baissière ou
> range. Une fois la tendance établie on analyse les 50 bougies précédant le
> point d'entrée ou la nouvelle touche sur le support ou résistance. On
> cherche s'il y a un ou plusieurs types de bougie en particulier qui se
> forment, on vérifie la taille des bougies, est-ce qu'elles sont vendeuses
> ou acheteuses, est-ce qu'avant l'entrée ce sont les vendeurs ou les
> acheteurs qui sont puissants à ce moment. »

**Statut** : brouillon. Aucune donnée n'a été lue pour l'écrire. Il n'est
lancé qu'après deux choses :
1. le rapport de `/rapprochement`, qui doit montrer que l'issue mesurée
   depuis l'entrée confirmée colle aux résultats du broker ;
2. la validation des définitions du §2 à §4, qui reprennent la méthode de
   trading de l'utilisateur.

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

## 2. La tendance M1 (proposition, à valider)

Mesurée sur les **120 bougies M1 (2 h)** qui finissent à l'ancre, puis
orientée par rapport au trade : **avec la tendance**, **contre la
tendance**, **range**.

- **Définition principale : la structure.** Elle se lit sur les deux
  derniers sommets et les deux derniers creux confirmés du ZigZag, celui de
  la stratégie (`zones_zigzag.pivots`), qui est causal.
  - sommets et creux montants : haussière ;
  - sommets et creux descendants : baissière ;
  - sinon : range.
- **Sensibilité : les moyennes.** EMA 20 face à EMA 50, avec un écart de
  moins de 0,05 % classé neutre. C'est la règle déjà écrite dans
  `potentiel.py`.

Le résultat de la sensibilité est publié à côté du principal. Il ne le
remplace jamais.

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

## 4. Hypothèses principales (proposition, à valider)

Elles sont **peu nombreuses**, et leur sens est fixé avant de lire une seule
issue. Chacune traduit une règle de trading, pas une trouvaille. Les 92
grandeurs du 01/11 ont montré ce que coûte l'inverse : à force de tester,
le hasard finit par ressembler à un pattern.

| | Hypothèse | Sens attendu |
|---|---|---|
| P1 | trades avec la tendance M1 face aux trades contre elle | avec > contre |
| P2 | pression des corps du camp du trade sur les 10 dernières bougies | plus elle est forte, plus on gagne |
| P3 | figure de confirmation du camp du trade (englobante, marteau ou étoile, rejet) sur la touche ou l'entrée | présente > absente |
| P4 | grandes bougies adverses juste avant l'entrée (plus grande des 10 dernières ≥ 2 amplitudes) | présente < absente |

- **Par régime de tendance**, P2 à P4 sont aussi lues dans chacun des trois
  régimes. C'est la logique « une fois la tendance établie ». Ces lectures
  sont secondaires : couper l'échantillon en trois divise la puissance.
- **Le reste du §3 est exploratoire**, avec correction pour comparaisons
  multiples. Une trouvaille exploratoire est une piste, pas une règle.

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

## 6. Mise en œuvre envisagée

- Une passe en lecture seule, du même type que `/micro_selection`. Elle
  lit les **bougies M1** sur la relève, sans les ticks : la lecture est
  légère.
- Sorties : CSV seulement. Un signal par ligne, avec toutes les mesures
  du §3, le régime, l'issue et l'appartenance aux hypothèses ; plus un
  registre des tests.
- Aucune table dérivée, aucune écriture.

## 7. Décisions attendues de l'utilisateur

1. **La tendance.** Structure des sommets et creux (proposée) ou moyennes
   mobiles ? Sur 2 h, ou un autre horizon ? Qu'appelles-tu « range » ?
2. **Les types de bougies.** Le catalogue du §3.1 te convient-il ?
   Lesquels regardes-tu en premier ?
3. **La taille.** Des grandes bougies dans le sens du trade juste avant
   l'entrée, c'est pour toi de la force (on gagne plus) ou de
   l'essoufflement (on gagne moins) ?
4. **L'ancre.** L'entrée réelle (proposée en principal) et la touche en
   second, ou l'inverse ?
