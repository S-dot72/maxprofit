# Revue des graphiques à l'aveugle (`/graphiques`, 04/11)

Demandé le 04/11 :

> « Il faut chercher les micro structures ou indices qui valident ce qu'on
> voulait faire […] déterminer s'il y a un ou plusieurs trucs qui nous
> disent si ça va aller comme on le pensait. Vérifie les bougies, leurs
> types, leurs tailles, leur juxtaposition. »

Code : `maxprofit/live/graphiques.py`. Tests : `tests/test_graphiques.py`.
Lecture seule, sur la relève.

## Le tirage

- **40 gagnants et 40 perdants**, signaux ZoneH1 jouables (flux ≥ 84),
  tirés au hasard avec une graine fixe (7), puis mélangés. Leur issue est
  celle de l'entrée immédiate, validée par `/rapprochement` face au broker.
- **Décisions d'avant le 03/11 seulement.** Les confirmations de H1/H2 et
  de H_IB s'ouvrent ensuite, et leurs issues ne se regardent pas.
- **Un signal par zone** : deux signaux d'une même paire à moins de 15
  minutes n'en font qu'un, le premier.

## Les fichiers

- `graphiques_aveugles.html` : chaque graphique s'arrête à l'entrée.
  - En haut, 5 h de contexte ; en bas, les 60 dernières bougies.
  - Pointillés bleus : la zone. Trait gris : l'entrée. Le sens prévu (CALL
    ou PUT) est dans le titre.
  - Aucun résultat n'y figure.
- `graphiques_reponses.html` : les mêmes graphiques, avec les 15 minutes
  d'après sur fond gris, et le résultat.
- `graphiques_bougies.csv` et `graphiques_signaux.csv` : les mêmes données
  pour la revue hors ligne. La colonne `apres_entree` marque les bougies
  d'après.

## Le protocole

1. Pour chaque graphique aveugle, noter ce qui confirme ou infirme le trade
   prévu :
   - l'arrivée sur la zone (impulsion ou glissement, nombre de touches) ;
   - les types de bougies et leurs tailles ;
   - leur enchaînement juste avant l'entrée ;
   - la place laissée devant le prix.
2. Ensuite seulement, ouvrir les réponses, et comparer ce qu'avaient les
   gagnants que n'avaient pas les perdants.
3. Une revue visuelle fait naître des idées, elle n'en valide aucune. Tout
   indice retenu se traduit en une règle chiffrée, se vérifie sur
   l'ensemble des signaux d'octobre, puis se gèle et se confirme sur des
   données neuves.

## Lot 1 (04/11) : la revue à l'aveugle de Claude

**Anonymisation.** Le fichier des signaux joint contenait les résultats, et
ils se sont affichés. Les 80 graphiques ont donc été renumérotés au hasard
(A01 à A80), sans paire, date ni prix, avec une clé tenue à part.

**Notes figées avant la clé** : `docs/revue_graphiques_lot1_notes.txt`,
empreinte `e0d759f4604c89625546250644e4854e922c6069b4764b2dc6e17d54650101df`.

| Verdict à l'aveugle | Gagnants |
|---|---|
| confirme | 21/33 = 64 % |
| neutre | 14/31 = 45 % |
| infirme | 5/16 = 31 % |

- La comparaison confirme / infirme donne p = 0,065 (Fisher, bilatéral).
- L'indice le plus net est une bougie de rejet franche au contact : 14/20 =
  70 %.
- **Traduit en règle simple, il ne tient pas sur octobre.** 60 % des
  signaux ZoneH1 ont déjà une bougie de retournement au contact (`e_serie` =
  +1), et ils gagnent 52,1 % sur 1 079 signaux. La taille de cette bougie n'y
  change rien de façon stable.

Le lot 1 suggère qu'une lecture d'ensemble vaut peut-être quelque chose. Il
ne le prouve pas.

## Lot 2 : critères de lecture GELÉS avant de voir le lot

Les verdicts sont attribués selon ces critères, à l'aveugle, sur 80
graphiques jamais vus (`/graphiques 2` : graine 8, lot 1 et ses zones
exclus).

- **Confirme**, s'il y a les deux :
  - au contact de la zone, la bougie du signal clôture dans le sens du
    trade et montre un rejet visible : un corps qui reprend la ou les
    bougies d'approche, une longue mèche du côté de la zone, ou un doji
    suivi d'une bougie dans le sens du trade ;
  - la tendance des 5 h n'est pas contre le trade.
- **Infirme**, dans l'un de ces trois cas :
  - l'arrivée sur la zone est impulsive (plusieurs grandes bougies contre
    le trade jusqu'au contact) et la bougie du signal ne montre pas de
    rejet ;
  - le prix arrive en escalier long (une quarantaine de bougies ou plus)
    contre le trade ;
  - la tendance des 5 h est contre le trade, sans rejet.
- **Neutre** : tout le reste (zone usée, prix collé à la zone, oscillation,
  cas ambigus).

**Le test, fixé d'avance**, sur le lot 2 :
1. le taux de gain des « confirme » dépasse celui des « infirme » (Fisher
   unilatéral, p ≤ 0,05) ;
2. le taux des « confirme » dépasse le point mort de 52,08 %.

- **Si les deux passent**, la lecture devient une règle candidate. Elle est
  chiffrée, puis éprouvée sur des données neuves comme les autres pistes.
- **Sinon**, l'écart du lot 1 tenait au hasard.

**Ordre des envois** :
1. `graphiques_bougies.csv` et `graphiques_signaux.csv`, qui ne contiennent
   aucun résultat ;
2. les notes sont figées par leur empreinte ;
3. ensuite seulement, `graphiques_issues.csv`.

## Combinaisons de bougies vues au lot 1, gelées comme prédictions du lot 2

Elles ont été comptées après la révélation du lot 1 (outil :
`outils/combinaisons_graphiques.py`). Ce sont des pistes, sur des groupes
de 5 à 18 graphiques. Toutes portent sur la bougie du signal et sa voisine,
au contact de la zone. Le corps moyen est celui des 50 dernières bougies.

| Code | Combinaison | Lot 1 | Prédiction pour le lot 2 |
|---|---|---|---|
| K1 | bougie du signal dans le sens du trade, corps ≥ 1,5 fois le corps moyen | 9/11 = 82 % | gagne plus que le reste |
| K2 | englobante : avant-dernière contre, bougie du signal pour, corps plus grand | 13/18 = 72 % | gagne plus que le reste |
| K3 | grande bougie contre (≥ 1,5 fois), puis doji (corps ≤ 10 % de l'étendue) | 5/5 | gagne plus que le reste |
| K4 | longue mèche du côté de la zone (≥ 50 % de l'étendue) sur la bougie du signal | 5/15 = 33 % | gagne MOINS que le reste |
| K5 | bougie du signal qui clôture contre le trade | 6/20 = 30 % | gagne MOINS que le reste |

K4 est la seule qui se voit aussi sur des données indépendantes. Dans
`/bougies`, `marteau_fav_ancre` donnait 40,5 % (42 signaux) en
exploration, puis 38,1 % (42) en validation.

Le lot 2 donnera environ 80 graphiques : trop peu pour trancher une
combinaison rare. Il dira si le SENS se répète. Une combinaison qui se
répète sera ensuite éprouvée sur l'ensemble des signaux, puis sur des
données neuves.

## Lot 2 (08/11) : résultats

**Envoi.** Les issues sont arrivées avec les autres fichiers et se sont
affichées. La parade du lot 1 a donc été reprise :
- les 80 graphiques ont été renumérotés au hasard (A01 à A80), sans paire,
  date ni prix, avec une clé tenue à part ;
- les notes ont été écrites sur ces numéros, puis figées avant la lecture
  de la clé : `docs/revue_graphiques_lot2_notes.txt`, empreinte
  `811f9d0010f72e4c582123c6426184fe804960dcb58113253f4f352858043c78`,
  poussées sur la branche (commit 68b0802) avant la révélation.

**Deux précisions de lecture**, fixées pendant la revue, avant la clé :
- le rejet compte si le corps de la bougie du signal reprend tout le corps
  de la dernière bougie d'approche, ou si sa mèche côté zone fait au moins
  la moitié de son étendue, ou si un doji est suivi d'une bougie dans le
  sens du trade (écrite dans les notes, appliquée dès A05) ;
- pour une 2e touche après un rebond, l'arrivée se compte depuis le rebond
  (A52, A62, A67 ; non écrite dans les notes).

| Verdict à l'aveugle | Gagnants |
|---|---|
| confirme | 14/31 = 45,2 % |
| neutre | 14/22 = 63,6 % |
| infirme | 12/27 = 44,4 % |

**Le test fixé d'avance échoue sur ses deux points** :
1. confirme contre infirme : p = 0,58 (Fisher unilatéral) ;
2. les « confirme » gagnent 45,2 %, sous le point mort de 52,08 %.

Ce qui suit est descriptif, sur de petits groupes :
- avec un rejet visible, 18/36 = 50 % ; sans, 22/44 = 50 % ;
- tendance des 5 h avec le trade, 24/44 = 54,5 % ; range, 13/28 = 46,4 % ;
  contre, 3/8 = 37,5 % ;
- les « neutre » font mieux que le reste (63,6 %), mais sans que ce soit
  significatif (p = 0,21, bilatéral), et ce n'était pas prédit.

**Les combinaisons K1 à K5** (outil `outils/combinaisons_graphiques.py`,
comparées au reste du lot) :

| Code | Lot 1 | Lot 2 | Reste du lot 2 | Sens prédit retrouvé |
|---|---|---|---|---|
| K1 grande bougie pour | 9/11 | 7/14 = 50 % | 33/66 = 50 % | non |
| K2 englobante | 13/18 | 10/21 = 48 % | 30/59 = 51 % | non |
| K3 grande contre puis doji | 5/5 | 1/2 | 39/78 = 50 % | non |
| K4 longue mèche côté zone | 5/15 | 7/14 = 50 % | 33/66 = 50 % | non |
| K5 signal qui clôture contre | 6/20 | 5/10 = 50 % | 35/70 = 50 % | non |

**Conclusion.**
- La lecture visuelle, telle qu'elle a été figée, ne sépare pas les
  gagnants des perdants. L'écart du lot 1 (64 % contre 31 %) tenait au
  hasard ou à un biais de lecture.
- Aucune combinaison ne se répète, K4 comprise. L'idée d'éprouver
  « K4 inversé » est abandonnée.
- Rien de ceci ne touche la course, ni les épreuves gelées H1/H2 et H_IB,
  qui continuent.
