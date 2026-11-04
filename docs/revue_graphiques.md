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
