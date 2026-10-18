"""
Le protocole de simulation OTC, version 1 : EXP-001 à EXP-004.

--- D'où il vient ----------------------------------------------------------

Demandé le 2026-10-13 : un laboratoire reproductible, qui compare les
observations des paires OTC à une référence figée « sans confondre
découverte statistique et performance réelle ». Quatre expériences :

- EXP-001, qualité des données (`apprentissage.qualite`) ;
- EXP-002, information prédictive de RSI, MACD et ATR, sans en faire des
  règles (`apprentissage.indicateurs`, et les mêmes indicateurs à
  l'entrée ZoneH1 dans les expériences contrôlées) ;
- EXP-003, comparaison avec ZoneH1 : couverture, distribution des
  résultats, stabilité semaine par semaine ;
- EXP-004, validation indépendante : un GEL daté, après lequel plus rien
  n'est ajusté, et un test aveugle de `DUREE_TEST_AVEUGLE_JOURS` jours.

--- Les trois fenêtres -----------------------------------------------------

1. Recherche : avant le gel, les 70 % les plus anciens du rejeu
   (étalonnage des expériences).
2. Validation : avant le gel, les 30 % récents.
3. Test aveugle : du gel à gel + `DUREE_TEST_AVEUGLE_JOURS` jours. Les
   coupes des expériences et les seuils des indicateurs sont FIGÉS au
   premier rejeu après le gel ; le résultat final est figé à la fin de la
   fenêtre, et ne bouge plus ensuite même quand ces jours sortent du rejeu.

`DATE_GEL` (AAAA-MM-JJ, UTC) se fixe UNE fois, avant de regarder. La
déplacer après avoir vu un résultat reviendrait à choisir la fenêtre où il
est le meilleur — exactement ce que le protocole interdit.

--- Ce qui rend le rapport reproductible ------------------------------------

Le commit déployé, l'empreinte des règles jouées, les paramètres de chaque
mesure, la période rejouée, et l'empreinte SHA-256 des bougies lues ; et un
JOURNAL de toutes les hypothèses examinées, verdicts négatifs compris, d'un
rejeu à l'autre — sans lui, le nombre de comparaisons, et donc la part de
« découvertes » dues au hasard, est sous-estimé.

⚠ Rien ici ne modifie une règle de la course : ce sont des mesures.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from maxprofit.apprentissage.indicateurs import CASES, FENETRE_JOURS, PARIS
from maxprofit.apprentissage.lecons import Exemple, wilson
from maxprofit.apprentissage.qualite import LIBELLES_DRAPEAUX, SOURCE
from maxprofit.live.laboratoire import (
    N_MIN_VALIDATION, PART_ETALONNAGE, confirmes)
from maxprofit.live.preinscriptions import (
    DECLAREES_LE, PREINSCRIPTIONS, fenetre_pre_inscrite, juger, ouverture)
from maxprofit.live import validation_p2
from maxprofit.live.stabilite import (
    N_MIN_FENETRE, TIRAGES, intervalle_des_blocs, intervalle_par_blocs)

PAYOUT = 0.92
#: Taux à partir duquel un pari rapporte au payout de 92 % : 52,08 %.
SEUIL_RENTABLE = 1 / (1 + PAYOUT)
#: Le gel par défaut, fixé le 2026-10-13 en écrivant ce module — avant
#: d'avoir vu un seul résultat des indicateurs.
DATE_GEL_DEFAUT = "2026-10-14"
DUREE_TEST_AVEUGLE_JOURS = 14
#: Minutes contrôlées AVANT la bougie du signal : la fenêtre que lisent le
#: contexte et les indicateurs.
RECUL_QUALITE_SEC = 1800
#: Les variables d'EXP-002 versées aux expériences contrôlées.
VARIABLES_EXP002: tuple[str, ...] = ("rsi_sens", "macd_sens", "atr_pct")
#: Verdicts gardés par hypothèse dans le journal.
JOURNAL_MAX = 12
#: Les variables d'environnement où l'hébergeur annonce le commit — les
#: mêmes que `hosting.version`, que `live` ne peut pas importer.
VARIABLES_COMMIT = ("RENDER_GIT_COMMIT", "RAILWAY_GIT_COMMIT_SHA",
                    "FLY_MACHINE_VERSION", "SOURCE_COMMIT", "GIT_COMMIT")


def commit() -> str | None:
    for nom in VARIABLES_COMMIT:
        brut = os.environ.get(nom, "").strip()
        if brut:
            return brut[:7]
    return None


def date_de_gel() -> int | None:
    """`DATE_GEL` en secondes UTC (0 h), `None` si « aucune ». Une valeur
    illisible retombe sur le défaut plutôt que d'annuler le test aveugle."""
    brut = os.environ.get("DATE_GEL", DATE_GEL_DEFAUT).strip().lower()
    if brut in ("aucune", "aucun", "non", "0"):
        return None
    for texte in (brut, DATE_GEL_DEFAUT):
        try:
            jour = datetime.strptime(texte, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
            return int(jour.timestamp())
        except ValueError:
            continue
    return None


def marquer_qualite(exemples: Sequence[Exemple], qualite,
                    echeance_sec: int) -> None:
    """Pose `donnees_douteuses` (0 ou 1) sur chaque signal : sa fenêtre de
    lecture ou son chemin jusqu'à l'échéance de l'entrée traversait-il une
    minute absente ou marquée ? La clé suit le signal jusqu'à son entrée
    confirmée (`confirmes` recopie le contexte). Hors de `CARACTERISTIQUES`."""
    for e in exemples:
        debut = e.ts_sec - 60                 # la bougie du signal
        attente = int(e.contexte.get("attente_confirmation", 0) or 0)
        fin = debut + 60 * attente + echeance_sec
        e.contexte["donnees_douteuses"] = float(
            qualite.touche(e.pair, debut - RECUL_QUALITE_SEC, fin))


# --------------------------------------------------------------------------- #
# Calculs
# --------------------------------------------------------------------------- #

def _taux(n: int, g: int) -> float | None:
    return g / n if n else None


def _esperance(p: float) -> float:
    return p * PAYOUT - (1 - p)


def _verdict(periodes: Sequence[int], ic, temoin: bool = False) -> str:
    """Le verdict d'un pari (ou d'un témoin) sur toutes les bougies :
    stable sur les deux périodes ET intervalle par blocs."""
    na, ga, nr, gr = periodes
    if na < N_MIN_VALIDATION or nr < N_MIN_VALIDATION:
        return "indécise — trop peu d'observations"
    if not ic:
        return "indécise — trop peu de blocs paire × jour"
    ta, tr = ga / na, gr / nr
    if temoin:
        if ic and (ic[0] > 0.5 or ic[1] < 0.5):
            return "⚠ témoin loin de 50 % : mesure à examiner"
        return "✅ témoin compatible avec 50 %"
    if ic and ic[0] > SEUIL_RENTABLE and min(ta, tr) > SEUIL_RENTABLE:
        return "✅ au-dessus de 52,1 % sur les deux périodes"
    if ic and ic[1] < 1 - SEUIL_RENTABLE and max(ta, tr) < 1 - SEUIL_RENTABLE:
        return "↩ le pari INVERSE passerait 52,1 % sur les deux périodes"
    if ic and (ic[0] > 0.5 or ic[1] < 0.5) and (ta - 0.5) * (tr - 0.5) > 0:
        return "≈ information mesurable (≠ 50 %), sous le seuil rentable"
    return "❌ pas d'information établie (compatible avec 50 %)"


def _reunir(indicateurs, cles: Sequence[str], sec: str) -> dict:
    """Les cases `cles` réunies en une : comptes et blocs additionnés."""
    periodes = [0, 0, 0, 0]
    blocs: dict[tuple[str, int], list[int]] = {}
    for cle in cles:
        for i, v in enumerate(indicateurs.cases[cle][sec]["periodes"]):
            periodes[i] += v
        for b, (n, g) in indicateurs.blocs[cle][sec].items():
            somme = blocs.setdefault(b, [0, 0])
            somme[0] += n
            somme[1] += g
    return {"periodes": periodes, "blocs": blocs}


def _par_semaine(blocs: Mapping[tuple[str, int], Sequence[int]],
                 premier_jour: int) -> list[list[int]]:
    semaines: dict[int, list[int]] = {}
    for (_p, jour), (n, g) in blocs.items():
        s = semaines.setdefault((jour - premier_jour) // FENETRE_JOURS, [0, 0])
        s[0] += n
        s[1] += g
    if not semaines:
        return []
    return [semaines.get(k, [0, 0]) for k in range(max(semaines) + 1)]


def _stabilite(semaines: Sequence[Sequence[int]], n_min: int) -> dict:
    """Combien de semaines suffisamment fournies passent le seuil, et
    l'écart entre la meilleure et la pire."""
    taux = [g / n for n, g in semaines if n >= n_min]
    return {"jugees": len(taux),
            "au_dessus": sum(1 for t in taux if t > SEUIL_RENTABLE),
            "ecart": round(max(taux) - min(taux), 4) if len(taux) >= 2
            else None}


def _source(nom: str, blocs, periodes, jours: float, premier_jour: int,
            n_min_semaine: int, ic=None) -> dict:
    n = periodes[0] + periodes[2]
    g = periodes[1] + periodes[3]
    semaines = _par_semaine(blocs, premier_jour)
    if ic is None:
        ic = intervalle_des_blocs(list(blocs.values()))
    return {"nom": nom, "n": n, "g": g, "par_jour": round(n / jours, 1),
            "paires": len({p for (p, _j), (nb, _g) in blocs.items() if nb}),
            "ic": list(ic) if ic else None, "periodes": list(periodes),
            "semaines": semaines,
            "stabilite": _stabilite(semaines, n_min_semaine)}


def _blocs_exemples(liste: Sequence[Exemple]) -> dict:
    blocs: dict[tuple[str, int], list[int]] = {}
    for e in liste:
        b = blocs.setdefault((e.pair, e.ts_sec // 86400), [0, 0])
        b[0] += 1
        b[1] += int(e.gagne)
    return blocs


def _periodes(liste: Sequence[Exemple], coupure: int) -> list[int]:
    c = [0, 0, 0, 0]
    for e in liste:
        k = 0 if e.ts_sec < coupure else 2
        c[k] += 1
        c[k + 1] += int(e.gagne)
    return c


def _qualite_des_signaux(base: Sequence[Exemple], coupure: int) -> dict:
    propres = [e for e in base if not e.contexte.get("donnees_douteuses")]
    touchees = [e for e in base if e.contexte.get("donnees_douteuses")]
    return {"propres": _periodes(propres, coupure),
            "touchees": _periodes(touchees, coupure)}


def _test_aveugle(base: Sequence[Exemple], indicateurs, gel: int,
                  fin_fenetre: int, gele: Mapping, sec: str) -> dict:
    """Les résultats de la fenêtre aveugle, avec les paramètres figés."""
    apres = [e for e in base if gel <= e.ts_sec < fin_fenetre]
    n, g = len(apres), sum(e.gagne for e in apres)
    sortie: dict = {"reference": [n, g],
                    "wilson": list(wilson(g, n, 1.96)) if n else None,
                    "blocs": intervalle_par_blocs(apres), "variables": [],
                    "paris": []}
    for cle, (c1, c2, pire, libelle) in gele.get("variables", {}).items():
        def dans_pire(e, cle=cle, c1=c1, c2=c2, pire=pire):
            if cle not in e.contexte:
                return False
            x = e.contexte[cle]
            if c2 is None:                    # valeurs discrètes
                return x == c1
            return (0 if x < c1 else 1 if x < c2 else 2) == pire
        garde = [e for e in apres if not dans_pire(e)]
        sortie["variables"].append(
            {"cle": cle, "libelle": libelle,
             "sans_pire": [len(garde), sum(e.gagne for e in garde)]})
    jour_gel, jour_fin = gel // 86400, fin_fenetre // 86400
    for ident, _libelle, cles in PARIS:
        r = _reunir(indicateurs, cles, sec)
        blocs = {b: v for b, v in r["blocs"].items()
                 if jour_gel <= b[1] < jour_fin}
        n_p = sum(v[0] for v in blocs.values())
        g_p = sum(v[1] for v in blocs.values())
        ic = intervalle_des_blocs(list(blocs.values()))
        sortie["paris"].append({"id": ident, "n": n_p, "g": g_p,
                                "ic": list(ic) if ic else None,
                                "avant": gele.get("paris", {}).get(ident)})
    return sortie


def _preinscriptions(base: Sequence[Exemple], serie: Sequence[Exemple],
                     debut: int, fin: int) -> list[dict]:
    """P4 jugée sur SA fenêtre aveugle, [debut, fin), avec le critère écrit
    d'avance ; « avant » : ce que la même mesure donnait sur les jours
    rejoués qui précèdent la fenêtre. P2 a sa propre validation
    (`live.validation_p2`) depuis l'amendement du 14/10."""
    sortie = []
    for p in PREINSCRIPTIONS:
        if p.parametres.get("exclusions"):
            continue
        avant = [e for e in serie if e.ts_sec < debut]
        pop = [e for e in serie if debut <= e.ts_sec < fin]
        ref = None
        n, g = len(pop), sum(e.gagne for e in pop)
        ic = intervalle_par_blocs(pop)
        sortie.append({
            "id": p.ident, "nom": p.nom, "n": n, "g": g,
            "ic": list(ic) if ic else None,
            "avant": [len(avant), sum(e.gagne for e in avant)],
            "reference": ref, "verdict": juger(n, g, ic, p.n_min, ref)})
    return sortie


def _geler(base: Sequence[Exemple], experiences: Mapping, indicateurs,
           gel: int, fin: int, version: str, sec: str) -> dict:
    """Ce qui est figé au gel : les coupes et le pire tiers de chaque
    variable des expériences, la référence et les paris d'avant le gel."""
    variables = {}
    for v in experiences.get("variables", ()):
        if v.get("coupes"):
            c1, c2 = v["coupes"]
            variables[v["cle"]] = [c1, c2, v["pire"], v["libelle"]]
        elif v.get("valeurs"):
            variables[v["cle"]] = [v["valeurs"][v["pire"]], None, v["pire"],
                                   v["libelle"]]
    avant = [e for e in base if e.ts_sec < gel]
    jour_gel = gel // 86400
    paris = {}
    for ident, _l, cles in PARIS:
        r = _reunir(indicateurs, cles, sec)
        n = sum(v[0] for b, v in r["blocs"].items() if b[1] < jour_gel)
        g = sum(v[1] for b, v in r["blocs"].items() if b[1] < jour_gel)
        paris[ident] = [n, g]
    return {"date": gel, "pris_le": fin, "version_regles": version,
            "commit": commit(),
            "coupure_etalonnage": experiences.get("coupure"),
            "variables": variables,
            "reference": [len(avant), sum(e.gagne for e in avant)],
            "paris": paris}


def _journal(ancien: Mapping | None, hypotheses: Sequence[Sequence[str]],
             fin: int) -> dict:
    """Le journal, d'un rejeu à l'autre : chaque hypothèse examinée garde
    sa première date et ses derniers verdicts."""
    journal = {k: {"libelle": v["libelle"], "premiere": v["premiere"],
                   "verdicts": [list(x) for x in v["verdicts"]]}
               for k, v in ((ancien or {}).get("journal") or {}).items()}
    for ident, libelle, verdict in hypotheses:
        entree = journal.setdefault(
            ident, {"libelle": libelle, "premiere": fin, "verdicts": []})
        entree["libelle"] = libelle
        # Le symbole seul (✅ ❌ ≈ ↩ ⚠ indécise) : le journal est sauvé
        # avec l'état, et les phrases entières y pèseraient pour rien.
        code = verdict.split()[0] if verdict else "?"
        if not entree["verdicts"] or entree["verdicts"][-1][0] != fin:
            entree["verdicts"].append([fin, code])
        entree["verdicts"] = entree["verdicts"][-JOURNAL_MAX:]
    return journal


def protocole(exemples: Sequence[Exemple],
              en_direct: Callable[[Exemple], bool], fenetre: int,
              echeance_sec: int, *, qualite, indicateurs,
              exclusions: Mapping[str, int], couverture: Mapping | None,
              experiences: Mapping, ancien: Mapping | None,
              version_regles: str, version_experiences: int,
              debut: int, fin: int, gel: int | None,
              jours: int, serie: Sequence[Exemple] = (),
              paires: Sequence[str] = (),
              regle_heure: float | None = None,
              potentiel: Mapping | None = None,
              reference8: Mapping | None = None) -> dict:
    """Le rapport complet, en JSON. `ancien` : le protocole du rejeu
    précédent, d'où viennent le gel figé et le journal. `serie` : les
    signaux de la stratégie pré-inscrite P4 (`strategies.serie_rouge`),
    rejoués sur les mêmes bougies."""
    sec = str(echeance_sec if echeance_sec in indicateurs.echeances
              else indicateurs.echeances[-1])
    base = [e for e in sorted(confirmes(exemples, fenetre),
                              key=lambda e: e.ts_sec) if en_direct(e)]
    # La coupure de TEMPS (70 % de la période) que partagent la qualité,
    # les indicateurs et la persistance : ZoneH1 est coupée au même jour
    # ici, pour que les deux colonnes d'EXP-003 se comparent.
    coupure = indicateurs.coupure
    premier_jour = debut // 86400
    # Les débits « par jour » se comptent en jours de COLLECTE réelle : la
    # collecte n'écoutait parfois que la moitié du temps, et diviser par
    # des jours de calendrier divisait le débit d'autant.
    duree_jours = (qualite.jours_collectes(debut, fin)
                   or max(1.0, (fin - debut) / 86400))
    sortie: dict = {"debut": debut, "fin": fin, "coupure": coupure,
                    "echeance": int(sec), "population": len(base)}

    # EXP-001
    sortie["exp001"] = {
        **qualite.resultat(), "exclusions": dict(exclusions),
        "couverture": dict(couverture) if couverture else None,
        "signaux": _qualite_des_signaux(base, coupure)}
    sortie["exp001"].pop("seuils", None)

    # EXP-002 : toutes les bougies.
    res_ind = indicateurs.resultat()
    paris, temoins = [], []
    for ident, libelle, cles in PARIS:
        r = _reunir(indicateurs, cles, sec)
        ic = intervalle_des_blocs(list(r["blocs"].values()))
        paris.append({"id": ident, "libelle": libelle,
                      "periodes": r["periodes"],
                      "ic": list(ic) if ic else None,
                      "verdict": _verdict(r["periodes"], ic)})
    for cle, libelle, sens in CASES:
        if sens:
            continue
        r = _reunir(indicateurs, (cle,), sec)
        ic = intervalle_des_blocs(list(r["blocs"].values()))
        temoins.append({"id": cle, "libelle": libelle,
                        "periodes": r["periodes"],
                        "ic": list(ic) if ic else None,
                        "verdict": _verdict(r["periodes"], ic, True)})
    sortie["exp002"] = {
        "observees": res_ind["observees"], "paires": res_ind["paires"],
        "exclusions": res_ind["exclusions"], "deciles": res_ind["deciles"],
        "paris": paris, "temoins": temoins,
        # Les mêmes indicateurs à l'entrée ZoneH1 : apportent-ils plus ?
        "a_l_entree": [
            {"cle": v["cle"], "libelle": v["libelle"], "tiers": v["tiers"],
             "gradient": v["gradient"], "verdict": v["verdict"]}
            for v in experiences.get("variables", ())
            if v["cle"] in VARIABLES_EXP002]}

    # EXP-003 : ZoneH1 face aux indicateurs.
    n_min_semaine = 2 * N_MIN_FENETRE
    sources = [_source("ZoneH1 + confirmation (jouée)",
                       _blocs_exemples(base), _periodes(base, coupure),
                       duree_jours, premier_jour, n_min_semaine,
                       intervalle_par_blocs(base))]
    for ident, _libelle, cles in PARIS:
        r = _reunir(indicateurs, cles, sec)
        sources.append(_source(ident, r["blocs"], r["periodes"],
                               duree_jours, premier_jour, n_min_semaine))
    if serie:
        sources.append(_source("P4 série rouge (2 min)",
                               _blocs_exemples(serie),
                               _periodes(serie, coupure), duree_jours,
                               premier_jour, n_min_semaine,
                               intervalle_par_blocs(serie)))
    sortie["exp003"] = {"sources": sources, "fenetre_jours": FENETRE_JOURS}

    # EXP-004 : le gel et le test aveugle.
    exp004: dict = {"date": gel, "duree_jours": DUREE_TEST_AVEUGLE_JOURS}
    precedent = (ancien or {}).get("exp004") or {}
    if gel is None:
        exp004["note"] = "désactivé (DATE_GEL=aucune)"
    elif fin < gel:
        exp004["note"] = "pas encore commencé"
    else:
        gele = (precedent.get("gele")
                if precedent.get("date") == gel and precedent.get("gele")
                else _geler(base, experiences, indicateurs, gel, fin,
                            version_regles, sec))
        exp004["gele"] = gele
        fin_fenetre = gel + DUREE_TEST_AVEUGLE_JOURS * 86400
        if precedent.get("date") == gel and precedent.get("final"):
            exp004["final"] = precedent["final"]
        else:
            resultats = _test_aveugle(base, indicateurs, gel, fin_fenetre,
                                      gele, sec)
            if fin >= fin_fenetre:
                exp004["final"] = {**resultats, "fige_le": fin}
            else:
                exp004["en_cours"] = {
                    **resultats,
                    "jours": round((fin - gel) / 86400, 1)}
        exp004["regles_modifiees"] = gele["version_regles"] != version_regles
        exp004["etalonnage_apres_gel"] = bool(
            gele.get("coupure_etalonnage")
            and gele["coupure_etalonnage"] >= gel)
    # Les pré-inscriptions, sur LEUR fenêtre (écrite le jour de leur
    # déclaration, indépendante de DATE_GEL) ; figées à sa clôture.
    debut_p, fin_p = fenetre_pre_inscrite()
    pre_prec = precedent.get("preinscriptions") or {}
    if pre_prec.get("final"):
        pre = pre_prec
    else:
        pre = {"declarees_le": DECLAREES_LE, "debut": debut_p, "fin": fin_p,
               "ids": [p.ident for p in PREINSCRIPTIONS
                       if not p.parametres.get("exclusions")]}
        if fin >= debut_p:
            pre["resultats"] = _preinscriptions(base, serie, debut_p, fin_p)
            pre["jours"] = round((min(fin, fin_p) - debut_p) / 86400, 1)
            if fin >= fin_p:
                pre["final"] = True
                pre["fige_le"] = fin
    exp004["preinscriptions"] = pre
    # P2 : la validation d'EXP-020 C et D (amendement du 14/10).
    debut_v = ouverture()
    exp004["validation_p2"] = validation_p2.validation(
        base, precedent.get("validation_p2"), fin=fin,
        jours_collecte=(qualite.jours_collectes(debut_v, fin)
                        if fin > debut_v else 0.0),
        reference={"version_regles": version_regles,
                   "confirmation_m1": fenetre, "regle_heure": regle_heure,
                   "echeance_sec": echeance_sec,
                   "paires": sorted(paires)},
        jours_avant=max(0.0, (min(debut_v, fin) - debut) / 86400),
        gain=validation_p2.gain_minimal())
    sortie["exp004"] = exp004

    # Le journal des hypothèses.
    hypotheses = []
    for v in experiences.get("variables", ()):
        hypotheses.append((f"VAR {v['cle']}", v["libelle"], v["verdict"]))
    for e in experiences.get("elan", ()):
        hypotheses.append((f"ÉLAN ≥ {e['seuil']}",
                           f"écarter l'élan contre le trade au-delà de "
                           f"{e['seuil']}", e["verdict"]))
    if experiences.get("combine"):
        hypotheses.append(("EXP-020 E", "filtre combiné EXP-020",
                           experiences["combine"]["verdict"]))
    for p in paris:
        hypotheses.append((p["id"], p["libelle"], p["verdict"]))
    for t in temoins:
        hypotheses.append((f"TÉMOIN {t['id']}", t["libelle"], t["verdict"]))
    for r in pre.get("resultats", ()):
        hypotheses.append((r["id"], r["nom"], r["verdict"]))
    hypotheses.extend(validation_p2.hypotheses(exp004["validation_p2"]))
    # Les variantes de /potentiel (expiration, filtres, indicateurs) : des
    # comparaisons comme les autres, qui comptent au journal.
    from maxprofit.live.potentiel import hypotheses as hypotheses_potentiel
    hypotheses.extend(hypotheses_potentiel(potentiel))
    # La mission Reference 8 : une décision par hypothèse, au même journal.
    from maxprofit.live.reference8 import hypotheses as hypotheses_r8
    hypotheses.extend(hypotheses_r8(reference8))
    sortie["hypotheses"] = [list(h) for h in hypotheses]
    sortie["journal"] = _journal(ancien, hypotheses, fin)

    # De quoi reproduire.
    sortie["reproductibilite"] = {
        "commit": commit(), "version_regles": version_regles,
        "version_experiences": version_experiences, "source": SOURCE,
        "periode": [debut, fin], "jours": jours,
        "empreinte": qualite.empreinte(),
        "parametres": {
            "qualite": qualite.resultat()["seuils"],
            "indicateurs": res_ind["seuils"],
            "part_etalonnage": PART_ETALONNAGE,
            "bootstrap": {"blocs": "paire × jour", "tirages": TIRAGES,
                          "graine": 7},
            "echeance_sec": int(sec), "confirmation_m1": fenetre,
            "gel": gel, "test_aveugle_jours": DUREE_TEST_AVEUGLE_JOURS}}
    return sortie


# --------------------------------------------------------------------------- #
# Texte
# --------------------------------------------------------------------------- #

def _jour(ts: int) -> str:
    return time.strftime("%d/%m", time.gmtime(ts))


def _jour_iso(date: str) -> str:
    """« 2026-10-13 » → « 13/10 »."""
    return f"{date[8:10]}/{date[5:7]}"


def _pct(n: int, g: int) -> str:
    return f"{g / n:.1%}" if n else "—"


def _ic(ic) -> str:
    return f"{ic[0]:.1%}–{ic[1]:.1%}" if ic else "trop peu de blocs"


def _ligne_periodes(p: Sequence[int]) -> str:
    return (f"étalonnage {_pct(p[0], p[1])} ({p[0]}) · récente "
            f"{_pct(p[2], p[3])} ({p[2]})")


def texte(resultat: Mapping | None) -> str:
    """`/protocole`."""
    if not resultat:
        return ("🧪 Protocole OTC pas encore calculé : il accompagne le rejeu "
                "(quelques minutes après un redémarrage).")
    r = resultat
    lignes = [f"🧪 <b>Protocole de simulation OTC v1</b> — rejeu du "
              f"{_jour(r['debut'])} au {_jour(r['fin'])}, échéance "
              f"{r['echeance'] // 60} min, coupure étalonnage/récente le "
              f"{_jour(r['coupure'])}", ""]

    # EXP-001
    q = r["exp001"]
    lignes.append("<b>EXP-001 — qualité des données</b>")
    lignes.append(f"Source : {q['source']}. Doublons : impossibles (clé "
                  f"primaire). Horodatage et OHLC : contrôlés à la lecture, "
                  f"une bougie incohérente est refusée.")
    lues = q["periodes"]["lues"]
    ok = q["periodes"]["ok"]
    lignes.append(f"Bougies lues : {sum(lues)}, saines "
                  f"{sum(ok) / max(1, sum(lues)):.1%} (étalonnage "
                  f"{_pct(lues[0], ok[0])}, récente {_pct(lues[1], ok[1])}).")
    for d, libelle in LIBELLES_DRAPEAUX.items():
        n = sum(q["periodes"].get(d, [0, 0]))
        if n:
            lignes.append(f"  {libelle} : {n}")
    trous = sum(p["trous"] for p in q["paires"].values())
    manquantes = sum(p["manquantes"] for p in q["paires"].values())
    if trous:
        pire = max(q["paires"].items(), key=lambda kv: kv[1]["manquantes"])
        lignes.append(f"  trous de collecte : {trous} ({manquantes} minutes "
                      f"absentes) ; le plus touché : "
                      f"{pire[0].replace('_otc', '')} "
                      f"({pire[1]['manquantes']} min, plus long "
                      f"{pire[1]['plus_long_trou']} min)")
    debit = [p.replace("_otc", "") for p, d in q["paires"].items()
             if d["jours_debit_anormal"]]
    if debit:
        lignes.append(f"  débit de ticks anormal certains jours "
                      f"(changement de flux ?) : {', '.join(debit)}")
    c = q.get("couverture")
    if c and c.get("fenetre_sec"):
        lignes.append(f"  collecteur à l'écoute {c['part']:.1%} du temps, "
                      f"{c['interruptions']} interruption(s), la plus longue "
                      f"{c['plus_long_trou_sec'] / 3600:.1f} h")
    if q["exclusions"]:
        lignes.append("  écartés par le rejeu : " + " · ".join(
            f"{motif} {n}" for motif, n in sorted(q["exclusions"].items())))
    s = q["signaux"]
    pr, to = s["propres"], s["touchees"]
    if pr[0] + pr[2] + to[0] + to[2]:
        lignes.append(
            f"  entrées jouées dont la fenêtre ou le chemin traverse une "
            f"donnée douteuse : {to[0] + to[2]} sur "
            f"{pr[0] + pr[2] + to[0] + to[2]} — gagnent "
            f"{_pct(to[0] + to[2], to[1] + to[3])}, contre "
            f"{_pct(pr[0] + pr[2], pr[1] + pr[3])} pour les autres")
    lignes.append("")

    # EXP-002
    e2 = r["exp002"]
    lignes.append(f"<b>EXP-002 — RSI, MACD, ATR</b> sur toutes les bougies "
                  f"({e2['observees']} observées ; seuils fixés d'avance, "
                  f"jamais ajustés ; seuil rentable 52,1 %)")
    for p in e2["paris"]:
        n = p["periodes"][0] + p["periodes"][2]
        g = p["periodes"][1] + p["periodes"][3]
        lignes.append(f"  {p['id']} — {p['libelle']} : <b>{_pct(n, g)}</b> "
                      f"sur {n} · par blocs {_ic(p['ic'])} · "
                      f"{_ligne_periodes(p['periodes'])} → {p['verdict']}")
    for t in e2["temoins"]:
        n = t["periodes"][0] + t["periodes"][2]
        g = t["periodes"][1] + t["periodes"][3]
        lignes.append(f"  {t['libelle']} : hausses {_pct(n, g)} sur {n} · "
                      f"{_ic(t['ic'])} → {t['verdict']}")
    deciles = [f"{10 * i}–{10 * i + 10} : {_pct(d[0] + d[2], d[1] + d[3])}"
               for i, d in enumerate(e2["deciles"]) if d[0] + d[2] >= 30]
    if deciles:
        lignes.append("  part des hausses par décile de RSI (distribution "
                      "descriptive, sans intervalle — pas un verdict) : "
                      + " · ".join(deciles))
    if e2["a_l_entree"]:
        lignes.append("  À l'entrée ZoneH1 (information EN PLUS de "
                      "ZoneH1 ? mêmes règles que /experiences) :")
        for v in e2["a_l_entree"]:
            tiers = " / ".join(
                f"{t['libelle']} {_pct(c[0] + c[2], c[1] + c[3])}"
                for t in v["tiers"] for c in (t["compte"],))
            pente = " · gradient stable" if v["gradient"] else ""
            lignes.append(f"    {v['libelle']} : {tiers}{pente} → "
                          f"{v['verdict']}")
    lignes.append("")

    # EXP-003
    e3 = r["exp003"]
    lignes.append(f"<b>EXP-003 — ZoneH1 face aux indicateurs</b> (semaines "
                  f"de {e3['fenetre_jours']} jours fixées d'avance)")
    for s3 in e3["sources"]:
        st = s3["stabilite"]
        semaines = " ".join(_pct(n, g) if n else "—"
                            for n, g in s3["semaines"])
        stab = (f"{st['au_dessus']}/{st['jugees']} semaines au-dessus de "
                f"52,1 %" + (f", écart {st['ecart']:.0%}"
                             if st["ecart"] is not None else "")
                if st["jugees"] else "aucune semaine assez fournie")
        lignes.append(f"  {s3['nom']} : <b>{_pct(s3['n'], s3['g'])}</b> · "
                      f"{_ic(s3['ic'])} · espérance "
                      f"{_esperance(s3['g'] / s3['n']):+.3f}/ordre · "
                      f"{s3['par_jour']}/jour · {s3['paires']} paires · "
                      f"semaines {semaines} · {stab}"
                      if s3["n"] else f"  {s3['nom']} : aucune observation")
    lignes.append("")

    # EXP-004
    e4 = r["exp004"]
    lignes.append("<b>EXP-004 — validation indépendante</b>")
    if e4.get("date") is None or "note" in e4:
        lignes.append(f"  Gel du "
                      f"{_jour(e4['date']) if e4.get('date') else '—'} : "
                      f"{e4.get('note', '')}. Recherche = 70 % anciens, "
                      f"validation = 30 % récents, test aveugle = "
                      f"{e4['duree_jours']} jours après le gel.")
    else:
        gele = e4["gele"]
        n0, g0 = gele["reference"]
        lignes.append(f"  Gel le {_jour(e4['date'])} (figé au rejeu du "
                      f"{_jour(gele['pris_le'])}, règles "
                      f"{gele['version_regles']}) : avant le gel, la "
                      f"référence gagnait {_pct(n0, g0)} sur {n0}.")
        if e4.get("regles_modifiees"):
            lignes.append("  ⚠ Les règles jouées ont changé depuis le gel : "
                          "le test n'est plus aveugle pour elles.")
        if e4.get("etalonnage_apres_gel"):
            lignes.append("  ⚠ Coupes figées sur un étalonnage qui déborde "
                          "après le gel.")
        res = e4.get("final") or e4.get("en_cours") or {}
        etat = ("résultat FINAL" if e4.get("final") else
                f"en cours, {res.get('jours', 0)} j sur {e4['duree_jours']}")
        n1, g1 = res.get("reference", [0, 0])
        lignes.append(f"  Test aveugle ({etat}) : référence "
                      f"<b>{_pct(n1, g1)}</b> sur {n1}"
                      + (f" · par blocs {_ic(res.get('blocs'))}"
                         if res.get("blocs") else ""))
        for v in res.get("variables", ()):
            n2, g2 = v["sans_pire"]
            if n1 and n2:
                lignes.append(f"    sans le pire tiers figé de "
                              f"« {v['libelle']} » : {_pct(n2, g2)} sur {n2}")
        for p in res.get("paris", ()):
            av = p.get("avant") or [0, 0]
            lignes.append(f"    {p['id']} : {_pct(p['n'], p['g'])} sur "
                          f"{p['n']} (avant le gel {_pct(av[0], av[1])})")
    pre = e4.get("preinscriptions") or {}
    if pre:
        ids = ", ".join(pre.get("ids", ()))
        fenetre = f"{_jour(pre['debut'])} → {_jour(pre['fin'])}"
        if not pre.get("resultats"):
            lignes.append(f"  Pré-inscrites le {_jour_iso(pre['declarees_le'])}"
                          f" ({ids}) : jugées sur la fenêtre {fenetre}, pas "
                          f"encore ouverte.")
        else:
            etat = ("résultat FINAL" if pre.get("final") else
                    f"en cours, {pre.get('jours', 0)} j")
            lignes.append(f"  <b>Pré-inscriptions</b> (déclarées le "
                          f"{_jour_iso(pre['declarees_le'])}, fenêtre "
                          f"{fenetre}, {etat}) :")
            for x in pre["resultats"]:
                ref = x.get("reference")
                comparaison = (f" · référence {_pct(ref[0], ref[1])} sur "
                               f"{ref[0]}" if ref else "")
                av = x["avant"]
                lignes.append(
                    f"    {x['nom']} : <b>{_pct(x['n'], x['g'])}</b> sur "
                    f"{x['n']}"
                    + (f" · par blocs {_ic(x['ic'])}" if x.get("ic") else "")
                    + f"{comparaison} (avant la fenêtre "
                      f"{_pct(av[0], av[1])} sur {av[0]}) → {x['verdict']}")
    lignes += validation_p2.texte(e4.get("validation_p2"), _jour)
    lignes.append("")

    # Journal
    journal = r.get("journal") or {}
    # Les témoins ne sont pas des hypothèses : ils contrôlent la mesure.
    actuelles = [h for h in r.get("hypotheses", ())
                 if not h[0].startswith("TÉMOIN")]
    positifs = [h for h in actuelles if h[2].startswith("✅")]
    examinees = [k for k in journal if not k.startswith("TÉMOIN")]
    lignes.append(f"<b>Journal des hypothèses</b> : {len(examinees)} "
                  f"examinées depuis le début du journal, {len(actuelles)} "
                  f"à ce rejeu ; au seuil de 5 %, environ "
                  f"{0.05 * len(examinees):.1f} « succès » sont attendus du "
                  f"seul hasard.")
    if positifs:
        lignes.append("  ✅ à ce rejeu : " + " · ".join(
            h[0] for h in positifs))
    negatifs = sum(1 for h in actuelles if h[2].startswith("❌"))
    lignes.append(f"  ❌ négatives à ce rejeu : {negatifs} (gardées au "
                  f"journal, elles comptent autant que les autres)")
    lignes.append("")

    # Reproductibilité
    rp = r["reproductibilite"]
    lignes.append("<b>Reproductibilité</b>")
    lignes.append(f"  code {rp['commit'] or 'commit inconnu'} · règles "
                  f"{rp['version_regles']} · expériences "
                  f"v{rp['version_experiences']} · période "
                  f"{_jour(rp['periode'][0])}–{_jour(rp['periode'][1])} "
                  f"({rp['jours']} j)")
    lignes.append(f"  empreinte des bougies : <code>{rp['empreinte'][:16]}"
                  f"</code> (SHA-256)")
    lignes.append(
        "\nDes mesures, pas des règles : rien ici ne change ce que la course "
        "joue. Un ✅ désigne une piste à confirmer au test aveugle.")
    return "\n".join(lignes)
