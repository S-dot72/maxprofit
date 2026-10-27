# Transfert sortant de Neon : diagnostic en lecture seule

Demandé le 27/10, avant toute modification. Aucun arrêt de collecte, aucun
nettoyage, aucun changement d'architecture ni déploiement correctif ne se
décide sur une estimation. Ce document dit **comment on mesure**, puis donne
un **inventaire du code**. Cet inventaire est une estimation à confirmer :
il ne désigne aucun responsable.

## 1. Le constat de départ

- **Limite** : 5 Go de transfert par projet et par mois sur l'offre
  gratuite (documentation de Neon). Au-delà, le calcul est suspendu
  jusqu'à la période suivante.
- **Consommation** : la console indiquait 2,39 Go du 19/10 au 26/10, soit
  environ 0,34 Go par jour sur **une seule période de 7 jours**. À ce
  rythme, le plafond tomberait vers le 3 novembre. C'est une
  **projection**, pas un diagnostic.

## 2. La mesure : `/transfert` (admin)

**Côté bot**, la mesure est passive (`store/mesure_transfert.py`) :
- c'est un compteur en mémoire, branché sur le curseur PostgreSQL ;
- il ne change aucune requête ni aucun résultat, et n'écrit rien ;
- une erreur de comptage le désactive, elle ne remonte pas.

Pour chaque lecture, il relève :
- le **consommateur** : la première fonction du projet hors de la couche de
  stockage (par exemple `live/apprenti.py:apprendre_une_fois`) ;
- l'**opération** : la méthode de `MarketReader`, ou le premier mot et la
  table ;
- la **base** : active, relève ou autre, jamais l'adresse ;
- les requêtes, les lignes et les **octets estimés sur le fil** ;
- la **portée** dans le temps : moins d'une heure, moins d'un jour, 1 à
  7 jours, au-delà, ou sans bornes ;
- les **journées relues** : une même paire et un même jour lus plusieurs
  fois par le même consommateur.

**Les octets sont une borne basse.** PostgreSQL envoie les résultats en
texte :
- un BYTEA (les chemins de ticks) voyage en hexadécimal, soit **2 octets
  par octet stocké** ;
- un nombre voyage comme son écriture décimale ;
- le chiffrement TLS et les messages de protocole ne sont pas comptés.

**Côté serveur**, `pg_stat_user_tables` est relevé toutes les 30 minutes :
ce sont les lignes parcourues par table, toutes connexions confondues. Ce
relevé recoupe l'ordre de grandeur, il n'attribue rien.

**Facteur réel** : noter le compteur « Network transfer » de la console à
l'instant t et à t + 24 h, puis le comparer au total mesuré sur la même
période. Le rapport sépare ce qui est mesuré de ce qui est extrapolé, et
donne la durée observée.

**Pour une mesure utile** :
- au moins 24 h sans redémarrage, contenant un rejeu quotidien ;
- la mesure repart de zéro à chaque démarrage.

## 3. Inventaire du code — ESTIMATIONS à confirmer

Le chiffre de chaque ligne est un calcul à partir du code et des tailles
moyennes observées : environ 300 octets par chemin stocké, et environ 100
octets par bougie sur le fil.

| Lecteur (consommateur attendu) | Ce qu'il lit | Fréquence | Estimation sur le fil |
|---|---|---|---|
| Rejeu quotidien (`live/apprenti.py:apprendre_une_fois`) | bougies de 30 jours × 20 paires (`JOURS_APPRENTISSAGE`) | une fois par jour, et au démarrage si `VERSION_EXPERIENCES` change | ≈ 85 Mo |
| idem, audit `/ticks` | chemins de 7 jours × 20 paires (`AUDIT_TICKS_JOURS`), en hexadécimal | une fois par jour | ≈ 130 Mo |
| Synchronisation de la relève (`store/pool.py`) | toutes les séries de la dernière heure **et 20 min** (marge de 1 h) : chemins, bougies, payouts de toutes les paires, battements ; ordres de 2 jours ; petites tables entières | toutes les 20 min (72 fois par jour) | ≈ 1,3 Mo par passage, soit ≈ 100 Mo par jour ; chaque minute de données est relue environ 4 fois par construction |
| Course (`live/plan_demo.py:_rafraichir_le_cache`) | bougies des 20 paires depuis la dernière connue, moins 5 min | à chaque passage | quelques Mo par jour |
| `/micro_essai` | fenêtres des signaux d'un jour | à la demande | ≈ 10 Mo par lancement |
| `/stockage`, `/etat`, sondes | catalogue, comptes, battements | à la demande, ou toutes les 30 min | faible, à mesurer |
| `/ticks`, `/lecons` | rien : servis depuis la mémoire du dernier rejeu | — | 0 |

**Somme des estimations** : environ 0,3 Go par jour, cohérente avec la
console (0,34 Go par jour). C'est une **cohérence d'ordre de grandeur**, pas
une preuve. Seule la mesure dira la part réelle de chacun, les relectures
effectives, et si un lecteur manque à cet inventaire.

## 4. Ce qui ne sera décidé qu'après la mesure

**Pistes possibles**, aucune n'est lancée :
- raccourcir la marge de relecture de la synchronisation ;
- rejouer sur moins de jours ;
- lire les chemins en binaire plutôt qu'en hexadécimal ;
- espacer la synchronisation ;
- passer à une offre supérieure.

Chacune sera évaluée sur les chiffres mesurés et soumise avant d'être
appliquée.
