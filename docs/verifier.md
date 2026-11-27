# `/verifier` : rejouer des trades OTC sur nos ticks réels

Demandé le 27/11, à propos d'un trader qui gagnerait des millions sur les
paires OTC :

> « J'ai l'impression que tu n'utilises pas ton plein potentiel […] au lieu
> d'apporter des solutions concrètes »

Le plus sûr moyen de savoir si quelqu'un a vraiment un avantage est de
rejouer ses trades sur les prix réels, un par un. `/verifier` le fait en
quelques secondes, sur la base active, en lecture seule, sans aucun ordre.

## Utilisation

On colle les trades sous la commande, un par ligne :

```
/verifier
fuseau UTC+3
EURUSD CALL 27/11 14:05:30 1min 1.08452 gagné
GBPJPY PUT 27/11 14:12:05 5min
```

| Élément | Obligatoire | Formats acceptés |
|---|---|---|
| Paire | oui | `EURUSD`, `EUR/USD OTC`, `eurusd_otc`… (paires OTC collectées) |
| Sens | oui | `CALL`/`PUT`, `HAUT`/`BAS`, `ACHAT`/`VENTE`, `BUY`/`SELL`, `UP`/`DOWN` |
| Date | oui, ou une ligne `date 27/11/2026` | `27/11`, `27/11/2026`, `2026-11-27` |
| Heure d'ouverture | oui | `14:05:30` (avec les secondes si possible), `14:05`, `14h05` |
| Échéance | oui, ou une heure de clôture | `30s`, `1min`, `5m`, `M5`, `2` (minutes) |
| Prix d'entrée | non | `1.08452` ou `1,08452` |
| Résultat annoncé | non | `gagné`/`perdu`, `win`/`loss`, `✅`/`❌` |

La ligne `fuseau` donne celui de l'écran où l'heure a été lue : `UTC+3`,
`-5`, `+5:30` ou un nom comme `Europe/Paris`. Sans elle, l'outil retrouve le
fuseau à partir des prix d'entrée, s'il y en a au moins 3. Il essaie chaque
décalage, par demi-heure de −12 h à +14 h, et garde celui où nos ticks collent
le mieux aux prix donnés (écart médian d'au plus 3 pips). Sinon, il prend
l'heure d'Haïti et le dit en tête du rapport.

## Ce qu'il calcule

- **L'entrée** est le dernier tick connu à l'heure d'ouverture, **la sortie**
  le dernier tick connu à l'échéance. Les deux doivent dater de moins de
  10 s, sinon le trade n'est pas jugé et la raison est donnée.
- **Le résultat** : gagné, perdu, ou égalité (sortie égale à l'entrée). Dans
  le taux, une égalité compte comme une perte ; le taux hors égalités est
  donné aussi.
- **Le bilan** :
  - le taux de réussite, avec son intervalle à 95 % (Wilson) ;
  - le résultat à mise fixe avec un payout de 92 % ;
  - la probabilité qu'un tel score arrive par pur hasard ;
  - un verdict par rapport au point mort de 52,08 %.
- **La cohérence** : si les résultats annoncés sont donnés, combien
  concordent avec les prix réels ; si les prix d'entrée sont donnés, l'écart
  médian avec nos ticks.
- **Le détail** des 25 premiers trades dans le message, et de tous dans
  `verification.csv`.

## Comment lire le verdict

- **Moins de 30 trades** : on ne peut rien conclure, car le hasard fait
  facilement 60 % ou 40 % sur si peu. Il en faut au moins 100, idéalement
  300.
- **Au-dessus du point mort, nettement** (le bas de l'intervalle dépasse
  52,08 %) : c'est un vrai indice. Il reste à le confirmer sur les trades
  **futurs** de la même source, notés avant leur issue.
- **Au-dessus, mais dans ce que le hasard produit** : il faut plus de trades.
- **Pas au-dessus du point mort** : à ce taux, la mise fixe perd de
  l'argent.

## Limites

- **Seules les paires OTC que la collecte enregistre** peuvent être
  vérifiées, à partir du 06/10. Une échéance trop récente (moins d'une
  minute) n'est pas jugée.
- **Une heure sans les secondes** est prise au début de la minute : le
  résultat devient approximatif, et le rapport le signale.
- **Nos ticks viennent du flux du compte démo.** Si l'écran d'un compte réel
  montrait d'autres prix, les prix d'entrée donnés ne colleraient à aucun
  décalage, et le rapport le dirait. Donner les prix d'entrée est donc le
  meilleur contrôle de la vérification elle-même.
- **Des trades choisis après coup** (les meilleurs d'une vidéo) ne prouvent
  rien, même s'ils sont tous gagnants. Seule une série complète compte, ou
  mieux, des signaux notés avant leur issue.

## Code et tests

- Code : `maxprofit/live/verification.py`. Il n'importe ni la course, ni
  l'exécution des ordres, ni la collecte, et n'écrit rien en base.
- Tests : `tests/test_verification.py`. Ils couvrent :
  - les formats de ligne ;
  - le rejeu sur des ticks connus (gain, perte, égalité, tick périmé, minute
    manquante, échéance à venir) ;
  - le fuseau retrouvé grâce aux prix ;
  - le bilan et le verdict ;
  - les résultats annoncés, l'échappement HTML et la lecture seule.

  Huit erreurs injectées volontairement dans le code sont toutes
  détectées.
- Mémoire : l'outil lit une paire à la fois, dans l'ordre du temps, et oublie
  les jours passés. Sur la base de banc, 300 trades répartis sur 39 jours se
  vérifient en moins d'une seconde, avec 17 Mo de mémoire en plus.
