# Étape 3 — Registre des essais et pré-inscription

Le code est dans `maxprofit/apprentissage/etude_micro.py` (`Registre`,
`variantes_de_sensibilite`, `empreinte`, `exiger_le_gel`). Ce document fixe le
format **avant** le premier essai.

## 1. Le registre

Chaque calcul qui produit une statistique y entre **au moment où il est
lancé**, qu'il soit gardé ou abandonné. Une ligne abandonnée reste, avec sa
raison. Le nombre total d'essais, par nature, est publié avec chaque
résultat.

| Colonne | Contenu |
|---|---|
| `n` | numéro d'ordre, attribué par le registre |
| `phase` | exploration / validation / confirmation |
| `nature` | contrôle / exploratoire / principale / sensibilité |
| `hypothese` | H1 ou H2 pour une principale ou sa sensibilité |
| `variable`, `w`, `traitement`, `k`, `horizon` | l'objet de l'essai |
| `modele` | référence, référence + variable, combinaison |
| `statistique`, `valeur`, `p` | Δℓ, effet du tercile, Brier… |
| `statut` | calculé / abandonné |
| `note` | raison d'un abandon, remarque |

Sont comptés comme essais :
- chaque variable × sous-fenêtre × échéance de l'exploration (92 × 3) ;
- chaque seuil ou sous-fenêtre essayé puis écarté ;
- chaque interaction, avec son hypothèse écrite avant le calcul ;
- chaque variante de sensibilité d'une principale : 24 par hypothèse
  (bruts / distincts × k = 0 / 2 / 5 × blocs paire × jour / jour × égalités
  exclues / perdues).

## 2. La règle de retrait, et sa trace

Elle est appliquée une fois sur l'exploration, sans issue
(`retrait_des_quasi_constantes`, version 1, seuils de 30 valeurs et 5 %).
Une ligne par candidate (variable × fenêtre) :
- `valeurs`, `manquantes`, `distinctes` ;
- `q1`, `q3`, `iqr` ;
- `mode`, `hors_mode`, `part_hors_mode` ;
- `decision`, `raison`, `version`.

Cette trace est gelée avec le registre. Les ticks bruts n'entrent jamais en
sélection : ils servent de diagnostic. B3 et B6 bruts sont des artefacts du
doublement.

## 3. La pré-inscription (deux hypothèses principales au plus)

Elles sont choisies **a priori**, avant tout examen de leur lien avec les
issues. Les critères sont la définition, l'interprétabilité, la fiabilité
de la mesure et le mécanisme, jamais les résultats. Champs :

| Champ | Exemple | Origine |
|---|---|---|
| `id` | H1 | — |
| `famille` | A | — |
| `variable`, `w` | `A7_penetration`, 300 | choix a priori |
| `traitement`, `k` principaux | distincts, 0 | fixés par le plan |
| `sens` | « plus de pénétration, moins de gains » (−1) | choix a priori |
| `mecanisme` | « la zone a déjà cédé » | choix a priori |
| `tercile` | 2 (le haut) | choix a priori |
| `bornes` | (0,31 ; 0,87) | exploration, sans issue (second gel) |
| `horizon` | 900 s | fixé par le plan |
| `reference` | liste des variables et modalités | plan §5 |
| `preparation` | médianes, moyennes, écarts | exploration (second gel) |
| `modele` | coefficients réestimés sur exploration + validation | une fois, puis figés (second gel) |
| `regards`, `seuils` | 500, 1 000, 2 000, 4 000 ; incréments O'Brien-Fleming | plan §7 bis |
| `effet_pratique` | +2 points | plan §9 |

## 4. Le gel, en deux temps

1. **Gel des hypothèses** : les deux pré-inscriptions et les règles
   communes forment un document JSON :
   - λ, graine, tirages ;
   - regards et seuils ;
   - périodes et maturité ;
   - règle de retrait ;
   - sensibilités.

   Son empreinte SHA-256 (`empreinte`) est notée au registre et dans le
   message du commit qui l'ajoute au dépôt.
2. **Gel des paramètres** : après la passe de sélection, les valeurs
   déterminées mécaniquement par ces règles sont ajoutées dans un second
   document, qui cite la première empreinte :
   - bornes des terciles ;
   - préparation ;
   - coefficients ;
   - liste des variables retenues.

   Sa propre empreinte est notée de la même façon.
3. **Ouverture de la confirmation** : selon le choix (a) ou (b) du plan
   (§6), au premier minuit UTC qui suit le second ou le premier gel. Ce qui
   tombe avant est en quarantaine.
4. **Verrou du code** : tout calcul de confirmation commence par
   `exiger_le_gel(parametres, empreinte)` sur le second document. Sans gel,
   ou sur des paramètres modifiés, il refuse.

Une hypothèse ajoutée ou modifiée après le gel est **exploratoire**, quels
que soient ses résultats.
