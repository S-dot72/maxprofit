"""
Les échelles de l'imprévisibilité OTC, de 5 secondes à 1 heure : une étude
ISOLÉE, en LECTURE SEULE.

Protocole figé : `docs/preinscription_echelles.json`, empreinte b25ad6eb…
La passe refuse de tourner si `PARAMETRES` ne donnent plus cette empreinte.

À chaque échelle k (5 s, 15 s, 30 s, 1, 2, 3, 5, 10, 15, 30 et 60 min), sur
une grille d'instants t, le mouvement passé d = prix(t) − prix(t − k) est-il
suivi d'un mouvement dans le même sens (continuation) ou dans l'autre
(retournement) entre t + 2 s et t + 2 s + k ? Trois règles par échelle :
toutes les décisions, les grands mouvements (décile supérieur de |d| /
prix), et deux mouvements de même sens à la suite. Les prix sont les
derniers ticks connus, frais de moins de min(10 s, k / 2) ; une égalité est
une perte ; seules les minutes jouables comptent (flux ≥ 84). Une
corrélation par échelle mesure l'aléatoire, sans verdict.

Plusieurs millions de décisions : elles sont comptées au fil de l'eau.
L'exploration est balayée d'abord, paire par paire ; ses décisions gardent
leur ampleur (en simple précision) le temps de fixer la borne du grand
mouvement. La validation est balayée ensuite, avec cette borne.

Elle n'importe ni la course, ni l'exécution des ordres, ni la collecte.
Sorties en CSV seulement, aucune écriture en base. Rien n'est ventilé par
heure : la confirmation EURJPY 22 h reste aveugle.
"""

from __future__ import annotations

import heapq
import html
import json
import logging
import math
import time
from array import array
from bisect import bisect_right
from collections import Counter
from typing import Any, Callable, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core import memoire
from maxprofit.live import engulfing as en
from maxprofit.live.selection3 import PAYOUT_AVANT, PAYOUTS_DU_JOUR

log = logging.getLogger(__name__)

PARAMETRES: dict[str, Any] = json.loads(r"""
{
  "objet": "Les échelles de l'imprévisibilité OTC : de 5 secondes à 1 heure, le dernier mouvement du prix annonce-t-il le suivant (continuation ou retournement), en général, après un grand mouvement ou après deux mouvements de même sens, assez pour battre le point mort de 52,0833 % hors échantillon ? Les études précédentes ont jugé des motifs de 1 à 5 minutes ; celle-ci balaie toutes les échéances que l'OTC propose, y compris les très courtes, aux ticks.",
  "demande": "27/11 : « Effectue des test et motifs approprié au marché otc et à son imprévisibilité »",
  "donnees": {
    "ticks": "tick_paths (un chemin par paire et par minute, horodatages à la ms), lus sur la relève ; un jour dont le résumé diffère de l'active est lu sur l'active",
    "payouts": "payouts (points de changement), mêmes règles de copie",
    "paires": "toutes les paires qui ont des bougies M1 dans la période",
    "debut_utc": "2026-10-06T00:00:00Z",
    "fin_exploration_utc": "2026-11-03T00:00:00Z",
    "fin_validation": "début du jour UTC du lancement : seuls des jours complets sont lus"
  },
  "echelles_sec": [
    5,
    15,
    30,
    60,
    120,
    180,
    300,
    600,
    900,
    1800,
    3600
  ],
  "prix": "le prix à l'instant t est le dernier tick connu à t ; il n'est valable que s'il date de moins de F(k) = min(10 s, k / 2) pour l'échelle k, et que les minutes nécessaires ont leur chemin de ticks",
  "decisions": "pour chaque échelle k, les instants t multiples de pas(k) = max(15 s, k / 2) secondes UTC ; décision à t, à partir des prix à t − k et à t (le mouvement passé d = prix(t) − prix(t − k)) ; un mouvement passé nul n'est pas une décision ; il n'y a pas d'épisodes, la dépendance entre décisions voisines est prise en compte par le bootstrap par blocs paire × jour",
  "execution": {
    "entree": "prix à t + 2 s",
    "sortie": "prix à t + 2 s + k",
    "continuite": "toutes les minutes de t − k à la sortie ont leur chemin de ticks",
    "issue": "hausse si la sortie dépasse strictement l'entrée, baisse si elle est strictement en dessous, égalité sinon ; une égalité est une perte",
    "jouable": "dernier payout du flux relevé au plus tard à l'entrée ≥ 84"
  },
  "regles": {
    "dernier_mouvement": "toutes les décisions : continuation = parier dans le sens de d, retournement = parier contre",
    "grand_mouvement": "les décisions dont le mouvement relatif |d| / prix(t − k) atteint le décile supérieur à cette échelle (borne = quantile 9/10 de |d| / prix(t − k) sur les décisions jouables de l'exploration) ; une variation relative, pour que les paires en yen, cent fois plus chères, ne remplissent pas le décile à elles seules",
    "deux_mouvements": "les décisions dont le mouvement précédent, prix(t − k) − prix(t − 2k), est non nul et de même signe que d (prix à t − 2k valable aux mêmes conditions)"
  },
  "cases": "3 règles × 11 échelles = 33 cases ; chaque case choisit en exploration entre continuation et retournement (retournement s'il gagne strictement plus souvent), et garde ce choix en validation",
  "descriptif": "pour chaque échelle, la corrélation de Pearson entre le mouvement passé relatif, d / prix(t − k), et le mouvement suivant relatif, (sortie − entrée) / entrée, sur les décisions jouables de chaque période : une mesure de l'aléatoire, positive en continuation, négative en retournement, sans verdict (des variations relatives, pour que les paires en yen ne pèsent pas cent fois plus)",
  "bornes": "le quantile q d'une liste de n valeurs est la valeur de rang ⌊q × n⌋ (rangs comptés à partir de 0) de la liste triée, plafonné au rang n − 1",
  "selection": {
    "exploration": "jours du 06/10 au 02/11 : une case est retenue si son sens choisi atteint 52,0833 % sur au moins 300 décisions jouables ; les bornes du grand mouvement sont fixées ici",
    "validation": "jours du 03/11 à la veille du lancement : chaque case retenue est jugée dans son sens contre 52,0833 %, avec au moins 100 décisions ; p unilatérale du bootstrap par blocs paire × jour (1 000 tirages, graine 7) ; Benjamini-Hochberg à 10 % sur les cases jugées",
    "verdict": "validée si BH la retient et que son taux dépasse 52,0833 % ; sinon non reproduite",
    "confirmation": "une case validée n'est pas une stratégie : elle est gelée, puis confirmée sur des jours postérieurs au lancement, avant tout essai en démo"
  },
  "rapport": "aucune ventilation par heure ni par paire × heure (la confirmation EURJPY 22 h reste aveugle) ; pour chaque échelle, les taux de continuation des trois règles et la corrélation ; le détail par paire et par sens des cases retenues",
  "interdits": [
    "lecture seule : aucune écriture en base, sortie en CSV seulement",
    "aucun appel à l'exécution des ordres, aucun changement de la course, de la collecte, de ZoneH1, des échéances ni des configurations",
    "aucune échelle, aucune règle ni aucun réglage ne s'ajoute ou ne se rechoisit après le gel",
    "aucun argent réel sur une case validée avant sa confirmation, et pas même après sans un essai en démo"
  ],
  "remplacement": "cette version remplace le gel 54c2a6ff / b11bb9e3 (commit 637d98a), avant tout code et tout résultat : le grand mouvement y était mesuré en prix brut, ce qui aurait mêlé les échelles des paires"
}
""")
EMPREINTE = "b25ad6eb243d4c2b70ccc4ea38ea37c192c249a5712aed7b3e133cddcc8c17ac"

DEBUT = en._instant(PARAMETRES["donnees"]["debut_utc"])
FIN_EXPLORATION = en._instant(PARAMETRES["donnees"]["fin_exploration_utc"])
ECHELLES: tuple[int, ...] = tuple(PARAMETRES["echelles_sec"])
POINT_MORT = en.POINT_MORT
EXPLORATION, VALIDATION = en.EXPLORATION, en.VALIDATION
PERIODES = (EXPLORATION, VALIDATION)
DELAI_MS = 2_000
N_EXPLO_MIN = 300
N_VALID_MIN = 100
#: Le grand mouvement : quantile 9/10 des ampleurs de l'exploration.
DECILE = (9, 10)
JOURS_ESSAI = 2
RESPIRATION_SEC = 0.02

#: pas(k) = max(15 s, k / 2) ; tous divisent le cycle de 30 minutes, et la
#: grille de 15 s les contient tous.
PAS: dict[int, int] = {k: max(15, k // 2) for k in ECHELLES}
FRAICHEUR_MS: dict[int, int] = {k: int(min(10.0, k / 2) * 1000)
                                for k in ECHELLES}
GRILLE_SEC = 15
CYCLE_SEC = 1800
#: Les échelles jouées à chaque instant de la grille, selon t mod 30 min :
#: (rang de l'échelle, k, fraîcheur en ms).
PHASES: tuple[tuple[tuple[int, int, int], ...], ...] = tuple(
    tuple((ki, k, FRAICHEUR_MS[k]) for ki, k in enumerate(ECHELLES)
          if (GRILLE_SEC * j) % PAS[k] == 0)
    for j in range(CYCLE_SEC // GRILLE_SEC))
#: Temps de grille entre deux oublis de ticks, et entre deux pauses.
OUBLI_SEC = 3600
PAUSE_SEC = 6 * 3600
#: Ce qu'un oubli garde des ticks : t − 2k remonte jusqu'à deux heures, et
#: le dernier tick connu peut être dans la minute d'avant.
MEMOIRE_TICKS_SEC = 2 * max(ECHELLES) + 120

REGLES: tuple[str, ...] = ("dernier_mouvement", "grand_mouvement",
                           "deux_mouvements")
LIBELLES_REGLES = {"dernier_mouvement": "dernier mouvement",
                   "grand_mouvement": "grand mouvement",
                   "deux_mouvements": "deux mouvements"}
CONTINUATION, RETOURNEMENT = "continuation", "retournement"
#: Les six comptes d'un bloc : [continuation gagnée, retournement gagné,
#: égalité] après une hausse, puis les mêmes après une baisse.
SENS_PASSE: tuple[tuple[str, int], ...] = (("hausse", 0), ("baisse", 3))

#: Les statuts des décisions, dans l'ordre des contrôles.
STATUTS: tuple[str, ...] = (
    "non jouable", "pas de prix frais à t ou t − k", "mouvement passé nul",
    "fin des données", "minutes manquantes", "pas de prix frais à l'entrée",
    "pas de prix frais à la sortie", "jugée")
(S_NON_JOUABLE, S_PRIX_PASSE, S_NUL, S_FIN, S_TROU, S_ENTREE, S_SORTIE,
 S_JUGEE) = range(len(STATUTS))
JUGEE = STATUTS[S_JUGEE]


def libelle_echelle(k: int) -> str:
    return f"{k} s" if k < 60 else f"{k // 60} min"


def quantile(valeurs: Sequence[float], num: int, den: int) -> float:
    """La valeur de rang ⌊num × n / den⌋ (à partir de 0) de la liste triée,
    plafonnée au rang n − 1 ; lue parmi les n − rang plus grandes valeurs,
    sans trier toute la liste (un dixième de la mémoire pour le décile)."""
    n = len(valeurs)
    if not n:
        raise ValueError("quantile d'une liste vide")
    rang = min(n - 1, n * num // den)
    return min(heapq.nlargest(n - rang, valeurs))


def simple(x: float) -> float:
    """x arrondi en simple précision, comme une ampleur mise en attente."""
    return array("f", (x,))[0]


def compter(par_bloc: dict[int, list[int]], bloc: int, code: int) -> None:
    x = par_bloc.get(bloc)
    if x is None:
        x = par_bloc[bloc] = [0] * 6
    x[code] += 1


def prix(ticks: en.Ticks, t_ms: int, fraicheur_ms: int) -> float | None:
    """Le dernier tick connu à t, s'il date de moins de `fraicheur_ms` et
    que sa minute a son chemin (sinon None)."""
    connu = ticks.prix_a(t_ms)
    if connu is None or t_ms - connu[0] >= fraicheur_ms:
        return None
    return connu[1]


# --------------------------------------------------------------------------- #
# Les cumuls
# --------------------------------------------------------------------------- #

class Cumuls:
    """Les six comptes par règle × échelle × période × bloc paire-jour ; les
    sommes de la corrélation par échelle × période ; les décisions de
    l'exploration en attente de la borne du grand mouvement (ampleur en
    simple précision, et bloc × 6 + code de l'issue)."""

    def __init__(self, paires: Sequence[str], debut: int, fin: int) -> None:
        self.paires = list(paires)
        self.rang = {p: i for i, p in enumerate(self.paires)}
        self.jour0 = debut // 86400
        self.n_jours = max(1, (fin - debut) // 86400)
        self.comptes: dict[tuple[str, int, str], dict[int, list[int]]] = {
            (r, k, p): {} for r in REGLES for k in ECHELLES for p in PERIODES}
        self.correlation: dict[tuple[int, str], list[float]] = {
            (k, p): [0, 0.0, 0.0, 0.0, 0.0, 0.0]
            for k in ECHELLES for p in PERIODES}
        self.type_attente = ("h" if 6 * max(1, len(self.paires))
                             * self.n_jours < 32768 else "i")
        self.attente = [(array("f"), array(self.type_attente))
                        for _ in ECHELLES]
        self.bornes: list[float | None] = [None] * len(ECHELLES)
        self.bornes_fixees = False
        self.statuts: Counter = Counter()
        self.illisibles = 0
        self.n_decisions = 0

    def bloc_de(self, paire: str, ts_sec: int) -> int:
        jour = min(self.n_jours - 1, max(0, ts_sec // 86400 - self.jour0))
        return self.rang[paire] * self.n_jours + jour

    def paire_du_bloc(self, bloc: int) -> str:
        return self.paires[bloc // self.n_jours]

    def octets_en_attente(self) -> int:
        return sum(a.itemsize * len(a) + c.itemsize * len(c)
                   for a, c in self.attente)

    def fixer_les_bornes(self) -> None:
        """La borne du grand mouvement à chaque échelle, puis les décisions
        de l'exploration qui l'atteignent ; l'attente est ensuite libérée."""
        for ki, k in enumerate(ECHELLES):
            ampleurs, codes = self.attente[ki]
            self.attente[ki] = (array("f"), array(self.type_attente))
            if not ampleurs:
                continue
            borne = quantile(ampleurs, *DECILE)
            self.bornes[ki] = borne
            par_bloc = self.comptes[("grand_mouvement", k, EXPLORATION)]
            for a, cb in zip(ampleurs, codes):
                if a >= borne:
                    compter(par_bloc, *divmod(cb, 6))
            del ampleurs, codes
        self.bornes_fixees = True


# --------------------------------------------------------------------------- #
# Le balayage d'une paire
# --------------------------------------------------------------------------- #

def balayer(nom: str, ticks: en.Ticks,
            payout_a: Callable[[float], float | None], debut: int,
            fin_grille: int, fin_donnees: int, periode: str, cumuls: Cumuls,
            respirer: Callable[[], None]) -> int:
    """Les décisions d'une paire aux instants t de la grille dans [debut,
    fin_grille), toutes comptées dans `periode`. Rend le nombre de décisions
    jugées. L'exploration se balaie avant que les bornes soient fixées, la
    validation après."""
    if (periode == EXPLORATION) == cumuls.bornes_fixees:
        raise ValueError("l'exploration se balaie avant de fixer les bornes "
                         "du grand mouvement, la validation après")
    n_ech = len(ECHELLES)
    rang_paire, n_jours, jour0 = cumuls.rang[nom], cumuls.n_jours, cumuls.jour0
    dernier = [cumuls.comptes[("dernier_mouvement", k, periode)]
               for k in ECHELLES]
    grand = [cumuls.comptes[("grand_mouvement", k, periode)]
             for k in ECHELLES]
    deux = [cumuls.comptes[("deux_mouvements", k, periode)]
            for k in ECHELLES]
    sommes = [cumuls.correlation[(k, periode)] for k in ECHELLES]
    st = [[0] * len(STATUTS) for _ in range(n_ech)]
    attente = cumuls.attente if periode == EXPLORATION else None
    bornes = cumuls.bornes
    tampon = array("f", (0.0,))
    limite_ms = fin_donnees * 1000
    t = -(-debut // GRILLE_SEC) * GRILLE_SEC
    prochain_oubli, prochaine_pause = t + OUBLI_SEC, t + PAUSE_SEC
    n = 0
    while t < fin_grille:
        if t >= prochain_oubli:
            ticks.oublier_avant(t - MEMOIRE_TICKS_SEC)
            prochain_oubli = t + OUBLI_SEC
        if t >= prochaine_pause:
            respirer()
            prochaine_pause = t + PAUSE_SEC
        phase = PHASES[t % CYCLE_SEC // GRILLE_SEC]
        t_ms = t * 1000
        entree_ms = t_ms + DELAI_MS
        payout = payout_a(entree_ms / 1000)
        if payout is None or payout < en.FLUX_MIN:
            for ki, _, _ in phase:
                st[ki][S_NON_JOUABLE] += 1
            t += GRILLE_SEC
            continue
        bloc = rang_paire * n_jours + min(n_jours - 1,
                                          max(0, t // 86400 - jour0))
        for ki, k, f in phase:
            p_t = prix(ticks, t_ms, f)
            p_k = prix(ticks, t_ms - 1000 * k, f) if p_t is not None else None
            if p_k is None:
                st[ki][S_PRIX_PASSE] += 1
                continue
            d = p_t - p_k
            if d == 0:
                st[ki][S_NUL] += 1
                continue
            sortie_ms = entree_ms + 1000 * k
            if sortie_ms > limite_ms:
                st[ki][S_FIN] += 1
                continue
            if not ticks.continu(t_ms - 1000 * k, sortie_ms):
                st[ki][S_TROU] += 1
                continue
            p_e = prix(ticks, entree_ms, f)
            if p_e is None:
                st[ki][S_ENTREE] += 1
                continue
            p_x = prix(ticks, sortie_ms, f)
            if p_x is None:
                st[ki][S_SORTIE] += 1
                continue
            st[ki][S_JUGEE] += 1
            n += 1
            suite = p_x - p_e
            if d > 0:
                code = 0 if suite > 0 else (1 if suite < 0 else 2)
            else:
                code = 3 if suite < 0 else (4 if suite > 0 else 5)
            compter(dernier[ki], bloc, code)
            p_2k = prix(ticks, t_ms - 2000 * k, f)
            if p_2k is not None and (p_k - p_2k) * d > 0:
                compter(deux[ki], bloc, code)
            ampleur = abs(d) / p_k
            if attente is not None:
                ampleurs, codes = attente[ki]
                ampleurs.append(ampleur)
                codes.append(bloc * 6 + code)
            elif bornes[ki] is not None:
                tampon[0] = ampleur
                if tampon[0] >= bornes[ki]:
                    compter(grand[ki], bloc, code)
            x, y = d / p_k, suite / p_e
            c = sommes[ki]
            c[0] += 1
            c[1] += x
            c[2] += y
            c[3] += x * x
            c[4] += y * y
            c[5] += x * y
        t += GRILLE_SEC
    for ki, k in enumerate(ECHELLES):
        for code, nombre in enumerate(st[ki]):
            if nombre:
                cumuls.statuts[(periode, k, STATUTS[code])] += nombre
    cumuls.n_decisions += n
    return n


def correlation(c: Sequence[float]) -> float | None:
    n, sx, sy, sxx, syy, sxy = c
    if n < 3:
        return None
    vx, vy = sxx - sx * sx / n, syy - sy * sy / n
    if vx <= 0 or vy <= 0:
        return None
    return (sxy - sx * sy / n) / math.sqrt(vx * vy)


# --------------------------------------------------------------------------- #
# Les cases et les verdicts
# --------------------------------------------------------------------------- #

def cases(cumuls: Cumuls, tirages: en.Tirages) -> list[dict[str, Any]]:
    """Une ligne par règle × échelle × période ; le sens choisi en
    exploration (retournement s'il gagne strictement plus souvent) est gardé
    en validation, où les cases retenues reçoivent l'IC et la p du
    bootstrap par blocs (`tirages` : ceux de la validation)."""
    lignes = []
    for regle in REGLES:
        for k in ECHELLES:
            explo = cumuls.comptes[(regle, k, EXPLORATION)].values()
            choisi = 1 if (sum(x[1] + x[4] for x in explo)
                           > sum(x[0] + x[3] for x in explo)) else 0
            par_per = {}
            for per in PERIODES:
                par_bloc = cumuls.comptes[(regle, k, per)]
                cont = sum(x[0] + x[3] for x in par_bloc.values())
                ret = sum(x[1] + x[4] for x in par_bloc.values())
                n = sum(sum(x) for x in par_bloc.values())
                gains = ret if choisi else cont
                bas, haut = en.wilson(gains, n)
                par_per[per] = {
                    "regle": regle, "echelle_sec": k, "periode": per, "n": n,
                    "continuations": cont, "retournements": ret,
                    "egalites": n - cont - ret,
                    "taux_continuation": cont / n if n else None,
                    "sens_choisi": RETOURNEMENT if choisi else CONTINUATION,
                    "taux": gains / n if n else None, "ic_bas": bas,
                    "ic_haut": haut, "p": None, "verdict": ""}
            x, v = par_per[EXPLORATION], par_per[VALIDATION]
            if (x["n"] >= N_EXPLO_MIN and x["taux"] >= POINT_MORT
                    and v["n"] >= N_VALID_MIN):
                par_bloc = cumuls.comptes[(regle, k, VALIDATION)]
                gb = {b: float(y[choisi] + y[choisi + 3])
                      for b, y in par_bloc.items()}
                nb = {b: float(sum(y)) for b, y in par_bloc.items()}
                v["ic_bas"], v["ic_haut"], v["p"] = tirages.ratio(
                    gb, nb, v["taux"])
            lignes += [x, v]
    return lignes


def verdicts(lignes: list[dict[str, Any]]) -> None:
    par_cle = {(l["regle"], l["echelle_sec"], l["periode"]): l
               for l in lignes}
    jugees: dict[str, float] = {}
    cles: dict[str, dict] = {}
    for (regle, k, per), l in par_cle.items():
        if per != EXPLORATION:
            continue
        val = par_cle[(regle, k, VALIDATION)]
        if l["n"] < N_EXPLO_MIN:
            l["verdict"] = "trop peu en exploration"
        elif l["taux"] < POINT_MORT:
            l["verdict"] = "non retenue en exploration"
        elif val["n"] < N_VALID_MIN or val["p"] is None:
            l["verdict"] = "retenue, trop peu en validation"
            val["verdict"] = "trop peu en validation"
        else:
            l["verdict"] = "retenue en exploration"
            jugees[f"{regle}|{k}"] = val["p"]
            cles[f"{regle}|{k}"] = val
    decouvertes = em.benjamini_hochberg(jugees) if jugees else {}
    for cle, val in cles.items():
        val["verdict"] = ("validée (BH 10 %)" if decouvertes[cle]
                          and val["taux"] > POINT_MORT else "non reproduite")


def attendues_au_hasard(lignes: Sequence[Mapping[str, Any]]) -> float:
    """Combien de cases passeraient l'exploration par pur hasard : sans
    lien entre deux mouvements, chaque sens gagne (1 − part des égalités) /
    2 des décisions, et la case prend le meilleur des deux."""
    total = 0.0
    for l in lignes:
        n = l["n"]
        if l["periode"] != EXPLORATION or n < N_EXPLO_MIN:
            continue
        q = (1 - l["egalites"] / n) / 2
        sigma = math.sqrt(q * (1 - q) / n)
        if sigma <= 0:
            continue
        z = (POINT_MORT - q) / sigma
        total += min(1.0, 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))))
    return total


# --------------------------------------------------------------------------- #
# L'avancement
# --------------------------------------------------------------------------- #

_ETAT: dict[str, Any] = {}


def avancement() -> str | None:
    etat = dict(_ETAT)
    if not etat:
        return None
    minutes = (time.monotonic() - etat["debut"]) / 60
    faites, total = etat.get("faites", 0), etat.get("total", 0)
    texte_ = (f"{etat['etape']} ; {faites}/{total} passes de paires ; "
              f"{en._entier(etat.get('signaux', 0))} décisions jugées ; "
              f"{minutes:.0f} min")
    debut_paires = etat.get("debut_paires")
    if debut_paires is not None and 0 < faites < total:
        par_paire = (time.monotonic() - debut_paires) / 60 / faites
        texte_ += (f" ; fin des paires dans "
                   f"~{par_paire * (total - faites):.0f} min")
    texte_ += f" ; {memoire.resume()}"
    if etat.get("rss_max"):
        texte_ += f" ; pic de l'étude {etat['rss_max']:.0f} Mo"
    return texte_


def _mesurer_memoire() -> None:
    rss = memoire.residente()
    if rss is not None and rss > _ETAT.get("rss_max", 0):
        _ETAT["rss_max"] = rss


# --------------------------------------------------------------------------- #
# La passe en lecture seule
# --------------------------------------------------------------------------- #

def _sources(nom: str, debut_p: int, fin_p: int, debut: int, fin: int,
             base_de, lue):
    """(payout_a, ticks) d'une paire : les payouts de [debut_p, fin_p) et le
    dernier d'avant ; les ticks des jours de [debut, fin), chargés jour par
    jour à la demande (rien avant le début de l'étude)."""
    changements: list[tuple[int, float]] = []
    ligne = lue.execute(PAYOUT_AVANT, (nom, debut_p)).fetchone()
    if ligne and ligne[1] is not None:
        changements.append((int(ligne[0]), float(ligne[1])))
    for jour in range(debut_p // 86400, -(-fin_p // 86400)):
        for ts, pct in base_de("payouts", nom, jour).execute(
                PAYOUTS_DU_JOUR, (nom, jour * 86400,
                                  (jour + 1) * 86400)).fetchall():
            if pct is not None:
                changements.append((int(ts), float(pct)))
    instants = [x[0] for x in changements]

    def payout_a(t: float) -> float | None:
        k = bisect_right(instants, t) - 1
        return changements[k][1] if k >= 0 else None

    def charger(jour: int):
        if jour * 86400 >= fin or (jour + 1) * 86400 <= debut:
            return {}
        return {int(r[0]): (int(r[1]), int(r[2]), bytes(r[3]))
                for r in base_de("tick_paths", nom, jour).execute(
                    en.SQL_CHEMINS, (nom, jour * 86400,
                                     min(fin, (jour + 1) * 86400)))
                .fetchall()}
    return payout_a, en.Ticks(charger)


def passer(paires: Sequence[str], debut: int, separation: int, fin: int,
           base_de, lue, respirer: Callable[[], None]) -> Cumuls:
    """L'exploration de toutes les paires (instants de [debut, separation)),
    les bornes du grand mouvement, puis la validation de toutes les paires
    (instants de [separation, fin))."""
    cumuls = Cumuls(paires, debut, fin)
    _ETAT.update(total=2 * len(paires), debut_paires=time.monotonic())
    faites = 0
    for periode, debut_p, fin_p in ((EXPLORATION, debut, separation),
                                    (VALIDATION, separation, fin)):
        if fin_p > debut_p:
            for nom in paires:
                _ETAT.update(etape=f"{periode}, paire {nom}", faites=faites)
                payout_a, ticks = _sources(nom, debut_p, fin_p, debut, fin,
                                           base_de, lue)
                balayer(nom, ticks, payout_a, debut_p, fin_p, fin, periode,
                        cumuls, respirer)
                cumuls.illisibles += ticks.illisibles
                _ETAT.update(signaux=cumuls.n_decisions)
                faites += 1
                del ticks
                respirer()
        if periode == EXPLORATION:
            _ETAT.update(etape="bornes du grand mouvement",
                         attente=cumuls.octets_en_attente())
            cumuls.fixer_les_bornes()
            respirer()
    return cumuls


def analyser(cumuls: Cumuls) -> list[dict[str, Any]]:
    blocs = sorted({b for (_, _, p), d in cumuls.comptes.items()
                    if p == VALIDATION for b in d})
    lignes = cases(cumuls, en.Tirages(blocs))
    verdicts(lignes)
    return lignes


def etude(lue_brute, active_brute, course=None, *,
          nom_lecture: str = "relève", maintenant: float | None = None,
          respirer: Callable[[], None] | None = None,
          essai: bool = False, version: str | None = None
          ) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers). Les deux bases sont déjà en lecture seule ;
    `active_brute` peut être None. `essai` : le contrôle technique, sans
    aucun taux."""
    em.exiger_le_gel(PARAMETRES, EMPREINTE)
    pause = respirer or (lambda: time.sleep(RESPIRATION_SEC))

    def respirer() -> None:
        _mesurer_memoire()
        pause()

    maintenant = time.time() if maintenant is None else maintenant
    t0 = time.monotonic()
    fin = int(maintenant) // 86400 * 86400
    if fin <= FIN_EXPLORATION and not essai:
        return ("⏱ L'exploration court jusqu'au 02/11 : rien à valider "
                "encore."), []
    _ETAT.clear()
    _ETAT.update(debut=t0, etape="résumés des bases", faites=0, total=0,
                 signaux=0, rss_depart=memoire.residente())
    try:
        if essai:
            return _essai(lue_brute, active_brute, nom_lecture, fin,
                          respirer, t0, version), []
        return _etude(lue_brute, active_brute, nom_lecture, fin, respirer,
                      t0, version)
    finally:
        _ETAT.clear()


def _etude(lue_brute, active_brute, nom_lecture, fin, respirer, t0,
           version=None):
    lue, active, lecture, resume_lue, divergents, base_de = en._bases(
        lue_brute, active_brute, nom_lecture, DEBUT, fin)
    paires = sorted({p for (table, p, _) in resume_lue if table == "candles"})
    cumuls = passer(paires, DEBUT, FIN_EXPLORATION, fin, base_de, lue,
                    respirer)
    _ETAT.update(etape="cases et rapport", faites=2 * len(paires))
    lignes = analyser(cumuls)
    meta = {"lecture": lecture, "fin": fin, "debut": DEBUT,
            "separation": FIN_EXPLORATION, "paires": paires,
            "divergents": len(divergents),
            "requetes": sum(lue.requetes.values()) + (
                sum(active.requetes.values()) if active is not None else 0),
            "refusees": lue.refusees + (active.refusees if active else 0),
            "temps": time.monotonic() - t0, "version": version,
            "rss_depart": _ETAT.get("rss_depart"),
            "rss_max": _ETAT.get("rss_max"),
            "attente": _ETAT.get("attente")}
    return texte(cumuls, lignes, meta), fichiers(cumuls, lignes, resume_lue)


def _essai(lue_brute, active_brute, nom_lecture, fin, respirer, t0,
           version=None) -> str:
    """Le contrôle technique : la paire la plus fournie en ticks, sur les
    `JOURS_ESSAI` derniers jours complets, le premier jouant l'exploration
    et le second la validation. Aucun taux n'est montré."""
    debut = max(DEBUT, fin - JOURS_ESSAI * 86400)
    lue, active, lecture, resume_lue, divergents, base_de = en._bases(
        lue_brute, active_brute, nom_lecture, debut, fin)
    t_resumes = time.monotonic() - t0
    min_ticks: Counter = Counter()
    for (table, p, _), v in resume_lue.items():
        if table == "tick_paths" and v and v[0]:
            min_ticks[p] += v[0]
    entete = ("⏱ <b>Essai technique /echelles</b> : il ne compte pas, et "
              "aucun taux n'y est montré."
              + (f"\nVersion déployée : {html.escape(version)}."
                 if version else ""))
    if not min_ticks:
        return (entete + f"\nAucun tick entre le {en._utc(debut)} et le "
                f"{en._utc(fin - 86400)} : rien à essayer.")
    nom = max(sorted(min_ticks), key=lambda p: min_ticks[p])
    separation = min(fin, debut + 86400)
    jours = max(1, (fin - DEBUT) // 86400)
    jours_explo = max(1, (FIN_EXPLORATION - DEBUT) // 86400)
    n_paires = len({p for (table, p, _) in resume_lue if table == "candles"}
                   | set(min_ticks))
    t1 = time.monotonic()
    attente_prevue = None
    try:
        cumuls = passer([nom], debut, separation, fin, base_de, lue,
                        respirer)
        t_paire = time.monotonic() - t1
        lignes = analyser(cumuls)
        texte(cumuls, lignes, {
            "lecture": lecture, "fin": fin, "debut": debut,
            "separation": separation, "paires": [nom],
            "divergents": len(divergents), "requetes": 0, "refusees": 0,
            "temps": 0.0})
        tailles = sum(len(c) for c, _, _ in fichiers(cumuls, lignes,
                                                      resume_lue))
        jugees = {per: sum(nb for (p, _, s), nb in cumuls.statuts.items()
                           if p == per and s == JUGEE) for per in PERIODES}
        attente_prevue = (_ETAT.get("attente", 0) * n_paires * jours_explo
                          / 1e6)
        suite = (f"Décisions jugées : {en._entier(jugees[EXPLORATION])} le "
                 f"premier jour, {en._entier(jugees[VALIDATION])} le second ; "
                 f"chemins illisibles : {cumuls.illisibles} ; cases, rapport "
                 f"et fichiers : passés ({tailles // 1024} Kio de fichiers, "
                 "jetés).")
    except Exception as erreur:                          # noqa: BLE001
        log.exception("Essai /echelles : la suite a échoué")
        t_paire = time.monotonic() - t1
        suite = ("⚠ Balayage ou rapport en échec : "
                 + html.escape(f"{type(erreur).__name__}: {erreur}")[:300])
    t_total = time.monotonic() - t0
    projection = (t_paire * n_paires * jours / JOURS_ESSAI
                  + t_resumes * jours / JOURS_ESSAI) / 60
    rss_depart, rss_max = _ETAT.get("rss_depart"), _ETAT.get("rss_max")
    if rss_depart is not None and rss_max is not None:
        memoire_txt = (f"Mémoire du processus : {rss_depart:.0f} Mo au "
                       f"départ, pic {rss_max:.0f} Mo pendant l'essai ; "
                       f"{memoire.resume()}.")
    else:
        memoire_txt = f"Mémoire : {memoire.resume()}."
    if attente_prevue is not None:
        pic = max(0.0, (rss_max or 0) - (rss_depart or 0))
        prevu = (rss_depart or 0) + pic + 1.5 * attente_prevue
        memoire_txt += (f" Ampleurs de l'exploration en attente prévues : "
                        f"~{attente_prevue:.0f} Mo, soit un pic de l'ordre "
                        f"de {prevu:.0f} Mo (prudent).")
    requetes = sum(lue.requetes.values()) + (
        sum(active.requetes.values()) if active is not None else 0)
    refusees = lue.refusees + (active.refusees if active else 0)
    return "\n".join([
        entete,
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in lecture.items())
        + ". Rien n'est écrit en base, aucun ordre.",
        f"Paire essayée : {html.escape(nom)} (la plus fournie en ticks), du "
        f"{en._utc(debut)} au {en._utc(fin - 86400)} ({JOURS_ESSAI} jours, "
        "le premier en exploration, le second en validation).",
        suite,
        f"Durées : résumés des bases {t_resumes:.0f} s, paire {t_paire:.0f} "
        f"s, total {t_total:.0f} s ; requêtes {requetes}, refusées "
        f"{refusees}.",
        memoire_txt,
        f"Projection prudente de /echelles complet ({n_paires} "
        f"paire{'s' if n_paires > 1 else ''} × {jours} jours) : environ "
        f"{max(1, round(projection))} min.",
        "➡ Si l'essai est passé, lancez /echelles ; un point d'avancement "
        "arrive après 2 minutes, puis toutes les 10 minutes.",
    ])


# --------------------------------------------------------------------------- #
# Le rapport et les fichiers
# --------------------------------------------------------------------------- #

def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}".replace(".", ",")


def _corr(x) -> str:
    return "—" if x is None else f"{x:+.3f}".replace(".", ",")


def _part_egalites(l: Mapping[str, Any]) -> float | None:
    return l["egalites"] / l["n"] if l["n"] else None


def texte(cumuls: Cumuls, lignes, meta: Mapping) -> str:
    fin, paires = meta["fin"], meta["paires"]
    par_cle = {(l["regle"], l["echelle_sec"], l["periode"]): l
               for l in lignes}
    jugees = {per: sum(nb for (p, _, s), nb in cumuls.statuts.items()
                       if p == per and s == JUGEE) for per in PERIODES}
    morceaux = [
        "⏱ <b>Les échelles de l'imprévisibilité OTC</b> : de 5 secondes à "
        "1 heure, le dernier mouvement annonce-t-il le suivant ? (recherche "
        "isolée, lecture seule)",
        "Bases : " + ", ".join(f"{html.escape(n)} : {html.escape(v)}"
                               for n, v in meta["lecture"].items())
        + f" ; requêtes refusées : {meta['refusees']}. Rien n'a été écrit "
        "en base, aucun ordre.",
        f"Gel vérifié : empreinte {EMPREINTE[:8]}…",
        f"<b>Données</b> : {len(paires)} "
        f"paire{'s' if len(paires) > 1 else ''}, du {en._utc(meta['debut'])}"
        f" au {en._utc(fin - 86400)} ; exploration jusqu'au "
        f"{en._utc(meta['separation'] - 86400)}, validation ensuite. "
        f"Décisions jouables jugées aux ticks : "
        f"{en._entier(jugees[EXPLORATION])} en exploration, "
        f"{en._entier(jugees[VALIDATION])} en validation ; chemins "
        f"illisibles : {cumuls.illisibles} ; résumés lus sur l'active : "
        f"{meta['divergents']}.",
        "<b>Par échelle</b>, la part des continuations, exploration → "
        "validation, pour le dernier mouvement · le grand mouvement · deux "
        "mouvements de même sens ; la part des égalités (perdues dans les "
        "deux sens) ; la corrélation du mouvement suivant avec le précédent "
        "(0 : aucun lien) :"]
    for k in ECHELLES:
        taux = " · ".join(
            f"{_pct(par_cle[(r, k, EXPLORATION)]['taux_continuation'])} → "
            f"{_pct(par_cle[(r, k, VALIDATION)]['taux_continuation'])}"
            for r in REGLES)
        ex = par_cle[("dernier_mouvement", k, EXPLORATION)]
        va = par_cle[("dernier_mouvement", k, VALIDATION)]
        egal = f"{_pct(_part_egalites(ex))} → {_pct(_part_egalites(va))} %"
        cx = correlation(cumuls.correlation[(k, EXPLORATION)])
        cv = correlation(cumuls.correlation[(k, VALIDATION)])
        morceaux.append(f"• {libelle_echelle(k)} : {taux} ; égalités {egal}"
                        f" ; corrélation {_corr(cx)} → {_corr(cv)}")
    retenues = [l for l in lignes if l["periode"] == EXPLORATION
                and l["verdict"].startswith("retenue")]
    validees = [l for l in lignes if l["verdict"] == "validée (BH 10 %)"]
    sans_validation = [l for l in retenues
                       if l["verdict"] == "retenue, trop peu en validation"]
    if retenues:
        morceaux.append("<b>Cases retenues en exploration</b> (leur sens "
                        "atteint 52,08 %), exploration → validation :")
        for l in retenues:
            v = par_cle[(l["regle"], l["echelle_sec"], VALIDATION)]
            marque = "✅" if v["verdict"].startswith("validée") else "★"
            morceaux.append(
                f"• {marque} {libelle_echelle(l['echelle_sec'])}, "
                f"{LIBELLES_REGLES[l['regle']]}, {l['sens_choisi']} : "
                f"{_pct(l['taux'])} % sur {en._entier(l['n'])} → "
                f"{_pct(v['taux'])} % sur {en._entier(v['n'])}"
                + (f" [{_pct(v['ic_bas'])} ; {_pct(v['ic_haut'])}]"
                   if v["p"] is not None else "")
                + f" ; {v['verdict'] or l['verdict']}.")
    hasard = f"{attendues_au_hasard(lignes):.1f}".replace(".", ",")
    s_r = "s" if len(retenues) > 1 else ""
    s_v = "s" if len(validees) > 1 else ""
    morceaux.append(
        f"<b>Bilan</b> : {len(retenues)} case{s_r} retenue{s_r} en "
        f"exploration sur {len(REGLES) * len(ECHELLES)}, quand le hasard en "
        f"donnerait environ {hasard} ; {len(validees)} case{s_v} "
        f"validée{s_v}.")
    if validees:
        morceaux.append("Une case validée n'est pas une stratégie : elle se "
                        "gèle, puis se confirme sur des jours futurs, sans "
                        "argent réel.")
    else:
        s_s = "s" if len(sans_validation) > 1 else ""
        morceaux.append(
            "Aucune case ne bat le point mort de 52,08 % hors échantillon : "
            "de 5 secondes à 1 heure, le dernier mouvement n'annonce pas le "
            "suivant assez souvent pour payer un payout de 92 %"
            + (f" ({len(sans_validation)} case{s_s} retenue{s_s} sans assez "
               f"de décisions en validation pour être jugée{s_s})"
               if sans_validation else "") + ".")
    duree = f"{meta['temps'] / 60:.1f}".replace(".", ",")
    pied = f"Requêtes : {meta['requetes']}. Durée : {duree} min."
    if meta.get("rss_max") is not None:
        pied += (f" Mémoire : pic {meta['rss_max']:.0f} Mo pendant l'étude"
                 + (f" (départ {meta['rss_depart']:.0f} Mo)"
                    if meta.get("rss_depart") is not None else "")
                 + (f", dont {meta['attente'] / 1e6:.0f} Mo d'ampleurs en "
                    "attente" if meta.get("attente") else "") + ".")
    if meta.get("version"):
        pied += f" Version : {html.escape(meta['version'])}."
    morceaux.append(pied)
    return "\n".join(morceaux)


COLONNES_CASES = ("regle", "echelle_sec", "periode", "n", "continuations",
                  "retournements", "egalites", "taux_continuation",
                  "sens_choisi", "taux", "ic_bas", "ic_haut", "p", "verdict")
COLONNES_DETAILS = ("regle", "echelle_sec", "sens_choisi", "periode",
                    "paire", "mouvement_passe", "n", "gains", "taux",
                    "ic_bas", "ic_haut")
COLONNES_CORRELATIONS = ("echelle_sec", "periode", "n", "correlation",
                         "borne_grand_mouvement")
COLONNES_STATUTS = ("source", "periode", "echelle_sec", "statut", "nombre")


def _details(cumuls: Cumuls, lignes) -> list[dict[str, Any]]:
    """Les cases retenues, par période, paire et sens du mouvement passé."""
    sortie = []
    for l in lignes:
        if not (l["periode"] == EXPLORATION
                and l["verdict"].startswith("retenue")):
            continue
        choisi = 1 if l["sens_choisi"] == RETOURNEMENT else 0
        for per in PERIODES:
            par_paire: dict[tuple[str, str], list[int]] = {}
            for b, x in cumuls.comptes[(l["regle"], l["echelle_sec"],
                                        per)].items():
                for sens, decalage in SENS_PASSE:
                    y = par_paire.setdefault(
                        (cumuls.paire_du_bloc(b), sens), [0, 0])
                    y[0] += x[decalage + choisi]
                    y[1] += x[decalage] + x[decalage + 1] + x[decalage + 2]
            for (p, sens), (g, n) in sorted(par_paire.items()):
                if not n:
                    continue
                bas, haut = en.wilson(g, n)
                sortie.append({"regle": l["regle"],
                               "echelle_sec": l["echelle_sec"],
                               "sens_choisi": l["sens_choisi"],
                               "periode": per, "paire": p,
                               "mouvement_passe": sens, "n": n, "gains": g,
                               "taux": g / n, "ic_bas": bas,
                               "ic_haut": haut})
    return sortie


def fichiers(cumuls: Cumuls, lignes, resume) -> list[tuple[bytes, str, str]]:
    correlations = [
        {"echelle_sec": k, "periode": per,
         "n": int(cumuls.correlation[(k, per)][0]),
         "correlation": correlation(cumuls.correlation[(k, per)]),
         "borne_grand_mouvement": cumuls.bornes[ki]}
        for ki, k in enumerate(ECHELLES) for per in PERIODES]
    statuts = [{"source": "décisions", "periode": p, "echelle_sec": k,
                "statut": s, "nombre": nb}
               for (p, k, s), nb in sorted(
                   cumuls.statuts.items(),
                   key=lambda kv: (PERIODES.index(kv[0][0]), kv[0][1],
                                   STATUTS.index(kv[0][2])))]
    statuts.append({"source": "données", "periode": "", "echelle_sec": "",
                    "statut": "chemins illisibles",
                    "nombre": cumuls.illisibles})
    jours: dict[tuple[str, int], dict[str, Any]] = {}
    for (table, p, j), v in resume.items():
        d = jours.setdefault((p, j), {"paire": p, "jour": en._utc(
            j * 86400, "%Y-%m-%d")})
        if table == "candles" and v:
            d["minutes_bougies"] = v[0]
        elif table == "tick_paths" and v:
            d["minutes_ticks"] = v[0]
        elif table == "payouts" and v:
            d["releves_payout"] = v[0]
    donnees = [jours[k] for k in sorted(jours)]
    return [
        (en._csv(lignes, COLONNES_CASES), "echelles_cases.csv",
         "Une ligne par règle × échelle × période : continuations, "
         "retournements, égalités, sens choisi, taux, IC, verdict"),
        (en._csv(_details(cumuls, lignes), COLONNES_DETAILS),
         "echelles_details.csv",
         "Les cases retenues, par paire et par sens du mouvement passé"),
        (en._csv(correlations, COLONNES_CORRELATIONS),
         "echelles_correlations.csv",
         "La corrélation par échelle et période, et la borne du grand "
         "mouvement (variation relative)"),
        (en._csv(statuts, COLONNES_STATUTS), "echelles_statuts.csv",
         "Les décisions jugées et écartées, par période et échelle"),
        (en._csv(donnees, ("paire", "jour", "minutes_bougies",
                           "minutes_ticks", "releves_payout")),
         "echelles_donnees.csv", "Les données lues, par paire et par jour"),
    ]
