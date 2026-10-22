"""
EXP-030 — trois stratégies simples, sur TOUTES les bougies M1 (22/10).

--- D'où elles viennent ------------------------------------------------------

Demandé par l'utilisateur le 2026-10-22, après une liste de stratégies
« moins complexes » que ZoneH1. Trois corrections de sa part, justes :

1. **Le croisement de deux moyennes mobiles n'avait jamais été mesuré.**
   EXP-002 juge l'histogramme MACD (ligne MOINS signal) et le croisement
   de la ligne avec son SIGNAL — pas une moyenne rapide du PRIX qui croise
   une moyenne lente.
2. **Le momentum de 7 bougies passe le seuil.** Dans la table de
   persistance, « 7 vertes, 1 min » est ✅ à chaque rejeu depuis le 10/10
   (55,0 % sur 318, puis 54,6 %, 54,6 %, 54,2 % sur 1 086 ; anciens
   56 à 58 %). Une case sur 112, certes — mais la même, rejeu après rejeu.
3. **Le retour vers la moyenne n'est pas « après 8 rouges »** (P4) : c'est
   le retour vers la bande MÉDIANE de Bollinger, après AU MOINS 3 bougies,
   et « c'est en tendance haussière » : le repli d'une hausse jusqu'à la
   médiane, où l'on achète (première lecture, du même jour, corrigée avant
   l'ouverture de la fenêtre : parier contre toute série de 3 bougies
   au-delà de la médiane, quel que soit le sens de la tendance).

--- Les règles, écrites AVANT toute mesure -------------------------------------

Toutes lues à la clôture d'une bougie M1 complète ; les bougies doivent se
suivre minute après minute (un trou remet tout à zéro, rien n'est réparé) ;
une bougie sans corps interrompt une série, comme dans la persistance.

- **Momentum** : la bougie termine une série d'EXACTEMENT 7 bougies de même
  couleur (la 7e, comme la case « 7 » de la persistance) → dans le sens de
  la série. Vertes → CALL ; rouges → PUT, le miroir.
- **Croisement de moyennes** : moyennes mobiles EXPONENTIELLES des clôtures
  (α = 2 / (n + 1)), rapide et lente ; à la clôture où la rapide passe
  au-dessus de la lente → CALL, en dessous → PUT. Trois couples classiques,
  fixés d'avance : 9/21, 12/26 (la ligne MACD qui croise zéro) et 20/50.
  Une moyenne n'est lue qu'après `CHAUFFE` fois la période lente de
  bougies contiguës : la valeur de départ y pèse moins de 0,3 %.
- **Retour vers la bande médiane de Bollinger en tendance haussière** : la
  médiane (moyenne simple des 20 dernières clôtures) MONTE — plus haute à
  la veille du repli que `PENTE_MEDIANE` minutes plus tôt — et le prix
  clôturait au-dessus d'elle à la veille du repli ; puis un repli d'AU
  MOINS 3 bougies rouges ramène le prix à la médiane (plus bas de la bougie
  à la médiane ou dessous) → CALL, à la PREMIÈRE bougie du repli qui
  remplit tout cela (un seul signal par repli). Deux variantes selon la
  clôture de cette bougie : au-dessus de la médiane (elle a tenu), ou à la
  médiane et dessous (elle a cédé). Le miroir — tendance baissière, 3
  vertes ou plus qui remontent à la médiane, PUT — est mesuré à part, pour
  information.

Chaque règle est jugée à 1, 2, 3, 5, 10 et 15 min : position prise à la
clôture, issue à la clôture de la bougie de l'échéance ; une égalité ne
compte pas. Les 70 % les plus anciens de la période et les 30 % récents sont
comptés à part, comme partout au laboratoire, et la fenêtre indépendante
(pré-inscriptions S-, `live.preinscriptions`) à part encore.

--- Ce que ce n'est pas --------------------------------------------------------

Des mesures : la course ne lit rien d'ici. ⚠ Deux écarts avec un ordre réel,
à garder en tête à 1 min surtout : l'ordre s'ouvre 2 à 3 s APRÈS la clôture
(mesuré au premier ordre réel), et le payout aux échéances courtes n'est pas
forcément 92 %.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Sequence

from maxprofit.core.types import Candle

ECHEANCES: tuple[int, ...] = (60, 120, 180, 300, 600, 900)
N_SERIE = 7
COUPLES_EMA: tuple[tuple[int, int], ...] = ((9, 21), (12, 26), (20, 50))
#: Bougies contiguës, en multiples de la période lente, avant de lire une
#: moyenne : (1 − 2/(n+1))^(3n) < 0,3 %.
CHAUFFE = 3
BB_PERIODE = 20
BB_SERIE_MIN = 3
#: La tendance : la médiane à la veille du repli comparée à elle-même tant
#: de minutes plus tôt.
PENTE_MEDIANE = 10

#: Bougies récentes gardées pour lire la veille du repli et la pente : un
#: repli plus long que cela n'est plus un repli.
HISTORIQUE = 64

#: (clé, famille, libellé). Les libellés partent en HTML vers Telegram : ni
#: « < », ni « > », ni « & ».
REGLES: tuple[tuple[str, str, str], ...] = (
    ("mom7_vertes", "momentum", "7 vertes de suite → CALL (suivre)"),
    ("mom7_rouges", "momentum", "7 rouges de suite → PUT (suivre, miroir)"),
    *((f"ema_{r}_{l}", "moyennes",
       f"EMA {r} croise EMA {l} → dans le sens du croisement"
       + (" (= ligne MACD qui croise zéro)" if (r, l) == (12, 26) else ""))
      for r, l in COUPLES_EMA),
    ("bb_hausse_tient", "bollinger",
     f"hausse, repli de {BB_SERIE_MIN} rouges ou plus jusqu'à la médiane, "
     f"clôture au-dessus → CALL"),
    ("bb_hausse_cede", "bollinger",
     f"hausse, repli de {BB_SERIE_MIN} rouges ou plus jusqu'à la médiane, "
     f"clôture dessous → CALL"),
    ("bb_baisse", "bollinger",
     f"miroir : baisse, remontée de {BB_SERIE_MIN} vertes ou plus jusqu'à "
     f"la médiane → PUT"),
)
FAMILLES: tuple[tuple[str, str], ...] = (
    ("momentum", "Momentum — 7 bougies de même couleur, on suit la série"),
    ("moyennes", "Croisement de deux moyennes mobiles exponentielles du "
                 "prix, on suit le croisement"),
    ("bollinger", f"Retour vers la bande médiane de Bollinger "
                  f"({BB_PERIODE}) en tendance : la médiane monte depuis "
                  f"{PENTE_MEDIANE} min, repli d'au moins {BB_SERIE_MIN} "
                  f"bougies jusqu'à elle"),
)


class StrategiesSimples:
    """Accumule, paire après paire, les issues des trois stratégies."""

    def __init__(self, debut_ts: int, coupure_ts: int, ouverture_ts: int,
                 echeances: Sequence[int] = ECHEANCES):
        self.debut = int(debut_ts)
        self.coupure = int(coupure_ts)
        self.ouverture = int(ouverture_ts)
        self.echeances = tuple(echeances)
        cles = [cle for cle, _f, _l in REGLES]
        #: {règle: {échéance: [anciens, gagnés, récents, gagnés]}}
        self.periodes = {c: {s: [0, 0, 0, 0] for s in self.echeances}
                         for c in cles}
        #: {règle: {échéance: {(paire, jour): [n, gagnés]}}} — en mémoire
        #: seulement : l'intervalle par blocs, l'avant et la fenêtre.
        self.blocs: dict[str, dict[int, dict[tuple[str, int], list[int]]]] = {
            c: {s: {} for s in self.echeances} for c in cles}
        self.paires: dict[str, int] = {}

    def ajouter(self, bougies: Sequence[Candle],
                respirer: Callable[[], None] | None = None) -> None:
        completes = sorted((b for b in bougies if b.complete),
                           key=lambda b: b.ts_sec)
        if not completes:
            return
        pair = completes[0].pair
        closes = {b.ts_sec: b.close for b in completes}
        alphas = [(2 / (r + 1), 2 / (l + 1), CHAUFFE * l, f"ema_{r}_{l}")
                  for r, l in COUPLES_EMA]
        precedente: Candle | None = None
        serie = couleur = 0
        signaux = 0
        for i, c in enumerate(completes):
            if respirer is not None and i and i % 5000 == 0:
                respirer()
            if precedente is None or c.ts_sec - precedente.ts_sec != 60:
                # Un trou : tout repart de zéro.
                serie = couleur = 0
                ema = [[c.close, c.close, None] for _ in alphas]
                vues = 0
                fenetre: deque[float] = deque(maxlen=BB_PERIODE)
                # La médiane et la clôture de chaque bougie récente : la
                # veille du repli et la pente se lisent en arrière.
                historique: deque[tuple[float | None, float]] = deque(
                    maxlen=HISTORIQUE)
                repli_signale = False
            precedente = c
            vues += 1
            col = (c.close > c.open) - (c.close < c.open)
            serie = serie + 1 if col and col == couleur else (1 if col else 0)
            if serie <= 1:
                repli_signale = False
            couleur = col
            paris: list[tuple[str, int]] = []
            if serie == N_SERIE:
                paris.append(("mom7_vertes" if col > 0 else "mom7_rouges",
                              col))
            for etat, (a_r, a_l, chauffe, cle) in zip(ema, alphas):
                if vues > 1:
                    etat[0] += a_r * (c.close - etat[0])
                    etat[1] += a_l * (c.close - etat[1])
                ecart = etat[0] - etat[1]
                if vues > chauffe and etat[2] is not None:
                    if etat[2] <= 0 < ecart:
                        paris.append((cle, 1))
                    elif etat[2] >= 0 > ecart:
                        paris.append((cle, -1))
                etat[2] = ecart
            fenetre.append(c.close)
            mediane = (sum(fenetre) / BB_PERIODE
                       if len(fenetre) == BB_PERIODE else None)
            historique.append((mediane, c.close))
            if (mediane is not None and serie >= BB_SERIE_MIN
                    and not repli_signale
                    and len(historique) > serie + PENTE_MEDIANE):
                # La veille du repli, et la médiane PENTE_MEDIANE min avant.
                m_veille, c_veille = historique[-1 - serie]
                m_avant = historique[-1 - serie - PENTE_MEDIANE][0]
                if m_veille is not None and m_avant is not None:
                    if (col < 0 and m_veille > m_avant
                            and c_veille > m_veille and c.low <= mediane):
                        paris.append(("bb_hausse_tient"
                                      if c.close > mediane
                                      else "bb_hausse_cede", 1))
                        repli_signale = True
                    elif (col > 0 and m_veille < m_avant
                          and c_veille < m_veille and c.high >= mediane):
                        paris.append(("bb_baisse", -1))
                        repli_signale = True
            if not paris:
                continue
            signaux += 1
            k = 0 if c.ts_sec < self.coupure else 2
            jour = c.ts_sec // 86400
            for sec in self.echeances:
                sortie = closes.get(c.ts_sec + sec)
                if sortie is None or sortie == c.close:
                    continue
                for cle, sens in paris:
                    gagne = int((sortie - c.close) * sens > 0)
                    p = self.periodes[cle][sec]
                    p[k] += 1
                    p[k + 1] += gagne
                    b = self.blocs[cle][sec].setdefault((pair, jour), [0, 0])
                    b[0] += 1
                    b[1] += gagne
        self.paires[pair] = signaux

    def resultat(self) -> dict:
        """Les comptes de chaque règle à chaque échéance : `periodes`
        (anciens / récents), `avant` (ce qui précède l'ouverture de la
        fenêtre), et les blocs (paire, jour) de tout et de la fenêtre, pour
        les intervalles. ⚠ Les blocs pèsent : `live.simples` les réduit à
        des intervalles avant que rien ne soit sauvé."""
        jour_ouverture = self.ouverture // 86400
        regles = {}
        for cle, par_sec in self.periodes.items():
            regles[cle] = {}
            for sec, periodes in par_sec.items():
                avant, fenetre = [0, 0], []
                for (_pair, jour), (n, g) in self.blocs[cle][sec].items():
                    if jour < jour_ouverture:
                        avant[0] += n
                        avant[1] += g
                    else:
                        fenetre.append([n, g])
                regles[cle][str(sec)] = {
                    "periodes": list(periodes), "avant": avant,
                    "blocs_fenetre": fenetre,
                    "blocs": [list(v) for v in
                              self.blocs[cle][sec].values()]}
        return {"debut": self.debut, "coupure": self.coupure,
                "ouverture": self.ouverture,
                "echeances": list(self.echeances),
                "parametres": {"n_serie": N_SERIE,
                               "ema": [list(c) for c in COUPLES_EMA],
                               "chauffe": CHAUFFE,
                               "bollinger": [BB_PERIODE, BB_SERIE_MIN,
                                             PENTE_MEDIANE]},
                "paires": dict(self.paires), "regles": regles}
