# Pré-inscription : recherche automatique de motifs sur l'OTC (`/motifs`, 27/11)

Demandé le 27/11 :

> « C'est pas à moi de te décrire quoi que ce soit c'est ton boulot de voir
> ces trucs là »

Paramètres figés : `docs/preinscription_motifs.json`, empreinte
`028f63700036a68c96cd97a73d7438ed6439cef67a21235790a1740e7e7af6e3`
(`etude_micro.empreinte`). Le protocole a été figé et poussé avant le code
(commit 04efeab). La commande refuse de tourner si les paramètres embarqués
ne donnent plus cette empreinte.

## Le principe

Plutôt que de tester une règle à la fois, la machine passe au crible
**chaque minute de chaque paire OTC**. À la clôture de chaque bougie M1,
elle décrit l'état récent du prix par 22 familles de motifs objectifs, puis
regarde ce que le prix a fait dans les 1, 2, 3 et 5 minutes suivantes,
rejoué aux vrais ticks. Si un motif annonce la direction plus souvent que le
point mort de 52,08 %, octobre le désigne et novembre le confirme ou
l'infirme.

C'est de l'analyse du comportement des prix observés, comme un trader qui
regarde des graphiques, mais à l'échelle d'un million de minutes. Rien ne
touche au fonctionnement interne de la plateforme.

## Les 22 familles (511 états)

| Groupe | Familles | États |
|---|---|---|
| Couleurs des dernières bougies | les 1 à 6 dernières (VRV…), sans bougie sans corps | 126 |
| Couleur et taille | les 1 à 3 dernières, chaque corps petit, moyen ou grand (tiers de l'exploration) | 258 |
| Mèches | dernière bougie : couleur × mèche haute × mèche basse (< 0,2, 0,2-0,5, ≥ 0,5 de l'étendue) | 18 |
| Rendement | le chemin des 1, 3, 5, 15, 30 et 60 dernières minutes, en déciles | 60 |
| Position | où est le prix dans l'étendue des 20, 60 ou 240 dernières minutes, en quintiles | 15 |
| Volatilité | marché calme, moyen ou agité (30 contre 240 minutes) × couleur | 6 |
| Dernier extrême | le plus haut ou le plus bas de l'heure, et depuis combien de minutes | 12 |
| Série en cours | couleur × longueur de la série (1 à 8 et plus) | 16 |

Chaque état est jugé aux 4 échéances, soit 2 044 cases. Chaque case choisit
en exploration son sens (hausse ou baisse), puis le garde.

## Les deux scores

- **Score bayésien** : il additionne ce que 9 familles disent, état par
  état, de la hausse contre la baisse en exploration. C'est la façon la plus
  simple de combiner beaucoup d'indices.
- **Score logistique** : une régression sur 18 variables continues
  (rendements, positions, volatilité, corps, mèches, série, âge des
  extrêmes), ajustée en exploration sur une minute sur trois, pour tenir
  dans la mémoire du serveur.

Chaque score ne joue que le tiers de minutes où il est le plus sûr de lui ;
le seuil est fixé en exploration. Cela fait 8 cases (2 scores × 4
échéances).

## Ce qui compte

- **Exécution aux ticks** : entrée au dernier tick connu 2 s après la
  clôture, sortie à l'échéance. Les deux ticks doivent dater de moins de
  10 s, et les minutes entre les deux doivent avoir leurs ticks. Une égalité
  est une perte.
- **Seules les minutes jouables comptent** : payout du flux ≥ 84.
- **Pas d'épisodes** : chaque minute est une décision. La dépendance entre
  minutes voisines est prise en compte par le bootstrap par blocs paire ×
  jour.

## Les trois temps

1. **Exploration**, du 06/10 au 02/11 : une case est retenue si son sens
   atteint 52,08 % sur au moins 300 décisions. Les bornes, les poids et le
   modèle sont fixés ici.
2. **Validation**, du 03/11 à la veille du lancement : chaque case retenue
   et chaque case de score est jugée sur au moins 100 décisions. Le
   jugement utilise un bootstrap par blocs paire × jour (1 000 tirages), puis
   Benjamini-Hochberg à 10 % sur toutes les cases jugées.
3. **Confirmation** : une case validée est gelée, puis confirmée sur des
   jours futurs, sans argent réel.

## Limites connues, écrites avant le résultat

- **2 052 cases.** Le rapport dit combien passeraient l'exploration par pur
  hasard. Avec des centaines de milliers de décisions, la barre de 52,08 %
  est très stricte pour les états fréquents.
- **Les familles se recouvrent** (les couleurs des 3 et des 6 dernières
  bougies, par exemple) : leurs résultats ne sont pas indépendants.
- **Les jours de validation ont déjà beaucoup servi.** La confirmation sur
  des jours futurs reste obligatoire.
- **Rien n'est ventilé par heure ni par paire × heure**, pour que la
  confirmation EURJPY 22 h reste aveugle.
- **C'est une étude, pas un changement** : la course, la collecte, ZoneH1,
  les échéances et les configurations restent telles quelles.

## Lancer l'étude

Code : `maxprofit/live/motifs.py`. Il travaille en deux passes, pour tenir
dans les 512 Mo du serveur :
1. chaque décision jouable est enregistrée en colonnes compactes ;
2. les états sont calculés une fois les bornes connues, puis comptés dans
   un tableau plat (case × bloc paire-jour × issue).

Tests : `tests/test_motifs.py`. Ils vérifient :
- les 18 variables et les états des familles sur un graphique fait à la
  main ;
- les médianes glissantes contre un calcul direct ;
- l'absence de lecture du futur ;
- les issues aux ticks contre un recalcul brut, avec des ticks irréguliers
  et des minutes en retard ;
- les comptes contre un comptage direct, et les poids bayésiens ;
- deux scores qui retrouvent un effet planté ;
- les verdicts et la lecture seule.

Douze erreurs injectées volontairement dans le code sont toutes détectées,
dont une lecture du futur.

Commandes :
- **`/motifs essai`** : à lancer d'abord. C'est le contrôle technique sur
  une paire et deux jours, sans aucun taux, avec la durée et la mémoire
  prévues.
- **`/motifs`** : la recherche complète, à lancer une seule fois. Elle
  prend 20 à 60 minutes ; un point d'avancement arrive après 2 minutes, puis
  toutes les 10 minutes.
- **`/motifs active`** : tout sur la base active, si la relève ne répond pas.

La passe rend un rapport et cinq fichiers :
- `motifs_cases.csv` : chaque case et chaque période, avec le taux, l'IC et
  le verdict ;
- `motifs_details.csv` : les cases retenues, par paire ; les scores, par
  paire et par sens prédit ;
- `motifs_parametres.csv` : les bornes, les poids bayésiens, et les
  coefficients et seuils des scores ;
- `motifs_statuts.csv` : les décisions jouables, mesurées et écartées, et
  les statuts des issues ;
- `motifs_donnees.csv` : les données lues sur la relève.
