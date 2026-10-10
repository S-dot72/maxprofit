"""
L'audit des signaux : une fiche par signal, et d'où viennent les pertes.

--- D'où il vient ----------------------------------------------------------

Demandé le 2026-10-11, après deux rapports : « suspendre l'ajout de
nouveaux filtres et auditer les signaux existants ». Avant de chercher une
règle de plus, savoir si les pertes viennent surtout d'entrées tardives, de
faux rejets, de zones invalidées ou d'une expiration inadaptée.

--- Ce qu'il produit ------------------------------------------------------

1. Un fichier CSV, une ligne par signal ZoneH1 rejoué : identifiant,
   version des règles, paire, sens, heure du signal et de l'entrée
   confirmée, populations auxquelles il appartient (ZoneH1 en direct, 8,
   8f3), prix du signal, de la zone, d'entrée et de règlement, issue aux
   sept échéances (entrée au signal, et entrée confirmée), meilleur et
   pire point des 15 minutes suivantes, contexte au signal et à l'entrée.
2. Pour la référence jouée, chaque PERTE reçoit une cause principale,
   dans cet ordre de priorité :
   - « zone cassée » : une clôture a franchi la zone contre le trade ;
   - « pas de réaction » : le prix n'est jamais allé à une demi-amplitude
     dans le sens du trade — le rejet n'en était pas un ;
   - « gagnant puis retourné » : l'ordre aurait gagné à une échéance de 5
     minutes ou moins, le meilleur point est arrivé tôt — l'échéance de 15
     minutes l'a rendu perdant ;
   - « entrée tardive » : l'entrée était dans le tiers le plus avancé du
     chemin vers le prochain sommet/creux (ou le plus loin de la zone) ;
   - « autre ».
3. Les mêmes profils mesurés sur les GAGNANTS aussi, avec le taux de
   réussite des signaux qui les portent et des autres : un profil qu'ont
   autant les gagnants que les perdants n'explique rien, et l'écarter
   coûterait les bons ordres avec les mauvais.

⚠ Descriptif. Les causes « zone cassée », « pas de réaction » et
« gagnant puis retourné » se lisent APRÈS l'entrée : elles expliquent une
perte, elles ne peuvent pas l'éviter. Seule l'« entrée tardive » est
connue au moment d'entrer.
"""

from __future__ import annotations

import csv
import hashlib
import io
import time
from typing import Callable, Mapping, Sequence

from maxprofit.apprentissage.contexte import CARACTERISTIQUES
from maxprofit.apprentissage.historique import CONTEXTE_A_L_ENTREE
from maxprofit.apprentissage.lecons import Exemple
from maxprofit.live.laboratoire import confirmes

ECHEANCES: tuple[int, ...] = (60, 120, 180, 240, 300, 600, 900)

#: En dessous, en amplitudes M1, le prix n'a pas réagi à la zone.
REACTION_MIN = 0.5

CAUSES: tuple[str, ...] = ("zone cassée", "pas de réaction",
                           "gagnant puis retourné", "entrée tardive",
                           "autre")


def version_des_regles(strategie) -> str:
    """Le nom de la stratégie et une empreinte de ses paramètres : deux
    fiches au même identifiant viennent des mêmes règles."""
    params = getattr(strategie, "params", {}) or {}
    empreinte = hashlib.sha1(repr(sorted(dict(params).items())).encode())
    return f"{getattr(strategie, 'name', 'strategie')}:" \
           f"{empreinte.hexdigest()[:8]}"


def _tiers_haut(valeurs: Sequence[float]) -> float | None:
    """La coupe du tiers haut. Comparée STRICTEMENT : à valeurs égales,
    « au moins la coupe » rangeait tout le monde dans le tiers."""
    v = sorted(valeurs)
    return v[2 * len(v) // 3] if len(v) >= 9 else None


def _tardive(base: Sequence[Exemple]) -> Callable[[Exemple], bool]:
    """Le tiers le plus avancé : part du chemin parcourue si elle est
    mesurée, sinon distance à la zone."""
    coupe_part = _tiers_haut([e.contexte["part_parcourue"] for e in base
                              if "part_parcourue" in e.contexte])
    coupe_dist = _tiers_haut([e.contexte["distance_niveau"] for e in base
                              if "distance_niveau" in e.contexte])

    def tardive(e: Exemple) -> bool:
        c = e.contexte
        if coupe_part is not None and "part_parcourue" in c:
            return c["part_parcourue"] > coupe_part
        if coupe_dist is not None and "distance_niveau" in c:
            return c["distance_niveau"] > coupe_dist
        return False
    return tardive


def _profils(base: Sequence[Exemple]) -> dict[str, Callable[[Exemple], bool]]:
    tardive = _tardive(base)
    coupe_bougie = _tiers_haut([e.contexte["taille_bougie"] for e in base
                                if "taille_bougie" in e.contexte])
    obstacles = sorted(e.contexte["espace_obstacle"] for e in base
                       if "espace_obstacle" in e.contexte)
    coupe_obstacle = (obstacles[len(obstacles) // 3]
                      if len(obstacles) >= 9 else None)
    return {
        # Connus À L'ENTRÉE : ceux-là pourraient devenir des règles.
        "entrée tardive": tardive,
        "bougie d'entrée excessive": lambda e: coupe_bougie is not None
        and e.contexte.get("taille_bougie", 0.0) > coupe_bougie,
        "obstacle proche": lambda e: coupe_obstacle is not None
        and e.contexte.get("espace_obstacle", 99.0) < coupe_obstacle,
        # Constatés APRÈS : ils expliquent, ils n'évitent rien.
        "pas de réaction": lambda e: "mfe_15" in e.contexte
        and e.contexte["mfe_15"] < REACTION_MIN,
        "zone cassée": lambda e: e.contexte.get("zone_cassee") == 1.0,
        "gagnant puis retourné": lambda e: _gagnant_tot(e),
    }


def _gagnant_tot(e: Exemple) -> bool:
    return e.contexte.get("t_mfe", 99.0) <= 5 and any(
        e.issues.get(sec) for sec in (60, 120, 180, 240, 300))


def cause(e: Exemple, tardive: Callable[[Exemple], bool]) -> str:
    """La cause principale d'une perte (voir l'en-tête)."""
    c = e.contexte
    if c.get("zone_cassee") == 1.0:
        return "zone cassée"
    if "mfe_15" in c and c["mfe_15"] < REACTION_MIN:
        return "pas de réaction"
    if _gagnant_tot(e):
        return "gagnant puis retourné"
    if tardive(e):
        return "entrée tardive"
    return "autre"


def audit(exemples: Sequence[Exemple], en_direct: Callable[[Exemple], bool],
          fenetre: int = 1) -> dict:
    """Le résumé de l'audit sur la référence jouée (petit : il est gardé
    avec le laboratoire)."""
    base = [e for e in sorted(confirmes(exemples, fenetre),
                              key=lambda e: e.ts_sec) if en_direct(e)]
    nom = "8" if fenetre == 1 else f"8f{fenetre}"
    if not base:
        return {"note": f"Aucune entrée confirmée rejouée ({nom})."}
    tardive = _tardive(base)
    perdus = [e for e in base if not e.gagne]
    causes = {k: 0 for k in CAUSES}
    for e in perdus:
        causes[cause(e, tardive)] += 1
    profils = {}
    for nom_p, porte in _profils(base).items():
        avec = [e for e in base if porte(e)]
        sans = [e for e in base if not porte(e)]
        profils[nom_p] = [len(avec), sum(e.gagne for e in avec),
                          len(sans), sum(e.gagne for e in sans)]
    return {"nom": nom, "n": len(base), "gagnes": sum(e.gagne for e in base),
            "pertes": len(perdus), "causes": causes, "profils": profils,
            "debut": base[0].ts_sec, "fin": base[-1].ts_sec}


def texte(resultat: Mapping | None, avec_fichier: bool) -> str:
    """`/audit`."""
    if not resultat:
        return ("🧾 Audit pas encore calculé : il accompagne le rejeu "
                "(quelques minutes après un redémarrage).")
    if "note" in resultat:
        return f"🧾 {resultat['note']}"
    jour = lambda ts: time.strftime("%d/%m", time.gmtime(ts))  # noqa: E731
    n, g, p = resultat["n"], resultat["gagnes"], resultat["pertes"]
    lignes = [f"🧾 <b>Audit des signaux — référence {resultat['nom']}</b> "
              f"(entrées confirmées rejouées du {jour(resultat['debut'])} au "
              f"{jour(resultat['fin'])})",
              f"{n} entrées : {g} gagnées ({g / n:.0%}), {p} perdues.",
              "", f"<b>D'où viennent les {p} pertes</b> (cause principale, "
                  f"dans cet ordre de priorité) :"]
    for nom_c, k in resultat["causes"].items():
        lignes.append(f"  {nom_c} : {k} ({k / max(1, p):.0%})")
    lignes += ["", "<b>Profils, gagnants ET perdants</b> (taux des signaux "
                   "qui le portent / des autres) :"]
    for i, (nom_p, c) in enumerate(resultat["profils"].items()):
        if i == 3:
            lignes.append("  — constatés APRÈS l'entrée (expliquent, "
                          "n'évitent pas) :")
        elif i == 0:
            lignes.append("  — connus À L'ENTRÉE (pourraient devenir des "
                          "règles) :")
        avec = f"{c[1] / c[0]:.0%} sur {c[0]}" if c[0] else "—"
        sans = f"{c[3] / c[2]:.0%} sur {c[2]}" if c[2] else "—"
        lignes.append(f"  {nom_p} : {avec} · sans : {sans}")
    lignes.append(
        "\nUn profil n'explique les pertes que si les signaux qui le "
        "portent gagnent NETTEMENT moins que les autres.")
    if avec_fichier:
        lignes.append("📎 Fiche complète de chaque signal rejoué dans le "
                      "fichier joint (CSV, séparateur « ; »).")
    else:
        lignes.append("📎 Fiche par signal indisponible : le rejeu n'a pas "
                      "encore tourné depuis le dernier démarrage.")
    return "\n".join(lignes)


def csv_du_rejeu(exemples: Sequence[Exemple],
                 en_direct: Callable[[Exemple], bool], version: str,
                 fenetre: int = 1) -> bytes:
    """Une ligne par signal ZoneH1 rejoué. Séparateur « ; », décimales au
    point, heures en UTC."""
    tous = sorted(exemples, key=lambda e: (e.ts_sec, e.pair))
    base_jouee = [e for e in confirmes(tous, fenetre) if en_direct(e)]
    tardive = _tardive(base_jouee)
    cles_entree = list(CONTEXTE_A_L_ENTREE)
    entetes = (["id", "version_regles", "paire", "sens", "signal_utc",
                "zoneh1_en_direct", "confirme_8", "confirme_8f3",
                "attente_confirmation", "entree_utc", "prix_signal",
                "niveau_zone", "prix_entree", "prix_15"]
               + [f"gagne_signal_{s // 60}m" for s in ECHEANCES]
               + [f"gagne_entree_{s // 60}m" for s in ECHEANCES]
               + ["mfe_15", "mae_15", "t_mfe", "zone_cassee", "cause_perte"]
               + [f"signal_{c}" for c in CARACTERISTIQUES]
               + [f"entree_{c}" for c in cles_entree])
    sortie = io.StringIO()
    w = csv.writer(sortie, delimiter=";", lineterminator="\n")
    w.writerow(entetes)
    heure = lambda ts: time.strftime(  # noqa: E731
        "%Y-%m-%d %H:%M", time.gmtime(ts))
    for e in tous:
        c = e.contexte
        attente = int(c.get("attente_confirmation", 0) or 0)
        decision = e.ts_sec - 60            # clôture de la bougie du signal
        entree_ts = decision + 60 * attente if attente else None
        joue = None
        if 1 <= attente <= fenetre:
            (joue,) = confirmes([e], fenetre) or (None,)
        gagne_entree = [c.get(f"gagne_confirme_{s}") for s in ECHEANCES]
        perte = ""
        if joue is not None and en_direct(joue) and not joue.gagne:
            perte = cause(joue, tardive)
        w.writerow(
            [f"{e.pair}-{decision}", version, e.pair.replace("_otc", ""),
             "CALL" if c.get("sens_call", 1.0) else "PUT",
             heure(decision + 60), int(en_direct(e)), int(attente == 1),
             int(1 <= attente <= 3), attente,
             heure(entree_ts + 60) if entree_ts else "",
             c.get("prix_signal", ""), c.get("niveau", ""),
             c.get("prix_entree", ""), c.get("prix_15", "")]
            + [_01(e.issues.get(s)) for s in ECHEANCES]
            + [_01(x) for x in gagne_entree]
            + [c.get("mfe_15", ""), c.get("mae_15", ""), c.get("t_mfe", ""),
               _01(c.get("zone_cassee")), perte]
            + [c.get(k, "") for k in CARACTERISTIQUES]
            + [c.get(f"e_{k}", "") for k in cles_entree])
    return sortie.getvalue().encode("utf-8-sig")


def _01(valeur) -> str:
    return "" if valeur is None else str(int(bool(valeur)))
