# Étape 2 — A. Diagnostic du stockage (lecture seule)

Statuts : **Vérifié** (lu dans le code ou mesuré), **Probable**,
**Hypothèse**, **Inconnu**. Rien n'est écrit, rien n'est supprimé, aucune
migration.

## Pourquoi une commande du bot

Cette session de développement n'a pas accès à la base (et l'URL ne doit pas
transiter par la conversation). La mesure tourne donc **dans le bot**, sur
une connexion à part passée en lecture seule
(`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`) : commande
**`/stockage`** (`hosting/stockage.py`, mesures dans
`store/diagnostic_stockage.py`). Elle ne lance que des SELECT et des
fonctions de catalogue.

## Ce qui est mesuré, et comment

| Valeur | Méthode | Statut |
|---|---|---|
| Taille de la base | `pg_database_size(current_database())` | Vérifié |
| Toutes les bases du serveur | `SUM(pg_database_size(datname))` | Vérifié |
| Limite | `current_setting('neon.max_cluster_size', true)` si Neon l'expose ; sinon `QUOTA_STOCKAGE_MO` (512 par défaut) | Vérifié si exposée, sinon **Inconnu** : la console Neon fait foi |
| Taille comptée par Neon | `neon.pg_cluster_size()` si la fonction existe | Inconnu sinon |
| Par table | `pg_relation_size` (table), `pg_indexes_size`, TOAST (`reltoastrelid`), `pg_total_relation_size` ; « autres » = cartes d'espace libre et de visibilité | Vérifié |
| Lignes vivantes / mortes | `pg_stat_user_tables` (n_live_tup, n_dead_tup), dernier VACUUM | Vérifié (statistiques du serveur, approchées) |
| Croissance estimée | lignes par **jour de données** sur les 3 derniers jours complets × octets physiques moyens par ligne (`total / COUNT(*)`) | Probable : la taille physique avance par pages et peut gonfler de lignes mortes |
| Croissance mesurée | relevé de la taille toutes les 30 min, **en mémoire** (perdu au redémarrage) ; écarts sur 24, 48 et 72 h | Vérifié, dès 24 h de fonctionnement |
| Délai avant saturation | (limite − base) ÷ croissance : bas / central / haut = min / moyenne / max des jours observés | Hypothèse : croissance constante |

La croissance est comptée par jour de **données**, pas par jour
d'insertion : la base actuelle a reçu l'historique par synchronisation au
24/10, mais le volume d'une journée de données est le même, quel que soit le
moment où il est arrivé.

## Résultats mesurés — `/stockage` du 26/10 09:20 UTC (code f1aa617)

| Mesure | Valeur | Statut |
|---|---|---|
| Base | 423,5 Mo | Vérifié |
| Toutes les bases du serveur | 438,5 Mo | Vérifié |
| Limite | non exposée par le serveur (`neon.max_cluster_size` vide) | **Inconnu** |
| tick_paths | 208,9 Mo (table 172,4, index 36,3) ; 453 280 lignes vivantes, 28 mortes | Vérifié |
| payouts | 102,7 Mo (table 35,2, **index 67,5**) ; 550 334 lignes, 0 morte | Vérifié |
| candles | 102,7 Mo (table 53,3, index 49,3) ; 511 807 lignes, 59 mortes | Vérifié |
| autres tables | < 1,2 Mo au total | Vérifié |
| Relève | 409,2 Mo, limite non exposée | Vérifié |
| Croissance par jour (23, 24, 25/10) | 22,7 · 21,4 · 22,7 Mo (chemins ~13,9, bougies ~6,0, payouts ~3,5, battements ~0,2) | Probable (lignes × octets par ligne) |
| Croissance physique mesurée | pas encore : il faut 24 h de relevés en mémoire sans redémarrage | — |

**Délai avant saturation, si la limite est de 512 Mo [Hypothèse]** :
- si Neon ne compte que la base : 88,5 Mo restants, soit 3,9 à 4,1 jours ;
- si Neon compte toutes les bases du projet (438,5 Mo) : 73,5 Mo restants,
  soit **3,2 à 3,4 jours** (autour du 29/10 après-midi).
Le premier cas suppose que les bases système ne comptent pas ; rien ne
permet de le confirmer en SQL.

**Ce que les mesures excluent** :
- **Lignes mortes** : quasi nulles dans les trois grosses tables. Un VACUUM
  ne libérerait rien.
- **Payouts** : la compaction ne rapporterait rien (livrable B : 183 lignes
  répétées sur 550 334).

**Ce qui reste comme leviers, en Mo** — non mesuré tant qu'on ne l'a pas
fait :
- **Index** : 153 Mo d'index sur 414 Mo. [Hypothèse] Les index (paire,
  instant) se remplissent mal, parce que les insertions se répartissent sur
  20 à 183 paires, et les mises à jour des bougies (upsert) les font
  gonfler. Une reconstruction (REINDEX, sans toucher aux données) en
  rendrait peut-être 30 à 60 Mo, provisoirement : ils regonflent.
  Mesurable sans risque **sur la relève** d'abord.
- **Index redondant des payouts** : deux index portent les mêmes colonnes
  dans un ordre différent, la clé primaire `(ts_sec, pair)` et
  `idx_payouts_pair (pair, ts_sec)`. En faire un seul serait une migration,
  pour un gain d'environ un tiers des 67,5 Mo.
- **Croissance structurelle** : environ 22 Mo par jour. Même avec tous ces
  gains, 512 Mo ne tiennent pas plus de trois semaines d'historique complet.

## Console Neon (captures du 26/10, après la mesure de 09:20)

| Champ | Valeur | Statut |
|---|---|---|
| Offre | **Free plan**, 1 projet, 1 branche (`production`) | Vérifié |
| Calcul | 0,25 ↔ 2 CU ; 13 CU-h depuis le 19/10 | Vérifié |
| Storage | **464,83 MB** (0,46 GB) | Vérifié |
| History | 16,93 MB (rétention 6 h) | Vérifié |
| Network transfer | **2,39 GB** depuis le 19/10 | Vérifié |

- **Ce que compte Neon** [Probable] : la somme de **toutes les bases**, en
  Mo décimaux. À 09:20, « toutes les bases » faisait 438,5 Mio, soit
  459,8 Mo ; environ 5 Mo de croissance jusqu'à la capture mènent bien à
  464,83 Mo. La métrique qui déclenche la limite est donc le total du
  projet, pas notre seule base.
- **La valeur de la limite n'apparaît pas sur ces écrans** [Inconnu]. L'offre
  gratuite annonce 0,5 Go par projet (à confirmer sur la page de l'offre).
  Selon l'unité :
  - 500 Mo décimaux : **environ 35 Mo restants, soit environ 1,5 jour**
    (vers le 28/10 au matin) ;
  - 512 Mio (536,9 Mo) : environ 72 Mo restants, soit environ 3 jours.
  Il faut prévoir le pire cas. Que l'historique (16,93 Mo) compte ou non
  dans la limite est inconnu.
- **Transfert réseau, second risque** [Hypothèse sur la limite] : 2,39 Go en
  6 à 7 jours, soit 0,35 à 0,4 Go par jour. L'offre gratuite plafonne le
  transfert mensuel (5 Go à ma connaissance, à confirmer). À ce rythme, le
  plafond tomberait vers le 1er ou 2 novembre, bien avant le renouvellement
  du cycle (19/11).
  - Sources probables : les rejeux quotidiens (lecture de 7 jours de chemins
    de ticks et de l'historique des bougies), la synchronisation de la relève
    (qui relit la dernière heure toutes les 20 min), les lectures de la
    course.
  - Un `pg_dump` complet en coûterait environ 0,46 Go.
- **Calcul** : 13 CU-h en 7 jours n'est pas une contrainte à ce rythme (à
  confirmer face à l'allocation de l'offre).

## Ce qui était connu avant `/stockage`

- **Vérifié** (`/etat` du 26/10) : 416 Mo affichés sur 512 (81 %) ; chemins de
  ticks 205 Mo, payouts 102 Mo, bougies 101 Mo.
- **Probable** : environ 28 800 minutes de ticks et autant de bougies par
  jour, d'après deux `/etat` (20/10 : 298 520 et 357 496 ; 26/10 : 444 258
  et 502 754). Aux tailles moyennes actuelles, cela fait environ 14 Mo de
  chemins et 6 Mo de bougies par jour, sans les payouts.
- **Hypothèse** : 4 à 5 jours avant saturation si la limite est de 512 Mo.
  `/stockage` donnera les trois scénarios.

## Ce qui se passe à saturation (code lu)

- **Vérifié** : le pool ne bascule que sur le message « exceeded the
  quota » (`store/pool.py`, `est_quota`). **Probable** : le refus d'écrire
  pour taille dépassée a un autre texte (« project size limit … exceeded »,
  limite `neon.max_cluster_size`), donc **aucune bascule** — et une bascule
  n'y changerait rien : la relève reçoit tout l'historique.
- **Vérifié** : les écritures du collecteur échouent alors en boucle (pause
  croissante) ; les battements aussi, donc `/health` passe en 503.
- **Vérifié** : la course écrit la ligne de l'ordre AVANT de l'envoyer
  (`JournalExecution.reserver`), donc aucun ordre ne part : collecte et course
  s'arrêtent ensemble.

## Plan de continuité (à valider ; rien n'est lancé)

1. **Lire la vraie limite.** `/stockage` l'affiche si Neon l'expose ; sinon,
   la page *Usage* du projet dans la console Neon.
2. **Sauvegarder avant toute compaction**, hors de Neon : par exemple
   `pg_dump --format=custom --no-owner "$DATABASE_URL" > maxprofit.dump`
   depuis ton poste. C'est la condition de réversibilité.
3. **Gains sans perte de données**, par ordre de risque :
   - **VACUUM** : récupère la place des lignes mortes pour les écritures
     futures (le fichier ne rétrécit pas) ; `/stockage` dit combien il y en a.
   - **Compaction des payouts**, seulement si le livrable B la juge sûre.
   - **Offre Neon supérieure** : immédiat, payant.
4. **Dernier recours**, si la saturation arrive avant une décision :
   `STOCKER_TICKS=0` arrête l'écriture des chemins de ticks (205 Mo, la plus
   grosse table) ; bougies et payouts continuent. C'est une perte de données
   de recherche à venir, que toi seul peux arbitrer.
5. **Hors de portée de la relève** : ajouter des bases au pool ne donne pas de
   place, puisque chaque relève reçoit tout l'historique.

## Limites

- La croissance par lignes suppose des octets par ligne stables ; les index
  et les lignes mortes peuvent la faire varier.
- La croissance physique mesurée repart de zéro à chaque redémarrage.
- La limite et le mode de comptage de Neon (taille logique ou physique,
  historique de restauration) ne sont pas lisibles en SQL s'ils ne sont pas
  exposés.
