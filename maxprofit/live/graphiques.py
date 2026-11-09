"""
`/graphiques` : la revue des graphiques à l'aveugle, en LECTURE SEULE.

Demandé le 04/11 :

> « Il faut chercher les micro structures ou indices qui valident ce qu'on
> voulait faire […] déterminer s'il y a un ou plusieurs trucs qui nous
> disent si ça va aller comme on le pensait. Vérifie les bougies, leurs
> types, leurs tailles, leur juxtaposition. »

La passe tire au hasard des signaux ZoneH1 JOUABLES (flux ≥ 84), autant de
gagnants que de perdants (issue de l'entrée immédiate, que /rapprochement a
validée face au broker), et rend pour chacun le graphique au moment de
l'entrée.

Le tirage suit des règles fixes :
- décisions d'avant le 03/11 00:00 UTC : les confirmations de H1/H2 et de
  H_IB s'ouvrent ensuite, et leurs issues ne se regardent pas ;
- un seul signal par zone : les signaux d'une même paire à moins de 15
  minutes d'intervalle n'en font qu'un, le premier ;
- graine fixe, ordre des graphiques mélangé.

Les fichiers rendus :
- `graphiques_aveugles.html` : chaque graphique S'ARRÊTE à l'entrée. On y
  voit 5 h de contexte, un zoom sur les 60 dernières bougies, la zone et le
  sens prévu. Aucune issue n'y figure ;
- `graphiques_reponses.html` : les mêmes graphiques, avec les 15 minutes
  qui suivent et le résultat ;
- `graphiques_bougies.csv` (bougies jusqu'à l'entrée) et
  `graphiques_signaux.csv` (paire, sens, zone), pour une revue hors ligne ;
- `graphiques_issues.csv` : les résultats, À PART, à n'envoyer qu'une fois
  la revue figée.

`/graphiques 2` (lot 2, puis 3…) tire un nouveau lot, graine 7 + lot − 1,
sans aucun signal des lots déjà revus ni de leurs zones (`REVUS`).

Une revue visuelle FAIT NAÎTRE des idées ; elle n'en valide aucune. Tout
indice qui en sort se vérifie sur l'ensemble des signaux, puis se gèle et se
confirme sur des données neuves.
"""

from __future__ import annotations

import html
import random
import time
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core.payout import FLUX_POUR_LE_PLAFOND
from maxprofit.live import bougies_entree as be
from maxprofit.live import etude3
from maxprofit.live.etude3 import ConnexionTracee, etat_lecture_seule
from maxprofit.live.rapprochement import PAYOUT_AVANT
from maxprofit.store.market import MarketReader

#: Les confirmations (gel 2, H_IB) s'ouvrent le 03/11 et le 04/11.
FIN_TIRAGE = int(datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp())
PAR_ISSUE = 40
GRAINE = 7
#: Deux signaux d'une même paire à moins de 15 min : la même zone.
ECART_ZONE_SEC = 900
CONTEXTE_MIN = 300                    # 5 h de contexte
ZOOM = 60                             # les 60 dernières bougies
APRES = em.HORIZON_SEC // 60 + 1      # les 15 minutes qui suivent, et la sortie
PAYOUTS_MAX = 1500                    # plafond de requêtes de payout
RESPIRATION_SEC = 0.02

#: Le lot 1, revu le 04/11 (graine 7) : exclu des lots suivants, qui doivent
#: porter sur des graphiques JAMAIS vus. Liste figée (paire, décision).
DEJA_REVUS: frozenset[tuple[str, int]] = frozenset({
    ("AUDCAD_otc", 1791935520), ("AUDCAD_otc", 1792176300), ("AUDCAD_otc", 1793200800),
    ("AUDCAD_otc", 1793406780), ("AUDCHF_otc", 1792364340), ("AUDCHF_otc", 1792439940),
    ("AUDCHF_otc", 1793620620), ("AUDNZD_otc", 1792526880), ("AUDNZD_otc", 1793643360),
    ("AUDUSD_otc", 1792676760), ("AUDUSD_otc", 1792690440), ("AUDUSD_otc", 1792835880),
    ("AUDUSD_otc", 1792849560), ("AUDUSD_otc", 1793502840), ("CADCHF_otc", 1792431720),
    ("CADCHF_otc", 1792685160), ("CADJPY_otc", 1793460840), ("CHFJPY_otc", 1791869400),
    ("CHFJPY_otc", 1791929160), ("CHFJPY_otc", 1792539780), ("CHFJPY_otc", 1792787040),
    ("CHFJPY_otc", 1792954140), ("CHFJPY_otc", 1793564220), ("EURCHF_otc", 1792518960),
    ("EURCHF_otc", 1793439960), ("EURCHF_otc", 1793608500), ("EURGBP_otc", 1791780420),
    ("EURJPY_otc", 1791773640), ("EURJPY_otc", 1791840060), ("EURJPY_otc", 1791967200),
    ("EURJPY_otc", 1792067460), ("EURJPY_otc", 1792549020), ("EURJPY_otc", 1792856640),
    ("EURJPY_otc", 1793520180), ("EURJPY_otc", 1793522880), ("EURNZD_otc", 1792018260),
    ("EURNZD_otc", 1792436340), ("EURNZD_otc", 1793018520), ("EURNZD_otc", 1793623020),
    ("EURUSD_otc", 1791676920), ("EURUSD_otc", 1791810480), ("EURUSD_otc", 1791918180),
    ("EURUSD_otc", 1792033020), ("EURUSD_otc", 1792622820), ("EURUSD_otc", 1792707000),
    ("EURUSD_otc", 1792976100), ("EURUSD_otc", 1793541600), ("EURUSD_otc", 1793600340),
    ("GBPAUD_otc", 1792448760), ("GBPAUD_otc", 1792466520), ("GBPAUD_otc", 1792535760),
    ("GBPAUD_otc", 1792650420), ("GBPJPY_otc", 1791774000), ("GBPJPY_otc", 1791872280),
    ("GBPJPY_otc", 1792013100), ("GBPJPY_otc", 1792153440), ("GBPJPY_otc", 1792574580),
    ("GBPJPY_otc", 1792991160), ("GBPJPY_otc", 1793091720), ("GBPJPY_otc", 1793570280),
    ("GBPJPY_otc", 1793574360), ("GBPUSD_otc", 1791665100), ("GBPUSD_otc", 1791865080),
    ("GBPUSD_otc", 1791866400), ("GBPUSD_otc", 1791991740), ("GBPUSD_otc", 1792684620),
    ("NZDUSD_otc", 1791895560), ("NZDUSD_otc", 1792059600), ("NZDUSD_otc", 1792154100),
    ("NZDUSD_otc", 1792442940), ("USDCAD_otc", 1792526400), ("USDCAD_otc", 1792763040),
    ("USDCAD_otc", 1792848060), ("USDCAD_otc", 1793478180), ("USDCHF_otc", 1791813360),
    ("USDCHF_otc", 1793093640), ("USDJPY_otc", 1791646500), ("USDJPY_otc", 1791729060),
    ("USDJPY_otc", 1793520720), ("USDJPY_otc", 1793557200),
})
#: Le lot 2, revu le 08/11 (graine 8, issues lues après les notes figées) :
#: exclu à partir du lot 3. Liste figée (paire, décision).
DEJA_REVUS_LOT2: frozenset[tuple[str, int]] = frozenset({
    ("AUDCAD_otc", 1791689280), ("AUDCAD_otc", 1791701340), ("AUDCAD_otc", 1791737400),
    ("AUDCAD_otc", 1792138860), ("AUDCAD_otc", 1792155660), ("AUDCAD_otc", 1793307600),
    ("AUDCAD_otc", 1793398020), ("AUDCHF_otc", 1793505840), ("AUDCHF_otc", 1793594400),
    ("AUDNZD_otc", 1791759600), ("AUDNZD_otc", 1792475580), ("AUDNZD_otc", 1792525080),
    ("AUDNZD_otc", 1792893240), ("AUDUSD_otc", 1792582920), ("AUDUSD_otc", 1792785240),
    ("AUDUSD_otc", 1793595240), ("CADCHF_otc", 1792069020), ("CADCHF_otc", 1793632500),
    ("CADJPY_otc", 1791815820), ("CADJPY_otc", 1792525440), ("CADJPY_otc", 1793631660),
    ("CHFJPY_otc", 1791655500), ("CHFJPY_otc", 1791951060), ("CHFJPY_otc", 1792742280),
    ("CHFJPY_otc", 1792946700), ("CHFJPY_otc", 1793454720), ("CHFJPY_otc", 1793528820),
    ("EURCHF_otc", 1792496700), ("EURCHF_otc", 1792859460), ("EURCHF_otc", 1793607240),
    ("EURGBP_otc", 1791767640), ("EURGBP_otc", 1792535820), ("EURGBP_otc", 1793065620),
    ("EURJPY_otc", 1791685860), ("EURJPY_otc", 1792091880), ("EURJPY_otc", 1792665840),
    ("EURJPY_otc", 1792768680), ("EURJPY_otc", 1793096580), ("EURNZD_otc", 1791905280),
    ("EURNZD_otc", 1791943140), ("EURNZD_otc", 1791980520), ("EURNZD_otc", 1792431180),
    ("EURUSD_otc", 1791648180), ("EURUSD_otc", 1791738540), ("EURUSD_otc", 1792669380),
    ("EURUSD_otc", 1792865160), ("EURUSD_otc", 1792878600), ("EURUSD_otc", 1792919220),
    ("GBPAUD_otc", 1792671420), ("GBPAUD_otc", 1792882740), ("GBPAUD_otc", 1793051160),
    ("GBPAUD_otc", 1793097240), ("GBPAUD_otc", 1793560560), ("GBPJPY_otc", 1791793680),
    ("GBPJPY_otc", 1792035660), ("GBPUSD_otc", 1791712620), ("GBPUSD_otc", 1791731160),
    ("GBPUSD_otc", 1792071240), ("GBPUSD_otc", 1792918680), ("NZDJPY_otc", 1792662840),
    ("NZDJPY_otc", 1793481540), ("NZDUSD_otc", 1791950100), ("NZDUSD_otc", 1792473780),
    ("NZDUSD_otc", 1792911180), ("USDCAD_otc", 1791692160), ("USDCAD_otc", 1791696000),
    ("USDCAD_otc", 1791898320), ("USDCAD_otc", 1791997500), ("USDCAD_otc", 1792084680),
    ("USDCAD_otc", 1792520640), ("USDCAD_otc", 1792859460), ("USDCAD_otc", 1792927320),
    ("USDCAD_otc", 1793583240), ("USDCHF_otc", 1792573920), ("USDJPY_otc", 1791645480),
    ("USDJPY_otc", 1791927600), ("USDJPY_otc", 1792007940), ("USDJPY_otc", 1792131420),
    ("USDJPY_otc", 1792692480), ("USDJPY_otc", 1793660520),
})
#: Les lots déjà revus, par numéro. Le lot N exclut les lots 1 à N − 1, et
#: ne se tire que si tous ceux-là sont enregistrés ici.
REVUS: dict[int, frozenset[tuple[str, int]]] = {1: DEJA_REVUS,
                                                2: DEJA_REVUS_LOT2}
LOT_MAX = 9


def analyser_lot(argument: str) -> int:
    """« », « 2 » ou « lot 2 » → le numéro du lot (1 par défaut)."""
    chiffres = [m for m in argument.lower().replace("lot", " ").split()
                if m.isdigit()]
    lot = int(chiffres[0]) if chiffres else 1
    if not 1 <= lot <= LOT_MAX:
        raise ValueError(f"lot entre 1 et {LOT_MAX}")
    manquants = [k for k in range(1, lot) if k not in REVUS]
    if manquants:
        raise ValueError(f"lot {lot} indisponible : le lot {manquants[0]} "
                         f"n'est pas encore enregistré comme revu")
    return lot


def deja_revu(e, jusqu_au_lot: int = 1) -> bool:
    """Un signal d'un lot déjà revu (1 à `jusqu_au_lot`), ou de sa zone
    (même paire, à moins de 15 min)."""
    return any(p == e.pair and abs(d - e.ts_sec) < ECART_ZONE_SEC
               for k in range(1, jusqu_au_lot + 1)
               for p, d in REVUS.get(k, ()))

ATTENDRE_LE_REJEU = (
    "🖼 Le rejeu quotidien n'est pas encore en mémoire : il tourne quelques "
    "minutes après chaque démarrage. Relance /graphiques quand /lecons "
    "annonce le nouvel apprentissage.")


def _utc(ts: int, forme: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime(forme)


def candidats(exemples, fin: int = FIN_TIRAGE) -> list:
    """Les signaux à issue décisive, avec leur zone, un seul par zone."""
    tries = sorted((e for e in exemples if e.ts_sec < fin
                    and e.contexte.get("niveau") is not None),
                   key=lambda e: (e.pair, e.ts_sec))
    sortie, dernier = [], {}
    for e in tries:
        precedent = dernier.get(e.pair)
        dernier[e.pair] = e.ts_sec
        if precedent is not None and e.ts_sec - precedent < ECART_ZONE_SEC:
            continue
        sortie.append(e)
    return sortie


def tirer(candidats_: Sequence, jouable: Callable[[Any], bool],
          par_issue: int = PAR_ISSUE, graine: int = GRAINE) -> list:
    """`par_issue` gagnants et autant de perdants jouables, tirés au
    hasard (graine fixe), puis mélangés."""
    alea = random.Random(graine)
    ordre = list(candidats_)
    alea.shuffle(ordre)
    gagnants, perdants, vus = [], [], 0
    for e in ordre:
        cible = gagnants if e.gagne else perdants
        if len(cible) >= par_issue:
            if len(gagnants) >= par_issue and len(perdants) >= par_issue:
                break
            continue
        if vus >= PAYOUTS_MAX:
            break
        vus += 1
        if jouable(e):
            cible.append(e)
    tirage = gagnants + perdants
    alea.shuffle(tirage)
    return tirage


# --------------------------------------------------------------------------- #
# Le dessin
# --------------------------------------------------------------------------- #

def _panneau(bougies: Sequence, x0: float, y0: float, largeur: float,
             hauteur: float, niveau: float, n_avant: int,
             montrer_apres: bool) -> list[str]:
    """Un panneau de chandeliers en SVG ; les bougies d'après l'entrée sont
    sur fond gris, et seulement si `montrer_apres`."""
    visibles = bougies if montrer_apres else bougies[:n_avant]
    if not visibles:
        return []
    hauts = [b.high for b in visibles] + [niveau]
    bas = [b.low for b in visibles] + [niveau]
    p_haut, p_bas = max(hauts), min(bas)
    marge = (p_haut - p_bas) * 0.05 or 1e-9
    p_haut, p_bas = p_haut + marge, p_bas - marge
    n = len(bougies) if montrer_apres else n_avant
    pas = largeur / max(n, 1)

    def y(p: float) -> float:
        return y0 + (p_haut - p) / (p_haut - p_bas) * hauteur

    out = [f'<rect x="{x0}" y="{y0}" width="{largeur}" height="{hauteur}" '
           f'fill="#ffffff" stroke="#cccccc"/>']
    if montrer_apres and n > n_avant:
        out.append(f'<rect x="{x0 + n_avant * pas:.1f}" y="{y0}" '
                   f'width="{(n - n_avant) * pas:.1f}" height="{hauteur}" '
                   f'fill="#eeeeee"/>')
    out.append(f'<line x1="{x0}" x2="{x0 + largeur}" y1="{y(niveau):.1f}" '
               f'y2="{y(niveau):.1f}" stroke="#1f6fd1" stroke-width="1" '
               f'stroke-dasharray="5,3"/>')
    for i, b in enumerate(visibles):
        cx = x0 + (i + 0.5) * pas
        couleur = "#1a9850" if b.close >= b.open else "#d73027"
        out.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(b.high):.1f}" '
                   f'y2="{y(b.low):.1f}" stroke="{couleur}" '
                   f'stroke-width="1"/>')
        haut, bas_c = y(max(b.open, b.close)), y(min(b.open, b.close))
        out.append(f'<rect x="{cx - pas * 0.35:.1f}" y="{haut:.1f}" '
                   f'width="{max(pas * 0.7, 0.8):.1f}" '
                   f'height="{max(bas_c - haut, 0.6):.1f}" fill="{couleur}"/>')
    xe = x0 + n_avant * pas
    out.append(f'<line x1="{xe:.1f}" x2="{xe:.1f}" y1="{y0}" '
               f'y2="{y0 + hauteur}" stroke="#555555" stroke-width="1"/>')
    return out


def svg(numero: int, e, bougies_avant: Sequence, bougies_apres: Sequence,
        montrer_apres: bool) -> str:
    """Le graphique n° `numero` : contexte de 5 h en haut, zoom sur les 60
    dernières bougies en bas (et les 15 minutes d'après si demandé)."""
    call = bool(e.contexte.get("sens_call"))
    niveau = float(e.contexte["niveau"])
    titre = (f"Graphique {numero} — {e.pair} — "
             f"{'CALL (achat)' if call else 'PUT (vente)'} — entrée "
             f"{_utc(e.ts_sec)} UTC — zone {niveau:g}")
    if montrer_apres:
        titre += f" — {'GAGNÉ' if e.gagne else 'PERDU'}"
    l, h = 960, 600
    morceaux = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{l}" '
                f'height="{h}" viewBox="0 0 {l} {h}" '
                f'font-family="sans-serif" font-size="13">',
                f'<rect width="{l}" height="{h}" fill="#fafafa"/>',
                f'<text x="10" y="20" font-weight="bold">'
                f'{html.escape(titre)}</text>',
                '<text x="10" y="40" fill="#555">5 h de contexte</text>']
    contexte = list(bougies_avant[-CONTEXTE_MIN:])
    apres = list(bougies_apres) if montrer_apres else []
    morceaux += _panneau(contexte + apres, 10, 48, l - 20, 200, niveau,
                         len(contexte), montrer_apres)
    morceaux.append(f'<text x="10" y="272" fill="#555">Les {ZOOM} dernières '
                    f'bougies avant l\'entrée'
                    + (" et les 15 minutes d'après" if montrer_apres else "")
                    + "</text>")
    zoom = list(bougies_avant[-ZOOM:])
    morceaux += _panneau(zoom + apres, 10, 280, l - 20, 300, niveau,
                         len(zoom), montrer_apres)
    morceaux.append("</svg>")
    return "".join(morceaux)


def page(titre: str, graphiques: Sequence[str], intro: str) -> bytes:
    corps = "\n".join(f'<div style="margin:12px 0">{g}</div>'
                      for g in graphiques)
    return (f'<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, '
            f'initial-scale=1"><title>{html.escape(titre)}</title>'
            f'<style>body{{font-family:sans-serif;background:#fafafa;'
            f'color:#222;margin:16px}} svg{{max-width:100%;height:auto}}'
            f'</style></head><body><h1>{html.escape(titre)}</h1>'
            f'<p>{intro}</p>{corps}</body></html>').encode("utf-8")


# --------------------------------------------------------------------------- #
# La passe
# --------------------------------------------------------------------------- #

def revue(lue_brute, active_brute, course, *, nom_lecture: str = "relève",
          lot: int = 1, respirer: Callable[[], None] | None = None
          ) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers). Les bases sont déjà en lecture seule. À partir
    du lot 2, la graine change et les lots déjà revus sont exclus ; les issues partent
    dans un fichier À PART, à n'ouvrir qu'après la revue."""
    respirer = respirer or (lambda: time.sleep(RESPIRATION_SEC))
    t0 = time.monotonic()
    memoire = getattr(course, "rejeu_en_memoire", None)
    if not memoire:
        return ATTENDRE_LE_REJEU, []
    lue = ConnexionTracee(lue_brute)
    active = ConnexionTracee(active_brute) \
        if active_brute is not None and active_brute is not lue_brute \
        else None
    lecture = {nom_lecture: etat_lecture_seule(lue)}
    if active is not None:
        lecture["active"] = etat_lecture_seule(active)
    source_payout = active if active is not None else lue

    def jouable(e) -> bool:
        ligne = source_payout.execute(PAYOUT_AVANT,
                                      (e.pair, e.ts_sec)).fetchone()
        return bool(ligne) and ligne[0] is not None and \
            float(ligne[0]) >= FLUX_POUR_LE_PLAFOND

    tous = candidats(memoire[0])
    if lot >= 2:
        tous = [e for e in tous if not deja_revu(e, lot - 1)]
    tirage = tirer(tous, jouable, graine=GRAINE + lot - 1)
    if not tirage:
        return "🖼 Aucun signal jouable à dessiner avant le 03/11.", []
    debut = min(e.ts_sec for e in tirage) - CONTEXTE_MIN * 60 - 86400
    fin = max(e.ts_sec for e in tirage) + 2 * 86400
    divergents, copie = be.verifier_la_copie(lue, active, debut, fin)
    lecteurs = {"lue": MarketReader(lue),
                "active": MarketReader(active) if active is not None
                else None}
    aveugles, reponses, lignes_b, lignes_s, lignes_i = [], [], [], [], []
    for numero, e in enumerate(tirage, 1):
        d = e.ts_sec
        jours = {(d - CONTEXTE_MIN * 60 - 120) // 86400,
                 (d + em.HORIZON_SEC + 120) // 86400}
        ou = "active" if any((e.pair, j) in divergents for j in jours) \
            else "lue"
        copie["lus"][ou] += 1
        bougies = [b for b in lecteurs[ou].candles(
            e.pair, 60, d - (CONTEXTE_MIN + 1) * 60, d + APRES * 60)
            if b.complete]
        avant = [b for b in bougies if b.ts_sec < d]
        apres = [b for b in bougies if b.ts_sec >= d][:APRES]
        aveugles.append(svg(numero, e, avant, apres, False))
        reponses.append(svg(numero, e, avant, apres, True))
        # Les bougies d'AVANT l'entrée seulement : celles d'après donnent
        # l'issue, qui part dans son propre fichier.
        for b in avant:
            lignes_b.append({"graphique": numero, "paire": e.pair,
                             "ts_utc": _utc(b.ts_sec, "%Y-%m-%d %H:%M"),
                             "ts": b.ts_sec, "open": b.open, "high": b.high,
                             "low": b.low, "close": b.close})
        lignes_s.append({"graphique": numero, "paire": e.pair,
                         "decision": d, "decision_utc": _utc(d),
                         "sens": "call" if e.contexte.get("sens_call")
                         else "put", "zone": e.contexte.get("niveau"),
                         "bougies_avant": len(avant)})
        lignes_i.append({"graphique": numero,
                         "issue": em.GAIN if e.gagne else em.PERTE,
                         **{f"close_{k}": b.close
                            for k, b in enumerate(apres)}})
        respirer()
    gagnes = sum(1 for e in tirage if e.gagne)
    intro_aveugle = html.escape(
        f"{len(tirage)} signaux ZoneH1 jouables tirés au hasard avant le "
        f"03/11, un par zone, gagnants et perdants mêlés. Chaque graphique "
        f"s'arrête à l'entrée : pointillés bleus = la zone ; trait gris = "
        f"l'entrée. Pour chacun, note ce qui confirme ou infirme le trade "
        f"prévu (types, tailles, enchaînement des bougies, arrivée sur la "
        f"zone), AVANT d'ouvrir les réponses.")
    intro_reponses = html.escape(
        "Les mêmes graphiques, avec les 15 minutes qui suivent l'entrée "
        "(fond gris) et le résultat.")
    texte = "\n".join([
        "🖼 <b>Revue des graphiques à l'aveugle</b> (lecture seule)",
        f"Bases : {html.escape(nom_lecture)} et active "
        f"({', '.join(f'{k} : {v}' for k, v in lecture.items())}) ; "
        f"requêtes refusées : {lue.refusees + (active.refusees if active else 0)}"
        f". Rien n'a été écrit en base.",
        f"<b>Lot {lot}</b> : {len(tirage)} graphiques ({gagnes} gagnants, "
        f"{len(tirage) - gagnes} perdants, mélangés), tirés parmi "
        f"{len(tous)} signaux d'avant le 03/11, un par zone"
        + ("." if lot < 2 else ", lot 1 exclu." if lot == 2
           else f", lots 1 à {lot - 1} exclus."),
        "1. Ouvre <b>graphiques_aveugles.html</b> : note pour chaque "
        "graphique ce qui confirme ou infirme le trade, sans regarder le "
        "résultat.",
        "2. Ensuite seulement, <b>graphiques_reponses.html</b>.",
        "Envoie-moi d'abord <b>graphiques_bougies.csv</b> et "
        "<b>graphiques_signaux.csv</b> (sans résultat). Garde "
        "<b>graphiques_issues.csv</b> : je te le demanderai une fois ma revue "
        "figée.",
        f"Durée : {time.monotonic() - t0:.0f} s."])
    return texte, [
        (page("Graphiques à l'aveugle", aveugles, intro_aveugle),
         "graphiques_aveugles.html",
         "Les graphiques arrêtés à l'entrée, sans résultat"),
        (page("Graphiques — réponses", reponses, intro_reponses),
         "graphiques_reponses.html",
         "Les mêmes, avec les 15 minutes d'après et le résultat"),
        (etude3._csv(lignes_b, ["graphique", "paire", "ts_utc", "ts", "open",
                                "high", "low", "close"]),
         "graphiques_bougies.csv",
         "Les bougies de chaque graphique, jusqu'à l'entrée"),
        (etude3._csv(lignes_s, ["graphique", "paire", "decision",
                                "decision_utc", "sens", "zone",
                                "bougies_avant"]),
         "graphiques_signaux.csv",
         "Chaque graphique : paire, sens, zone (sans le résultat)"),
        (etude3._csv(lignes_i, ["graphique", "issue",
                                *(f"close_{k}" for k in range(APRES))]),
         "graphiques_issues.csv",
         "LES RÉSULTATS : à n'envoyer qu'une fois la revue figée"),
    ]
