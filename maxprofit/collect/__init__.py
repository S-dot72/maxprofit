"""
Couche Collecte : enregistrer ce qui passe, fidèlement.

Ne fait JAMAIS : analyser, filtrer sur des critères de stratégie, décider.
Toute logique de décision ici figerait les règles dans les données collectées et
rendrait le backtest circulaire (spec §0).

Le filtrage par payout minimal qu'opère le collecteur n'est pas une exception :
il ne décide de rien, il choisit à quelles paires s'abonner. L'historique
COMPLET des payouts est écrit en base, ouvertes ou non, pour que le backtest
puisse rejouer l'éligibilité telle qu'elle était à l'instant T (spec §2.3).

--- Dette connue, à solder à l'étape 1 (persistance) -------------------------

Ce code est antérieur à l'étape 0 et viole encore trois points de la spec §1.
Il est déplacé tel quel, sans réécriture, pour que l'étape 0 ne mélange pas
restructuration et correction :

1. §1.1 — `Config.db` et `--db` ont une valeur par défaut relative
   ("market_data.db"), qui crée la base à côté du code. Doit devenir
   `TRADING_DB_PATH` via `maxprofit.core.config.db_path()`, sans repli.
2. §1.3 — `Storage.__init__` exécute `SCHEMA` à chaque démarrage sans
   `PRAGMA user_version` ni migrations versionnées.
3. §1.4 — aucune sauvegarde automatique (`VACUUM INTO`).

Également ouvert : `refresh_pairs()` est appelée deux fois au démarrage
(une fois par `run()`, une fois par `_consume()` dont le minuteur part à 0),
ce qui écrit deux relevés de payouts à une seconde d'intervalle ; `--min-payout`
a un défaut silencieux de 92 (§5) ; et la
colonne SQL `candles.ts` ne porte pas le suffixe `_sec` de la convention (§5) —
elle est mappée sur `Candle.ts_sec` à la frontière, et sera renommée par une
migration copie-table à l'étape 1 plutôt que par un ALTER destructeur.
"""
