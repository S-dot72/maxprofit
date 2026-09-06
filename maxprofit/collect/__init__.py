"""
Couche Collecte : enregistrer ce qui passe, fidèlement.

Ne fait JAMAIS : analyser, filtrer sur des critères de stratégie, décider.
Toute logique de décision ici figerait les règles dans les données collectées et
rendrait le backtest circulaire (spec §0).

Le filtrage par payout minimal qu'opère le collecteur n'est pas une exception :
il ne décide de rien, il choisit à quelles paires s'abonner. L'historique
COMPLET des payouts est écrit en base, ouvertes ou non, pour que le backtest
puisse rejouer l'éligibilité telle qu'elle était à l'instant T (spec §2.3).

La persistance ne vit PAS ici : elle est dans `maxprofit.store`, pour que le
backtest puisse lire les tables de marché sans importer cette couche et sans
pouvoir y écrire. Le collecteur y accède par un `MarketWriter`.

Dette de l'étape 0 : soldée à l'étape 1. Le chemin de base vient désormais de
`TRADING_DB_PATH` sans repli, le schéma est migré par `PRAGMA user_version`,
les sauvegardes tournent toutes les 6 heures, `--min-payout` est obligatoire, et
`refresh_pairs()` n'est plus appelée deux fois au démarrage.
"""
