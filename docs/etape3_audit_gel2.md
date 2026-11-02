# Étape 3 — Audit indépendant des résultats du gel 2 (02/11)

Demandé le 02/11, avant toute nouvelle expérience. L'audit est **indépendant** :
- le code est neuf (`outils/audit_gel2.py`) et n'importe rien du projet ;
- il repart du seul `selection_signaux.csv` rendu par `/micro_selection`, et
  du document du gel ;
- aucune lecture en base, aucune écriture.

Il corrige trois affirmations du rapport du 02/11
(`docs/etape3_selection_resultats.md`, §4).

## 1. Cohérence des données : conforme

- **1 806 signaux**, sans doublon (paire, décision).
- **Entonnoir** :

  | Période | Signaux | Niveau 2 | VALID | Issue décisive |
  |---|---|---|---|---|
  | exploration | 1 025 | 1 011 | 991 | 991 |
  | validation | 781 | 762 | 749 | 742 |

  En validation, 4 égalités et 3 issues manquantes sont écartées. Le
  total, **1 733**, est la population de tous les tableaux des deux rapports.
- **Périodes** : aucun signal n'est mal classé, et aucune décision n'est
  postérieure au 28/10.
- **Ouverture de la confirmation** : la dernière bougie de sortie (28/10 à
  00:01 UTC) précède l'ouverture du 03/11.
- **Colonnes** : la colonne « principale » est cohérente avec le niveau et la
  classe, et l'issue avec y.

## 2. Recalcul du gel 2 : identique

Le code indépendant redonne exactement les paramètres du gel :

| Paramètre | Écart maximal au gel |
|---|---|
| Terciles de H1 et H2 | 4·10⁻⁷ |
| Préparation (médianes, moyennes, écarts) | 10⁻¹³ |
| 45 coefficients du modèle de référence M0 (1 733 signaux) | 2·10⁻¹⁵ |

## 3. Rendement aux payouts réellement observés : CORRECTION

Le rapport supposait un payout de 92 % pour tous les signaux. Or le payout
relevé à la décision a une **médiane de 82 %** (entre 20 et 92 %). Seuls
**620 signaux sur 1 733 (36 %) sont au maximum de 92 %**.

| Population | Taux de gain | Rendement par mise au payout observé |
|---|---|---|
| tous les signaux | 52,6 % | négatif |
| sous le plafond (payout < 92 %) | 50,7 % (1 113) | **−16,1 %** |
| **au plafond** (payout ≥ 92 %) | **56,0 %** (620) | **+7,5 %** |
| — exploration | 54,9 % (377) | +5,4 % |
| — validation | 57,6 % (243) | +10,6 % |

La course n'entre qu'au payout maximum. Ce filtre est **antérieur à toute
analyse**, donc la population au plafond est la population jouable définie a
priori. Les rendements « +6 à +8 % » du rapport ne valent que pour elle.

**Réserve majeure** : l'issue est un PROXY (clôture de la bougie du signal,
puis bougie complète à +900 s). La course, elle :
- entre après la confirmation M1 ;
- passe l'ordre environ 2 s après la décision, au prix du broker ;
- est réglée par le broker.

Le `/protocole` donnait 51,1 % pour « ZoneH1 jouée ». **Il faut rapprocher
le proxy des ordres réels**, signal par signal, avant de croire au +7,5 %.

## 4. La piste USD : AFFAIBLIE

**Taux et écarts, avec leur incertitude**. Les IC sont à 95 % ; les blocs
paire × jour sont la référence.

| | Exploration | Validation | Les deux |
|---|---|---|---|
| USD | 54,8 % (442) | 56,3 % (371) | 55,5 % (813) |
| autres | 52,1 % (549) | 46,9 % (371) | 50,0 % (920) |
| écart, IC paire × jour | −5,1 à +10,6 | −1,0 à +19,8 | −1,0 à +11,7 |
| écart, IC jour | −6,9 à +12,2 | +0,3 à +21,2 | −1,3 à +13,8 |
| rendement USD au payout observé | −2,8 % | −0,7 % | **−1,9 %** (IC −11,2 à +6,8) |

Le bootstrap par grappes de paires donne des IC plus étroits. Mais il ne
porte que sur 7 grappes, ce qui le rend optimiste : il n'est pas retenu.

**Test de permutation.** Les 7 paires USD sont comparées aux 77 520 groupes
possibles de 7 paires parmi 20.

| Population | Exploration | Validation | Les deux |
|---|---|---|---|
| tous les signaux | p = 0,24 | p = 0,020 | p = 0,013 |
| **au plafond** | **p = 0,31** | **p = 0,20** | **p = 0,13** |

**Lecture** :
- En exploration, USD n'a rien de particulier. L'effet vient de la
  validation, là où la piste a été remarquée.
- Dans la population jouable, le groupe USD ne se distingue pas d'un groupe
  quelconque de 7 paires.
- Les p ne tiennent pas compte des autres découpages examinés avant
  (paires, créneaux, 92 variables) : ils sont optimistes.

## 5. Dépendance et puissance : CORRECTION

**Dépendance.** Les signaux d'une même paire et d'un même jour sont
corrélés, car ils viennent souvent de la même zone.

| Population | Corrélation intra-bloc | Effet de plan | Taille effective |
|---|---|---|---|
| paires USD | 0,171 | 2,52 | 813 / 2,52 ≈ 320 |
| paires USD au plafond | 0,158 | 1,77 | — |

Le chiffre de « 1 300 signaux » du rapport **est faux**. Il ignorait cette
dépendance, et prenait pour vrai l'effet observé, qui est gonflé par la
sélection (malédiction du gagnant).

**Puissance** pour la population jouable, paires USD au plafond :
- environ 19 à 20 signaux par jour ;
- effet de plan de 1,77 ;
- test unilatéral à 5 %, puissance de 80 % ;
- H0 : taux ≤ 52,08 %.

| Taux réel supposé | Signaux nécessaires | Durée |
|---|---|---|
| 58 % (l'observé, gonflé) | 763 | ≈ 40 jours |
| 57 % | 1 111 | ≈ 58 jours |
| 56 % | 1 760 | ≈ 92 jours |
| 55 % | 3 184 | ≈ 157 jours |

Les regards intermédiaires (O'Brien-Fleming) ajoutent quelques pour cent.

## 6. Conclusions de l'audit

1. Les données, les frontières et les paramètres du gel 2 sont **justes et
   reproductibles**. La confirmation de H1 et H2 peut s'ouvrir le 03/11.
2. **La piste USD ne résiste pas à l'audit** comme hypothèse prioritaire :
   - en rendement observé, elle est négative sur l'ensemble ;
   - dans la population jouable, elle est indiscernable d'un groupe de
     paires tiré au hasard ;
   - un test honnête demanderait de 2 à 5 mois.
3. **Fait nouveau** : les signaux au plafond gagnent 56,0 %, contre 50,7 %
   pour les autres. Ce filtre est celui de la course, fixé avant l'analyse.
   Mais c'est un proxy : la question décisive est le **rapprochement avec les
   ordres réels**.
4. Rien de tout cela ne justifie de passer en production ni d'engager de
   l'argent réel.
