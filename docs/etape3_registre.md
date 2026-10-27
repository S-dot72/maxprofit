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
| `hypothese` | H1 à H4 pour une principale ou sa sensibilité |
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

## 2. La pré-inscription (une par hypothèse principale, au plus 4)

Elle est remplie à la fin de la validation (21/10 → 28/10), **sans** aucune
donnée de confirmation. Champs :

| Champ | Exemple | Origine |
|---|---|---|
| `id` | H1 | — |
| `famille` | A | une hypothèse au plus par famille A à D |
| `variable`, `w` | `A7_penetration`, 300 | validation |
| `traitement`, `k` principaux | distincts, 0 | fixés par le plan |
| `sens` | « plus de pénétration, moins de gains » | validation |
| `tercile` et ses `bornes` | 2, (0,31 ; 0,87) | bornes de l'EXPLORATION, figées |
| `horizon` | 900 s | fixé par le plan |
| `reference` | liste des variables et modalités | plan §5 |
| `preparation` | médianes, moyennes, écarts | exploration |
| `modele` | coefficients réestimés sur exploration + validation | une fois, puis figés |
| `regards`, `seuils` | 500 … 8 000 ; incréments O'Brien-Fleming | plan §7 bis |
| `effet_pratique` | +2 points | plan §9 |

## 3. Le gel

1. Toutes les pré-inscriptions, plus les paramètres communs (λ, graine,
   tirages, regards, seuils, périodes), forment **un** document JSON.
2. Son empreinte SHA-256 (`empreinte`) est notée au registre et dans le
   message du commit qui l'ajoute au dépôt.
3. La confirmation s'ouvre au premier minuit UTC qui suit ce commit. Ce qui
   tombe avant est en quarantaine.
4. Tout calcul de confirmation commence par `exiger_le_gel(parametres,
   empreinte)` : sans gel, ou sur des paramètres modifiés, il refuse.

Une hypothèse ajoutée ou modifiée après le gel est **exploratoire**, quels
que soient ses résultats.
