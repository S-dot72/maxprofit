# Étape 2 — B. Redondance de la table `payouts` (lecture seule)

Aucune compaction n'est lancée, préparée ni écrite. Les scénarios sont
**comptés** par `/stockage` (section 3 à 6), jamais appliqués.

## 4.1 Inspection (code lu)

- **Table** (`store/migrations.py`) : `payouts(ts_sec INTEGER, pair TEXT,
  payout_pct INTEGER, is_open INTEGER)`, toutes NOT NULL, clé primaire
  `(ts_sec, pair)`, index `idx_payouts_pair (pair, ts_sec)`, aucune clé
  étrangère.
- **Sens d'une ligne** : le payout affiché par le broker pour une paire à
  un instant, et si elle était ouverte ; la table couvre tout le catalogue
  (183 paires), collectées ou non.
- **Écriture** :
  - `MarketWriter.insert_payouts` (`store/market.py`), appelée par le
    collecteur à chaque relevé du catalogue (toutes les 5 min) ;
  - depuis le **20/09/2026** (a56fae8), une ligne n'est écrite que si
    `(payout_pct, is_open)` change pour la paire ; `_amorcer_payouts` relit
    le dernier état au démarrage pour ne pas tout réécrire ;
  - l'import SQLite → PostgreSQL (`outils/importer_vers_postgres.py`) a
    compressé l'historique en points de changement (« 95,1 % répétaient la
    précédente »).
- **Copies** : la relève (`store/pool.py`, règle « rien » : ON CONFLICT DO
  NOTHING) et la fusion (`store/fusion.py`) recopient toutes les lignes.

**Hypothèse** sur l'origine de répétitions dans la base actuelle, malgré
l'écriture en points de changement :
- deux collecteurs actifs en même temps (chevauchement de déploiement),
  chacun avec son propre état de déduplication ;
- des fusions de bases qui ont collecté la même période ;
- un amorçage manqué au démarrage ;
- un historique d'avant le 20/09 non compressé.

`/stockage` dit combien de répétitions il y a et où elles se trouvent :
relevés à moins de 60 s, par jour, par paire.

## Lecteurs de la table (code lu)

| Lecteur | Ce qu'il lit | Sensible aux répétitions ? |
|---|---|---|
| `MarketReader.payout_at` (`store/market.py`) | valeur en vigueur : relevé antérieur le plus proche ; aucun contrôle d'âge | **Non** |
| `PayoutsEnMemoire.payout_at` (`backtest/execution.py`), moteur de backtest | idem | **Non** |
| `MarketWriter._amorcer_payouts` | dernière ligne par paire | **Non** |
| `collect/classement.classer` (`/classement`, `outils/classer_paires.py`) | **compte les lignes** sur 7 jours : part des lignes au plafond (≥ 84), au moins 50 relevés | **Oui** |
| `outils/rejeu_plan.py` (`_payouts`) | lignes de [début − 1 jour ; fin] | **Oui** : sans ligne dans le jour qui précède, le payout est inconnu |
| `collect/inspect_data.py` | part des lignes au-dessus d'un seuil | **Oui** (diagnostic) |

**Constat annexe (Vérifié)** : `/classement` annonce une « part du temps au
payout maximum », mais depuis le 20/09 il compte des **points de
changement**. Ce n'est pas la même chose : une paire qui change souvent pèse
plus lourd. `/stockage` mesure l'écart entre part des lignes et part du temps
(section 6). C'est un défaut existant, indépendant de toute compaction.

## 4.2 Mesures (`/stockage`, section 3)

- **Totaux** : nombre de lignes, de paires, période couverte, premières
  lignes de chaque paire.
- **Changements** : payout seul, ouverture seule, les deux.
- **Catégories du cahier des charges** :
  1. répétées (même valeur que la précédente, autre horodatage) ;
  2. répétées au même horodatage : 0 par construction (clé primaire) ;
  3. identiques non consécutives (A → B → A) : par paire, les lignes qui
     changent la valeur moins les valeurs distinctes. Ce sont des
     changements, pas des répétitions ;
  4. doublons de clé métier : 0 (clé primaire), avec en plus les relevés à
     moins de 60 s du précédent ;
  5. validité indéterminable : payout hors 0-100, ouverture hors 0/1.
- **Distributions** : écart au relevé précédent (répétées et changements),
  par jour, par paire.

## 4.3 Reconstruction (`/stockage`, section 5)

Points de changement seuls, simulés en SQL sans rien écrire :
- **Valeur en vigueur** à chaque instant observé : elle doit être identique
  (0 différence attendue).
- **Âge du relevé** qui porterait cette valeur : c'est l'information
  temporelle qui disparaîtrait.
- **Dernière observation** par paire : avec les changements seuls, seule
  reste la date du dernier changement.

**Vérifié** : garder les seuls changements fait perdre la date des
observations intermédiaires. C'est une **perte fonctionnelle** pour :
- `/classement` (comptes de lignes) ;
- `rejeu_plan` (fenêtre d'un jour) ;
- toute preuve qu'une paire figurait encore au catalogue à un instant donné :
  une paire retirée du catalogue n'écrit plus rien, et seule une répétition
  distingue « inchangée » de « disparue ».

## Scénarios comptés (`/stockage`, section 4)

- **changements seuls** : toutes les répétitions retirées ;
- **ancrage par heure** : une ligne gardée par paire et par heure faite
  uniquement de répétitions ;
- **ancrage par jour** : idem par jour. Cela suffit à `rejeu_plan` ; c'est
  insuffisant pour `/classement` tel qu'il compte.

Les octets affichés sont une **borne haute** (lignes × octets physiques
moyens). La place n'est rendue au système qu'après réécriture de la table
(VACUUM FULL). Un VACUUM simple la rend réutilisable pour les écritures
futures, sans réduire la taille comptée.

## Recommandation

Conclusion provisoire : **données insuffisantes pour conclure** tant que
`/stockage` n'a pas tourné.

Si les répétitions pèsent peu, ce qui est probable puisque l'écriture est en
points de changement depuis le 20/09 et que l'import a déjà compressé
l'historique : **aucune compaction recommandée**, et la place doit être
cherchée ailleurs (lignes mortes, chemins de ticks, offre Neon).

Si elles pèsent lourd, une compaction ne serait **potentiellement sûre que
sous conditions** :
- une sauvegarde complète hors de Neon ;
- un ancrage par période qui garde ce dont les consommateurs ont besoin ;
- `/classement` corrigé d'abord pour mesurer le temps et non les lignes ;
- une comparaison avant/après des sorties de `payout_at`, `/classement` et
  `rejeu_plan`.
