"""
/stockage — le diagnostic du stockage et des payouts, en LECTURE SEULE
(étape 2, partie A : docs/etape2_stockage_diagnostic.md et
docs/etape2_payouts_redondance.md).

Les mesures sont dans `store.diagnostic_stockage` ; ici, on ouvre une
connexion à part, passée en lecture seule, on compose le texte, et l'on tient
en mémoire des relevés de taille pour la croissance PHYSIQUE mesurée. Rien
n'est écrit en base, rien n'est supprimé, aucune compaction n'est préparée.

Chaque constat porte son statut : Vérifié (mesuré ou lu dans le code),
Probable, Hypothèse, Inconnu.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from typing import Callable

from maxprofit.store import diagnostic_stockage as ds

log = logging.getLogger(__name__)

#: Relevés de taille gardés en mémoire (perdus au redémarrage).
ECHANTILLONS = ds.Echantillons()
#: Entre deux relevés de taille.
PAS_ECHANTILLON_SEC = 1800
ENV_QUOTA = "QUOTA_STOCKAGE_MO"
QUOTA_DEFAUT_MO = 512
MO = 2 ** 20


def _ouvrir_lecture(ouvrir: Callable | None, chemin):
    from maxprofit.store.db import open_read_only
    conn = ouvrir() if ouvrir else open_read_only(chemin)
    ds.lecture_seule(conn)
    return conn


def echantillonner(chemin, ouvrir: Callable | None = None) -> None:
    """Un relevé de taille, en mémoire. Ne lève jamais."""
    try:
        conn = _ouvrir_lecture(ouvrir, chemin)
        try:
            ECHANTILLONS.noter(time.time(), ds.tailles(conn))
        finally:
            conn.close()
    except Exception as erreur:                          # noqa: BLE001
        log.debug("Relevé de taille impossible : %s", erreur)


async def echantillonner_en_boucle(chemin, ouvrir=None) -> None:
    while True:
        await asyncio.to_thread(echantillonner, chemin, ouvrir)
        await asyncio.sleep(PAS_ECHANTILLON_SEC)


def taille_releve(url: str) -> dict | None:
    """Taille et limite de la base de relève, qui reçoit TOUT l'historique."""
    try:
        from maxprofit.store.db import open_postgres
        conn = open_postgres(url, ecriture=False)
        try:
            ds.lecture_seule(conn)
            mesures = ds.tailles(conn)
            return {"base": mesures["base"], "limite_mo": mesures["limite_mo"]}
        finally:
            conn.close()
    except Exception as erreur:                          # noqa: BLE001
        return {"erreur": str(erreur).splitlines()[0][:120]}


def mesurer(chemin, ouvrir: Callable | None = None,
            maintenant: float | None = None) -> dict:
    maintenant = maintenant if maintenant is not None else time.time()
    conn = _ouvrir_lecture(ouvrir, chemin)
    try:
        debut = time.monotonic()
        mesures = ds.tailles(conn)
        ECHANTILLONS.noter(maintenant, mesures)
        crois = ds.croissance(conn, mesures, maintenant)
        try:
            pay = ds.payouts(conn, maintenant)
        except Exception as erreur:                      # noqa: BLE001
            pay = {"erreur": str(erreur).splitlines()[0][:200]}
        return {"mesure_le": maintenant, "duree_sec": time.monotonic() - debut,
                "tailles": mesures, "croissance": crois, "payouts": pay,
                "physique": ECHANTILLONS.croissance(maintenant)}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Texte
# --------------------------------------------------------------------------- #

def _mo(octets) -> str:
    return "—" if octets is None else f"{octets / MO:.1f} Mo"


def _d(ts) -> str:
    return time.strftime("%d/%m", time.gmtime(int(ts)))


def _dh(ts) -> str:
    return time.strftime("%d/%m %H:%M", time.gmtime(int(ts)))


def _pc(a, b) -> str:
    return f"{100 * a / b:.1f} %" if b else "—"


def _quota() -> tuple[float, str]:
    try:
        return float(os.environ.get(ENV_QUOTA, "") or QUOTA_DEFAUT_MO), \
            f"{ENV_QUOTA} (hypothèse)"
    except ValueError:
        return float(QUOTA_DEFAUT_MO), "défaut (hypothèse)"


def _texte_classement(c: dict) -> list[str]:
    """/classement compte des LIGNES ; il annonce une part du TEMPS."""
    from maxprofit.collect.classement import (
        RELEVES_MIN, est_une_paire_flottante)
    lignes = [x for x in c["paires"] if est_une_paire_flottante(x[0])]
    if not lignes:
        return ["Classement : aucune paire flottante sur 7 jours."]

    def rang(cle, effectif):
        retenues = [x for x in lignes if effectif(x) >= RELEVES_MIN]
        return [x[0] for x in sorted(retenues, key=lambda x: -cle(x))][:20]

    tel_quel = rang(lambda x: x[2] / x[1], lambda x: x[1])
    changements = rang(lambda x: x[4] / x[3] if x[3] else 0, lambda x: x[3])
    duree = [x for x in lignes if x[6] > 0]
    temps = [x[0] for x in sorted(duree, key=lambda x: -x[5] / x[6])][:20]
    ecarts = [abs(x[2] / x[1] - x[5] / x[6]) for x in duree if x[1]]
    return [
        f"• Paires flottantes : {len(lignes)} ; retenues (≥ {RELEVES_MIN} "
        f"relevés) telles quelles {len(rang(lambda x: 0, lambda x: x[1]))}, "
        f"en points de changement seuls "
        f"{len(rang(lambda x: 0, lambda x: x[3]))}.",
        f"• Top 20 commun : tel quel / changements seuls "
        f"{len(set(tel_quel) & set(changements))} ; tel quel / part du temps "
        f"{len(set(tel_quel) & set(temps))}.",
        f"• Écart entre part des relevés et part du temps au plafond : "
        f"médiane {100 * sorted(ecarts)[len(ecarts) // 2]:.0f} pts, max "
        f"{100 * max(ecarts):.0f} pts." if ecarts else "• Écart : —"]


def texte(d: dict, releve: dict | None = None) -> str:
    t, c, p = d["tailles"], d["croissance"], d["payouts"]
    quota, source = ((t["limite_mo"], t["source_limite"])
                     if t.get("limite_mo") else _quota())
    lignes = [
        f"💾 <b>Étape 2 — diagnostic du stockage</b> (lecture seule) — mesuré "
        f"le {_dh(d['mesure_le'])} UTC en {d['duree_sec']:.0f} s. Rien n'est "
        f"écrit ni supprimé.", "",
        "<b>1. Taille</b> [Vérifié : catalogue PostgreSQL]",
        f"Base {_mo(t['base'])} ; toutes les bases du serveur "
        f"{_mo(t.get('toutes_bases'))} ; taille comptée par Neon "
        f"{_mo(t.get('taille_neon')) if t.get('taille_neon') else 'non exposée'}.",
        f"Limite : {quota:.0f} Mo — source : {source}"
        + ("" if t.get("limite_mo") else " [Inconnu : à lire dans la console "
           "Neon, qui seule fait foi]") + "."]
    for nom, v in list(t.get("tables", {}).items())[:8]:
        mortes = v.get("mortes")
        vivantes = v.get("vivantes")
        lignes.append(
            f"• {nom} : total {_mo(v['total'])} (table {_mo(v['table'])}, "
            f"index {_mo(v['index'])}, TOAST {_mo(v['toast'])}, autres "
            f"{_mo(v['autres'])})"
            + (f" ; lignes mortes {mortes} sur {mortes + vivantes} "
               f"({_pc(mortes, mortes + vivantes)}), dernier nettoyage "
               f"{v.get('dernier_vacuum') or '—'}"
               if mortes is not None and vivantes is not None
               and mortes + vivantes else ""))
    if releve:
        lignes.append(
            "Relève : " + (f"erreur ({html.escape(releve['erreur'])})"
                           if "erreur" in releve else
                           f"{_mo(releve['base'])}, limite "
                           f"{releve['limite_mo'] or '—'} Mo") +
            " — elle reçoit TOUT l'historique à la bascule.")

    lignes += ["", "<b>2. Croissance</b> [Probable : estimation — lignes par "
                   "jour de données × octets physiques moyens par ligne]"]
    for nom, v in c["tables"].items():
        jours = " · ".join(f"{_d(j)} {n}" for j, n in v["par_jour"].items())
        lignes.append(
            f"• {nom} : {v['lignes']} lignes, "
            f"{(v['octets_par_ligne'] or 0):.0f} o/ligne ; par jour {jours} "
            f"(aujourd'hui {v['aujourd_hui']})")
    jours_txt = " · ".join(
        f"{_d(j)} {_mo(o)}" for j, o in zip(c["jours"], c["octets_par_jour"]))
    lignes.append(f"Total estimé par jour : {jours_txt}.")
    phys = d["physique"]
    if phys["fenetres"]:
        lignes.append("Croissance PHYSIQUE mesurée [Vérifié, relevés en "
                      "mémoire] : " + " · ".join(
                          f"{h} h {_mo(v['base'])} (sur {v['duree_h']:.0f} h)"
                          for h, v in phys["fenetres"].items()) + ".")
    else:
        lignes.append(
            f"Croissance physique mesurée : pas encore — le processus relève "
            f"la taille toutes les {PAS_ECHANTILLON_SEC // 60} min depuis "
            f"{phys['depuis_sec'] / 3600:.1f} h (en mémoire, perdu au "
            f"redémarrage) ; il faut 24 h.")
    delais = ds.delais(t, c["scenarios"], quota)
    if delais:
        lignes.append(
            f"Reste {_mo(delais['reste_octets'])}. Jours avant saturation "
            f"[Hypothèse : croissance constante, limite {quota:.0f} Mo] : "
            + " · ".join(
                f"{nom} {(delais[nom] or 0):.1f} j"
                for nom in ("haut", "central", "bas") if nom in delais)
            + " (croissance haute → délai court).")
        # Neon peut compter TOUTES les bases du projet, bases système
        # comprises : le délai le plus court des deux est le prudent.
        if t.get("toutes_bases") and t["toutes_bases"] > t["base"]:
            toutes = ds.delais({"base": t["toutes_bases"]}, c["scenarios"],
                               quota)
            lignes.append(
                f"Si Neon compte toutes les bases ({_mo(t['toutes_bases'])}) "
                f": reste {_mo(toutes['reste_octets'])}, soit " + " · ".join(
                    f"{nom} {(toutes[nom] or 0):.1f} j"
                    for nom in ("haut", "central", "bas") if nom in toutes)
                + ".")

    lignes += ["", "<b>3. Payouts — répétitions</b> [Vérifié : SQL de fenêtre "
                   "sur toute la table]"]
    if "erreur" in p:
        lignes.append(f"Mesure impossible : {html.escape(p['erreur'])}")
        return "\n".join(lignes)
    n = p["lignes"]
    taille_pay = t.get("tables", {}).get("payouts", {}).get("total")
    par_ligne = taille_pay / n if taille_pay and n else None
    k = p["categories"]
    lignes += [
        f"{n} lignes, {p['paires']} paires, du "
        f"{_dh(p['periode'][0]) if p['periode'][0] else '—'} au "
        f"{_dh(p['periode'][1]) if p['periode'][1] else '—'} ; premières "
        f"lignes de paire {p['premiers']}.",
        f"Changements : payout seul {p['changements']['payout']}, ouverture "
        f"seule {p['changements']['ouverture']}, les deux "
        f"{p['changements']['les_deux']}.",
        f"1. Répétées (même valeur que la précédente, autre horodatage) : "
        f"{k['1_repetes_horodatages_differents']} ({_pc(k['1_repetes_horodatages_differents'], n)}).",
        "2. Répétées au même horodatage : 0 (clé primaire ts_sec, pair).",
        f"3. Identiques non consécutives (A → B → A, des changements) : "
        f"{k['3_identiques_non_consecutifs']}.",
        f"4. Doublons de clé métier : 0 ; relevés à moins de 60 s du "
        f"précédent {k['4b_releves_a_moins_de_60s']} (dont répétés "
        f"{k['4c_dont_repetes']}).",
        f"5. Validité indéterminable (payout hors 0-100, ouverture hors "
        f"0/1) : {k['5_validite_indeterminable']}.",
        "Écart au relevé précédent (répétés / changements) : " + " · ".join(
            f"{'> 1 j' if cas == -1 else f'≤ {cas // 60} min' if cas < 3600 else f'≤ {cas // 3600} h'} "
            f"{sum(x[2] for x in p['intervalles'] if x[0] == cas and x[1])}"
            f"/{sum(x[2] for x in p['intervalles'] if x[0] == cas and not x[1])}"
            for cas in (*ds.CASIERS_SEC, -1)) + ".",
        "Par jour (lignes / répétées) : " + " · ".join(
            f"{_d(j)} {nl}/{r}" for j, nl, r in p["par_jour"][-10:]) + ".",
        "Paires les plus répétées : " + ", ".join(
            f"{nom} {r}/{nl}" for nom, nl, r in p["par_paire"][:8]) + "."]
    lignes += ["", "<b>4. Scénarios de compaction</b> [comptés, JAMAIS "
                   "appliqués ; octets = borne haute, la place n'est rendue "
                   "qu'après réécriture de la table]"]
    for nom, retirees in p["scenarios_lignes"].items():
        lignes.append(
            f"• {nom.replace('_', ' ')} : {retirees} lignes "
            f"({_pc(retirees, n)}), ≤ "
            f"{_mo(retirees * par_ligne) if par_ligne else '—'}")
    r = p["reconstruction"]
    lignes += [
        "", "<b>5. Reconstruction en points de changement seuls</b> "
            "[Vérifié : SQL]",
        f"Valeur en vigueur différente à un instant observé : "
        f"{r['valeurs_differentes']} (attendu 0).",
        "Âge du relevé qui porterait la valeur, aux instants observés : "
        + " · ".join(
            f"{'> 1 j' if int(a) == -1 else f'≤ {int(a) // 60} min' if int(a) < 3600 else f'≤ {int(a) // 3600} h'} {v}"
            for a, v in r["age_du_releve_en_vigueur"].items()) + ".",
        f"Paires dont la dernière ligne est une répétition : "
        f"{r['derniere_ligne_repetee']}/{r['paires']} ; recul de la dernière "
        f"observation (médiane, max) : "
        + (f"{r['recul_derniere_observation_sec'][0] // 60} min, "
           f"{r['recul_derniere_observation_sec'][1] // 3600} h"
           if r["recul_derniere_observation_sec"] else "—") + ".",
        "[Vérifié : code] Perte fonctionnelle si l'on ne garde que les "
        "changements : la date de la DERNIÈRE OBSERVATION disparaît (seule "
        "reste la date du dernier changement).",
        "", "<b>6. Consommateurs qui comptent les lignes</b> [Vérifié : code]"]
    lignes += _texte_classement(p["classement"])
    rj = p["rejeu"]["ecarts_de_plus_d_un_jour"]
    lignes.append(
        f"• outils/rejeu_plan.py (ne lit que le jour qui précède sa fenêtre) : "
        f"écarts de plus d'un jour entre relevés {rj['tel_quel']} tels quels, "
        f"{rj['changements_seuls']} en changements seuls.")
    lignes.append(
        "• Insensibles (la valeur en vigueur suffit) : payout_at du store et "
        "du backtest, l'amorçage de la déduplication à l'écriture.")
    lignes.append("Méthode, requêtes, limites : docs/etape2_payouts_"
                  "redondance.md et docs/etape2_stockage_diagnostic.md.")
    return "\n".join(lignes)


def rapport(chemin, ouvrir: Callable | None = None,
            url_releve: str | None = None) -> str:
    try:
        diagnostic = mesurer(chemin, ouvrir)
    except Exception as erreur:                          # noqa: BLE001
        log.exception("Diagnostic du stockage impossible")
        return f"💾 Diagnostic du stockage impossible : {html.escape(str(erreur))}"
    return texte(diagnostic, taille_releve(url_releve) if url_releve else None)
