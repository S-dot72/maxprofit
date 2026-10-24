"""
/ticks — l'étape 0 : la qualité des ticks, en lecture seule.

Le calcul est dans `apprentissage.audit_ticks`, qui ne touche pas la base ;
ce module en fait les LECTURES (SELECT seulement, `REQUETES`) et la mise en
forme pour Telegram. Rien n'y décide quoi que ce soit : la course et la
fenêtre T0/T2/T3 n'en lisent rien.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Mapping

from maxprofit.apprentissage.audit_ticks import (
    BORD_SEC, EXPLICABLES, FENETRES_PRIX_MS, GEL_SEC, IDENTIQUE, INEXPLIQUE,
    INTERRUPTION_LONGUE_MIN, MOTIFS, REQUETES, AuditTicks)
from maxprofit.store.chemin_ticks import decoder

#: Jours audités par défaut : 7 jours, environ 20 Mo de chemins à relire —
#: l'audit tourne chaque jour avec le rejeu, sur une base au quota compté.
JOURS_PAR_DEFAUT = 7


def jours_audites() -> int:
    """`AUDIT_TICKS_JOURS` : jours audités (7 par défaut, 0 désactive)."""
    import os
    try:
        return max(0, int(os.environ.get("AUDIT_TICKS_JOURS",
                                         JOURS_PAR_DEFAUT)))
    except ValueError:
        return JOURS_PAR_DEFAUT


def nouvel_audit(debut: int, fin: int, joue=None) -> AuditTicks:
    """L'audit de [debut, fin[, avec le décodeur des chemins stockés."""
    return AuditTicks(debut, fin, decoder=decoder, joue=joue)


def lire_battements(conn, debut: int, fin: int) -> list[int]:
    """Les battements du collecteur de [debut, fin[ (table `uptime`)."""
    return [int(r[0]) for r in conn.execute(
        REQUETES["battements"], (int(debut), int(fin))).fetchall()]


def lire_ordres(conn) -> list[dict]:
    """Les ordres réels acceptés, tels que le journal les a gardés. Une table
    absente ou plus ancienne (sans `decalage_broker_ms`) rend une liste vide :
    le contrôle des prix est alors indéterminé, pas faux."""
    cles = ("pair", "sens", "signal_ts_ms", "prix_attendu", "prix_entree",
            "prix_sortie", "ouverture_ts_ms", "expiration_ts_ms",
            "decalage_broker_ms", "brut")
    try:
        lignes = conn.execute(REQUETES["ordres"]).fetchall()
    except Exception:                              # noqa: BLE001
        return []
    return [dict(zip(cles, ligne)) for ligne in lignes]

ICONES = {"PASS": "✅", "WARNING": "⚠️", "FAIL": "❌", "INDÉTERMINÉ": "❔"}
NOMS = {"horodatages": "Horodatages", "concordance": "Concordance M1",
        "trous": "Trous de collecte", "signaux": "Couverture des signaux",
        "prix": "Prix d'exécution"}


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
        return "au-delà de 60 s"
    return f"{x / 1000:g} s" if x >= 1000 else f"{x} ms"


def _n(x: int) -> str:
    return f"{x:,}".replace(",", " ")


def _e(x) -> str:
    return html.escape(str(x), quote=False)


def texte(r: Mapping | None, fichier: bool = False) -> str:
    if r and "erreur" in r:
        return (f"🔬 Qualité des ticks : l'audit a échoué au dernier rejeu "
                f"({_e(r['erreur'])}). Le rejeu, lui, a continué ; détails "
                f"dans les journaux du service.")
    if not r or "controles" not in r:
        return ("🔬 Qualité des ticks : pas encore mesurée — le prochain rejeu "
                "du laboratoire la calculera (AUDIT_TICKS_JOURS, 7 jours par "
                "défaut ; 0 la désactive).")
    c = r["controles"]
    h, k, t, s, p = (c["horodatages"], c["concordance"], c["trous"],
                     c["signaux"], c["prix"])
    jours = (r["fin"] - r["debut"]) / 86400
    lignes = [
        f"🔬 <b>Étape 0 — qualité des ticks</b> (lecture seule) — du "
        f"{_d(r['debut'])} au {_d(r['fin'])} UTC ({jours:g} j audités), "
        f"{len(r['paires'])} paires, {_n(h['ticks'])} ticks dans "
        f"{_n(t['minutes_avec_ticks'])} minutes.",
        "Aucune table n'est modifiée ; la course et la fenêtre T0/T2/T3 n'en "
        "lisent rien.", "", "<b>Résumé</b>"]
    resume = {
        "horodatages": (
            f"résolution {_ms(h['granularite_ms'])} ; {h['hors_minute']} "
            f"hors de sa minute ; doublons exacts "
            f"{_part(h['doublons_exacts'], h['ticks'])} ; gels de {GEL_SEC} s "
            f"ou plus sur {_part(h['minutes_gelees'], t['minutes_avec_ticks'])}"
            f" des minutes"),
        "concordance": (
            f"{_part(k['classes'].get(IDENTIQUE, 0), k['comparees'])} "
            f"identiques sur {k['comparees']} ; explicables "
            f"{_part(sum(k['classes'].get(x, 0) for x in EXPLICABLES), k['comparees'])}"
            f" ; inexpliquées {k['classes'].get(INEXPLIQUE, 0)}"),
        "trous": (
            f"{_pc(t['part_manquante_ecoutee'])} des minutes écoutées sans "
            f"ticks ; {t['interruptions_longues']} interruption(s) longue(s)"),
        "signaux": (
            f"{_part(s['signaux'][1], s['signaux'][0])} de "
            f"{s['signaux'][0]} fenêtres de 30 min complètes"),
        "prix": (
            f"ouverture retrouvée à ± 1 s : "
            f"{_part(p['ouverture']['trouves'][1], p['ouverture']['comparables'])}"
            f" de {p['ouverture']['comparables']} ; clôture : "
            f"{_part(p['cloture']['trouves'][1], p['cloture']['comparables'])}"
            f" de {p['cloture']['comparables']}"),
    }
    for cle, nom in NOMS.items():
        statut = r["statuts"][cle]
        lignes.append(f"{ICONES[statut]} {statut} — {nom} : {resume[cle]}")

    lignes += [
        "", "<b>1. Horodatages</b> (heure du serveur du broker, en UTC)",
        f"Granularité : {_ms(h['granularite_ms'])} (à la seconde ronde : "
        f"{_pc(h['part_seconde_ronde'])}) ; même instant : "
        f"{h['doublons_instant']} (dont {h['doublons_exacts']} doublons "
        f"exacts) ; hors de leur minute : {h['hors_minute']} ; décroissants : "
        f"{h['decroissants']} ; chemins illisibles : {h['illisibles']}.",
        f"Écart entre ticks consécutifs (minutes contiguës) : médiane ≤ "
        f"{_ms(h['ecarts_ms']['p50'])}, 90 % ≤ {_ms(h['ecarts_ms']['p90'])}, "
        f"99 % ≤ {_ms(h['ecarts_ms']['p99'])}, max "
        f"{_ms(h['ecarts_ms']['max'])} ; minutes avec un gel de {GEL_SEC} s "
        f"ou plus : {h['minutes_gelees']}.",
        "", "<b>2. Concordance M1</b> (bougies et chemins viennent des mêmes "
        "ticks : cohérence du stockage, pas exactitude du flux)",
        f"{k['comparees']} minutes comparées : " + " · ".join(
            f"{nom} {k['classes'].get(nom, 0)}"
            for nom in (IDENTIQUE, *EXPLICABLES, INEXPLIQUE))
        + (" ; champs en défaut : " + ", ".join(
            f"{x} {v}" for x, v in k["champs_inexpliques"].items())
           if k["champs_inexpliques"] else "") + ".",
        f"Minutes de ticks sans bougie : {k['chemin_sans_bougie']} ; bougies "
        f"sans chemin : {k['bougie_sans_chemin']}.",
        "", "<b>3. Trous et débit</b> (une absence de données n'est pas une "
        "absence de mouvement)",
        f"Minutes attendues {t['minutes_attendues']} (paires × minutes), avec "
        f"ticks {t['minutes_avec_ticks']} ; sans ticks alors que le "
        f"collecteur battait {t['manquantes_ecoutees']} "
        f"({_pc(t['part_manquante_ecoutee'])} des écoutées), collecteur muet "
        f"{t['manquantes_muettes']} ; battements lus {t['battements']} ; "
        f"minutes de ticks sans battement (horodatage décalé ?) "
        f"{t['minutes_sans_battement']} ; paires sans aucun tick : "
        f"{', '.join(x.replace('_otc', '') for x in t['paires_sans_ticks']) or 'aucune'}.",
        f"Interruptions de {INTERRUPTION_LONGUE_MIN} min ou plus : "
        f"{t['interruptions_longues']} ; "
        f"débits anormaux (jour hors de 0,5 à 2 fois la médiane de sa paire) "
        f": {t['debits_anormaux']} ; ticks par minute : p10 "
        f"{t['ticks_par_minute'][0]}, médiane {t['ticks_par_minute'][1]}, "
        f"p90 {t['ticks_par_minute'][2]}.",
        "", f"<b>4. Couverture des signaux ZoneH1</b> (30 min : 15 avant la "
        f"décision, 15 après ; bornes à {BORD_SEC} s, sans gel)",
        f"Tous : {s['signaux'][1]}/{s['signaux'][0]} complets "
        f"({_part(s['signaux'][1], s['signaux'][0])}) ; joués (règle de "
        f"l'heure) : {s['joues'][1]}/{s['joues'][0]} "
        f"({_part(s['joues'][1], s['joues'][0])}) ; trop près d'un bord de "
        f"la fenêtre auditée : {s['hors_fenetre']}.",
        "Motifs (le premier rencontré) : " + " · ".join(
            f"{m} {s['motifs'].get(m, 0)}" for m in MOTIFS) + ".",
        "", "<b>5. Prix d'exécution</b> (instants dans l'horloge du broker ; "
        "le dernier tick reçu n'est pas supposé être le prix du clic)",
        f"Ordres : {p['ordres']} (rapatriés {p['rapatries']}, décalage "
        f"d'horloge inconnu {p['sans_decalage']}) ; ouverture horodatée à la "
        f"seconde ronde : {p['seconde_ronde']}/{p['ordres']} ; "
        f"synchronisation broker − nous, heures entières ôtées : p10 "
        f"{p['synchronisation_ms'][0]} ms, médiane "
        f"{p['synchronisation_ms'][1]} ms, p90 {p['synchronisation_ms'][2]} "
        f"ms."]
    for cle, nom in (("ouverture", "Prix d'ouverture du broker"),
                     ("cloture", "Prix de clôture du broker"),
                     ("attendu", "Prix vu par la course au signal")):
        v = p[cle]
        lignes.append(
            f"{nom} : {v['comparables']} comparable(s) sur {v['ordres']} ; "
            f"retrouvé " + ", ".join(
                f"{'au dernier tick' if w == 0 else f'à ± {w // 1000} s'} "
                f"{_part(v['trouves'][i], v['comparables'])}"
                for i, w in enumerate(FENETRES_PRIX_MS))
            + f" ; écart médian au dernier tick : "
              f"{v['ecart_points_median'] if v['ecart_points_median'] is not None else '—'}"
              f" point(s).")

    lignes += ["", "<b>Par paire</b>", "<pre>paire   minutes tk/min gels "
               "inexp manq.éc signaux"]
    for paire, v in r["paires"].items():
        lignes.append(
            f"{paire.replace('_otc', ''):7} {v['minutes']:7} "
            f"{v['ticks_min'][1] if v['ticks_min'][1] is not None else '—':>6} "
            f"{v['gels']:4} {v['inexpliques']:5} "
            f"{v['manquantes_ecoutees']:7} "
            f"{v['signaux'][1]}/{v['signaux'][0]}")
    lignes.append("</pre>")
    lignes += ["<b>Par jour</b>", "<pre>jour   minutes tk/min gels inexp "
               "manq.éc"]
    for jour, v in r["jours"].items():
        lignes.append(
            f"{_d(int(jour), False):6} {v['minutes']:7} "
            f"{v['ticks_min'][1] if v['ticks_min'][1] is not None else '—':>6} "
            f"{v['gels']:4} {v['inexpliques']:5} {v['manquantes_ecoutees']:7}")
    lignes.append("</pre>")
    lignes.append("<b>Par heure UTC</b> (médiane ticks/min · minutes "
                  "manquantes écoutées)")
    heures = [f"{int(hh):02d}h {v['ticks_min'][1] if v['ticks_min'][1] is not None else '—'}"
              f" · {v['manquantes_ecoutees']}"
              for hh, v in sorted(r["heures"].items(), key=lambda kv: int(kv[0]))]
    lignes += ["   ".join(heures[i:i + 4]) for i in range(0, 24, 4)]

    a = r["anomalies"]
    totaux = {g: sum(v for gr, _n, v in a["comptes"] if gr == g)
              for g in ("FAIL", "WARNING", "INFO")}
    lignes += ["", f"<b>Anomalies</b> — FAIL {totaux['FAIL']} · WARNING "
                   f"{totaux['WARNING']} · INFO {totaux['INFO']}"]
    if a["comptes"]:
        lignes.append("Par nature : " + " · ".join(
            f"{gr} {_e(nature)} {v}" for gr, nature, v in a["comptes"]))
    for an in a["premieres"][:10]:
        lignes.append(f"• {an['gravite']} {an['paire'].replace('_otc', '')} "
                      f"{_d(an['debut'])} : {_e(an['nature'])} — "
                      f"{_e(an['detail'])}")
    if fichier:
        lignes.append("📎 Fiche complète des anomalies dans le fichier joint "
                      "(CSV, séparateur « ; »).")
    elif a["comptes"]:
        lignes.append("La fiche complète (CSV) revient au prochain rejeu : "
                      "elle n'est gardée qu'en mémoire.")

    lignes += ["", "<b>Limites</b>"] + [f"• {_e(x)}" for x in r["limites"]]

    rep = r["reproductibilite"]
    lignes += [
        "", "<b>Reproduire</b>",
        f"Code {_e(rep['commit'] or '—')} · empreinte des chemins "
        f"{rep['empreinte']} sur {rep['chemins_lus']} chemin(s) · fenêtre "
        f"[{r['debut']} ; {r['fin']}[ (secondes UTC).",
        f"Hors du bot, mêmes calculs : <code>python outils/audit_ticks.py "
        f"--debut {r['debut']} --fin {r['fin']}</code> (DATABASE_URL lue dans "
        f"l'environnement ; lecture seule).",
        "Seuils : " + " | ".join(f"{_e(x)} — {_e(v)}"
                                 for x, v in rep["seuils"].items()),
        "Requêtes (SELECT seulement) :"] + [
        f"• {_e(x)} : <code>{_e(v)}</code>"
        for x, v in rep["requetes"].items()]
    return "\n".join(lignes)
