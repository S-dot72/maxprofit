# Pré-inscription : les échelles de l'imprévisibilité OTC (`/echelles`, 27/11)

Demandé le 27/11 :

> « Effectue des test et motifs approprié au marché otc et à son
> imprévisibilité »

Paramètres figés : `docs/preinscription_echelles.json`, empreinte
`b25ad6eb243d4c2b70ccc4ea38ea37c192c249a5712aed7b3e133cddcc8c17ac`
(`etude_micro.empreinte`). Le protocole a été figé et poussé avant le code
(commit 713753a). La commande refuse de tourner si les paramètres embarqués
ne donnent plus cette empreinte.

Cette version remplace le gel `b11bb9e3` (commit 637d98a), retiré avant tout
code et tout résultat. Dans ce gel, le grand mouvement était mesuré en prix
brut : les paires en yen, cent fois plus chères, auraient rempli le décile à
elles seules. Il est désormais mesuré en variation relative, |d| / prix.

## La question

Les études précédentes ont jugé des motifs de 1 à 5 minutes. Celle-ci
balaie **toutes les échéances que l'OTC propose, de 5 secondes à 1 heure**,
aux ticks. La question est la plus simple qui soit, posée à chaque échelle :
le dernier mouvement du prix annonce-t-il le suivant ?

- **Continuation** : le prix repart dans le même sens.
- **Retournement** : il repart dans l'autre sens.

Si le marché OTC a une mémoire à une échelle, même courte, l'un des deux
gagne plus souvent que le point mort de 52,08 %. S'il n'en a aucune, les
deux restent sous ce point mort à toutes les échelles.

C'est de l'analyse du comportement des prix observés. Rien ne touche au
fonctionnement interne de la plateforme.

## Les décisions

- **11 échelles** k : 5 s, 15 s, 30 s, 1, 2, 3, 5, 10, 15, 30 et 60 min.
- **Instants** : les multiples de pas(k) = max(15 s, k / 2), soit toutes les
  15 s jusqu'à 30 s, puis k / 2.
- **Mouvement passé** : d = prix(t) − prix(t − k). Un mouvement nul n'est
  pas une décision.
- **Prix** : le dernier tick connu à l'instant voulu. Il doit dater de
  **moins de** F(k) = min(10 s, k / 2) : 2,5 s à 5 s d'échelle, 7,5 s à
  15 s, 10 s au-delà. Sa minute doit avoir son chemin de ticks, ainsi que la
  précédente si le tick vient de celle-ci.
- **Exécution** : entrée au prix de t + 2 s, sortie au prix de t + 2 s + k.
  Toutes les minutes de t − k à la sortie doivent avoir leur chemin.
  Une égalité est une perte, dans les deux sens.
- **Jouable** : le dernier payout du flux relevé au plus tard à l'entrée
  vaut au moins 84.

## Les trois règles × 11 échelles = 33 cases

| Règle | Décisions retenues |
|---|---|
| Dernier mouvement | toutes |
| Grand mouvement | \|d\| / prix(t − k) dans le décile supérieur de l'échelle (borne : quantile 9/10 de l'exploration) |
| Deux mouvements | prix(t − k) − prix(t − 2k) non nul et de même signe que d |

Chaque case choisit son sens en exploration : retournement s'il gagne
strictement plus souvent que la continuation, continuation sinon. Elle
garde ce choix en validation.

Pour chaque échelle, une **corrélation** entre le mouvement passé relatif et
le mouvement suivant relatif mesure l'aléatoire, sans verdict : 0 veut dire
aucun lien, un signe positif penche vers la continuation, un signe négatif
vers le retournement.

## Les trois temps

1. **Exploration**, du 06/10 au 02/11 : une case est retenue si son sens
   atteint 52,08 % sur au moins 300 décisions. Les bornes du grand
   mouvement sont fixées ici.
2. **Validation**, du 03/11 à la veille du lancement : chaque case retenue
   est jugée sur au moins 100 décisions. Le jugement utilise un bootstrap
   par blocs paire × jour (1 000 tirages, graine 7), puis
   Benjamini-Hochberg à 10 % sur les cases jugées. Une case est validée si
   BH la retient et que son taux dépasse 52,08 %.
3. **Confirmation** : une case validée est gelée, puis confirmée sur des
   jours futurs, sans argent réel, puis en démo.

## Limites connues, écrites avant le résultat

- **33 cases.** Le rapport dit combien passeraient l'exploration par pur
  hasard. Ce calcul tient compte des égalités, qui abaissent les deux sens.
- **Aux échelles courtes, les égalités pèsent** : en 5 secondes, le prix ne
  bouge souvent pas. La part des continuations et celle des retournements
  peuvent alors être toutes deux sous 50 %. Le rapport donne la part des
  égalités à chaque échelle.
- **Les décisions voisines se chevauchent** quand k dépasse le pas. Le
  bootstrap par blocs paire × jour tient compte de cette dépendance.
- **Les jours de validation ont déjà servi** à d'autres études. La
  confirmation sur des jours futurs reste obligatoire.
- **Rien n'est ventilé par heure ni par paire × heure**, pour que la
  confirmation EURJPY 22 h reste aveugle.
- **C'est une étude, pas un changement** : la course, la collecte, ZoneH1,
  les échéances et les configurations restent telles quelles.

## Choix d'écriture du code, fixés avant le résultat

- « Moins de F(k) » est pris au pied de la lettre : un tick âgé de F(k)
  tout juste est périmé.
- Les « décisions jouables » qui fixent la borne du grand mouvement sont,
  comme pour la sélection, les décisions jugées aux ticks : jouables, avec
  leurs prix et leur continuité.
- Les ampleurs de l'exploration sont gardées en simple précision (sept
  chiffres significatifs) le temps de fixer la borne. Celles de la
  validation sont arrondies de la même façon avant d'être comparées à la
  borne. Le classement est donc le même des deux côtés.
- Aucun tick d'avant le 06/10 n'est lu. Les premières décisions du 06/10
  aux longues échelles n'ont donc pas de prix à t − k : elles sont écartées.
- La grille s'arrête à 23 h 59 min 45 s : l'entrée a toujours lieu le jour
  de la décision. La période et le bloc sont ceux de ce jour.

## Lancer l'étude

Code : `maxprofit/live/echelles.py`. Pour tenir dans les 512 Mo du serveur,
la passe travaille ainsi :

1. Elle balaie l'exploration, paire par paire. Les comptes et les sommes
   de la corrélation se font au fil de l'eau. Chaque décision ne garde que
   son ampleur et son issue, soit 6 octets.
2. Elle fixe les bornes du grand mouvement, sans trier toute la liste, et
   compte les grands mouvements de l'exploration.
3. Elle balaie la validation avec ces bornes.

Banc local : une paire sur 41 jours, soit 938 000 décisions jugées, en
7 s, avec un pic de 37 Mo. L'essai donne la durée et la mémoire prévues sur
le serveur.

Tests : `tests/test_echelles.py`. Ils vérifient :
- le gel, la grille et les phases ;
- le quantile contre un tri complet ;
- le prix frais au tick près ;
- des décisions faites à la main : continuation, retournement, égalité, et
  un tick piège entre t et l'entrée, qui ne change rien ;
- tout le balayage contre un recalcul brut, sur des ticks irréguliers avec
  des minutes manquantes et des payouts changeants : statuts, comptes par
  sens, borne du grand mouvement et corrélations ;
- les cases, le détail par sens, les verdicts et le hasard attendu ;
- les sources, qui ne lisent rien hors de l'étude, et la lecture seule de
  bout en bout ;
- l'essai sans taux, et la commande.

Trente et une erreurs injectées volontairement dans le code sont toutes
détectées.

Commandes :
- **`/echelles essai`** : à lancer d'abord. C'est le contrôle technique
  sur une paire et deux jours, sans aucun taux, avec la durée et la mémoire
  prévues.
- **`/echelles`** : l'étude complète, à lancer une seule fois, et seule
  (pas pendant /motifs ou /cassure). Un point d'avancement arrive après
  2 minutes, puis toutes les 10 minutes.
- **`/echelles active`** : tout sur la base active, si la relève ne répond
  pas.

La passe rend un rapport et cinq fichiers :
- `echelles_cases.csv` : chaque case et chaque période, avec les
  continuations, les retournements, les égalités, le sens choisi, le taux,
  l'IC et le verdict ;
- `echelles_details.csv` : les cases retenues, par paire et par sens du
  mouvement passé (hausse ou baisse) ;
- `echelles_correlations.csv` : la corrélation par échelle et période, et
  la borne du grand mouvement ;
- `echelles_statuts.csv` : les décisions jugées et écartées, par période et
  échelle ;
- `echelles_donnees.csv` : les données lues, par paire et par jour.
