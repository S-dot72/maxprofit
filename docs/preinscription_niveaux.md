# Pré-inscription : les niveaux et la continuation des vidéos (`/niveaux`, 25/11)

Demandé le 25/11, après la lecture des six vidéos du trader OTC :

> « Le 1 et 2 sont correct mais il faut déterminer les bon niveaux on a
> toujours eu ce problème »

Paramètres figés : `docs/preinscription_niveaux.json`, empreinte
`afbcb91e085ae1b00bf6f9fd2a4955b10afbecd680d43582312ed7457d674fa4`
(`etude_micro.empreinte`). Le protocole a été figé avant tout code. La
commande refuse de tourner si les paramètres embarqués ne donnent plus cette
empreinte.

## D'où viennent ces deux règles

Les vidéos montrent un trader qui joue des paires OTC sur PocketOption et se
dit rentable. Rien ne le prouve :
- ce sont des vidéos de vente d'un pack payant ;
- le tableau de suivi qu'elles montrent est une martingale de 50 $ à cinq
  niveaux. Rejouée le 25/11 sur nos issues réelles, elle ruine 41 à 44 % des
  campagnes, et elle exige, comme la mise fixe, 52,0 % de trades gagnants.

Trois idées en ont été tirées. L'utilisateur en a confirmé deux :
- **R1, l'entrée des vendeurs ou des acheteurs.** Le prix revient sur le
  niveau d'où les vendeurs (ou les acheteurs) ont lancé le dernier
  mouvement ; on joue dans leur sens.
- **R2, la continuation après un repli.** Une série de bougies d'une couleur,
  un court repli, puis la reprise : on joue la reprise.

La troisième, le retournement après une série, n'est pas testée.

Les niveaux clés en général sont déjà testés ailleurs, sans résultat :
ZoneH1, la zone inversée, la prise de liquidité, les chiffres ronds (F5 de
`/classiques`) et les graphiques à l'aveugle. Ce qui est nouveau ici : le
niveau est l'**ouverture de la bougie qui a lancé le mouvement**, et il est
défini de douze façons objectives, figées d'avance.

## R1 : les « bons niveaux », sans les choisir à l'œil

Le problème signalé, « déterminer les bons niveaux », ne se règle pas en en
choisissant un après coup : un niveau choisi en voyant le graphique gagne
toujours. Douze définitions sont donc jugées chacune seule, et les données
d'octobre choisissent, puis celles de novembre jugent.

| Élément | Règle |
|---|---|
| Unité | bougies M1, M5 ou M15 ; une M5 ou une M15 n'existe que si toutes ses minutes sont là |
| Pivot | sommet : plus haut strictement au-dessus des w bougies de chaque côté ; creux : plus bas strictement au-dessous ; w = 5 ou 10 |
| Niveau des vendeurs | ouverture de la première bougie rouge à partir du sommet (sommet compris), au plus tard à la bougie de confirmation |
| Niveau des acheteurs | ouverture de la première bougie verte à partir du creux |
| Impulsion | écart entre le niveau et l'extrême opposé atteint jusqu'à la confirmation, en étendues médianes des 50 bougies qui précèdent le pivot ; seuil 0 ou 3 |
| Activation | à la clôture de la bougie de confirmation (w bougies après le pivot), si elle clôture du bon côté du niveau |
| Déclenchement | premier tick qui touche le niveau dans les 24 h ; un niveau ne sert qu'une fois |
| Sens | niveau des vendeurs → PUT ; niveau des acheteurs → CALL |

Les définitions croisent 3 unités, 2 fenêtres et 2 seuils d'impulsion, soit
12. Par exemple, `R1_M5_w10_s3` désigne les niveaux M5 sur des pivots à 10
bougies, avec une impulsion d'au moins 3 étendues médianes.

## R2 : la continuation après un repli

Les bougies sont des M1 qui se suivent :
1. au moins 3 bougies de même couleur ;
2. puis 1 ou 2 bougies de la couleur contraire ;
3. puis une bougie qui clôture dans la couleur de la série.

La décision se prend à la clôture de cette dernière bougie, dans le sens de
la série. Une bougie sans corps interrompt le motif.

## Ce qui compte

- **Exécution aux ticks.** Pour R1, l'entrée se fait au dernier tick connu
  2 s après le toucher ; pour R2, 2 s après la décision. La sortie a lieu à
  l'échéance : 1, 2, 3 ou 5 minutes. Les deux ticks doivent dater de moins
  de 10 s, et chaque minute entre les deux doit avoir ses ticks.
- **Seuls les trades jouables comptent** : payout du flux ≥ 84, soit 92 %
  appliqués et un point mort de 52,08 %. Une égalité est une perte.
- **Épisodes.** Dans une définition, deux trades sur la même paire
  s'espacent d'au moins 5 minutes.
- **Le sens.** Chaque case (définition × échéance) choisit en exploration
  entre le sens de la règle et l'inverse, puis le garde en validation.
- **Mise fixe.** La martingale n'est pas une stratégie : elle ne change pas
  le taux de gain nécessaire.

## Précisions d'exécution, écrites avant le résultat

Le texte figé laisse quelques cas implicites ; le code les tranche ainsi,
toujours dans le sens prudent :
- **L'étendue médiane** se calcule sur 50 bougies qui se suivent. Sinon,
  l'impulsion est inconnue : le niveau compte pour le seuil 0, pas pour le
  seuil 3.
- **Le toucher.** Les minutes doivent se suivre de l'activation au
  toucher : un trou pourrait cacher le vrai premier toucher, donc le niveau
  est écarté (« trou avant le toucher »). Il est aussi écarté si la bougie
  touche le niveau mais qu'aucun tick de sa minute ne le fait, ou si cette
  minute n'a pas de ticks.
- **L'ordre.** Les touchers d'une même définition dans une même minute se
  rangent par l'heure de leur tick.
- **Les fins de données.** Un trade qui finirait après le dernier jour
  complet n'est pas jugé. Comme un trade non jouable, il occupe quand même
  ses 5 minutes, comme dans `/classiques`.
- **Une égalité** entre les deux sens en exploration garde le sens de la
  règle.

## Les trois temps

1. **Exploration**, du 06/10 au 02/11 : une case est retenue si son meilleur
   sens atteint 52,08 % sur au moins 300 trades jouables.
2. **Validation**, du 03/11 à la veille du lancement : chaque case retenue
   est rejugée dans son sens, sur au moins 100 trades. Le jugement utilise
   un bootstrap par blocs paire × jour (1 000 tirages, graine 7), puis
   Benjamini-Hochberg à 10 %.
3. **Confirmation** : une case validée est gelée, puis confirmée sur des
   jours postérieurs au lancement, avant tout essai en démo et sans argent
   réel.

## Limites connues, écrites avant le résultat

- **52 cases** (13 définitions × 4 échéances), chacune avec deux sens
  possibles. Si tout était à 50 %, environ 5 cases passeraient
  l'exploration par hasard ; le rapport donne ce nombre exact.
  Benjamini-Hochberg et la confirmation sur des jours futurs sont là pour
  cela.
- **Les définitions se recouvrent.** Les niveaux du seuil 3 font partie de
  ceux du seuil 0, et un même toucher peut servir à plusieurs définitions.
  Leurs résultats ne sont pas indépendants.
- **Les définitions M15 donnent peu de trades.** Certaines de leurs cases
  n'atteindront pas les 300 trades de l'exploration et ne seront pas
  jugées.
- **Une seule lecture des vidéos.** D'autres lectures du « niveau des
  vendeurs » existent : une zone plutôt qu'un prix, plusieurs touchers. Elles
  ne sont pas testées, et aucune ne s'ajoute après le résultat.
- **Les jours de validation ont déjà servi** à d'autres études (`/terrain`,
  l'Engulfing, `/classiques`). Ils restent hors échantillon pour CES règles,
  mais à force d'études sur les mêmes jours, une case peut passer par
  hasard. La confirmation sur des jours futurs est donc obligatoire.
- **Trois semaines de validation** détectent un vrai écart de l'ordre de 2 à
  3 points. Un avantage plus fin passerait inaperçu.
- **Rien n'est ventilé par heure**, pour que la confirmation de la piste
  EURJPY 22 h reste aveugle.
- **C'est une étude, pas un changement** : la course, la collecte, ZoneH1,
  les échéances et les configurations restent telles quelles.

## Verdicts possibles

- **Piste** : une case validée. Elle est gelée, puis confirmée sur des jours
  futurs.
- **Rien** : aucune case validée. Ni ces définitions des niveaux, ni la
  continuation après repli, ne battent le point mort sur l'OTC.

## Lancer l'étude

Code : `maxprofit/live/niveaux.py`. Il reprend la lecture des bases, le
décodage des ticks et le bootstrap des études Engulfing et `/classiques`.

Tests : `tests/test_niveaux.py`. Ils vérifient :
- les niveaux sur des graphiques faits à la main ;
- le regroupement M5 et M15, et l'absence de lecture du futur ;
- la continuation, les issues aux ticks et les verdicts ;
- les trades de chaque définition R1, contre une recherche brute du premier
  tick qui touche chaque niveau ;
- la lecture seule.

Commandes :
- **`/niveaux essai`** : à lancer d'abord. C'est le contrôle technique sur
  une paire et deux jours. Il passe par toute la chaîne mais ne montre aucun
  taux. Il donne la durée et la mémoire prévues pour la passe complète.
- **`/niveaux`** : la passe complète, à lancer une seule fois. Elle prend 10
  à 40 minutes ; un point d'avancement arrive après 2 minutes, puis toutes
  les 10 minutes. Le premier lancement complet fait foi.
- **`/niveaux active`** : tout sur la base active, si la relève ne répond
  pas.

La passe rend un rapport et cinq fichiers :
- `niveaux_cases.csv` : une ligne par définition × échéance × période, avec
  le sens choisi, le taux, l'IC et le verdict ;
- `niveaux_details.csv` : les cases retenues, par paire et par sens ;
- `niveaux_statuts.csv` : niveaux formés, touchés et écartés, épisodes,
  statuts des issues ;
- `niveaux_donnees.csv` : les données lues, par paire et par jour ;
- `niveaux_signaux.csv.gz` : chaque trade retenu, ses issues et ses statuts.

## Résultats du 26/11

Rapport `/niveaux`, version 8131cc8 : 20 paires, du 06/10 au 25/11.
L'exploration couvre le 06/10 au 02/11, la validation le 03/11 au 25/11
(23 jours). La passe a pris 24,0 minutes sur le serveur, avec un pic de
mémoire de 193 Mo. Elle a retenu 341 222 trades, dont 153 103 jouables.

Empreintes des fichiers rendus (sha256, 16 premiers caractères, `.gz`
décompressé) :

| Fichier | Empreinte |
|---|---|
| `niveaux_cases.csv` | `a43ef55ff3454f38` |
| `niveaux_details.csv` | `a027d140debeb097` |
| `niveaux_statuts.csv` | `788b77acd0cc684f` |
| `niveaux_donnees.csv` | `b18ff145ceb5325f` |
| `niveaux_signaux.csv.gz` | `314619cddb7b66a8` |

Les 104 lignes des cases se recalculent à l'identique depuis le fichier des
trades. Aucun épisode ne fait moins de 5 minutes.

- **Aucune case validée sur les 52.** Deux cases ont été retenues en
  exploration, alors que le hasard seul en donnerait environ 3,9. Les deux
  sont « non reproduites », et ce sont deux échéances de la même
  définition, R1 M5 pivot 5 impulsion ≥ 3 :
  - à 2 minutes, 52,5 % sur 758 décisions, puis 49,7 % sur 992 en
    validation (p = 0,95) ; 6 jours de validation sur 23 passent le point
    mort ;
  - à 5 minutes, 53,7 % sur 756, puis 50,6 % sur 992 (p = 0,83) ; 11 jours
    sur 23 ;
  - les paires changent d'une période à l'autre : à 2 minutes, NZDUSD passe
    de 68 % à 44 % et EURJPY de 47 % à 64 %.
- **Les niveaux sont bien retouchés, puis le prix part au hasard.** Dans
  les 24 h, 88 à 93 % des niveaux M1 sont retouchés, et 58 à 86 % des
  niveaux M5 et M15. Après le toucher, toutes définitions R1 confondues (un
  même toucher compte dans plusieurs définitions) :

  | | Décisions | Sens de la règle | Sens inverse | Égalités |
  |---|---|---|---|---|
  | Exploration | 200 428 | 49,5 % | 49,8 % | 0,6 % |
  | Validation | 230 515 | 49,6 % | 49,8 % | 0,6 % |

  En validation, la meilleure case sur au moins 300 décisions plafonne à
  51,95 % : M15 pivot 10, à 2 minutes, sens inverse, qui n'avait que 50,9 %
  en exploration.
- **La continuation après repli (R2) reste à 50 %** sur 100 490 trades :
  dans le sens de la règle, 49,5 % en exploration et 49,4 % en validation ;
  l'inverse fait 50,0 %.
- **Les petits échantillons trompent.** R1 M15 pivot 5 impulsion ≥ 3 gagnait
  56,5 à 57,2 % sur 138 décisions en exploration, trop peu pour être jugée,
  puis 46,2 à 48,4 % sur 186 en validation. C'est l'effet des exemples
  choisis que montrent les vidéos.
- **55 % des trades retenus n'étaient pas jouables** : le payout du flux
  était alors sous 84. Ils ne comptent pas dans les taux.

**Verdict.** Ni les douze définitions des niveaux des vendeurs et des
acheteurs, ni la continuation après repli, ne battent le point mort sur
l'OTC. Aucune case n'est gelée : il n'y a rien à confirmer.

**Note sur les données.** La relève n'avait plus aucune donnée après le
23/11 vers 05 h 43 UTC. Comme le prévoit le protocole, les jours concernés
ont été lus sur l'active : ce sont pour l'essentiel les 709 résumés
divergents du rapport. Les trades de ces jours sont bien dans les fichiers.
Seuls les comptes de minutes du rapport et `niveaux_donnees.csv`, tirés du
résumé de la relève, les omettent.
