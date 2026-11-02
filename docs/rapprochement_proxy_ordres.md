# `/rapprochement` : le proxy face aux ordres réels (02/11)

Décision du 02/11, variante recommandée :
- la piste USD n'est **pas** gelée comme expérience principale ; elle reste
  exploratoire ;
- on vérifie d'abord que le proxy des études représente ce que la course
  exécute, toutes paires ;
- ni la course, ni ses règles d'entrée, ni ses expirations ne changent ;
- aucune nouvelle hypothèse avant ce rapport.

> « L'objectif immédiat n'est plus de trouver un filtre gagnant. C'est de
> savoir si les performances que nous avons mesurées représentent réellement
> ce que la course exécute. »

Code : `maxprofit/live/rapprochement.py`. Commande Telegram `/rapprochement`
(admin), en tâche de fond. Tests : `tests/test_rapprochement.py`.

## 1. Ce qui est lu, et où

**Base active, en lecture seule** (`transaction_read_only`, plus un
registre qui refuse toute requête autre que SELECT, WITH ou SHOW). Elle
lit :
- la table `executions` entière, toutes campagnes, refus compris ;
- un payout du flux par signal : le dernier relevé avant la décision ;
- la première minute de ticks, pour dater le début de l'étape 3.

Le volume est petit. L'active fait foi pour le journal : la relève ne
reprend que les mises à jour récentes des ordres.

**Signaux** : le rejeu quotidien en mémoire, c'est-à-dire ZoneH1 avec la
stratégie et les paramètres de production, issues décisives, égalités et
sorties manquantes comprises.

**Bornes** : du premier signal du rejeu (30 jours) jusqu'au plus tôt de deux
dates :
- les issues mûres ;
- le 03/11 00:00 UTC, ouverture de la confirmation du gel 2, dont les
  issues ne sont pas lues avant ses regards.

## 2. La clé de correspondance : l'empreinte du contexte

**Aucun identifiant n'est commun** à un signal du rejeu, désigné par
(paire, décision), et à une ligne du journal :
- `signal_ts_ms` est l'instant du placement (`courtier.placer`), pas la
  bougie du signal ;
- la fin de la bougie du signal n'est pas enregistrée ;
- il n'y a pas d'identifiant de corrélation.

**La seule trace du signal dans l'ordre est son contexte**,
`brut["contexte"]`. La course le mesure à la décision avec
`apprentissage.contexte.contexte`. Le rejeu applique la même fonction :
- sur la bougie du signal, pour l'entrée immédiate ;
- sur la bougie de confirmation M1, pour l'entrée confirmée (clés « e_ »).

Une vingtaine de grandeurs arrondies à 4 décimales forment donc une
empreinte du signal.

**Règle d'association**, jamais la seule proximité dans le temps :
1. même paire et même sens ;
2. un ordre placé dans le **créneau** qu'impose la course :
   - de la fin de la bougie d'entrée à 90 s plus tard (fraîcheur de la
     course), plus 30 s de placement et 5 s d'horloge ;
   - la bougie d'entrée est celle du signal (k = 0), ou la k-ième suivante
     si la confirmation M1 attend k bougies (k lu dans le rejeu) ;
3. l'**empreinte** : le contexte de l'ordre comparé au contexte du rejeu
   sur la même bougie d'entrée.

**Comparaison** :
- **égale** : toutes les grandeurs communes à l'arrondi près ;
- **proche** : au moins 80 % à moins de 5 % (ou de 0,05), les grandeurs
  entières égales, et la zone identique (touches, entrées déjà offertes) ;
- **discordante** : sinon. L'ordre n'est pas celui du signal, même s'il
  tombe à la seconde près dans le créneau ;
- **non vérifiable** : moins de 6 grandeurs communes, ou aucun contexte.

| Classe | Définition |
|---|---|
| exacte | empreinte égale, dans le créneau, unique des deux côtés |
| approximative | empreinte proche, ou égale hors du créneau (à moins de 15 min), unique |
| ambiguë | plusieurs candidats vérifiés, ou des candidats non vérifiables ; jamais forcée |
| absente | aucun ordre candidat ; le signal reste dans le rapport |

**Un cas départagé par la règle de la course.** Deux signaux consécutifs
de la même zone peuvent partager la même bougie d'entrée : le premier est
confirmé par la bougie où le second signale. Leurs empreintes sont alors
identiques. Les arêtes conformes au mode de confirmation **actuel**
(`CONFIRMATION_M1`, « suivante » par défaut) passent d'abord. En mode
« suivante », la course joue le premier signal confirmé et ne met pas le
second en attente.

**Ordres sans empreinte.** La course n'écrit le contexte qu'à
l'acceptation. Un ordre rejeté, non envoyé, réservé sans suite ou rapatrié
du broker n'en a pas. Sa correspondance est au mieux **ambiguë**, et la
limite est comptée.

## 3. Les états d'un ordre

| État | Lecture du journal |
|---|---|
| réglé | accepté, résultat win, loose ou draw |
| exécuté, non réglé | accepté, résultat vide |
| exécuté, issue inconnue | accepté, résultat « unknown » |
| rejeté | refusé par le broker (`accepte = 0`, motif du broker) |
| non envoyé | réservation annulée : « non envoyé : … » |
| réservé sans suite | réservation jamais complétée : l'ordre est PEUT-ÊTRE parti |

## 4. Réel et proxy, sans mélange

- **Entrée réelle** : `openPrice` et `openTimestamp` du broker.
- **Règlement réel** : `closePrice` et `closeTimestamp`.
- **Payout réel** : `percentProfit`, contrôlé par le profit.
- **Résultat réel** : le statut du broker et son `profit` net.

Les horodatages du broker sont ramenés en UTC par le décalage mesuré à
l'ordre, arrondi à l'heure : c'est la convention de la collecte, donc celle
des bougies.

**Aucune bougie M1 ne tient lieu de prix du broker.** Un champ absent est
compté dans la section « Ce que contiennent les données », et dans la
colonne `limites` de l'ordre. Il n'est jamais reconstruit.

Le proxy figure à part, sous des colonnes nommées comme telles :
- `issue_proxy` : la clôture de la bougie du signal, puis la bougie
  complète qui commence 900 s plus tard ;
- `issue_proxy_entree_confirmee` : la même mesure, depuis la bougie de
  confirmation ;
- `prix_signal_bougie_M1` : la clôture de la bougie du signal.

**Contrôles d'intégrité** :
- résultat cohérent avec les prix du broker ;
- payout tiré du profit égal au payout du broker ;
- règle min(flux + 8, 92) ;
- durée tenue égale à l'échéance.

## 5. Les quatre mesures

Elles sont calculées pour l'ensemble, puis par période, par payout du flux
et par paire. Les IC sont à 95 %, par bootstrap de blocs paire × jour
(1 000 tirages, graine 7).

1. **Taux de correspondance** : (exactes + approximatives) / signaux.
2. **Taux de gain du proxy**, sur les issues décisives.
3. **Taux de gain des ordres réellement réglés** : gains / (gains +
   pertes), les égalités à part. Deux populations :
   - tous les ordres réglés de la période ;
   - les ordres appariés à un signal.
4. **Rendement réel** par mise, aux payouts appliqués : profit / mise, une
   égalité valant 0.

**Comparaison appariée**, sur les MÊMES signaux appariés et réglés :
- taux du proxy contre taux réel ;
- écart proxy − réel, avec son IC ;
- part des signaux où le proxy et le broker donnent la même issue.

**Motif probable d'une absence**, tiré des seules règles relues ici et dans
cet ordre :
1. pas de confirmation M1 au rejeu (mode actuel) ;
2. payout du flux sous le plafond à la décision (flux < 84) ;
3. course occupée par un autre ordre (un seul ordre à la fois, 15 min) ;
4. sinon « inexpliqué » : filtres, leçons, paire écartée, course arrêtée…
   Ce n'est pas reconstitué.

## 6. Fichiers rendus (CSV seulement)

- `rapprochement_signaux.csv` : un signal par ligne. On y trouve :
  - son identifiant `paire@décision`, sa période, son payout du flux ;
  - son issue proxy et sa correspondance ;
  - les clés comparées, égales et proches, et le créneau ;
  - l'ordre apparié : identifiant du journal et du broker, état ;
  - le retard de placement et d'ouverture sur la fin de la bougie
    d'entrée ;
  - les prix et heures du broker, les payouts, le résultat, le rendement ;
  - les limites.
- `rapprochement_ordres.csv` : un ordre du journal par ligne. On y trouve
  son état, son origine, ses champs broker, la cohérence, les limites, et
  son signal apparié ou ses candidats.
- `rapprochement_mesures.csv` : les quatre mesures et l'écart apparié, par
  dimension, avec les bornes des IC.

## 7. Ce que ce rapport ne fait pas

- Il ne change rien à la course.
- Il ne propose aucune hypothèse.
- Il ne corrige pas le proxy.
- Un écart entre le proxy et le réel est un fait sur la course. Il ne
  valide ni n'invalide aucune règle.
