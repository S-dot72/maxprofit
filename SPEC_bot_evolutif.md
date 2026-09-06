# Spécification technique — Bot de signaux évolutif

Document de référence destiné à être fourni comme contexte à Claude Code.
Il définit l'architecture, les invariants et les critères d'acceptation.
Il ne contient pas l'implémentation complète : il contient ce qu'il ne faut pas
se tromper, parce que ces erreurs-là ne se voient pas avant plusieurs semaines.

---

## 0. Principe directeur

Le système a trois responsabilités séparées par des frontières strictes :

| Couche | Rôle | Ne fait jamais |
|---|---|---|
| Collecte | Enregistrer les ticks, payouts, uptime | Analyser, filtrer, décider |
| Backtest | Rejouer l'historique, mesurer | Écrire dans les tables de marché |
| Live | Émettre des signaux | Contenir sa propre copie de la stratégie |

**Invariant n°1 : le code de stratégie est identique en backtest et en live.**
Une seule classe `Strategy`, une seule méthode `on_bar(view) -> Signal | None`.
Deux implémentations divergeront, toujours, et le backtest deviendra un mensonge.

---

## 1. Persistance — le problème de la base écrasée

C'est un bug de conception, pas un accident. Quatre causes possibles, et les
quatre garde-fous correspondants. Les quatre sont obligatoires.

### 1.1 Le fichier de données sort du répertoire de code

```
~/trading_data/market.db        <- données, jamais touchées par un déploiement
~/trading_data/backups/
~/projets/bot/                  <- code, git, remplacé à chaque update
```

Chemin lu depuis une variable d'environnement `TRADING_DB_PATH`, avec
échec explicite au démarrage si elle est absente. Pas de valeur par défaut
relative. `*.db`, `*.db-wal`, `*.db-shm` dans `.gitignore`.

### 1.2 Aucune instruction destructrice dans le chemin normal

Interdits dans tout code exécuté au démarrage : `DROP TABLE`, `DELETE FROM`
sans `WHERE`, `TRUNCATE`, `create_all(drop=True)`, `os.remove` sur la base.

Ces instructions n'existent que dans un script séparé `reset_db.py`, qui exige
un argument `--i-understand-this-deletes-everything` et une saisie manuelle du
nom de la base. Ce script n'est jamais importé par le reste du projet.

### 1.3 Migrations versionnées, en avant seulement

`PRAGMA user_version` porte le numéro de schéma. Au démarrage :

```
version_en_base = PRAGMA user_version
si version_en_base > VERSION_CODE  -> ARRÊT (code plus vieux que la base)
si version_en_base < VERSION_CODE  -> appliquer les migrations manquantes,
                                      une par une, chacune dans sa transaction
```

Une migration ne peut faire que : `CREATE TABLE IF NOT EXISTS`,
`ALTER TABLE ... ADD COLUMN`, `CREATE INDEX IF NOT EXISTS`, ou une copie
table → nouvelle table → renommage. Jamais de suppression de colonne, jamais de
recréation « pour repartir propre ». Une colonne devenue inutile est laissée en
place. Le coût d'une colonne morte est nul ; le coût de trois semaines de
collecte perdues ne l'est pas.

Les migrations sont numérotées et immuables : une fois qu'une migration a été
appliquée en production, on ne la modifie plus, on en ajoute une nouvelle.

### 1.4 Sauvegardes automatiques

Toutes les 6 heures, `VACUUM INTO 'backups/market_YYYYMMDD_HHMM.db'`.
Cette commande produit une copie cohérente sans arrêter le collecteur.
Rétention : 7 quotidiennes + 4 hebdomadaires. Une restauration doit être testée
une fois, à vide, avant de faire confiance au dispositif.

### Critère d'acceptation

Écrire un test qui : crée une base, insère 100 lignes, simule un déploiement
(nouvelle version du code, migration ajoutée), redémarre, et vérifie que les
100 lignes sont toujours là et que la nouvelle colonne existe.
Ce test tourne en CI à chaque commit.

---

## 2. Le moteur de backtest

« Fonctionne à 100 % » n'est pas démontrable. Ce qui est atteignable, c'est un
moteur qui **échoue bruyamment** sur les erreurs classiques au lieu de produire
silencieusement des résultats flatteurs. Voici les sept pièges, par ordre de
gravité.

### 2.1 Le look-ahead bias — et le problème spécifique du ZigZag

C'est ce qui tue la quasi-totalité des backtests d'amateurs, et votre stratégie
y est particulièrement exposée.

**Le ZigZag est un indicateur repeignant.** Un pivot n'est confirmé qu'après un
retournement d'amplitude suffisante, donc plusieurs bougies plus tard. Si le
backtest lit `zigzag[t]` calculé sur la série complète, il utilise une
information qui n'existait pas à l'instant t. Le résultat sera spectaculaire et
entièrement faux.

**Les fractales de Chaos ont le même défaut** : une fractale à l'indice t n'est
confirmée qu'à t+2 (deux bougies suivantes nécessaires).

Deux conditions sur six sont donc concernées. Traitement obligatoire :

- Implémenter ZigZag et fractales avec leur **latence de confirmation explicite**.
  La fonction retourne, pour chaque instant t, uniquement les pivots déjà
  confirmés à t. Comparer les deux versions sur un même jeu de données : l'écart
  de performance entre la version repeignante et la version honnête vous donnera
  la mesure directe de l'illusion.

**Garde-fou structurel** : la stratégie ne reçoit jamais un DataFrame complet.
Elle reçoit un objet `MarketView` qui n'expose que `view.candles(n)` (les n
dernières bougies jusqu'à t inclus) et `view.now`. L'accès au futur devient
physiquement impossible, pas simplement déconseillé.

### 2.2 Réalisme d'exécution

- **Latence** : mesurer réellement le délai signal → clôture de bougie → message
  Telegram → lecture → clic. Chronométrer 20 fois. Utiliser la médiane, pas le
  minimum. Ce paramètre est dans la config du backtest et doit apparaître dans
  chaque rapport.
- **Prix d'entrée** : premier tick disponible à `t_signal + latence`, jamais le
  prix de clôture de la bougie de signal.
- **Prix de règlement** : dernier tick à `t_entrée + expiration`, depuis la table
  `ticks`. Si aucun tick dans une fenêtre de ±2 s, le trade est marqué
  `unresolvable` et **exclu des statistiques**, pas compté comme perdant.
- **Égalité** : vérifier la règle du broker (remboursement de la mise en général)
  et la coder explicitement. Ne pas l'ignorer : sur des paires peu volatiles à
  5 décimales, les égalités existent.

### 2.3 Payout d'époque

Le gain d'un trade se calcule avec le payout **en vigueur à l'instant du trade**,
lu dans la table `payouts` par jointure sur l'horodatage antérieur le plus
proche. Un trade sur une paire qui n'était pas éligible à cet instant n'est pas
généré du tout. Utiliser le payout d'aujourd'hui pour un trade d'il y a deux
semaines est un biais silencieux qui gonfle les résultats.

### 2.4 Qualité des données

Le backtest refuse de générer un signal si, dans la fenêtre d'indicateurs :
une bougie est `complete = 0`, ou `tick_count < 5`, ou un trou d'uptime
chevauche la période. Chaque exclusion est comptée et le rapport affiche le
pourcentage de bougies écartées. Si ce pourcentage dépasse 10 %, la collecte
est le problème, pas la stratégie.

### 2.5 Découpage temporel et walk-forward

Jamais de découpage aléatoire — les séries temporelles sont autocorrélées, un
split aléatoire fait fuiter le futur dans le passé.

```
Fenêtre 1 : calibrer sur J1-J14   -> tester sur J15-J18
Fenêtre 2 : calibrer sur J5-J18   -> tester sur J19-J22
Fenêtre 3 : calibrer sur J9-J22   -> tester sur J23-J26
```

Le seul chiffre qui compte est l'agrégat des périodes de test. La performance
sur la période de calibration n'est jamais rapportée comme un résultat.

### 2.6 Comptage des hypothèses

Table `experiments` : chaque exécution de backtest enregistre le commit git, le
hash du jeu de données, les paramètres complets, et les métriques. Non
modifiable, non supprimable.

Utilité réelle : après 60 variantes testées, il devient normal d'en trouver
quelques-unes à 58 % de réussite par pur hasard. Le compteur d'expériences est
ce qui vous permet de le savoir. Sans lui, vous confondrez chance et découverte.

### 2.7 Les tests-oracles — la vraie réponse à « backtest correct »

On ne prouve pas la justesse d'un moteur de backtest. On le soumet à des cas
dont on connaît la réponse à l'avance. Ces cinq tests sont bloquants :

1. **Entrées aléatoires.** 5 000 trades tirés au hasard, direction aléatoire.
   Résultat attendu : espérance ≈ `0,5 × 0,92 − 0,5 = −4 %` par trade, taux de
   réussite dans l'intervalle 50 % ± 1,4 %. Si le moteur affiche un profit,
   **il est cassé** — c'est le test qui attrape le look-ahead.
2. **Clairvoyance.** Stratégie qui lit le prix futur de règlement. Résultat
   attendu : 100 % de réussite, rendement ≈ +92 % par trade. Vérifie que la
   résolution des trades et le calcul du P&L sont corrects.
3. **Symétrie.** Inverser tous les signaux (CALL ↔ PUT) doit inverser le taux de
   réussite autour de 50 %. Une asymétrie révèle un biais dans la résolution.
4. **Données mélangées.** Permuter aléatoirement l'ordre des bougies. Toute
   performance résiduelle est un artefact du moteur.
5. **Déterminisme.** Deux exécutions identiques produisent des résultats
   identiques au bit près. Sinon un état fuit entre les trades.

Ces tests tournent en CI. Aucun résultat de stratégie n'est crédible tant qu'ils
ne passent pas tous.

---

## 3. La boucle d'apprentissage

### 3.1 Ce qui est enregistré — la décision de conception centrale

À chaque évaluation de bougie, qu'un signal soit émis ou non, écrire une ligne
dans `evaluations` :

```
id, ts, pair, direction_envisagée,
score_par_condition   -- 6 valeurs booléennes + les valeurs numériques brutes
features              -- JSON : ma14_distance_pct, bb_percent_b, stoch_k, stoch_d,
                      --        atr_normalisé, distance_pivot_pct, corps_bougie_pct,
                      --        mèche_haute_pct, mèche_basse_pct, heure_utc,
                      --        minutes_depuis_ouverture, payout_courant, ...
condition_bloquante   -- laquelle a échoué (NULL si signal émis)
signal_émis           -- 0/1
```

Puis, dans `outcomes`, le résultat de chaque évaluation **y compris celles qui
n'ont pas généré de signal** : quel aurait été le résultat si on avait pris le
trade.

C'est le point le plus important de la spec. Sans les quasi-signaux et leur
résultat contrefactuel, vous ne pouvez pas répondre à « qu'est-ce que le bot a
raté ». Avec eux, chaque condition devient mesurable.

Les features sont enregistrées **au moment de la décision**, jamais recalculées
après coup. Un recalcul ultérieur sur du code modifié réintroduit du look-ahead.

### 3.2 Analyse d'attribution

Trois rapports, à produire dès que 400 résultats sont disponibles :

**Valeur marginale de chaque condition.** Pour chaque condition C, comparer le
taux de réussite des trades où C était vraie contre ceux où elle était fausse,
toutes autres conditions validées. Une condition dont l'écart est inférieur à
2 points ne sert à rien : elle réduit votre nombre de trades sans améliorer la
qualité. Attendez-vous à ce que la majorité des six tombe dans ce cas.

**Ablation.** Rejouer le backtest complet en retirant une condition à la fois.
Si retirer une condition améliore le résultat net (plus de trades, taux stable),
elle est nuisible.

**Segmentation.** Taux de réussite par heure UTC, par paire, par régime de
volatilité (terciles d'ATR). Un edge réel est rarement uniforme. Un edge qui
n'apparaît que sur une seule paire à une seule heure est presque toujours du
surajustement — vérifier le nombre de trades dans le segment avant de s'en
réjouir.

### 3.3 Du ET strict au modèle

Une fois les features enregistrées, remplacer la porte ET par une **régression
logistique** entraînée uniquement sur la fenêtre de calibration.

Régression logistique, et pas un réseau de neurones ni un gradient boosting :
avec quelques centaines d'exemples et un rapport signal/bruit très faible, tout
modèle plus expressif mémorisera le bruit. De plus, les coefficients sont
lisibles — vous saurez ce que le modèle a appris, et vous verrez immédiatement
si c'est absurde.

Le modèle sort une probabilité. Trade pris si
`p × payout − (1−p) > marge`, avec `marge` fixée pour couvrir l'incertitude
d'estimation. Le seuil devient un paramètre unique et interprétable, au lieu de
six règles arbitraires.

### 3.4 Garde-fous contre l'auto-dégradation

Un bot qui se réentraîne automatiquement sur ses résultats récents et agit
immédiatement dessus se détruit en quelques semaines. Protocole
champion/challenger obligatoire :

1. Le modèle en production (**champion**) est figé. Il n'est jamais modifié
   en place.
2. Un nouveau modèle (**challenger**) est entraîné hors ligne, sur données
   historiques uniquement.
3. Le challenger doit battre le champion hors échantillon d'au moins 3 points de
   taux de réussite sur au moins 300 trades simulés.
4. S'il passe, il tourne en **shadow mode** : il produit ses signaux en parallèle
   du champion, ils sont enregistrés, aucun n'est envoyé. Pendant 200 trades.
5. Promotion seulement si l'avantage se confirme en shadow. Le champion sortant
   est conservé dans `model_registry`, jamais écrasé.
6. **Rollback automatique** : si le champion en production tombe sous son
   intervalle de confiance bas sur 100 trades glissants, retour au modèle
   précédent et alerte Telegram.

Fréquence de réentraînement : au maximum une fois par mois. Plus souvent, vous
poursuivez du bruit.

### 3.5 Ce que cette boucle ne peut pas faire

À dire clairement parce que cela détermine vos attentes :

- Elle ne peut pas créer un edge qui n'existe pas dans les données. Si les
  paires OTC sont un flux pseudo-aléatoire généré par le broker, aucune quantité
  d'apprentissage n'en tirera de profit, et l'analyse d'attribution le montrera
  en affichant des écarts nuls partout. C'est un résultat utile.
- Elle ne peut pas expliquer une perte individuelle. La granularité minimale de
  toute conclusion est la centaine de trades.
- Elle ne protège pas contre un changement de comportement du broker. Le
  monitoring de dérive (§3.4.6) est ce qui vous en avertit, avec du retard.

---

## 4. Ordre de construction

Chaque étape a un critère de sortie mesurable. Ne pas passer à la suivante avant.

| # | Module | Critère de sortie |
|---|---|---|
| 1 | Persistance + migrations | Le test de survie au déploiement passe |
| 2 | Collecteur | 14 jours de données, < 5 % de bougies écartées |
| 3 | Indicateurs sans repeint | ZigZag et fractales avec latence de confirmation, testés unitairement |
| 4 | Moteur de backtest | Les 5 tests-oracles passent |
| 5 | Stratégie initiale + journalisation | 400+ évaluations avec features et contrefactuels |
| 6 | Analyse d'attribution | Rapport produit ; décision go/no-go **honnête** |
| 7 | Modèle + walk-forward | Avantage hors échantillon confirmé sur 3 fenêtres |
| 8 | Bot Telegram | Uniquement si 7 est concluant |
| 9 | Shadow mode en démo | 200 trades papier, écart backtest/live < 3 points |

L'étape 6 doit pouvoir se conclure par « il n'y a pas d'edge, on arrête ». Un
système qui ne peut pas produire ce verdict n'est pas un système de mesure.

---

## 5. Notes pour Claude Code

- Interdire les valeurs par défaut silencieuses sur tout ce qui touche à
  l'argent ou aux données : chemin de base, payout, latence, expiration. Absence
  de configuration = arrêt, pas valeur par défaut.
- Typer les horodatages sans ambiguïté : suffixer systématiquement `_ms` ou
  `_sec`, tout en UTC. La confusion secondes/millisecondes est le bug le plus
  fréquent de ce type de projet et il produit des backtests décalés d'un facteur
  1000 sans lever d'erreur.
- Tests unitaires sur les indicateurs avec des séries construites à la main dont
  le résultat est calculable à la main.
- Le module Telegram n'a aucune logique métier : il formate et envoie.
