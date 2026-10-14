"""
Le potentiel de la zone à l'entrée, et l'expiration qui en découle.

--- D'où il vient ----------------------------------------------------------

Demandé le 2026-10-14 : « un calcul dynamique du temps d'expiration dans
les expériences ; déterminer si la zone est propice à une expiration de
15 minutes en tenant compte des creux précédents ; évaluer si la zone ne va
pas être cassée ou s'il y a des obstacles proches ; résoudre le problème
des entrées tardives ; utiliser le Stochastic (14, 3, 3) avec les lignes
100 et 10 ». Avec un protocole : quatre questions — amplitude, durée,
invalidation, qualité des données — et des variantes comparées sur les
MÊMES signaux.

--- Ce qui est mesuré, À L'ENTRÉE confirmée, sur les bougies déjà closes ---

- Amplitude : l'ATR(14), l'étendue des 5 et 15 dernières bougies en ATR.
- Durée : les jambes précédentes du ZigZag (pivots CONFIRMÉS), surtout
  celles qui partent d'un creux (achat) ou d'un sommet (vente) — combien
  de minutes un rebond a duré jusqu'ici, et de combien d'ATR il a monté.
- Invalidation : les percées de la zone dans l'heure (une mèche qui passe
  de l'autre côté puis se referme dedans — une zone « balayée »), et les
  obstacles CONFIRMÉS à moins de 2 ATR devant le prix.
- Stochastic lent (14, 3, 3), lignes 10 et 100 : survente au signal pour un
  achat (surachat pour une vente), ou retournement depuis la ligne
  extrême entre le signal et l'entrée.
- Tendance EMA 20 / 50 (neutre sous 0,05 % d'écart), et la figure de
  chandelier au signal et à l'entrée (englobante, marteau, étoile filante ;
  le doji est noté, jamais compté comme une confirmation).

Puis, de façon DÉTERMINISTE et écrite d'avance :

- l'**expiration dynamique** : l'horizon le plus long qui ne dépasse pas
  la durée médiane des rebonds précédents, ramené à 5 minutes si l'entrée
  est tardive (part parcourue d'au moins 0,46), s'il manque de place
  (moins de 1,95 amplitude avant l'obstacle) ou si la zone est instable
  (deux percées ou plus dans l'heure) ; « propice à 15 min » quand rien de
  tout cela ne l'a raccourcie ;
- la **catégorie de potentiel** : faible, intermédiaire, élevé, instable
  ou indéterminé.

Ce sont des conventions de départ, pas des seuils dont l'efficacité est
établie : le laboratoire les juge sur les mêmes signaux (`live.potentiel`).

⚠ Une percée de zone sur un marché OTC n'est pas la preuve d'une
manipulation : on décrit un comportement observé, rien de plus.
⚠ Hors de `CARACTERISTIQUES` : les leçons ne voient rien d'ici, et la
course ne lit rien d'ici.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from maxprofit.core.types import Candle
from maxprofit.indicators.oscillators import (
    atr, besoin_stochastique_lente, ema, stochastique_lente)

#: Les horizons d'expiration possibles, en minutes.
HORIZONS_MIN: tuple[int, ...] = (1, 2, 3, 4, 5, 10, 15)
STOCH = (14, 3, 3)
STOCH_BAS, STOCH_HAUT = 10.0, 100.0
EMA_RAPIDE, EMA_LENTE = 20, 50
#: Écart des EMA, en % du prix, sous lequel la tendance est neutre.
ECART_NEUTRE_PCT = 0.05
#: Jambes du ZigZag lues pour la durée et l'amplitude des rebonds.
JAMBES = 6
#: Minutes d'historique où l'on compte les percées de la zone.
RECUL_PERCEES = 60
#: Une mèche qui dépasse la zone de plus de ce nombre d'ATR est une percée.
SEUIL_PERCEE_ATR = 0.25
#: Un obstacle à moins de ce nombre d'ATR devant le prix est « proche ».
OBSTACLE_PROCHE_ATR = 2.0
#: Les seuils d'EXP-020 (C, D), pré-inscrits le 13/10.
PART_TARDIVE = 0.46
ESPACE_MIN = 1.95
#: À partir de ce nombre de percées dans l'heure, la zone est instable.
PERCEES_INSTABLE = 2
#: Horizon maximal quand une raison raccourcit l'expiration.
HORIZON_PRUDENT = 5
#: Catégories : étendue des 15 dernières bougies (ATR) et place (ATR).
AMPLITUDE_FAIBLE, AMPLITUDE_ELEVEE = 2.0, 4.0
ESPACE_FAIBLE_ATR, ESPACE_ELEVE_ATR = 1.0, 2.0
#: Bougies au plus après la confirmation pour une entrée « au repli ».
REPLI_MAX = 3

CATEGORIES: tuple[str, ...] = ("faible", "intermédiaire", "élevé",
                               "instable", "indéterminé")
MOTIFS: tuple[str, ...] = ("aucun", "englobante haussière",
                           "englobante baissière", "marteau",
                           "étoile filante", "doji")
#: Les clés posées à l'entrée (préfixées « e_ » par le rejeu), pour la
#: fiche CSV de /audit.
CLES_POTENTIEL: tuple[str, ...] = (
    "amplitude_5", "amplitude_15", "duree_rebond", "amplitude_rebond",
    "espace_atr", "obstacles_proches", "percees_zone", "stoch_k_signal",
    "stoch_extreme", "stoch_k", "stoch_d", "stoch_retournement",
    "tendance_ema", "ecart_ema_pct", "motif_signal", "motif_entree",
    "motif_favorable", "doji_signal", "expiration_dynamique",
    "raisons_expiration", "propice_15", "categorie_potentiel")
#: Les raisons qui raccourcissent l'expiration (masque de bits).
RAISONS = {1: "rebonds courts", 2: "entrée tardive", 4: "obstacle proche",
           8: "zone instable"}


def _contigues(bougies: Sequence[Candle], fin: int,
               n: int) -> Sequence[Candle] | None:
    if fin + 1 < n or fin >= len(bougies):
        return None
    fenetre = bougies[fin + 1 - n:fin + 1]
    if fenetre[-1].ts_sec - fenetre[0].ts_sec != 60 * (n - 1):
        return None
    return fenetre


def motif(precedente: Candle | None, c: Candle) -> int:
    """La figure de la bougie `c` (indice dans `MOTIFS`). Règles fixes,
    identiques pour toutes les paires :

    - englobante : le corps de `c` englobe STRICTEMENT celui de la
      précédente, de couleur opposée ;
    - marteau : corps non nul, mèche basse d'au moins 2 corps, mèche haute
      d'au plus 1 corps ; étoile filante : l'inverse ;
    - doji : corps d'au plus 10 % de l'étendue.
    """
    etendue = c.high - c.low
    if etendue <= 0:
        return 0
    corps = abs(c.close - c.open)
    if precedente is not None:
        corps_p = abs(precedente.close - precedente.open)
        haut, bas = max(c.open, c.close), min(c.open, c.close)
        haut_p = max(precedente.open, precedente.close)
        bas_p = min(precedente.open, precedente.close)
        englobe = corps > corps_p > 0 and haut >= haut_p and bas <= bas_p
        if englobe and c.close > c.open and precedente.close < precedente.open:
            return 1
        if englobe and c.close < c.open and precedente.close > precedente.open:
            return 2
    if corps > 0:
        meche_haute = c.high - max(c.open, c.close)
        meche_basse = min(c.open, c.close) - c.low
        if meche_basse >= 2 * corps and meche_haute <= corps:
            return 3
        if meche_haute >= 2 * corps and meche_basse <= corps:
            return 4
    if corps <= 0.1 * etendue:
        return 5
    return 0


def favorable(code: int, call: bool) -> bool:
    """La figure va-t-elle dans le sens du trade ?"""
    return code in ((1, 3) if call else (2, 4))


def horizon_au_plus(minutes: float) -> int:
    """Le plus long horizon possible qui ne dépasse pas `minutes`."""
    possibles = [h for h in HORIZONS_MIN if h <= minutes]
    return possibles[-1] if possibles else HORIZONS_MIN[0]


def expiration(duree_rebond: float | None, part: float | None,
               espace: float | None, percees: int | None
               ) -> tuple[int | None, int]:
    """(horizon en minutes, masque des raisons qui l'ont raccourci). La
    règle complète, écrite d'avance — voir l'en-tête."""
    if duree_rebond is None:
        return None, 0
    horizon = horizon_au_plus(duree_rebond)
    raisons = 1 if horizon < HORIZONS_MIN[-1] else 0
    for bit, raccourcir in ((2, part is not None and part >= PART_TARDIVE),
                            (4, espace is not None and espace < ESPACE_MIN),
                            (8, percees is not None
                             and percees >= PERCEES_INSTABLE)):
        if raccourcir:
            raisons |= bit
            horizon = min(horizon, HORIZON_PRUDENT)
    return horizon, raisons


def categorie(amplitude_15: float | None, espace_atr: float | None,
              percees: int | None, duree: float | None) -> int:
    """L'indice dans `CATEGORIES` — conventions de départ, à juger."""
    if amplitude_15 is None or espace_atr is None or duree is None \
            or percees is None:
        return 4
    if percees >= PERCEES_INSTABLE:
        return 3
    if amplitude_15 < AMPLITUDE_FAIBLE or espace_atr < ESPACE_FAIBLE_ATR:
        return 0
    if amplitude_15 >= AMPLITUDE_ELEVEE and espace_atr >= ESPACE_ELEVE_ATR:
        return 2
    return 1


def _jambes(bougies: Sequence[Candle], call: bool, a: float
            ) -> tuple[float | None, float | None, list]:
    """(durée médiane, amplitude médiane en ATR) des dernières jambes du
    ZigZag dans le sens du trade (creux → sommet pour un achat), à défaut
    de toutes les jambes ; et les pivots, pour les obstacles."""
    from maxprofit.strategies.zones_zigzag import pivots
    try:
        p = pivots(list(bougies))
    except Exception:                             # noqa: BLE001
        return None, None, []
    jambes = list(zip(p[-JAMBES - 1:-1], p[-JAMBES:]))
    sens = [(x, y) for x, y in jambes
            if (x.kind.name != "HAUT") == call]
    choisies = sens if len(sens) >= 2 else jambes
    if not choisies:
        return None, None, p
    durees = sorted((y.ts_sec - x.ts_sec) / 60 for x, y in choisies)
    amplitudes = sorted(abs(y.price - x.price) / a for x, y in choisies)
    return (durees[len(durees) // 2], amplitudes[len(amplitudes) // 2], p)


def _stoch(bougies: Sequence[Candle], fin: int):
    fenetre = _contigues(bougies, fin, besoin_stochastique_lente(*STOCH))
    return stochastique_lente(fenetre, *STOCH) if fenetre else None


def potentiel_a_l_entree(completes: Sequence[Candle], i: int, j: int,
                         call: bool, niveau: float | None,
                         part: float | None = None,
                         espace: float | None = None) -> dict[str, float]:
    """Les mesures de l'en-tête, à la clôture de `completes[j]` (entrée),
    le signal étant `completes[i]`. `part` et `espace` : la part parcourue
    et la place avant l'obstacle (en amplitudes), déjà mesurées par le
    contexte. Une mesure dont la fenêtre est trouée manque."""
    sortie: dict[str, float] = {}
    entree = completes[j]
    fen_atr = _contigues(completes, j, 15)
    a = atr(fen_atr, 14) if fen_atr else None
    if not a:
        sortie["categorie_potentiel"] = 4.0
        return sortie
    for n in (5, 15):
        fen = _contigues(completes, j, n)
        if fen:
            sortie[f"amplitude_{n}"] = round(
                (max(c.high for c in fen) - min(c.low for c in fen)) / a, 3)

    fenetre = completes[max(0, j + 1 - 300):j + 1]
    duree, ampl, piv = _jambes(fenetre, call, a)
    if duree is not None:
        sortie["duree_rebond"] = round(duree, 1)
        sortie["amplitude_rebond"] = round(ampl, 3)
    devant = [x.price for x in piv
              if (x.price > entree.close if call else x.price < entree.close)
              and (x.kind.name == "HAUT") == call]
    proche = min((abs(x - entree.close) for x in devant), default=None)
    sortie["espace_atr"] = round(proche / a, 3) if proche is not None \
        else 99.0
    sortie["obstacles_proches"] = float(sum(
        1 for x in devant if abs(x - entree.close) <= OBSTACLE_PROCHE_ATR * a))

    percees = None
    if niveau is not None:
        recentes = completes[max(0, j + 1 - RECUL_PERCEES):j + 1]
        marge = SEUIL_PERCEE_ATR * a
        percees = sum(
            1 for c in recentes
            if (call and c.low < niveau - marge and c.close >= niveau)
            or (not call and c.high > niveau + marge and c.close <= niveau))
        sortie["percees_zone"] = float(percees)

    # Stochastic lent : au signal, et son retournement jusqu'à l'entrée.
    s_signal = _stoch(completes, i)
    s_entree = _stoch(completes, j)
    if s_signal is not None:
        sortie["stoch_k_signal"] = round(s_signal.k, 2)
        sortie["stoch_extreme"] = float(
            s_signal.k <= STOCH_BAS if call else s_signal.k >= STOCH_HAUT)
    if s_entree is not None:
        sortie["stoch_k"] = round(s_entree.k, 2)
        sortie["stoch_d"] = round(s_entree.d, 2)
        avant = [s for s in (_stoch(completes, k) for k in range(i - 1, j))
                 if s is not None]
        if avant:
            if call:
                sortie["stoch_retournement"] = float(
                    min(s.k for s in avant) <= STOCH_BAS
                    and s_entree.k > STOCH_BAS and s_entree.k > s_entree.d)
            else:
                sortie["stoch_retournement"] = float(
                    max(s.k for s in avant) >= STOCH_HAUT
                    and s_entree.k < STOCH_HAUT and s_entree.k < s_entree.d)

    fen_ema = _contigues(completes, j, 3 * EMA_LENTE)
    if fen_ema:
        rapide, lente = ema(fen_ema, EMA_RAPIDE), ema(fen_ema, EMA_LENTE)
        ecart = (rapide - lente) / entree.close * 100
        tendance = (0 if abs(ecart) < ECART_NEUTRE_PCT
                    else 1 if ecart > 0 else -1)
        sortie["tendance_ema"] = float(tendance if call else -tendance)
        sortie["ecart_ema_pct"] = round(ecart, 4)

    precedente = completes[i - 1] if i >= 1 and \
        completes[i - 1].ts_sec == completes[i].ts_sec - 60 else None
    m_signal = motif(precedente, completes[i])
    m_entree = motif(completes[j - 1] if j >= 1 else None, entree)
    sortie["motif_signal"] = float(m_signal)
    sortie["motif_entree"] = float(m_entree)
    sortie["motif_favorable"] = float(favorable(m_signal, call)
                                      or favorable(m_entree, call))
    sortie["doji_signal"] = float(m_signal == 5)

    horizon, raisons = expiration(duree, part, espace, percees)
    if horizon is not None:
        sortie["expiration_dynamique"] = float(horizon * 60)
        sortie["raisons_expiration"] = float(raisons)
        sortie["propice_15"] = float(horizon == HORIZONS_MIN[-1])
    sortie["categorie_potentiel"] = float(categorie(
        sortie.get("amplitude_15"), sortie.get("espace_atr"), percees,
        duree))
    return sortie


def entree_au_repli(completes: Sequence[Candle], i: int, j: int, call: bool,
                    niveau: float | None, closes: Mapping[int, float],
                    echeance_sec: int) -> dict[str, float]:
    """Contre l'entrée tardive : après la confirmation (bougie `j`), attendre
    au plus `REPLI_MAX` bougies que le prix revienne au prix du SIGNAL
    (clôture de `completes[i]`) sans casser la zone, et entrer là.

    `attente_repli` : la bougie d'entrée (1 à REPLI_MAX), 0 si le prix n'est
    pas revenu ; `gagne_repli` : l'issue à l'échéance. Mesure si l'on peut
    garder la confirmation sans payer son prix d'entrée."""
    cible = completes[i].close
    for k in range(1, REPLI_MAX + 1):
        n = j + k
        if n >= len(completes) or \
                completes[n].ts_sec != completes[j].ts_sec + 60 * k:
            break
        c = completes[n]
        if niveau is not None and ((call and c.close < niveau)
                                   or (not call and c.close > niveau)):
            break                               # zone cassée : on renonce
        if (call and c.close <= cible) or (not call and c.close >= cible):
            sortie: dict[str, float] = {"attente_repli": float(k)}
            prix = closes.get(c.ts_sec + echeance_sec)
            if prix is not None and prix != c.close:
                sortie["gagne_repli"] = float((prix > c.close) == call)
            return sortie
    return {"attente_repli": 0.0}
