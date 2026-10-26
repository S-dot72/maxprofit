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

## Ce qui est déjà connu (avant `/stockage`)

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
