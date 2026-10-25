"""
/ticks — l'étape 0 : l'audit forensique des ticks, en lecture seule.

Le calcul est dans `apprentissage.audit_ticks` (contrôles) et
`apprentissage.microstructure` (analyses exploratoires), qui ne touchent pas
la base ; ce module en fait les LECTURES (SELECT seulement, `REQUETES`) et la
mise en forme pour Telegram. Inventaire et méthode : docs/etape0_ticks.md.
Rien n'y décide quoi que ce soit : la course et la fenêtre T0/T2/T3 n'en
lisent rien.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Mapping, Sequence

from maxprofit.apprentissage import microstructure
from maxprofit.apprentissage.audit_ticks import (
    BORD_SEC, CATEGORIES, CODES, COMPLETE, DECALAGE, EXPLICABLES,
    FENETRES_PRIX_MS, GEL_SEC, IDENTIQUE, INEXPLIQUE, INTERRUPTION_LONGUE_MIN,
    ORDRES_ANALYSE, REQUETES, AuditTicks, csv_anomalies, csv_ordres,
    csv_signaux)
from maxprofit.store.chemin_ticks import decoder, encoder

#: Jours audités par défaut : 7 jours, environ 20 Mo de chemins à relire —
#: l'audit tourne chaque jour avec le rejeu, sur une base au quota compté.
JOURS_PAR_DEFAUT = 7

#: Les colonnes lues par `lire_ordres`, dans l'ordre de `REQUETES["ordres"]`.
COLONNES_ORDRES = ("pair", "sens", "signal_ts_ms", "clic_ts_ms",
                   "accepte_ts_ms", "prix_attendu", "prix_entree",
                   "prix_sortie", "ouverture_ts_ms", "expiration_ts_ms",
                   "decalage_broker_ms", "expiration_sec", "accepte",
                   "resultat", "brut")


def jours_audites() -> int:
    """`AUDIT_TICKS_JOURS` : jours audités (7 par défaut, 0 désactive)."""
    import os
    try:
        return max(0, int(os.environ.get("AUDIT_TICKS_JOURS",
                                         JOURS_PAR_DEFAUT)))
    except ValueError:
        return JOURS_PAR_DEFAUT


def nouvel_audit(debut: int, fin: int, joue=None) -> AuditTicks:
    """L'audit de [debut, fin[, avec le décodeur et l'encodeur des chemins
    stockés (le second pour le contrôle de compression)."""
    return AuditTicks(debut, fin, decoder=decoder, encoder=encoder, joue=joue)


def lire_battements(conn, debut: int, fin: int) -> list[int]:
    """Les battements du collecteur de [debut, fin[ (table `uptime`)."""
    return [int(r[0]) for r in conn.execute(
        REQUETES["battements"], (int(debut), int(fin))).fetchall()]


def lire_ordres(conn) -> list[dict]:
    """Tous les ordres du journal, refusés compris (ils comptent dans les
    exclusions). Une table absente ou trop ancienne rend une liste vide : le
    contrôle des prix est alors NOT TESTABLE, pas faux."""
    try:
        lignes = conn.execute(REQUETES["ordres"]).fetchall()
    except Exception:                              # noqa: BLE001
        return []
    return [dict(zip(COLONNES_ORDRES, ligne)) for ligne in lignes]


def fichiers(audit: AuditTicks) -> list[tuple[bytes, str, str]]:
    """Les trois fiches CSV de l'audit : (contenu, nom, légende)."""
    return [
        (csv_anomalies(audit.anomalies_triees()), "anomalies_ticks.csv",
         "Registre des anomalies, la plus grave d'abord"),
        (csv_signaux(audit.fiches_signaux), "signaux_ticks.csv",
         "Couverture de chaque signal ZoneH1 et motif d'exclusion"),
        (csv_ordres(audit.fiches_ordres), "ordres_ticks.csv",
         "Chaque ordre réel face au flux : prix, écarts signés, délais"),
    ]


# --------------------------------------------------------------------------- #
# Texte
# --------------------------------------------------------------------------- #

ICONES = {"PASS": "✅", "WARNING": "⚠️", "FAIL": "❌", "NOT TESTABLE": "❔"}
NOMS = {"horodatages": "Horodatages et continuité",
        "compression": "Compression et relecture",
        "concordance": "Concordance M1",
        "trous": "Trous de collecte",
        "signaux": "Couverture des signaux ZoneH1",
        "prix": "Prix d'exécution",
        "ticks_tardifs": "Ticks tardifs",
        "heure_reception": "Heure de réception"}


def _d(ts: float, avec_heure: bool = True) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%d/%m %H:%M" if avec_heure else "%d/%m")


def _pc(x: float | None, chiffres: int = 1) -> str:
    return "—" if x is None else f"{100 * x:.{chiffres}f} %"


def _part(a: int, b: int) -> str:
    return _pc(a / b) if b else "—"


def _ms(x) -> str:
    if x is None:
        return "—"
    if x == -1:
        return "plus de 60 s"
    return f"{x / 1000:g} s" if x >= 1000 else f"{x} ms"


def _n(x: int) -> str:
    return f"{x:,}".replace(",", " ")


def _e(x) -> str:
    return html.escape(str(x), quote=False)


def _v(x, suffixe: str = "") -> str:
    return "—" if x is None else f"{x:g}{suffixe}"


def _q(q: Sequence | None, suffixe: str = "") -> str:
    """Médiane (quartiles) d'un triplet [q1, médiane, q3]."""
    if not q:
        return "—"
    return f"{_v(q[1], suffixe)} ({_v(q[0])}–{_v(q[2])})"


def _ic(ic) -> str:
    return "" if not ic else f" [{ic[0]:+.3f} ; {ic[1]:+.3f}]"


def _ic_pc(ic) -> str:
    return "" if not ic else f" [{100 * ic[0]:.0f} ; {100 * ic[1]:.0f} %]"


def texte(r: Mapping | None, fichier: bool = False) -> str:
    if r and "erreur" in r:
        return (f"🔬 Qualité des ticks : l'audit a échoué au dernier rejeu "
                f"({_e(r['erreur'])}). Le rejeu, lui, a continué ; détails "
                f"dans les journaux du service.")
    if r and "controles" in r and "compression" not in r["controles"]:
        return ("🔬 Qualité des ticks : le résultat gardé vient d'une version "
                "précédente de l'audit ; le rejeu en cours le recalcule au "
                "nouveau format (quelques minutes après un déploiement).")
    if not r or "controles" not in r:
        return ("🔬 Qualité des ticks : pas encore mesurée — le prochain rejeu "
                "du laboratoire la calculera (AUDIT_TICKS_JOURS, 7 jours par "
                "défaut ; 0 la désactive).")
    c = r["controles"]
    h, z, k, t, s, p = (c["horodatages"], c["compression"],
                        c["concordance"], c["trous"], c["signaux"], c["prix"])
    jours = (r["fin"] - r["debut"]) / 86400
    rep = r["reproductibilite"]
    lignes = [
        f"🔬 <b>Étape 0 — audit forensique des ticks</b> (lecture seule) — du "
        f"{_d(r['debut'])} au {_d(r['fin'])} UTC ({jours:g} j audités), "
        f"{len(r['paires'])} paires ; calculé le {_e(r.get('calcule_le', ''))}"
        f" UTC.",
        "Tout est recalculé à ce rejeu ; rien n'est modifié ; la course et la "
        "fenêtre T0/T2/T3 n'en lisent rien.", "",
        "<b>1. Résumé</b> (statut — contrôle : mesure)"]
    j = FENETRES_PRIX_MS.index(1000)
    resume = {
        "horodatages": (
            f"résolution {_ms(h['granularite_ms'])} ; {h['hors_minute']} "
            f"hors de sa minute, {h['decroissants']} décroissant(s) ; "
            f"doublons exacts {_part(h['doublons_exacts'], h['ticks'])} "
            f"(minutes à flux doublé "
            f"{_part(h.get('minutes_doublees', 0), t['minutes_avec_ticks'])}) ; "
            f"gels de {GEL_SEC} s ou plus sur "
            f"{_part(h['minutes_gelees'], t['minutes_avec_ticks'])} des "
            f"minutes"),
        "compression": (
            f"{z['illisibles']} chemin(s) illisible(s) sur {_n(z['chemins'])} ;"
            f" réencodés identiques {z['reencodees'][0] - z['reencodees'][1]}/"
            f"{z['reencodees'][0]} ; relectures identiques "
            f"{z['relectures'][0] - z['relectures'][2]}/{z['relectures'][0]}"),
        "concordance": (
            f"{_part(k['classes'].get(IDENTIQUE, 0), k['comparees'])} exactes "
            f"sur {_n(k['comparees'])} ; explicables "
            f"{_part(sum(k['classes'].get(x, 0) for x in EXPLICABLES), k['comparees'])}"
            f" ; inexpliquées {k['classes'].get(INEXPLIQUE, 0)}, décalages de "
            f"minute {k['classes'].get(DECALAGE, 0)}"),
        "trous": (
            f"{_pc(t['part_manquante_ecoutee'])} des minutes écoutées sans "
            f"ticks ; {t['interruptions_longues']} interruption(s) longue(s)"
            + (f" ; {_n(t['minutes_sans_historique'])} minute(s) de ticks sans "
               f"historique des battements (hors contrôle)"
               if t.get("minutes_sans_historique") else "")),
        "signaux": (
            f"{s['signaux'][1]}/{s['signaux'][0]} fenêtres de 30 min "
            f"complètes ({_part(s['signaux'][1], s['signaux'][0])})"),
        "prix": (
            f"ouverture retrouvée à ± 1 s "
            f"{_part(p['ouverture']['trouves'][j], p['ouverture']['comparables'])}"
            f" de {p['ouverture']['comparables']}, clôture "
            f"{_part(p['cloture']['trouves'][j], p['cloture']['comparables'])}"
            f" de {p['cloture']['comparables']} ; niveau : {p['niveau']}"),
        "ticks_tardifs": "écartés par l'agrégateur sans compteur : nombre "
                         "impossible à établir",
        "heure_reception": "non stockée : latence du flux non mesurable",
    }
    for cle, nom in NOMS.items():
        statut = r["statuts"][cle]
        lignes.append(f"{ICONES[statut]} {statut} — {nom} : {resume[cle]}")

    # 2. Couverture
    principales = " ; ".join(
        f"{x[0].replace('_otc', '')} {_d(x[1])}–{_d(x[2])}"
        for x in t["principales_interruptions"]) or "aucune"
    tpm = t["ticks_par_minute"]
    lignes += [
        "", "<b>2. Couverture</b>",
        f"Minutes attendues {_n(t['minutes_attendues'])} "
        f"({_e(t['definition'])}) ; observées {_n(t['minutes_avec_ticks'])} "
        f"({_part(t['minutes_avec_ticks'], t['minutes_attendues'])}) ; "
        f"absentes alors que le collecteur battait "
        f"{_n(t['manquantes_ecoutees'])}, collecteur muet "
        f"{_n(t['manquantes_muettes'])}, sans historique des battements "
        f"{_n(t.get('manquantes_sans_historique', 0))} ; ticks "
        f"{_n(h['ticks'])}.",
        f"Ticks par minute (distribution observée, aucune cadence imposée) : "
        f"p10 {_v(tpm[0])}, p25 {_v(tpm[1])}, médiane {_v(tpm[2])}, p75 "
        f"{_v(tpm[3])}, p90 {_v(tpm[4])} ; en ticks DISTINCTS (doublons "
        f"exacts ôtés) : " + _quintiles(h.get("ticks_distincts_min"))
        + f" ; minutes à moins de 5 ticks distincts : "
        f"{t['minutes_pauvres']}.",
        "Battements du collecteur en base, par jour (trouvés/attendus) : "
        + (" · ".join(f"{_d(int(j), False)} {v[0]}/{v[1]}"
                      for j, v in t.get("battements_par_jour", {}).items())
           or "—") + ".",
        f"Interruptions de {INTERRUPTION_LONGUE_MIN} min ou plus : "
        f"{t['interruptions_longues']} ({_n(t['interruptions_min'])} min) ; "
        f"principales : {principales}."]

    # 3. Horodatages
    e = h["ecarts_ms"]
    lignes += [
        "", "<b>3. Horodatages et continuité</b> (horloge du serveur du "
        "broker, en UTC ; ordre de stockage, l'ordre d'arrivée est perdu)",
        f"Granularité {_ms(h['granularite_ms'])} (à la seconde ronde "
        f"{_pc(h['part_seconde_ronde'])}) ; même instant {h['doublons_instant']}"
        f" (dont {h['doublons_exacts']} doublons exacts, gardés) ; minutes de "
        f"ticks sans battement du collecteur {t['minutes_sans_battement']}, "
        f"dans un trou de l'historique des battements (non testable) "
        f"{t.get('minutes_sans_historique', 0)}.",
        f"Écart entre ticks consécutifs : p25 ≤ {_ms(e['p25'])}, médiane ≤ "
        f"{_ms(e['p50'])}, p75 ≤ {_ms(e['p75'])}, p90 ≤ {_ms(e['p90'])}, p99 ≤ "
        f"{_ms(e['p99'])}, max {_ms(e['max'])} ; minutes avec un gel de "
        f"{GEL_SEC} s ou plus : {h['minutes_gelees']}.",
        f"Compression : {_n(z['chemins'])} chemins lus, {z['illisibles']} "
        f"illisible(s) ; 1 minute sur {z['echantillon_reencodage']} réencodée "
        f"et comparée à ses octets (avant zlib) : "
        f"{z['reencodees'][1]} différence(s) sur {z['reencodees'][0]} ; "
        f"{z['relectures'][0]} fenêtre(s) de {z['fenetre_relecture_min']} min "
        f"relue(s) par MarketReader.ticks() ({_n(z['relectures'][1])} ticks) :"
        f" {z['relectures'][2]} différence(s)."]

    # 4. Concordance
    lignes += ["", "<b>4. Concordance M1</b> (bougies et chemins viennent des "
               "mêmes ticks : cohérence du stockage, pas exactitude du flux)"]
    for classe in (IDENTIQUE, *EXPLICABLES, DECALAGE, INEXPLIQUE):
        nombre = k["classes"].get(classe, 0)
        ex = k["exemples"].get(classe) or []
        exemple = (f" — ex. {ex[0][0].replace('_otc', '')} {_d(ex[0][1])}"
                   if ex else "")
        lignes.append(f"• {_e(CATEGORIES[classe])} : {_n(nombre)} "
                      f"({_part(nombre, k['comparees'])}){exemple}")
    lignes.append(
        f"Comparaison impossible : {k['chemin_sans_bougie']} minute(s) de "
        f"ticks sans bougie ; {_n(k['bougie_sans_chemin'])} bougie(s) sans "
        f"ticks (couverture)."
        + (" Champs en défaut : " + ", ".join(
            f"{x} {v}" for x, v in k["champs_inexpliques"].items())
           if k["champs_inexpliques"] else "")
        + (" Pris à l'arrivée (ordre d'arrivée) : " + ", ".join(
            f"{x} {v}" for x, v in k["champs_ordre"].items()) + "."
           if k.get("champs_ordre") else ""))

    # 5. Signaux
    codes = s["codes"]
    lignes += [
        "", f"<b>5. Couverture des signaux ZoneH1</b> (30 min : 15 avant la "
        f"décision, 15 après ; bornes à {BORD_SEC} s ; l'issue du contrat "
        f"n'entre jamais dans le statut)",
        f"Admissibles {s['signaux'][0]}, exploitables {codes.get(COMPLETE, 0)},"
        f" exclus {s['signaux'][0] - codes.get(COMPLETE, 0)} ; joués (règle "
        f"de l'heure) {s['joues'][1]}/{s['joues'][0]} ; trop près d'un bord "
        f"de la fenêtre auditée : {s['hors_fenetre']} ; exploitables dans un "
        f"trou de l'historique des battements (contrôle des battements non "
        f"testable) : {s.get('complets_sans_historique', 0)}.",
        "Exclusions (premier motif rencontré) : " + " · ".join(
            f"{code} {codes.get(code, 0)}" for code in CODES
            if codes.get(code)) + ("." if any(
                codes.get(code) for code in CODES) else "aucune."),
        "Par heure UTC (exploitables/admissibles) : " + " ".join(
            f"{int(hh):02d}h {v[1]}/{v[0]}"
            for hh, v in s["par_heure"].items())]

    # 6. Exécutions
    lignes += _texte_executions(p)
    # 7. Tableaux
    lignes += _tableaux(r)
    # 8. Anomalies
    a = r["anomalies"]
    totaux = {g: sum(x[3] for x in a["comptes"] if x[0] == g)
              for g in ("FAIL", "WARNING", "INFO")}
    lignes += ["", f"<b>8. Registre des anomalies</b> — FAIL {totaux['FAIL']} "
                   f"· WARNING {totaux['WARNING']} · INFO {totaux['INFO']}"]
    if a["comptes"]:
        lignes.append("Par nature : " + " · ".join(
            f"{gr} {_e(controle)}/{_e(nature)} {v}"
            for gr, nature, controle, v in a["comptes"]))
    for an in a["premieres"][:10]:
        lignes.append(f"• {an['id']} {an['gravite']} {_e(an['controle'])} "
                      f"{an['paire'].replace('_otc', '')} {_d(an['debut'])} : "
                      f"{_e(an['nature'])} — {_e(an['mesure'])}")
    if fichier:
        lignes.append("📎 Fiches complètes jointes (CSV, séparateur « ; ») : "
                      "anomalies, signaux, ordres.")
    else:
        lignes.append("Les fiches CSV reviennent au prochain rejeu : elles "
                      "ne sont gardées qu'en mémoire.")
    # 9. Exploratoire
    lignes += _texte_exploration(r["exploration"])
    # 10. Inventaire, limites, reproduire
    inv = r["inventaire"]
    lignes += ["", "<b>10. Inventaire des analyses</b> (déduit des statuts)"]
    for cle, titre in (("faisables", "Faisables"),
                       ("sous_reserve", "Sous réserve"),
                       ("non_fiables", "Non fiables"),
                       ("impossibles", "Impossibles sans autres données")):
        lignes.append(f"{titre} : " + (" ; ".join(_e(x) for x in inv[cle])
                                       or "aucune") + ".")
    lignes += ["", "<b>11. Limites</b>"] + [f"• {_e(x)}"
                                             for x in r["limites"]]
    lignes += [
        "", "<b>12. Reproduire</b>",
        f"Code {_e(rep['commit'] or '—')} · empreinte des chemins "
        f"{rep['empreinte']} sur {rep['chemins_lus']} chemin(s) · fenêtre "
        f"[{r['debut']} ; {r['fin']}[ (secondes UTC).",
        f"Hors du bot, mêmes calculs : <code>python outils/audit_ticks.py "
        f"--debut {r['debut']} --fin {r['fin']}</code> (DATABASE_URL lue dans "
        f"l'environnement ; session en lecture seule). Méthode, inventaire, "
        f"codes et conventions : docs/etape0_ticks.md.",
        "Critères (et justification) : " + " | ".join(
            f"{_e(x)} — {_e(v)} ({_e(rep['justifications'].get(x, ''))})"
            for x, v in rep["seuils"].items()),
        "Requêtes (SELECT seulement) :"] + [
        f"• {_e(x)} : <code>{_e(v)}</code>"
        for x, v in rep["requetes"].items()]
    return "\n".join(lignes)


def _texte_executions(p: Mapping) -> list[str]:
    exclus = " · ".join(f"{_e(m)} {v}" for m, v in p["exclus"].items())
    lignes = [
        "", f"<b>6. Prix d'exécution</b> — niveau : {p['niveau']} "
        f"({'analyse possible' if p['niveau'] == 'analyse' else f'comparaison descriptive seulement, il faut {ORDRES_ANALYSE} ordres comparables'})",
        f"Ordres au journal {p['ordres']} (rapatriés {p['rapatries']}) ; "
        f"exclus : {exclus or 'aucun'} ; ouverture horodatée à la seconde "
        f"ronde : {p['seconde_ronde']}/{p['horodates']} ; synchronisation "
        f"broker − nous, heures entières ôtées : p10 "
        f"{_v(p['synchronisation_ms'][0], ' ms')}, médiane "
        f"{_v(p['synchronisation_ms'][1], ' ms')}, p90 "
        f"{_v(p['synchronisation_ms'][2], ' ms')}."]
    for cle, nom in (("ouverture", "Prix d'ouverture du broker"),
                     ("cloture", "Prix de clôture du broker")):
        v = p[cle]
        lignes.append(
            f"{nom} dans notre flux : {v['comparables']} comparable(s) ; "
            + ", ".join(
                f"{'au dernier tick' if w == 0 else f'à ± {w // 1000} s'} "
                f"{_part(v['trouves'][i], v['comparables'])}"
                for i, w in enumerate(FENETRES_PRIX_MS))
            + f" ; tick égal le plus proche : médiane "
              f"{_v((v['proche_ms'] or [None])[0], ' ms')}.")
    lignes.append("Écarts signés = prix obtenu − référence, en points ; "
                  "coût pour le pari = l'écart pour un CALL, son opposé pour "
                  "un PUT (positif = défavorable). Les écarts favorables sont "
                  "gardés.")
    libelles = {"signal": "flux au signal", "clic": "flux au clic",
                "acceptation": "flux à l'acceptation",
                "flux_ouverture": "flux à l'ouverture (broker)",
                "attendu": "prix attendu par la course",
                "attendu_moins_flux": "prix attendu − flux au signal"}
    for nom, x in p["ecarts"].items():
        cout = x["cout"]
        lignes.append(
            f"• vs {libelles.get(nom, nom)} : n {x['n']}, coût médian "
            f"{_v(cout[2])} pt (p25 {_v(cout[1])}, p75 {_v(cout[3])}) ; "
            f"défavorables {x['defavorables']}, nuls {x['nuls']}, favorables "
            f"{x['favorables']} ; médiane CALL {_v(x['call'])}, PUT "
            f"{_v(x['put'])} ; par latence " + ", ".join(
                f"{_e(tr)} {v[1] if v[1] is not None else '—'} (n {v[0]})"
                for tr, v in x["par_latence"].items()))
    d = p["delais_ms"]
    lignes.append(
        "Délais (p10 / médiane / p90, ms) : " + " ; ".join(
            f"{nom.replace('_', ' → ')} {_v(v[0])} / {_v(v[1])} / {_v(v[2])}"
            for nom, v in d.items()) + ".")
    if p["horodates"] and p["seconde_ronde"] == p["horodates"]:
        lignes.append(
            "⚠ L'ouverture du broker est horodatée à la seconde entière pour "
            "tous les ordres : l'instant réel est jusqu'à 999 ms plus tard. "
            "« vs flux à l'ouverture (broker) », « acceptation → ouverture » "
            "et « signal → ouverture » portent cette troncature (d'où des "
            "délais négatifs) : ce sont des artefacts d'horodatage, pas des "
            "délais ni des coûts.")
    return lignes


def _quintiles(q: Sequence | None) -> str:
    if not q:
        return "—"
    return (f"p10 {_v(q[0])}, p25 {_v(q[1])}, médiane {_v(q[2])}, p75 "
            f"{_v(q[3])}, p90 {_v(q[4])}")


def _tableaux(r: Mapping) -> list[str]:
    def ligne(nom, v):
        tm = v["ticks_min"][2]
        dist = (v.get("distincts_min") or [None] * 5)[2]
        dbl = v.get("doublees")
        p99 = v["ecarts_ms"][3]
        return (f"{nom:7} {_pc(v['couverture'], 0):>6} "
                f"{'—' if tm is None else tm:>6} "
                f"{'—' if dist is None else dist:>5} "
                f"{'—' if dbl is None else f'{100 * dbl:.0f} %':>5} "
                f"{_ms(p99) if p99 is not None else '—':>8} {v['gels']:4} "
                f"{v['pauvres']:4} {v['inexpliques']:5} "
                f"{v['manquantes_ecoutees']:6}")
    entete = ("        couv.  tk/min  dist.  dbl. écart99 gels pauv inexp "
              "manq.é")
    lignes = ["", "<b>7. Par paire, par jour, par heure</b> (couverture des "
              "minutes attendues ; médiane des ticks par minute, puis des "
              "ticks distincts ; part des minutes à flux doublé ; 99e "
              "percentile de l'écart entre ticks)", "<pre>" + entete]
    for paire, v in r["paires"].items():
        lignes.append(ligne(paire.replace("_otc", "")[:7], v)
                      + f" {v['signaux'][1]}/{v['signaux'][0]}")
    lignes.append("</pre>")
    lignes.append("<pre>" + entete)
    for jour, v in r["jours"].items():
        lignes.append(ligne(_d(int(jour), False), v))
    lignes.append("</pre>")
    lignes.append("<pre>" + entete)
    for heure, v in sorted(r["heures"].items(), key=lambda kv: int(kv[0])):
        lignes.append(ligne(f"{int(heure):02d}h", v))
    lignes.append("</pre>")
    return lignes


def _texte_exploration(x: Mapping) -> list[str]:
    lignes = ["", "<b>9. Exploratoire — phase F</b> (rien ici ne modifie la "
              "stratégie ni la fenêtre T0/T2/T3 ; une piste devient une "
              "candidate à pré-inscrire, jamais un filtre)"]
    if not x.get("signaux"):
        lignes.append("[Fait observé] Aucun signal à fenêtre complète : pas "
                      "de trajectoire au tick à décrire.")
        return lignes
    acc = x["accord"]
    lignes.append(
        f"[Fait observé] {x['signaux']} contrats T0 suivis au tick ; issues "
        f"au tick et aux clôtures M1 identiques : " + ", ".join(
            f"{int(sec) // 60} min {v[1]}/{v[0]}" for sec, v in acc.items())
        + ".")
    ech = x["echeances"]
    if ech["communs"]:
        lignes.append(
            f"[Fait observé] Échéances sur les MÊMES {ech['communs']} "
            f"signaux : " + ", ".join(
                f"{int(sec) // 60} min {_part(ech[str(sec)][1], ech[str(sec)][0])}"
                f" (EV {1.92 * ech[str(sec)][1] / ech[str(sec)][0] - 1:+.3f})"
                for sec in microstructure.ECHEANCES) + ".")
        lignes.append(
            "[Association] Écart d'espérance par ordre, appariés : 4 − 15 min "
            f"{_v(ech['240-900']['ecart_ev'])}{_ic(ech['240-900']['ic'])}, "
            f"10 − 15 min {_v(ech['600-900']['ecart_ev'])}"
            f"{_ic(ech['600-900']['ic'])}. Les valeurs historiques citées "
            f"(T0 : +0,091 à 4 min, −0,010 à 10 min, +0,053 à 15 min) viennent "
            f"de /entrees (clôtures M1, tous les signaux de la règle de "
            f"l'heure, égalités exclues par échéance, payout 92 % supposé) : "
            f"ancien rapport, population différente, non recalculées ici.")
    tr = x["trajectoires"]
    lignes.append(
        f"[Fait observé] MFE en amplitudes M1, médiane (quartiles) : gagnants "
        f"{_q(tr['gagnants']['mfe'])} sur {tr['gagnants']['n']}, perdants "
        f"{_q(tr['perdants']['mfe'])} sur {tr['perdants']['n']} ; MAE : "
        f"gagnants {_q(tr['gagnants']['mae'])}, perdants "
        f"{_q(tr['perdants']['mae'])} ; perdants passés du bon côté à un "
        f"moment : {_pc(tr['perdants_passes_gagnants'])}.")
    ecart = tr["mfe_gagnants_moins_perdants"]
    lignes.append(f"[Association] MFE moyen gagnants − perdants : "
                  f"{_v(ecart['ecart'])}{_ic(ecart['ic'])} (attendu : un "
                  f"gagnant finit du bon côté).")
    fin = x["fin_echeance"]
    morceaux = []
    for f_sec in microstructure.FINS_SEC:
        v = fin[str(f_sec)]
        morceaux.append(
            f"{f_sec} s : perdants {_pc(v['signal'])}{_ic_pc(v['ic_signal'])}"
            f", témoins {_pc(v['temoin'])}{_ic_pc(v['ic_temoin'])}, loi "
            f"{_pc(v['arcsinus'])}")
    lignes.append(
        f"[Fait observé] Dernier passage dans la fin de l'échéance "
        f"({fin['perdants']} perdants, {fin['temoins_perdants']} témoins "
        f"perdants sans signal) : " + " ; ".join(morceaux) + ".")
    lignes.append(
        "[Hypothèse explicative] Pour une marche aléatoire, le dernier "
        "passage se concentre au début et à la fin (loi de l'arcsinus) : un "
        "« pic de fin » n'est un écart que s'il dépasse nettement les "
        "témoins. [Conclusion non établie] Une intention du broker : aucune "
        "donnée ici ne permet de l'affirmer.")
    fr = x["franchissements"]
    if fr["n"]:
        g, p = fr["gagnants"], fr["perdants"]
        e = fr["franchi_perdants_moins_gagnants"]
        lignes.append(
            f"[Fait observé] Zone franchie au tick, contre le trade : "
            f"perdants {p['franchis']}/{p['n']}, gagnants "
            f"{g['franchis']}/{g['n']} ; parmi les franchis, profondeur "
            f"médiane perdants {_q(p['profondeur'])}, gagnants "
            f"{_q(g['profondeur'])} amplitude(s) ; temps au-delà perdants "
            f"{_q(p['temps_au_dela'], ' s')}, gagnants "
            f"{_q(g['temps_au_dela'], ' s')} ; retour dans la zone perdants "
            f"{_pc(p['retour'])}, gagnants {_pc(g['retour'])}.")
        lignes.append(
            f"[Association] Part franchie, perdants − gagnants : "
            f"{_pc(e['ecart'])}{_ic_pc(e['ic'])}. [Conclusion non établie] "
            f"Un franchissement suivi d'un retour décrit une trajectoire ; il "
            f"ne prouve ni une chasse aux positions ni une manipulation.")
    return lignes
