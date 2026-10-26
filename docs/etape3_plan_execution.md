# Étape 3 — Plan d'exécution (à approuver avant tout code)

Analyse hors ligne et en lecture seule. Aucune modification de la stratégie,
des signaux, des échéances, du collecteur ni de T0/T1/T2/T3. Aucune écriture
de séries de ticks en base. Un résultat négatif bien établi est un résultat.

## 1. Code existant réutilisé

| Besoin | Réutilisé |
|---|---|
| Signaux ZoneH1 et issues à 60…900 s | `apprentissage.historique.exemples_historiques` (paramètres pré-inscrits, échéance 900 s) |
| Contexte M1 à la décision | même module : `distance_niveau`, `elan_15m`, `elan_30m`, `mouvement_heure`, `volatilite_relative`, `depuis_contact`, `alternances_10`… ; features ZoneH1 `niveau`, `touches`, `entrees_deja_offertes` |
| Ticks décodés par minute, qualité des fenêtres | `apprentissage.audit_ticks.AuditTicks` (déjà parcouru par le rejeu quotidien de `/ticks`) |
| Règle de déduplication (instant, prix) et sa borne | `apprentissage.doublons` |
| Intervalles par blocs paire × jour | `apprentissage.microstructure.ic_blocs` |
| Payout en vigueur à la décision | `store.market.MarketReader.payout_at` (lu par `live`) |
| Registre des essais (nombre de comparaisons) | journal des hypothèses de `live.protocole` |
| Pré-inscription au gel | `live.preinscriptions` |

## 2. Données réellement disponibles

- **Tables utilisées** :
  - `tick_paths` (paire, minute, chemin compressé à la ms) ;
  - `candles` (OHLC M1, `tick_count` gonflé par les doublons) ;
  - `payouts` (payout et ouverture à chaque changement) ;
  - `uptime` (battements, seulement depuis le 24/10) ;
  - `executions`.
- **Pas stockés** : les signaux (recalculés), l'heure de réception, l'ordre
  d'arrivée, le socket d'origine, achat/vente, volume.
- **Historique de ticks** : environ 16 jours pour 20 paires, environ 453 000
  minutes. La date exacte du premier jour exploitable sera mesurée par la
  première passe, sans être supposée.

## 3. Calculable sans nouvelle collecte

Les familles A à D du livrable D, calculées sur ticks **distincts**
d'instant strictement antérieur à décision − G (G = 2 s) :
- **A — approche** : distances, vitesse, accélération, contacts, temps dans
  la zone, pénétration, temps depuis le dernier contact.
- **B — régime** : volatilité réalisée, amplitude, part de variations
  nulles, efficience, ratio de volatilité 1/15 min, concentration.
- **C — retournements** : retournements, persistance, part dans le sens du
  trade, bruit.
- **D — zone** : traversées, sorties et réintégrations.
- **E — qualité** : couverture, plus long gel, âge du dernier tick, part de
  doublons, proximité d'un gel ou d'une fermeture. Elle **sert à qualifier**
  les fenêtres, pas à prédire.

Sous-fenêtres de 1, 3, 5 et 15 min, fixées maintenant.

## 4. Impossible avec l'historique actuel

- Latence de réception et variables fondées sur l'ordre d'arrivée.
- Attribution d'un tick à un flux.
- Ticks tardifs écartés.
- Achat/vente, volume.
- « Largeur de zone » : ZoneH1 a une tolérance fixe de 0,02 % du prix, donc
  la largeur se réduit au niveau de prix.
- Limites basse/haute de la zone : un seul niveau, la zone est niveau ±
  marge.

## 5. Volume

- **Signaux** : environ 108 par jour dont la fenêtre de 30 min est complète,
  soit environ 1 700 sur l'historique disponible.
- **Ticks** : environ 1 800 ticks distincts par fenêtre de 15 min, environ
  3 millions au total.
- **Calcul** : faible (le même ordre que la passe doublons, environ 0,2 ms
  par minute de ticks).
- **Lecture** : c'est elle qui coûte. Relire tout l'historique, c'est environ
  0,2 à 0,3 Go de transfert réseau, à faire **une fois**.

## 6. Saturation et charge

- **Aucune écriture de ticks.** Les variables sont calculées dans le rejeu
  quotidien de `/ticks`, sur les chemins qu'il a **déjà décodés** : aucun
  transfert de plus au jour le jour.
- **Une passe unique sur tout l'historique**, avant toute conservation
  glissante ou archive des ticks, sinon ces jours seraient perdus pour
  l'étude.
- **Essai sur un échantillon d'abord** (1 paire, 1 jour) pour mesurer le
  temps et la mémoire avant la passe complète.
- **La période de confirmation exige que la collecte continue.** Elle dépend
  donc de la décision de continuité du stockage, toujours ouverte.

## 7. Tests de qualité, avant toute comparaison gagnants/perdants

- **Classes de fenêtre** VALID / PARTIAL / INVALID, aux règles du livrable D,
  figées dans le code et testées.
- **Anti-fuite** :
  - un test perturbe tous les ticks postérieurs à décision − G, puis les
    issues : aucune variable ne doit changer ;
  - un autre ajoute un tick juste avant la décision : il doit être exclu par
    la garde.
- **Doublons** : chaque variable de fréquence est calculée en ticks
  distincts et en ticks bruts. Les écarts sont publiés.
- **Cohérence** :
  - issue au tick = issue M1, attendu 100 % comme dans l'audit ;
  - résultat recalculé = résultat du journal pour les ordres comparables.
- **Reproductibilité** : empreinte des chemins lus, version du code,
  paramètres, période.

## 8. Hypothèses à pré-inscrire

**Découpage chronologique, fixé maintenant, avant toute lecture
gagnants/perdants** :
- exploration : décisions avant le **21/10 00:00 UTC** ;
- validation : du 21/10 00:00 au **gel**, proposé au 28/10 00:00 ;
- confirmation : décisions **à partir du gel + 1 jour**, sur des jours
  collectés après le gel, jusqu'à **500 signaux VALID** au moins (environ 5 à
  6 jours si la collecte continue).

**Référence**, connue à la décision :
- paire, créneau de 4 h UTC, sens ;
- `touches`, `entrees_deja_offertes` ;
- payout en vigueur ;
- `distance_niveau`, `elan_15m`, `volatilite_relative`, `mouvement_heure`,
  `depuis_contact`, `alternances_10`.

**Au gel** :
- au plus **4 hypothèses principales**, une par famille A à D, choisies en
  validation ;
- pour chacune, le sens de l'effet et les terciles fixés sur l'exploration.

Tout le reste est exploratoire et compté dans le registre des essais.

**Métriques** :
- principale : amélioration de la log-loss hors échantillon, référence +
  famille face à la référence. Modèle : régression logistique régularisée,
  en Python pur (le bot n'a pas numpy).
- secondaires : Brier, calibration, taux de gain par tercile, EV au payout
  réel, couverture.

**Incertitude** : bootstrap par blocs paire × jour, 1 000 tirages, graine 7.
Correction Holm sur les principales, Benjamini-Hochberg sur le reste.

## 9. Critères de réussite et d'arrêt

- **Confirmé** :
  - la log-loss s'améliore en confirmation, intervalle à 95 % au-dessus de 0
    après Holm ;
  - le tercile retenu gagne au moins 2 points ;
  - la meilleure log-loss tient dans au moins 2 semaines sur 3 ;
  - le résultat résiste à la garde G (0, 2, 5 s) et aux ticks bruts /
    distincts.
- **Arrêt ou « données insuffisantes »** :
  - un test anti-fuite échoue : arrêt jusqu'à correction ;
  - moins de 80 % de fenêtres VALID ;
  - moins de 500 signaux de confirmation ;
  - perte de l'historique des ticks avant la passe complète.
- **Conséquence d'un succès** : une candidate confirmée devient une
  pré-inscription, jamais un filtre direct de la course.

## 10. Fichiers créés ou modifiés (après approbation)

| Fichier | Rôle |
|---|---|
| `maxprofit/apprentissage/microstructure_pre.py` (nouveau) | variables A à E par signal, garde G, classes de qualité ; pur |
| `maxprofit/apprentissage/etude_micro.py` (nouveau) | découpage, référence, logistique régularisée, log-loss / Brier, bootstrap, Holm / BH ; pur |
| `maxprofit/apprentissage/audit_ticks.py` | appel de `microstructure_pre` sur les fenêtres des signaux, avec les chemins déjà décodés |
| `maxprofit/live/etude3.py` (nouveau) et commande `/micro` | rapport, fiche CSV par signal |
| `maxprofit/live/apprenti.py` | passe complète une fois, puis fenêtre quotidienne |
| `tests/test_microstructure_pre.py`, `tests/test_etude_micro.py` (nouveaux) | définitions, anti-fuite, qualité, déduplication, découpage, statistiques |
| `docs/etape3_*.md` (6 livrables) | au fil des résultats, sans écraser les précédents |

## Décisions à prendre

1. **Approuver le plan**, en particulier le découpage, la référence, la
   métrique et les critères.
2. **Autoriser la passe unique sur tout l'historique**, environ 0,2 à 0,3 Go
   de transfert réseau.
3. **Où garder les lignes calculées** (une par signal, environ 1 ko) :
   - **(a)** nulle part, on recalcule : il faut garder les ticks ;
   - **(b)** en CSV par Telegram, archivé par toi ;
   - **(c)** une petite table dérivée, environ 2 Mo puis 0,1 Mo par jour.
     C'est une écriture en base : à autoriser explicitement.
4. **Continuité du stockage** : elle conditionne la période de confirmation.
