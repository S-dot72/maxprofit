# Pré-inscription : le terrain OTC est-il prévisible ? (`/terrain`, 08/11)

Demandé le 08/11 :

> « Si ZoneH1 n'est pas rentable tu dois trouver autre chose. »

Paramètres figés : `docs/preinscription_terrain.json`, empreinte
`60e6ddaf41de7d3fe7409ca753cab7f10c956861658ae64278c6fbdb47138f79`
(`etude_micro.empreinte`). La commande refuse de tourner si les paramètres
embarqués ne donnent plus cette empreinte.

## Pourquoi commencer par le terrain

Toutes les pistes tirées de ZoneH1 ont échoué. Avant d'inventer une
nouvelle stratégie, il faut savoir si le prix M1 des paires OTC se laisse
prévoir tout court. Ce prix est fabriqué par le broker :
- s'il est imprévisible à 1, 5 et 15 minutes, aucune règle ne battra le
  point mort, et il faudra changer de terrain (paires non OTC, autre
  instrument) ;
- s'il a une faille, elle doit apparaître dans au moins une famille de
  règles simples, sans ZoneH1.

## Les cinq familles

| Famille | Idée | Ce qu'une faille voudrait dire |
|---|---|---|
| F1 suite | le mouvement des 1, 5 ou 15 dernières minutes continue, ou revient | le générateur a de la mémoire à court terme |
| F2 excès | après un mouvement de plus de 3 fois l'habituel, le prix continue ou revient | il corrige ses propres excès |
| F3 meneur | une paire qui partage une devise annonce le mouvement d'une autre | les paires sont tirées d'une base commune, avec un décalage |
| F4 triangle | EURJPY s'écarte de EURUSD × USDJPY, puis revient | les crosses sont recalculées à partir des autres paires |
| F5 heure | une paire monte ou baisse plus souvent à certaines heures | le générateur a une dérive horaire |

Chaque règle est jugée aux échéances de 1, 5 et 15 minutes, à la clôture
de chaque bougie M1. Il n'y a pas de filtre de zone, ni de choix à la main.

## Les trois périodes

1. **Exploration**, du 06/10 au 02/11 inclus. Chaque case choisit son sens
   (suite ou retour, hausse ou baisse). Elle n'est retenue que si elle
   atteint le point mort avec au moins 500 décisions jouables.
2. **Validation**, du 03/11 au 09/11 inclus (jusqu'à la veille du
   lancement si celui-ci a lieu avant le 10/11). Chaque case retenue y est
   rejugée dans le même sens, contre 52,08 %, avec une correction de
   Benjamini-Hochberg à 10 % sur l'ensemble des cases jugées.
3. **Confirmation**, à partir du 10/11 : ces bougies ne sont jamais lues
   par `/terrain`. Une case découverte est d'abord gelée, puis confirmée
   sur elles.

## Ce qui compte

- **Seules les décisions jouables comptent** : celles où le payout du flux
  vaut au moins 84, soit 92 % appliqués et un point mort de 52,08 %.
- **Une égalité est une perte.** Le taux avec égalités exclues est publié
  à part.
- **Les sauts aberrants sont écartés** : une variation de plus de 1 % en
  une minute est un trou de données, pas un mouvement jouable. Les
  graphiques des lots 1 et 2 en montraient.
- **L'incertitude** vient d'un bootstrap par blocs paire × jour.

## Limites connues, écrites avant le résultat

- L'entrée est la clôture de la bougie, comme dans le proxy de ZoneH1 que
  `/rapprochement` a validé (écart médian nul). À 1 minute pourtant,
  quelques secondes de retard pèsent. Une piste à 1 minute devrait être
  vérifiée sur les ticks avant toute autre chose.
- Une semaine de validation détecte un vrai écart de l'ordre de 2 points.
  Un avantage plus fin passerait inaperçu.
- C'est une étude, pas un changement : la course, la collecte, la
  stratégie et les échéances restent telles quelles.

## Verdicts possibles

- **Piste** : une case découverte en validation. Elle est gelée, puis
  confirmée sur les bougies à partir du 10/11.
- **Terrain muet** : aucune case découverte. Aucune de ces règles ne bat
  le point mort sur l'OTC. C'est un argument fort pour changer de terrain
  plutôt que de stratégie.
