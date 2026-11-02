# Étape 3 — Audit indépendant des résultats du gel 2 (02/11)

Demandé le 02/11, avant toute nouvelle expérience. L'audit est **indépendant** :
- le code est neuf (`outils/audit_gel2.py`) et n'importe rien du projet ;
- il repart du seul `selection_signaux.csv` rendu par `/micro_selection`, et
  du document du gel ;
- aucune lecture en base, aucune écriture.

Il corrige trois affirmations du rapport du 02/11
(`docs/etape3_selection_resultats.md`, §4).

> **Erratum du 02/11, même jour.** La première version de cet audit
> prenait la colonne `payout` pour le payout appliqué. C'est le payout du
> **flux** (table `payouts`). Le broker applique **min(flux + 8, 92)**
> (règle mesurée sans exception sur des ordres réels, `core/payout.py`), et
> la course entre dès que le flux vaut **84** (`au_plafond`). Les §3 à §6
> sont corrigés ; `outils/audit_gel2.py` aussi. Les §1 et §2 ne changent
> pas.

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

## 3. Rendement aux payouts réellement appliqués : CORRECTION

Le rapport supposait un payout de 92 % pour tous les signaux. Or le payout
du flux relevé avant la décision a une **médiane de 82 %** (entre 20 et
92 %). Le payout appliqué vaut min(flux + 8, 92) : il atteint 92 % dès un
flux de 84. **829 signaux sur 1 733 (48 %) sont jouables**, au sens du filtre
de la course.

| Population | Taux de gain | Rendement par mise au payout appliqué |
|---|---|---|
| tous les signaux | 52,6 % (1 733) | −5,3 % |
| sous le plafond (flux < 84) | 51,2 % (904) | **−13,5 %** |
| **jouables** (flux ≥ 84, payout de 92 %) | **54,0 %** (829) | **+3,8 %** |
| — exploration | 54,4 % (493) | +4,4 % |
| — validation | 53,6 % (336) | +2,9 % |
| — IC 95 %, blocs paire × jour | 50,1 à 58,1 % | −3,9 à +11,6 % |

Le filtre de la course est **antérieur à toute analyse** : les jouables
sont la population définie a priori. Leur rendement n'est pas
significativement positif.

**Découpage après coup, sans valeur de règle.** Parmi les jouables, ceux au
flux de 92 gagnent 56,0 % (620), ceux au flux de 84 à 91 gagnent 48,3 %
(209). Les deux paient pourtant 92 %. Ce découpage a été vu en regardant les
résultats : la première version de cet audit le présentait à tort comme le
filtre de la course.

**Réserve majeure** : l'issue est un PROXY (clôture de la bougie du signal,
puis bougie complète à +900 s). La course, elle :
- entre après la confirmation M1 ;
- passe l'ordre environ 2 s après la décision, au prix du broker ;
- est réglée par le broker.

Le `/protocole` donnait 51,1 % pour « ZoneH1 jouée ». **Il faut rapprocher
le proxy des ordres réels**, signal par signal, avant de croire au +3,8 %.

## 4. La piste USD : AFFAIBLIE

**Taux et écarts, avec leur incertitude**. Les IC sont à 95 % ; les blocs
paire × jour sont la référence.

| | Exploration | Validation | Les deux |
|---|---|---|---|
| USD | 54,8 % (442) | 56,3 % (371) | 55,5 % (813) |
| autres | 52,1 % (549) | 46,9 % (371) | 50,0 % (920) |
| écart, IC paire × jour | −5,1 à +10,6 | −1,0 à +19,8 | −1,0 à +11,7 |
| écart, IC jour | −6,9 à +12,2 | +0,3 à +21,2 | −1,3 à +13,8 |
| rendement USD au payout appliqué | −0,5 % | +1,7 % | **+0,5 %** (IC −8,9 à +9,3) |

Le bootstrap par grappes de paires donne des IC plus étroits. Mais il ne
porte que sur 7 grappes, ce qui le rend optimiste : il n'est pas retenu.

**Test de permutation.** Les 7 paires USD sont comparées aux 77 520 groupes
possibles de 7 paires parmi 20.

| Population | Exploration | Validation | Les deux |
|---|---|---|---|
| tous les signaux | p = 0,24 | p = 0,020 | p = 0,013 |
| **jouables** (flux ≥ 84) | **p = 0,16** | **p = 0,14** | **p = 0,028** |

**Jouables USD** : 57,3 % (412), rendement **+10,0 %** (IC paire × jour
−1,5 à +20,9 %) ; autres jouables : 50,8 % (417), −2,4 %.

**Lecture** :
- En exploration, USD n'a rien de particulier (p = 0,24 sur tous les
  signaux, 0,16 sur les jouables). L'effet vient surtout de la validation,
  là où la piste a été remarquée.
- Sur les deux périodes réunies, le groupe USD jouable se distingue d'un
  groupe quelconque de 7 paires (p = 0,028). La première version, avec le
  mauvais payout, donnait p = 0,13.
- Ces p ne tiennent pas compte des autres découpages examinés avant
  (paires, créneaux, 92 variables) : ils sont optimistes.

## 5. Dépendance et puissance : CORRECTION

**Dépendance.** Les signaux d'une même paire et d'un même jour sont
corrélés, car ils viennent souvent de la même zone.

| Population | Corrélation intra-bloc | Effet de plan | Taille effective |
|---|---|---|---|
| paires USD | 0,171 | 2,52 | 813 / 2,52 ≈ 320 |
| paires USD jouables (flux ≥ 84) | 0,144 | 1,83 | 412 / 1,83 ≈ 225 |

Le chiffre de « 1 300 signaux » du rapport **est faux**. Il ignorait cette
dépendance, et prenait pour vrai l'effet observé, qui est gonflé par la
sélection (malédiction du gagnant).

**Puissance** pour la population jouable, paires USD au flux ≥ 84 :
- environ 26 signaux par jour ;
- effet de plan de 1,83 ;
- test unilatéral à 5 %, puissance de 80 % ;
- H0 : taux ≤ 52,08 %.

| Taux réel supposé | Signaux nécessaires | Durée |
|---|---|---|
| 58 % (proche de l'observé, gonflé) | 788 | ≈ 31 jours |
| 57 % | 1 148 | ≈ 44 jours |
| 56 % | 1 818 | ≈ 70 jours |
| 55 % | 3 292 | ≈ 128 jours |

Les regards intermédiaires (O'Brien-Fleming) ajoutent quelques pour cent.

## 6. Conclusions de l'audit (corrigées)

1. Les données, les frontières et les paramètres du gel 2 sont **justes et
   reproductibles**. La confirmation de H1 et H2 peut s'ouvrir le 03/11.
2. **La population jouable de la course** (flux ≥ 84) gagne 54,0 % au proxy,
   soit un rendement de +3,8 % non significatif (IC −3,9 à +11,6 %).
3. **La piste USD** reste une hypothèse née après coup :
   - jouable, elle rapporte +10,0 % au proxy (IC −1,5 à +20,9 %) ;
   - sa permutation donne p = 0,028 sur les deux périodes, mais 0,16 en
     exploration seule ;
   - un test honnête demanderait de 1 à 4 mois.
   Décision du 02/11 : elle n'est **pas** gelée comme expérience principale.
4. Tout repose sur un proxy. La question décisive est le **rapprochement
   avec les ordres réels** (`/rapprochement`).
5. Rien de tout cela ne justifie de passer en production ni d'engager de
   l'argent réel.
