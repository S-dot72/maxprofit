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
