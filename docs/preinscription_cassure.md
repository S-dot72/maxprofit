# Pré-inscription : cassure ou retournement au toucher d'une zone (`/cassure`, 26/11)

Demandé le 26/11, après les résultats de `/niveaux` :

> « On a des zones c'est bien ca ? Il suffit qu'on arrive à déterminer ce qui
> fait que le prix casse la zone ou fait un retournement »

puis :

> « Tu dois trouver la solution analyse tout »

Paramètres figés : `docs/preinscription_cassure.json`, empreinte
`14108d3b4e50ed694861771e54e18ed74e491fcd91fd7205ee68486bced1c875`
(`etude_micro.empreinte`). Le protocole a été figé et poussé avant le code
(commit 84dcac0). La commande refuse de tourner si les paramètres embarqués
ne donnent plus cette empreinte.

## La question

`/niveaux` l'a montré : quand le prix touche une zone, il se retourne une
fois sur deux. En validation, sur 230 515 décisions, le retournement fait
49,6 % et la cassure 49,8 %. Pour gagner, il faudrait donc un signe, connu
**avant** le toucher, qui sépare les zones qui vont casser de celles qui vont
tenir. Il peut exister même si la moyenne est à 50 % : des cassures d'un côté
et des retournements de l'autre peuvent s'annuler.

Les signes déjà essayés n'ont rien séparé :
- la taille de la zone (M1, M5, M15) et la force de son départ (`/niveaux`) ;
- la tendance de l'heure, la longue mèche et le marché comprimé
  (`/classiques`) ;
- la zone cassée qui change de camp et la fausse cassure suivie d'un retour.

## Les zones et les trades

Ce sont exactement ceux de `/niveaux` (gel afbcb91e) pour trois définitions :
M1, M5 et M15, pivot à 5 bougies, sans seuil d'impulsion. La formation, le
premier toucher au tick, les épisodes de 5 minutes et l'exécution sont les
mêmes : entrée 2 s après le toucher, échéances de 1, 2, 3 et 5 minutes, flux
≥ 84, une égalité est une perte. Un test le vérifie trade par trade.

- **Retournement** : à l'échéance, le prix est revenu du côté d'où il venait.
  C'est le gain du sens de la règle : PUT sur une zone de vendeurs, CALL sur
  une zone d'acheteurs.
- **Cassure** : il est passé de l'autre côté ; c'est le gain du sens inverse.

## Les dix signes

Tous se mesurent sur les bougies M1 closes **avant** la minute du toucher.
L'échelle m est l'étendue médiane des 50 dernières bougies, et d vaut +1
quand le prix monte vers la zone, −1 quand il descend vers elle.

| Signe | Ce qu'il mesure | L'idée reçue |
|---|---|---|
| Vitesse d'arrivée | le chemin fait vers la zone en 5 minutes, en m | arrivée rapide → cassure |
| Élan de l'heure | le même chemin, sur 60 minutes | — |
| Approches manquées | le nombre de bougies passées à moins de 0,5 m de la zone sans la toucher | une zone testée s'use et casse |
| Âge de la zone | les minutes depuis son activation | une zone fraîche tient |
| Force de départ | l'écart qui a créé la zone, en m | — |
| Éloignement avant le retour | jusqu'où le prix s'est éloigné avant de revenir | — |
| Compression | l'étendue médiane des 30 dernières bougies, rapportée à celle des 240 dernières | un marché calme casse |
| Dernière bougie | son corps, dans le sens de l'arrivée | un gros corps pousse et casse |
| Confluence | les autres zones du même camp, actives, à moins de 0,5 m | des zones empilées tiennent |
| Position dans le jour | la place de la zone entre le plus bas et le plus haut des 24 h | une zone au plus haut du jour tient |

Un toucher n'est mesuré que si :
- les 240 minutes qui le précèdent se suivent ;
- les 24 h précédentes comptent au moins 720 bougies ;
- les étendues utiles sont non nulles.

Sinon, il est écarté et compté.

## Deux façons de juger

1. **Chaque signe seul, par tiers.** Les bornes des tiers viennent de
   l'exploration. Chaque tiers choisit en exploration entre retournement et
   cassure, puis garde ce choix. Cela fait 10 signes × 3 tiers × 4
   échéances = 120 cases. Une case est retenue si son sens atteint 52,08 %
   sur au moins 300 décisions en exploration.
2. **Tous ensemble, dans un score.** Une régression logistique par échéance
   est ajustée sur l'exploration (`etude_micro.Logistique`, pénalité L2
   λ = 1). Elle donne la probabilité du retournement, et le score joue le
   sens qu'elle prédit, seulement sur le tiers de touchers où il est le plus
   confiant (seuil fixé en exploration). Il existe en deux versions :
   - **score simple** : les dix signes, l'unité et le camp, soit 13
     variables ;
   - **score croisé** : les mêmes, plus les 45 produits deux à deux des
     signes, pour saisir des combinaisons comme « arrivée rapide ET marché
     comprimé ».

   Les 8 cases de score (2 scores × 4 échéances) se jugent directement en
   validation.

## Les trois temps

1. **Exploration**, du 06/10 au 02/11 : bornes des tiers, sens des cases,
   préparation des variables, ajustement des scores et de leurs seuils.
2. **Validation**, du 03/11 à la veille du lancement : chaque case retenue
   et chaque case de score est jugée contre 52,08 %, sur au moins 100
   décisions. Le jugement utilise un bootstrap par blocs paire × jour
   (1 000 tirages, graine 7), puis Benjamini-Hochberg à 10 % sur toutes les
   cases jugées.
3. **Confirmation** : une case validée est gelée, puis confirmée sur des
   jours postérieurs au lancement, avant tout essai en démo, sans argent
   réel.

## Limites connues, écrites avant le résultat

- **Les jours de validation sont ceux de `/niveaux`**, pour les mêmes trades.
  `/niveaux` n'a jamais regardé ces signes, mais ces jours ont beaucoup
  servi. La confirmation sur des jours futurs reste donc obligatoire.
- **128 cases en tout.** Avec plusieurs milliers de décisions par tiers, la
  barre de l'exploration (52,08 %) est très stricte ; le rapport donne
  combien de cases passeraient par hasard.
- **Les zones M1 dominent** : environ 80 % des touchers. Les scores
  apprennent surtout leur comportement ; l'unité n'entre que comme un
  décalage.
- **Les touchers se recouvrent** : une zone M1 et une zone M5 peuvent être
  touchées au même instant. Le bootstrap par blocs paire × jour en tient
  compte.
- **Les scores ne voient que deux formes de relation** : chaque signe seul
  (simple) et les paires de signes (croisé). Des formes plus complexes, à
  seuils ou à trois signes, leur échappent. C'est un choix : un modèle plus
  souple aurait plus de chances de trouver un faux signal, et l'étude ne se
  joue qu'une fois.
- **Les signes sont lus sur les bougies M1**, pas sur les ticks, et rien
  n'est ventilé par heure, pour que la confirmation EURJPY 22 h reste
  aveugle.
- **C'est une étude, pas un changement** : la course, la collecte, ZoneH1,
  les échéances et les configurations restent telles quelles.

## Verdicts possibles

- **Piste** : une case validée, signe seul ou score. Elle est gelée, puis
  confirmée sur des jours futurs.
- **Rien** : aucune case validée. Aucun de ces dix signes, seul ou combiné,
  ne permet de prévoir une cassure ou un retournement mieux que le point mort
  sur l'OTC.

## Lancer l'étude

Code : `maxprofit/live/cassure.py`. Il reprend les zones, les touchers et
l'exécution de `/niveaux`, ainsi que la lecture des bases et le bootstrap
des études précédentes.

Tests : `tests/test_cassure.py`. Ils vérifient :
- les dix signes sur un graphique fait à la main, et sur son miroir ;
- la fenêtre exigée et l'absence de lecture du futur ;
- les touchers, identiques aux trades de `/niveaux` ;
- la confluence, contre un comptage brut ;
- la préparation des variables ;
- un score qui retrouve un effet planté ;
- les verdicts et la lecture seule.

Quatorze erreurs injectées volontairement dans le code sont toutes
détectées, dont une lecture du futur.

Commandes :
- **`/cassure essai`** : à lancer d'abord. C'est le contrôle technique sur
  une paire et deux jours ; il ne montre aucun taux et donne la durée et la
  mémoire prévues.
- **`/cassure`** : la passe complète, à lancer une seule fois. Elle prend 15
  à 45 minutes ; un point d'avancement arrive après 2 minutes, puis toutes
  les 10 minutes. Le premier lancement complet fait foi.
- **`/cassure active`** : tout sur la base active, si la relève ne répond
  pas.

La passe rend un rapport et six fichiers :
- `cassure_cases.csv` : une ligne par case et par période, avec le taux,
  l'IC et le verdict ;
- `cassure_details.csv` : les cases retenues, par paire et par camp ; les
  scores, par paire et par sens prédit ;
- `cassure_scores.csv` : les bornes des tiers, la préparation des
  variables, et les coefficients et seuils des scores ;
- `cassure_statuts.csv` : zones formées et touchées, épisodes, mesure,
  statuts des issues ;
- `cassure_donnees.csv` : les données lues sur la relève, par paire et par
  jour ;
- `cassure_touchers.csv.gz` : chaque toucher retenu, avec ses dix signes et
  ses issues.
