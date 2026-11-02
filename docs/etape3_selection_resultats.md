# Étape 3 — Résultats de la passe de sélection (01/11) et analyse des gains

La passe `/micro_selection` a tourné le 01/11, en 83,5 minutes. Elle a lu
299 paires-jours, dont 265 sur la relève et 34 sur l'active pour les jours
dont la copie divergeait. Toutes les lectures étaient en lecture seule.
- Les **fichiers d'origine** sont rendus par Telegram et ne sont pas
  versionnés, à l'exception du gel 2.
- Le **second gel** est commité : `docs/etape3_parametres_gel2.json`,
  empreinte `b64a289e…a0a5` (`docs/etape3_registre.md` §5).
- La **confirmation** de H1 et H2 s'ouvre le 03/11 à 00:00 UTC.

## 1. Données et contrôles

**Population principale** (niveau 2 et fenêtre VALID, issue décisive) :
- exploration : 991 signaux ;
- validation : 742 signaux.

**Fenêtres** : 96 % VALID, 3 % PARTIAL, 1 % INVALID.

**Anti-fuite** : 7 224 contrôles sur 7 224 sans effet.

**Panne de collecte** (`/historique_ticks`, `docs/collecte_diagnostic.md`) :
- elle commence le 27/10 vers 11:00 UTC pour 17 paires, dans la validation ;
- une paire muette n'a pas de bougies, donc pas de signal : ces heures
  n'apportent aucun signal à la passe ;
- les fenêtres partiellement touchées sont classées PARTIAL ou INVALID, donc
  hors de la population principale ;
- les fenêtres utilisées ont des ticks complets.

## 2. Ce que le protocole pré-inscrit a produit

**Règle de retrait** : 92 candidates conservées, 0 retirée.

**Hypothèses principales**, coefficients figés pour la confirmation :

| Hypothèse | Variable | Coefficient | Sens |
|---|---|---|---|
| H1 | `A7_penetration@300` | −0,056 | dans le sens pré-inscrit |
| H2 | `D1_traversees@300` | +0,009 | opposé au sens pré-inscrit |

H2 ne peut donc être confirmée que si son effet pratique, mesuré dans le sens
pré-inscrit, atteint le seuil.

**Criblage exploratoire** : 270 tests, chacun ajouté au modèle de référence,
ajusté sur l'exploration et jugé sur la validation.
- **Aucune découverte** au seuil BH de 10 %.
- La meilleure p vaut 0,056.
- Les gains Δℓ sont de l'ordre du millième de nat.

## 3. Où gagne-t-on ? Filtres simples, choisis puis jugés

Script hors dépôt, sur `selection_signaux.csv` :
- terciles des 92 variables, paires, créneaux de 4 h et sens, soit 286
  filtres ;
- les 30 meilleurs sur l'exploration sont jugés sur la validation ;
- les 10 meilleurs simples sont aussi combinés deux à deux.

**Référence ZoneH1** : 53,3 % en exploration, 51,6 % en validation, et
52,6 % sur l'ensemble. Le point mort est à 52,1 % (payout de 92 %).

**Les filtres les plus favorables en exploration s'effondrent en
validation** : c'est un retour vers la moyenne.

| Filtre | Exploration | Validation |
|---|---|---|
| EURUSD | 63 % | 49 % |
| créneau 20-24 h | 60 % | 45 % |
| `B6_concentration@180` en tercile du milieu | 60 % | 47 % |

Les meilleures combinaisons tombent de 70 % en exploration à environ 35 % en
validation.

**Après correction pour 30 tests, aucun filtre ne bat le point mort en
validation.**

**La seule famille qui garde son effet, l'amplitude des retournements**
(`C4_amplitude_retournement`, tercile bas, 57 % puis 54 à 56 %), **est un
effet de paire déguisé.** Son tercile bas contient presque tous les signaux
de USDCAD, EURUSD, USDJPY et AUDUSD. Le modèle, qui tient compte de la paire,
n'y trouve d'ailleurs aucun gain.

## 4. Une piste, née après coup

| Paires | Exploration | Validation |
|---|---|---|
| contenant USD | 54,8 % (442 signaux, IC 48–62 %) | 56,3 % (371 signaux, IC 48–65 %) |
| les autres | 52,1 % (549 signaux) | 46,9 % (371 signaux) |

Ce n'est **pas une preuve** :
- la piste a été trouvée en regardant les résultats ;
- aucune des deux périodes ne bat seule le point mort de façon
  significative (P ≈ 0,2) ;
- les taux par paire changent beaucoup d'une période à l'autre, par exemple
  AUDCAD passe de 63 % à 27 %.

Pour en faire une règle, il faudrait la pré-inscrire, puis la juger sur des
données nouvelles. La collecte des 20 paires est rétablie depuis le 31/10.
Distinguer environ 55,5 % de 52,1 % demande de l'ordre de 1 300 signaux,
soit environ un mois au rythme des seules paires en USD.

## 5. Conclusion

Sur les signaux ZoneH1 d'octobre, **les 92 variables de microstructure
n'apportent pas d'information exploitable** au-delà de la référence : ni en
modèle, ni en filtres simples, ni en combinaisons.

ZoneH1 seule est au point mort. Une absence de résultat significatif ne
démontre pas l'absence d'un effet modeste, mais aucun effet de l'ordre de
5 points n'apparaît.

Les étapes suivantes possibles :
- la confirmation de H1 et H2, pré-inscrite ;
- la pré-inscription éventuelle de la piste « paires en USD » ;
- d'autres familles de signaux que ZoneH1.

## 6. Corrections apportées par l'audit du 02/11

Voir `docs/etape3_audit_gel2.md` (avec son erratum du même jour). Le §4
ci-dessus est corrigé sur trois points.
- **Rendement** : le « +6 à +8 % » supposait un payout de 92 % pour tous les
  signaux. La table `payouts` donne le payout du FLUX (médiane 82 %) ; le
  broker applique min(flux + 8, 92). Au payout appliqué, les paires USD
  rapportent +0,5 % par mise sur l'ensemble des signaux, et +10,0 % sur les
  signaux jouables (flux ≥ 84 ; IC −1,5 à +20,9 %).
- **Piste USD** : jouable, le groupe USD se distingue d'un groupe quelconque
  de 7 paires sur les deux périodes (permutation, p = 0,028), mais pas en
  exploration seule (p = 0,16), et la piste a été trouvée après coup.
- **Nombre de signaux** : les « 1 300 signaux » ignoraient la dépendance
  entre signaux d'une même paire et d'un même jour (effet de plan de 1,8 à
  2,5) et prenaient l'effet observé pour vrai. Un test honnête demande de
  800 à 3 300 signaux selon l'effet réel supposé, soit de 1 à 4 mois.

Les données, les frontières et les paramètres du gel 2 sont confirmés
identiques par un recalcul indépendant.
