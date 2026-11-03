# Pré-inscription H_IB — les bougies incluses (GELÉE le 03/11)

**Empreinte SHA-256** :
`ccc23575fbe3ce090a2d09a5409440545b7c2fccb89c5c2851bc29dec3d36b1e`

Elle est calculée sur `docs/preinscription_incluses.json`, sérialisé en
JSON à clés triées, par `etude_micro.empreinte`. Le gel est commité le
03/11 vers 10:40 UTC. **La confirmation s'ouvre le 04/11 à 00:00 UTC.**

Décision du 03/11 : « Fais ce qu'il faut pour trouver une bonne piste. »

## D'où vient la piste

`/bougies` du 03/11 a examiné les 50 bougies M1 avant chaque signal
ZoneH1. Il en ressort une seule découverte, sur 64 mesures distinctes,
après correction BH à 10 % :

> **moins il y a de bougies incluses avant le signal, plus le signal
> gagne.**

Une bougie incluse a son plus haut et son plus bas à l'intérieur de la
précédente : c'est un marché qui se comprime et hésite.

| | Exploration (10 au 21/10) | Validation (21 au 28/10) |
|---|---|---|
| moins de 5 incluses sur 50 | 58,2 % | 60,5 % |
| 6 ou plus | 51,1 % | 45,0 % |

Contrôles faits avant le gel :
- l'effet tient à l'intérieur des paires (17 sur 20, écart moyen de
  +11 points) ;
- il tient dans les trois régimes de tendance, et sur les signaux
  jouables (61,6 % contre 49,1 %) ;
- il n'est corrélé à aucune autre mesure : doji, toupies, tailles,
  rapport de force.

Ce n'est **pas une règle**. La piste est née des données et a été choisie
parmi 64 mesures : son effet réel est probablement plus petit.

## Ce qui est figé

| | |
|---|---|
| Signaux | ZoneH1, stratégie de production, recalculés sur les bougies de la période |
| Entrée | immédiate, à la clôture de la bougie du signal. C'est le mode 0 actuel de la course, validé par `/rapprochement` : même issue que le broker pour 81 ordres sur 86 |
| Mesure | bougies incluses parmi les 50 bougies M1 complètes et contiguës qui finissent à la bougie du signal |
| Groupes | **favorable** : moins de 5 incluses ; **défavorable** : 6 ou plus ; exactement 5 : exclu |
| Issue | clôture de la bougie qui finit 900 s après la décision, face à la clôture de la bougie du signal ; égalités et sorties manquantes exclues et comptées |
| Jouable | payout du flux ≥ 84, dernier relevé avant la décision (payout appliqué de 92 %) |

**Critères**, jugés ensemble par Holm (alpha de famille 5 %, unilatéral) :
- **C1** : le groupe favorable gagne plus que le groupe défavorable,
  toutes paires et tous payouts ;
- **C2** : le groupe favorable ET jouable gagne plus que le point mort de
  52,08 %. C'est le critère qui compte pour l'argent.

**Incertitude** : bootstrap par blocs paire × jour (1 000 tirages, graine
7). La p unilatérale vient de l'approximation normale de l'écart-type
bootstrap.

## Deux épreuves

1. **Réplication immédiate sur la quarantaine** (28/10 au 03/11). Ces
   données sont déjà collectées mais n'ont jamais été examinées sous cet
   angle. Un seul calcul. Il est rapporté, sans pouvoir de verdict. La
   collecte était dégradée du 27/10 au 31/10 : une fenêtre trouée est
   écartée et comptée.
2. **Confirmation prospective**, à partir du 04/11 à 00:00 UTC. Les
   regards ont lieu à 300, 600 et 900 signaux favorables et jouables, avec
   des seuils O'Brien-Fleming. Aucun résultat n'est montré avant le premier
   regard.
   - **confirmée** : C1 et C2 passent au seuil du regard ;
   - **abandonnée** : à un regard, la borne haute de l'IC de C2 passe sous
     52,08 %, ou celle de C1 sous 0 ;
   - **indéterminée** : au 900e signal, ni l'un ni l'autre.

**Calendrier**, au rythme d'octobre (environ 14,6 signaux favorables et
jouables par jour) :

| Regard | Date estimée |
|---|---|
| 1 | vers le 25/11 |
| 2 | vers le 15/12 |
| 3 | début janvier |

**Puissance** (effet de plan de 1,6, puissance de 80 %) : un taux réel de
60 % se montre en 379 signaux, 58 % en 689, 57 % en 1 003.

## Interdits

- Aucune modification des seuils, de la population, de l'issue ni des
  regards après le gel.
- Aucun résultat de la confirmation consulté hors des regards.
- Aucune règle de la course modifiée par cette pré-inscription. La course
  continue telle quelle. Passer en production, ou engager de l'argent
  réel, est une autre décision.
