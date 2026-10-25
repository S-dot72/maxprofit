"""
Étape 0, phase F — analyses EXPLORATOIRES au tick (cahier des charges v1.0,
§9 à §12). Lecture seule ; rien ici ne devient un filtre ni ne touche la
fenêtre T0/T2/T3.

--- Ce qui est mesuré, sur chaque signal dont la fenêtre est complète ---------

Le contrat T0 (entrée au dernier tick avant la décision, comme la clôture de
la bougie du signal), suivi tick par tick pendant 15 minutes :

- les issues à 4, 10 et 15 min (dernier tick avant l'échéance ; une égalité
  n'est ni gagnée ni perdue), comparées à celles des clôtures M1 du rejeu ;
- MFE et MAE en amplitudes M1 (convention de `historique._excursions`), et
  l'instant de chacune ;
- la position à 1, 2, 4, 10 et 14 min ;
- « a été gagnant » : un tick du bon côté de l'entrée avant l'échéance ;
- le DERNIER PASSAGE : l'instant où le prix a changé de côté par rapport à
  l'entrée pour la dernière fois — pour un perdant, le moment où l'avance est
  devenue insuffisante ;
- le franchissement de la zone (`niveau`, contre le trade, au tick) : s'il a
  lieu, sa profondeur maximale, le temps passé au-delà, le retour éventuel et
  son délai, le nombre de franchissements.

--- La fin d'échéance n'est jugée que contre un témoin ------------------------

Pour une marche aléatoire, l'instant du dernier passage suit la loi de
l'arcsinus : il se concentre AU DÉBUT ET À LA FIN de l'intervalle. Un « pic de
retournements en fin d'échéance » est donc attendu du seul hasard. Il est
comparé à des pseudo-contrats témoins (entrées à des minutes fixées d'avance,
sans signal) et à la loi théorique. Les fenêtres de fin (30, 60, 120, 300 s)
sont fixées ici, avant toute lecture.

--- Niveaux de preuve ----------------------------------------------------------

Le rapport classe chaque phrase : fait observé, association statistique,
hypothèse explicative, conclusion non établie. Aucune trajectoire, aucun
franchissement suivi d'un retour ne prouve une intention du broker.
"""

from __future__ import annotations

import math
import random
from typing import Any, Callable, Mapping, Sequence

ECHEANCES: tuple[int, ...] = (240, 600, 900)
POSITIONS_SEC: tuple[int, ...] = (60, 120, 240, 600, 840)
FINS_SEC: tuple[int, ...] = (30, 60, 120, 300)
DUREE_SEC = 900
TIRAGES, GRAINE = 1000, 7


def arcsinus_fin(fin_sec: int, duree_sec: int = DUREE_SEC) -> float:
    """P(dernier passage dans les `fin_sec` dernières secondes) pour une
    marche aléatoire sur `duree_sec` : 1 − (2/π)·arcsin(√((T − f)/T))."""
    return 1 - 2 / math.pi * math.asin(math.sqrt(
        (duree_sec - fin_sec) / duree_sec))


def trajectoire(ticks: Sequence[tuple[int, float]], d_ms: int, entree: float,
                call: bool, amplitude: float,
                niveau: float | None = None) -> dict[str, Any]:
    """Le contrat suivi tick par tick. `ticks` : (instant ms, prix),
    ordonnés ; seuls ceux de [d_ms, d_ms + 15 min[ comptent."""
    sens = 1 if call else -1
    amplitude = amplitude or 1e-12
    fin_ms = d_ms + DUREE_SEC * 1000
    apres = [(t, p) for t, p in ticks if d_ms <= t < fin_ms]
    sortie: dict[str, Any] = {}
    for sec in ECHEANCES:
        limite = d_ms + sec * 1000
        prix = entree
        for t, p in apres:
            if t >= limite:
                break
            prix = p
        sortie[f"issue_{sec}"] = (None if prix == entree
                                  else (prix - entree) * sens > 0)
    mfe = mae = 0.0
    t_mfe = t_mae = None
    gagnant = False
    cote = 0
    dernier = None
    for t, p in apres:
        ecart = (p - entree) * sens
        if ecart > mfe:
            mfe, t_mfe = ecart, (t - d_ms) / 1000
        if -ecart > mae:
            mae, t_mae = -ecart, (t - d_ms) / 1000
        gagnant = gagnant or ecart > 0
        nouveau = (ecart > 0) - (ecart < 0)
        if nouveau:
            if cote and nouveau != cote:
                dernier = (t - d_ms) / 1000
            cote = nouveau
    sortie.update({
        "mfe": round(mfe / amplitude, 3), "mae": round(mae / amplitude, 3),
        "t_mfe": t_mfe, "t_mae": t_mae, "a_ete_gagnant": gagnant,
        "dernier_passage": dernier})
    for sec in POSITIONS_SEC:
        limite = d_ms + sec * 1000
        prix = entree
        for t, p in apres:
            if t >= limite:
                break
            prix = p
        sortie[f"pos_{sec}"] = round((prix - entree) * sens / amplitude, 3)
    if niveau is not None:
        sortie.update(_franchissement(apres, d_ms, fin_ms, sens, niveau,
                                      amplitude))
    return sortie


def _franchissement(apres, d_ms: int, fin_ms: int, sens: int, niveau: float,
                    amplitude: float) -> dict[str, Any]:
    """Au-delà = un tick de l'autre côté du niveau, contre le trade."""
    franchi = False
    profondeur = 0.0
    temps_ms = 0
    n = 0
    premier = retour = None
    au_dela_prec = False
    for i, (t, p) in enumerate(apres):
        au_dela = (p - niveau) * sens < 0
        if au_dela:
            franchi = True
            profondeur = max(profondeur, (niveau - p) * sens)
            suivant = apres[i + 1][0] if i + 1 < len(apres) else fin_ms
            temps_ms += suivant - t
            if not au_dela_prec:
                n += 1
                if premier is None:
                    premier = t
        elif premier is not None and retour is None:
            retour = t
        au_dela_prec = au_dela
    return {"franchi": franchi,
            "profondeur": round(profondeur / amplitude, 3) if franchi else None,
            "temps_au_dela": round(temps_ms / 1000, 1) if franchi else None,
            "retour": (retour is not None) if franchi else None,
            "delai_retour": (round((retour - premier) / 1000, 1)
                             if retour is not None else None),
            "n_franchissements": n}


# --------------------------------------------------------------------------- #
# Statistiques : bootstrap par blocs (paire, jour)
# --------------------------------------------------------------------------- #

def _quantiles(valeurs: Sequence[float]) -> list | None:
    v = sorted(x for x in valeurs if x is not None)
    if not v:
        return None
    return [v[int(q * (len(v) - 1))] for q in (0.25, 0.5, 0.75)]


def ic_blocs(blocs: Mapping[Any, Sequence[Any]],
             stat: Callable[[list], float | None],
             tirages: int = TIRAGES, graine: int = GRAINE
             ) -> list[float] | None:
    """Intervalle à 95 % de `stat`, en rééchantillonnant des BLOCS entiers
    (les signaux d'une même paire un même jour se ressemblent)."""
    cles = [k for k, v in blocs.items() if v]
    if len(cles) < 5:
        return None
    hasard = random.Random(graine)
    tirees = []
    for _ in range(tirages):
        echantillon: list = []
        for _ in cles:
            echantillon.extend(blocs[cles[hasard.randrange(len(cles))]])
        x = stat(echantillon)
        if x is not None:
            tirees.append(x)
    if len(tirees) < tirages // 2:
        return None
    tirees.sort()
    return [round(tirees[int(0.025 * len(tirees))], 4),
            round(tirees[int(0.975 * len(tirees)) - 1], 4)]


def _blocs(fiches: Sequence[Mapping], valeur: Callable[[Mapping], Any]
           ) -> dict:
    blocs: dict = {}
    for f in fiches:
        blocs.setdefault((f["paire"], f["jour"]), []).append(valeur(f))
    return blocs


def _part(liste: Sequence[Any]) -> float | None:
    v = [x for x in liste if x is not None]
    return sum(1 for x in v if x) / len(v) if v else None


def _moyenne(liste: Sequence[Any]) -> float | None:
    v = [x for x in liste if x is not None]
    return sum(v) / len(v) if v else None


def _ecart_groupes(fiches, groupe, valeur, stat=_part):
    """Différence de `stat(valeur)` entre groupe vrai et groupe faux, avec son
    intervalle par blocs."""
    def calcul(paires):
        a = [v for g, v in paires if g]
        b = [v for g, v in paires if g is False]
        sa, sb = stat(a), stat(b)
        return None if sa is None or sb is None else sa - sb
    paires = [(groupe(f), valeur(f)) for f in fiches]
    blocs = _blocs(fiches, lambda f: (groupe(f), valeur(f)))
    return {"ecart": None if calcul(paires) is None
            else round(calcul(paires), 4),
            "ic": ic_blocs(blocs, calcul)}


def resumer(fiches: Sequence[Mapping], temoins: Sequence[Mapping]) -> dict:
    """Les comparaisons de la phase F. `fiches` : un dictionnaire par signal
    (trajectoire + `paire`, `jour`, `issues_bougies`) ; `temoins` : les
    pseudo-contrats (trajectoire + `paire`, `jour`)."""
    sortie: dict = {"signaux": len(fiches), "temoins": len(temoins)}
    if not fiches:
        return sortie
    # 1. Accord des issues au tick et aux clôtures M1.
    accord = {}
    for sec in ECHEANCES:
        couples = [(f[f"issue_{sec}"], f["issues_bougies"].get(str(sec)))
                   for f in fiches]
        juges = [(a, b) for a, b in couples if a is not None
                 and b is not None]
        accord[str(sec)] = [len(juges), sum(1 for a, b in juges if a == b)]
    sortie["accord"] = accord
    # 2. Échéances sur les MÊMES signaux.
    communs = [f for f in fiches
               if all(f[f"issue_{s}"] is not None for s in ECHEANCES)]
    echeances = {"communs": len(communs)}
    for sec in ECHEANCES:
        gagnes = sum(1 for f in communs if f[f"issue_{sec}"])
        echeances[str(sec)] = [len(communs), gagnes]
    for sec in ECHEANCES[:-1]:
        def ecart(paires, sec=sec):
            if not paires:
                return None
            return sum(1.92 * (a - b) for a, b in paires) / len(paires)
        blocs = _blocs(communs, lambda f, sec=sec: (
            int(f[f"issue_{sec}"]), int(f["issue_900"])))
        tout = [x for v in blocs.values() for x in v]
        echeances[f"{sec}-900"] = {
            "ecart_ev": round(ecart(tout), 4) if tout else None,
            "ic": ic_blocs(blocs, ecart)}
    sortie["echeances"] = echeances
    # 3. Trajectoires, gagnants contre perdants (issue à 15 min au tick).
    groupes = {"gagnants": [f for f in fiches if f["issue_900"] is True],
               "perdants": [f for f in fiches if f["issue_900"] is False]}
    traj = {}
    for nom, liste in groupes.items():
        traj[nom] = {
            "n": len(liste),
            **{cle: _quantiles([f[cle] for f in liste])
               for cle in ("mfe", "mae", "t_mfe", "t_mae")},
            **{f"pos_{s}": _quantiles([f[f"pos_{s}"] for f in liste])
               for s in POSITIONS_SEC}}
    traj["perdants_passes_gagnants"] = _part(
        [f["a_ete_gagnant"] for f in groupes["perdants"]])
    juges = [f for f in fiches if f["issue_900"] is not None]
    traj["mfe_gagnants_moins_perdants"] = _ecart_groupes(
        juges, lambda f: f["issue_900"], lambda f: f["mfe"], _moyenne)
    sortie["trajectoires"] = traj
    # 4. Fin d'échéance : perdants contre témoins perdants, et la loi.
    perdants = groupes["perdants"]
    temoins_perdants = [t for t in temoins if t["issue_900"] is False]
    fin = {"perdants": len(perdants),
           "temoins_perdants": len(temoins_perdants)}
    for f_sec in FINS_SEC:
        seuil = DUREE_SEC - f_sec

        def dans_la_fin(f, seuil=seuil):
            d = f["dernier_passage"]
            return None if d is None else d >= seuil
        signal = _part([dans_la_fin(f) for f in perdants])
        temoin = _part([dans_la_fin(t) for t in temoins_perdants])
        fin[str(f_sec)] = {
            "signal": signal, "temoin": temoin,
            "arcsinus": round(arcsinus_fin(f_sec), 4),
            "ic_signal": ic_blocs(
                _blocs(perdants, dans_la_fin), _part),
            "ic_temoin": ic_blocs(
                _blocs(temoins_perdants, dans_la_fin), _part)}
    sortie["fin_echeance"] = fin
    # 5. Franchissements de la zone.
    avec_niveau = [f for f in juges if f.get("franchi") is not None]
    franch = {"n": len(avec_niveau)}
    for nom, issue in (("gagnants", True), ("perdants", False)):
        liste = [f for f in avec_niveau if f["issue_900"] is issue]
        franchis = [f for f in liste if f["franchi"]]
        franch[nom] = {
            "n": len(liste), "franchis": len(franchis),
            "profondeur": _quantiles([f["profondeur"] for f in franchis]),
            "temps_au_dela": _quantiles([f["temps_au_dela"]
                                         for f in franchis]),
            "retour": _part([f["retour"] for f in franchis]),
            "delai_retour": _quantiles([f["delai_retour"]
                                        for f in franchis])}
    franch["franchi_perdants_moins_gagnants"] = _ecart_groupes(
        avec_niveau, lambda f: f["issue_900"] is False,
        lambda f: f["franchi"])
    sortie["franchissements"] = franch
    return sortie
