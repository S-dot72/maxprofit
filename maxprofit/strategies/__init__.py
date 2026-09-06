"""
Le SEUL endroit où une sous-classe de `Strategy` a le droit d'exister.

Invariant n°1 (spec §0) : le code de stratégie est identique en backtest et en
live. Les deux moteurs importent depuis ce paquet ; aucun des deux ne définit
de logique de décision. `tests/test_layering.py` refuse toute sous-classe de
`Strategy` déclarée hors de ce répertoire.

Vide pour l'instant : la première stratégie arrive à l'étape 5 de la spec §4,
après les indicateurs sans repeint (étape 3) et le moteur de backtest (étape 4).
"""
