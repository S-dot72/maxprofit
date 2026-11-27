"""
/verifier : rejouer, sur nos ticks réels, une liste de trades OTC — ceux d'un
trader vu en vidéo, d'un canal de signaux, ou les siens. LECTURE SEULE.

Chaque ligne donne une paire, un sens, une date et une heure d'ouverture, une
échéance ; en option, le prix d'entrée lu à l'écran et le résultat annoncé
(gagné, perdu). L'entrée retenue est le dernier tick connu à l'heure
d'ouverture, la sortie le dernier tick connu à l'échéance ; les deux doivent
dater de moins de 10 s. Une égalité est comptée à part, et comme une perte
dans le taux (prudence) ; le taux hors égalités est donné aussi.

L'heure lue sur un écran dépend du fuseau de cet écran. Il se donne par une
ligne « fuseau UTC+3 » (ou un nom IANA comme Europe/Paris) ; sinon il est
retrouvé à partir des prix d'entrée donnés (au moins 3) : c'est le décalage,
par demi-heure de −12 h à +14 h, où nos ticks collent le mieux à ces prix.
Sans l'un ni l'autre, c'est le fuseau d'affichage du projet, et le rapport le
dit en premier.

Rien n'est écrit en base, aucun ordre ; la course et la collecte ne sont pas
touchées.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from statistics import median
from zoneinfo import ZoneInfo

from maxprofit.core import timebase
from maxprofit.live import engulfing as en
from maxprofit.live.selection3 import PAYOUT_AVANT

CALL, PUT = 1, -1
FRAICHEUR_MS = 10_000
DUREE_MIN_SEC = 5
DUREE_MAX_SEC = 4 * 3600
MAX_TRADES = 500
MAX_DETAIL = 25
POINT_MORT = en.POINT_MORT
PAYOUT = 0.92
#: Décalages essayés pour retrouver le fuseau : −12 h à +14 h, par demi-heure.
DECALAGES = tuple(h / 2 for h in range(-24, 29))
#: Écart médian maximal, en pips, pour accepter un fuseau retrouvé.
ECART_MAX_PIPS = 3.0

SENS = {
    "call": CALL, "haut": CALL, "hausse": CALL, "achat": CALL, "buy": CALL,
    "up": CALL, "acheter": CALL, "⬆": CALL, "↑": CALL,
    "put": PUT, "bas": PUT, "baisse": PUT, "vente": PUT, "sell": PUT,
    "down": PUT, "vendre": PUT, "⬇": PUT, "↓": PUT,
}
ANNONCES = {
    "gagné": 1, "gagne": 1, "gagnant": 1, "win": 1, "won": 1, "w": 1,
    "✅": 1, "itm": 1,
    "perdu": -1, "perdant": -1, "loss": -1, "lost": -1, "l": -1, "❌": -1,
    "otm": -1,
    "nul": 0, "égalité": 0, "egalite": 0, "draw": 0, "remboursé": 0,
}
GAGNE, PERDU, EGALITE = "gagné", "perdu", "égalité"

_DATE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DATE_FR = re.compile(r"^(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?$")
_HEURE = re.compile(r"^(\d{1,2})[:h](\d{2})(?::(\d{2}))?$")
_DUREE = re.compile(r"^(\d+)(s|sec|secs|seconde|secondes|m|mn|min|mins|"
                    r"minute|minutes)?$")
_DUREE_M = re.compile(r"^m(\d+)$")
_PRIX = re.compile(r"^\d+[.,]\d+$")
_FUSEAU = re.compile(r"^(?:utc|gmt)?\s*([+\-−])\s*(\d{1,2})(?::?(\d{2}))?$")


@dataclass
class Trade:
    ligne: int
    texte: str
    paire: str
    sens: int
    local: datetime            # naïf : l'heure lue sur l'écran
    secondes: bool             # l'heure avait-elle ses secondes ?
    duree_sec: int
    prix: float | None = None
    annonce: int | None = None
    utc_ms: int | None = None
    entree: tuple[int, float] | None = None
    sortie: tuple[int, float] | None = None
    issue: int | None = None   # 1 gagné, −1 perdu, 0 égalité
    statut: str = ""
    payout: float | None = None

    @property
    def pip(self) -> float:
        return 0.01 if "JPY" in self.paire else 0.0001


# --------------------------------------------------------------------------- #
# La lecture du message
# --------------------------------------------------------------------------- #

def lire_fuseau(texte: str) -> tuple[tzinfo, str]:
    """« UTC+3 », « +5:30 », « -5 », « GMT-4 » ou un nom IANA. Lève
    ValueError si illisible."""
    brut = texte.strip()
    m = _FUSEAU.match(brut.lower().replace(" ", ""))
    if m:
        signe = -1 if m.group(1) in "-−" else 1
        heures, minutes = int(m.group(2)), int(m.group(3) or 0)
        if heures > 14 or minutes not in (0, 30, 45):
            raise ValueError(f"décalage impossible : {brut}")
        delta = signe * timedelta(hours=heures, minutes=minutes)
        return timezone(delta), "UTC" + _decalage_texte(delta)
    try:
        return ZoneInfo(brut), brut
    except Exception:                                    # noqa: BLE001
        raise ValueError(f"fuseau illisible : {brut}") from None


def _decalage_texte(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    signe = "+" if minutes >= 0 else "−"
    h, m = divmod(abs(minutes), 60)
    return f"{signe}{h}" + (f":{m:02d}" if m else "")


def _jetons(ligne: str) -> list[str]:
    if any(s in ligne for s in ";|\t"):
        morceaux = re.split(r"[;|\t]", ligne)
    else:
        morceaux = ligne.split()
    sortie = []
    for m in morceaux:
        m = m.strip()
        if not m:
            continue
        if "T" in m and _DATE_ISO.match(m.split("T")[0]):
            sortie.extend(m.split("T", 1))
        elif "," in m and not _PRIX.match(m):
            sortie.extend(x for x in m.split(",") if x)
        else:
            sortie.extend(m.split())
    return sortie


def _date(jeton: str, maintenant: datetime) -> datetime | None:
    m = _DATE_ISO.match(jeton)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _DATE_FR.match(jeton)
    if m:
        jour, mois = int(m.group(1)), int(m.group(2))
        if m.group(3):
            annee = int(m.group(3))
            annee += 2000 if annee < 100 else 0
            return datetime(annee, mois, jour)
        d = datetime(maintenant.year, mois, jour)
        return d if d <= maintenant + timedelta(days=1) else \
            datetime(maintenant.year - 1, mois, jour)
    return None


def _paire(jeton: str) -> str | None:
    lettres = re.sub(r"[^A-Za-z]", "", jeton).upper()
    if lettres.endswith("OTC"):
        lettres = lettres[:-3]
    if len(lettres) == 6 and lettres.isalpha():
        return f"{lettres}_otc"
    return None


def lire_trade(numero: int, ligne: str, maintenant: datetime,
               date_defaut: datetime | None) -> tuple[Trade | None, str]:
    """(trade, "") ou (None, ce qui manque)."""
    paire = sens = jour = None
    heures: list[tuple[int, int, int | None]] = []
    duree = prix = annonce = None
    for jeton in _jetons(ligne):
        bas = jeton.lower().strip(".:")
        if bas in SENS and sens is None:
            sens = SENS[bas]
            continue
        if bas in ANNONCES and annonce is None:
            annonce = ANNONCES[bas]
            continue
        if bas == "otc":
            continue
        d = _date(jeton, maintenant)
        if d is not None and jour is None:
            jour = d
            continue
        m = _HEURE.match(jeton)
        if m:
            heures.append((int(m.group(1)), int(m.group(2)),
                           int(m.group(3)) if m.group(3) else None))
            continue
        m = _DUREE.match(bas) or _DUREE_M.match(bas)
        if m and duree is None:
            n = int(m.group(1))
            unite = m.group(2) if m.re is _DUREE else "m"
            duree = n if unite and unite.startswith("s") else 60 * n
            continue
        if _PRIX.match(jeton) and prix is None:
            prix = float(jeton.replace(",", "."))
            continue
        p = _paire(jeton)
        if p is not None and paire is None:
            paire = p
    jour = jour or date_defaut
    if duree is None and len(heures) >= 2 and None not in (
            heures[0][2], heures[1][2]):
        a = heures[0][0] * 3600 + heures[0][1] * 60 + heures[0][2]
        b = heures[1][0] * 3600 + heures[1][1] * 60 + heures[1][2]
        duree = (b - a) % 86400            # ouverture → clôture
    manque = [quoi for quoi, v in (("la paire", paire), ("le sens", sens),
                                   ("la date", jour),
                                   ("l'heure", heures or None),
                                   ("l'échéance", duree)) if v is None]
    if manque:
        return None, "manque " + ", ".join(manque)
    if not DUREE_MIN_SEC <= duree <= DUREE_MAX_SEC:
        return None, f"échéance hors limites ({duree} s)"
    h, mi, s = heures[0]
    if h > 23 or mi > 59 or (s or 0) > 59:
        return None, "heure impossible"
    local = jour.replace(hour=h, minute=mi, second=s or 0)
    return Trade(numero, ligne.strip(), paire, sens, local, s is not None,
                 duree, prix, annonce), ""


def lire(texte: str, maintenant: datetime):
    """(trades, erreurs [(ligne, texte, raison)], fuseau donné ou None)."""
    trades: list[Trade] = []
    erreurs: list[tuple[int, str, str]] = []
    fuseau = None
    date_defaut = None
    for numero, ligne in enumerate(texte.splitlines(), start=1):
        brut = ligne.strip()
        if numero == 1 and brut.lower().startswith("/verifier"):
            brut = brut.split(None, 1)[1] if " " in brut else ""
        if not brut:
            continue
        bas = brut.lower()
        if bas.startswith("fuseau") or _FUSEAU.match(bas.replace(" ", "")):
            valeur = brut[6:] if bas.startswith("fuseau") else brut
            try:
                fuseau = lire_fuseau(valeur.strip(" :="))
            except ValueError as erreur:
                erreurs.append((numero, brut, str(erreur)))
            continue
        if bas.startswith("date"):
            d = _date(brut[4:].strip(" :="), maintenant)
            if d is None:
                erreurs.append((numero, brut, "date illisible"))
            else:
                date_defaut = d
            continue
        if len(trades) >= MAX_TRADES:
            erreurs.append((numero, brut, f"au-delà de {MAX_TRADES} trades"))
            continue
        trade, raison = lire_trade(numero, brut, maintenant, date_defaut)
        if trade is None:
            erreurs.append((numero, brut, raison))
        else:
            trades.append(trade)
    return trades, erreurs, fuseau


# --------------------------------------------------------------------------- #
# Les ticks
# --------------------------------------------------------------------------- #

class Marche:
    """Les ticks et les payouts lus à la demande sur une base en lecture
    seule. Une seule paire à la fois, et les jours passés s'oublient : les
    trades se lisent paire par paire, dans l'ordre du temps, pour que la
    mémoire ne dépende pas du nombre de jours couverts."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self._ticks: dict[str, en.Ticks] = {}
        self.jours_lus: list[tuple[str, int]] = []

    def ticks(self, paire: str) -> en.Ticks:
        if paire not in self._ticks:
            self._ticks.clear()
            def charger(jour: int, paire=paire):
                self.jours_lus.append((paire, jour))
                return {int(r[0]): (int(r[1]), int(r[2]), bytes(r[3]))
                        for r in self.conn.execute(
                            en.SQL_CHEMINS, (paire, jour * 86400,
                                             (jour + 1) * 86400)).fetchall()}
            self._ticks[paire] = en.Ticks(charger)
        return self._ticks[paire]

    def oublier_avant(self, paire: str, t_sec: int) -> None:
        self.ticks(paire).oublier_avant(t_sec)

    def prix_a(self, paire: str, t_ms: int) -> tuple[int, float] | None:
        """Le dernier tick connu à t, s'il date de moins de 10 s."""
        connu = self.ticks(paire).prix_a(t_ms)
        if connu is None or t_ms - connu[0] > FRAICHEUR_MS:
            return None
        return connu

    def payout(self, paire: str, t_sec: float) -> float | None:
        ligne = self.conn.execute(PAYOUT_AVANT, (paire, int(t_sec) + 1)
                                  ).fetchone()
        return float(ligne[1]) if ligne and ligne[1] is not None else None


def _utc_ms(local: datetime, fuseau: tzinfo) -> int:
    return int(local.replace(tzinfo=fuseau).astimezone(timezone.utc)
               .timestamp() * 1000)


def retrouver_fuseau(trades: list[Trade], marche: Marche
                     ) -> tuple[float, float, int] | None:
    """(décalage en heures, écart médian en pips, trades comparés) du
    décalage où nos ticks collent le mieux aux prix d'entrée donnés ; None
    s'il y a moins de 3 prix comparables."""
    avec_prix = sorted((t for t in trades if t.prix is not None),
                       key=lambda t: (t.paire, t.local))
    if len(avec_prix) < 3:
        return None
    fuseaux = [(d, timezone(timedelta(hours=d))) for d in DECALAGES]
    ecarts: dict[float, list[float]] = {d: [] for d in DECALAGES}
    for t in avec_prix:
        plus_tot = _utc_ms(t.local, fuseaux[-1][1]) // 1000
        marche.oublier_avant(t.paire, plus_tot - 86400)
        for decalage, fuseau in fuseaux:
            connu = marche.prix_a(t.paire, _utc_ms(t.local, fuseau))
            if connu is not None:
                ecarts[decalage].append(abs(t.prix - connu[1]) / t.pip)
    meilleur = None
    for decalage in DECALAGES:
        if len(ecarts[decalage]) >= 3:
            score = median(ecarts[decalage])
            if meilleur is None or score < meilleur[1]:
                meilleur = (decalage, score, len(ecarts[decalage]))
    return meilleur


def evaluer(t: Trade, marche: Marche, fuseau: tzinfo,
            maintenant_ms: int) -> None:
    t.utc_ms = _utc_ms(t.local, fuseau)
    fin_ms = t.utc_ms + 1000 * t.duree_sec
    if fin_ms > maintenant_ms - 60_000:
        t.statut = "échéance trop récente ou à venir"
        return
    t.entree = marche.prix_a(t.paire, t.utc_ms)
    if t.entree is None:
        t.statut = "pas de tick frais à l'ouverture"
        return
    t.sortie = marche.prix_a(t.paire, fin_ms)
    if t.sortie is None:
        t.statut = "pas de tick frais à l'échéance"
        return
    ecart = (t.sortie[1] - t.entree[1]) * t.sens
    t.issue = 1 if ecart > 0 else (-1 if ecart < 0 else 0)
    t.payout = marche.payout(t.paire, t.utc_ms / 1000)
    t.statut = "jugé"


# --------------------------------------------------------------------------- #
# Le bilan
# --------------------------------------------------------------------------- #

def p_unilaterale(gains: int, n: int) -> float | None:
    """P(au moins `gains` succès sur n, à 50 %) : la chance d'un tel score
    par pur hasard."""
    if n <= 0:
        return None
    return sum(math.comb(n, k) for k in range(gains, n + 1)) / 2 ** n


def bilan(trades: list[Trade]) -> dict:
    juges = [t for t in trades if t.issue is not None]
    g = sum(t.issue == 1 for t in juges)
    l_ = sum(t.issue == -1 for t in juges)
    e = sum(t.issue == 0 for t in juges)
    n = len(juges)
    bas, haut = en.wilson(g, n)
    annonces = [t for t in juges if t.annonce is not None]
    conformes = sum(t.annonce == t.issue for t in annonces)
    ecarts = [abs(t.prix - t.entree[1]) / t.pip for t in juges
              if t.prix is not None and t.entree is not None]
    return {
        "n": n, "gains": g, "pertes": l_, "egalites": e,
        "taux": g / n if n else None,
        "taux_decisifs": g / (g + l_) if g + l_ else None,
        "ic_bas": bas, "ic_haut": haut,
        "p": p_unilaterale(g, g + l_),
        "resultat_mises": g * PAYOUT - l_,
        "annonces": len(annonces), "conformes": conformes,
        "ecart_median_pips": median(ecarts) if ecarts else None,
        "a_la_minute": sum(not t.secondes for t in juges),
    }


def verdict(b: dict) -> str:
    n, taux = b["n"], b["taux"]
    if n == 0:
        return "aucun trade n'a pu être jugé."
    if n < 30:
        return (f"trop peu de trades pour conclure : sur {n}, le hasard seul "
                "fait facilement 60 % ou 40 %. Il en faut au moins 100, idéalement "
                "300, pour départager 52 % de 50 %.")
    if b["ic_bas"] is not None and b["ic_bas"] > POINT_MORT:
        return ("au-dessus du point mort de 52,08 %, nettement : même le bas "
                "de l'intervalle le dépasse. À confirmer sur ses trades "
                "FUTURS, notés avant l'issue.")
    if taux > POINT_MORT:
        return (f"au-dessus du point mort de 52,08 %, mais l'écart reste dans "
                f"ce que le hasard produit (p = {_p(b['p'])}). Il faut plus de "
                "trades, de préférence notés avant l'issue.")
    return ("pas au-dessus du point mort de 52,08 % : à ce taux, la mise "
            "fixe perd de l'argent.")


def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}".replace(".", ",")


def _p(p) -> str:
    if p is None:
        return "—"
    return f"{p:.3f}".replace(".", ",") if p >= 0.001 else "< 0,001"


def _prix(x: float, pip: float) -> str:
    return f"{x:.{3 if pip >= 0.01 else 5}f}".replace(".", ",")


# --------------------------------------------------------------------------- #
# La commande
# --------------------------------------------------------------------------- #

AIDE = (
    "🔎 <b>/verifier</b> : collez sous la commande des trades OTC, un par "
    "ligne, et je les rejoue sur nos ticks réels. Exemple :\n"
    "<code>/verifier\n"
    "fuseau UTC+3\n"
    "EURUSD CALL 27/11 14:05:30 1min 1.08452 gagné\n"
    "GBPJPY PUT 27/11 14:12:05 5min</code>\n"
    "• Obligatoire : la paire, le sens (CALL/PUT, HAUT/BAS, ACHAT/VENTE), la "
    "date et l'heure d'ouverture (avec les secondes si possible), "
    "l'échéance (30s, 1min, 5min…). Deux heures (ouverture et clôture) "
    "suffisent aussi pour l'échéance.\n"
    "• En option : le prix d'entrée affiché et le résultat annoncé (gagné, "
    "perdu).\n"
    "• « fuseau » : celui de l'écran où l'heure a été lue (UTC+3, -5, "
    "Europe/Paris…). Sans lui, je le retrouve grâce aux prix d'entrée (il en "
    "faut au moins 3) ; sinon je prends l'heure d'Haïti.\n"
    "• Plus il y a de trades, plus le verdict est solide : 100 au moins, 300 "
    "idéalement.")


def verifier(conn, argument: str, *, maintenant: float | None = None,
             fuseau_defaut: tzinfo | None = None
             ) -> tuple[str, list[tuple[bytes, str, str]]]:
    """(rapport, fichiers) pour les trades du message. `conn` est en lecture
    seule."""
    import time
    maintenant = time.time() if maintenant is None else maintenant
    fuseau_defaut = fuseau_defaut or timebase.fuseau_affichage()
    ici = datetime.fromtimestamp(maintenant, timezone.utc).astimezone(
        fuseau_defaut).replace(tzinfo=None)
    trades, erreurs, donne = lire(argument, ici)
    if not trades and not erreurs:
        return AIDE, []
    marche = Marche(conn)
    if donne is not None:
        fuseau, origine = donne[0], f"{donne[1]} (donné)"
    else:
        trouve = retrouver_fuseau(trades, marche)
        if trouve is not None and trouve[1] <= ECART_MAX_PIPS:
            delta = timedelta(hours=trouve[0])
            fuseau = timezone(delta)
            ecart = f"{trouve[1]:.1f}".replace(".", ",")
            origine = (f"UTC{_decalage_texte(delta)}, retrouvé grâce aux prix "
                       f"d'entrée (écart médian {ecart} pip sur {trouve[2]} "
                       "trades)")
        else:
            fuseau = fuseau_defaut
            nom = getattr(fuseau_defaut, "key", str(fuseau_defaut))
            origine = (f"⚠ {nom} par défaut : précisez « fuseau UTC+X » si "
                       "l'heure vient d'un autre écran")
            if trouve is not None:
                ecart = f"{trouve[1]:.1f}".replace(".", ",")
                origine += (" ; aucun décalage ne colle aux prix donnés "
                            f"(meilleur écart médian {ecart} pip)")
    for t in sorted(trades, key=lambda t: (t.paire, _utc_ms(t.local,
                                                             fuseau))):
        marche.oublier_avant(t.paire, _utc_ms(t.local, fuseau) // 1000
                             - 86400)
        evaluer(t, marche, fuseau, int(maintenant * 1000))
    b = bilan(trades)
    return texte(trades, erreurs, b, origine), fichiers(trades, erreurs)


def texte(trades: list[Trade], erreurs, b: dict, origine: str) -> str:
    morceaux = [
        f"🔎 <b>Vérification de {len(trades)} trade"
        f"{'s' if len(trades) > 1 else ''} OTC</b> sur nos ticks réels "
        "(lecture seule, aucun ordre)",
        f"Fuseau des heures : {html.escape(origine, quote=False)}.",
    ]
    if b["n"]:
        morceaux.append(
            f"<b>Jugés : {b['n']}</b> — gagnés {b['gains']}, perdus "
            f"{b['pertes']}, égalités {b['egalites']} → <b>{_pct(b['taux'])} "
            f"%</b> de réussite [{_pct(b['ic_bas'])} ; {_pct(b['ic_haut'])}] "
            f"(égalités comptées perdues ; {_pct(b['taux_decisifs'])} % hors "
            "égalités).")
        res = f"{b['resultat_mises']:+.1f}".replace(".", ",")
        morceaux.append(
            f"À mise fixe, payout 92 % : {res} mises. Le hasard seul ferait "
            f"un tel score avec une probabilité de {_p(b['p'])}.")
        morceaux.append(f"<b>Verdict</b> : {verdict(b)}")
        if b["annonces"]:
            morceaux.append(
                f"Résultat annoncé : {b['conformes']} conforme"
                f"{'s' if b['conformes'] > 1 else ''} sur {b['annonces']} "
                "aux prix réels."
                + (" ⚠ Des écarts nombreux trahissent une heure fausse, un "
                   "mauvais fuseau ou un historique arrangé."
                   if b["conformes"] < 0.9 * b["annonces"] else ""))
        if b["ecart_median_pips"] is not None:
            ecart = f"{b['ecart_median_pips']:.1f}".replace(".", ",")
            morceaux.append(f"Prix d'entrée annoncés contre nos ticks : "
                            f"écart médian {ecart} pip.")
        if b["a_la_minute"]:
            morceaux.append(
                f"⚠ {b['a_la_minute']} heure(s) sans les secondes : prises "
                "au début de la minute, le résultat est alors approximatif.")
    morceaux.append("<b>Détail</b> :" if trades else "")
    for t in trades[:MAX_DETAIL]:
        quand = t.local.strftime("%d/%m %H:%M:%S" if t.secondes
                                 else "%d/%m %H:%M")
        sens = "CALL" if t.sens == CALL else "PUT"
        duree = (f"{t.duree_sec // 60} min" if t.duree_sec % 60 == 0
                 else f"{t.duree_sec} s")
        if t.issue is None:
            fin = f"non jugé : {t.statut}"
        else:
            marque = {1: "✅", -1: "❌", 0: "＝"}[t.issue]
            fin = (f"{_prix(t.entree[1], t.pip)} → "
                   f"{_prix(t.sortie[1], t.pip)} {marque}")
            if t.annonce is not None and t.annonce != t.issue:
                fin += " (annoncé autrement)"
        paire = html.escape(t.paire, quote=False)
        morceaux.append(f"• L{t.ligne} {quand} {paire} {sens} {duree} : "
                        f"{fin}")
    if len(trades) > MAX_DETAIL:
        morceaux.append(f"… et {len(trades) - MAX_DETAIL} autres dans "
                        "verification.csv.")
    if erreurs:
        morceaux.append(f"<b>Lignes non lues</b> ({len(erreurs)}) :")
        for numero, brut, raison in erreurs[:10]:
            morceaux.append(
                f"• L{numero} « {html.escape(brut[:60], quote=False)} » : "
                f"{html.escape(raison, quote=False)}")
    return "\n".join(m for m in morceaux if m)


COLONNES = ("ligne", "texte", "paire", "sens", "heure_ecran", "utc",
            "duree_sec", "prix_annonce", "prix_entree", "ecart_pips",
            "prix_sortie", "issue", "resultat_annonce", "conforme",
            "payout_flux", "statut")


def fichiers(trades: list[Trade], erreurs) -> list[tuple[bytes, str, str]]:
    lignes = []
    noms = {1: GAGNE, -1: PERDU, 0: EGALITE}
    for t in trades:
        lignes.append({
            "ligne": t.ligne, "texte": t.texte, "paire": t.paire,
            "sens": "CALL" if t.sens == CALL else "PUT",
            "heure_ecran": t.local.strftime("%Y-%m-%d %H:%M:%S"),
            "utc": "" if t.utc_ms is None else en._utc(
                t.utc_ms // 1000, "%Y-%m-%d %H:%M:%S"),
            "duree_sec": t.duree_sec,
            "prix_annonce": "" if t.prix is None else t.prix,
            "prix_entree": "" if t.entree is None else t.entree[1],
            "ecart_pips": "" if t.prix is None or t.entree is None else
            f"{abs(t.prix - t.entree[1]) / t.pip:.2f}",
            "prix_sortie": "" if t.sortie is None else t.sortie[1],
            "issue": noms.get(t.issue, ""),
            "resultat_annonce": noms.get(t.annonce, ""),
            "conforme": "" if t.annonce is None or t.issue is None else
            int(t.annonce == t.issue),
            "payout_flux": "" if t.payout is None else f"{t.payout:.0f}",
            "statut": t.statut})
    for numero, brut, raison in erreurs:
        lignes.append({"ligne": numero, "texte": brut,
                       "statut": f"non lue : {raison}"})
    if not lignes:
        return []
    return [(en._csv(lignes, COLONNES), "verification.csv",
             "Chaque trade rejoué sur nos ticks : prix d'entrée et de sortie, "
             "issue, résultat annoncé")]

