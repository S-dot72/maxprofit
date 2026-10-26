# Étape 2 — D. Schéma du journal pré-entrée et variables candidates

Rien n'est créé en production : ni table, ni collecte supplémentaire. Le
« journal » est un **jeu de données calculé hors ligne**, en mémoire ou dans
un fichier hors de Neon, à partir de ce qui est déjà stocké. Le stockage est
saturé, voir le livrable A.

## Ce qui existe, et ce qui manque (code et audits)

| Horodatage | Disponible ? | Source |
|---|---|---|
| Marché (serveur du broker, ramené en UTC) | oui, à la ms | `tick_paths` (chemins compressés) |
| Réception par le collecteur | **non** : jamais stocké | — |
| Décision du signal | oui (`ts_sec`), recalculée à chaque rejeu | `apprentissage.historique.exemples_historiques` |
| Enregistrement en base | non par tick ; à la minute (flush toutes les 2 s) | — |
| Origine du flux (socket) | **non** | — |
| Reconnexions, trous | indirectement : gels (écart ≥ 10 s), interruptions, battements (absents avant le 24/10) | audit `/ticks` |

**Limite à documenter, pas à contourner.** Sans heure de réception, on ne
peut pas prouver qu'un tick horodaté avant la décision était **reçu** avant
elle. On ne la reconstruit pas à partir de l'heure du marché. La règle de
disponibilité est donc une **garde** :
- seuls les ticks d'instant < décision − G entrent dans les variables ;
- G = 2 s par défaut, avec une sensibilité à G = 0, 2 et 5 s ;
- une variable qui change de conclusion selon G est classée fragile.

## Une ligne par (signal, sous-fenêtre)

**Identifiants**
- `id` = `paire|ts_sec`, l'identifiant stable des audits.
- `paire`, `ts_sec` (décision), `sens` (CALL/PUT).

**Zone, connue à la décision**
- `niveau` : le prix de la zone, feature du signal ZoneH1.
- `marge` = niveau × 0,02 %, la tolérance de ZoneH1 (`tolerance_pct`).
- `touches` et `entrees_deja_offertes`, features du signal.

**Bougies M1 closes**
- Les bougies dont la clôture précède la décision, jamais celle en cours.

**Fenêtre, par sous-fenêtre W de 1, 3, 5 et 15 min**
- `ticks_bruts` et `ticks_distincts` : la règle de déduplication analytique
  (paire, instant, prix), livrable C, option 4.
- `couverture` : minutes de W qui ont des ticks.
- `ecart_max_ms` : la plus longue période sans tick dans W.
- `age_dernier_tick_ms` : décision − instant du dernier tick retenu.
- `qualite` ∈ {VALID, PARTIAL, INVALID}, règles fixées avant toute
  comparaison :
  - **VALID** : couverture 100 %, aucun écart ≥ 10 s, dernier tick à ≤ 5 s
    de la garde, minutes non classées « divergence inexpliquée » ;
  - **PARTIAL** : couverture ≥ 80 % et aucun écart ≥ 30 s ;
  - **INVALID** : sinon ;
  - aucune interpolation ; une variable n'est calculée que sur une fenêtre
    de qualité suffisante pour elle.

**Variables candidates**, définies plus bas.

**Résultat, colonnes séparées, jamais lues par les variables**
- issue T0 à 900 s (l'échéance pré-inscrite de ZoneH1) ;
- payout en vigueur à la décision (`payout_at`) ;
- issues à 240 et 600 s, secondaires.

## Conventions

- Prix en **points** : unités de la grille décimale de la paire.
- Distances signées dans le sens du trade : positif = du côté favorable au
  pari.
- Les écarts et durées utilisent l'instant du broker, en ms.
- `d(t)` = distance signée du prix à `niveau`, en marges :
  (prix − niveau) × s / marge, avec s = +1 pour un CALL, −1 pour un PUT.

## Variables candidates — définition, disponibilité, risque

Ticks distincts de W, notés p_1 … p_n aux instants t_1 < … < t_n
< décision − G. Ce sont des **candidates**, pas des filtres.

| Famille | Variable | Définition | Données | Manquant | Fuite | Coût |
|---|---|---|---|---|---|---|
| A — approche | `dist_debut`, `dist_fin` | d(t_1), d(t_n) | ticks, niveau | NaN si n = 0 | aucune (avant décision − G) | O(n) |
| A | `vitesse` | (d(t_n) − d(t_1)) ÷ (t_n − t_1), en marges par minute | idem | NaN si n < 2 | aucune | O(n) |
| A | `acceleration` | vitesse sur la 2e moitié de W − vitesse sur la 1re | idem | NaN si une moitié a moins de 2 ticks | aucune | O(n) |
| A | `contacts` | nombre d'entrées dans [−1 ; +1] marge, depuis l'extérieur | idem | 0 si aucun | aucune | O(n) |
| A | `temps_dans_zone` | durée cumulée avec \|d\| ≤ 1, chaque tick valant jusqu'au suivant | idem | 0 | aucune | O(n) |
| A | `penetration_max` | max de −d(t), en marges | idem | 0 | aucune | O(n) |
| A | `depuis_dernier_contact` | décision − instant du dernier tick avec \|d\| ≤ 1 | idem | NaN si aucun contact | aucune | O(n) |
| B — régime | `vol_realisee` | √(Σ (Δp)²) en points, sur ticks distincts | ticks | NaN si n < 2 | aucune | O(n) |
| B | `amplitude` | max p − min p, en points | ticks | NaN | aucune | O(n) |
| B | `part_variations_nulles` | part des Δp = 0 entre ticks distincts consécutifs | ticks | NaN | aucune | O(n) |
| B | `efficience` | \|p_n − p_1\| ÷ Σ\|Δp\| | ticks | NaN si Σ = 0 | aucune | O(n) |
| B | `ratio_vol` | vol_realisee sur 1 min ÷ vol_realisee sur 15 min, ramenées à la minute | ticks | NaN | aucune | O(n) |
| B | `concentration` | part de Σ\|Δp\| portée par les 5 % plus grands \|Δp\| | ticks | NaN | aucune | O(n log n) |
| C — retournements | `retournements` | changements de signe de Δp, en ignorant les Δp = 0 | ticks | NaN | aucune | O(n) |
| C | `persistance` | longueur moyenne des séquences de même signe | ticks | NaN | aucune | O(n) |
| C | `part_sens_trade` | Σ max(0, s·Δp) ÷ Σ\|Δp\| | ticks, sens | NaN | aucune | O(n) |
| C | `bruit` | efficience × (1 − part_variations_nulles) | ticks | NaN | aucune | O(n) |
| D — zone | `traversees` | passages de d de part et d'autre de 0 | ticks, niveau | 0 | aucune | O(n) |
| D | `sorties_reintegrations` | sorties de [−1 ; 1] suivies d'un retour, dans W | idem | 0 | aucune | O(n) |
| E — qualité | `couverture`, `ecart_max_ms`, `age_dernier_tick_ms`, `part_doublons`, proximité d'un gel ou d'une interruption (≤ 15 min) | voir plus haut | audit | — | aucune | O(n) |

**Exclues par construction**, car postérieures à la décision ou
inobservables :
- rupture de la zone après l'entrée, MFE, MAE, retour dans la zone,
  résultat ;
- ordre d'arrivée et latence de réception (non stockés).

**La famille E sert à qualifier les fenêtres et à mesurer les biais**, pas à
prédire. Une anomalie technique ne devient pas un filtre de marché.

**Remarque** : la largeur de zone de ZoneH1 est une proportion fixe du prix
(0,02 %). La variable « largeur » du cahier des charges n'existe donc pas
comme telle : elle se réduit au niveau de prix de la paire.

## Coût de calcul estimé

Environ 750 signaux par semaine. Les 15 min avant chaque signal font environ
1 800 ticks distincts. Tout se calcule sur les chemins déjà lus par le rejeu
de `/ticks` (environ 0,2 ms par minute pour la passe doublons), avec un ordre
de grandeur de quelques secondes par semaine de signaux.

**Aucune écriture en base** : les fiches partent en CSV, comme celles de
l'audit.
