# Étape 3 — Plan d'exécution, version 2 (à approuver avant tout code)

Analyse hors ligne et en lecture seule. Aucune modification de la stratégie,
des signaux, des échéances, du collecteur ni de T0/T1/T2/T3. Aucune écriture
de séries de ticks en base. Un résultat négatif bien établi est un résultat.

La version 2 répond à la revue du 26/10 : la disponibilité de l'information,
la compatibilité des composants, la définition du résultat, la qualité et le
coût, les familles complètes, la référence, le découpage, l'incertitude, les
comparaisons multiples, les critères de verdict et le protocole d'exécution.

## 0. Disponibilité : l'heure du marché n'est pas l'heure de réception

L'heure de réception et l'ordre d'arrivée ne sont pas stockés. Une marge de
2 s avant la décision **n'est pas** une garantie : elle est retirée comme
règle de disponibilité. Deux niveaux d'analyse remplacent cette marge.

**Niveau 1 — historique indicatif.** Les variables lisent tous les ticks
d'instant antérieur à la décision. Leur disponibilité réelle n'est pas
démontrée, et chaque résultat de ce niveau le dit.

**Niveau 2 — disponibilité établie par le code et les données.** Ce qu'on
peut démontrer, sans supposer :
- **[Vérifié, code]** Une minute close est écrite dans cet ordre : d'abord
  son chemin de ticks, puis sa bougie, puis la validation (`valider`), puis
  le réveil de la course (`Collector.flush`, ordre inchangé depuis le 27/09,
  84762e5 ; toute l'histoire des ticks est postérieure). Je ne prétends pas
  que chemin et bougie forment une seule transaction : seul compte que le
  chemin est écrit avant le réveil.
- **[Vérifié, code]** La course n'évalue la bougie du signal qu'après cette
  écriture (réveil sur `BOUGIES_ECRITES`). Le chemin de la minute du signal
  est donc en base avant la décision.
- **[Vérifié, code]** Un tick arrivé après la clôture de sa minute est écarté
  par l'agrégateur, du chemin comme de la bougie (règle présente depuis le
  20/09). « Horodaté dans une minute close » veut donc dire « reçu avant la
  clôture de cette minute ».
- **[Vérifié, code]** La décision du rejeu est la fin de la bougie du signal
  (`Exemple.ts_sec = début de bougie + 60`). La course décide quelques
  secondes plus tard, donc le niveau 2 ne voit pas plus que la course.
- **Ce qui casserait l'équivalence** : un chemin réécrit plus tard, par un
  redémarrage (minute partielle complétée), une fusion ou une
  synchronisation (« le plus fourni gagne »). L'audit de concordance le
  détecte, car la bougie que la stratégie a vue ne correspond plus au
  chemin.

**Règle du niveau 2** : un signal y entre si **toutes** les minutes de sa
fenêtre avant la décision sont en concordance « identique » ou « ordre
d'arrivée » (même ensemble de ticks, seule l'ouverture ou la clôture prise à
l'arrivée diffère). Sont exclus les signaux dont une minute est « minute
coupée », « bougie incomplète », « chemin plus ou moins fourni »,
« divergence inexpliquée » ou absente.

**Conclusion par niveau** : une hypothèse ne peut être déclarée confirmée
qu'au niveau 2. Le niveau 1 sert à vérifier que l'exclusion ne biaise pas.

**Robustesse** : on refait chaque calcul en retirant les k dernières
secondes de la fenêtre (k = 0, 2, 5 s). Ce sont les ticks dont l'inclusion
dépend le plus de l'ordre d'arrivée autour de la clôture. C'est un test, pas
une règle de disponibilité.

## 1. Compatibilité des composants réutilisés (vérifiée par des tests avant tout calcul)

| Composant | Convention | Test prévu |
|---|---|---|
| `exemples_historiques` | décision = fin de la bougie du signal ; issues = clôtures M1 à décision + h | modifier les bougies d'après la décision ne change aucune variable de contexte de référence ; les champs `e_*` (mesurés à l'entrée) et `imm_*` (après la décision) sont exclus de la référence |
| `AuditTicks` | minute = son début ; la fenêtre avant décision = minutes [d − w, d) | les minutes lues sont toutes closes à d |
| `doublons` | doublons cherchés dans la minute (une copie porte le même instant) | variables calculées en ticks bruts et en ticks distincts |
| `payout_at` | relevé d'instant ≤ d, écrit au moment du relevé | aucun relevé postérieur à d n'est lu |

La déduplication par (paire, instant, prix) reste un **traitement d'analyse**
:
- l'historique n'est pas modifié ;
- chaque résultat est donné en ticks distincts ET en ticks bruts ;
- une conclusion qui change selon le traitement est classée « fragile ».

## 2. Résultat

**Principal, fixé maintenant** : issue T0 à **900 s** (l'échéance
pré-inscrite de ZoneH1).
- **Gain** : la clôture M1 à d + 900 est du bon côté du prix d'entrée,
  c'est-à-dire la clôture de la bougie du signal.
- **Égalité** (prix inchangé) : exclue de la métrique principale, comptée
  et publiée. En sensibilité, elle est comptée comme perte (règle
  défavorable).
- **Issue manquante** (bougie absente à d + 900) : signal exclu, compté par
  motif.
- **Payout** : en vigueur à d (`payout_at`). Il ne sert qu'aux résultats
  économiques (secondaires). S'il manque, l'EV de ce signal n'est pas
  calculée, sans valeur supposée.
- **Échéances 240 et 600 s** : secondaires, exploratoires, dans la
  correction BH.

## 3. Qualité et coût, mesurés avant toute comparaison

**Passe de mesure** (sans issue lue) :
- par paire et par jour : minutes présentes, trous, fermetures au broker,
  fenêtres VALID / PARTIAL / INVALID, part du niveau 2 ;
- période réellement exploitable par signal : **pas de « 16 jours » supposés**.

**Classes de fenêtre, figées maintenant** (15 min avant d) :
- **VALID** : couverture 100 %, aucun écart ≥ 10 s, dernier tick à ≤ 5 s de
  d, aucune minute fermée au broker ;
- **PARTIAL** : couverture ≥ 80 %, aucun écart ≥ 30 s ;
- **INVALID** : sinon.
- Aucune interpolation.

**Coût** :
- un essai sur 1 paire × 1 jour mesure le temps et la mémoire ;
- arrêt si la passe complète dépasse 30 min de calcul ou 150 Mo de mémoire.

## 4. Variables candidates (familles A à E)

**Notation pour un signal** :
- d : décision ; s = +1 pour un CALL, −1 pour un PUT ;
- L : niveau de la zone ; μ = 0,0002 · L (tolérance de ZoneH1) ;
- δ : pas de cotation de la paire, soit le plus petit écart de prix non nul
  observé sur la paire pendant l'exploration, figé ensuite ;
- sous-fenêtre W de w ∈ {60, 180, 300, 900} s ;
- ticks (distincts ou bruts) (t_k, p_k), k = 1…n, d − w ≤ t_k < d, triés ;
- Δp_k = (p_k − p_{k−1}) / δ, en points ;
- z_k = s · (p_k − L) / μ : distance signée en marges, positive du côté
  favorable au trade.

**Règle commune** : une variable vaut « manquante » si elle n'est pas
définie (n trop petit, division par zéro). Une variable manquante n'est
jamais imputée par un résultat ; elle reçoit la médiane de l'exploration et
une indicatrice de manque.

**A — approche de la zone**
- A1 `dist_fin` = z_n.
- A2 `dist_debut` = z_1.
- A3 `vitesse` = (z_n − z_1) / ((t_n − t_1) / 60 000), en marges par minute ;
  manquante si n < 2.
- A4 `acceleration` = vitesse de la 2e moitié de W − vitesse de la 1re ;
  manquante si une moitié a moins de 2 ticks.
- A5 `contacts` = #{k : |z_k| ≤ 1 et |z_{k−1}| > 1}.
- A6 `temps_zone` = Σ (t_{k+1} − t_k) · 1[|z_k| ≤ 1], le dernier tick valant
  jusqu'à d, en s.
- A7 `penetration` = max(0, max_k(−z_k)).
- A8 `depuis_contact` = (d − max{t_k : |z_k| ≤ 1}) / 1000, en s ; manquante
  sans contact.
- A9 `apres_contact` = z_n − z au dernier contact.

**B — régime**
- B1 `vol` = √(Σ Δp_k²).
- B2 `amplitude` = (max p − min p) / δ.
- B3 `nulles` = #{Δp_k = 0} / (n − 1).
- B4 `efficience` = |p_n − p_1| / (δ · Σ|Δp_k|) ; manquante si Σ = 0.
- B5 `ratio_vol` = (vol du dernier cinquième de W / √(w/5)) / (vol_W / √w) ;
  manquante si vol_W = 0.
- B6 `concentration` = part de Σ|Δp| portée par les 5 % plus grands |Δp|.

**C — retournements** (signes σ des Δp ≠ 0)
- C1 `retournements` = #{σ_j ≠ σ_{j−1}}.
- C2 `persistance` = longueur moyenne des suites de même signe.
- C3 `part_sens` = Σ max(0, s·Δp_k) / Σ|Δp_k|.
- C4 `amplitude_retournement` = moyenne de |p(r_{j+1}) − p(r_j)| / δ, où les
  r_j sont les ticks où le signe change ; manquante s'il y a moins de deux
  retournements.
- C5 `delai_retournement` = médiane du temps entre deux retournements, en s.

**D — interactions avec la zone** (bornes L ± μ)
- D1 `traversees` = #{signe(z_k) ≠ signe(z_{k−1}), z ≠ 0}.
- D2 `sorties_retours` = nombre de sorties de |z| ≤ 1 suivies d'un retour
  dans W.
- D3 `temps_au_dela` = Σ (t_{k+1} − t_k) · 1[z_k < −1], temps passé au-delà
  de la zone contre le trade, en s.

**E — qualité** : elle qualifie les fenêtres, ne prédit pas, et n'est
jamais un filtre de marché.
- E1 couverture ;
- E2 plus long écart (y compris d − t_n) ;
- E3 âge du dernier tick ;
- E4 part de doublons bruts ;
- E5 gel ≥ 10 s à ± 15 min ;
- E6 fermeture au broker à ± 15 min ;
- E7 minute à flux doublé ;
- E8 niveau de disponibilité (1 ou 2).

E sert à classer les fenêtres et à mesurer le **biais de sélection** :
on compare les signaux VALID et les autres sur la référence et les issues.

**Exclu par construction** : tout ce qui suit la décision (rupture de zone
future, MFE, MAE, retour, résultat), et tout ce qui dépend de l'ordre
d'arrivée ou de la réception.

**Total** : 23 variables de marché × 4 sous-fenêtres = 92 candidates.

## 5. Référence sans microstructure

**Variables**, toutes connues à la décision et figées maintenant :
- paire (indicatrices) ;
- créneau UTC de 4 h ;
- sens ;
- `touches`, `entrees_deja_offertes` ;
- payout en vigueur, avec une indicatrice de manque ;
- contexte M1 : `distance_niveau`, `elan_15m`, `volatilite_relative`,
  `mouvement_heure`, `depuis_contact`, `alternances_10`.

**Modèle** :
- régression logistique pénalisée (L2, λ = 1 sur variables standardisées),
  en Python pur ;
- λ fixé a priori, pas réglé sur la validation ;
- standardisation et imputation (médiane + indicatrice) calculées sur
  l'**exploration seule**, puis appliquées telles quelles.

**Modèles comparés** :
- hypothèses principales : référence + **une** variable ;
- exploration : référence + chaque variable, une à une ;
- puis référence + combinaison restreinte des familles retenues.

## 6. Séparation chronologique (fixée maintenant, avant lecture des issues)

- **Exploration** : décisions avant le **21/10 00:00 UTC**. Distributions,
  formulation des hypothèses, terciles.
- **Validation** : du 21/10 00:00 au **gel**, fixé au **28/10 00:00 UTC**.
  Sélection d'au plus 4 hypothèses principales, une par famille A à D, avec
  leur sens et leurs terciles.
- **Confirmation** : décisions à partir du **29/10 00:00 UTC** (un jour de
  battement après le gel). Jours collectés après le gel, jusqu'à au moins
  **500 signaux de niveau 2**.
  - Le modèle est réestimé une seule fois, sur exploration + validation,
    puis figé.
  - La date de verdict est fixée au gel.
- **Pas de mélange aléatoire** de signaux voisins entre périodes.
- **Ce qui a déjà été vu** :
  - les issues T0, T2 et T3 agrégées sur toute la période (`/entrees`) ;
  - la phase F : trajectoires au tick **après** l'entrée.
  Aucune variable de ticks avant la décision n'a été regardée. La référence
  et les variables sont fixées ici sans réglage sur les issues.

## 7. Incertitude

**Statistique** : l'amélioration par signal Δℓ_i = ℓ_réf,i − ℓ_mod,i (en
nats, positive = mieux), moyennée sur la période évaluée.

**Bootstrap par blocs paire × jour** :
- tirage des blocs avec remise ;
- 1 000 tirages, graine 7 ;
- intervalle percentile à 95 % ;
- p-valeur unilatérale = part des tirages ≤ 0.

**Pourquoi des blocs** : les signaux d'une même paire un même jour partagent
un régime.

**Sensibilité** : blocs **par jour** (toutes paires confondues), parce que
les signaux d'une même heure sont corrélés entre paires. Une conclusion qui
ne tient qu'avec les blocs paire × jour est « fragile ».

## 8. Comparaisons multiples

- **Principales** : 4 au plus, Holm au seuil global de 5 %.
- **Exploratoires** : 92 variables × échéances secondaires, Benjamini-Hochberg
  à 10 %. Le nombre total d'essais est publié.
- **Seuils et sous-fenêtres** : tous les essais, même abandonnés, entrent au
  registre (journal du protocole).
- **Interactions** : seulement après les familles seules, chacune avec son
  hypothèse écrite avant le calcul, et comptées dans les essais.

## 9. Verdicts

| Verdict | Critères, tous en confirmation, niveau 2 |
|---|---|
| **Confirmée** | p Holm < 0,05 pour Δℓ > 0 ; **et** effet pratique : tercile retenu ≥ +2 points de taux de gain dans le sens pré-inscrit ; **et** Δℓ > 0 dans au moins 2 des 3 tiers chronologiques de la confirmation ; **et** signe inchangé pour : ticks bruts / distincts, k = 0 / 2 / 5 s, blocs paire × jour / jour |
| **Rejetée** | borne haute à 95 % de l'effet du tercile < +2 points (aucun effet utile possible), **ou** borne haute de Δℓ ≤ 0 |
| **Indéterminée** | ni l'un ni l'autre (l'intervalle contient 0 et l'effet utile), **ou** moins de 500 signaux de niveau 2 en confirmation, **ou** résultat « fragile » |

Le seuil de +2 points est fixé a priori :
- au payout de 92 %, le point mort est de 52,08 % ;
- chaque point de taux de gain vaut 1,92 % de la mise en espérance ;
- +2 points font donc environ +3,8 % de la mise par ordre.

Une hypothèse confirmée devient une **pré-inscription**, jamais un filtre
direct de la course.

## 10. Protocole d'exécution : aucune écriture, charge limitée sur Neon

- **Lectures**
  - SELECT seulement, listés (`REQUETES`), sur une connexion passée en
    `READ ONLY` pour la passe complète.
  - Aucune table créée, aucune migration, aucune ligne écrite : c'est le
    choix par défaut (option b ci-dessous).
- **Passe complète, une fois**
  - Paire par paire, par tranches d'un jour.
  - Environ 0,2 à 0,3 Go de transfert réseau.
  - Lancée après l'essai sur échantillon.
  - À faire **avant** toute conservation glissante ou archive des ticks.
- **Au quotidien** : les variables sont calculées dans le rejeu de `/ticks`,
  sur les chemins **déjà décodés**, donc sans transfert supplémentaire.
- **Mémoire** : une paire à la fois, comme l'audit.
- **Sorties** : CSV d'une ligne par signal (variables, qualité, niveau,
  issue) envoyé par Telegram. Aucune écriture en base.
- **Preuves de lecture seule** : tests sur SQLite (empreinte du fichier
  inchangée) ; toutes les requêtes commencent par SELECT.
- **Reproductibilité** : empreinte des chemins, version du code,
  paramètres, période, graine.
- **Arrêts** :
  - un test anti-fuite échoue ;
  - moins de 80 % de fenêtres VALID ;
  - coût hors des bornes du §3 ;
  - historique de ticks perdu avant la passe complète.

## 11. Fichiers (après approbation)

| Fichier | Rôle |
|---|---|
| `maxprofit/apprentissage/microstructure_pre.py` (nouveau) | variables A à E, niveaux 1 et 2, classes de qualité ; pur |
| `maxprofit/apprentissage/etude_micro.py` (nouveau) | découpage, référence, logistique L2, Δℓ, Brier, bootstrap, Holm / BH, verdicts ; pur |
| `maxprofit/apprentissage/audit_ticks.py` | appel sur les fenêtres des signaux, chemins déjà décodés |
| `maxprofit/live/etude3.py` (nouveau), commande `/micro` | rapport et CSV |
| `maxprofit/live/apprenti.py` | passe complète une fois, puis fenêtre quotidienne |
| `tests/test_microstructure_pre.py`, `tests/test_etude_micro.py` | définitions, anti-fuite, compatibilité (§1), qualité, statistiques |
| `docs/etape3_*.md` | les six livrables, sans écraser les précédents |

## 12. Décisions à prendre

1. **Approuver ce plan**, en particulier :
   - les niveaux de disponibilité ;
   - le résultat à 900 s ;
   - le découpage (exploration avant le 21/10, gel le 28/10, confirmation à
     partir du 29/10) ;
   - la référence ;
   - les verdicts.
2. **Autoriser la passe complète** (0,2 à 0,3 Go de transfert, une fois).
3. **Où garder les lignes calculées** :
   - **(b)** CSV par Telegram, par défaut, sans écriture ;
   - **ou (c)** une petite table dérivée, environ 2 Mo puis 0,1 Mo par jour,
     à autoriser explicitement.
4. **Continuité du stockage** : elle conditionne la confirmation (à partir
   du 29/10).
