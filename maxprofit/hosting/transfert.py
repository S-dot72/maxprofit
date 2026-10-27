"""
`/transfert` : le diagnostic du transfert sortant de Neon, en lecture seule
(demandé le 27/10, avant toute modification).

Deux sources, publiées séparément :
- **la mesure côté bot** (`store.mesure_transfert`), passive et en mémoire.
  Chaque lecture est attribuée à la fonction du projet qui l'a lancée, avec
  ses lignes, ses octets estimés sur le fil, sa portée dans le temps et les
  journées qu'elle relit ;
- **les compteurs du serveur** (`pg_stat_user_tables`), relevés toutes les
  30 min avec la taille de la base. Ce sont les lignes lues par table, toutes
  connexions confondues : un recoupement, pas une attribution.

Le rapport sépare ce qui est **mesuré** de ce qui est **extrapolé**, et dit
sur quelle période. La console de Neon fait foi pour le transfert facturé :
la mesure est une borne basse (TLS et messages de protocole non comptés),
dont le facteur se lit en comparant les deux sur 24 h.
"""

from __future__ import annotations

import csv
import html
import io
import logging
import time
from collections import Counter, defaultdict
from typing import Any

from maxprofit.store.mesure_transfert import MESURE, PORTEES, SANS_BORNES

log = logging.getLogger(__name__)

MO = 1e6
#: Relevés des compteurs du serveur, en mémoire (perdus au redémarrage).
SERVEUR: list[dict[str, Any]] = []
SERVEUR_MAX = 200

REQUETE_SERVEUR = (
    "SELECT relname, COALESCE(seq_tup_read, 0) + COALESCE(idx_tup_fetch, 0) "
    "FROM pg_stat_user_tables")
REQUETE_REMISE = ("SELECT stats_reset FROM pg_stat_database "
                  "WHERE datname = current_database()")


def echantillonner_serveur(conn, maintenant: float | None = None) -> None:
    """Un relevé des lignes lues par table, côté serveur. PostgreSQL
    seulement ; ne lève jamais."""
    from maxprofit.store import postgres
    if not postgres.est_postgres(conn):
        return
    try:
        lignes = {str(r[0]): int(r[1])
                  for r in conn.execute(REQUETE_SERVEUR).fetchall()}
        remise = conn.execute(REQUETE_REMISE).fetchone()
        SERVEUR.append({"ts": maintenant or time.time(), "lignes": lignes,
                        "remise": str(remise[0]) if remise else None})
        del SERVEUR[:-SERVEUR_MAX]
    except Exception as erreur:                          # noqa: BLE001
        log.debug("Compteurs du serveur illisibles : %s", erreur)


def _serveur() -> dict[str, Any] | None:
    """Lignes lues par table entre le premier et le dernier relevé, si les
    statistiques n'ont pas été remises à zéro entre-temps."""
    if len(SERVEUR) < 2:
        return None
    premier, dernier = SERVEUR[0], SERVEUR[-1]
    if premier["remise"] != dernier["remise"]:
        return {"remis_a_zero": True, "heures":
                (dernier["ts"] - premier["ts"]) / 3600}
    delta = {t: dernier["lignes"].get(t, 0) - premier["lignes"].get(t, 0)
             for t in dernier["lignes"]}
    return {"remis_a_zero": False,
            "heures": (dernier["ts"] - premier["ts"]) / 3600,
            "lignes": {t: v for t, v in sorted(delta.items(),
                                               key=lambda kv: -kv[1]) if v}}


def _mo(octets: float) -> str:
    return f"{octets / MO:.1f} Mo"


def _date(ts: float) -> str:
    return time.strftime("%d/%m %H:%M", time.gmtime(ts))


def rapport(course=None, instantane: dict | None = None
            ) -> tuple[str, list[tuple[bytes, str, str]]]:
    m = instantane or MESURE.instantane()
    heures = max((m["maintenant"] - m["debut"]) / 3600, 1e-9)
    par_conso: dict[tuple, list] = defaultdict(lambda: [0, 0, 0])
    par_conso_table: dict[tuple, Counter] = defaultdict(Counter)
    par_base: Counter = Counter()
    par_table: Counter = Counter()
    lignes_table: Counter = Counter()
    for (base, conso, operation, table), (req, lignes, octets) in \
            m["par_cle"].items():
        c = par_conso[(base, conso)]
        c[0] += req
        c[1] += lignes
        c[2] += octets
        par_conso_table[(base, conso)][f"{operation} ({table})"] += octets
        par_base[base] += octets
        par_table[table] += octets
        lignes_table[table] += lignes
    total = sum(par_base.values())
    rejeu = None
    apprentissage = getattr(getattr(course, "etat", None), "apprentissage",
                            None)
    if apprentissage is not None and getattr(apprentissage, "cree_ts", None):
        rejeu = float(apprentissage.cree_ts)
    l = ["📶 <b>Transfert sortant de Neon — mesure passive</b> (lecture "
         "seule, rien n'est écrit)",
         f"Période observée : depuis le démarrage, {_date(m['debut'])} UTC, "
         f"soit {heures:.1f} h. Dernier rejeu quotidien : "
         + (f"{_date(rejeu)} UTC, "
            + ("DANS la période" if rejeu >= m["debut"] else
               "AVANT la période (non mesuré)") if rejeu else "inconnu")
         + "."]
    if not m["actif"]:
        l.append(f"⚠️ Mesure désactivée après une erreur : "
                 f"{html.escape(m['erreur'] or '')}")
    l += ["", "<b>1. Total mesuré</b> (octets estimés sur le fil, borne basse)",
          f"- {_mo(total)} en {heures:.1f} h, soit {_mo(total / heures)} par "
          f"heure ; par base : " + (", ".join(
              f"{html.escape(b)} {_mo(v)}" for b, v in par_base.most_common())
              or "aucune lecture"),
          "", "<b>2. Par consommateur</b> (le plus lourd d'abord)"]
    for (base, conso), (req, lignes, octets) in sorted(
            par_conso.items(), key=lambda kv: -kv[1][2])[:12]:
        part = 100 * octets / total if total else 0
        detail = ", ".join(f"{html.escape(k)} {_mo(v)}" for k, v in
                           par_conso_table[(base, conso)].most_common(3))
        l.append(f"- {html.escape(conso)} [{html.escape(base)}] : "
                 f"{_mo(octets)} ({part:.0f} %), {req} requêtes, {lignes} "
                 f"lignes — {detail}")
    l += ["", "<b>3. Journées relues</b> (même paire, même jour, lu plusieurs "
          "fois par le même consommateur)"]
    relues = sorted(((k, v) for k, v in m["relues"].items()
                     if v["relectures"]), key=lambda kv: -kv[1]["octets_relus"])
    if not relues:
        l.append("- aucune relecture mesurée.")
    for (base, conso, table), v in relues[:10]:
        l.append(f"- {html.escape(conso)}, {html.escape(table)} : "
                 f"{v['paires_jours']} paires-jours, {v['relectures']} "
                 f"relectures, ≈ {_mo(v['octets_relus'])} relus")
    portees: Counter = Counter()
    for v in m["portees"].values():
        portees.update(v)
    tot_p = sum(portees.values())
    l += ["", "<b>4. Portée des lectures</b> (part des octets)",
          "- " + (", ".join(
              f"{nom} {100 * portees.get(nom, 0) / tot_p:.0f} %"
              for nom in [n for _, n in PORTEES] + [SANS_BORNES]
              if portees.get(nom)) if tot_p else "aucune")]
    l += ["", "<b>5. Par table</b> (mesuré ici)",
          "- " + (", ".join(f"{html.escape(t)} {_mo(v)} "
                            f"({lignes_table[t]} lignes)"
                            for t, v in par_table.most_common(8))
                  or "aucune")]
    serveur = _serveur()
    l += ["", "<b>6. Côté serveur</b> (pg_stat_user_tables, toutes "
          "connexions, lignes lues)"]
    if serveur is None:
        l.append("- moins de deux relevés (un toutes les 30 min) : "
                 "réessayer plus tard.")
    elif serveur["remis_a_zero"]:
        l.append("- statistiques remises à zéro par le serveur pendant la "
                 "période : pas de recoupement possible.")
    else:
        l.append(f"- sur {serveur['heures']:.1f} h : " + ", ".join(
            f"{html.escape(t)} {v} lignes"
            for t, v in list(serveur["lignes"].items())[:8]))
        l.append("- les lignes PARCOURUES par le serveur dépassent les lignes "
                 "RENDUES : un recoupement de l'ordre de grandeur, pas une "
                 "attribution.")
    l += ["", "<b>7. Extrapolation</b> (pas une mesure)",
          f"- au rythme observé : {_mo(total / heures * 24)} par 24 h"
          + (" ; la période ne contient pas de rejeu quotidien, qui s'y "
             "ajouterait" if not rejeu or rejeu < m["debut"] else
             " ; la période contient un rejeu quotidien")
          + (" ; moins de 24 h observées : rythme fragile"
             if heures < 24 else ""),
          "- facteur réel : noter le compteur « Network transfer » de la "
          "console Neon maintenant et dans 24 h, puis le comparer au total "
          "mesuré ici sur la même période.",
          "", "<b>Incertitudes</b> : TLS et messages de protocole non "
          "comptés ; octets estimés sur le format texte de PostgreSQL ; "
          "remise à zéro à chaque redémarrage ; la relève est un autre projet "
          "Neon, avec son propre plafond."]
    return "\n".join(l), [(_csv(m), "transfert.csv",
                           "Transfert mesuré : base × consommateur × "
                           "opération × table")]


def _csv(m: dict) -> bytes:
    sortie = io.StringIO()
    w = csv.writer(sortie, delimiter=";")
    w.writerow(["base", "consommateur", "operation", "table", "requetes",
                "lignes", "octets_estimes"])
    for (base, conso, operation, table), (req, lignes, octets) in sorted(
            m["par_cle"].items(), key=lambda kv: -kv[1][2]):
        w.writerow([base, conso, operation, table, req, lignes, octets])
    return sortie.getvalue().encode("utf-8")
