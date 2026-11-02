"""
`/rapprochement` de bout en bout, sur une base SQLite et un rejeu en mémoire
fabriqués :
- chaque classe de correspondance (exacte, approximative, ambiguë, absente)
  et chaque état d'ordre (réglé, rejeté, non envoyé, réservé sans suite) ;
- un ordre dans le créneau mais d'empreinte discordante n'est JAMAIS associé
  (pas de correspondance à la seule heure) ;
- deux signaux de la même zone à la même bougie d'entrée sont départagés
  par le mode de confirmation, pas par l'heure ;
- un prix de règlement absent est une limite comptée, jamais reconstruite ;
- les décisions s'arrêtent à l'ouverture de la confirmation du gel 2 ;
- la base reste inchangée.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.apprentissage.contexte import CARACTERISTIQUES
from maxprofit.apprentissage.historique import CONTEXTE_A_L_ENTREE
from maxprofit.apprentissage.lecons import Exemple
from maxprofit.core.types import Tick
from maxprofit.execution.journal import RESERVE, Execution, JournalExecution
from maxprofit.live import rapprochement as rp
from maxprofit.store.chemin_ticks import encoder
from maxprofit.store.db import open_read_only, open_read_write
from maxprofit.store.market import MarketWriter

H = 3600
HEURE_MS = 3_600_000
T0 = em.FIN_EXPLORATION - 2 * 86400 + 10 * H      # 19/10 10:00, exploration
MAINTENANT = rp.OUVERTURE_CONFIRMATION + 2 * 86400


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def _contexte(g: float, fin: int, touches: float = 2.0,
              offertes: float = 1.0) -> dict:
    """Un contexte au format de `apprentissage.contexte`, arrondi à 4
    décimales, propre à la graine `g` et à la bougie d'entrée finissant à
    `fin`."""
    ctx = {}
    for i, c in enumerate(CARACTERISTIQUES):
        if c in rp.CLES_ENTIERES:
            ctx[c] = float((int(g) + i) % 7)
        else:
            ctx[c] = round(0.5 + g * 1.37 + i * 0.2113, 4)
    ctx["heure_utc"] = float((fin // 3600) % 24)
    ctx["touches"], ctx["entrees_deja_offertes"] = touches, offertes
    return ctx


def _exemple(paire: str, d: int, call: bool, gagne: bool, *, g_signal: float,
             g_entree: float | None, k: int = 1) -> tuple[Exemple, dict]:
    """Un signal du rejeu, et le contexte que la course mesure à l'entrée
    confirmée (son empreinte)."""
    ctx = _contexte(g_signal, d)
    ctx.update(sens_call=float(call), prix_signal=1.1)
    entree = None
    if g_entree is not None and k >= 1:
        entree = _contexte(g_entree, d + 60 * k)
        ctx["attente_confirmation"] = float(k)
        ctx["gagne_confirme"] = float(gagne)
        for c in CONTEXTE_A_L_ENTREE:
            ctx[f"e_{c}"] = entree[c]
    else:
        ctx["attente_confirmation"] = 0.0
    return Exemple(ts_sec=d, pair=paire, contexte=ctx, gagne=gagne), entree


def _ordre(journal, paire: str, sens: str, placement: float, *,
           contexte: dict | None, resultat: str | None = "win",
           accepte: bool = True, refus: str | None = None,
           prix=(1.1001, 1.1005), payout: float = 92, flux: float = 92,
           denouement: bool = True, brut_extra: dict | None = None,
           decalage_ms: int | None = 2 * HEURE_MS + 1500) -> None:
    ms = int(placement * 1000)
    brut: dict = dict(brut_extra or {})
    if contexte is not None:
        brut["contexte"] = {**contexte, "pas": 1}
    if accepte and resultat is not None and denouement:
        brut["denouement"] = {"closePrice": prix[1]}
    ouverture = ms + 2000 + 2 * HEURE_MS if accepte else None
    profit = None
    if resultat == "win":
        profit = payout / 100
    elif resultat == "loose":
        profit = -1.0
    elif resultat == "draw":
        profit = 0.0
    journal.ecrire(Execution(
        pair=paire, sens=sens, mise=1.0, signal_ts_ms=ms, prix_attendu=1.1,
        payout_flux_pct=flux, expiration_sec=900, clic_ts_ms=ms,
        accepte_ts_ms=ms + 500 if accepte else None, accepte=accepte,
        refus=refus, order_id=f"b{ms}" if accepte else None,
        prix_entree=prix[0] if accepte else None,
        prix_sortie=prix[1] if accepte and denouement else None,
        payout_broker_pct=payout if accepte else None,
        ouverture_ts_ms=ouverture,
        expiration_ts_ms=None if ouverture is None or resultat is None
        else ouverture + 900_000,
        decalage_broker_ms=decalage_ms if accepte else None,
        resultat=resultat if accepte else None, profit=profit, brut=brut))


def _reservation(conn, paire: str, sens: str, placement: float,
                 refus: str) -> None:
    ms = int(placement * 1000)
    conn.execute(
        "INSERT INTO executions (campagne, pair, sens, mise, signal_ts_ms, "
        "clic_ts_ms, prix_attendu, payout_flux_pct, expiration_sec, accepte, "
        "refus, order_id, brut) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("c1", paire, sens, 1.0, ms, ms, 0.0, 0.0, 900, 0, refus,
         f"reserve-{ms}", "{}"))


#: Les décisions du scénario : une heure d'écart entre deux signaux, pour
#: qu'aucun ordre n'occupe la course au moment d'un autre (sauf F → G).
D = {nom: T0 + i * H for i, nom in enumerate("ABCDEFHIJKM")}
D["G"] = D["F"] + 60


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    chemin = tmp_path_factory.mktemp("rapprochement") / "base.db"
    conn = open_read_write(chemin)
    ecrivain = MarketWriter(conn)
    ecrivain.insert_tick_paths([encoder([Tick("EURUSD_otc", (T0 - 3 * 86400)
                                              * 1000, 1.1)])])
    for paire, flux in (("EURUSD_otc", 92), ("USDJPY_otc", 88),
                        ("GBPUSD_otc", 80), ("AUDUSD_otc", 90),
                        ("EURJPY_otc", 92), ("CADJPY_otc", 92),
                        ("NZDUSD_otc", 92), ("USDCAD_otc", 92)):
        conn.execute("INSERT INTO payouts (ts_sec, pair, payout_pct, is_open)"
                     " VALUES (?,?,?,1)", (T0 - 600, paire, flux))
    conn.commit()
    journal = JournalExecution(conn, "c1")
    exemples, sans_issue = [], []

    def signal(nom, paire, call, gagne, g_s, g_e, k=1):
        e, entree = _exemple(paire, D[nom], call, gagne, g_signal=g_s,
                             g_entree=g_e, k=k)
        exemples.append(e)
        return entree

    # A : exacte, réglée gagnante.
    ea = signal("A", "EURUSD_otc", True, True, 1, 11)
    _ordre(journal, "EURUSD_otc", "call", D["A"] + 63, contexte=ea)
    # B : approximative (deux grandeurs à +3 %), perdante ; le proxy gagnait.
    eb = signal("B", "EURUSD_otc", False, True, 2, 12)
    proche = dict(eb)
    proche["elan_15m"] = round(proche["elan_15m"] * 1.03, 4)
    proche["volatilite_relative"] = round(
        proche["volatilite_relative"] * 1.03, 4)
    _ordre(journal, "EURUSD_otc", "put", D["B"] + 70, contexte=proche,
           resultat="loose", prix=(1.2, 1.2005))
    # C : un REFUS dans le créneau, sans contexte : ambiguë.
    signal("C", "USDJPY_otc", True, False, 3, 13)
    _ordre(journal, "USDJPY_otc", "call", D["C"] + 62, contexte=None,
           accepte=False, refus="error: not enough money", resultat=None)
    # D : aucun ordre, payout du flux sous le plafond.
    signal("D", "GBPUSD_otc", True, False, 4, 14)
    # E : aucun ordre, pas de confirmation M1 au rejeu.
    signal("E", "GBPUSD_otc", True, True, 5, None, k=0)
    # F et G : même zone, la bougie d'entrée de F est la bougie du signal G.
    ef = signal("F", "AUDUSD_otc", True, True, 6, 16)
    eg, _ = _exemple("AUDUSD_otc", D["G"], True, False, g_signal=0,
                     g_entree=17)
    eg.contexte.update({c: ef[c] for c in CARACTERISTIQUES})
    exemples.append(eg)
    _ordre(journal, "AUDUSD_otc", "call", D["F"] + 64, contexte=ef)
    # H : un ordre dans le créneau, MAIS d'une autre empreinte.
    signal("H", "EURJPY_otc", True, True, 7, 18)
    _ordre(journal, "EURJPY_otc", "call", D["H"] + 64,
           contexte=_contexte(99, D["H"] + 60, touches=5.0),
           resultat="draw", prix=(150.0, 150.0))
    # I : deux ordres de même empreinte pour un signal : ambiguë.
    ei = signal("I", "CADJPY_otc", False, False, 8, 19)
    _ordre(journal, "CADJPY_otc", "put", D["I"] + 61, contexte=ei,
           prix=(110.0, 109.9))
    _ordre(journal, "CADJPY_otc", "put", D["I"] + 75, contexte=ei,
           resultat="loose", prix=(110.0, 110.1))
    # J : exacte, gagnante, mais sans prix ni message de règlement.
    ej = signal("J", "NZDUSD_otc", True, False, 9, 20)
    _ordre(journal, "NZDUSD_otc", "call", D["J"] + 65, contexte=ej,
           denouement=False)
    # K : une réservation « non envoyé » dans le créneau : ambiguë.
    signal("K", "USDCAD_otc", True, True, 10, 21)
    _reservation(conn, "USDCAD_otc", "call", D["K"] + 63,
                 "non envoyé : BotError: plafond")
    # M : une égalité au rejeu, sans ordre.
    sans_issue.append({"ts_sec": D["M"], "pair": "EURGBP_otc", "call": False,
                       "contexte": _contexte(30, D["M"]), "issue": "egalite"})
    # N : après l'ouverture de la confirmation : jamais lu.
    en, _ = _exemple("EURUSD_otc", rp.OUVERTURE_CONFIRMATION + H, True, True,
                     g_signal=40, g_entree=41)
    exemples.append(en)
    # Hors scénario de signaux : un réservé sans suite, un rapatrié, un
    # ordre antérieur au rejeu.
    _reservation(conn, "USDCHF_otc", "put", D["M"] + 1800, RESERVE)
    _ordre(journal, "GBPJPY_otc", "put", D["M"] + 2400, contexte=None,
           resultat="loose", prix=(190.0, 190.1), decalage_ms=None,
           brut_extra={"rapatrie_du_broker": {"decalage_broker_sec": 7200}})
    _ordre(journal, "EURUSD_otc", "call", T0 - 86400, contexte=ea)
    conn.commit()
    conn.close()
    course = SimpleNamespace(
        rejeu_en_memoire=(exemples, None, 1, None),
        sans_issue_en_memoire=sans_issue, niveaux_en_memoire={},
        confirmation_m1="suivante",
        strategie=SimpleNamespace(p=SimpleNamespace(expiry_sec=900)))
    avant = hashlib.sha256(Path(chemin).read_bytes()).hexdigest()
    lue = open_read_only(chemin)
    try:
        rapport, fichiers = rp.rapprochement(lue, course,
                                             maintenant=MAINTENANT,
                                             respirer=lambda: None)
    finally:
        lue.close()
    return chemin, avant, course, rapport, fichiers


def _csv(fichiers, nom) -> list[dict]:
    contenu = next(c for c, n, _ in fichiers if n == nom)
    lignes = contenu.decode("utf-8").splitlines()
    entete = lignes[0].split(";")
    return [dict(zip(entete, ligne.split(";"))) for ligne in lignes[1:]]


def _signal(fichiers, nom) -> dict:
    return next(s for s in _csv(fichiers, "rapprochement_signaux.csv")
                if s["decision"] == str(D[nom]))


def test_la_base_ne_change_pas(scenario):
    chemin, avant, _, rapport, fichiers = scenario
    assert hashlib.sha256(Path(chemin).read_bytes()).hexdigest() == avant
    assert "requêtes refusées : 0" in rapport
    assert "Rien n'a été écrit en base." in rapport
    assert [n for _, n, _ in fichiers] == [
        "rapprochement_signaux.csv", "rapprochement_ordres.csv",
        "rapprochement_mesures.csv"]


def test_les_classes_de_correspondance(scenario):
    *_, fichiers = scenario
    attendu = {"A": rp.EXACTE, "B": rp.APPROXIMATIVE, "C": rp.AMBIGUE,
               "D": rp.ABSENTE, "E": rp.ABSENTE, "F": rp.EXACTE,
               "G": rp.ABSENTE, "H": rp.ABSENTE, "I": rp.AMBIGUE,
               "J": rp.EXACTE, "K": rp.AMBIGUE, "M": rp.ABSENTE}
    assert {nom: _signal(fichiers, nom)["correspondance"]
            for nom in attendu} == attendu


def test_une_empreinte_discordante_n_est_jamais_associee(scenario):
    """H : un ordre de la bonne paire, du bon sens, dans le créneau exact —
    mais ce n'est pas le même signal. Il n'est pas associé."""
    *_, fichiers = scenario
    h = _signal(fichiers, "H")
    assert h["correspondance"] == rp.ABSENTE and h["ordre_id"] == ""
    ordre = next(o for o in _csv(fichiers, "rapprochement_ordres.csv")
                 if o["paire"] == "EURJPY_otc")
    assert ordre["correspondance"] == "aucun signal du rejeu"
    assert ordre["resultat"] == "égalité"
    assert ordre["meilleure_empreinte_vue"].startswith(f"EURJPY_otc@{D['H']}")
    assert ordre["meilleure_empreinte_vue"].endswith("(discordante)")


def test_la_meme_bougie_d_entree_est_departagee_par_le_mode(scenario):
    *_, fichiers = scenario
    f, g = _signal(fichiers, "F"), _signal(fichiers, "G")
    assert f["correspondance"] == rp.EXACTE
    assert f["mode_entree"] == "confirmée (k = 1)"
    assert g["correspondance"] == rp.ABSENTE
    assert g["motif_absence"].startswith("course occupée")


def test_les_champs_du_broker_et_l_horloge(scenario):
    *_, fichiers = scenario
    a = _signal(fichiers, "A")
    assert a["cles_comparees"] == a["cles_egales"] == str(
        len(CONTEXTE_A_L_ENTREE) + 3)
    assert a["dans_le_creneau"] == "oui"
    assert a["etat_ordre"] == rp.REGLE and a["resultat_reel"] == "gain"
    assert a["prix_entree_broker"] == "1.1001"
    assert a["prix_reglement_broker"] == "1.1005"
    # Placé 3 s après la fin de la bougie d'entrée ; ouvert 2 s plus tard,
    # horodatage du broker ramené en UTC par le décalage arrondi à l'heure.
    assert a["retard_placement_s"] == "3.0"
    assert a["retard_ouverture_s"] == "5.0"
    assert a["duree_tenue_s"] == "900.0"
    assert a["payout_broker_pct"] == "92.0"
    assert a["payout_du_profit_pct"] == "92.0"
    assert a["coherence_prix_resultat"] == "oui"
    assert a["issue_proxy"] == em.GAIN
    b = _signal(fichiers, "B")
    assert (b["issue_proxy"], b["resultat_reel"]) == (em.GAIN, "perte")
    assert int(b["cles_egales"]) == int(b["cles_comparees"]) - 2


def test_un_prix_de_reglement_absent_est_une_limite(scenario):
    _, _, _, rapport, fichiers = scenario
    j = _signal(fichiers, "J")
    assert j["correspondance"] == rp.EXACTE and j["resultat_reel"] == "gain"
    assert j["prix_reglement_broker"] == ""
    assert "prix de règlement broker absent" in j["limites_ordre"]
    assert "message de dénouement du broker non conservé" in j["limites_ordre"]
    assert "⚠ <b>Limite</b>" in rapport and "prix de règlement (1)" in rapport
    assert "rien n'est reconstruit à partir des bougies M1" in rapport


def test_les_etats_des_ordres(scenario):
    *_, fichiers = scenario
    ordres = _csv(fichiers, "rapprochement_ordres.csv")
    assert {o["etat"] for o in ordres} == {
        rp.REGLE, rp.REJETE, rp.NON_ENVOYE, rp.RESERVE_SANS_SUITE}
    rejete = next(o for o in ordres if o["etat"] == rp.REJETE)
    assert rejete["correspondance"] == rp.AMBIGUE
    assert rejete["contexte_present"] == "non"
    assert rejete["candidats_signaux"] == f"USDJPY_otc@{D['C']}"
    non_envoye = next(o for o in ordres if o["etat"] == rp.NON_ENVOYE)
    assert non_envoye["correspondance"] == rp.AMBIGUE
    reserve = next(o for o in ordres if o["etat"] == rp.RESERVE_SANS_SUITE)
    assert reserve["correspondance"] == "aucun signal du rejeu"
    rapatrie = next(o for o in ordres if o["origine"] == "rapatrié du broker")
    assert rapatrie["correspondance"] == "aucun signal du rejeu"
    assert rapatrie["ouverture_utc"] != ""
    ancien = next(o for o in ordres if o["placement_utc"].startswith(
        rp._utc(T0 - 86400)[:10]))
    assert ancien["correspondance"] == "hors de la période du rejeu"
    doubles = [o for o in ordres if o["paire"] == "CADJPY_otc"]
    assert {o["correspondance"] for o in doubles} == {rp.AMBIGUE}


def test_les_motifs_d_absence(scenario):
    *_, fichiers = scenario
    assert _signal(fichiers, "D")["motif_absence"] == (
        "payout du flux sous le plafond à la décision")
    assert _signal(fichiers, "E")["motif_absence"] == (
        "non confirmé en M1 au rejeu (mode suivante)")
    assert _signal(fichiers, "H")["motif_absence"].startswith("inexpliqué")
    m = _signal(fichiers, "M")
    assert (m["issue_proxy"], m["correspondance"]) == (em.EGALITE, rp.ABSENTE)


def test_les_decisions_s_arretent_a_l_ouverture_de_la_confirmation(scenario):
    _, _, _, rapport, fichiers = scenario
    signaux = _csv(fichiers, "rapprochement_signaux.csv")
    assert len(signaux) == 12
    assert all(int(s["decision"]) < rp.OUVERTURE_CONFIRMATION
               for s in signaux)
    assert "ouverture de la confirmation du gel 2" in rapport


def test_les_quatre_mesures(scenario):
    _, _, _, rapport, fichiers = scenario
    mesures = _csv(fichiers, "rapprochement_mesures.csv")
    ens = next(m for m in mesures if m["dimension"] == "ensemble")
    assert ens["signaux"] == "12"
    assert float(ens["taux_correspondance"]) == pytest.approx(4 / 12)
    assert (ens["n_exacte"], ens["n_approximative"], ens["n_ambiguë"],
            ens["n_absente"]) == ("3", "1", "3", "5")
    # Proxy : 11 issues décisives (M est une égalité), 6 gains.
    assert ens["proxy_decisifs"] == "11"
    assert float(ens["taux_proxy"]) == pytest.approx(6 / 11)
    # Ordres réglés de la période : A, B, F, H, I×2, J, rapatrié ; l'ordre
    # antérieur au rejeu n'y est pas. Gains A F I J, pertes B I rapatrié.
    assert ens["ordres_regles"] == "7"
    assert float(ens["taux_reel_ordres"]) == pytest.approx(4 / 7)
    assert float(ens["rendement_reel_ordres"]) == pytest.approx(
        (4 * 0.92 - 3) / 8)
    # Les mêmes signaux, appariés et réglés : A B F J.
    assert ens["apparies_regles_decisifs"] == "4"
    assert float(ens["taux_proxy_apparies"]) == pytest.approx(3 / 4)
    assert float(ens["taux_reel_apparies"]) == pytest.approx(3 / 4)
    assert float(ens["ecart_proxy_moins_reel"]) == pytest.approx(0)
    assert float(ens["accord_proxy_reel"]) == pytest.approx(2 / 4)
    assert ens["ic_correspondance_bas"] != ""
    dimensions = {m["dimension"] for m in mesures}
    assert dimensions == {"ensemble", "période", "payout", "paire"}
    assert {m["valeur"] for m in mesures if m["dimension"] == "payout"} >= {
        rp.JOUABLE, rp.SOUS_LE_PLAFOND}
    assert "<b>4. Les quatre mesures</b>" in rapport
    assert "ordres AVEC empreinte mais sans signal du rejeu : 1" in rapport
    assert "Par payout du flux" in rapport and "Par période" in rapport


def test_les_limites_comptees_dans_le_rapport(scenario):
    _, _, _, rapport, _ = scenario
    assert "prix d'entrée du broker (openPrice) : 8/8" in rapport
    assert "prix de règlement du broker (closePrice) : 7/8" in rapport
    assert "contexte du signal (empreinte) : 7/8 (88 %) des exécutés" in \
        rapport
    assert "0/3 (0 %) des refusés" in rapport


# --------------------------------------------------------------------------- #
# Les pièces
# --------------------------------------------------------------------------- #

def test_comparer():
    ref = _contexte(1, 600)
    assert rp.comparer(ref, dict(ref))[0] == rp.EGALE
    proche = dict(ref, elan_15m=ref["elan_15m"] * 1.03)
    assert rp.comparer(ref, proche)[0] == rp.PROCHE
    # Une autre zone (touches différentes) n'est jamais « proche ».
    assert rp.comparer(ref, dict(proche, touches=3.0))[0] == rp.DISCORDANTE
    assert rp.comparer(ref, _contexte(50, 600))[0] == rp.DISCORDANTE
    assert rp.comparer(ref, None)[0] == rp.NON_VERIFIABLE
    assert rp.comparer(ref, {"elan_15m": 1.0})[0] == rp.NON_VERIFIABLE


def test_les_modes_de_confirmation():
    assert rp.conforme_au_mode("suivante", 1)
    assert not rp.conforme_au_mode("suivante", 0)
    assert not rp.conforme_au_mode("suivante", 2)
    assert rp.conforme_au_mode("3", 2)
    assert rp.conforme_au_mode("0", 0) and not rp.conforme_au_mode("0", 1)
    assert rp.confirme_selon("suivante", {"attente_confirmation": 1.0})
    assert not rp.confirme_selon("suivante", {"attente_confirmation": 2.0})
    assert rp.confirme_selon("2", {"attente_confirmation": 2.0})
    assert rp.confirme_selon("suivante", {}) is None


def test_ic_ratio():
    blocs = {i: (float(i % 2), 1.0) for i in range(40)}
    bas, haut = rp.ic_ratio(blocs)
    assert bas < 0.5 < haut
    assert rp.ic_ratio(blocs) == (bas, haut), "graine fixe"
    assert rp.ic_ratio({0: (1.0, 1.0)}) is None


def test_la_fraicheur_est_celle_de_la_course():
    from maxprofit.live.plan_demo import FRAICHEUR_MAX_SEC
    assert rp.FRAICHEUR_SEC == FRAICHEUR_MAX_SEC


def test_l_etat_d_un_ordre():
    assert rp.etat_ordre({"accepte": 1, "resultat": "win"}) == rp.REGLE
    assert rp.etat_ordre({"accepte": 1, "resultat": None}) == rp.NON_REGLE
    assert rp.etat_ordre({"accepte": 1, "resultat": "unknown"}) == rp.INCONNUE
    assert rp.etat_ordre({"accepte": 0, "refus": RESERVE}) == \
        rp.RESERVE_SANS_SUITE
    assert rp.etat_ordre({"accepte": 0, "refus": "non envoyé : x"}) == \
        rp.NON_ENVOYE
    assert rp.etat_ordre({"accepte": 0, "refus": "error"}) == rp.REJETE


def test_sans_rejeu_en_memoire(tmp_path):
    course = SimpleNamespace(rejeu_en_memoire=None, sans_issue_en_memoire=None)
    texte, fichiers = rp.rapprochement(None, course)
    assert texte == rp.ATTENDRE_LE_REJEU and fichiers == []


def test_le_service_de_bout_en_bout_sur_l_active(scenario):
    """Le chemin réel : l'active ouverte en lecture seule, sans relève."""
    from maxprofit.hosting import service
    chemin, avant, course, _, _ = scenario
    texte, fichiers = service._rapprochement(chemin, course, "", environ={})
    assert "Rapprochement" in texte and len(fichiers) == 3
    assert hashlib.sha256(Path(chemin).read_bytes()).hexdigest() == avant
    texte, fichiers = service._rapprochement(chemin, None, "", environ={})
    assert "Course indisponible" in texte and fichiers == []


def test_la_commande_telegram_tourne_en_tache_de_fond(tmp_path):
    from tests.test_telegram import (CHAT, INTRUS, FauxClient, _annuaire,
                                     _bot, _message)
    from maxprofit.hosting.telegram import COMMANDES
    assert "rapprochement" in [nom for nom, _ in COMMANDES]
    client = FauxClient()
    documents = []

    async def envoyer_document(chat_id, contenu, nom, legende=""):
        documents.append(nom)

    client.envoyer_document = envoyer_document
    bot = _bot(client, annuaire=_annuaire(
        tmp_path, inscrits=((CHAT, "admin"), (INTRUS, "observateur"))))
    appels = []

    async def rapprocher(argument):
        appels.append(argument)
        return "rapport", [(b"a;b\n", "rapprochement_signaux.csv", "")]

    bot._rapprochement = rapprocher

    async def deroule():
        await bot._traiter(_message("/rapprochement", chat=INTRUS)["message"])
        assert appels == []
        await bot._traiter(_message("/rapprochement")["message"])
        assert "lancé" in client.envoyes[-1][1]
        await bot._rapprochement_en_cours

    asyncio.run(deroule())
    assert appels == [""]
    assert client.envoyes[-1] == (CHAT, "rapport")
    assert documents == ["rapprochement_signaux.csv"]


def test_le_brut_illisible_ne_fait_pas_tomber_la_fiche():
    fiche = rp.fiche_ordre({"id": 1, "pair": "X", "sens": "call",
                            "accepte": 1, "resultat": "win", "mise": 1.0,
                            "profit": 0.9, "brut": "{pas du json"})
    assert fiche["contexte"] is None
    assert "sans contexte du signal" in fiche["limites"]
    assert json.loads(json.dumps(fiche["rendement"])) == pytest.approx(0.9)
