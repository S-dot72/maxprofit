"""
Couche d'exécution : démarre les processus et expose la sonde attendue par
l'hébergeur. Aucune logique métier — elle assemble ce que les autres couches
fournissent, et c'est la seule raison pour laquelle elle a le droit d'importer
à la fois `collect` et `store`.
"""
