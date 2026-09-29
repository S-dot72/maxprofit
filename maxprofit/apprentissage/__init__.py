"""
L'apprentissage : ce que les pertes enseignent, et seulement ce qu'elles
prouvent.

--- Ce que ce paquet fait ---------------------------------------------------

1. Il décrit chaque signal comme un trader le regarderait au moment d'entrer
   (`contexte`) : la tendance, l'élan des dernières minutes, la volatilité,
   les bougies contraires, l'usure de la zone.
2. Il rejoue la stratégie sur l'historique collecté (`historique`) et note,
   pour chaque signal, son contexte et son issue. Des milliers d'exemples,
   pas trois pertes.
3. Il en tire des LEÇONS (`lecons`) : un contexte n'est écarté que s'il perd
   de façon ÉTABLIE sur une première période et que cela se CONFIRME sur la
   suivante, qu'il n'a pas vue.
4. Après chaque session perdue, il en fait l'AUTOPSIE : ce qui, dans les pas
   perdus, relève d'un contexte connu pour perdre, et ce qui relève de la
   variance.

--- ⚠ Ce qu'il ne fait pas, et pourquoi (SPEC §3.4, §3.5) --------------------

Il ne tire AUCUNE règle d'une perte isolée. « Ne plus refaire l'erreur » après
chaque perte, c'est fabriquer une règle par perte : chacune écarte un contexte
qui a perdu une fois par hasard, le débit s'effondre, et le taux de réussite
ne bouge pas. La granularité minimale d'une conclusion est la centaine de
trades. Une leçon qui ne se confirme pas hors échantillon n'est pas appliquée.

Il ne crée pas d'avantage là où il n'y en a pas : si aucun contexte ne perd
de façon établie, il le dit, et c'est un résultat.
"""
