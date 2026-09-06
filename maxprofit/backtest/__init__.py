"""
Couche Backtest : rejoue l'historique et mesure. N'écrit JAMAIS dans les tables
de marché (`ticks`, `candles`, `payouts`, `uptime`) — elle n'y a accès qu'en
lecture. Ses écritures vont dans `experiments`, `evaluations`, `outcomes`.

Vide pour l'instant : étape 4 de la spec §4, conditionnée par les étapes 1 à 3.
Le moteur n'est pas crédible tant que les cinq tests-oracles (§2.7) ne passent
pas.
"""
