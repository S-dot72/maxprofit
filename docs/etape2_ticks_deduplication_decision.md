# Étape 2 — C. Doublons de ticks : options et décision à prendre

Rien n'est appliqué. Le collecteur est dans son état d'avant #89 (retrait
#90). Sources : `docs/enquete_doublons.md` (mécanisme, code, tests) et la
section « 3 bis » du `/ticks` du 26/10 05:05 (code f1aa617, 7 jours,
20 paires, 197 312 minutes).

## Les preuves disponibles

| Constat | Valeur | Statut |
|---|---|---|
| Minutes simples / mixtes / doublées | 18 262 / 22 / 179 028 | Vérifié |
| Copies par (instant, prix) | 1 copie : 2 280 887 ; 2 copies : 22 317 240 ; **3 ou plus : 0** | Vérifié |
| Coïncidences dans un flux simple (borne des faux positifs) | 4 enregistrements identiques sur 2 275 551 ticks, soit ≤ 0,0002 % | Vérifié |
| Ticks distincts sans jumeau dans les minutes doublées | 0,02 % | Vérifié |
| Même instant, autre prix | **0** | Vérifié |
| Ticks distincts par minute | médiane 122 en flux simple comme en flux doublé | Vérifié |
| OHLC, ordre temporel après déduplication (instant, prix) | inchangés sur les 197 312 minutes | Vérifié |
| `tick_count` des bougies | égal au nombre stocké, doublons compris, sur 196 832/196 832 bougies | Vérifié |
| Deux sockets, abonnés aux mêmes paires, écrivent dans un seul tampon global | lu dans le code et rejoué par un test | Vérifié (code) |
| Le second socket est celui du courtier ; la fenêtre simple du 20/10 est sa reconnexion sans réabonnement | concordances temporelles | **Probable** |

**Ce que cela établit.** Dans un flux simple, deux événements n'ont
pratiquement jamais le même instant. On n'en voit aucun à prix différent, et
4 sur 2,3 millions au même prix. Le doublement produit toujours exactement
deux copies, jamais trois. La clé (paire, instant, prix) sépare donc les
événements avec un taux d'erreur mesuré d'au plus 2 par million. Ce n'est pas
une identité d'événement fournie par le broker : c'est une borne
**empirique**, valable tant que le flux garde ces propriétés.

**Ce qui reste non établi.** Que le second flux soit bien le courtier (aucune
trace du socket d'origine), et que les 4 coïncidences soient de vrais ticks
plutôt que des doublons isolés.

## Ce qui serait nécessaire pour attribuer un tick à son flux

Marquer chaque entrée à son arrivée avec le client qui l'a reçue : tampon par
client, ou champ ajouté par l'adaptateur. Rien de tel n'existe dans
l'historique. C'est un **changement du collecteur**, utile pour une preuve
directe du mécanisme, et sans effet sur les données déjà stockées.

## Les quatre options

### 1. Ne rien changer tant que le mécanisme n'est pas démontré
- **Risque** : nul pour les données. Le doublement continue : `tick_count` et
  les débits restent gonflés, les chemins pèsent environ 17 % de plus.
- **Preuve nécessaire** : aucune.
- **Acceptation** : sans objet.

### 2. Corriger la source (abonnements, tampons)
Exemples : le courtier ne s'abonne plus aux paires collectées, ou chaque
client a son propre tampon.
- **Risques** :
  - on perd la redondance actuelle : quand un socket perd ses abonnements,
    l'autre continue d'alimenter le collecteur, et le courtier lit ses prix
    dans le tampon que remplit le collecteur ;
  - il faudrait réabonner le courtier après chaque reconnexion et contrôler
    l'âge du prix dans `prix()`, qui ne le fait pas aujourd'hui (constat
    annexe) ;
  - changer la source touche la course.
- **Preuve nécessaire** : le marquage du flux d'origine, pour montrer que le
  second flux est bien le courtier.
- **Acceptation** :
  - sur 48 h après déploiement, minutes doublées 0 % ;
  - ticks distincts par minute inchangés (122 ± 2) ;
  - gels non plus fréquents qu'avant ;
  - aucun ordre en échec pour « aucun tick ».

### 3. Dédupliquer à la réception, dans l'agrégateur, par (paire, instant, prix)
- **Risques** :
  - jusqu'à environ 2 ticks légitimes par million écartés à tort (borne
    mesurée) ;
  - rupture datée de `tick_count` et des débits ;
  - les fusions de bases gardent le chemin le plus fourni, donc la version
    doublée l'emporte sur les minutes écrites par l'ancien ET le nouveau
    code (la minute du déploiement).
- **Preuve nécessaire** : celle qui existe déjà (tableau ci-dessus). Le
  mécanisme n'a pas besoin d'être démontré pour que la règle soit valable :
  elle ne repose que sur les propriétés mesurées du flux.
- **Acceptation** :
  - minutes doublées 0 % ;
  - ticks distincts par minute inchangés ;
  - OHLC et classes de concordance inchangés hors minute du déploiement ;
  - écart par minute entre `tick_count` et le nombre de ticks distincts : 0 ;
  - compteur de doublons écartés ≈ nombre de ticks reçus ÷ 2 ;
  - un réglage (variable d'environnement) pour revenir en arrière sans
    redéployer de code.
- **Gain de stockage [Probable]** : environ 17 % de la croissance des
  chemins, soit 1,5 à 2 Mo par jour sur environ 22. Ce n'est **pas** une
  solution au stockage.

### 4. Garder les flux tels quels, dédupliquer dans la couche d'analyse
- **Risque** : nul pour la collecte. Les analyses doivent toutes appliquer la
  même règle ; c'est déjà le cas dans l'audit (« 3 bis ») et ce sera imposé
  par le protocole de l'étape 3.
- **Preuve nécessaire** : la borne mesurée, qui suffit pour une règle
  analytique déclarée.
- **Acceptation** : toute variable de fréquence est calculée en ticks
  distincts, avec un test de sensibilité sur les ticks bruts.

## Recommandation

- **Maintenant** : **option 4**. Elle ne coûte rien, ne touche rien, et
  l'étape 3 peut avancer.
- **Pour le collecteur, à ta décision** : **option 3**, derrière un réglage
  réversible, plutôt que l'option 2. La 3 corrige l'effet sans retirer la
  redondance qui protège la collecte, et sa limite est chiffrée.
  L'option 2 n'est à envisager qu'après un marquage du flux d'origine,
  lui-même un petit changement du collecteur, à valider séparément.
- **Dans tous les cas** : ne pas la présenter comme une économie de
  stockage. Le gain est marginal face aux 22 Mo par jour.
