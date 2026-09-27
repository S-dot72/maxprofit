"""
Le plan, joué en DÉMO sur les signaux de l'hypothèse pré-inscrite.

--- ⚠ Ce que cette course peut établir, et ce qu'elle ne peut pas ---------

Elle **ne peut pas** établir que l'hypothèse gagne. Il faudrait 2 071 signaux
pour trancher 57,9 % à 3 sigma ; dix jours en produiront ~180. Une course
bénéficiaire ne prouverait rien, et une course perdante non plus.

Elle **peut** établir que la chaîne tient : signal -> dimensionnement ->
ordre -> dénouement -> solde, avec les gardes qui se déclenchent quand il
faut. C'est la phase 18 du protocole, et c'est la seule chose qui manque
entre un backtest et de l'argent réel.

--- Un diagnostic qui arrive AVANT la fin ---------------------------------

La fréquence des sessions perdues est elle-même informative, et elle parle
bien plus vite que le solde :

    précision 57,9 %  ->  session perdue 7,5 %   ->  deux d'affilée ~10 jours
    précision 50,0 %  ->  session perdue 12,5 %  ->  deux d'affilée ~3,5 jours

Si le réancrage se déclenche dans les trois premiers jours, l'hypothèse est
probablement du bruit — et on le saura sans attendre la dixième journée.

--- Les règles, telles qu'elles ont été fixées ----------------------------

    capital 250 $, risque 1/7, échéance 15 min
    trois pas maximum : si le troisième est perdu, la session s'arrête
    deux sessions perdues d'affilée -> on se réancre sur le jour du plan
    le plus proche du solde réel, on ne court pas après le plan

--- ⚠ D'où viennent les bougies, et pourquoi ça a changé -----------------

Elles venaient de NOTRE base. C'était le bon choix tant que la course se
limitait aux quatre paires épinglées — une seule source, celle-là même que
le backtest relit.

Mais les quatre épinglées sont une décision de COLLECTE, pas de trading.
S'y limiter réduisait le champ à une poignée d'actifs, et à la moitié du
temps UNE SEULE des quatre paie le maximum. La plateforme en cote près de
deux cents.

La course demande donc son historique AU BROKER, seule source qui couvre les
actifs qu'on ne collecte pas. Le prix à payer est réel et il faut le dire :
le direct et le backtest ne lisent plus la même source. Elles devraient
coïncider — même flux, même agrégation à la minute — mais ce n'est pas
garanti. `bougies_collectees()` reste là pour le contrôle : sur les quatre
paires épinglées, on peut comparer ce que le broker rend à ce qu'on a
enregistré, et c'est à faire avant d'accorder du crédit à un résultat obtenu
en direct.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from maxprofit.core.errors import BotError
from maxprofit.core.market_view import SequenceMarketView
from maxprofit.core.payout import PLAFOND_PCT, au_plafond
from maxprofit.core.types import Candle, Direction
from maxprofit.execution.courtier import CourtierDemo
from maxprofit.execution.journal import JournalExecution
from maxprofit.plan import (
    Arret,
    Echelle,
    EtatSession,
    Journee,
    PlanCapital,
    Session,
    jour_le_plus_proche,
)
from maxprofit.store.db import valider
from maxprofit.store.market import MarketReader
from maxprofit.strategies.zone_h1 import ZoneH1

log = logging.getLogger(__name__)

#: Le signal est daté à la clôture de la bougie. Au-delà de ce retard, on ne
#: le prend plus : la décision reposait sur un prix qui n'est plus le prix.
#: Le débit MESURÉ sur les quatre paires épinglées, en sessions par jour.
#: Affiché à côté du quota pour que l'écart se lise comme ce qu'il est — une
#: limite du marché — et non comme un retard de la course.
#:
#: 276 signaux éligibles sur 12,3 jours calendaires = 22,4 par jour ; une
#: session consomme 1,68 pas en moyenne (1 + q + q² à q = 46,4 %) ; donc
#: 13,4 sessions par jour.
#:
#: ⚠ Valait 16,1, et c'était surestimé : le calcul divisait par les jours
#: OBSERVÉS (10,2, les trous de collecte retirés) au lieu des jours
#: CALENDAIRES (12,3). Un trou de collecte ne produit pas de signaux, mais il
#: ne suspend pas le calendrier pour autant.
#:
#: ⚠⚠ ET LA MOYENNE N'EST PAS LA BONNE GRANDEUR. Écart-type 6,5 sur 11 jours
#: complets : pire jour 2,4 sessions, meilleur 23,8. Le quota de 16 n'est
#: atteignable que 27 % des jours. Sur DOUZE heures — la question qui se pose
#: en pratique — la moyenne tombe à 6,7 sessions et 16 n'est atteint que
#: 4,2 % du temps. Les cinq sessions observées en douze heures de direct sont
#: donc au milieu de la distribution, pas en dessous.
#:
#: Le quota reste à 18 : c'est un PLAFOND, et le rabaisser n'ajouterait
#: aucune session. Ce qui manque n'est pas de l'autorisation, c'est des
#: signaux — et ils dépendent de l'heure, très fortement : 1 pour 56 bougies
#: à 3 h UTC, 1 pour 1 031 à 14 h. Attendre plus longtemps ne rattrape pas
#: une heure creuse.
#:
#: Le seul levier serait d'élargir la collecte. Écarté le 2026-09-23 : les
#: quatre paires sont celles où l'hypothèse est PRÉ-INSCRITE (registre #58,
#: #59), et une validation hors échantillon faite sur un autre univers que
#: celui déclaré ne vaut rien.
SESSIONS_PAR_JOUR_MESUREES = 13.4

#: L'écart-type du débit journalier, en sessions. Affiché avec la moyenne :
#: sans lui, « 13,4 » se lit comme une promesse alors que la moitié des jours
#: en sont à plus de six sessions d'écart.
ECART_DEBIT_PAR_JOUR = 6.5

#: Refus d'affilée sur une paire avant de la mettre de côté.
#:
#: ⚠ MESURÉ EN PRODUCTION, ET IL A COÛTÉ UNE NUIT. BTCUSD_otc a reçu 34 ordres
#: en 34 minutes — un par minute, tous refusés par le broker avec « refus sans
#: motif », tous journalisés, aucun joué. Un refus rendait la main sans rien
#: retenir, donc le passage suivant retrouvait le même signal sur la même paire
#: et retentait. Pendant ce temps la course ne faisait rien d'autre : c'était
#: la seule paire au plafond de payout à ce moment-là.
#:
#: Trois, et non un : un refus isolé arrive (un payout qui bouge entre la
#: décision et le clic), et bannir sur un accident coûterait une paire saine.
REFUS_AVANT_QUARANTAINE = 3

#: Plafond de la quarantaine quand elle double. Vingt-quatre heures : au-delà
#: on ne réessaie plus assez souvent pour rattraper une cause qui a disparu.
QUARANTAINE_MAX_SEC = 86400

#: Combien de temps une paire refusée reste écartée, la PREMIÈRE fois.
#:
#: Une heure, parce que les causes plausibles se résolvent à cette échelle —
#: une mise sous le minimum de l'actif, une échéance que le broker n'accepte
#: pas sur lui, un actif suspendu — et parce qu'une paire définitivement
#: bannie disparaîtrait sans qu'on l'ait décidé. On réessaie, mais pas toutes
#: les minutes.
QUARANTAINE_SEC = 3600

#: Plage du rapport « tolérance de la stratégie / amplitude d'une bougie »
#: dans laquelle la stratégie a été calibrée.
#:
#: ⚠ CE N'EST PAS UN RÉGLAGE DE CONFORT : HORS DE CETTE PLAGE, LA STRATÉGIE
#: NE MESURE PLUS CE QU'ELLE CROIT MESURER.
#:
#: `tolerance_pct` sert à deux choses à la fois — dire qu'une bougie TOUCHE la
#: zone, et dire qu'elle la CASSE. C'est un pourcentage du prix, donc il suit
#: l'échelle des prix ; il ne suit PAS l'amplitude des bougies. Le même 0,02 %
#: est donc un filtre serré sur un actif agité et un filtre inexistant sur un
#: actif immobile.
#:
#: Mesuré sur les quatre paires où la stratégie a été calibrée : le rapport y
#: vaut 0,22 à 0,64, et il prédit le débit de signaux à la perfection
#: (Spearman rho = -1,00). Mesuré sur BTCUSD_otc : **36,01** — une bougie type
#: y fait 0,0006 % quand EURUSD_otc fait 0,0301 %. Résultat, toute bougie
#: touche toute zone proche : 820 signaux sur 1 139 bougies, soit un par
#: minute et demie, contre un pour trente-cinq sur EURUSD_otc.
#:
#: Ces 820 signaux sont aujourd'hui inoffensifs parce que le broker refuse
#: l'échéance de 900 s sur les cryptos. Ils cesseraient de l'être à la première
#: échéance acceptée — c'est exactement le piège d'une « adaptation aux
#: cryptos » qui ne changerait que la durée.
#:
#: La borne haute est large (4,0) : on écarte un actif hors d'échelle, pas un
#: actif un peu plus calme que les paires de calibration.
RAPPORT_TOLERANCE_MIN = 0.05
RAPPORT_TOLERANCE_MAX = 4.0

#: Bougies sur lesquelles l'amplitude typique est estimée.
FENETRE_AMPLITUDE = 60

FRAICHEUR_MAX_SEC = 90

#: Minutes relues à chaque rafraîchissement du cache des bougies, en plus des
#: nouvelles : une bougie écrite incomplète lors d'une coupure peut être
#: réécrite ensuite.
RECOUVREMENT_CACHE_SEC = 300

#: Actifs examinés par passage EN DEMANDANT L'HISTORIQUE AU BROKER. Les
#: paires que nous collectons ne comptent pas : leur historique est dans
#: notre base, et le lire ne coûte rien.
#:
#: ⚠ CE CHIFFRE EST UN BUDGET DE TEMPS, et il a été mesuré, pas choisi.
#:
#: Valait 8, dans l'idée qu'examiner large valait mieux qu'examiner peu.
#: Le direct a dit le contraire : 810 bougies en 401 minutes, soit 101
#: passages de 238 secondes, soit **27 secondes pour l'historique d'une
#: seule paire** (la borne de sécurité de `get_candles` est à 15 s, et
#: elle était atteinte souvent). Huit paires par passage faisaient donc
#: un passage de quatre minutes — alors qu'une bougie doit être traitée
#: dans les 90 secondes qui suivent sa clôture.
#:
#: Chaque bougie était donc marquée « évaluée », puis jetée pour
#: péremption avant d'atteindre la stratégie. Les compteurs montraient
#: une course très active qui n'analysait presque rien : 810 bougies
#: pour 1 signal, là où le taux mesuré sur les données collectées (1
#: pour 95) en promettait 9.
#:
#: Élargir l'univers COÛTAIT donc des signaux au lieu d'en rapporter. À
#: 1, le passage tient dans ~47 s et les paires collectées restent
#: fraîches. Un changement d'actif de plus lui
#: avaient fait fermer le socket. On en prend donc une poignée, en rotation,
#: plutôt que la centaine d'un coup.
PAIRES_MAX_PAR_PASSAGE = 1

#: Pause entre deux demandes d'historique, pour la même raison.
DELAI_ENTRE_ACTIFS_SEC = 0.4

#: Le catalogue des payouts est relu au plus toutes les quinze secondes. Il
#: est déjà en mémoire (`GetPairs`, aucune trame envoyée) : le relire toutes
#: les deux minutes n'économisait rien, et laissait jusqu'à deux minutes hors
#: de la recherche une paire qui venait d'atteindre le maximum.
RAFRAICHIR_UNIVERS_SEC = 15

#: Les deux univers, et ils répondent à deux questions différentes.
#:
#:   EPINGLEES  les quatre paires sur lesquelles l'hypothèse a été
#:              PRÉ-INSCRITE (registre #58, #59). C'est la seule campagne qui
#:              puisse la valider hors échantillon — et une validation brûlée
#:              ne se refait pas.
#:   PLAFOND    tous les actifs au payout maximal, ~39 sur 146 ouverts. Dix
#:              fois plus de signaux, mais sur un univers qui n'est pas celui
#:              de l'hypothèse : des actions, du crypto, des devises
#:              exotiques. Un solde qui monte n'y confirmerait RIEN de #58.
#:
#: Le défaut est EPINGLEES. Le test de la chaîne — signal, mise, ordre,
#: solde, gardes — se fait de toute façon dès les premiers ordres, quel que
#: soit l'univers : c'est un sous-produit, pas une campagne à part.
#: ⚠ LA RÈGLE D'INDÉPENDANCE DES PAS — et elle n'est pas cosmétique.
#:
#: Une martingale suppose que ses pas sont des paris INDÉPENDANTS. Sans cette
#: règle, ils ne le sont pas : la stratégie tire plusieurs signaux d'affilée
#: sur la MÊME zone, à une minute d'intervalle, et le pas 2 rejoue le pari que
#: le pas 1 vient de perdre.
#:
#: Mesuré en rejouant le plan sur les données collectées :
#:
#:     sans la règle   sessions perdues 15,3 %   250 $ -> 189,89 $  (-24,0 %)
#:     avec la règle   sessions perdues  0,0 %   250 $ -> 420,09 $  (+68,0 %)
#:
#: À 56 % de précision, une session à trois pas devrait être perdue 8,5 % du
#: temps. Les 15,3 % observés sont la signature de pas corrélés.
#:
#: ⚠ Le +68 % est MESURÉ EN ÉCHANTILLON, sur la période même où l'hypothèse a
#: été trouvée, avec une règle choisie APRÈS avoir vu que les pertes se
#: regroupent. Il ne vaut rien tant qu'il n'a pas tenu sur des données jamais
#: vues — c'est ce que cette course doit trancher.
DELAI_INDEPENDANCE_SEC = 900
#: Au-delà, une session qui attend un pas éligible est INTERROMPUE. Sans cette
#: borne elle attendrait indéfiniment, bloquant la journée entière, et ses
#: mises déjà engagées resteraient hors des comptes.
ATTENTE_MAX_PAS_SEC = 2 * 3600

#: L'univers sur lequel l'hypothèse a été PRÉ-INSCRITE (registre #58, #59).
#:
#: ⚠ EN DUR, ET NON DANS LA CONFIGURATION. Il l'était : la course recevait
#: `cfg.paires_fixes or PAIRES_PAR_DEFAUT`, c'est-à-dire la liste du
#: COLLECTEUR. Les deux coïncidaient, donc rien ne se voyait — mais élargir la
#: collecte d'une seule paire élargissait du même geste l'univers TRADÉ, et une
#: validation hors échantillon faite sur un autre univers que celui déclaré ne
#: vaut rien. Le défaut aurait détruit le test au moment précis où l'on croyait
#: seulement collecter plus.
#:
#: Ce que l'on collecte et ce que l'on trade sont deux décisions distinctes.
#: Celle-ci est figée par une pré-inscription ; l'autre est un réglage.
UNIVERS_PRE_INSCRIT: tuple[str, ...] = (
    "AUDCAD_otc", "AUDUSD_otc", "EURUSD_otc", "GBPAUD_otc")

UNIVERS_EPINGLEES = "epinglees"
UNIVERS_PLAFOND = "plafond"


@dataclass
class Etat:
    """Ce qui avance pendant la course. Sérialisable pour la reprise."""

    plan: PlanCapital
    solde: float
    jour: int = 1
    journee: Journee | None = None
    session: Session | None = None
    sessions_perdues_daffilee: int = 0
    reancrages: list[tuple[int, int, float]] = field(default_factory=list)
    derniere_bougie: dict[str, int] = field(default_factory=dict)
    #: De quoi distinguer « j'attends un signal » de « je suis cassé ».
    #:
    #: Sans ces compteurs, `/etat` affichait « pas encore démarrée » aussi
    #: bien pour une course qui évalue 240 bougies par heure sans rien trouver
    #: que pour une course qui ne lit plus la base du tout. Les deux
    #: ressemblaient à du vert.
    bougies_evaluees: int = 0
    signaux_trouves: int = 0
    #: Paires ÉCARTÉES faute de payout maximal, et le dernier payout vu sur
    #: chacune. Affichés tous les deux : sans eux, une course qui n'analyse
    #: rien parce que rien ne paie 92 % ressemble trait pour trait à une
    #: course qui ne trouve aucun signal — et l'on ne sait pas s'il faut
    #: patienter ou intervenir.
    paires_ecartees_payout: int = 0
    payouts_vus: dict[str, int] = field(default_factory=dict)
    #: Nombre d'actifs au plafond au dernier relevé du catalogue.
    univers_taille: int = 0
    #: Bougies jetées pour PÉREMPTION, c'est-à-dire lues trop tard pour être
    #: jouées. `bougies_evaluees` les compte ; la stratégie ne les voit pas.
    #: L'écart entre les deux est le rendement réel de la boucle.
    bougies_perimees: int = 0
    #: Signaux rendus par la stratégie, AVANT le filtre de payout.
    signaux_bruts: int = 0
    #: Paires lues dans notre base au dernier passage — celles qui ne coûtent
    #: rien, et les seules qu'on puisse suivre bougie par bougie.
    paires_gratuites: int = 0
    #: Paires écartées après des refus répétés, et jusqu'à quand.
    #: Persistées : un redéploiement remettrait sinon la boucle en route.
    quarantaine: dict[str, int] = field(default_factory=dict)
    #: Refus d'affilée par paire, remis à zéro dès qu'un ordre passe.
    refus_daffilee: dict[str, int] = field(default_factory=dict)
    #: Combien de fois une paire a DÉJÀ été écartée. La peine double à chaque
    #: récidive : réessayer toutes les heures une paire dont la cause de refus
    #: est permanente ne mène qu'à trois alertes par matinée.
    quarantaines_subies: dict[str, int] = field(default_factory=dict)
    #: Quand le dernier refus a eu lieu, par paire.
    #:
    #: ⚠ SANS LUI, « D'AFFILÉE » NE VEUT RIEN DIRE. Le compteur ne décroissait
    #: jamais : un refus isolé laissait un 1 permanent, et trois refus isolés
    #: espacés d'une semaine finissaient par écarter une paire parfaitement
    #: saine. La suite n'était consécutive que dans le nom.
    dernier_refus_ts: dict[str, int] = field(default_factory=dict)
    #: Paires écartées parce que la stratégie y est hors de sa calibration.
    paires_hors_calibration: int = 0
    #: Paires de l'univers dont NOTRE BASE n'a pas encore assez d'historique.
    #:
    #: Une paire qu'on vient d'ajouter à la collecte est éligible au payout et
    #: pourtant muette : la stratégie a besoin de 300 bougies de recul, soit
    #: cinq heures après l'abonnement. Sans ce compteur, « 6 actifs au plafond,
    #: 0 signal » ressemble à une panne alors que c'est le temps qui manque.
    paires_sans_historique: int = 0
    #: Les compteurs d'activité, nommés une fois pour que la persistance
    #: et la relecture ne puissent pas diverger.
    COMPTEURS = ("bougies_evaluees", "bougies_perimees", "signaux_bruts",
                 "signaux_trouves", "pas_sautes_independance",
                 "sessions_interrompues")

    #: Quand la course a démarré. Affiché, parce que « rien ne bouge » et
    #: « ça tourne depuis trois minutes » se ressemblent trait pour trait à
    #: l'écran, et appellent des gestes opposés : chercher une panne, ou
    #: attendre.
    #:
    #: ⚠ PERSISTÉ, et il ne l'était pas. Il était posé sur l'état NEUF à la
    #: construction, puis l'état rechargé de la base l'écrasait avec sa
    #: valeur par défaut, 0. `/etat` annonçait donc « en route depuis 0 min »
    #: en toutes circonstances — y compris sous 2 087 bougies évaluées, ce
    #: qui rendait le débit impossible à calculer au moment précis où l'on
    #: cherchait à savoir pourquoi il était bas.
    demarre_ts: int = 0
    #: Le solde du BROKER au démarrage de la course.
    #:
    #: ⚠ N'ENTRE PLUS DANS LE CALCUL DU SOLDE. Il l'a fait, sous la forme
    #: `solde_plan = capital_initial + (solde_broker - ancre)`, et cette
    #: dérivation avait un défaut qu'on ne voit qu'en la mettant à l'épreuve
    #: du compte réel : elle absorbe TOUT mouvement du compte, y compris ceux
    #: qui ne sont pas de nous. L'ancre a été posée à 259,71 $ ; le compte a
    #: ensuite été vidé à la main puis rechargé à 250 $ ; le plan a affiché
    #: 241,69 $ quand le broker en montrait 251,40. L'écart n'était pas une
    #: erreur de calcul — c'était le calcul qui faisait exactement ce qu'on
    #: lui avait demandé.
    #:
    #: Conservée pour mémoire du point de départ, et pour situer l'écart.
    solde_broker_ancre: float | None = None
    #: Le solde du broker au dernier relevé. AFFICHÉ, jamais utilisé pour
    #: décider : il sert de contre-vérification, pas de source.
    solde_broker: float | None = None
    #: Le solde au moment où la session en cours s'est ouverte. Sert à dire
    #: ce qu'elle a RÉELLEMENT coûté ou rapporté — la différence de deux
    #: soldes venus du broker, et non un montant théorique.
    solde_ouverture_session: float | None = None
    #: (actif, instant) du dernier pas RÉELLEMENT joué. Persisté : sans lui,
    #: un redémarrage rendrait le pas suivant immédiatement éligible et l'on
    #: rejouerait le pari corrélé que la règle existe pour empêcher.
    dernier_trade: tuple[str, int] | None = None
    pas_sautes_independance: int = 0
    sessions_interrompues: int = 0
    #: L'ordre EN COURS, tant qu'il n'est pas dénoué. Affiché : sans lui,
    #: `/etat` reste muet pendant le quart d'heure où l'option vit, c'est-à-dire
    #: pendant presque tout le temps où il se passe quelque chose.
    trade_en_cours: tuple[str, str, float, int] | None = None
    derniere_evaluation_ts: int = 0

    def ouvrir_la_journee(self) -> None:
        self.journee = Journee(plan=self.plan, solde=self.solde)


class CoursePlanDemo:
    """Fait tourner le plan sur les signaux de `ZoneH1`, en démo."""

    def __init__(self, lecteur: MarketReader, courtier: CourtierDemo,
                 journal: JournalExecution, plan: PlanCapital,
                 paires: tuple[str, ...], strategie: ZoneH1 | None = None,
                 mode_univers: str = UNIVERS_EPINGLEES, alerter=None,
                 paires_collectees: tuple[str, ...] | None = None):
        self.lecteur = lecteur
        self.courtier = courtier
        self.journal = journal
        self.paires = paires
        # Ce que NOTRE BASE contient, qui n'est pas ce que l'on trade. Toute
        # paire d'ici est lue localement — instantanément et toujours fraîche —
        # au lieu d'être demandée au broker pour 27 secondes. Par défaut les
        # deux listes coïncident : c'était le cas jusqu'ici, et c'est ce qui
        # rendait le couplage invisible.
        self.paires_collectees = tuple(
            paires_collectees if paires_collectees is not None else paires)
        self.strategie = strategie or ZoneH1()
        #: Prévenir l'opérateur. Une course qui tourne dix jours sans rien
        #: dire oblige à interroger `/etat` au hasard : on découvre un
        #: réancrage trois jours après, ou jamais.
        self._alerter = alerter
        #: Appelé quand un état doit être figé sans attendre la fin du tour.
        #: Posé par `fabriquer_course`, qui seul connaît la connexion.
        self._sauver = None
        if mode_univers not in (UNIVERS_EPINGLEES, UNIVERS_PLAFOND):
            raise BotError(
                f"mode_univers inconnu : {mode_univers!r}. Attendu "
                f"{UNIVERS_EPINGLEES!r} (les paires pré-inscrites) ou "
                f"{UNIVERS_PLAFOND!r} (tous les actifs au maximum).")
        self.mode_univers = mode_univers
        self.etat = Etat(plan=plan, solde=plan.capital_initial)
        self.etat.ouvrir_la_journee()
        # Ne vaut que pour une course NEUVE : `fabriquer_course` remplace
        # l'état par celui de la base juste après, et n'en repose une que si
        # l'état rechargé n'en avait pas.
        self.etat.demarre_ts = int(time.time())
        self._univers: list[str] | None = None
        self._univers_ts = 0
        self._rotation = 0
        #: Rotation des paires COLLECTÉES, distincte de celle des payantes.
        #: Les deux files n'avancent pas au même rythme — les payantes d'une
        #: par passage, les gratuites toutes — et un compteur partagé aurait
        #: fait tourner l'une au rythme de l'autre.
        self._rotation_gratuites = 0
        #: La journée UTC que la course croit être en cours. 0 = pas encore
        #: établie ; le premier passage la pose sans rien déclencher.
        self.jour_utc_courant = 0
        #: Les bougies des paires collectées, gardées d'un passage à l'autre :
        #: seul ce qui est nouveau est relu. Voir `_rafraichir_le_cache`.
        self._cache_bougies: dict[str, list[Candle]] = {}
        self._cache_fin: int | None = None
        #: Secondes entre la clôture d'une bougie et son évaluation, puis
        #: entre la clôture et le clic : les deux délais que la vitesse de
        #: la course doit réduire. Les derniers seulement — c'est une jauge.
        self.delais_evaluation: deque[float] = deque(maxlen=200)
        self.delais_clic: deque[float] = deque(maxlen=50)
        self._fin_bougie_du_signal: int | None = None
        # ⚠ L'ABONNEMENT N'EST PAS FACULTATIF, ET PLUS DANS AUCUN MODE.
        #
        # Les bougies des paires collectées viennent de la base, mais
        # `placer()` a besoin du dernier PRIX, que le broker ne sert que sur
        # un actif souscrit. Sans cet abonnement, l'ordre échoue sur « aucun
        # tick disponible » — au moment de trader, pas avant.
        #
        # La condition portait sur le mode, et c'était juste tant que le mode
        # décidait de la source : en mode plafond, l'abonnement se faisait
        # tout seul puisque demander un historique change déjà d'actif.
        # Depuis que la source dépend de la PAIRE et non du mode, une paire
        # épinglée n'est plus jamais demandée au broker — donc plus jamais
        # souscrite — et c'est précisément celle sur laquelle on tradera le
        # plus souvent.
        #
        # Quatre abonnements tiennent : c'est ce que le mode épinglées faisait
        # déjà. C'est huit changements CONCURRENTS qui avaient fermé le
        # socket, et le budget d'une paire par passage les exclut désormais.
        #
        # ⚠ AU MIEUX, ET SURTOUT PAS EN LEVANT. `suivre()` refuse un actif qui
        # n'envoie aucun tick en 20 s, et ce refus est JUSTE avant un ordre :
        # on ne mise pas sur un prix inconnu. Au démarrage il est destructeur.
        #
        # Une paire FERMÉE n'envoie légitimement aucun tick. AUDCAD_otc était
        # fermée, la construction a levé, le superviseur a reconstruit, la
        # construction a levé encore — cinq fois, puis course ABANDONNÉE. Une
        # paire hors séance a donc arrêté tout le plan, et les cinq autres
        # étaient parfaitement tradables.
        #
        # La garantie reste où elle compte : `placer()` lit le prix et lève si
        # le tampon est vide. Un actif non souscrit fait échouer SON ordre, pas
        # la course — et trois échecs le mettent en quarantaine.
        self._non_souscrites: set[str] = set()
        for p in paires:
            self._suivre_au_mieux(p)

    # --- le signal ---------------------------------------------------------

    def _suivre_au_mieux(self, paire: str) -> bool:
        """S'abonne si possible, note l'échec sinon. Ne lève jamais."""
        try:
            self.courtier.suivre(paire)
        except BotError as erreur:
            self._non_souscrites.add(paire)
            log.warning(
                "Abonnement à %s impossible (%s). La course continue sans "
                "elle et réessaiera avant d'y passer un ordre.", paire, erreur)
            return False
        self._non_souscrites.discard(paire)
        return True

    def bougies_collectees(self, paire: str, n: int) -> list[Candle]:
        """Les bougies de NOTRE base — seulement pour les paires collectées.

        C'est de nouveau la source de décision sur ces paires-là, et pour une
        raison qui se chiffre : la lire coûte une requête locale, quand
        demander la même chose au broker coûte 27 secondes (voir
        `PAIRES_MAX_PAR_PASSAGE`). À ce prix, les paires collectées sont les
        seules qu'on puisse évaluer À CHAQUE bougie.

        C'est aussi la source du backtest : sur ces paires, le direct
        redevient comparable à l'hypothèse pré-inscrite.

        Lues dans le cache que `_rafraichir_le_cache` tient à jour.
        """
        if paire not in self._cache_bougies:
            self._rafraichir_le_cache()
        if self._cache_fin is None:
            return []
        # La même fenêtre que la requête d'origine, [fin - n min, fin] :
        # la stratégie doit voir exactement les bougies qu'elle voyait.
        plancher = self._cache_fin - n * 60
        return [b for b in self._cache_bougies.get(paire, ())
                if b.complete and b.ts_sec >= plancher]

    def _rafraichir_le_cache(self) -> None:
        """Relit les bougies des paires collectées — seulement les nouvelles.

        ⚠ C'ÉTAIT L'ESSENTIEL DU TEMPS D'UN PASSAGE. Chaque paire coûtait deux
        requêtes à la base distante — le dernier horodatage, puis ses 300
        dernières bougies — à chaque passage, même quand rien n'avait changé :
        une vingtaine d'allers-retours et 3 000 lignes pour dix paires. On
        relit désormais toutes les paires en UNE requête, depuis la dernière
        bougie connue.

        Les dernières minutes sont relues quand même (`RECOUVREMENT_CACHE_SEC`)
        : une bougie écrite incomplète à une coupure peut être réécrite.
        """
        paires = self.paires_collectees
        n = self.strategie.p.lookback
        if self._cache_fin is None:
            fin = self.lecteur.last_candle_ts_sec()
            if fin is None:
                return
            depuis = fin - n * 60
        else:
            depuis = self._cache_fin - RECOUVREMENT_CACHE_SEC
        try:
            fraiches = self.lecteur.candles_de(paires, 60, depuis)
        except BotError as erreur:
            # Une ligne illisible écartait autrefois UNE paire ; lue en bloc,
            # elle ne doit pas priver toutes les autres de leurs bougies.
            log.warning("Bougies collectées illisibles, cache conservé : %s",
                        erreur)
            return
        for paire in paires:
            gardees = [b for b in self._cache_bougies.get(paire, ())
                       if b.ts_sec < depuis]
            self._cache_bougies[paire] = gardees + fraiches.get(paire, [])
        fin = max((c[-1].ts_sec for c in self._cache_bougies.values() if c),
                  default=self._cache_fin)
        if fin is None:
            return
        self._cache_fin = fin
        plancher = fin - n * 60
        for paire, bougies in self._cache_bougies.items():
            if bougies and bougies[0].ts_sec < plancher:
                self._cache_bougies[paire] = [
                    b for b in bougies if b.ts_sec >= plancher]

    def _bougies_de(self, paire: str) -> list[Candle]:
        """La source dépend du MODE, et ce n'est pas un détail.

        La source ne dépend plus du MODE mais de la PAIRE, et c'est la
        correction : une paire que nous collectons est lue dans notre base,
        même quand l'univers est ouvert à tous les actifs au plafond.
        Instantané, même source que le backtest, aucun risque de blocage.

        Pour les autres, il n'y a pas le choix : on ne les collecte pas, seul
        le broker a leur historique. On y perd la comparabilité avec le
        backtest, on hérite d'une fonction de bibliothèque qui boucle sans
        limite — bornée dans `CourtierDemo` — et l'on paie 27 secondes. D'où
        le budget strict de `PAIRES_MAX_PAR_PASSAGE`.
        """
        if paire in self.paires_collectees:
            return self.bougies_collectees(paire, self.strategie.p.lookback)
        return self.courtier.bougies(paire, self.strategie.p.lookback)

    def univers(self) -> list[str]:
        """TOUS les actifs ouverts qui paient le maximum, pas seulement les
        paires épinglées.

        ⚠ Les quatre épinglées sont une décision de COLLECTE. S'y limiter pour
        chercher des signaux réduirait le champ à une poignée d'actifs alors
        que la plateforme en cote près de deux cents, dont plusieurs dizaines
        au plafond à tout instant — et à la moitié du temps une seule des
        quatre est éligible.

        Le catalogue est relu périodiquement et non à chaque passage : les
        payouts bougent en minutes, pas en secondes, et le relire vingt fois
        par minute n'apprendrait rien.
        """
        maintenant = int(time.time())
        if (self._univers is None
                or maintenant - self._univers_ts >= RAFRAICHIR_UNIVERS_SEC):
            try:
                au_plafond_ = self.courtier.paires_au_plafond()
            except BotError as erreur:
                log.warning("Catalogue des paires indisponible : %s", erreur)
                return self._univers or []
            if self.mode_univers == UNIVERS_EPINGLEES:
                # L'intersection, et pas les épinglées telles quelles : une
                # paire épinglée qui ne paie pas le maximum reste écartée.
                self._univers = [p for p in self.paires if p in au_plafond_]
            else:
                self._univers = au_plafond_
            self._univers_ts = maintenant
            self.etat.univers_taille = len(self._univers)
        return self._univers

    def chercher_un_signal(self):
        """Le premier signal frais parmi les actifs au plafond, ou `None`.

        Les actifs sont parcourus en ROTATION et non toujours dans le même
        ordre : sans cela, les premiers de la liste monopoliseraient les
        signaux et les derniers ne seraient jamais examinés quand le plafond
        de paires par passage est atteint.
        """
        maintenant = int(time.time())
        candidats = self.univers()
        self._rafraichir_le_cache()
        # Les paires écartées sortent de la course. Filtrer ICI plutôt que
        # dans `univers()` garde le catalogue des payouts intact :
        # `univers_taille` doit continuer à dire combien d'actifs paient le
        # maximum, pas combien on accepte de jouer.
        #
        # Ce filtre ÉCONOMISE le balayage ; ce n'est pas lui qui garantit la
        # règle. La garantie est dans `tour()`, parce qu'une garde posée dans
        # la recherche de signal disparaît dès qu'on remplace cette recherche.
        if self.etat.quarantaine:
            candidats = [p for p in candidats
                         if p not in self.etat.quarantaine]
        # Pendant une session, l'actif du pas précédent ne peut pas porter le
        # pas suivant. L'écarter ICI laisse la recherche trouver un signal sur
        # un autre actif dans le même passage, au lieu de s'arrêter sur un
        # signal que `tour()` refuserait. La garantie, elle, reste dans `tour()`.
        session = self.etat.session
        if (session is not None and session.pas_joues
                and self.etat.dernier_trade is not None):
            candidats = [p for p in candidats
                         if p != self.etat.dernier_trade[0]]
        if not candidats:
            return None

        # ⚠ DEUX FILES, PARCE QUE LES DEUX N'ONT PAS LE MÊME PRIX.
        #
        # Les paires que nous collectons sont lues dans notre base : gratuites
        # et toujours fraîches, on les passe TOUTES à chaque passage. Les
        # autres coûtent 27 secondes de broker chacune, et seul ce qui tient
        # dans le budget est examiné — en rotation, pour que les dernières de
        # la liste ne soient pas condamnées à ne jamais être vues.
        gratuites = [p for p in candidats if p in self.paires_collectees]
        payantes = [p for p in candidats
                    if p not in self.paires_collectees]

        # ⚠ LES GRATUITES TOURNENT AUSSI, ET CE N'EST PAS UN DÉTAIL DE STYLE.
        #
        # Elles étaient parcourues toujours dans le même ordre — celui de
        # `PAIRES_FIXES` — et la recherche rend le PREMIER signal trouvé. La
        # première de la liste emportait donc chaque égalité, et la troisième
        # ne passait que si les deux d'avant n'avaient rien.
        #
        # Mesuré : GBPAUD_otc est troisième et n'a jamais reçu un seul ordre,
        # alors qu'elle est au plafond 35 % du temps. Pire, la première était
        # EURUSD_otc — la plus prolixe (22,3 signaux/jour) ET la moins précise
        # des quatre (44,1 %, sous le seuil de rentabilité). L'ordre fixe
        # maximisait donc la part de la pire paire.
        #
        # Une rotation ne crée aucun signal : elle répartit ceux qui existent.
        # C'est sans effet sur le débit, et c'est tout l'effet sur la précision.
        if gratuites:
            depart = self._rotation_gratuites % len(gratuites)
            gratuites = gratuites[depart:] + gratuites[:depart]
            self._rotation_gratuites += 1
        if payantes:
            depart = self._rotation % len(payantes)
            payantes = payantes[depart:] + payantes[:depart]
        self.etat.paires_gratuites = len(gratuites)

        examinees = 0
        sans_historique = 0
        hors_calibration = 0
        for paire in gratuites + payantes:
            payante = paire not in self.paires
            if payante and examinees >= PAIRES_MAX_PAR_PASSAGE:
                break
            try:
                bougies = self._bougies_de(paire)
            except BotError as erreur:
                log.debug("Historique indisponible sur %s : %s", paire, erreur)
                continue
            if payante:
                self._rotation += 1
                examinees += 1
                # Le broker n'aime pas qu'on enchaîne les changements
                # d'actif : huit abonnements simultanés lui avaient fait
                # fermer le socket. Une paire lue dans notre base ne lui
                # demande rien, et n'a donc rien à attendre.
                time.sleep(DELAI_ENTRE_ACTIFS_SEC)
            if not self._dans_sa_plage_de_calibration(paire, bougies):
                hors_calibration += 1
                continue
            if len(bougies) < 2 * self.strategie.p.fenetre_pique + 2:
                # Trop peu d'historique pour même chercher une zone. C'est le
                # cas normal d'une paire fraîchement ajoutée à la collecte,
                # et il dure des heures : il doit se compter, pas se taire.
                sans_historique += 1
                continue
            derniere = bougies[-1]
            if derniere.ts_sec <= self.etat.derniere_bougie.get(paire, 0):
                continue          # déjà évaluée
            self.etat.derniere_bougie[paire] = derniere.ts_sec
            self.etat.bougies_evaluees += 1
            self.etat.derniere_evaluation_ts = maintenant
            # La bougie close à ts_sec couvre [ts_sec, ts_sec+60[. Le signal
            # est donc daté de sa FIN, et c'est de là qu'on compte la
            # fraîcheur — pas de son début.
            #
            # ⚠ Une bougie jetée ICI n'a JAMAIS vu la stratégie, alors
            # qu'elle vient d'être comptée « évaluée » juste au-dessus. Le
            # compteur annonçait donc 810 quand la stratégie en avait vu une
            # poignée — et l'on cherchait le défaut dans la stratégie.
            if maintenant - (derniere.ts_sec + 60) > FRAICHEUR_MAX_SEC:
                self.etat.bougies_perimees += 1
                continue
            # Après le filtre de fraîcheur : une bougie vieille de deux jours
            # parce que la collecte est arrêtée ne dit rien de la vitesse de
            # la course, et noyait la mesure (« 174 540 s »).
            self.delais_evaluation.append(
                time.time() - (derniere.ts_sec + 60))
            vue = SequenceMarketView(paire, bougies)
            signal = self.strategie.on_bar(vue)
            if signal is None:
                continue
            # Compté AVANT la revérification du payout : sans cela, un signal
            # écarté pour cause de payout retombé est indiscernable d'un
            # signal jamais produit, et les deux appellent des gestes opposés.
            self.etat.signaux_bruts += 1
            # Le payout est revérifié À L'INSTANT DU SIGNAL. Le catalogue peut
            # dater de quelques minutes, et entrer sur un payout périmé est
            # exactement ce que le filtre existe pour empêcher.
            if not self._payout_au_maximum(paire):
                continue
            self.etat.signaux_trouves += 1
            self.etat.paires_sans_historique = sans_historique
            self.etat.paires_hors_calibration = hors_calibration
            self._fin_bougie_du_signal = derniere.ts_sec + 60
            return signal
        self.etat.paires_sans_historique = sans_historique
        self.etat.paires_hors_calibration = hors_calibration
        return None

    def _payout_au_maximum(self, paire: str) -> bool:
        """N'entrer QUE lorsque le broker paie son maximum.

        ⚠ « Payout 92 % » se lit sur le FLUX à 84, pas à 92. Le broker
        applique `min(flux + 8, 92)` — mesuré sur 26 ordres réels et confirmé
        par l'affichage de la plateforme. Filtrer sur `flux >= 92` écarterait
        42 % d'occasions qui paient exactement la même chose.

        Une indisponibilité du payout vaut REFUS. Entrer sans savoir ce qu'on
        sera payé est précisément ce que ce filtre existe pour empêcher.
        """
        try:
            flux = self.courtier.payout(paire)
        except BotError as erreur:
            log.warning("Payout indisponible sur %s : %s", paire, erreur)
            self.etat.payouts_vus[paire] = -1
            return False
        self.etat.payouts_vus[paire] = int(flux)
        if au_plafond(flux):
            return True
        self.etat.paires_ecartees_payout += 1
        log.info("Signal écarté sur %s : payout %d %% (appliqué %d %%), "
                 "le maximum de %d %% demande un flux >= %d %%.",
                 paire, flux, min(flux + 8, PLAFOND_PCT), PLAFOND_PCT,
                 PLAFOND_PCT - 8)
        return False

    # --- la session --------------------------------------------------------

    def rafraichir_le_solde(self) -> None:
        """Le solde du plan vient du JOURNAL DE NOS ORDRES.

        ⚠ Troisième source essayée, et celle-ci est la bonne. Les deux
        précédentes ont échoué pour des raisons opposées :

        - un livre tenu EN MÉMOIRE divergeait du réel dès qu'un ordre se
          perdait — cinq gagnants chez le broker, un solde figé à 250 $ ;
        - le SOLDE BRUT DU BROKER, lui, ne divergeait jamais du compte… mais
          absorbait tout ce qui n'était pas nous. Un trade passé à la main,
          un rechargement, et le plan affichait 241,69 $ quand le compte
          montrait 251,40 $.

        Le journal n'a aucun de ces défauts : il est persisté en base (donc
        il survit aux redémarrages) et il ne contient QUE nos ordres (donc
        rien d'extérieur n'y entre).

            solde = capital_initial + somme des profits journalisés

        Le solde du broker reste lu, mais pour être AFFICHÉ à côté : un écart
        entre les deux signale une activité manuelle sur le compte, et il vaut
        mieux la voir que l'absorber.
        """
        # Une seule ligne demandée à la base, et non tout le journal : relire
        # et décoder chaque ordre (réponse brute du broker comprise) à chaque
        # passage coûtait de plus en plus cher à mesure que la course avance.
        # La règle de calcul est dans `JournalExecution.profits_du_plan`.
        try:
            profits = self.journal.profits_du_plan()
        except Exception as erreur:              # noqa: BLE001
            log.warning("Journal illisible : %s", erreur)
            return
        solde = self.etat.plan.capital_initial + profits
        self.etat.solde = solde
        if self.etat.journee is not None:
            self.etat.journee.solde = solde
        try:
            self.etat.solde_broker = self.courtier.solde()
        except BotError:
            pass
        if self.etat.solde_broker_ancre is None:
            self.etat.solde_broker_ancre = self.etat.solde_broker
            self._sauvegarder()

    def _echelle(self) -> Echelle:
        """L'échelle du moment, dimensionnée sur le SOLDE COURANT.

        Sur le solde et non sur le capital initial : c'est ce qui fait qu'une
        perte réduit les mises suivantes au lieu de les laisser calibrées sur
        un capital qu'on n'a plus.
        """
        gain = self.etat.plan.gain_par_session_pct / 100 * self.etat.solde
        return Echelle(payout_pct=92, gain_vise=gain)

    def _reancrer(self) -> None:
        """Deux sessions perdues d'affilée : on repart du jour le plus proche.

        On ne rattrape pas, on se réancre. Sans cela, un compte tombé au
        niveau du jour 7 continue de viser les gains du jour 12 : les mises
        restent calibrées sur un capital qu'on n'a plus.
        """
        nouveau = jour_le_plus_proche(self.etat.plan, self.etat.solde)
        log.warning(
            "Deux sessions perdues d'affilée. Réancrage : jour %d -> jour %d "
            "(solde %.2f $).", self.etat.jour, nouveau, self.etat.solde)
        self.etat.reancrages.append(
            (self.etat.jour, nouveau, self.etat.solde))
        self.etat.jour = max(1, nouveau)
        self.etat.sessions_perdues_daffilee = 0
        self._prevenir(
            "⚠️ <b>Réancrage</b> — deux sessions perdues d'affilée.\n"
            f"Jour {self.etat.reancrages[-1][0]} → jour {nouveau}, "
            f"solde {self.etat.solde:.2f} $.\n"
            "On ne court pas après le plan : les mises repartent du niveau "
            "qu'on a vraiment.")

    def jouer_un_pas(self, signal) -> None:
        """Place l'ordre du pas courant et enregistre son dénouement.

        ⚠ UN SEUL ORDRE EN VOL À LA FOIS, et la garde est explicite.

        Le plan est SÉQUENTIEL : une session descend son échelle un pas après
        l'autre, et chaque pas attend de connaître son sort avant que le
        suivant soit décidé. Deux ordres ouverts en même temps voudraient dire
        que le pas 2 a été misé sans savoir si le pas 1 était perdu — la
        martingale n'aurait plus de sens, et l'exposition d'une session ne
        serait plus celle qu'on a calculée.

        Le blocage de `denouer` suffit en théorie. La garde existe parce que
        « en théorie » ne vaut rien ici : un thread relancé, une reprise
        concurrente, et deux ordres partent. Elle rend le cas impossible au
        lieu de le rendre improbable.
        """
        if self.etat.trade_en_cours is not None:
            paire, sens, mise, expire = self.etat.trade_en_cours
            log.error(
                "Ordre déjà en vol (%s %s %.2f $, expire dans %d s) : on ne "
                "superpose pas. Le plan est séquentiel.",
                paire, sens, mise, max(0, expire - int(time.time())))
            return
        if self.etat.session is None:
            self.etat.session = Session(echelle=self._echelle())
            self.etat.solde_ouverture_session = self.etat.solde
        session = self.etat.session
        mise = session.mise_courante()

        # Une paire qu'on n'a pas pu souscrire au démarrage était peut-être
        # simplement hors séance. Elle a pu ouvrir depuis : on réessaie ici,
        # une fois, plutôt que de la perdre pour toute la durée de la course.
        if signal.pair in self._non_souscrites:
            self._suivre_au_mieux(signal.pair)

        sens = "call" if signal.direction is Direction.CALL else "put"
        entree = int(time.time())
        self.etat.trade_en_cours = (
            signal.pair, sens, mise, entree + self.strategie.p.expiry_sec)
        try:
            execution = self.courtier.placer(
                signal.pair, sens, self.strategie.p.expiry_sec, mise=mise)
        except BaseException:
            self.etat.trade_en_cours = None
            raise
        if self._fin_bougie_du_signal is not None:
            self.delais_clic.append(
                execution.clic_ts_ms / 1000 - self._fin_bougie_du_signal)
            self._fin_bougie_du_signal = None
        if not execution.accepte:
            log.warning("Ordre refusé (%s) : le pas n'est pas joué.",
                        execution.refus)
            self.etat.trade_en_cours = None
            self.journal.ecrire(execution)
            self._noter_un_refus(signal.pair, execution.refus)
            return
        # ⚠ ÉCRIRE MAINTENANT, avant les quinze minutes d'attente.
        #
        # L'ordre est PARTI. Entre son départ et son dénouement il s'écoule
        # un quart d'heure, et tout peut arriver : un redéploiement, une mise
        # en veille, une coupure. La première version écrivait APRÈS le
        # dénouement — cinq ordres exécutés chez le broker, zéro dans nos
        # livres, et un solde de plan resté à 250 $ pendant que le compte
        # réel bougeait.
        self.journal.ecrire(execution)
        # ⚠ DATÉ DE L'ENTRÉE, PAS DU DÉNOUEMENT.
        #
        # La règle d'indépendance (#68) veut le pas suivant « au moins quinze
        # minutes plus tard » que celui-ci : c'est ainsi qu'elle a été rejouée
        # et pré-inscrite, et avec une échéance de 900 s cela tombe pile au
        # dénouement. Posé au RETOUR de `jouer_un_pas`, donc après les quinze
        # minutes de l'option, il imposait trente minutes entre deux entrées :
        # une règle deux fois plus stricte que celle qu'on mesure, qui coûtait
        # un quart d'heure à chaque pas de martingale.
        #
        # Seul un ordre ACCEPTÉ compte : un refus n'a engagé aucun pari.
        # Persisté tout de suite avec le reste, pour qu'un redémarrage pendant
        # l'option ne rende pas le pas suivant éligible trop tôt.
        self.etat.dernier_trade = (signal.pair, entree)
        # Accepté : la paire a prouvé qu'elle marche, son ardoise est nette.
        self.etat.refus_daffilee.pop(signal.pair, None)
        self.etat.dernier_refus_ts.pop(signal.pair, None)
        self._sauvegarder()
        execution = self.courtier.denouer(execution)
        self.journal.mettre_a_jour(execution)

        self.etat.trade_en_cours = None
        if execution.resultat not in ("win", "loose", "draw"):
            log.error("Dénouement inconnu (%s) : mise %.2f $ notée comme "
                      "engagée, session INTERROMPUE plutôt que comptée au "
                      "hasard.", execution.resultat, mise)
            # La mise est PARTIE. Interrompre sans la noter la ferait
            # disparaître du solde : l'argent serait sorti du compte sans
            # laisser de trace dans le plan.
            session.engager_sans_resoudre(mise)
            session.interrompre()
            self._cloturer_session()
            return

        gagne = execution.resultat == "win"
        pas = session.pas_joues + 1
        self._prevenir(
            f"{'✅' if gagne else '❌'} <b>{signal.pair}</b> "
            f"{sens.upper()} {mise:.2f} $\n"
            f"{datetime.now(timezone.utc):%H:%M:%S} UTC · "
            f"{'pas ' + str(pas) + '/3 (MARTINGALE)' if pas > 1 else 'pas 1 (entrée)'}\n"
            f"résultat <b>{'WIN' if gagne else 'LOSS'}</b> "
            f"{execution.profit or 0:+.2f} $ · payout "
            f"{execution.payout_broker_pct or 0:.0f} %")
        etat = session.enregistrer(gagne)
        log.info("pas %d/%d  %s %s  mise %.2f $  -> %s",
                 session.pas_joues, session.echelle.pas_max, signal.pair,
                 sens, mise, execution.resultat)
        if etat.terminee:
            self._cloturer_session()

    def _cloturer_session(self) -> None:
        session = self.etat.session
        if session is None:
            return
        # ⚠ LE SOLDE VIENT DU JOURNAL, PAS DE CE COMPTEUR.
        #
        # `Journee.enregistrer` fait deux choses : il avance les compteurs, et
        # il déplace son propre solde du montant qu'on lui passe. Le second
        # geste est de trop ici — le solde reflète DÉJÀ chaque pas, puisque
        # chaque pas est journalisé. Lui passer le montant de la session le
        # comptait une seconde fois : une session perdue creusait le résultat
        # du jour de 6 % au lieu de 4,7 %, et la garde de perte journalière se
        # déclenchait à tort dès la première.
        #
        # On lui passe donc zéro — les COMPTEURS viennent de nous, le SOLDE de
        # la source — puis on lui réimpose la vérité et l'on recalcule sa
        # garde dessus.
        ouverture = self.etat.solde_ouverture_session
        self.rafraichir_le_solde()
        montant = (self.etat.solde - ouverture if ouverture is not None
                   else session.montant)
        # ⚠ UNE SESSION INTERROMPUE N'EST PAS UNE SESSION JOUÉE.
        #
        # Elle l'était, et cela coûtait deux fois. `Journee.enregistrer`
        # avance `sessions_jouees` — donc une session abandonnée après un seul
        # pas brûlait un créneau sur les dix-huit — ET incrémente
        # `sessions_perdues_daffilee` quand elle n'est pas gagnée, donc deux
        # interruptions auraient déclenché un réancrage.
        #
        # Interrompue veut dire NI gagnée NI perdue : le pas 1 a échoué, aucun
        # pas indépendant n'est venu dans les deux heures, et la session a été
        # soldée sans avoir déroulé son échelle. La compter comme jouée est
        # aussi faux que la compter comme perdue.
        #
        # Elle était comptée pour une raison qui n'existe plus : le solde était
        # alors tenu dans un livre à part, et une session dont les mises
        # disparaissaient des comptes aurait fait croire à un solde qu'on n'a
        # pas. Le solde vient maintenant du JOURNAL DES ORDRES — l'argent est
        # compté qu'on enregistre la session ou non.
        #
        # Repéré en production : sept sessions terminées chez le broker, huit
        # annoncées par le plan. La huitième avait perdu son pas 1 à 08:31 et
        # n'avait jamais trouvé de pas 2 quatorze heures plus tard.
        # Le compteur d'interruptions est tenu par `_interrompre_la_session`,
        # qui est le seul endroit d'où elles viennent. L'incrémenter ici aussi
        # les compterait deux fois.
        if session.etat is not EtatSession.INTERROMPUE:
            self.etat.journee.enregistrer(
                session.etat is EtatSession.GAGNEE, 0.0)
        self.etat.journee.solde = self.etat.solde
        self.etat.journee.arret = self.etat.journee.peut_ouvrir_une_session()
        self.etat.solde_ouverture_session = None
        if session.etat is EtatSession.PERDUE:
            self.etat.sessions_perdues_daffilee += 1
            log.warning(
                "%s", session.message_protection(
                    self.etat.solde + session.engage,
                    self.etat.plan.pas_avant_liquidation()))
            if self.etat.sessions_perdues_daffilee >= 2:
                self._reancrer()
        elif session.etat is EtatSession.GAGNEE:
            self.etat.sessions_perdues_daffilee = 0
        self.etat.session = None
        log.info("session %s  montant %+.2f $  solde %.2f $",
                 session.etat, montant, self.etat.solde)
        self._prevenir(
            f"{'🟢' if session.etat is EtatSession.GAGNEE else '🔴'} "
            f"<b>Session {session.etat}</b> en {session.pas_joues} pas\n"
            f"{montant:+.2f} $  →  solde <b>{self.etat.solde:.2f} $</b>\n"
            f"jour {self.etat.jour}/{self.etat.plan.jours}, session "
            f"{self.etat.journee.sessions_jouees}/"
            f"{self.etat.plan.sessions_par_jour} de la journée "
            f"({self.etat.journee.resultat_pct:+.2f} %)")

    # --- la boucle ---------------------------------------------------------

    def peut_ouvrir(self) -> Arret | None:
        return self.etat.journee.peut_ouvrir_une_session()

    def tour(self) -> bool:
        """Un passage : cherche un signal, joue un pas si c'est possible.

        Rend `True` si quelque chose a été joué. Une session ENTAMÉE a la
        priorité sur un nouveau signal : la martingale doit finir sa descente
        avant qu'on en ouvre une autre, sinon deux échelles courent en même
        temps et l'exposition n'est plus celle qu'on a calculée.
        """
        self.rafraichir_le_solde()
        self._purger_la_quarantaine()
        if self.etat.session is None:
            arret = self.peut_ouvrir()
            if arret is not None:
                return False
        elif self._session_a_trop_attendu():
            self._interrompre_la_session()
            return True
        signal = self.chercher_un_signal()
        if signal is None:
            return False
        # ⚠ LA GARANTIE EST ICI, et pas dans `chercher_un_signal`.
        #
        # Le filtre y est aussi, pour économiser le balayage. Mais une règle
        # posée dans la recherche de signal disparaît dès que quelqu'un
        # remplace cette recherche — ce que font les tests, et ce que ferait
        # n'importe quelle autre source de signaux. La règle qui empêche 34
        # ordres refusés d'affilée ne peut pas dépendre de ça.
        if signal.pair in self.etat.quarantaine:
            return False
        if not self._pas_independant(signal):
            self.etat.pas_sautes_independance += 1
            return False
        self.jouer_un_pas(signal)
        return True

    def _sauvegarder(self) -> None:
        """Fige l'état tout de suite. Ne doit jamais faire tomber la course."""
        if self._sauver is None:
            return
        try:
            self._sauver()
        except Exception:                        # noqa: BLE001
            log.exception("État non sauvegardé")

    def _prevenir(self, message: str) -> None:
        """Alerter ne doit jamais pouvoir faire tomber la course."""
        if self._alerter is None:
            return
        try:
            self._alerter(message)
        except Exception:                        # noqa: BLE001
            log.debug("Alerte de course non envoyée", exc_info=True)

    def _pas_independant(self, signal) -> bool:
        """Un pas de martingale se joue-t-il sur un pari VRAIMENT nouveau ?

        Le premier pas d'une session n'a rien à respecter : il ouvre le pari.
        Les suivants, si — sinon ils rejouent celui qui vient d'être perdu.
        """
        session = self.etat.session
        if session is None or session.pas_joues == 0:
            return True
        dernier = self.etat.dernier_trade
        if dernier is None:
            return True
        meme_actif = dernier[0] == signal.pair
        trop_tot = int(time.time()) - dernier[1] < DELAI_INDEPENDANCE_SEC
        if meme_actif or trop_tot:
            log.info("Pas %d sauté sur %s : %s. La martingale a besoin d'un "
                     "pari indépendant, pas du même une minute plus tard.",
                     session.pas_joues + 1, signal.pair,
                     "même actif" if meme_actif else "trop tôt")
            return False
        return True

    def _session_a_trop_attendu(self) -> bool:
        session = self.etat.session
        dernier = self.etat.dernier_trade
        if session is None or not session.pas_joues or dernier is None:
            return False
        return int(time.time()) - dernier[1] > ATTENTE_MAX_PAS_SEC

    def _interrompre_la_session(self) -> None:
        """Aucun pas éligible depuis trop longtemps : on solde et on repart.

        Interrompue et non perdue : elle n'a coûté que les pas déjà joués.
        Mais elle EST comptée — une session abandonnée dont les mises
        disparaîtraient des comptes ferait croire à un solde qu'on n'a pas.
        """
        session = self.etat.session
        log.warning(
            "Session interrompue : aucun pas indépendant depuis %.0f min. "
            "%d pas joué(s), %.2f $ engagé(s).",
            ATTENTE_MAX_PAS_SEC / 60, session.pas_joues, session.engage)
        session.interrompre()
        self.etat.sessions_interrompues += 1
        self._cloturer_session()

    def passer_le_jour_si_besoin(self, jour_utc_courant: int = 0) -> bool:
        """Avance d'une journée de plan quand celle en cours est TERMINÉE.

        ⚠ UN JOUR DU PLAN NE DURE PAS VINGT-QUATRE HEURES. Il dure
        `sessions_par_jour` sessions — six — et l'on en enchaîne trois par
        journée calendaire pour finir les trente jours en dix. C'est toute la
        raison du test compressé.

        Le passage était branché sur minuit UTC. Un jour du plan durait donc
        une journée entière quoi qu'il arrive : dix-huit sessions comptées dans
        le même jour, et « Jour 2/30 » annoncé à 258,67 $ quand le jour 1
        demandait 258,70. La détection arrivait en retard parce qu'elle
        attendait l'horloge au lieu de compter les sessions.

        `jour_utc_courant` n'est plus lu. Il reste dans la signature parce que
        l'appelant le persiste, et le retirer demanderait de toucher à la
        table au même moment que la logique.

        Deux défauts ont été corrigés ici l'un après l'autre, et le second est
        né de ma correction du premier.

        D'ABORD, LE PASSAGE N'EXISTAIT PAS. `nouveau_jour()` était écrite,
        testée, et appelée par personne ; la colonne `jour_utc` était écrite à
        chaque pas et relue par personne. Le plan restait sur « jour 1/30 »
        indéfiniment, `sessions_jouees` ne repartait jamais de zéro — donc le
        quota s'appliquait à TOUTE la course — et la garde de perte
        journalière se calculait sur le cumul.

        ENSUITE, JE L'AI BRANCHÉ SUR LA MAUVAISE HORLOGE. Sur minuit UTC, donc
        sur le calendrier, alors qu'un jour du plan se compte en SESSIONS. Le
        symptôme était le même à l'écran — un numéro de jour qui n'avance
        pas quand il faut — mais la cause avait changé de place.

        Rend `True` si la journée a tourné, pour que l'appelant persiste.
        """
        self.jour_utc_courant = jour_utc_courant or self.jour_utc_courant
        if self.etat.session is not None:
            # Une session en cours finit d'abord. La couper laisserait des
            # mises engagées dans une journée qui n'existe plus.
            return False
        if self.etat.jour >= self.etat.plan.jours:
            return False                 # le plan est fini, on ne boucle pas
        arret = self.etat.journee.peut_ouvrir_une_session()
        if arret is None:
            return False                 # la journée a encore des sessions

        # ⚠ SIX SESSIONS SONT LE CHEMIN NOMINAL, PAS LA CONDITION.
        #
        # Un jour du plan est fini quand sa CIBLE est atteinte : +3,50 %, soit
        # ce que six sessions gagnées rapportent. Si certaines sont perdues, six
        # sessions n'y suffisent pas — et clore le jour là-dessus le laisserait
        # en retard pour toujours, chaque jour suivant visant une cible calculée
        # sur un solde qui n'a jamais atteint la précédente.
        #
        # Épuiser les six sessions ne clôt donc PAS le jour : on rouvre le
        # compteur en gardant la même ancre, et l'on continue à viser la même
        # cible. C'est le « combien il manque pour compléter le jour ».
        #
        # Les deux gardes de PERTE, elles, clôturent pour de bon. Elles existent
        # précisément pour arrêter d'insister sur une mauvaise journée : les
        # contourner en rouvrant le compteur les réduirait à de la décoration.
        if arret is Arret.SESSIONS_EPUISEES:
            manque = (self.etat.journee.solde_ouverture
                      * (1 + (self.etat.plan.objectif_journalier_pct or 0) / 100)
                      - self.etat.solde)
            self.etat.journee.sessions_jouees = 0
            self.etat.journee.sessions_perdues_daffilee = 0
            self.etat.journee.arret = None
            log.info(
                "Jour %d : %d sessions jouées sans atteindre la cible, il "
                "manque %.2f $. On continue le MÊME jour plutôt que de le "
                "clore en retard.",
                self.etat.jour, self.etat.plan.sessions_par_jour, manque)
            return True

        ancien = self.etat.jour
        nouveau_jour(self.etat)
        log.info("Journée de plan %d -> %d (%s). Solde %.2f $.",
                 ancien, self.etat.jour, arret, self.etat.solde)
        cible = self.etat.solde * (
            1 + (self.etat.plan.objectif_journalier_pct or 0) / 100)
        self._prevenir(
            f"📅 <b>Jour {ancien}/{self.etat.plan.jours} terminé</b> — {arret}\n"
            f"solde <b>{self.etat.solde:.2f} $</b>\n"
            f"jour {self.etat.jour} : cible <b>{cible:.2f} $</b> en "
            f"{self.etat.plan.sessions_par_jour} sessions")
        return True

    def _purger_la_quarantaine(self) -> None:
        """Lève les peines arrivées à terme. Appelée à chaque passage."""
        maintenant = int(time.time())
        for paire in [p for p, t in self.etat.quarantaine.items()
                      if t <= maintenant]:
            del self.etat.quarantaine[paire]
            self.etat.refus_daffilee.pop(paire, None)
            self.etat.dernier_refus_ts.pop(paire, None)
            # `quarantaines_subies` n'est PAS effacé : c'est lui qui porte
            # l'escalade. Le remettre à zéro à chaque levée ferait repartir la
            # peine à une heure, indéfiniment.
            log.info("Quarantaine levée sur %s : on réessaie (récidives : %d).",
                     paire, self.etat.quarantaines_subies.get(paire, 0))

    def _noter_un_refus(self, paire: str, motif: str | None) -> None:
        """Compte les refus d'affilée et met la paire de côté au troisième.

        ⚠ Sans ce compteur, un refus ne laissait AUCUNE trace dans l'état : le
        passage suivant retrouvait le même signal sur la même paire, retentait,
        et échouait pareil. Mesuré en production : 34 ordres sur BTCUSD_otc en
        34 minutes, tous refusés, pendant que la course ne faisait rien
        d'autre — c'était la seule paire au plafond à ce moment-là.

        La boucle ne levait aucune erreur et ne consommait aucune mise. Elle
        ne coûtait que du temps, ce qui est exactement ce qui manque quand on
        vise dix-huit sessions par jour.
        """
        maintenant = int(time.time())
        precedent = self.etat.dernier_refus_ts.get(paire, 0)
        # Une série interrompue par une longue accalmie n'est pas une série.
        # On repart de un plutôt que d'additionner des incidents sans rapport.
        n = 1 if maintenant - precedent > QUARANTAINE_SEC else \
            self.etat.refus_daffilee.get(paire, 0) + 1
        self.etat.refus_daffilee[paire] = n
        self.etat.dernier_refus_ts[paire] = maintenant
        if n < REFUS_AVANT_QUARANTAINE:
            return
        # ⚠ LA PEINE DOUBLE À CHAQUE RÉCIDIVE, et la raison est mesurée.
        #
        # À une heure fixe, on réessayait indéfiniment toutes les heures : le
        # journal de production montre trois quarantaines de suite sur
        # BTCUSD_otc, à trois payouts différents. Une cause que le temps ne
        # faisait pas passer.
        #
        # Elle est maintenant connue, et elle est PERMANENTE : l'échéance de
        # 900 s n'existe pas sur cet actif. Mesuré en passant de vrais ordres
        # sur le compte démo — BTCUSD_otc refuse 900 s à 1,66 $, à 5 $ et à
        # 20 $, puis accepte 60 s et 300 s aux mêmes mises, quand EURUSD_otc
        # accepte 900 s. Ce n'était donc ni la mise ni le payout : c'est le
        # contrat de quinze minutes qui n'est pas proposé sur une crypto.
        #
        # On ne bannit pas pour autant. Le broker ne donne pas son motif, et
        # une cause qui disparaît — actif suspendu, incident passager — mérite
        # un réessai. La peine double simplement, jusqu'à un jour.
        rang = self.etat.quarantaines_subies.get(paire, 0)
        duree = min(QUARANTAINE_MAX_SEC, QUARANTAINE_SEC * (2 ** rang))
        self.etat.quarantaines_subies[paire] = rang + 1
        self.etat.quarantaine[paire] = int(time.time()) + duree
        log.warning(
            "%s écartée pour %d min après %d refus d'affilée (%s). "
            "Quarantaine n°%d sur cette paire : la peine double à chaque "
            "récidive plutôt que de réessayer toutes les heures.",
            paire, duree // 60, n, motif, rang + 1)
        self._prevenir(
            f"⏸ <b>{paire}</b> écartée {duree // 60} min "
            f"(récidive n°{rang + 1})\n"
            f"{n} ordres refusés d'affilée par le broker "
            f"({motif or 'sans motif'})")
        self._sauvegarder()

    def _dans_sa_plage_de_calibration(self, paire, bougies) -> bool:
        """La tolérance de la stratégie est-elle à l'échelle de cet actif ?

        Une condition de SÉCURITÉ, pas un filtre de stratégie : elle refuse de
        jouer là où le réglage ne veut plus rien dire, au lieu de produire des
        signaux qu'on prendrait pour des signaux.

        Voir `RAPPORT_TOLERANCE_MIN` / `MAX` pour les chiffres et leur origine.
        """
        recentes = bougies[-FENETRE_AMPLITUDE:]
        if len(recentes) < 10:
            return True             # trop peu pour juger : on ne tranche pas
        amplitudes = sorted(
            100.0 * (b.high - b.low) / b.close
            for b in recentes if b.close)
        if not amplitudes:
            return True
        mediane = amplitudes[len(amplitudes) // 2]
        if mediane <= 0:
            # Un actif dont la bougie médiane n'a AUCUNE amplitude n'est pas un
            # actif calme, c'est un actif dont on ne reçoit pas le prix.
            log.debug("%s écartée : amplitude médiane nulle.", paire)
            return False
        rapport = self.strategie.p.tolerance_pct / mediane
        if RAPPORT_TOLERANCE_MIN <= rapport <= RAPPORT_TOLERANCE_MAX:
            return True
        log.info(
            "%s écartée : tolérance/amplitude = %.2f, hors de la plage "
            "[%.2f, %.2f] où la stratégie a été calibrée (bougie médiane "
            "%.4f %%). Elle y produirait des signaux qui n'en sont pas.",
            paire, rapport, RAPPORT_TOLERANCE_MIN, RAPPORT_TOLERANCE_MAX,
            mediane)
        return False

    def _payouts_lisibles(self) -> str:
        return " ".join(
            f"{p.replace('_otc', '')}:{v if v >= 0 else '?'}"
            for p, v in sorted(self.etat.payouts_vus.items())) or "—"

    def resume(self) -> str:
        j = self.etat.journee
        e = self.etat
        if e.trade_en_cours is not None:
            paire, sens, mise, expire = e.trade_en_cours
            reste = max(0, expire - int(time.time()))
            pas = (e.session.pas_joues + 1) if e.session else 1
            return (f"ORDRE EN COURS — {paire} {sens.upper()} {mise:.2f} $ "
                    f"(pas {pas}/3), dénouement dans {reste} s | "
                    f"jour {e.jour}/{e.plan.jours} solde {e.solde:.2f} $ "
                    f"sessions {j.sessions_jouees}/{e.plan.sessions_par_jour}")
        if e.bougies_evaluees == 0:
            # Deux silences très différents, et il faut les distinguer : une
            # course qui n'analyse rien parce que rien ne paie 92 % attend ;
            # une course qui ne lit plus la base est en panne.
            if e.univers_taille == 0:
                return ("connectée, AUCUN actif au plafond pour l'instant — "
                        "le catalogue des payouts ne rend rien d'éligible")
            if e.paires_sans_historique:
                # Le cas normal après une coupure de collecte : la fenêtre ne
                # contient que les minutes fraîches, trop peu pour une zone.
                return (f"connectée, {e.univers_taille} actif(s) au plafond, "
                        f"{e.paires_sans_historique} en attente de bougies "
                        f"fraîches — il en faut "
                        f"{2 * self.strategie.p.fenetre_pique + 2} minutes "
                        f"avant la première évaluation")
            return (f"connectée, {e.univers_taille} actif(s) au plafond mais "
                    f"aucune bougie évaluée — c'est l'HISTORIQUE demandé au "
                    f"broker qui est en cause, pas la base")
        age = int(time.time()) - e.derniere_evaluation_ts
        base = (f"jour {e.jour}/{e.plan.jours}  solde {e.solde:.2f} $  "
                f"sessions {j.sessions_jouees}/{e.plan.sessions_par_jour}  "
                f"journée {j.resultat_pct:+.2f} %  "
                f"réancrages {len(e.reancrages)}")
        # Les compteurs d'activité viennent APRÈS le plan mais ils sont le
        # seul moyen de dire qu'une course sans ordre est vivante.
        depuis = (int(time.time()) - e.demarre_ts) // 60 if e.demarre_ts else 0
        # ⚠ Le débit mesuré : 248 signaux éligibles en 10,1 jours sur quatre
        # paires, soit un toutes les ~230 bougies évaluées. Sans ce repère,
        # « 8 bougies, 0 signal » ressemble à une panne alors que c'est
        # exactement ce qu'on attend au bout de trois minutes.
        vues = e.bougies_evaluees - e.bougies_perimees
        reste = max(0, 95 - vues % 95)
        attente = (f"~{reste} bougies avant le prochain signal attendu"
                   if e.signaux_trouves == 0 else "")
        broker = (f" (broker {e.solde_broker:.2f} $)"
                  if e.solde_broker is not None else "")
        ecartees = ""
        if e.quarantaine:
            reste = {p: max(0, (t - int(time.time())) // 60)
                     for p, t in sorted(e.quarantaine.items())}
            ecartees = (" | écartées : "
                        + ", ".join(f"{p} ({m} min)"
                                    for p, m in reste.items()))
        # Le DÉBIT RÉEL, maintenant que les compteurs survivent aux
        # redémarrages. Sans lui, « 6/18 » se lit comme un retard alors que le
        # plafond du marché est à 13,4 — et l'on cherche une panne qui n'existe
        # pas. Avec lui, on voit tout de suite si la course est sous son
        # plafond ou simplement dans une heure creuse.
        #
        # ⚠ LE NUMÉRATEUR ET LE DÉNOMINATEUR DOIVENT COUVRIR LA MÊME FENÊTRE.
        #
        # Ils ne le faisaient pas : `sessions_jouees` compte la JOURNÉE UTC en
        # cours, et l'on divisait par le temps écoulé depuis le DÉMARRAGE de la
        # course. Après un redéploiement en milieu de journée, six sessions de
        # la journée divisées par 1,2 h de course donnaient « 116,9 sessions /
        # jour » — un chiffre qui ne veut rien dire et qui, affiché à côté d'un
        # plafond de 13,4, ferait croire à un débit neuf fois supérieur au
        # maximum du marché.
        #
        # On divise donc par le temps écoulé DANS LA JOURNÉE UTC, qui est la
        # fenêtre que compte `sessions_jouees`.
        ecoule_h = (int(time.time()) % 86400) / 3600
        depuis_h = ((int(time.time()) - e.demarre_ts) / 3600
                    if e.demarre_ts else 0.0)
        debit = ""
        # Deux conditions, et les deux sont nécessaires : une heure de journée
        # écoulée pour que le taux ait un sens, et une heure de course pour ne
        # pas attribuer à la course ce qu'elle n'a pas eu le temps de faire.
        if ecoule_h >= 1 and depuis_h >= 1:
            par_jour = j.sessions_jouees / ecoule_h * 24
            pour = vues / e.signaux_bruts if e.signaux_bruts else 0
            debit = (f" | débit {par_jour:.1f} sessions/jour "
                     f"(mesuré {SESSIONS_PAR_JOUR_MESUREES} "
                     f"± {ECART_DEBIT_PAR_JOUR}), "
                     f"1 signal pour "
                     f"{(f'{pour:.0f}' if pour else '—')} bougies, "
                     f"{j.sessions_jouees} session(s) en {ecoule_h:.1f} h "
                     f"de journée, course en route depuis {depuis_h:.1f} h")
        return (f"{base}{broker} | en route depuis {depuis} min | "
                f"{e.univers_taille} actif(s) au plafond dont "
                f"{e.paires_gratuites} en base"
                f"{f' ({e.paires_sans_historique} sans historique suffisant)'
                   if e.paires_sans_historique else ''}"
                f"{f' ({e.paires_hors_calibration} hors calibration)'
                   if e.paires_hors_calibration else ''}, "
                f"{vues} bougies vues par la stratégie "
                f"({e.bougies_perimees} périmées sur {e.bougies_evaluees}), "
                f"{e.signaux_bruts} signal(aux) bruts dont "
                f"{e.signaux_trouves} retenu(s), {e.pas_sautes_independance} "
                f"pas sauté(s), {e.sessions_interrompues} interrompue(s), "
                f"lecture il y a {age} s{ecartees}{debit}"
                f"{self._reactivite()}"
                f"{' | ' + attente if attente else ''}")

    def _reactivite(self) -> str:
        """Délai médian et maximal entre la clôture d'une bougie et son
        évaluation, puis le clic. C'est la mesure de la vitesse de la course."""
        morceaux = []
        for nom, delais in (("évaluation", self.delais_evaluation),
                            ("clic", self.delais_clic)):
            if delais:
                tries = sorted(delais)
                morceaux.append(f"{nom} {tries[len(tries) // 2]:.1f} s "
                                f"(max {tries[-1]:.1f} s)")
        if not morceaux:
            return ""
        return " | après clôture : " + ", ".join(morceaux)


def nouveau_jour(etat: Etat) -> None:
    """Passe à la journée suivante : nouvelle `Journee`, mêmes soldes."""
    etat.jour += 1
    etat.ouvrir_la_journee()


def jour_utc(ts_sec: int | None = None) -> int:
    ts = int(time.time()) if ts_sec is None else ts_sec
    return int(datetime.fromtimestamp(ts, timezone.utc).timestamp() // 86400)


# --------------------------------------------------------------------------- #
# La persistance — sans elle, un redémarrage efface dix jours
# --------------------------------------------------------------------------- #

def sauver_etat(conn, campagne: str, etat: Etat, jour_utc_courant: int) -> None:
    """Écrit l'état complet de la course, session en cours comprise.

    ⚠ Appelée après CHAQUE pas, pas à la fin. Le processus peut mourir à
    n'importe quel moment — l'hébergeur redéploie, met en veille, redémarre —
    et un état sauvé « de temps en temps » rejouerait des ordres déjà passés
    ou en oublierait.

    La session en cours est incluse, et c'est le point délicat. Sans elle, un
    redémarrage au milieu d'une martingale repartirait au pas 1 : les mises
    déjà engagées auraient quitté le compte sans que le plan les connaisse.
    """
    session = etat.session
    conn.execute(
        """INSERT INTO plan_etat
               (campagne, maj_ts_sec, jour, solde, solde_ouverture,
                sessions_jouees, sessions_perdues_daffilee, jour_utc,
                reancrages, derniere_bougie, session_pas_joues,
                session_engagees, session_gain_vise,
                dernier_trade_pair, dernier_trade_ts_sec,
                solde_broker_ancre, compteurs, demarre_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(campagne) DO UPDATE SET
               maj_ts_sec = excluded.maj_ts_sec,
               jour = excluded.jour,
               solde = excluded.solde,
               solde_ouverture = excluded.solde_ouverture,
               sessions_jouees = excluded.sessions_jouees,
               sessions_perdues_daffilee = excluded.sessions_perdues_daffilee,
               jour_utc = excluded.jour_utc,
               reancrages = excluded.reancrages,
               derniere_bougie = excluded.derniere_bougie,
               session_pas_joues = excluded.session_pas_joues,
               session_engagees = excluded.session_engagees,
               session_gain_vise = excluded.session_gain_vise,
               dernier_trade_pair = excluded.dernier_trade_pair,
               dernier_trade_ts_sec = excluded.dernier_trade_ts_sec,
               solde_broker_ancre = excluded.solde_broker_ancre,
               compteurs = excluded.compteurs,
               demarre_ts = excluded.demarre_ts""",
        (campagne, int(time.time()), etat.jour, etat.solde,
         etat.journee.solde_ouverture, etat.journee.sessions_jouees,
         etat.sessions_perdues_daffilee, jour_utc_courant,
         json.dumps(etat.reancrages), json.dumps(etat.derniere_bougie),
         session.pas_joues if session else 0,
         json.dumps(session.engagees if session else []),
         session.echelle.gain_vise if session else 0.0,
         etat.dernier_trade[0] if etat.dernier_trade else None,
         etat.dernier_trade[1] if etat.dernier_trade else None,
         etat.solde_broker_ancre,
         json.dumps({**{n: getattr(etat, n) for n in Etat.COMPTEURS},
                     # Préfixées, pour que la relecture les distingue des
                     # compteurs entiers sans avoir à les énumérer deux fois.
                     "_quarantaine": etat.quarantaine,
                     "_refus": etat.refus_daffilee,
                     "_refus_ts": etat.dernier_refus_ts,
                     "_quarantaines": etat.quarantaines_subies}),
         etat.demarre_ts),
    )
    valider(conn)


def charger_etat(conn, campagne: str, plan: PlanCapital) -> tuple[Etat, int] | None:
    """Relit une course interrompue. `None` s'il n'y en a pas.

    Rend aussi le jour UTC de la dernière écriture : c'est lui qui dit si la
    journée doit repartir à zéro ou continuer. Le déduire de l'horloge seule
    ferait repartir une journée entamée avec ses compteurs remis à neuf.
    """
    ligne = conn.execute(
        """SELECT jour, solde, solde_ouverture, sessions_jouees,
                  sessions_perdues_daffilee, jour_utc, reancrages,
                  derniere_bougie, session_pas_joues, session_engagees,
                  session_gain_vise, dernier_trade_pair,
                  dernier_trade_ts_sec, solde_broker_ancre,
                  compteurs, demarre_ts
           FROM plan_etat WHERE campagne = ?""", (campagne,)).fetchone()
    if ligne is None:
        return None
    etat = Etat(plan=plan, solde=float(ligne[1]), jour=int(ligne[0]))
    etat.journee = Journee(plan=plan, solde=float(ligne[2]))
    etat.journee.solde = float(ligne[1])
    etat.journee.sessions_jouees = int(ligne[3])
    etat.sessions_perdues_daffilee = int(ligne[4])
    etat.reancrages = [tuple(x) for x in json.loads(ligne[6])]
    etat.derniere_bougie = {k: int(v) for k, v
                            in json.loads(ligne[7]).items()}
    if ligne[11] is not None and ligne[12] is not None:
        # Sans cette reprise, un redémarrage rendrait le pas suivant
        # immédiatement éligible et l'on rejouerait le pari corrélé que la
        # règle d'indépendance existe pour empêcher.
        etat.dernier_trade = (str(ligne[11]), int(ligne[12]))
    if ligne[13] is not None:
        etat.solde_broker_ancre = float(ligne[13])
    # Les compteurs d'activité. Absents d'un état écrit avant la migration
    # v9 : on repart de zéro plutôt que d'échouer — perdre une mesure est
    # moins grave que refuser de reprendre un plan en cours.
    sauve = json.loads(ligne[14] or "{}")
    for nom, valeur in sauve.items():
        if nom in Etat.COMPTEURS:
            setattr(etat, nom, int(valeur))
    # ⚠ La quarantaine DOIT survivre au redémarrage. Sans cela, un
    # redéploiement remettrait en route exactement la boucle qu'elle existe
    # pour arrêter — et les redéploiements sont fréquents.
    etat.quarantaine = {str(k): int(v)
                        for k, v in (sauve.get("_quarantaine") or {}).items()}
    etat.refus_daffilee = {str(k): int(v)
                           for k, v in (sauve.get("_refus") or {}).items()}
    etat.dernier_refus_ts = {
        str(k): int(v) for k, v in (sauve.get("_refus_ts") or {}).items()}
    # Le compte des récidives DOIT survivre : sans lui, chaque redéploiement
    # ramènerait la peine à une heure et l'escalade ne servirait à rien.
    etat.quarantaines_subies = {
        str(k): int(v) for k, v in (sauve.get("_quarantaines") or {}).items()}
    etat.demarre_ts = int(ligne[15] or 0)
    pas, engagees, gain = int(ligne[8]), json.loads(ligne[9]), float(ligne[10])
    if pas or engagees:
        # Une session était en cours. On la reconstruit telle quelle : même
        # échelle, mêmes mises déjà engagées, même profondeur atteinte.
        session = Session(echelle=Echelle(payout_pct=92, gain_vise=gain))
        session.pas_joues = pas
        session.engagees = [float(x) for x in engagees]
        etat.session = session
    return etat, int(ligne[5])


def _resoudre_les_ordres_en_vol(course, courtier, journal) -> None:
    """Rattrape les ordres partis juste avant un arrêt.

    Un ordre accepté puis laissé sans réponse s'est dénoué CHEZ LE BROKER
    pendant qu'on était mort. Le compte réel a bougé ; le plan l'ignore. Sans
    cette reprise, les deux divergent définitivement — et c'est exactement ce
    qui s'est produit : cinq ordres gagnants chez le broker, un solde de plan
    figé à 250 $.
    """
    en_vol = journal.en_vol()
    if not en_vol:
        return
    log.warning("%d ordre(s) parti(s) sans réponse : on va chercher leur "
                "sort chez le broker.", len(en_vol))
    for execution in en_vol:
        try:
            resolu = courtier.denouer(execution)
        except Exception as erreur:              # noqa: BLE001
            log.error("Sort de l'ordre %s introuvable : %s. Il reste marqué "
                      "en vol plutôt que deviné.", execution.order_id, erreur)
            continue
        journal.mettre_a_jour(resolu)
        log.info("Ordre %s retrouvé : %s (%.2f $).", resolu.order_id,
                 resolu.resultat, resolu.profit or 0.0)


def fabriquer_course(ssid: str, *, campagne: str, capital: float,
                     sessions_par_jour: int, jours: int,
                     paires: tuple[str, ...], chemin_lecture="lecture",
                     chemin_ecriture="ecriture",
                     mode_univers: str = UNIVERS_EPINGLEES, alerter=None,
                     paires_collectees: tuple[str, ...] | None = None):
    """Assemble une course prête à tourner, et reprend celle en cours s'il y en a.

    ⚠ `ssid` est PASSÉ et non résolu ici. Le résoudre demanderait d'importer
    `collect`, droit que `live` n'a pas et ne doit pas avoir : la couche qui
    décide et exécute n'a rien à faire dans celle qui collecte. C'est
    l'appelant — `hosting`, dont c'est le métier d'assembler — qui le fournit.

    Rend un objet à `tour()` / `resume()`, plus la fonction qui sauve son
    état. Le superviseur appelle les deux sans rien savoir du reste.
    """
    from pathlib import Path

    from maxprofit.execution.courtier import CourtierDemo
    from maxprofit.execution.garde import Plafonds
    from maxprofit.execution.journal import JournalExecution
    from maxprofit.plan import Echelle, Risque, solde_projete
    from maxprofit.store.db import open_read_only, open_read_write
    from maxprofit.store.market import MarketReader

    plan = PlanCapital.depuis_risque(
        capital_initial=capital, risque=Risque(1, 7), payout_pct=92,
        sessions_par_jour=sessions_par_jour, jours=jours,
        sessions_perdues_max=2)
    # ⚠ L'OBJECTIF DU JOUR EST CE QUE SES SESSIONS DOIVENT RAPPORTER.
    #
    # Il valait `None` — aucun arrêt au gain — donc une journée ne s'arrêtait
    # que sur SESSIONS_EPUISEES, et rien ne disait qu'elle avait ATTEINT sa
    # cible. Le plan annonçait « Jour 2/30 » avec 258,67 $ quand le jour 1
    # demandait 258,70 : trois centimes manquants, et personne pour le voir.
    #
    # Six sessions à 0,5834 % font 3,50 %. Le chiffre n'est donc pas un réglage
    # de plus : c'est la définition d'un jour du plan, déduite du nombre de
    # sessions qui le composent.
    plan = replace(
        plan,
        objectif_journalier_pct=(
            plan.sessions_par_jour * plan.gain_par_session_pct))
    # Le plafond est le 3e pas AU CAPITAL VISÉ, pas au capital initial.
    #
    # Les mises sont dimensionnées sur le solde COURANT : elles grandissent
    # avec lui. Un plafond calculé sur les 250 $ de départ aurait refusé chaque
    # ordre dès que le solde aurait dépassé ce niveau — silencieusement, en
    # abandonnant la course au bout de cinq refus. C'est exactement ce qui est
    # arrivé, à une nuance près : le plafond absolu de 10 $ a bloqué dès le
    # premier ordre.
    vise = solde_projete(plan, plan.jours)
    pire = Echelle(payout_pct=92,
                   gain_vise=vise * plan.gain_par_session_pct / 100).mises()[-1]
    plafond = round(pire * 1.1, 2)
    plafonds = Plafonds(mise=plafond, mise_max_absolue=plafond,
                        ordres_max=2000, duree_max_sec=11 * 86400)
    log.info("Plafond de mise : %.2f $ (3e pas au capital visé de %.2f $)",
             plafond, vise)

    lecteur = MarketReader(open_read_only(Path(chemin_lecture)))
    ecriture = open_read_write(Path(chemin_ecriture))
    journal = JournalExecution(ecriture, campagne=campagne)
    courtier = CourtierDemo(ssid, plafonds)
    courtier.connecter()
    # Une connexion réussie efface la dette d'attente du collecteur, qui lit
    # le même compteur : sinon il se tairait encore une demi-heure.
    from maxprofit.store import etat_broker
    etat_broker.noter_succes(ecriture)
    try:
        return _assembler(courtier, lecteur, ecriture, journal, plan, paires,
                          campagne, mode_univers, paires_collectees, alerter)
    except BaseException:
        # Connecté mais jamais rendu : personne d'autre ne fermerait ce client,
        # qui reconnecterait à vie à côté du suivant.
        courtier.fermer()
        raise


def _assembler(courtier, lecteur, ecriture, journal, plan, paires, campagne,
               mode_univers, paires_collectees, alerter):
    from maxprofit.strategies.zone_h1 import ZoneH1

    course = CoursePlanDemo(lecteur, courtier, journal, plan, paires,
                            ZoneH1(), mode_univers=mode_univers,
                            paires_collectees=paires_collectees,
                            alerter=alerter)
    log.info("Univers : %s (%s)", mode_univers,
             ", ".join(paires) if mode_univers == UNIVERS_EPINGLEES
             else "tous les actifs au plafond")

    repris = charger_etat(ecriture, campagne, plan)
    if repris is not None:
        course.etat, jour_utc_repris = repris
        # ⚠ Cette seconde valeur était JETÉE. C'est elle qui dit quelle
        # journée UTC était en cours au dernier pas, donc la seule à pouvoir
        # détecter que minuit est passé pendant que le processus était mort.
        course.jour_utc_courant = int(jour_utc_repris or 0)
        log.info("Course REPRISE depuis la base : %s", course.resume())
    else:
        log.info("Nouvelle course : %s", course.resume())
    # ⚠ L'état rechargé ÉCRASE celui que le constructeur vient de bâtir, y
    # compris la date de départ qu'il y avait posée. Une course reprise garde
    # donc la sienne — c'est le but — mais un état écrit avant la migration
    # v9 n'en a aucune, et sans ce garde-fou `/etat` répondait « en route
    # depuis 0 min » indéfiniment, au moment précis où l'on cherchait à
    # mesurer un débit.
    if not course.etat.demarre_ts:
        course.etat.demarre_ts = int(time.time())

    def sauver():
        sauver_etat(ecriture, campagne, course.etat, jour_utc())

    course._sauver = sauver
    _resoudre_les_ordres_en_vol(course, courtier, journal)

    tour_nu = course.tour

    def tour_persistant() -> bool:
        # L'état est sauvé après CHAQUE pas. Le processus peut mourir à
        # n'importe quel moment — l'hébergeur redéploie, met en veille — et un
        # état sauvé par intermittence rejouerait des ordres déjà passés.
        aujourdhui = jour_utc()
        # AVANT le tour, pas après : ouvrir une session au nom d'hier
        # l'imputerait à des compteurs qui vont être remis à zéro.
        if course.passer_le_jour_si_besoin(aujourdhui):
            sauver_etat(ecriture, campagne, course.etat, aujourdhui)
        joue = tour_nu()
        if joue:
            sauver_etat(ecriture, campagne, course.etat, aujourdhui)
        return joue

    course.tour = tour_persistant
    return course
