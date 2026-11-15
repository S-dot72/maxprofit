# Pré-inscription : le terrain OTC est-il prévisible ? (`/terrain`, 08/11)

Demandé le 08/11 :

> « Si ZoneH1 n'est pas rentable tu dois trouver autre chose. »

Paramètres figés : `docs/preinscription_terrain.json`, empreinte
`60e6ddaf41de7d3fe7409ca753cab7f10c956861658ae64278c6fbdb47138f79`
(`etude_micro.empreinte`). La commande refuse de tourner si les paramètres
embarqués ne donnent plus cette empreinte.

## Pourquoi commencer par le terrain

Toutes les pistes tirées de ZoneH1 ont échoué. Avant d'inventer une
nouvelle stratégie, il faut savoir si le prix M1 des paires OTC se laisse
prévoir tout court. Ce prix est fabriqué par le broker :
- s'il est imprévisible à 1, 5 et 15 minutes, aucune règle ne battra le
  point mort, et il faudra changer de terrain (paires non OTC, autre
  instrument) ;
- s'il a une faille, elle doit apparaître dans au moins une famille de
  règles simples, sans ZoneH1.

## Les cinq familles

| Famille | Idée | Ce qu'une faille voudrait dire |
|---|---|---|
| F1 suite | le mouvement des 1, 5 ou 15 dernières minutes continue, ou revient | le générateur a de la mémoire à court terme |
| F2 excès | après un mouvement de plus de 3 fois l'habituel, le prix continue ou revient | il corrige ses propres excès |
| F3 meneur | une paire qui partage une devise annonce le mouvement d'une autre | les paires sont tirées d'une base commune, avec un décalage |
| F4 triangle | EURJPY s'écarte de EURUSD × USDJPY, puis revient | les crosses sont recalculées à partir des autres paires |
| F5 heure | une paire monte ou baisse plus souvent à certaines heures | le générateur a une dérive horaire |

Chaque règle est jugée aux échéances de 1, 5 et 15 minutes, à la clôture
de chaque bougie M1. Il n'y a pas de filtre de zone, ni de choix à la main.

## Les trois périodes

1. **Exploration**, du 06/10 au 02/11 inclus. Chaque case choisit son sens
   (suite ou retour, hausse ou baisse). Elle n'est retenue que si elle
   atteint le point mort avec au moins 500 décisions jouables.
2. **Validation**, du 03/11 au 09/11 inclus (jusqu'à la veille du
   lancement si celui-ci a lieu avant le 10/11). Chaque case retenue y est
   rejugée dans le même sens, contre 52,08 %, avec une correction de
   Benjamini-Hochberg à 10 % sur l'ensemble des cases jugées.
3. **Confirmation**, à partir du 10/11 : ces bougies ne sont jamais lues
   par `/terrain`. Une case découverte est d'abord gelée, puis confirmée
   sur elles.

## Ce qui compte

- **Seules les décisions jouables comptent** : celles où le payout du flux
  vaut au moins 84, soit 92 % appliqués et un point mort de 52,08 %.
- **Une égalité est une perte.** Le taux avec égalités exclues est publié
  à part.
- **Les sauts aberrants sont écartés** : une variation de plus de 1 % en
  une minute est un trou de données, pas un mouvement jouable. Les
  graphiques des lots 1 et 2 en montraient.
- **L'incertitude** vient d'un bootstrap par blocs paire × jour.

## Limites connues, écrites avant le résultat

- L'entrée est la clôture de la bougie, comme dans le proxy de ZoneH1 que
  `/rapprochement` a validé (écart médian nul). À 1 minute pourtant,
  quelques secondes de retard pèsent. Une piste à 1 minute devrait être
  vérifiée sur les ticks avant toute autre chose.
- Une semaine de validation détecte un vrai écart de l'ordre de 2 points.
  Un avantage plus fin passerait inaperçu.
- C'est une étude, pas un changement : la course, la collecte, la
  stratégie et les échéances restent telles quelles.

## Verdicts possibles

- **Piste** : une case découverte en validation. Elle est gelée, puis
  confirmée sur les bougies à partir du 10/11.
- **Terrain muet** : aucune case découverte. Aucune de ces règles ne bat
  le point mort sur l'OTC. C'est un argument fort pour changer de terrain
  plutôt que de stratégie.

## Lancer l'étude

Code : `maxprofit/live/terrain.py`. Tests : `tests/test_terrain.py` (une
base synthétique où deux failles sont plantées, et que l'étude retrouve).

- **`/terrain`**, à partir du 10/11 00:00 UTC : une seule lecture, sur la
  semaine de validation entière. Avant cette date, la commande explique
  qu'il faut attendre.
- **`/terrain partielle`** : juge tout de suite, sur les jours complets
  déjà écoulés. Ce premier jugement est alors le seul, et on ne relance pas
  pour en obtenir un meilleur.
- **`/terrain active`** : lit tout sur la base active (son quota de
  transfert), si la relève ne répond pas.

L'étude dure quelques minutes. Elle rend un rapport et deux fichiers :
- `terrain_cases.csv` : une ligne par case, avec l'exploration, la
  validation et le verdict ;
- `terrain_paires.csv` : le descriptif par paire (minutes, part jouable,
  minutes immobiles, autocorrélations, sauts), qui n'est jamais un critère.

## Résultats du 15/11

Rapport `/terrain`, 20 paires, du 06/10 au 09/11, 733 496 minutes lues dont
46,4 % jouables, en 6,9 minutes sur le serveur.

- **Aucune mémoire à court terme.** L'autocorrélation des rendements d'une
  minute reste entre −0,009 et +0,010 pour les 20 paires (médiane −0,001),
  aux retards 1 à 5.
- **1 974 cases, 220 retenues en exploration, 133 jugées en validation.**
  En exploration, elles gagnaient 55,1 % en moyenne ; en validation, 49,2 %.
  Seules 39 restent au-dessus du point mort. C'est le retour à la moyenne
  attendu quand on choisit les meilleures cases dans du bruit.
- Rien pour F1 (suite, retour), F2 (excès), F3 (meneur) ni F4 (triangle).
- **Une seule découverte BH : F5, EURJPY à 22 h UTC, à la hausse, 15 min**
  (exploration 57,9 % sur 634 ; validation 72,5 % sur 222).

**Pourquoi cette découverte est très probablement due au hasard, et ce que
le protocole avait de faible.** Pour une case d'heure (F5), les décisions
de la même heure d'un même jour se chevauchent presque entièrement à
15 minutes : l'information réelle est le nombre de JOURS, pas le nombre de
décisions. Les 222 décisions de la validation viennent de 7 jours ; 72,5 %
dit en gros « EURJPY a monté à cette heure-là 5 jours sur 7 ». Le seuil de
200 décisions ne protégeait pas contre ce chevauchement, et le bootstrap
sur 7 blocs donne un p beaucoup trop optimiste. Les heures voisines (21 h,
23 h) et les autres paires à 22 h ne montrent rien de cohérent.

**Confirmation figée** (`docs/preinscription_terrain_confirmation.json`,
empreinte `a477b555…`) :
- l'unité est le jour : le score d'un jour est la part de ses décisions
  jouables gagnées, et un jour compte s'il en a au moins 15 ;
- seules les bougies à partir du 10/11 sont lues, et aucune ne l'a été par
  `/terrain` ;
- un seul regard, sur les 20 premiers jours qui comptent. La piste est
  confirmée si leur moyenne dépasse 52,08 % avec p ≤ 0,05 (bootstrap des
  jours) ; sinon, elle est abandonnée ;
- `/terrain confirmation` dit, avant ce regard, combien de jours sont
  acquis, sans montrer aucun taux.

Leçon pour les protocoles suivants : compter les unités indépendantes
(jours, épisodes), pas les décisions qui se chevauchent.
