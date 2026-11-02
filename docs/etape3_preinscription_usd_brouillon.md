# Brouillon — pré-inscription de la piste « paires USD » (NON GELÉ, EXPLORATOIRE)

> **Statut, décision du 02/11 : NON GELÉ.** La piste USD n'est pas retenue
> comme expérience principale ; elle reste une hypothèse exploratoire. La
> variante recommandée (§6) a été choisie : rapprocher d'abord le proxy des
> ordres réellement exécutés et réglés (`/rapprochement`), toutes paires,
> sans rien changer à la course. Aucune nouvelle hypothèse avant ce
> rapport. Ce brouillon est gardé pour mémoire.

Ce brouillon fixait tout à l'avance. Il n'aurait été gelé (empreinte,
commit) que sur accord explicite, sans consulter aucune donnée de la
période de test.

Erratum : la première version prenait le payout du flux pour le payout
appliqué. La population jouable est **flux ≥ 84** (payout appliqué de 92 %),
pas « payout ≥ 92 % » ; les chiffres ci-dessous sont corrigés
(`docs/etape3_audit_gel2.md`).

## 1. Hypothèse

**H_USD** : sur les signaux ZoneH1 jouables des 7 paires USD, le rendement
par mise au payout observé est positif.

Au payout de 92 %, cela revient à un taux de gain supérieur à 52,08 %.

## 2. Population, fixée à l'avance

- **Signaux** : ZoneH1 du rejeu quotidien, avec la stratégie et les
  paramètres de production inchangés.
- **Paires** : AUDUSD, EURUSD, GBPUSD, NZDUSD, USDCAD, USDCHF et USDJPY (OTC),
  les 7 paires collectées qui contiennent USD. Aucune n'est ajoutée ni
  retirée en cours de test.
- **Jouable** : payout du flux relevé avant la décision ≥ 84, soit un payout
  appliqué de 92 % (`au_plafond`, le filtre de la course). Si la règle du
  broker change, le test s'arrête et le dernier regard devient final.
- **Issue** : le proxy du protocole. La bougie du signal est comparée à la
  bougie complète qui commence 900 s plus tard. Une égalité est exclue et
  comptée ; une issue manquante est exclue.
- **Période** : décisions à partir du premier minuit UTC qui suit le gel.

## 3. Critère, incertitude, regards

- **Statistique** : le rendement moyen par mise au payout appliqué.
- **Incertitude** : intervalle percentile par bootstrap de blocs paire ×
  jour, 1 000 tirages, graine 7. La p-valeur unilatérale est
  P(rendement ≤ 0).
- **Regards** à 500, 1 000, 2 000 et 3 200 signaux, avec dépense d'α de type
  O'Brien-Fleming (α famille = 5 %). Les seuils nominaux sont ceux de
  `etude_micro.seuils_des_regards` pour ces regards.
- **Verdicts** :
  - **confirmée** si le seuil du regard est atteint et que le rendement est
    positif dans au moins 2 des 3 tiers chronologiques ;
  - **rejetée** si la borne haute de l'IC est ≤ 0 ;
  - **indéterminée** sinon, et définitivement à 3 200 signaux.
- **Secondaires, sans verdict** :
  - l'écart entre USD et les autres paires jouables ;
  - la permutation sur les 77 520 groupes de 7 paires ;
  - le rendement des ordres RÉELLEMENT passés par la course sur ces paires,
    réglé par le broker.

## 4. Puissance (justification du plafond de 3 200 signaux)

Paramètres mesurés par l'audit sur les paires USD jouables :
- environ 26 signaux par jour ;
- corrélation intra-bloc de 0,144, soit un effet de plan de 1,83.

Test unilatéral à 5 %, puissance de 80 % :

| Taux réel supposé | Signaux | Durée |
|---|---|---|
| 58 % (proche de l'observé, gonflé par la sélection) | 788 | ≈ 31 jours |
| 57 % | 1 148 | ≈ 44 jours |
| 56 % | 1 818 | ≈ 70 jours |
| 55 % | 3 292 | ≈ 128 jours |

Le plafond de 3 200 signaux couvre presque un effet réel de 55 %. En dessous, le
test ne peut pas conclure dans un délai raisonnable, et il le dira.

## 5. Interdits

- Aucune modification de la course, des paires, du seuil de payout, de
  l'issue ni des regards après le gel.
- Aucun résultat de la période de test n'est consulté avant un regard.

## 6. Variante recommandée par l'audit

L'audit montre trois choses :
- la piste USD est née après coup, et ne se distingue pas en exploration
  seule (permutation, p = 0,16 sur les jouables) ;
- **toute** la population jouable (flux ≥ 84, filtre de la course antérieur
  à l'analyse) gagne 54,0 % au proxy, contre 51,2 % sous le plafond ;
- cet écart repose sur un PROXY, alors que la course entre après la
  confirmation M1, environ 2 s plus tard, au prix du broker.

**Variante** : le critère principal devient « ZoneH1 jouable, toutes paires,
rendement positif ». Il porte d'abord sur le **rapprochement avec les ordres réels** : même
signal, issue du proxy face au règlement du broker, avec le taux d'accord
publié. La piste USD reste en analyse secondaire.

Cette variante teste ce que la course fait déjà, sans rien y changer.
