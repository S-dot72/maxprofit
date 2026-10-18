# Mission Reference 8 — rapport de cohérence (phase 0)

Rédigé le 2026-10-18, avant toute lecture des résultats de la mission.
Les chiffres vivants (populations, disponibilité des variables, couverture,
livrables A, B et C, verdicts) sont dans `/reference8`, recalculés à chaque
rejeu par `maxprofit/live/reference8.py`. Ce document donne les définitions
validées dans le code, les divergences trouvées entre les rapports, les
contrôles contre la fuite d'information, et ce qui ne peut pas être fait.

Rien dans cette mission ne modifie la stratégie de référence, ses seuils ou
les données : ce sont des mesures.

---

## 1. Définitions validées dans le code

### Opportunité, entrée immédiate, entrée confirmée

| Notion | Définition | Où |
|---|---|---|
| Opportunité (signal ZoneH1) | La stratégie rend un signal à la clôture de la bougie M1 `i`. Elle voit seulement sa fenêtre de `lookback` minutes, bougies closes, après la garde de calibration. | `apprentissage/historique.py`, `exemples_historiques` |
| Entrée immédiate | Clôture de la bougie du signal `i`. | idem |
| Entrée confirmée | La première des 3 bougies suivantes (minutes contiguës) qui va dans le sens du trade et clôture au-delà de l'extrême de la bougie du signal (`_retournement`). L'entrée se fait à sa clôture. Avec `CONFIRMATION_M1=suivante`, seule la bougie suivante compte. | `historique._confirmation_m1`, `laboratoire.confirmes` |
| Issue à l'échéance h | Clôture de la bougie qui commence à `t_entrée + h`, comparée au prix d'entrée. Une égalité ou une bougie manquante donne une issue absente : elle n'est jamais devinée. Mêmes conventions pour les deux entrées et pour chaque échéance. | `issues` (immédiate), `gagne_confirme_{h}` (confirmée) |
| Population jouée (« référence 8 ») | Les entrées confirmées qui passent la règle de l'heure en cours, LUE À L'ENTRÉE : le contexte `e_` remplace celui du signal. | `laboratoire._a_l_entree` |
| Espérance | 1,92 p − 1 par ordre. Elle suppose un payout de 92 % à toutes les échéances (voir divergence D7). | `validation_p2.esperance` |

### B, C, D et la valeur 99

Toutes ces grandeurs sont mesurées sur la fenêtre (au plus 300 bougies) qui
finit à la bougie d'entrée, cette bougie comprise. Au rejeu, ce sont les
clés `e_…`. Unité : l'amplitude M1, c'est-à-dire la moyenne des
`haut − bas` des 60 dernières bougies de la fenêtre.

| Variable | Formule | Où |
|---|---|---|
| **B** `taille_bougie` | (haut − bas de la bougie d'entrée) / médiane des `haut − bas` des 15 bougies précédentes. Vaut 1,0 si cette médiane est nulle. | `apprentissage/contexte.py` |
| **C** `part_parcourue` | (clôture d'entrée − niveau de zone) × sens / (obstacle − niveau) × sens. Définie seulement si le niveau est connu ET si un obstacle confirmé existe au-delà du prix et de la zone. Elle reste sous 1 et peut être négative si le prix est passé de l'autre côté de la zone. | idem |
| **D** `espace_obstacle` | \|obstacle − clôture d'entrée\| / amplitude. | idem |
| Obstacle (le « prochain sommet ou creux ») | Le pivot ZigZag CONFIRMÉ le plus proche au-delà du prix : un sommet pour un achat, un creux pour une vente. Seuil du ZigZag : 4 × la médiane des amplitudes en % sur les 60 dernières bougies. Seule la version honnête est utilisée (pivots confirmés, jamais l'extrême en cours). | `contexte._prochain_pivot`, `indicators/zigzag.zigzag` |
| Trajet zone → prochain sommet/creux | Le dénominateur de C : de la zone à l'obstacle. | idem |
| **99** (`CHAMP_LIBRE`) | Aucun pivot confirmé ne barre la route dans la fenêtre. C'est une **catégorie**, pas une distance : D ne l'écarte jamais (99 ≥ 1,95), et C est alors absente. Dans `/reference8`, les médianes de D l'excluent et son effectif est compté à part. | `contexte.CHAMP_LIBRE` |

### Seuils gelés pour la mission

| Filtre | Exclut | Origine |
|---|---|---|
| B | B ≥ 1,31 | Document de la mission (« taille relative d'au moins 1,31 »). Pré-inscrit le 18/10 (`R8-M1`, `R8-M3`). |
| C | C ≥ 0,46 | Pré-inscrit le 13/10 (`P2-C`). |
| D | D < 1,95 | Pré-inscrit le 13/10 (`P2-D`). |
| E | B ou C ou D | B + C + D, seuils ci-dessus (`R8-M3`). |

Une valeur manquante ne déclenche aucune exclusion (amendement du 14/10).

### Les mesures ajoutées pour la mission (`VERSION_EXPERIENCES = 10`)

- Trajectoire de l'**entrée immédiate**, jusque-là absente (`imm_mfe_15`,
  `imm_mae_15`, `imm_t_mfe`, `imm_zone_cassee`, `imm_t_pour`, `imm_t_contre`,
  `imm_net_*`).
- Pour les deux entrées : `t_pour` et `t_contre`, la première minute où le
  prix est allé d'au moins **1 amplitude** dans le sens du trade, puis contre
  lui. L'amplitude a été fixée avant lecture. 0 veut dire « pas en 15 min » ;
  quand les deux arrivent dans la même minute, une bougie M1 ne dit pas
  lequel est venu d'abord. S'y ajoutent `net_4`, `net_10`, `net_15`, le
  chemin net à chaque horizon.
- La couverture par **paire-heure** : minutes à bougie complète, par paire et
  par heure UTC (`Qualite.minutes_par_paire_heure`). Une paire-heure est
  exploitable avec au moins 54 minutes sur 60.

Ce sont des ISSUES : elles décrivent ce qui suit l'entrée et ne servent
jamais de filtre.

---

## 2. Contrôles contre la fuite d'information

| Contrôle | Où |
|---|---|
| Les valeurs `e_` (B, C, D compris) sont identiques quand on remplace toutes les bougies qui suivent l'entrée. | `tests/test_validation_p2.py::test_c_et_d_ne_dependent_pas_de_ce_qui_suit_l_entree` (B ajouté à l'assertion) |
| Un pivot n'est jamais visible avant sa confirmation, et un pivot confirmé ne bouge plus. | `tests/test_causalite.py` |
| Les indicateurs à fenêtre finie (RSI, MACD, ATR, Stochastic, EMA) ne dépendent pas de la longueur de l'historique. | `tests/test_causalite.py` |
| La règle de l'heure à l'entrée lit `e_mouvement_heure`, mesurée à la bougie d'entrée. | `historique.CONTEXTE_A_L_ENTREE` |
| Les strates de la sélection d'échéance (`R8-H`) n'utilisent que B, C et D à l'entrée. | `reference8.strate` |
| Les classes de trajectoire (rapide, lente, invalidée) et la rupture de zone sont des issues. Le rapport les décrit et ne les utilise jamais pour sélectionner. | `reference8._classe` |

Aucune fuite n'a été trouvée dans B, C et D.

---

## 3. Divergences entre les rapports (documentées, pas corrigées en silence)

**D1 — Le seuil de B : 1,31 ou 1,27.** Le document de la mission cite 1,31
(21,1 % sur 19 récents, 50,0 % sur 52 anciens). La lecture du 14/10 donnait
1,27 (30 % sur 20 récents), et c'est la valeur qu'utilise `/potentiel`
(`TAILLE_MAX`).
- **Cause.** `/experiences` recalcule ses terciles à chaque rejeu, sur
  70 % d'une fenêtre glissante de 30 jours. Les seuils « de Reference 8 »
  sont donc une lecture à un instant donné, pas une constante.
- **Décision.** 1,31 est gelé pour la mission. `/potentiel` reste inchangé :
  sa variante +B+C+D est une autre lecture. `/reference8` compte les entrées
  situées entre 1,27 et 1,31.

**D2 — Le seuil de D : 1,94 ou 1,95.** Le document de la mission cite 1,94,
la pré-inscription du 13/10 cite 1,95. La valeur pré-inscrite est conservée,
et `/reference8` compte les entrées situées entre les deux.

**D3 — Ce que « E » veut dire.** Trois définitions coexistent :
- dans le document de la mission, E = B + C + D ;
- « EXP-020 E » dans `/experiences` réunit aussi B, C et D, mais avec, pour
  chacun, le pire tiers désigné sur l'étalonnage À CHAQUE REJEU : sa
  composition peut changer d'un jour à l'autre ;
- **P2-E**, pré-inscrite le 13/10, ne réunit que C et D.

Pour la mission, M3 = B + C + D avec les seuils gelés. P2-E est gardée
telle quelle, sous le nom « C+D ». Aucune variante pré-inscrite n'est
supprimée.

**D4 — Les populations.**
- Les « 196 signaux confirmés : environ 69 % en entrée immédiate, 56 % en
  entrée confirmée » viennent de `/stabilite` (EXP-022). Ce rapport applique
  la règle de l'heure au SIGNAL. Les 220 entrées de `/audit`,
  `/experiences` et `/potentiel` l'appliquent à l'ENTRÉE. Les deux
  populations diffèrent, et la section 0 de `/reference8` compte l'écart
  dans chaque sens. Le livrable A applique la règle au signal, là où
  l'entrée immédiate décide.
- `/experiences` prend `fenetre or 3`, alors que `/audit`, `/potentiel`,
  `/stabilite` et `/reference8` prennent `fenetre or 1`. C'est identique quand
  `CONFIRMATION_M1` est réglée, différent quand elle est coupée.
- La coupure étalonnage / récents se fait au NOMBRE d'entrées (70 %) dans
  `/experiences`, `/potentiel` et `/reference8`, mais au TEMPS (70 % de la
  période) dans `/protocole` et la qualité.

**D5 — Les 220 entrées ne se reproduisent pas à l'identique.** Le rejeu
couvre les 30 derniers jours (`JOURS_APPRENTISSAGE`) : chaque rejeu voit une
population différente. La reproductibilité tient au commit, à
`version_regles`, à la période et à l'empreinte SHA-256 des bougies
(`/protocole`). Rejouer exactement la période de Reference 8 demanderait un
instantané de la base, qui n'a pas été fait.

**D6 — Le retard de la confirmation n'avait qu'une mesure** : l'écart
médian de distance à la zone (0,62 amplitude). L'entrée immédiate n'avait
ni MFE/MAE ni temps de progression. C'est ajouté dans la version 10.

**D7 — Le payout.** L'espérance suppose 92 % à toutes les échéances. Le
rejeu ne connaît pas le payout réel par échéance ou par paire. Les
espérances à 4 et 10 minutes sont donc des hypothèses, à recalculer avec le
payout observé.

**D8 — Deux mesures de couverture.**
- Les « 52,8 % d'écoute effective » viennent des battements de cœur du
  collecteur (`MarketReader.couverture`), toutes paires confondues.
- `/reference8` mesure aussi la couverture par paire et par heure, en
  minutes de bougies complètes.

Les deux ne se comparent pas directement.

**D9 — La valeur manquante de C.** La pré-inscription du 13/10 écartait
une entrée sans C. L'amendement du 14/10 la garde, comme la formule
mesurée dans `/experiences`. C'est déjà consigné dans
`live/preinscriptions.py`.

**D10 — La rupture de zone est lue après l'entrée.** Les « 81 % de zones
cassées » de l'audit des pertes décrivent ce qui s'est passé : ce n'est pas
un prédicteur. Le livrable C compare donc les caractéristiques connues
AVANT l'entrée, entre les gagnées, les perdues et les perdues à zone
cassée.

---

## 4. Règles de décision

- **Une décision par hypothèse** : soutenue, non soutenue ou indéterminée.
  Les intervalles sont à 95 %, par blocs paire × jour, avec 1 000 tirages
  et la graine 7.
- **Historique** (A1 à A4, B-x, B+x, C4, C10, Q) :
  - « non soutenue » si l'écart va dans le mauvais sens ;
  - « soutenue » seulement si l'intervalle est entièrement du bon côté ET si
    la règle n'a pas été conçue sur ces données ;
  - sinon « indéterminée ».

  Les seuils de B, C et D et le choix de 4 ou 10 minutes ont été inspirés
  par ces données. Un résultat favorable y reste donc indéterminé.
- **Fenêtre indépendante** (M1, M2, M3, D, C+D), ouverte le **19/10 à
  0 h UTC** :
  - le plan (taux de M0, part conservée, effet de plan, règle d'échéance)
    est figé au premier rejeu qui suit l'ouverture, sur les entrées
    d'avant ;
  - le gain minimal (`P2_GAIN_MIN_POINTS`) est figé à sa première lecture ;
  - le volume cible en découle ;
  - une fois ce volume atteint, chaque modèle est jugé sur les cinq critères
    de P2, et le verdict est figé.
- **R8-H** (sélection d'échéance) : la règle est apprise avant le 19/10 et
  figée, puis jugée contre 15 min fixe sur les mêmes entrées. « Soutenue »
  demande un intervalle au-dessus de 0 avec au moins 100 entrées.

---

## 5. Ce qui manque, et ce qui ne peut pas être fait

- Le payout réel par échéance et par paire (D7).
- Un instantané de la base à la date de Reference 8 (D5). Les analyses
  portent sur la fenêtre glissante courante, jamais sur exactement les 220
  entrées.
- La séquence exacte du haut et du bas dans une même minute :
  `meme_minute` reste une classe à part, sans ordre.
- Le nombre de contacts, les dépassements et les réintégrations ne sont
  connus qu'en partie à l'entrée : `touches`, `percees_zone` (1 h),
  `depuis_contact`. Aucune nouvelle variable n'est ajoutée : c'est hors du
  périmètre gelé.

## 6. Ce que la mission ne fait pas

Elle n'active aucune règle, ne retouche aucun seuil et ne poursuit pas
RSI, MACD ou les autres indicateurs. Elle ne déduit aucune manipulation des
ruptures de zone et n'optimise pas le nombre de signaux par jour.
