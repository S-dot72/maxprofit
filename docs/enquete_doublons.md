# Enquête : double réception des ticks

Demandée le 25/10/2026 après le premier `/ticks` réel (47,6 % de doublons
exacts, ~250 ticks/min au lieu de ~125). **Rien n'est modifié** : ni le
collecteur, ni la stratégie, ni les données. La déduplication brièvement
déployée (#89, fusionnée à 21:50 UTC) a été retirée par #90 (fusionnée à
21:55 UTC) : au plus quelques minutes en service, peut-être aucune selon les
délais de déploiement.

Statut des affirmations : **[code]** lu dans le code, fichier et ligne cités ;
**[test]** rejoué par un test sans réseau ; **[données]** mesuré par
`/ticks` ; **[hypothèse]** ; **[non établi]**.

## 1. Le chemin d'un tick

1. **[code]** Chaque client de la bibliothèque a son `WebsocketClient` et son
   drapeau `updateStream` (pocketoptionapi `ws/client.py`, `on_message`). Une
   trame texte `451-["updateStream",…]` arme le drapeau ; la trame binaire
   suivante, `[[actif, instant, prix]]`, est ajoutée par
   `global_value.pairs[actif]['ticks'].append({'time': …, 'price': …})`
   (`ws/client.py` l. 215-222).
2. **[code]** `global_value` est un MODULE : un seul `pairs` par processus
   (`global_value.py` l. 40). Version lue : celle épinglée par
   `pyproject.toml` (c94c2b0), identique au paquet installé.
3. **[code]** Le collecteur (`collect/pocketoption.py`,
   `PocketOptionSource.connect`, l. 988 : `self._globals = global_value`) lit
   ce tampon par `GetTicks` (`stable_api.py` l. 189-192) dans `_drainer`,
   avec une position `_vus[paire]` ; `_vers_tick` en fait un `Tick(paire,
   ts_ms, prix)` ; au-delà de 5 000 entrées, il compacte (`del tampon[:fin]`).
4. **[code]** `Collector._consume` → `CandleAggregator.add`
   (`collect/collector.py`), sans aucune déduplication : la bougie compte
   chaque tick reçu (`tick_count`), le chemin les garde tous ;
   `store/chemin_ticks.encoder` les TRIE par horodatage (l'ordre d'arrivée
   est perdu) ; `flush` écrit les chemins, puis les bougies.
5. **[code]** Le courtier (`execution/courtier.py`, `CourtierDemo.connecter`,
   l. 107-) ouvre SON client, avec le même `global_value` (l. 115) et la même
   boucle de reconnexion (`preparer_client`). `suivre(paire)` envoie
   `change_symbol` sur son socket (l. 252-255) ; `prix()` lit le dernier
   élément du MÊME tampon (l. 266-273).
6. **[code]** La course (`live/plan_demo.py`, `CoursePlanDemo.__init__`,
   l. 917-919) appelle `suivre` pour chaque paire de son univers à sa
   construction, dans le processus du collecteur (« le collecteur, qui
   partage ce processus »).

**Conclusion [code]** : deux sockets, abonnés aux mêmes paires, écrivent dans
le même tampon, que le collecteur vide. **[test]** Rejoué sans réseau, chaque
tick y entre deux fois et la bougie le compte deux fois, OHLC inchangé
(`test_deux_sockets_sur_un_tampon_chaque_tick_compte_deux_fois`).

## 2. Abonnements et reconnexions

- **[code]** Collecteur : `Collector.refresh_pairs` renvoie `subscribe` pour
  TOUTES les paires éligibles quand la liste change (`collector.py`
  l. 410-420), et après chaque reconnexion (`run` remet `subscribed` à vide).
- **[code]** Reconnexion du collecteur : `_vider_tampons` (l'agrégateur est
  vidé, minutes partielles écrites incomplètes), puis le client EXISTANT est
  attendu (`_attendre_retablissement`, `_vus.clear()`), puis le tampon est
  relu depuis le début. **[test]** Cette relecture ne double rien à elle
  seule (`test_une_reconnexion_ne_double_pas_a_elle_seule`) ; elle rattrape
  même les ticks arrivés pendant la coupure par l'autre socket.
- **[code]** Les deux sockets se reconnectent seuls (`boucle_de_connexion`,
  `pocketoption.py` l. 381-418). Rien ne renvoie les `change_symbol` du
  COURTIER après une telle reconnexion ; `suivre` n'est rappelé avant un
  ordre que pour une paire qui avait échoué au démarrage
  (`plan_demo.py` l. 1671).
- **[code]** `get_candles` du courtier (historique) vaut aussi changement
  d'actif côté broker (commentaire `plan_demo.py`, mode plafond) : un autre
  chemin d'abonnement.
- **[code]** `ChangeSymbol` (majuscule) de la bibliothèque REMPLACE le
  tampon ; notre code ne l'appelle pas.

**[données]** Flux doublé partout, sauf le 20/10 de 08h à 23h20. **[hypothèse]**
Le socket du courtier s'est reconnecté seul vers 08h sans se réabonner, puis
le déploiement de 23h16 (nouveau processus, nouveaux `suivre`) a rétabli le
double flux. **[non établi]** : lequel des deux sockets a perdu ses
abonnements, et si le serveur envoie à un socket les actifs abonnés par
l'autre (session du même compte). Les 20 paires étant doublées, l'un ou
l'autre est requis, selon l'univers passé à la course.

## 3. Identité d'un événement

**[code]** Non. La trame est `[actif, instant, prix]` (instant en secondes à
la milliseconde, cf. `outils/diagnostic_pocketoption.py`) ; ni identifiant, ni
numéro de séquence. La bibliothèque ne garde que `{time, price}`.

**Conséquence** : la politique « doublons prouvés » est **NOT TESTABLE**.
Deux enregistrements identiques sont soit un même tick reçu deux fois, soit
deux ticks légitimes au même instant et au même prix ; aucune donnée stockée
ne les distingue (`test_l_heuristique_ne_distingue_pas_une_coincidence_legitime`).

## 4. Le socket d'origine après insertion

**[code]** Non identifiable : même liste, même forme d'entrée ; seul l'ordre
d'arrivée dans le tampon en garde une trace, et l'encodeur le détruit.

**[test]** Un socket en retard de plus d'un écart entre ticks change la
clôture de la bougie à la frontière de minute : sa copie du dernier tick
arrive après la minute suivante et est écartée comme tardive. La clôture est
alors un tick plus ancien, celle du chemin trié le dernier tick : la classe
« ordre d'arrivée » de l'audit (`test_un_socket_en_retard_change_la_cloture_a_la_frontiere`).
**[hypothèse]** Cela expliquerait les 176 écarts groupés par minutes communes
à toutes les paires (un socket qui prend du retard à un instant donné).
**[non établi]** Que les 176 aient tous cette cause.

## 5. Qui lit `tick_count`

| Consommateur | Usage | Effet des doublons |
|---|---|---|
| ZoneH1, la course, T0/T2/T3 (`strategies/zone_h1.py`, `live/plan_demo.py`, `/entrees`) | **aucun** (grep) | aucun : prix et OHLC seulement |
| EXP-001 `apprentissage/qualite.py` | drapeau `peu_de_ticks` (< 5, l. 167) ; `jours_debit_anormal` (l. 211) ; empreinte (l. 96) | moins de minutes « pauvres » ; un jour simple parmi des jours doublés peut être signalé ; variables de rapport |
| `store/research.py` l. 180 | empreinte de reproductibilité | change si les données changent |
| `backtest/engine.py` l. 300 | filtre `min_ticks` (5) | outil hors ligne, non importé par `live` ni `apprentissage` |
| `core/types.Candle.is_usable` | seuil 5 | aucun appelant |
| `strategies/six_conditions.py` l. 195 | variable de contexte | stratégie non sélectionnable (`strategie_de_la_course`) |
| Audit `/ticks` | concordance n ; minutes pauvres et débit en ticks distincts | décrit par l'audit |
| `store/market.upsert_candles`, fusion | MAX ; la bougie présente reste | une version doublée l'emporte sur une simple |
| `collect/inspect_data.py`, `outils/laboratoire.py` | affichage, transport | aucun |

**Les résultats historiques de T0/T2/T3 ne lisent pas `tick_count`** : ils ne
sont ni recalculés ni réinterprétés. Seules des variables d'audit et d'EXP-001
dépendent de la fréquence.

## 6-7. Comparaison des politiques, hors ligne

Module pur `apprentissage/doublons.py` (en mémoire, aucune écriture) et outil
`outils/doublons_ticks.py` (session en LECTURE SEULE, comme
`outils/audit_ticks.py`) :

- politiques : **aucune** ; **prouvée** (NOT TESTABLE, §3) ;
  **expérimentale** (instant, prix déjà vu dans la minute), pour comparaison ;
- mesures : régimes de minute (simple < 10 % de doublons, doublée ≥ 40 %),
  copies par (instant, prix) (1, 2, 3 ou plus), ticks sans jumeau dans les
  minutes doublées, même instant à un autre prix, OHLC changé, ordre
  temporel, écarts entre ticks, `tick_count` face aux bougies, minutes
  pauvres, jours de débit anormal d'EXP-001 ;
- **borne des faux positifs** : part des enregistrements identiques dans les
  minutes simples (un seul flux), qui majore ce que l'heuristique écarterait à
  tort.

**Sur les données réelles : PAS ENCORE EXÉCUTÉ.** Cette session n'a pas accès
à la base. Les nombres réels viendront soit de l'outil lancé par toi, soit
d'une section ajoutée à `/ticks` après ta validation (lecture seule, mais
c'est un déploiement). Sur données synthétiques (tests seulement, pas une
preuve) :
- la déduplication expérimentale ne change ni l'OHLC, ni l'ordre temporel ;
- elle divise `tick_count` par deux sur les minutes doublées ;
- elle fait passer une minute maigre en « pauvre » ;
- elle supprime le signalement d'EXP-001 d'un jour doublé parmi des jours
  simples.

## 8. Tests (`tests/test_enquete_doublons.py`)

- **Caractérisation du code actuel :**
  - un socket, chaque tick compte une fois ;
  - deux sockets, chaque tick compte deux fois, OHLC inchangé ;
  - un socket en retard change la clôture à la frontière de minute ;
  - une reconnexion ne double rien à elle seule.
- **Politiques :**
  - des ticks légitimes répétés au même prix à des instants différents sont gardés ;
  - le même instant à un autre prix est gardé ;
  - une coïncidence légitime est écartée à tort (limite assumée) ;
  - le résultat ne dépend pas de l'ordre d'arrivée ;
  - la politique « prouvée » est non testable ;
  - la comparaison couvre les régimes, la borne des faux positifs, les copies multiples, les ticks sans jumeau, l'OHLC, les minutes pauvres et le débit d'EXP-001.
- **Outil :** il lit sans écrire (empreinte du fichier inchangée).

## Risques de faux positifs de la déduplication (instant, prix)

- Des ticks légitimes identiques (même milliseconde, même prix) seraient
  écartés. Le risque est faible à ~2 ticks/s et à la milliseconde, mais
  **non mesuré** tant que la borne n'a pas été lue sur les minutes simples
  réelles (20/10, 08h-23h20).
- Une rafale du broker au même horodatage, si elle existe, serait tronquée :
  les copies multiples (3 ou plus) et la borne la révéleraient.

## Recommandation (à valider, rien n'est fait)

1. **D'abord les nombres réels** : la borne des faux positifs et les copies
   par (instant, prix), mesurées sur les données existantes.
2. **Point de correction.**
   - **Séparer les tampons, ou retirer l'abonnement redondant du courtier.**
     Cela traite la cause, mais supprime la redondance actuelle. Aujourd'hui,
     quand l'un des sockets perd ses abonnements, l'autre continue d'alimenter
     le collecteur. Et le courtier lit ses prix dans le tampon alimenté par le
     collecteur. Il faudrait alors, en plus :
     - réabonner le courtier après une reconnexion ;
     - contrôler l'âge du prix dans `prix()`.
   - **Garder les deux flux et les fusionner par (instant, prix) au
     drainage.** On conserve la redondance, mais l'heuristique a des limites
     explicites. C'est acceptable seulement si la borne mesurée est
     négligeable et si les copies multiples (3 ou plus) sont absentes.
   - Dans les deux cas : une modification distincte, versionnée et datée,
     avec la frontière visible dans `/ticks` (colonne « flux doublé »).
3. **Constats annexes, à traiter séparément :**
   - `CourtierDemo.prix()` ne vérifie pas l'âge du dernier prix, malgré sa
     docstring ;
   - la compaction du tampon par le collecteur peut le vider un instant sous
     le courtier (rare).
