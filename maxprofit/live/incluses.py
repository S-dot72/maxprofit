"""
`/incluses` : l'épreuve de la piste H_IB, les bougies incluses, gelée le 03/11.
Voir `docs/preinscription_incluses.md` ; empreinte
ccc23575fbe3ce090a2d09a5409440545b7c2fccb89c5c2851bc29dec3d36b1e. En
LECTURE SEULE.

La piste : moins il y a de bougies incluses parmi les 50 bougies M1 avant
le signal, plus le signal ZoneH1 gagne. Deux épreuves, fixées par le gel :
1. **la réplication sur la quarantaine** (28/10 au 03/11) : un calcul,
   rapporté, sans pouvoir de verdict ;
2. **la confirmation**, à partir du 04/11 00:00 UTC, avec des regards à
   300, 600 et 900 signaux favorables et jouables à issue décisive. Rien
   n'est montré avant le premier regard : ni taux, ni issue dans les
   fichiers.

Les signaux sont RECALCULÉS sur les bougies de la période par
`exemples_historiques`, la fonction du rejeu quotidien, avec la stratégie
de la course. Le rejeu en mémoire ne couvre que 30 jours, alors que le
dernier regard tombe vers janvier.

Paramètres : ceux du gel, inscrits ici et vérifiés par leur empreinte
(`etude_micro.exiger_le_gel`). Un test vérifie qu'ils sont identiques au
document commité. Sorties en CSV seulement, aucune écriture en base.
"""

from __future__ import annotations

import html
import json
import random
import time
from collections import Counter
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Any, Callable, Mapping, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.apprentissage.historique import exemples_historiques
from maxprofit.core.types import Direction
from maxprofit.live import bougies_entree as be
from maxprofit.live import etude3
from maxprofit.live.etude3 import ConnexionTracee, etat_lecture_seule
from maxprofit.live.rapprochement import PAYOUT_AVANT
from maxprofit.store.market import MarketReader

#: Le document gelé, à l'identique (`docs/preinscription_incluses.json`).
PARAMETRES: dict[str, Any] = json.loads("""
{
  "confirmation": {
    "abandonnee": "à un regard, borne haute de l'IC 95 % de C2 sous 52,0833 % ou borne haute de C1 sous 0",
    "arret": "si la règle de payout du broker ou la stratégie ZoneH1 change, le regard suivant devient final",
    "confirmee": "C1 et C2 rejetées par Holm au seuil nominal du regard",
    "indeterminee": "au dernier regard, ni confirmée ni abandonnée",
    "ouverture_utc": "2026-11-04T00:00:00Z",
    "regard": "le regard N porte sur toutes les décisions jusqu'à celle du N-ième signal favorable et jouable, dans l'ordre chronologique ; aucun résultat n'est montré avant le premier regard",
    "regards_favorables_jouables": [
      300,
      600,
      900
    ],
    "seuils": "O'Brien-Fleming (Lan-DeMets), incrément de dépense à chaque regard (etude_micro.seuils_des_regards)"
  },
  "criteres": {
    "C1": "taux de gain du groupe favorable − taux du groupe défavorable > 0 (toutes paires, tous payouts)",
    "C2": "taux de gain du groupe favorable ET jouable > 52,0833 %",
    "famille": "Holm sur C1 et C2, alpha de famille 5 % unilatéral, au seuil nominal du regard",
    "incertitude": "bootstrap par blocs paire × jour, 1 000 tirages, graine 7 ; IC 95 % percentile ; p unilatérale par l'approximation normale de l'écart-type bootstrap"
  },
  "groupes": {
    "borne_basse": 5,
    "borne_haute": 6,
    "defavorable": "incluse_50 >= 6",
    "exclu": "incluse_50 = 5",
    "favorable": "incluse_50 < 5"
  },
  "interdits": [
    "aucune modification des seuils, de la population, de l'issue ni des regards après le gel",
    "aucun résultat de la confirmation consulté hors des regards",
    "aucune règle de la course modifiée par cette pré-inscription"
  ],
  "issue": {
    "definition": "clôture de la bougie complète qui finit 900 s après la décision, face à la clôture de la bougie du signal, dans le sens du trade",
    "egalite": "exclue et comptée",
    "horizon_sec": 900,
    "manquante": "exclue et comptée"
  },
  "mesure": {
    "definition": "nombre de bougies M1 dont le plus haut est inférieur ou égal au plus haut de la précédente ET le plus bas supérieur ou égal au plus bas de la précédente, parmi les 50 bougies M1 complètes et contiguës qui finissent à la bougie du signal, celle-ci comprise ; la première est comparée à la bougie qui la précède si elle est contiguë",
    "fenetre_bougies": 50,
    "fenetre_incomplete": "signal exclu et compté",
    "nom": "incluse_50"
  },
  "nom": "H_IB — moins de bougies incluses avant l'entrée, plus de gains",
  "origine": "/bougies du 03/11 : e_incluse_50, groupe et sens choisis sur l'exploration (10/10-21/10 : 58,2 % contre 51,1 %), jugés sur la validation (21/10-28/10 : 60,5 % contre 45,0 %, p = 0,0007), seule découverte au BH de 10 % parmi 64 mesures distinctes. Piste née des données : elle n'est pas une règle.",
  "payout": {
    "flux_min": 84,
    "jouable": "payout du flux du dernier relevé strictement avant la décision >= 84 (appliqué : 92 %)",
    "point_mort": 0.520833
  },
  "puissance": {
    "effet_de_plan": 1.6,
    "rythme_favorables_jouables_par_jour": 14.6,
    "signaux_necessaires_C2": {
      "57 %": 1003,
      "58 %": 689,
      "60 %": 379
    }
  },
  "redige_le": "2026-11-03",
  "replication_quarantaine": {
    "debut_utc": "2026-10-28T00:00:00Z",
    "fin_utc": "2026-11-03T00:00:00Z",
    "role": "réplication immédiate sur des données jamais examinées sous cet angle ; un seul calcul, Holm à 5 % ; rapportée, sans pouvoir de verdict"
  },
  "signaux": {
    "entree": "immédiate : clôture de la bougie du signal, quel que soit le mode de confirmation de la course",
    "justification_entree": "mode 0 de la course ; /rapprochement du 03/11 : même issue que le broker pour 81 ordres sur 86",
    "source": "ZoneH1 du rejeu, stratégie et paramètres de production, recalculés sur les bougies de la période"
  },
  "version": 1
}
""")
EMPREINTE = "ccc23575fbe3ce090a2d09a5409440545b7c2fccb89c5c2851bc29dec3d36b1e"


def _instant(texte: str) -> int:
    return int(datetime.strptime(texte, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp())


QUARANTAINE = (_instant(PARAMETRES["replication_quarantaine"]["debut_utc"]),
               _instant(PARAMETRES["replication_quarantaine"]["fin_utc"]))
OUVERTURE = _instant(PARAMETRES["confirmation"]["ouverture_utc"])
REGARDS: tuple[int, ...] = tuple(
    PARAMETRES["confirmation"]["regards_favorables_jouables"])
BORNE_BASSE = PARAMETRES["groupes"]["borne_basse"]
BORNE_HAUTE = PARAMETRES["groupes"]["borne_haute"]
FLUX_MIN = PARAMETRES["payout"]["flux_min"]
POINT_MORT = 1 / 1.92
HORIZON = PARAMETRES["issue"]["horizon_sec"]
ALPHA = em.ALPHA_FAMILLE
RYTHME = PARAMETRES["puissance"]["rythme_favorables_jouables_par_jour"]
FAVORABLE, DEFAVORABLE, EXCLU = "favorable", "défavorable", "exclu (= 5)"
RESPIRATION_SEC = 0.02
_N = NormalDist()


def groupe(incluses: float) -> str:
    if incluses < BORNE_BASSE:
        return FAVORABLE
    return DEFAVORABLE if incluses >= BORNE_HAUTE else EXCLU


class _Temoin:
    """Enveloppe la stratégie pour noter le SENS de chaque signal : sans le
    contexte (coûteux, inutile ici), `exemples_historiques` ne le garde
    pas. Elle ne change rien à ce que la stratégie décide."""

    def __init__(self, strategie) -> None:
        self.p = strategie.p
        self._s = strategie
        self.sens: dict[int, bool] = {}

    def on_bar(self, vue):
        signal = self._s.on_bar(vue)
        if signal is not None:
            self.sens[vue.now_ms // 1000] = signal.direction is Direction.CALL
        return signal


def signaux_de_la_paire(paire: str, bougies: Sequence, strategie,
                        garde: Callable, debut: int, fin: int
                        ) -> list[dict[str, Any]]:
    """Les signaux ZoneH1 dont la décision tombe dans [debut, fin[, avec
    leur mesure, leur groupe et leur issue. Les décisions, la fenêtre et la
    garde de calibration sont celles du rejeu quotidien."""
    temoin = _Temoin(strategie)
    sans_issue: list[dict] = []
    exemples = exemples_historiques(
        {paire: bougies}, temoin, echeance_sec=HORIZON, garde=garde,
        avec_contexte=False, sans_issue=sans_issue)
    decisions = sorted({e.ts_sec for e in exemples}
                       | {int(s["ts_sec"]) for s in sans_issue})
    index = {b.ts_sec: i for i, b in enumerate(bougies)}
    sortie = []
    for d in decisions:
        if not debut <= d < fin:
            continue
        call = temoin.sens.get(d)
        ligne: dict[str, Any] = {
            "paire": paire, "decision": d,
            "decision_utc": datetime.fromtimestamp(d, timezone.utc).strftime(
                "%Y-%m-%d %H:%M"),
            "sens": "" if call is None else ("call" if call else "put"),
            "exclusion": ""}
        i = index.get(d - 60)
        if call is None or i is None:
            ligne["exclusion"] = "signal sans bougie ou sans sens"
            sortie.append(ligne)
            continue
        m = be.mesures(bougies, i, call)
        if m is None:
            ligne["exclusion"] = "50 bougies incomplètes ou trouées"
            sortie.append(ligne)
            continue
        ligne["incluse_50"] = int(m["incluse_50"])
        ligne["groupe"] = groupe(m["incluse_50"])
        s = index.get(bougies[i].ts_sec + HORIZON)
        if s is None:
            ligne["issue"] = em.MANQUANTE
        elif bougies[s].close == bougies[i].close:
            ligne["issue"] = em.EGALITE
        else:
            ligne["issue"] = (em.GAIN if (bougies[s].close > bougies[i].close)
                              == call else em.PERTE)
        sortie.append(ligne)
    return sortie


def _bloc(r: Mapping) -> tuple[str, int]:
    return (r["paire"], int(r["decision"]) // 86400)


def taux_contre_seuil(lignes: Sequence[Mapping], seuil: float,
                      tirages: int = em.TIRAGES, graine: int = em.GRAINE
                      ) -> dict[str, Any]:
    """Taux de gain − seuil, IC 95 % percentile et p unilatérale (approx.
    normale de l'écart-type bootstrap), blocs paire × jour."""
    blocs: dict[Any, list[int]] = {}
    for r in lignes:
        b = blocs.setdefault(_bloc(r), [0, 0])
        b[0] += r["issue"] == em.GAIN
        b[1] += 1
    valeurs = list(blocs.values())
    n = sum(v[1] for v in valeurs)
    sortie: dict[str, Any] = {"n": n, "taux": None, "ecart": None,
                              "bas": None, "haut": None, "p": None}
    if not n:
        return sortie
    taux = sum(v[0] for v in valeurs) / n
    sortie.update(taux=taux, ecart=taux - seuil)
    if len(valeurs) < 2:
        return sortie
    alea = random.Random(graine)
    k = len(valeurs)
    tirs = []
    for _ in range(tirages):
        g = m = 0
        for i in alea.choices(range(k), k=k):
            g += valeurs[i][0]
            m += valeurs[i][1]
        tirs.append(g / m - seuil)
    tirs.sort()
    moyenne = sum(tirs) / len(tirs)
    et = (sum((t - moyenne) ** 2 for t in tirs) / (len(tirs) - 1)) ** 0.5
    sortie.update(bas=tirs[int(0.025 * len(tirs))],
                  haut=tirs[int(0.975 * len(tirs)) - 1],
                  p=(1 - _N.cdf(sortie["ecart"] / et)) if et > 0
                  else (0.0 if sortie["ecart"] > 0 else 1.0))
    return sortie


def epreuve(lignes: Sequence[Mapping], alpha: float) -> dict[str, Any]:
    """C1, C2 et Holm au seuil `alpha`, sur les signaux à issue décisive."""
    decisifs = [r for r in lignes if not r["exclusion"]
                and r["issue"] in (em.GAIN, em.PERTE)]
    c1 = be.ecart(decisifs, lambda r: {FAVORABLE: 1, DEFAVORABLE: 0}.get(
        r["groupe"]))
    c2 = taux_contre_seuil([r for r in decisifs if r["groupe"] == FAVORABLE
                            and r.get("jouable")], POINT_MORT)
    p = {k: v["p"] for k, v in (("C1", c1), ("C2", c2)) if v["p"] is not None}
    rejet = em.holm(p, alpha) if len(p) == 2 else {"C1": False, "C2": False}
    abandon = ((c2["haut"] is not None and c2["haut"] < 0)
               or (c1["haut"] is not None and c1["haut"] < 0))
    return {"C1": c1, "C2": c2, "rejet": rejet, "alpha": alpha,
            "decisifs": len(decisifs), "abandon": abandon,
            "confirmee": all(rejet.get(k) for k in ("C1", "C2"))}


def regards_atteints(confirmation: Sequence[Mapping]
                     ) -> list[tuple[int, int, dict]]:
    """(N, décision du N-ième signal favorable et jouable décisif, épreuve)
    pour chaque regard atteint, jusqu'au premier verdict."""
    cles = sorted(confirmation, key=lambda r: (r["decision"], r["paire"]))
    favorables = [r for r in cles if not r["exclusion"]
                  and r["groupe"] == FAVORABLE and r.get("jouable")
                  and r["issue"] in (em.GAIN, em.PERTE)]
    seuils = {s["N"]: s["nominal"] for s in em.seuils_des_regards(REGARDS,
                                                                    ALPHA)}
    sortie = []
    for n in REGARDS:
        if len(favorables) < n:
            break
        coupure = favorables[n - 1]["decision"]
        resultat = epreuve([r for r in cles if r["decision"] <= coupure],
                           seuils[n])
        sortie.append((n, coupure, resultat))
        if resultat["confirmee"] or resultat["abandon"]:
            break
    return sortie


def verdict(n: int, r: Mapping) -> str:
    if r["confirmee"]:
        return "CONFIRMÉE"
    if r["abandon"]:
        return "ABANDONNÉE"
    return "INDÉTERMINÉE (dernier regard)" if n == REGARDS[-1] else \
        "poursuite jusqu'au regard suivant"


# --------------------------------------------------------------------------- #
# La passe
# --------------------------------------------------------------------------- #

def epreuves(lue_brute, active_brute, course, *, nom_lecture: str = "relève",
             maintenant: float | None = None, garde: Callable | None = None,
             respirer: Callable[[], None] | None = None
             ) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers). Les bases sont déjà en lecture seule."""
    em.exiger_le_gel(PARAMETRES, EMPREINTE)
    maintenant = time.time() if maintenant is None else maintenant
    respirer = respirer or (lambda: time.sleep(RESPIRATION_SEC))
    t0 = time.monotonic()
    strategie_course = getattr(course, "strategie", None)
    if strategie_course is None:
        return "🔬 Course indisponible : la stratégie n'est pas chargée.", []
    strategie = type(strategie_course)(strategie_course.p)
    if garde is None:
        from maxprofit.live.plan_demo import dans_la_plage_de_calibration
        tolerance = strategie.p.tolerance_pct

        def garde(p, f):
            return dans_la_plage_de_calibration(tolerance, f)
    fin_mure = int(maintenant) - HORIZON - em.MARGE_MATURITE_SEC
    fin = max(QUARANTAINE[1], fin_mure)
    lecture_debut = QUARANTAINE[0] - 86400
    lecture_fin = fin // 86400 * 86400 + 2 * 86400
    lue = ConnexionTracee(lue_brute)
    active = ConnexionTracee(active_brute) \
        if active_brute is not None and active_brute is not lue_brute \
        else None
    lecture = {nom_lecture: etat_lecture_seule(lue)}
    if active is not None:
        lecture["active"] = etat_lecture_seule(active)
    divergents, copie = be.verifier_la_copie(lue, active, lecture_debut,
                                             lecture_fin)
    lecteurs = {"lue": MarketReader(lue),
                "active": MarketReader(active) if active is not None
                else None}
    paires = tuple(getattr(course, "paires_collectees", ()) or ()) or \
        tuple(getattr(course, "paires", ()) or ())
    lignes: list[dict[str, Any]] = []
    for paire in paires:
        bougies = be.bougies_de(paire, lecteurs, divergents, copie,
                                lecture_debut, lecture_fin, respirer)
        for r in signaux_de_la_paire(paire, bougies, strategie, garde,
                                     QUARANTAINE[0], fin):
            d = r["decision"]
            if QUARANTAINE[0] <= d < QUARANTAINE[1]:
                r["epreuve"] = "quarantaine"
            elif OUVERTURE <= d < fin_mure:
                r["epreuve"] = "confirmation"
            else:
                continue
            lignes.append(r)
        del bougies
        respirer()
    source = active if active is not None else lue
    for r in lignes:
        if r["exclusion"]:
            continue
        ligne = source.execute(PAYOUT_AVANT,
                               (r["paire"], r["decision"])).fetchone()
        r["payout_flux"] = None if not ligne or ligne[0] is None \
            else float(ligne[0])
        r["jouable"] = r["payout_flux"] is not None and \
            r["payout_flux"] >= FLUX_MIN
    quarantaine = [r for r in lignes if r["epreuve"] == "quarantaine"]
    confirmation = [r for r in lignes if r["epreuve"] == "confirmation"]
    replication = epreuve(quarantaine, ALPHA)
    regards = regards_atteints(confirmation)
    favorables = sum(1 for r in confirmation if not r["exclusion"]
                     and r["groupe"] == FAVORABLE and r.get("jouable")
                     and r["issue"] in (em.GAIN, em.PERTE))
    # Les issues de la confirmation au-delà du dernier regard restent
    # cachées, dans les fichiers comme dans le rapport.
    coupure = regards[-1][1] if regards else None
    for r in confirmation:
        if r.get("issue") and (coupure is None or r["decision"] > coupure):
            r["issue"] = "masquée jusqu'au prochain regard"
    rapport = texte(quarantaine, confirmation, replication, regards, {
        "nom": nom_lecture, "lecture": lecture, "copie": copie,
        "maintenant": maintenant, "favorables": favorables,
        "requetes": sum(lue.requetes.values()) + (
            sum(active.requetes.values()) if active is not None else 0),
        "refusees": lue.refusees + (active.refusees if active else 0),
        "temps": time.monotonic() - t0})
    return rapport, fichiers(lignes, replication, regards)


# --------------------------------------------------------------------------- #
# Le rapport et les fichiers
# --------------------------------------------------------------------------- #

def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f} %"


def _pts(x) -> str:
    return "—" if x is None else f"{100 * x:+.1f} pts"


def _lignes_epreuve(r: Mapping) -> list[str]:
    c1, c2 = r["C1"], r["C2"]
    ic1 = "" if c1["bas"] is None else \
        f" (IC {100 * c1['bas']:+.1f} à {100 * c1['haut']:+.1f}, p = {c1['p']:.4f})"
    ic2 = "" if c2["bas"] is None else \
        f" (IC {100 * c2['bas']:+.1f} à {100 * c2['haut']:+.1f}, p = {c2['p']:.4f})"
    marque = {True: "✅", False: "❌"}
    return [
        f"- C1, favorable contre défavorable : {_pct(c1['taux1'])} "
        f"({c1['n1']}) contre {_pct(c1['taux0'])} ({c1['n0']}) ; écart "
        f"{_pts(c1['ecart'])}{ic1} {marque[bool(r['rejet'].get('C1'))]}",
        f"- C2, favorable et jouable face au point mort de 52,08 % : "
        f"{_pct(c2['taux'])} sur {c2['n']} ; écart {_pts(c2['ecart'])}{ic2} "
        f"{marque[bool(r['rejet'].get('C2'))]}",
        f"- Holm au seuil {r['alpha']:.4f} : C1 "
        f"{'rejetée' if r['rejet'].get('C1') else 'non rejetée'}, C2 "
        f"{'rejetée' if r['rejet'].get('C2') else 'non rejetée'}"]


def texte(quarantaine, confirmation, replication, regards, meta) -> str:
    lu = meta["copie"]["lus"]
    exclus_q = Counter(r["exclusion"] for r in quarantaine if r["exclusion"])
    issues_q = Counter(r.get("issue") for r in quarantaine
                       if not r["exclusion"])
    l = ["🔬 <b>H_IB — les bougies incluses</b> (pré-inscription gelée, "
         f"empreinte {EMPREINTE[:8]}…{EMPREINTE[-7:]})",
         f"Bases : {html.escape(meta['nom'])} et active "
         f"({', '.join(f'{k} : {v}' for k, v in meta['lecture'].items())}) ; "
         f"requêtes refusées : {meta['refusees']}. Rien n'a été écrit en "
         f"base.",
         (f"Copie vérifiée par paire × jour : "
          f"{len(meta['copie']['divergences'])} divergence(s), lues sur "
          f"l'active ({lu.get('active', 0)} paires-jours sur "
          f"{sum(lu.values())})." if meta["copie"]["verifiee"]
          else "Copie NON vérifiée."),
         "Signaux ZoneH1 recalculés sur les bougies (fonction du rejeu) ; "
         "entrée immédiate ; favorable : moins de 5 bougies incluses sur "
         "50, défavorable : 6 ou plus.",
         "",
         "<b>1. Réplication sur la quarantaine</b> (28/10 → 03/11, sans "
         "pouvoir de verdict)",
         f"- {len(quarantaine)} signaux ; écartés : " + (" ; ".join(
             f"{html.escape(m)} {n}" for m, n in exclus_q.most_common())
             or "aucun") + " ; issues : " + " ; ".join(
             f"{k} {n}" for k, n in issues_q.most_common()),
         *_lignes_epreuve(replication),
         "",
         "<b>2. Confirmation</b> (depuis le 04/11 00:00 UTC)"]
    favorables = meta["favorables"]
    l.append(f"- {len(confirmation)} signaux mûrs ; favorables et jouables "
             f"à issue décisive : {favorables}")
    if not regards:
        reste = max(0, REGARDS[0] - favorables)
        date = datetime.fromtimestamp(
            meta["maintenant"] + reste / RYTHME * 86400, timezone.utc)
        l.append(f"- premier regard à {REGARDS[0]} : encore {reste}, vers le "
                 f"{date.strftime('%d/%m')} au rythme d'octobre. Aucun "
                 f"résultat n'est montré avant.")
    for n, coupure, r in regards:
        l.append(f"<i>Regard à {n}</i> (décisions jusqu'au "
                 f"{datetime.fromtimestamp(coupure, timezone.utc):%d/%m %H:%M}"
                 f" UTC) : <b>{verdict(n, r)}</b>")
        l += _lignes_epreuve(r)
    l += ["",
          "La course n'est pas modifiée. Une piste confirmée reste à "
          "décider avant toute mise en production.",
          f"Durée : {meta['temps']:.0f} s ; requêtes : {meta['requetes']}."]
    return "\n".join(l)


def _ligne_resultat(nom: str, r: Mapping, n: int | None = None,
                    coupure: int | None = None) -> dict[str, Any]:
    c1, c2 = r["C1"], r["C2"]
    return {"epreuve": nom, "regard": n, "coupure_utc": None if coupure is None
            else datetime.fromtimestamp(coupure, timezone.utc).strftime(
                "%Y-%m-%d %H:%M"),
            "alpha": r["alpha"], "decisifs": r["decisifs"],
            "c1_n_fav": c1["n1"], "c1_n_def": c1["n0"],
            "c1_taux_fav": c1["taux1"], "c1_taux_def": c1["taux0"],
            "c1_ecart": c1["ecart"], "c1_bas": c1["bas"],
            "c1_haut": c1["haut"], "c1_p": c1["p"],
            "c2_n": c2["n"], "c2_taux": c2["taux"], "c2_ecart": c2["ecart"],
            "c2_bas": c2["bas"], "c2_haut": c2["haut"], "c2_p": c2["p"],
            "holm_c1": r["rejet"].get("C1"), "holm_c2": r["rejet"].get("C2"),
            "verdict": "sans pouvoir de verdict" if n is None
            else verdict(n, r)}


def fichiers(lignes, replication, regards) -> list[tuple[bytes, str, str]]:
    resultats = [_ligne_resultat("quarantaine", replication)] + [
        _ligne_resultat("confirmation", r, n, c) for n, c, r in regards]
    colonnes = ["epreuve", "paire", "decision", "decision_utc", "sens",
                "exclusion", "incluse_50", "groupe", "payout_flux",
                "jouable", "issue"]
    return [
        (etude3._csv(sorted(lignes, key=lambda r: (r["decision"],
                                                   r["paire"])), colonnes),
         "incluses_signaux.csv",
         "Chaque signal des deux épreuves (issues de la confirmation "
         "masquées au-delà du dernier regard)"),
        (etude3._csv(resultats, list(resultats[0])), "incluses_resultats.csv",
         "C1, C2 et Holm : réplication sur la quarantaine, puis chaque "
         "regard atteint"),
    ]
