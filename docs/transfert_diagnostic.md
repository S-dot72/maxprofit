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

## 5. Mesure du 27/10 22:55 au 28/10, 25,9 h (rejeu quotidien inclus)

**Total mesuré** : 402 Mo, soit environ 372 Mo par 24 h. C'est une borne
basse, cohérente avec la console (environ 0,34 Go par jour).

| Consommateur | Mo | Part | Constat |
|---|---|---|---|
| `hosting/health.py:rapport` (sonde `/health`) | 161 | 40 % | relit **toute** la table `uptime` deux fois par appel (`couverture`), 307 appels ; le volume grossit avec la table |
| `live/apprenti.py:apprendre_une_fois` (rejeu) | 162 | 40 % | chemins de 7 jours (118 Mo, en hexadécimal) et bougies de 30 jours (43 Mo) |
| `store/fusion.py:_lire` (relève, `plan_etat`) | 39 | 10 % | 3 lignes d'environ 170 Ko relues à chaque passage de 20 min |
| `store/pool.py:_lire_tranche` (relève, séries) | 13 | 3 % | |
| `execution/journal.py:_lire` | 12 | 3 % | |
| `live/plan_demo.py:_rafraichir_le_cache` | 9 | 2 % | |

**Côté serveur** : 180 millions de lignes de payouts et 167 millions de
bougies parcourues. Cela correspond, à 1 % près, à 307 `COUNT(*)` de la
sonde. C'est du calcul, pas du transfert.

**Corrections de l'inventaire du §3, faites par la mesure** :
- la sonde n'était pas « faible » : c'est le premier poste ;
- la synchronisation des séries était surestimée d'un facteur 8 environ.

**Limite de l'indicateur « journées relues »** : il compte par jour, et
surestime donc la relecture des fenêtres glissantes, comme celle de la
synchronisation.

## 6. Corrections A et B (29/10), déployées ensemble

**A — sonde `/health`** :
- la couverture (fenêtre de 24 h et cumul) est calculée par le serveur et
  tient en **une ligne d'agrégats**. Le résultat est identique à l'ancien
  calcul, vérifié sur SQLite (25 séries aléatoires et cas limites) et sur un
  PostgreSQL local (360 comparaisons) ;
- les compteurs de lignes (`COUNT(*)`) et les tailles sont gardés 30 min,
  avec leur âge publié (`compteurs_age_sec`) ;
- le battement reste relu à chaque sonde : c'est lui qui dit si la collecte
  vit.

**B — synchronisation de la relève** : pour les petites tables, on lit
d'abord la clé et le critère de remplacement (`maj_ts_sec`). On applique la
**même règle** que la fusion, et l'on ne rapatrie en entier que les lignes
qu'elle va écrire.
- **Résultat** : identique à la lecture complète, ligne pour ligne et bilan
  compris (20 états aléatoires sur SQLite, 36 sur PostgreSQL).
- **Garde-fous** : une comparaison qui échoue retombe sur la lecture
  complète ; au-delà de 50 lignes à relire aussi.
- **Mesuré en local** : un `plan_etat` inchangé passe de 120 Ko lus à
  114 octets.

**Gains attendus** : des **estimations**, pas des économies garanties.
- **A** : environ 150 Mo par jour, et la croissance quotidienne s'arrête.
- **B** : entre presque rien et environ 26 Mo par jour. La ligne de la
  campagne active change toutes les 10 min environ et restera copiée ; le
  gain dépend de la taille des lignes inactives.

**Vérification après déploiement**, 24 h sans autre changement :
- `/transfert` : `health.py:rapport` ne doit plus lire qu'une ligne par
  couverture ; `fusion.py:_lire` sur `plan_etat` ne doit plus porter que
  des clés ;
- console Neon : compteur avant et après, sur des périodes comparables.

**C — lecture binaire des chemins** : reportée (décision du 29/10).

## 7. Après A et B : mesure du 29/10 01:15 au 30/10, 24,2 h (rejeu inclus)

**Total mesuré** : 197 Mo, soit environ **195 Mo par 24 h**, contre 372
avant (−48 %). Borne basse, comme au §5.

| Consommateur | Avant (Mo/24 h) | Après | Constat |
|---|---|---|---|
| sonde `/health` (A) | 149 | 0,06 | une ligne d'agrégats par couverture ; comptages toutes les 30 min |
| relève, `plan_etat` (B) | 36 | 21 | seule la ligne de la campagne active, modifiée entre deux passes, est copiée (≈ 300 Ko) |
| rejeu quotidien | 150 | 140 | chemins de 7 jours (97 Mo, hexadécimal) et bougies de 30 jours (44 Mo) : **72 %** du reste |
| journal des ordres | 11 | 12 | |
| relève, séries | 12 | 10 | |
| cache de la course | 9 | 8 | |

**Côté serveur** : 31 millions de lignes de payouts et 29 millions de
bougies parcourues, contre 180 et 167 millions.

**Console Neon, le 30/10 vers 9 h 37** : 3,83 Go de « Network transfer »
depuis le 20/10. Le reste du quota mensuel (5 Go) est d'environ 1,2 Go. La
date de remise à zéro (début du mois civil ou anniversaire de la période)
n'a pas pu être lue dans la documentation de Neon depuis cette session.

**Conséquence pour l'étape 3** : la passe de sélection lit sur la relève
(son propre quota) et ne coûte à l'active que des résumés par paire × jour
(quelques Ko). Les pistes du rejeu (lecture binaire C, audit `/ticks`
incrémental) restent proposées, non lancées.
