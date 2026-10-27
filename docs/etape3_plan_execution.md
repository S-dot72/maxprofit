# Étape 3 — Plan d'exécution, version 3

**Statut** : approuvé sous conditions le 27/10. Seuls la préparation et les
contrôles préalables sont autorisés. La passe complète attend une nouvelle
validation.

**Ce qui ne change pas** :
- analyse hors ligne et en lecture seule ;
- aucune modification de la stratégie, des signaux, des échéances, du
  collecteur ni de T0/T1/T2/T3 ;
- aucune écriture de séries de ticks en base ;
- un résultat négatif bien établi est un résultat.

La version 3 répond aux points A à D de la revue du 27/10, aux trois
corrections statistiques, à la justification des deux points et aux
prérequis de stockage.

## 0. Disponibilité de l'information

L'heure de réception et l'ordre d'arrivée ne sont pas stockés.

**Niveau 1, historique indicatif.** Les variables lisent tous les ticks
horodatés avant la décision. Leur disponibilité réelle n'est pas démontrée,
et chaque résultat de ce niveau le dit.

**Niveau 2, disponibilité étayée par le mécanisme de collecte, sous réserve
des tests de concordance.** Ce n'est pas une certitude absolue. Il repose
sur trois choses.

### Ce que les tests du vrai code établissent

Ces tests (`tests/test_disponibilite_collecte.py`) utilisent l'agrégateur,
`Collector.flush`, `Collector.run`, SQLite et la classification de l'audit.
- **Ordre d'écriture** : le chemin, puis la bougie, puis la validation, puis
  le réveil de la course. Aucun tick d'une minute encore ouverte n'est en
  base au moment du réveil.
- **Frontières** : un tick appartient à la minute de son instant (un tick à
  m + 60 000 ms est dans la minute suivante). Une minute ne se ferme qu'à
  l'arrivée d'un tick de la minute suivante sur la même paire. Une paire
  silencieuse garde donc sa minute ouverte.
- **Retards** : un tick arrivé après la fermeture de sa minute est écarté du
  chemin **et** de la bougie. Un tick horodaté dans une minute close a donc
  été reçu avant sa fermeture.
- **Écritures concurrentes** : avec deux sockets, les copies d'un tick sont
  dans le chemin et dans le compte de la bougie, qui portent donc le même
  ensemble de ticks. Une arrivée hors d'ordre donne la classe « ordre
  d'arrivée » : mêmes ticks, ouverture ou clôture prise à l'arrivée.
- **Réécriture** : une coupure au milieu d'une minute, suivie d'une reprise
  dans la même minute, donne la classe « minute coupée ». La minute sort du
  niveau 2.

### Ce qu'un contrôle en production mesure

`/micro_essai`, section 4, mesure deux choses sur les ordres réels :
- le retard de la décision de la course sur la frontière de minute ;
- si le prix qu'elle avait en main à cet instant (`prix_attendu`, lu dans le
  tampon vivant) figure dans le flux stocké, comme dernier tick puis à ± 1,
  2 et 5 s.

Premier aperçu, sur les 25 ordres du `/ticks` du 26/10 :
- 15 fois sur 25, le dernier tick stocké au même instant est le prix vu ;
- dans les 10 autres cas, l'écart va de 1 à 7 points.

Cet aperçu dépend de la conversion d'horloge locale → broker, précise à
quelques centaines de ms. Le contrôle « présent à ± 5 s » tranche : un prix
absent à ± 5 s serait une vraie divergence.

### Ce qui reste indémontrable

L'instant exact de réception de chaque tick. Le niveau 2 en est une borne
étayée, pas une preuve.

### Règle du niveau 2

Un signal est au niveau 2 si ses 15 minutes avant d sont toutes présentes et
en concordance « identique » ou « ordre d'arrivée ». Sinon, il est au niveau
1.

Une seule dépendance à une minute postérieure à d existe. Le test
« décalage de minute » compare la dernière minute à la bougie suivante.
- Il ne peut évincer « identique » : ce test passe avant.
- Il ne peut évincer « ordre d'arrivée » que si deux bougies voisines ont le
  même nombre de ticks, le même haut et le même bas.
- Ce n'est pas une information de marché.

### Conclusion par niveau et robustesse

Une hypothèse ne peut être confirmée qu'au niveau 2. Le niveau 1 sert à
mesurer le biais de l'exclusion.

En robustesse, chaque calcul est refait en retirant les k dernières secondes
(k = 0, 2, 5). C'est un test, pas une règle de disponibilité.

## 1. Compatibilité des composants (testée)

| Composant | Convention | Test |
|---|---|---|
| `exemples_historiques` | décision = fin de la bougie du signal | les variables de référence ne changent pas quand toutes les bougies d'après la coupure changent, alors que les issues changent (`test_etude_micro`) |
| `AuditTicks` | minute = son début ; classes de concordance | classes gardées minute par minute (`garder_classes`) ; niveau 2 testé sur collecte réelle |
| déduplication | copies exactes (instant, prix) dans la minute | tout résultat en ticks bruts **et** distincts ; l'historique n'est jamais modifié |
| fenêtres | [d − w, d − k) | perturbation : des ticks à d − k, d, d + 30 s, d + 15 min ne changent aucune variable (toutes les combinaisons de w et k) ; le contrôle sait échouer |
| `payout_at` | relevé d'instant ≤ d | aucun relevé postérieur lu |

## 2. Résultat : règle unique, testée

Le signal porte sur la bougie M (début m), et d = m + 60.

- **Issue à h secondes** : la clôture de la bougie **complète** qui commence
  à m + h. Elle couvre [d + h − 60, d + h) et clôt donc à d + h. Le prix
  d'entrée est la clôture de M.
- **Principale** : h = 900 s.
- **Cas limites** :
  - bougie de sortie **absente ou incomplète** : MANQUANTE, aucune voisine
    ne la remplace ;
  - **égalité** : rapportée à part ; en sensibilité, comptée perdue ;
  - **frontière** : début et horizon sont des multiples de 60, sinon refus ;
  - **deux bougies possibles** : impossible, la clé (paire, tf, début)
    l'interdit.
- **Règle unique** : `etude_micro.issue` et le rejeu rendent la même issue
  sur chaque signal. Les signaux écartés par le rejeu (égalité, bougie
  absente ou incomplète) ressortent désormais avec leur motif (`sans_issue`)
  au lieu d'être perdus.
- **Payout** en vigueur à d : il ne sert qu'aux résultats économiques. S'il
  manque, pas d'EV pour ce signal.
- **240 et 600 s** : secondaires, en BH.
- **Limite connue** : l'issue M1 est un approchant de l'échéance du broker,
  qui court depuis le clic (quelques secondes après d).

## 3. Qualité et coût, mesurés avant toute comparaison

**Classes de fenêtre**, figées, sur [d − 900, d) en ticks bruts, sans rien
lire après d :
- **VALID** : couverture 100 %, aucun écart ≥ 10 s (bords compris), dernier
  tick à ≤ 5 s de d, aucune minute fermée chez le broker ;
- **PARTIAL** : couverture ≥ 80 %, aucun écart ≥ 30 s ;
- **INVALID** : sinon.

E5 (gels) et E6 (fermetures) ne lisent plus que la fenêtre avant d.

**Coût** (`/micro_essai`, 1 paire × 1 jour d'exploration) :
- lignes, octets compressés, temps par étape ;
- mémoire, mesurée dans une seconde passe pour ne pas fausser le temps ;
- extrapolation à la passe de sélection.

**Bornes** : 30 min de calcul, 150 Mo de mémoire.

## 4. Variables (A à E)

Inchangées depuis la version 2, avec trois précisions :
- **δ** = 10^−e, où e est la plus grande échelle décimale des chemins de la
  paire, figée sur l'exploration ;
- **B5** est défini par sous-fenêtre (dernier cinquième de W face à W) ;
- **C4** est la moyenne de |p(r_{j+1}) − p(r_j)| / δ entre ticks de
  retournement consécutifs.

**Total** : 23 variables de marché × 4 sous-fenêtres = 92 candidates. Les
définitions sont testées sur un cas calculé à la main
(`tests/test_microstructure_pre.py`).

## 5. Référence, et mêmes signaux

**Variables** :
- paire et créneau de 4 h ;
- sens, `touches`, `entrees_deja_offertes` ;
- payout, avec une indicatrice de manque ;
- `distance_niveau`, `elan_15m`, `volatilite_relative`, `mouvement_heure`,
  `depuis_contact`, `alternances_10`.

Toutes sont calculées sur les bougies jusqu'à la bougie du signal. Le test de
perturbation le vérifie, et les champs `e_*` et `imm_*` sont exclus.

**Modèle** : logistique L2 (λ = 1, l'interception n'est pas pénalisée).
Imputation et standardisation sont figées sur l'exploration.

**Mêmes signaux** :
- **Population commune** : niveau 2, issue dans la métrique, une seule
  règle d'exclusion appliquée **avant** tout ajustement.
- **Une variable de microstructure manquante** est imputée (médiane de
  l'exploration et indicatrice) : elle n'exclut jamais un signal d'un seul
  des deux modèles.
- **En code**, une ligne de comparaison porte les deux prédictions ; une
  ligne incomplète ou un signal compté deux fois est refusé
  (`LigneComparee`, `comparer`).

## 6. Découpage et gel formel

**Périodes** :
- **Exploration** : avant le 21/10 00:00 UTC. Les bornes des terciles y sont
  calculées, puis figées.
- **Validation** : du 21/10 au 28/10 00:00 UTC, bornes fixes. On y choisit
  au plus 4 hypothèses (variable, w, sens, tercile retenu).
- **Gel formel** : la pré-inscription (JSON) est enregistrée avec son
  empreinte SHA-256, dans le registre et dans un commit.
  - Le code refuse tout calcul de confirmation sans gel, ou sur des
    paramètres différents (`exiger_le_gel`).
- **Confirmation** : elle s'ouvre au **premier minuit UTC qui suit le gel**,
  jamais avant le 28/10.
- **Quarantaine** : ce qui tombe entre le 28/10 et l'ouverture n'est ni
  choisi ni confirmé.
- Le gel ne peut venir qu'après la passe de sélection, donc après ton feu
  vert.

**Biais de sélection** : les 4 hypothèses sont choisies parmi 92.
- Holm sur 4 tests ne corrige que les 4 tests de confirmation.
- La validité repose sur des données de confirmation **nouvelles**,
  indépendantes du choix.
- Les effets observés en validation sont gonflés par la sélection (effet du
  gagnant). Ils ne servent ni à la puissance ni au verdict, et sont publiés
  comme tels.

## 7. Incertitude

**Bootstrap par blocs paire × jour** : 1 000 tirages, graine 7, intervalle
percentile à 95 %.
- La p-valeur vaut (1 + #{tirages ≤ 0}) / (tirages + 1). Elle ne descend pas
  sous 1/1001 : un seuil nominal plus bas ne peut pas confirmer.
- **Blocs vides** : un bloc existe s'il a au moins un signal admissible. Une
  paire × jour sans signal admissible n'a aucun poids : on conditionne sur
  l'ensemble observé. Le nombre de blocs est publié.
- **Mêmes blocs** pour la référence et le modèle (Δℓ par signal).

**Sensibilité** : blocs par jour, toutes paires confondues. Une conclusion
qui ne tient qu'avec les blocs paire × jour est « fragile ».

## 7 bis. Puissance, et règle de poursuite fixée à l'avance

500 signaux sont un seuil opérationnel, pas une garantie de puissance.

**Estimation**, depuis les seules issues de l'exploration
(`etude_micro.puissance`, publiée par `/micro_essai`) :
- variance du contraste « tercile contre les deux autres » ≈ p(1 − p) ×
  4,5/N × DEFF ;
- DEFF = 1 + (m − 1)ρ, avec ρ la corrélation intra-bloc et m la taille
  moyenne pondérée des blocs ;
- α = 0,0125 unilatéral (le pas le plus sévère de Holm), puissance 80 %.

**Ordres de grandeur** (sans dépendance, p ≈ 0,5) :

| N signaux de niveau 2 | Effet minimal détectable | Demi-IC 95 % |
|---|---|---|
| 500 | ≈ 15 points | ≈ 9 points |
| 2 000 | ≈ 7 points | ≈ 4,6 points |
| 8 000 | ≈ 3,7 points | ≈ 2,3 points |

Détecter +2 points demande environ 27 000 signaux. L'exclure (borne haute
sous +2 points) en demande environ 22 000. Avec la dépendance, c'est plus.

**Conséquence, à dire maintenant** : en quelques semaines, l'étude ne peut
confirmer ou exclure que de **grands** effets (au-delà de 6 à 15 points).
- Le verdict « rejetée » au seuil de +2 points est pratiquement hors de
  portée.
- À chaque regard, on publie l'effet exclu (« au-delà de X points »).

**Règle de poursuite** :
- **Regards** à N = 500, 1 000, 2 000, 4 000, 8 000 signaux de niveau 2 en
  confirmation.
- **Dépense d'α** de type O'Brien-Fleming (Lan-DeMets). Le seuil nominal de
  la famille de Holm à chaque regard est l'**incrément** de dépense : une
  borne de Bonferroni entre regards, conservatrice.
  - Seuils : environ 4·10⁻¹⁵, 3·10⁻⁸, 9·10⁻⁵, 0,0055 et 0,044.
  - Les trois premiers regards ne peuvent pas confirmer avec 1 000 tirages :
    c'est voulu.
- **Arrêt de la collecte** pour une raison étrangère aux résultats
  (stockage, broker) : le dernier regard disponible devient final et reçoit
  l'α restant.
- **À 8 000 signaux** sans verdict : **indéterminée**, définitivement, avec
  les bornes.
- **Interdit** : changer les regards, les seuils ou N_max après
  l'ouverture de la confirmation.

**Décision pour toi** : vu l'ordre de grandeur, accepter ce plafond (environ
3 mois à 90 signaux de niveau 2 par jour, à mesurer) ou réduire l'ambition à
« détecter un grand effet ».

## 8. Comparaisons multiples et registre

**Corrections** :
- **Principales** : au plus 4, Holm à 5 %, à chaque regard au seuil
  nominal.
- **Exploration** : 92 variables plus les échéances secondaires, BH à 10 %.
  Une découverte BH reste **exploratoire**, jamais une preuve.

**Sensibilités de chaque principale**, toutes comptées au registre, soit
2 × 3 × 2 × 2 = 24 variantes :
- ticks bruts / distincts ;
- k = 0 / 2 / 5 ;
- blocs paire × jour / jour ;
- égalités exclues / perdues.

**Registre** (`etude_micro.Registre`) :
- chaque essai y entre, abandonné compris avec sa raison ;
- le nombre total d'essais est publié ;
- format et pré-inscription : `docs/etape3_registre.md`.

## 9. Verdicts

Inchangés : confirmée / rejetée / indéterminée. Le code est
`etude_micro.verdict`, testé cas par cas.

**Fragile** : le signe de Δℓ change entre variantes. Le verdict est alors
indéterminé.

**Effet économique** : ΔEV = Δp × (1 + 0,92). +2 points de taux de gain
donnent 0,02 × 1,92 = 3,84 % de la mise par ordre. Ce calcul suppose :
- un règlement binaire, sans égalité ;
- un payout constant de 92 %.

Ce n'est pas un gain promis : il reste soumis à l'incertitude, à la
stabilité dans le temps et à la qualité des données.

## 10. Protocole : aucune écriture, charge limitée

- **Lectures** : connexion passée en lecture seule, SELECT uniquement.
- **Aucune écriture en base** :
  - sorties en **CSV uniquement**, par Telegram ;
  - pas de table dérivée (décision du 27/10).
- **Preuve** : test de bout en bout de `/micro_essai` sur SQLite, avec
  l'empreinte du fichier inchangée.
- **Arrêts** :
  - un test anti-fuite échoue, en test ou sur une vraie fenêtre ;
  - moins de 80 % de fenêtres VALID ;
  - le coût dépasse les bornes ;
  - l'historique de ticks est perdu avant la passe.

## 11. Contrôles préalables : `/micro_essai` (admin)

`/micro_essai [PAIRE] [AAAA-MM-JJ]`. Par défaut : EURUSD_otc, le 20/10. Le
jour doit être dans l'exploration.

1. **Coût** : lignes, Mo compressés, temps par étape, mémoire,
   extrapolation.
2. **Couverture et qualité** :
   - minutes présentes, fermées, à flux doublé ;
   - concordance ;
   - signaux (avec issue / égalité / manquante) ;
   - classes de fenêtre ;
   - part du niveau 2 ;
   - variables manquantes.
3. **Anti-fuite** sur chaque vraie fenêtre.
4. **Disponibilité** sur les ordres réels (§0).
5. **Puissance** depuis l'exploration, et seuils des regards.

**Fiches** :
- `micro_essai.csv` : qualité et variables par signal × traitement × k × w,
  **sans aucune issue** ;
- `ordres_disponibilite.csv`.

**Coût réseau** : environ 1 Mo pour l'échantillon. Le rejeu de l'exploration
(bougies de 20 paires, environ 30 Mo) n'est lu que si le rejeu quotidien
n'est pas en mémoire.

## 12. Stockage : prérequis indépendant

À confirmer séparément, avant la passe complète :
- **Marge réelle sur Neon** : limite et unité, lues dans la console.
- **Volume temporaire** : aucun en base, la mémoire est mesurée par
  `/micro_essai`.
- **Coût et temps du parcours des chemins compressés** : mesurés par
  `/micro_essai`. Le transfert est un plafond distinct du stockage.
- **Sauvegarde vérifiée** (`pg_dump`, puis restauration d'essai).
- **Risque d'interruption de la collecte** avant la fin de la
  confirmation : une décision de continuité est à prendre.

## 13. Autorisations, et ordre d'exécution

**Autorisé, fait dans cette version** :
- vérification des composants et des frontières temporelles ;
- tests anti-fuite et de concordance ;
- mesure du coût, estimation de la puissance et diagnostic des ordres, dans
  une commande en lecture seule ;
- registre et modèle de pré-inscription.

**Pas encore autorisé** :
- passe complète ;
- table dérivée ;
- modification du collecteur ou de la stratégie ;
- sélection définitive des hypothèses sans gel formel.

**Ordre** :

| Étape | État |
|---|---|
| 1. Corriger et tester A à D | fait (ce document, 4 fichiers de tests) |
| 2. Concordance et anti-fuite sur un échantillon contrôlé | `/micro_essai`, à lancer après déploiement |
| 3. Coût, couverture, puissance | même commande |
| 4. Stockage et continuité sécurisés | de ton côté (§12) |
| 5. Gel des 4 hypothèses et des paramètres | après la passe de sélection |
| 6. Plan corrigé soumis avant la passe complète | ce document, plus les résultats de `/micro_essai` |

## 14. Fichiers

| Fichier | Rôle |
|---|---|
| `maxprofit/apprentissage/microstructure_pre.py` | fenêtres, variables A à E, classes, niveaux, autotest anti-fuite ; pur |
| `maxprofit/apprentissage/etude_micro.py` | issue, périodes, préparation, logistique L2, Δℓ sur mêmes signaux, bootstrap, terciles, Holm / BH, verdicts, puissance, dépense d'α, registre, gel ; pur |
| `maxprofit/live/etude3.py` | `/micro_essai` |
| `maxprofit/apprentissage/historique.py` | option `sans_issue` (signaux à égalité ou sans issue, avec contexte) |
| `maxprofit/apprentissage/audit_ticks.py` | option `garder_classes` (concordance minute par minute) |
| `tests/test_disponibilite_collecte.py`, `tests/test_microstructure_pre.py`, `tests/test_etude_micro.py`, `tests/test_etude3.py` | disponibilité, définitions, anti-fuite, protocole, bout en bout |
| `docs/etape3_registre.md` | registre et pré-inscription |
