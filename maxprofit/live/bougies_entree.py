"""
`/bougies` : la tendance M1 et les 50 bougies avant l'entrée, en LECTURE
SEULE. Demandé le 03/11 (plan : `docs/etude_bougies_entree_plan.md`).

> « Il faut prendre en compte sur M1 la tendance haussière, baissière ou
> range. Une fois la tendance établie on analyse les 50 bougies précédant le
> point d'entrée ou la nouvelle touche sur le support ou résistance. »

--- La tendance (définition de l'utilisateur, 03/11) ---------------------------

- haussière : sommets et creux montants (higher high ET higher low) ;
- baissière : sommets et creux descendants (lower high ET lower low) ;
- range : ni l'un ni l'autre, le marché stagne.

Sommets et creux : les deux derniers pivots CONFIRMÉS de chaque sorte du
ZigZag de la stratégie (`zones_zigzag.pivots`, causal), sur les 300 bougies
M1 qui finissent à l'entrée. Ce ZigZag trace environ huit pivots par
tranche de cinq heures : c'est l'horizon qui donne deux sommets et deux
creux à comparer. La tendance est ensuite rapportée au trade : AVEC elle,
CONTRE elle, ou en RANGE. L'écart des moyennes EMA 20 / 50 sert de
sensibilité, jamais de remplacement.

--- Les 50 bougies --------------------------------------------------------------

Deux ancres : l'ENTRÉE réelle (la bougie de confirmation M1, k du rejeu) et
la TOUCHE de la zone (la bougie du signal). Pour chacune, les 50 dernières
bougies M1 complètes, ancre comprise, sans trou ; et leurs 10 et 5
dernières. Tout est orienté dans le sens du trade (« fav » : le camp du
trade, acheteurs pour un CALL ; « adv » : le camp adverse) :
- figures de bougies, catalogue figé (§3.1 du plan) ;
- tailles en amplitudes M1 moyennes des 50 bougies ;
- rapport de force : part des bougies du camp du trade, pression des corps,
  pression des mèches, position des clôtures, série en cours.

--- « Les autres, c'est à toi de les déterminer dans les données » ---------------

Fait honnêtement, comme l'étape 3 :
- **P1, a priori** : avec la tendance > contre elle ;
- **tout le reste** : le groupe comparé (tercile haut contre tercile bas,
  ou présence contre absence) et le SENS sont choisis sur l'EXPLORATION
  (avant le 21/10), puis jugés sur la VALIDATION (21 au 28/10), en BH à
  10 % ;
- une découverte reste une piste, à confirmer sur des données neuves après
  un gel.

L'issue est celle de l'entrée confirmée : clôture de la bougie d'entrée face
à la bougie complète qui commence 900 s plus tard (`gagne_confirme` du
rejeu, recalculé et contrôlé). Elle ne vaut que si `/rapprochement` montre
qu'elle s'accorde avec le règlement du broker.

Aucune décision n'est lue après le 28/10 : la confirmation du gel 2 est
ouverte depuis le 03/11. Sorties en CSV seulement, aucune écriture en base.
"""

from __future__ import annotations

import html
import logging
import random
import time
from collections import Counter
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Any, Callable, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.apprentissage.contexte import FENETRE
from maxprofit.core.payout import FLUX_POUR_LE_PLAFOND
from maxprofit.indicators.base import PivotKind
from maxprofit.live import etude3, selection3
from maxprofit.live.etude3 import ConnexionTracee, etat_lecture_seule
from maxprofit.live.rapprochement import PAYOUT_AVANT, confirme_selon, ic_ratio
from maxprofit.store.market import MarketReader
from maxprofit.strategies.zones_zigzag import pivots

log = logging.getLogger(__name__)

BOUGIES = 50
RECENTES: tuple[int, ...] = (BOUGIES, 10, 5)
FENETRE_TENDANCE = FENETRE                    # 300 bougies M1, 5 h
#: Une fenêtre de tendance trouée de plus de 10 % est « indéterminée ».
ETENDUE_TENDANCE_MAX_SEC = (FENETRE_TENDANCE * 11 // 10) * 60
EMA_COURTE, EMA_LONGUE, EMA_NEUTRE_PCT = 20, 50, 0.05

#: Le catalogue des figures (§3.1 du plan), règles fixes.
DOJI_CORPS = 0.10
MARUBOZU_CORPS = 0.80
TOUPIE_CORPS, TOUPIE_MECHE = 0.30, 0.25
REJET_MECHE = 0.60
MARTEAU_MECHE = 2.0
FIGURES: tuple[str, ...] = (
    "englobante_fav", "englobante_adv", "marteau_fav", "marteau_adv",
    "marubozu_fav", "marubozu_adv", "rejet_fav", "rejet_adv", "doji",
    "toupie", "incluse")
ANCRES = {"e": "entrée", "t": "touche"}

HAUSSIERE, BAISSIERE, RANGE, INDETERMINEE = (
    "haussière", "baissière", "range", "indéterminée")
AVEC, CONTRE = "avec", "contre"
ORIENTATIONS = (AVEC, CONTRE, RANGE, INDETERMINEE)

#: Un groupe plus petit, dans une période, ne se compare pas.
GROUPE_MIN = 30
RESPIRATION_SEC = 0.02

ATTENDRE_LE_REJEU = (
    "🕯 Le rejeu quotidien n'est pas encore en mémoire : il tourne quelques "
    "minutes après chaque démarrage. Relance /bougies quand /lecons annonce "
    "le nouvel apprentissage.")


def _utc(ts: float | None, forme: str = "%Y-%m-%d %H:%M") -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime(forme)


# --------------------------------------------------------------------------- #
# La tendance
# --------------------------------------------------------------------------- #

def tendance_structure(bougies: Sequence) -> tuple[str, dict[str, float]]:
    """(tendance, les deux derniers sommets et creux confirmés)."""
    pv = pivots(list(bougies))
    hauts = [p.price for p in pv if p.kind is PivotKind.HAUT][-2:]
    bas = [p.price for p in pv if p.kind is PivotKind.BAS][-2:]
    details = {"sommet_1": hauts[0] if len(hauts) > 1 else None,
               "sommet_2": hauts[-1] if hauts else None,
               "creux_1": bas[0] if len(bas) > 1 else None,
               "creux_2": bas[-1] if bas else None}
    if len(hauts) < 2 or len(bas) < 2:
        return INDETERMINEE, details
    if hauts[1] > hauts[0] and bas[1] > bas[0]:
        return HAUSSIERE, details
    if hauts[1] < hauts[0] and bas[1] < bas[0]:
        return BAISSIERE, details
    return RANGE, details


def _ema(valeurs: Sequence[float], n: int) -> float:
    alpha, e = 2 / (n + 1), valeurs[0]
    for v in valeurs[1:]:
        e += alpha * (v - e)
    return e


def tendance_ema(bougies: Sequence) -> str:
    """Sensibilité : EMA 20 face à EMA 50, neutre sous 0,05 % d'écart."""
    closes = [b.close for b in bougies]
    if len(closes) < EMA_LONGUE:
        return INDETERMINEE
    courte, longue = _ema(closes, EMA_COURTE), _ema(closes, EMA_LONGUE)
    ecart = 100 * (courte - longue) / longue if longue else 0.0
    if abs(ecart) < EMA_NEUTRE_PCT:
        return RANGE
    return HAUSSIERE if ecart > 0 else BAISSIERE


def orientation(tendance: str, call: bool) -> str:
    if tendance in (RANGE, INDETERMINEE):
        return tendance
    return AVEC if (tendance == HAUSSIERE) == call else CONTRE


# --------------------------------------------------------------------------- #
# Les bougies
# --------------------------------------------------------------------------- #

def figures(precedente, c, call: bool) -> set[str]:
    """Les figures de la bougie `c`, orientées dans le sens du trade."""
    etendue = c.high - c.low
    if etendue <= 0:
        return {"doji"}
    corps = abs(c.close - c.open)
    haut_c, bas_c = max(c.open, c.close), min(c.open, c.close)
    meche_haute, meche_basse = c.high - haut_c, bas_c - c.low
    vert, rouge = c.close > c.open, c.close < c.open
    fav = vert if call else rouge
    sortie: set[str] = set()
    if corps <= DOJI_CORPS * etendue:
        sortie.add("doji")
    if corps >= MARUBOZU_CORPS * etendue:
        sortie.add("marubozu_fav" if fav else "marubozu_adv")
    if corps <= TOUPIE_CORPS * etendue and \
            min(meche_haute, meche_basse) >= TOUPIE_MECHE * etendue:
        sortie.add("toupie")
    # Marteau : rejet par le bas, acheteur ; étoile filante : l'inverse.
    if corps > 0 and meche_basse >= MARTEAU_MECHE * corps and \
            meche_haute <= corps:
        sortie.add("marteau_fav" if call else "marteau_adv")
    if corps > 0 and meche_haute >= MARTEAU_MECHE * corps and \
            meche_basse <= corps:
        sortie.add("marteau_adv" if call else "marteau_fav")
    # Rejet : la mèche du côté de la zone (sous le prix pour un CALL).
    if meche_basse >= REJET_MECHE * etendue:
        sortie.add("rejet_fav" if call else "rejet_adv")
    if meche_haute >= REJET_MECHE * etendue:
        sortie.add("rejet_adv" if call else "rejet_fav")
    if precedente is not None:
        corps_p = abs(precedente.close - precedente.open)
        haut_p = max(precedente.open, precedente.close)
        bas_p = min(precedente.open, precedente.close)
        englobe = corps > corps_p > 0 and haut_c >= haut_p and bas_c <= bas_p
        if englobe and (vert and precedente.close < precedente.open
                        or rouge and precedente.close > precedente.open):
            sortie.add("englobante_fav" if fav else "englobante_adv")
        if c.high <= precedente.high and c.low >= precedente.low:
            sortie.add("incluse")
    return sortie


def _contigues(bougies: Sequence) -> bool:
    return all(b.ts_sec - a.ts_sec == 60 for a, b in zip(bougies, bougies[1:]))


def mesures(bougies: Sequence, a: int, call: bool) -> dict[str, float] | None:
    """Les mesures des 50 bougies finissant à l'indice `a` (ancre comprise),
    ou None si la fenêtre est incomplète ou trouée."""
    if a - BOUGIES + 1 < 0:
        return None
    w = bougies[a - BOUGIES + 1:a + 1]
    if not _contigues(w):
        return None
    sens = 1.0 if call else -1.0
    amplitude = sum(b.high - b.low for b in w) / len(w) or 1e-12
    precedente = bougies[a - BOUGIES] if a - BOUGIES >= 0 and \
        w[0].ts_sec - bougies[a - BOUGIES].ts_sec == 60 else None
    figs = []
    for i, b in enumerate(w):
        figs.append(figures(w[i - 1] if i else precedente, b, call))

    def fav(b) -> bool:
        return (b.close - b.open) * sens > 0

    def adv(b) -> bool:
        return (b.close - b.open) * sens < 0

    sortie: dict[str, float] = {}
    for n in RECENTES:
        sub, fsub = w[-n:], figs[-n:]
        etendues = sum(b.high - b.low for b in sub) or 1e-12
        sortie[f"part_fav_{n}"] = sum(1 for b in sub if fav(b)) / n
        sortie[f"pression_corps_{n}"] = sum(
            (b.close - b.open) * sens for b in sub) / etendues
        meches = 0.0
        for b in sub:
            basse = min(b.open, b.close) - b.low
            haute = b.high - max(b.open, b.close)
            meches += (basse - haute) * sens
        sortie[f"pression_meches_{n}"] = meches / etendues
        positions = []
        for b in sub:
            e = b.high - b.low
            p = (b.close - b.low) / e if e > 0 else 0.5
            positions.append(p if call else 1 - p)
        sortie[f"position_cloture_{n}"] = sum(positions) / n
        for f in FIGURES:
            sortie[f"{f}_{n}"] = float(sum(1 for s in fsub if f in s))
    for f in FIGURES:
        sortie[f"{f}_ancre"] = float(f in figs[-1])
    corps_fav = [abs(b.close - b.open) for b in w if fav(b)]
    corps_adv = [abs(b.close - b.open) for b in w if adv(b)]
    sortie["corps_fav_moyen"] = (sum(corps_fav) / len(corps_fav) / amplitude
                                 if corps_fav else 0.0)
    sortie["corps_adv_moyen"] = (sum(corps_adv) / len(corps_adv) / amplitude
                                 if corps_adv else 0.0)
    sortie["taille_5_sur_50"] = (sum(b.high - b.low for b in w[-5:]) / 5
                                 / amplitude)
    sortie["plus_grande_adv_10"] = max(
        (abs(b.close - b.open) for b in w[-10:] if adv(b)), default=0.0) \
        / amplitude
    sortie["plus_grande_fav_10"] = max(
        (abs(b.close - b.open) for b in w[-10:] if fav(b)), default=0.0) \
        / amplitude
    sortie["taille_ancre"] = (w[-1].high - w[-1].low) / amplitude
    couleur = (w[-1].close > w[-1].open) - (w[-1].close < w[-1].open)
    serie = 0
    if couleur:
        for b in reversed(w):
            if (b.close > b.open) - (b.close < b.open) != couleur:
                break
            serie += 1
    sortie["serie"] = float(serie * couleur * (1 if call else -1))
    return {k: round(v, 6) for k, v in sortie.items()}


def noms_des_variables() -> list[str]:
    """Les variables de chaque ancre, dans l'ordre des fichiers."""
    noms = []
    for n in RECENTES:
        noms += [f"part_fav_{n}", f"pression_corps_{n}",
                 f"pression_meches_{n}", f"position_cloture_{n}"]
        noms += [f"{f}_{n}" for f in FIGURES]
    noms += [f"{f}_ancre" for f in FIGURES]
    noms += ["corps_fav_moyen", "corps_adv_moyen", "taille_5_sur_50",
             "plus_grande_adv_10", "plus_grande_fav_10", "taille_ancre",
             "serie"]
    return noms


# --------------------------------------------------------------------------- #
# Les signaux
# --------------------------------------------------------------------------- #

def fiche(paire: str, bougies: Sequence, index: Mapping[int, int], e,
          mode: str) -> dict[str, Any]:
    """Un signal du rejeu, mesuré. `exclusion` dit pourquoi il ne compte
    pas, s'il ne compte pas."""
    ctx = dict(e.contexte)
    call = bool(ctx.get("sens_call"))
    d = int(e.ts_sec)
    sortie: dict[str, Any] = {
        "paire": paire, "decision": d, "decision_utc": _utc(d),
        "periode": em.periode(d, None), "sens": "call" if call else "put",
        "exclusion": ""}
    confirme = confirme_selon(mode, ctx)
    if not confirme:
        sortie["exclusion"] = "non confirmé en M1 au mode actuel"
        return sortie
    k = int(ctx.get("attente_confirmation") or 0) \
        if mode not in ("0", "meme") else 0
    i = index.get(d - 60)
    if i is None:
        sortie["exclusion"] = "bougie du signal absente ou incomplète"
        return sortie
    j = i + k
    if j >= len(bougies) or bougies[j].ts_sec != bougies[i].ts_sec + 60 * k:
        sortie["exclusion"] = "bougie d'entrée absente"
        return sortie
    entree = bougies[j]
    sortie["k"] = k
    sortie["entree_utc"] = _utc(entree.ts_sec + 60)
    s = index.get(entree.ts_sec + em.HORIZON_SEC)
    if s is None:
        sortie["issue"] = em.MANQUANTE
    elif bougies[s].close == entree.close:
        sortie["issue"] = em.EGALITE
    else:
        sortie["issue"] = (em.GAIN if (bougies[s].close > entree.close)
                           == call else em.PERTE)
    # Le contrôle compare à l'issue du rejeu pour la MÊME entrée : la bougie
    # du signal en mode 0 (`gagne`), la bougie de confirmation sinon.
    rejeu = e.gagne if k == 0 else ctx.get("gagne_confirme")
    sortie["issue_rejeu"] = ("" if rejeu is None else
                             em.GAIN if rejeu else em.PERTE)
    debut = max(0, j - FENETRE_TENDANCE + 1)
    fenetre = bougies[debut:j + 1]
    if len(fenetre) < FENETRE_TENDANCE or \
            fenetre[-1].ts_sec - fenetre[0].ts_sec > ETENDUE_TENDANCE_MAX_SEC:
        tendance, details, ema = INDETERMINEE, {}, INDETERMINEE
        sortie["tendance_note"] = "fenêtre de 5 h incomplète ou trouée"
    else:
        tendance, details = tendance_structure(fenetre)
        ema = tendance_ema(fenetre)
    sortie.update(details)
    sortie["tendance"] = tendance
    sortie["orientation"] = orientation(tendance, call)
    sortie["tendance_ema"] = ema
    sortie["orientation_ema"] = orientation(ema, call)
    me, mt = mesures(bougies, j, call), mesures(bougies, i, call)
    if me is None or mt is None:
        sortie["exclusion"] = "50 bougies incomplètes ou trouées"
        return sortie
    sortie.update({f"e_{c}": v for c, v in me.items()})
    sortie.update({f"t_{c}": v for c, v in mt.items()})
    # P3 et sa jumelle : une figure de confirmation du camp du trade sur
    # la touche ou l'entrée.
    sortie["confirmation_fav"] = float(any(
        sortie[f"{a}_{f}_ancre"] for a in ANCRES
        for f in ("englobante_fav", "marteau_fav", "rejet_fav")))
    if sortie["issue"] not in (em.GAIN, em.PERTE):
        sortie["exclusion"] = f"issue : {sortie['issue']}"
    return sortie


# --------------------------------------------------------------------------- #
# Les comparaisons
# --------------------------------------------------------------------------- #

def _bloc(r: Mapping) -> tuple[str, int]:
    return (r["paire"], int(r["decision"]) // 86400)


def preparer_tirages(lignes: Sequence[Mapping], tirages: int = em.TIRAGES,
                     graine: int = em.GRAINE) -> dict[str, Any]:
    """Les blocs paire × jour et les tirages bootstrap, faits UNE fois et
    partagés par toutes les comparaisons d'une même population."""
    cles = sorted({_bloc(r) for r in lignes})
    alea = random.Random(graine)
    k = len(cles)
    tirs = []
    for _ in range(tirages if k >= 2 else 0):
        compte = Counter(alea.choices(range(k), k=k))
        tirs.append(list(compte.items()))
    return {"cles": cles, "rang": {c: i for i, c in enumerate(cles)},
            "tirs": tirs}


_N = NormalDist()


def ecart(lignes: Sequence[Mapping], groupe: Callable[[Mapping], int | None],
          sens: int = 1, prep: Mapping[str, Any] | None = None,
          avec_ic: bool = True) -> dict[str, Any]:
    """sens × (taux de gain du groupe 1 − taux du groupe 0), par bootstrap
    de blocs paire × jour : IC 95 % percentile, et p unilatérale par
    l'approximation normale de l'écart-type bootstrap (1 000 tirages ne
    descendent pas sous p = 0,001, quand BH sur 127 tests en exige moins).
    `groupe` rend 1, 0 ou None (hors comparaison)."""
    prep = prep or preparer_tirages(lignes)
    k = len(prep["cles"])
    g1, n1, g0, n0 = [0] * k, [0] * k, [0] * k, [0] * k
    for r in lignes:
        g = groupe(r)
        if g is None:
            continue
        i = prep["rang"][_bloc(r)]
        y = int(r["issue"] == em.GAIN)
        if g:
            g1[i] += y
            n1[i] += 1
        else:
            g0[i] += y
            n0[i] += 1
    t_n1, t_n0 = sum(n1), sum(n0)
    sortie: dict[str, Any] = {
        "n1": t_n1, "n0": t_n0, "blocs": sum(1 for i in range(k)
                                              if n1[i] or n0[i]),
        "taux1": None, "taux0": None, "ecart": None, "bas": None,
        "haut": None, "p": None}
    if not t_n1 or not t_n0:
        return sortie
    t1, t0 = sum(g1) / t_n1, sum(g0) / t_n0
    sortie.update(taux1=t1, taux0=t0, ecart=sens * (t1 - t0))
    if not avec_ic or not prep["tirs"]:
        return sortie
    valeurs = []
    for tir in prep["tirs"]:
        a = b = c = d = 0
        for i, m in tir:
            a += g1[i] * m
            b += n1[i] * m
            c += g0[i] * m
            d += n0[i] * m
        if b and d:
            valeurs.append(sens * (a / b - c / d))
    if len(valeurs) < len(prep["tirs"]) // 2:
        return sortie
    valeurs.sort()
    moyenne = sum(valeurs) / len(valeurs)
    et = (sum((v - moyenne) ** 2 for v in valeurs) / (len(valeurs) - 1)) \
        ** 0.5
    sortie.update(bas=valeurs[int(0.025 * len(valeurs))],
                  haut=valeurs[int(0.975 * len(valeurs)) - 1],
                  p=(1 - _N.cdf(sortie["ecart"] / et)) if et > 0
                  else (0.0 if sortie["ecart"] > 0 else 1.0))
    return sortie


def regle_de_groupe(valeurs: Sequence[float]) -> dict[str, Any] | None:
    """Le groupe comparé, fixé sur l'EXPLORATION : tercile haut contre
    tercile bas ; présence contre absence quand les terciles se confondent
    (figures rares)."""
    bornes = em.terciles(valeurs)
    if bornes is None:
        return None
    b1, b2 = bornes
    if b1 < b2:
        return {"forme": "tercile haut / tercile bas", "b1": b1, "b2": b2}
    return {"forme": f"> {b1:g} / ≤ {b1:g}", "b1": b1, "b2": b1}


def appliquer(regle: Mapping, v) -> int | None:
    if v is None:
        return None
    v = float(v)
    if regle["b1"] < regle["b2"]:
        return 1 if v >= regle["b2"] else (0 if v < regle["b1"] else None)
    return 1 if v > regle["b1"] else 0


def criblage(explo: Sequence[Mapping], valid: Sequence[Mapping],
             variables: Sequence[str], respirer: Callable[[], None]
             ) -> list[dict[str, Any]]:
    """Sens et groupe choisis sur l'exploration, jugés sur la validation."""
    sortie = []
    tirages = preparer_tirages(valid)
    for rang, nom in enumerate(variables, 1):
        if rang % 20 == 0:
            respirer()
        ligne: dict[str, Any] = {"variable": nom, "nature": "exploratoire"}
        regle = regle_de_groupe([r.get(nom) for r in explo])
        if regle is None:
            ligne["verdict"] = "non testable"
            sortie.append(ligne)
            continue
        ligne.update(groupe=regle["forme"], borne_1=regle["b1"],
                     borne_2=regle["b2"])
        x = ecart(explo, lambda r: appliquer(regle, r.get(nom)),
                  avec_ic=False)
        ligne.update(explo_n1=x["n1"], explo_n0=x["n0"],
                     explo_taux1=x["taux1"], explo_taux0=x["taux0"])
        if x["ecart"] is None or min(x["n1"], x["n0"]) < GROUPE_MIN:
            ligne["verdict"] = "groupes trop petits en exploration"
            sortie.append(ligne)
            continue
        sens = 1 if x["ecart"] >= 0 else -1
        ligne.update(sens=sens, explo_ecart=sens * x["ecart"])
        v = ecart(valid, lambda r: appliquer(regle, r.get(nom)), sens,
                  prep=tirages)
        ligne.update(valid_n1=v["n1"], valid_n0=v["n0"],
                     valid_taux1=v["taux1"], valid_taux0=v["taux0"],
                     valid_ecart=v["ecart"], valid_bas=v["bas"],
                     valid_haut=v["haut"], p=v["p"])
        if v["p"] is None or min(v["n1"], v["n0"]) < GROUPE_MIN:
            ligne["verdict"] = "groupes trop petits en validation"
        sortie.append(ligne)
    testes = {l["variable"]: l["p"] for l in sortie
              if l.get("p") is not None and "verdict" not in l}
    decouvertes = em.benjamini_hochberg(testes) if testes else {}
    for l in sortie:
        if l["variable"] in testes:
            l["verdict"] = ("découverte (BH 10 %)" if decouvertes[l["variable"]]
                            else "même sens, non significatif"
                            if (l["valid_ecart"] or 0) > 0
                            else "sens inversé en validation")
    return sortie


# --------------------------------------------------------------------------- #
# La passe
# --------------------------------------------------------------------------- #

def verifier_la_copie(lue, active, debut: int, fin: int
                      ) -> tuple[set, dict[str, Any]]:
    """Les paires × jours dont le résumé des bougies diffère entre la base
    lue et l'active (comptes et sommes calculés par le serveur)."""
    copie: dict[str, Any] = {"verifiee": active is not None,
                             "divergences": [], "lus": Counter()}
    if active is None:
        return set(), copie
    sql = selection3.REQUETES_COPIE["candles"]

    def resumes(conn):
        return {(str(r[0]), int(r[1])): tuple(
            None if v is None else int(round(float(v))) for v in r[2:])
            for r in conn.execute(sql, (debut, fin)).fetchall()}
    a, b = resumes(lue), resumes(active)
    divergents = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    copie["divergences"] = sorted(divergents)
    return divergents, copie


def bougies_de(paire: str, lecteurs: Mapping[str, Any], divergents: set,
               copie: dict, debut: int, fin: int,
               respirer: Callable[[], None]) -> list:
    """Les bougies M1 complètes d'UNE paire, jour par jour ; un jour
    divergent est lu sur l'active. Une paire à la fois : 20 paires sur un
    mois tiendraient sinon des centaines de milliers de bougies en
    mémoire, à côté de la course."""
    serie = []
    for jour in range(debut // 86400, (fin - 1) // 86400 + 1):
        ou = "active" if (paire, jour) in divergents else "lue"
        copie["lus"][ou] += 1
        serie += [c for c in lecteurs[ou].candles(
            paire, 60, max(debut, jour * 86400),
            min(fin, (jour + 1) * 86400)) if c.complete]
        respirer()
    return serie


def etude(lue_brute, active_brute, course, *, nom_lecture: str = "relève",
          respirer: Callable[[], None] | None = None
          ) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers). Les deux bases sont déjà en lecture seule ;
    `active_brute` peut être None (copie non vérifiée)."""
    respirer = respirer or (lambda: time.sleep(RESPIRATION_SEC))
    t0 = time.monotonic()
    memoire = getattr(course, "rejeu_en_memoire", None)
    if not memoire:
        return ATTENDRE_LE_REJEU, []
    mode = str(getattr(course, "confirmation_m1", "suivante") or "suivante")
    exemples = [e for e in memoire[0] if e.ts_sec < em.FIN_VALIDATION]
    if not exemples:
        return ("🕯 Le rejeu en mémoire ne contient aucun signal avant le "
                "28/10."), []
    lue = ConnexionTracee(lue_brute)
    active = ConnexionTracee(active_brute) if active_brute is not None \
        else None
    lecture = {nom_lecture: etat_lecture_seule(lue)}
    if active is not None and active_brute is not lue_brute:
        lecture["active"] = etat_lecture_seule(active)
    debut_signaux = min(e.ts_sec for e in exemples)
    debut = (debut_signaux // 86400 - 1) * 86400
    fin = em.FIN_VALIDATION + 86400
    paires = sorted({e.pair for e in exemples})
    reference = active if active_brute is not lue_brute else None
    divergents, copie = verifier_la_copie(lue, reference, debut, fin)
    lecteurs = {"lue": MarketReader(lue),
                "active": MarketReader(reference) if reference is not None
                else None}
    fiches = []
    for paire in paires:
        serie = bougies_de(paire, lecteurs, divergents, copie, debut, fin,
                           respirer)
        index = {b.ts_sec: i for i, b in enumerate(serie)}
        for e in sorted((x for x in exemples if x.pair == paire),
                        key=lambda x: x.ts_sec):
            fiches.append(fiche(paire, serie, index, e, mode))
        del serie, index
        respirer()
    # Le payout du flux juste avant la décision, sur l'active (une ligne).
    source_payout = active if active is not None else lue
    for f in fiches:
        if f["exclusion"]:
            continue
        ligne = source_payout.execute(
            PAYOUT_AVANT, (f["paire"], f["decision"])).fetchone()
        f["payout_flux"] = None if not ligne or ligne[0] is None \
            else float(ligne[0])
        f["jouable"] = (f["payout_flux"] is not None
                        and f["payout_flux"] >= FLUX_POUR_LE_PLAFOND)
    retenues = [f for f in fiches if not f["exclusion"]]
    explo = [f for f in retenues if f["periode"] == em.EXPLORATION]
    valid = [f for f in retenues if f["periode"] == em.VALIDATION]

    def p1(lignes, cle="orientation"):
        return ecart(lignes, lambda r: {AVEC: 1, CONTRE: 0}.get(r[cle]))

    principale = {"exploration": p1(explo), "validation": p1(valid),
                  "ensemble": p1(retenues),
                  "ensemble_ema": p1(retenues, "orientation_ema"),
                  "jouables": p1([f for f in retenues if f.get("jouable")])}
    # En mode 0, la touche EST l'entrée : ses variables seraient des
    # doublons, qui compteraient deux fois chaque découverte.
    ancres = ("e",) if mode in ("0", "meme") else tuple(ANCRES)
    variables = [f"{a}_{n}" for a in ancres for n in noms_des_variables()] \
        + ["confirmation_fav"]
    tests = criblage(explo, valid, variables, respirer)
    regimes = par_regime(retenues)
    rapport = texte(fiches, retenues, principale, tests, regimes, {
        "nom": nom_lecture, "lecture": lecture, "copie": copie,
        "mode": mode, "debut": debut_signaux,
        "requetes": sum(lue.requetes.values()) + (
            sum(active.requetes.values()) if active is not None else 0),
        "refusees": lue.refusees + (active.refusees if active else 0),
        "temps": time.monotonic() - t0})
    return rapport, fichiers(fiches, tests, regimes, principale)


def par_regime(retenues: Sequence[Mapping]) -> list[dict[str, Any]]:
    """Le taux de gain par orientation de la tendance, par période."""
    sortie = []
    for definition, cle in (("structure", "orientation"),
                            ("EMA 20/50", "orientation_ema")):
        for periode in (em.EXPLORATION, em.VALIDATION, "ensemble"):
            for o in ORIENTATIONS:
                lignes = [r for r in retenues if r[cle] == o and (
                    periode == "ensemble" or r["periode"] == periode)]
                if not lignes:
                    continue
                blocs: dict[Any, tuple[float, float]] = {}
                for r in lignes:
                    g, n = blocs.get(_bloc(r), (0.0, 0.0))
                    blocs[_bloc(r)] = (g + (r["issue"] == em.GAIN), n + 1)
                ic = ic_ratio(blocs)
                gains = sum(1 for r in lignes if r["issue"] == em.GAIN)
                sortie.append({
                    "definition": definition, "periode": periode,
                    "orientation": o, "signaux": len(lignes),
                    "taux_gain": gains / len(lignes),
                    "ic_bas": ic[0] if ic else None,
                    "ic_haut": ic[1] if ic else None,
                    "jouables": sum(1 for r in lignes if r.get("jouable"))})
    return sortie


# --------------------------------------------------------------------------- #
# Le rapport et les fichiers
# --------------------------------------------------------------------------- #

def _pct(x, signe: bool = False) -> str:
    if x is None:
        return "—"
    return f"{100 * x:+.1f} pts" if signe else f"{100 * x:.1f} %"


def _ligne_ecart(nom: str, r: Mapping) -> str:
    if r["ecart"] is None:
        return f"- {nom} : groupes vides"
    ic = ("" if r["bas"] is None else
          f" (IC {100 * r['bas']:+.1f} à {100 * r['haut']:+.1f}, "
          f"p = {r['p']:.3f})")
    return (f"- {nom} : avec {_pct(r['taux1'])} ({r['n1']}) contre "
            f"{_pct(r['taux0'])} ({r['n0']}) ; écart "
            f"{_pct(r['ecart'], True)}{ic}")


def texte(fiches, retenues, principale, tests, regimes, meta) -> str:
    exclusions = Counter(f["exclusion"] for f in fiches if f["exclusion"])
    controle = [f for f in retenues if f.get("issue_rejeu")]
    accord = sum(1 for f in controle if f["issue_rejeu"] == f["issue"])
    orient = Counter(f["orientation"] for f in retenues)
    lu = meta["copie"]["lus"]
    l = ["🕯 <b>Les bougies avant l'entrée</b> (exploratoire, lecture seule)",
         f"Bases : {html.escape(meta['nom'])} et active "
         f"({', '.join(f'{k} : {v}' for k, v in meta['lecture'].items())}) ; "
         f"requêtes refusées : {meta['refusees']}. Rien n'a été écrit en "
         f"base.",
         ("Copie vérifiée par paire × jour : "
          f"{len(meta['copie']['divergences'])} divergence(s), lues sur "
          f"l'active ({lu.get('active', 0)} paires-jours sur "
          f"{sum(lu.values())})." if meta["copie"]["verifiee"]
          else "Copie NON vérifiée (active injoignable ou lue seule)."),
         f"Décisions du {_utc(meta['debut'], '%d/%m')} au 28/10 (la "
         f"confirmation du gel 2 n'est pas lue) ; mode de confirmation "
         f"{html.escape(meta['mode'])}. Issue : "
         + ("entrée immédiate (bougie du signal)" if meta["mode"] in
            ("0", "meme") else "entrée confirmée")
         + ", bougie complète à +900 s (proxy ; /rapprochement : même issue "
           "que le broker pour 81 ordres sur 86 en mode 0).",
         "",
         "<b>1. Population</b>",
         f"- {len(fiches)} signaux du rejeu ; retenus {len(retenues)} "
         f"(exploration {sum(1 for f in retenues if f['periode'] == em.EXPLORATION)}"
         f", validation {sum(1 for f in retenues if f['periode'] == em.VALIDATION)})",
         "- écartés : " + (" ; ".join(
             f"{html.escape(m)} {n}" for m, n in exclusions.most_common())
             or "aucun"),
         f"- contrôle de l'issue face au rejeu (même entrée) : "
         f"{accord}/{len(controle)} identiques",
         "",
         "<b>2. La tendance M1</b> (sommets et creux, 5 h)",
         "- " + " ; ".join(f"{o} {orient.get(o, 0)}" for o in ORIENTATIONS)]
    for r in regimes:
        if r["definition"] == "structure" and r["periode"] == "ensemble":
            ic = ("" if r["ic_bas"] is None else
                  f" (IC {100 * r['ic_bas']:.1f} à {100 * r['ic_haut']:.1f})")
            l.append(f"- {r['orientation']} : {_pct(r['taux_gain'])} sur "
                     f"{r['signaux']}{ic}")
    l += ["",
          "<b>3. P1, fixée à l'avance : avec la tendance &gt; contre</b>",
          _ligne_ecart("exploration", principale["exploration"]),
          _ligne_ecart("validation", principale["validation"]),
          _ligne_ecart("les deux", principale["ensemble"]),
          _ligne_ecart("les deux, tendance EMA (sensibilité)",
                       principale["ensemble_ema"]),
          _ligne_ecart("les deux, jouables (flux ≥ 84)",
                       principale["jouables"])]
    verdicts = Counter(t.get("verdict", "") for t in tests)
    decouvertes = [t for t in tests if t.get("verdict", "").startswith(
        "découverte")]
    juges = sorted((t for t in tests if t.get("explo_ecart") is not None
                    and t.get("valid_ecart") is not None),
                   key=lambda t: -t["explo_ecart"])
    l += ["",
          f"<b>4. Le reste, choisi sur l'exploration, jugé sur la "
          f"validation</b> ({len(tests)} variables"
          + (", la touche étant l'entrée en mode 0)" if meta["mode"] in
             ("0", "meme") else ", entrée et touche)"),
          "- " + " ; ".join(f"{html.escape(v)} {n}"
                            for v, n in verdicts.most_common())]
    if decouvertes:
        l.append("- découvertes (des PISTES, à confirmer sur des données "
                 "neuves) :")
        for t in sorted(decouvertes, key=lambda t: t["p"])[:10]:
            l.append(f"  • {html.escape(t['variable'])} "
                     f"[{html.escape(t['groupe'])}, sens {t['sens']:+d}] : "
                     f"exploration {_pct(t['explo_ecart'], True)}, validation "
                     f"{_pct(t['valid_ecart'], True)} (p = {t['p']:.4f})")
    else:
        l.append("- aucune découverte au seuil BH de 10 %.")
    if juges:
        l.append("- les 5 plus forts écarts d'exploration, et ce qu'ils "
                 "deviennent :")
        for t in juges[:5]:
            l.append(f"  • {html.escape(t['variable'])} : "
                     f"{_pct(t['explo_ecart'], True)} → "
                     f"{_pct(t['valid_ecart'], True)}")
    l += ["",
          "Une découverte ici est une piste : elle ne devient une règle "
          "qu'après un gel et une confirmation sur des données neuves. "
          "Aucune règle de la course n'est changée.",
          f"Durée : {meta['temps']:.0f} s ; requêtes : {meta['requetes']}."]
    return "\n".join(l)


def fichiers(fiches, tests, regimes, principale
             ) -> list[tuple[bytes, str, str]]:
    variables = [f"{a}_{n}" for a in ANCRES for n in noms_des_variables()]
    colonnes = ["paire", "decision", "decision_utc", "periode", "sens",
                "exclusion", "k", "entree_utc", "issue", "issue_rejeu",
                "payout_flux", "jouable", "tendance", "orientation",
                "tendance_ema", "orientation_ema", "tendance_note",
                "sommet_1", "sommet_2", "creux_1", "creux_2",
                "confirmation_fav", *variables]
    colonnes_tests = ["variable", "nature", "groupe", "borne_1", "borne_2",
                      "sens", "explo_n1", "explo_n0", "explo_taux1",
                      "explo_taux0", "explo_ecart", "valid_n1", "valid_n0",
                      "valid_taux1", "valid_taux0", "valid_ecart",
                      "valid_bas", "valid_haut", "p", "verdict"]
    lignes_p1 = []
    for nom, r in principale.items():
        lignes_p1.append({
            "variable": "P1_orientation", "nature": f"principale ({nom})",
            "groupe": "avec / contre", "sens": 1, "valid_n1": r["n1"],
            "valid_n0": r["n0"], "valid_taux1": r["taux1"],
            "valid_taux0": r["taux0"], "valid_ecart": r["ecart"],
            "valid_bas": r["bas"], "valid_haut": r["haut"], "p": r["p"],
            "verdict": "a priori"})
    return [
        (etude3._csv(fiches, colonnes), "bougies_signaux.csv",
         "Chaque signal : tendance, orientation, mesures des 50 bougies à "
         "l'entrée (e_) et à la touche (t_), issue"),
        (etude3._csv(lignes_p1 + list(tests), colonnes_tests),
         "bougies_tests.csv",
         "P1 a priori, puis chaque variable : groupe et sens choisis sur "
         "l'exploration, jugés sur la validation (BH 10 %)"),
        (etude3._csv(regimes, list(regimes[0]) if regimes else ["definition"]),
         "bougies_tendance.csv",
         "Taux de gain par orientation de la tendance et par période, IC "
         "paire × jour"),
    ]
