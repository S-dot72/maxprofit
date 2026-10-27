# Étape 3 — Plan d'exécution, version 4

**Statut** :
- plan approuvé sous conditions le 27/10 ;
- décisions du même jour : 2 hypothèses principales, plafond de 4 000
  signaux, règle de retrait des variables quasi constantes ;
- autorisé : l'essai technique de la passe optimisée (`/micro_essai`) ;
- la passe complète reste suspendue. Elle attend la validation du coût
  réel, la pré-inscription des deux hypothèses et la continuité de la
  collecte.

Les résultats actuels justifient une étude **exploratoire**. Ils ne
permettent encore aucune décision sur l'efficacité prédictive des ticks.

**Ce qui ne change pas** :
- analyse hors ligne et en lecture seule ;
- aucune modification de la stratégie, des signaux, des échéances, du
  collecteur ni de T0/T1/T2/T3 ;
- aucune écriture de séries de ticks en base ;
- un résultat négatif bien établi est un résultat.

La version 3 répondait aux points A à D de la revue du 27/10. La version 4
applique les décisions qui ont suivi le premier `/micro_essai`.

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

**Résultat du 27/10** (31 ordres, `/micro_essai`) :
- le prix vu par la course est présent dans le flux stocké à ± 1 s **31
  fois sur 31** ;
- c'est le dernier tick stocké au même instant 18 fois sur 31. Les autres
  fois, le tick de même prix est à environ 100 ms, soit la précision de la
  conversion d'horloge ; un écart de 1 à 9 points correspond à un tick
  d'écart ;
- la course décide entre 1,2 et 6,2 s après la frontière de minute
  (médiane 2,4 s). Les 4 à 26 ticks stockés entre d et la décision sont
  ignorés par le niveau 2, ce qui est prudent.

**Hypothèse, non prouvée** : le tick le plus proche est presque toujours à
100, 200, 300 ou 400 ms. Cela suggère une latence d'environ 100 ms entre
l'horodatage du broker et la réception.

**Aucune divergence n'est vue.** Ces ordres datent du 24/10 et après ; le
code de collecte est inchangé depuis le 27/09.

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
- **Maturité** : une issue n'est lue qu'une heure après son échéance. La
  bougie de sortie d'une paire calme ne s'écrit qu'au tick suivant.
  - Une période n'est figée qu'une fois toutes ses issues mûres :
    `issue_mure`, `periode_figeable`.
  - Seules les issues mûres comptent dans le N d'un regard.

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

**Ce que le premier essai a appris** (une seule paire-jour, donc
indicatif) :
- **Flux OTC presque périodique** : environ 2 ticks distincts par seconde.
  Le délai entre retournements (C5) vaut presque toujours 1,0 s. C'est une
  **limite de résolution** de la mesure : les délais sont des multiples de
  la cadence du flux. On n'en conclut rien sur le phénomène lui-même.
- **Ticks bruts et distincts** ne diffèrent que sur B3 et B6, et seulement
  dans les minutes à flux doublé.

## 4 bis. Retrait des variables quasi constantes (règle gelée)

La règle est appliquée sur l'**exploration seule**, **sans aucune issue**.
La liste des variables conservées et retirées est gelée avec le registre
avant toute analyse des résultats (`retrait_des_quasi_constantes`,
version 1).
- **IQR > 0** : conservée.
- **IQR nul** : l'IQR seul ne juge pas une variable rare, car un
  événement binaire peu fréquent a un IQR nul. Elle est conservée si au
  moins 30 valeurs **et** au moins 5 % des valeurs diffèrent du mode.
  Sinon, elle est retirée.
- **Moins de 30 valeurs mesurées** : retirée.
- **Trace, pour chaque candidate** (variable × fenêtre) :
  - valeurs, manquantes, valeurs distinctes ;
  - Q1, Q3, IQR ;
  - mode, nombre et part hors du mode ;
  - décision, raison, version de la règle.
- **Ticks bruts : diagnostic seulement**, jamais en sélection. B3 et B6
  bruts mesurent le doublement du flux, un artefact de collecte
  (`ARTEFACTS_BRUTS`). Leurs versions distinctes restent candidates.
- **A9, D3 et C5** : jugés par cette règle sur toute l'exploration, pas sur
  l'échantillon d'une paire-jour.
- **Une variable pré-inscrite retirée par la règle** : son hypothèse est
  abandonnée (indéterminée), jamais remplacée.

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

## 6. Hypothèses choisies a priori, découpage et gel

**Les deux hypothèses principales sont choisies et pré-inscrites AVANT tout
examen de leur lien avec les issues** (décision du 27/10).
- **Critères** : définition, interprétabilité, fiabilité de la mesure, et
  un mécanisme plausible.
- **Jamais** les résultats de l'exploration. Aucune relation variable–issue
  n'a été calculée à ce jour.
- Choisies ainsi, elles n'héritent d'aucun biais de sélection parmi les 92.
  Les 90 autres candidates restent des **analyses secondaires
  exploratoires**, en BH. Elles ne confirment rien.

**Candidates proposées, à valider** :

| | Variable, fenêtre | Tercile, sens | Mécanisme | Fiabilité de mesure |
|---|---|---|---|---|
| **H1** | `A7_penetration`, w = 300 s | tercile haut : **moins** de gains | ZoneH1 parie sur un rebond sur la zone. Une pénétration profonde au-delà du niveau, contre le trade, dans les 5 min avant d dit que la zone a déjà cédé. | en marges de zone, identique en ticks bruts et distincts, indépendante de la cadence du flux |
| **H2** | `D1_traversees`, w = 300 s | tercile haut : **moins** de gains | Un niveau franchi à répétition ne fait pas barrière : le prix oscille autour, et la prémisse du rebond n'est pas remplie. | compte de changements de côté, identique en bruts et distincts |

- H1 et H2 mesurent toutes deux la fragilité de la zone : elles sont
  corrélées. Holm reste valide, simplement conservateur.
- **Alternative** pour diversifier : `B5_ratio_vol`, w = 300 s, tercile
  haut, moins de gains (la volatilité s'accélère juste avant d : risque de
  cassure). Elle est moins robuste, car elle dépend de la cadence du flux.
- **Réserve** : le flux OTC semble généré. Un mécanisme « de marché » y
  reste une hypothèse. Le test dit si la prémisse de ZoneH1 (la zone tient)
  se voit dans les ticks.

**Périodes** (inchangées) :
- **Exploration** : avant le 21/10 00:00 UTC. On y calcule, sans aucune
  issue, les bornes des terciles, la préparation et la règle de retrait.
- **Validation** : du 21/10 au 28/10 00:00 UTC. Elle sert à réestimer le
  modèle sur exploration + validation, et aux analyses secondaires.

**Gel en deux temps** :
1. **Gel des hypothèses**, dès ta validation de H1 et H2 : variable,
   fenêtre, tercile, sens, horizon, règles (retrait, préparation,
   regards, seuils, périodes, sensibilités). Une empreinte SHA-256 est
   notée au registre et dans un commit.
2. **Gel des paramètres**, après la passe de sélection : bornes des
   terciles, préparation, coefficients et liste des variables retenues.
   Ces paramètres sont **déterminés mécaniquement** par les règles du
   premier gel, à partir des seules données d'avant le 28/10. Une seconde
   empreinte les fige.
3. **Confirmation** : choix à faire (voir §13) :
   - **(a) strict** : elle s'ouvre au premier minuit UTC qui suit le gel
     des paramètres ;
   - **(b)** : elle s'ouvre au premier minuit UTC qui suit le gel des
     hypothèses. C'est possible parce que rien de ce qui reste à calculer
     ne dépend d'un choix, ni des données de confirmation. Le code refuse
     tout calcul de confirmation avant le second gel (`exiger_le_gel`).
   - **Quarantaine** : ce qui tombe entre le 28/10 et l'ouverture n'est ni
     choisi ni confirmé.

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
- α = 0,025 unilatéral (le pas le plus sévère de Holm pour 2 hypothèses),
  puissance 80 %.

**Mesuré le 27/10** sur l'exploration : 1 025 signaux en 10 jours (environ
102 par jour), p = 53,3 %, ρ = 0,085 (paire × jour), DEFF = 1,66.

| N signaux de niveau 2 | Jours (à 102/j) | Effet détectable | Demi-IC 95 % |
|---|---|---|---|
| 500 | ≈ 5 | ≈ 17 points | ≈ 12 points |
| 1 000 | ≈ 10 | ≈ 12 points | ≈ 8,5 points |
| 2 000 | ≈ 20 | ≈ 8,5 points | ≈ 6,0 points |
| 4 000 | ≈ 39 | ≈ 6,0 points | ≈ 4,2 points |

**Décision du 27/10** :
- 2 hypothèses principales, Holm à 5 % ;
- plafond de 4 000 signaux ;
- effet recherché : environ 6 points.

Le plafond n'est pas une garantie de puissance. Le calcul sera refait avec
le nombre réel de blocs, la corrélation observée et la correction. Un effet
**détectable** n'est pas forcément **économiquement utile**.

**Résultat négatif** : on publie les effets encore compatibles avec les
données. On ne prétend pas exclure un gain de +2 points, ce qui demanderait
environ 36 000 signaux.

**Règle de poursuite** :
- **Regards** à N = 500, 1 000, 2 000 et 4 000 signaux de niveau 2 en
  confirmation, issues mûres.
- **Dépense d'α** de type O'Brien-Fleming (Lan-DeMets). Le seuil nominal de
  la famille de Holm à chaque regard est l'**incrément** de dépense : une
  borne de Bonferroni entre regards, conservatrice.
  - Seuils : environ 3·10⁻⁸, 9·10⁻⁵, 0,0055 et 0,044.
  - Les deux premiers regards ne peuvent pas confirmer avec 1 000 tirages :
    c'est voulu.
- **Arrêt de la collecte** pour une raison étrangère aux résultats
  (stockage, transfert, broker) : le dernier regard disponible devient final
  et reçoit l'α restant.
- **À 4 000 signaux** sans verdict : **indéterminée**, définitivement, avec
  les bornes.
- **Interdit** : changer les regards, les seuils ou le plafond après
  l'ouverture de la confirmation.

## 8. Comparaisons multiples et registre

**Corrections** :
- **Principales** : 2 au plus, choisies a priori, Holm à 5 %, à chaque
  regard au seuil nominal.
- **Exploration** : 92 variables plus les échéances secondaires, BH à 10 %.
  Une découverte BH reste **exploratoire**, jamais une preuve.

**Sensibilités de chaque principale**, toutes comptées au registre, soit
2 × 3 × 2 × 2 = 24 variantes. Elles ne sont calculées **que** pour les deux
hypothèses retenues ; la sélection n'utilise que les ticks distincts à
k = 0 :
- ticks bruts, à titre de diagnostic seulement / distincts ;
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

## 11. `/micro_essai` v2 : essai technique de la passe optimisée (admin)

**Premier essai, le 27/10** (v1, une paire-jour) : 49,5 s par paire-jour,
soit environ 5 h extrapolées pour la passe. C'est hors bornes. Les causes :
- les signaux étaient recalculés ;
- toute la journée était lue et décodée deux fois ;
- les 24 variantes étaient calculées ;
- l'autotest recopiait toute la journée.

**Commande** : `/micro_essai [AAAA-MM-JJ] [PAIRE]`. Par défaut, le 20/10 et
**toutes** les paires collectées. Le jour doit être dans l'exploration. Le
rejeu quotidien doit être en mémoire avec ses niveaux de zone ; sinon,
l'essai le dit et attend.

**La passe optimisée, telle que l'essai la mesure** :
- **Signaux** : ceux du rejeu quotidien. Le niveau de zone est relevé
  **passivement** dans ce rejeu (`niveaux`). La stratégie, les signaux et
  les issues ne changent pas.
- **Lectures** : seulement les fenêtres [d − 900, d) des signaux,
  fusionnées. Les bougies sont lues une minute de plus de chaque côté pour
  la concordance.
- **Décodage** : une seule fois par chemin, avec un cache partagé avec
  l'audit.
- **Calcul** : ticks distincts, k = 0, les 4 fenêtres, plus l'autotest
  anti-fuite par signal et par fenêtre.

**Vérifications publiées** (demandées le 27/10) :
1. **Temps réel** : durée totale, paires-jours, signaux, octets de chemins
   exacts, bougies, requêtes. Le pic mémoire est mesuré dans une seconde
   passe sur le paire-jour le plus chargé. L'extrapolation est comparée aux
   bornes (30 min, 150 Mo). Au-delà : suspendre et diagnostiquer.
2. **Lecture seule** :
   - toutes les requêtes passent par `ConnexionTracee`, qui les compte et
     **refuse** tout ce qui n'est pas SELECT, WITH ou SHOW ;
   - `transaction_read_only` est lu au début et à la fin (PostgreSQL) ;
   - une empreinte de fichier ne vaut que pour SQLite.
3. **Rejeu inchangé** : un paire-jour est rejoué avec la stratégie seule,
   puis comparé au rejeu en mémoire (décisions, issues, contexte). Un
   second contrôle vérifie que le relevé passif ne change rien.
4. **Concordance** : on publie les classes des minutes lues, et les causes
   d'exclusion du niveau 2. Aucune correction, aucune interpolation.
5. **Reproductibilité** : le paire-jour le plus chargé est relu et
   recalculé. Les empreintes des lignes lues et des variables doivent être
   identiques.

En plus : qualité des fenêtres, ordres réels face au flux (§0), puissance
(2 hypothèses, plafond 4 000).

**Fiches** :
- `micro_essai.csv` : distincts, k = 0, une ligne par signal × fenêtre,
  **sans aucune issue** ;
- `ordres_disponibilite.csv`.

**Déploiement** : `VERSION_EXPERIENCES` passe à 17, ce qui force le rejeu
quotidien au démarrage et met les niveaux en mémoire.

**Premier essai v2, le 27/10** (20/10, 20 paires, 123 signaux) :

| Vérification | Résultat |
|---|---|
| Temps réel | 65,9 s pour 20 paires-jours, soit 3,3 s chacun ; extrapolé à environ 20 min et 9 Mo ; pic mémoire de 7 Mo. **Dans les bornes.** Répartition : lecture 19 s (latence réseau, 187 requêtes), décodage 6 s, concordance 8 s, variables 33 s |
| Lecture seule | `transaction_read_only` = on au début et à la fin ; 277 SELECT, 0 refusée |
| Rejeu inchangé | « NON » sur EURUSD_otc : **défaut du contrôle**, pas du rejeu. `marquer_qualite` ajoute `donnees_douteuses` au contexte de chaque signal après le rejeu, et le contrôle le comptait. Corrigé : l'annotation est écartée en le disant, et tous les champs qui diffèrent sont nommés. **À refaire.** Le relevé passif est sans effet |
| Concordance | 1 259 minutes lues, toutes « identique » ; 123 signaux sur 123 au niveau 2 |
| Reproductibilité | empreintes identiques à la relecture |
| Anti-fuite | 492 sur 492 |

**Qualité des fenêtres** : 93 % VALID. Les 8 PARTIAL se répartissent
ainsi :
- 2 viennent d'un gel de 11 s ;
- 6 viennent de minutes déclarées **fermées** par le catalogue du broker
  alors que les cotations continuaient (couverture 100 %, aucun écart). La
  règle, fixée d'avance, n'est pas changée.

**Aperçu de la règle de retrait**, sur un seul jour : elle n'est **pas
gelée** ici, elle le sera sur toute l'exploration.
- H1 (`A7_penetration`) et H2 (`D1_traversees`) à 300 s sont bien mesurées :
  85 et 19 valeurs distinctes, IQR non nul.
- C5 a un IQR nul à 300 et 900 s, mais il reste conservé, car 61 à 72 % de
  ses valeurs diffèrent du mode, souvent de quelques millisecondes. C'est
  la limite de résolution déjà notée (§4) : C5 reste exploratoire.

## 12. Stockage et transfert : prérequis indépendants

**Limites de l'offre gratuite de Neon** (documentation lue le 27/10) :
- **Stockage** : **1 Go par projet** (0,5 Go était l'ancienne limite). La
  console compte en Mo décimaux.
  - Au rythme d'environ 22 Mo par jour, depuis 465 Mo le 26/10, la limite
    est atteinte vers mi-novembre.
  - Au-delà, les écritures échouent.
- **Transfert réseau** : **5 Go par projet et par mois**, remis à zéro à
  chaque période de facturation.
  - 2,39 Go étaient consommés du 19/10 au 26/10, soit environ 0,34 Go par
    jour. Le plafond tomberait vers le **3 novembre**.
  - Le calcul est alors **suspendu** jusqu'à la période suivante (vers le
    19/11). C'est **le** risque de continuité, avant le stockage.
- **La passe optimisée** est estimée à quelques dizaines de Mo, négligeable
  à côté des rejeux quotidiens. Un diagnostic des sources du transfert est
  proposé à part.

**Reste à confirmer avant la passe complète** :
- la sauvegarde vérifiée (`pg_dump`, puis restauration d'essai) ;
- la continuité de la collecte sur la durée de la confirmation (environ
  40 jours de niveau 2), compte tenu du transfert.

## 13. Autorisations, et ordre d'exécution

**Autorisé, fait** :
- composants, frontières, anti-fuite et concordance testés ;
- `/micro_essai` v2 avec les cinq vérifications ;
- relevé passif du niveau de zone dans le rejeu quotidien ;
- protocole à 2 hypothèses, plafond 4 000, règle de retrait, maturité.

**Pas encore autorisé** :
- passe complète ;
- table dérivée ;
- modification du collecteur ou de la stratégie ;
- sélection définitive des hypothèses sans gel formel.

**Décisions du 27/10 (soir)** :
- **H1 et H2 validées comme hypothèses à tester.** Ce n'est pas une
  validation de leur efficacité.
- **Confirmation : option (a)**, au premier minuit UTC qui suit le gel des
  paramètres.
- **Transfert réseau** : mesurer en lecture seule avant toute modification
  (`/transfert`, `docs/transfert_diagnostic.md`).

**Brouillon de pré-inscription** : `docs/etape3_preinscription.json`, non
gelé. Il fixe :
- les formules exactes de H1 et H2 : unité, sens, traitement des valeurs
  manquantes, oscillations, déduplication ;
- la population et les exclusions ;
- l'issue historique, distincte de l'issue réglée par le broker ;
- le modèle de référence et les modèles comparés ;
- le critère principal (Δℓ face à la référence, IC par blocs, Holm sur
  2) ;
- l'effet pratique, les verdicts, les sensibilités, les regards, les
  périodes et les interdits.

Un test vérifie qu'il ne diverge pas du code. Le gel (empreinte dans un
commit) viendra après la confirmation du rejeu et ton accord sur ce
brouillon.

**Ordre** :

| Étape | État |
|---|---|
| 1. Corriger et tester A à D | fait |
| 2. Concordance et anti-fuite sur échantillon | fait (27/10), à refaire avec la v2 |
| 3. Coût réel de la passe optimisée | `/micro_essai` v2, après déploiement |
| 4. Stockage, transfert, sauvegarde, continuité | de ton côté (§12) |
| 4 bis. Diagnostic du transfert | `/transfert`, 24 h de mesure contenant un rejeu |
| 5. Gel des 2 hypothèses | brouillon prêt ; après la confirmation du rejeu et ton accord |
| 6. Passe de sélection, puis gel des paramètres | après ton feu vert, transfert et continuité réglés |

## 14. Fichiers

| Fichier | Rôle |
|---|---|
| `maxprofit/apprentissage/microstructure_pre.py` | fenêtres, variables A à E, classes, niveaux, autotest anti-fuite ; pur |
| `maxprofit/apprentissage/etude_micro.py` | issue, périodes, préparation, logistique L2, Δℓ sur mêmes signaux, bootstrap, terciles, Holm / BH, verdicts, puissance, dépense d'α, registre, gel ; pur |
| `maxprofit/live/etude3.py` | `/micro_essai` v2 (passe fenêtrée, `ConnexionTracee`, contrôles) |
| `maxprofit/apprentissage/historique.py` | options `sans_issue` et `niveaux` (relevés passifs) |
| `maxprofit/live/apprenti.py` | niveaux de zone du rejeu en mémoire (`niveaux_en_memoire`) |
| `maxprofit/apprentissage/audit_ticks.py` | option `garder_classes` (concordance minute par minute) |
| `tests/test_disponibilite_collecte.py`, `tests/test_microstructure_pre.py`, `tests/test_etude_micro.py`, `tests/test_etude3.py` | disponibilité, définitions, anti-fuite, protocole, bout en bout |
| `docs/etape3_registre.md` | registre et pré-inscription |
