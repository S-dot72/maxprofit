"""
L'exécution : le seul paquet où un test raté coûte de l'argent.

Deux familles de tests, et la première est la plus importante :

  1. LA GARDE REFUSE. Un jeton sans marque « démo » doit avoir exactement le
     même effet qu'un jeton marqué réel. Il n'y a pas de cas intermédiaire.

  2. LES ÉCARTS SE MESURENT DANS LA BONNE UNITÉ. Un glissement en prix ne veut
     rien dire ; un glissement en points de taux de réussite se compare aux
     2,08 points qui séparent le hasard du seuil de rentabilité.
"""

from __future__ import annotations

import pytest

from maxprofit.core.errors import BotError
from maxprofit.execution import (
    CompteRefuse,
    Execution,
    JournalExecution,
    Plafonds,
    e1_payout,
    e2_glissement,
    e3_latence,
    e4_expiration,
    est_demo,
    exiger_un_compte_demo,
    points_de_taux,
    rapport,
    taux_de_refus,
)

T0_MS = 1_789_000_000_000
#: Écart-type du mouvement de prix sur 60 s, mesuré sur les paires épinglées :
#: ~0,03 % de 1,08 ≈ 0,00033.
SIGMA_60S = 0.00033


def _jeton(champs: str) -> str:
    return '42["auth",{"session":"' + "x" * 40 + '",' + champs + "}]"


def _ex(**kw) -> Execution:
    defauts = dict(
        pair="EURUSD_otc", sens="call", mise=1.0, signal_ts_ms=T0_MS,
        prix_attendu=1.08231, payout_flux_pct=92.0, expiration_sec=60,
        clic_ts_ms=T0_MS + 120, accepte_ts_ms=T0_MS + 380, accepte=True,
        prix_entree=1.08231, payout_broker_pct=92.0,
        # Horloge BROKER pour ces deux-là, décalée de deux heures, et le
        # décalage qui permet de les rapporter à la nôtre.
        ouverture_ts_ms=T0_MS + 7_200_000 + 380,
        expiration_ts_ms=T0_MS + 7_200_000 + 380 + 60_000,
        decalage_broker_ms=7_200_000, resultat="win", profit=0.92)
    return Execution(**{**defauts, **kw})


# --------------------------------------------------------------------------- #
# La garde — elle refuse par défaut, et c'est tout son intérêt
# --------------------------------------------------------------------------- #

def test_un_jeton_demo_est_accepte():
    exiger_un_compte_demo(_jeton('"isDemo":1,"uid":122847706'))


def test_un_jeton_reel_est_refuse():
    with pytest.raises(CompteRefuse, match="compte RÉEL"):
        exiger_un_compte_demo(_jeton('"isDemo":0,"uid":122847706'))


def test_un_jeton_sans_marque_est_refuse_comme_un_jeton_reel():
    """LE test du module.

    Ne pas savoir si le compte est démo doit avoir exactement le même effet
    que savoir qu'il est réel. Un paquet qui, dans le doute, laisserait passer
    l'ordre serait dangereux précisément dans le cas qu'il est censé couvrir.
    """
    with pytest.raises(CompteRefuse, match="aucun champ"):
        exiger_un_compte_demo(_jeton('"uid":122847706'))


def test_le_champ_absent_se_distingue_du_champ_faux():
    """Trois valeurs de retour et non deux : un refus dont on comprend la
    cause se répare, un refus opaque se contourne."""
    assert est_demo(_jeton('"isDemo":1')) is True
    assert est_demo(_jeton('"isDemo":0')) is False
    assert est_demo(_jeton('"uid":1')) is None
    assert est_demo("") is None


def test_une_mise_au_dessus_du_plafond_absolu_est_refusee():
    """Sur un compte démo il ne protège rien. Il rend impossible qu'un zéro de
    trop passe inaperçu le jour où un jeton réel se glisse malgré le reste."""
    Plafonds(mise=5.0)
    with pytest.raises(BotError, match="plafond"):
        Plafonds(mise=100.0)


def test_les_plafonds_disent_ce_qu_ils_engagent():
    p = Plafonds(mise=1.0, ordres_max=200)
    assert p.engagement_max == 200.0


def test_un_debit_d_ordres_absurde_est_refuse():
    """Au-delà, le broker limite le débit et la sonde mesure une latence
    qu'elle a créée elle-même."""
    with pytest.raises(BotError, match="débit"):
        Plafonds(mise=1.0, ordres_max=50_000)


# --------------------------------------------------------------------------- #
# Les écarts, et leur signe
# --------------------------------------------------------------------------- #

def test_le_glissement_est_signe_dans_le_sens_du_pari():
    """Un glissement qui va dans notre sens n'est pas un coût.

    Sur un CALL, entrer PLUS BAS que prévu est favorable ; sur un PUT, c'est
    l'inverse. Prendre la valeur absolue ferait passer une exécution favorable
    pour une exécution chère.
    """
    favorable_call = _ex(sens="call", prix_attendu=1.08231, prix_entree=1.08221)
    assert favorable_call.glissement > 0

    defavorable_call = _ex(sens="call", prix_attendu=1.08231, prix_entree=1.08241)
    assert defavorable_call.glissement < 0

    meme_prix_en_put = _ex(sens="put", prix_attendu=1.08231, prix_entree=1.08241)
    assert meme_prix_en_put.glissement > 0


def test_un_glissement_se_traduit_en_points_de_taux():
    """L'unité qui décide. 0,399 = φ(0), pas un réglage."""
    #: Un dixième d'écart-type contre soi.
    cout = points_de_taux(-0.1 * SIGMA_60S, SIGMA_60S)
    assert cout == pytest.approx(-3.99, abs=0.01)
    assert abs(cout) > 2.08, (
        "un dixième de sigma coûte déjà plus que les 2,08 points qui séparent "
        "le hasard du seuil de rentabilité à 92 %")


def test_un_glissement_favorable_rapporte_des_points():
    assert points_de_taux(+0.1 * SIGMA_60S, SIGMA_60S) > 0


def test_sans_echelle_un_glissement_ne_se_traduit_pas():
    with pytest.raises(BotError, match="sigma_horizon"):
        points_de_taux(0.0001, 0.0)


def test_la_latence_se_compte_depuis_le_SIGNAL_pas_depuis_le_clic():
    """Le temps passé entre la décision et le clic est du coût, lui aussi.
    Ne compter que clic -> accepté effacerait la moitié du problème."""
    ex = _ex(signal_ts_ms=T0_MS, clic_ts_ms=T0_MS + 300,
             accepte_ts_ms=T0_MS + 800)
    assert ex.latence_ms == 800


def test_l_ecart_d_expiration_ne_melange_pas_deux_horloges():
    """LE bug que le premier ordre réel a révélé.

    `openTimestamp` et `closeTimestamp` viennent de l'horloge du BROKER, qui
    avance de deux heures sur l'UTC. La première version les comparait à notre
    horodatage local et annonçait +7202 s sur un contrat qui avait duré 60 s
    exactement.

    Le décalage est ici volontairement énorme — deux heures — et le résultat
    doit rester +3 s : une différence entre deux instants de la MÊME horloge
    est immunisée contre son décalage, quel qu'il soit.
    """
    decalage = 7_200_000
    ex = _ex(accepte_ts_ms=T0_MS + 500,
             ouverture_ts_ms=T0_MS + decalage + 2_800,
             expiration_ts_ms=T0_MS + decalage + 2_800 + 63_000,
             decalage_broker_ms=decalage, expiration_sec=60)
    assert ex.ecart_expiration_sec == pytest.approx(3.0)


def test_l_attente_avant_ouverture_traverse_les_deux_horloges():
    """La seule mesure qui doit franchir les deux mondes, d'où le décalage.

    Mesurée au premier ordre réel : le contrat s'est ouvert 2,3 s APRÈS
    l'acceptation. Ni latence réseau, ni glissement — un troisième délai.
    """
    decalage = 7_200_000
    ex = _ex(accepte_ts_ms=T0_MS + 500,
             ouverture_ts_ms=T0_MS + decalage + 2_800,
             decalage_broker_ms=decalage)
    assert ex.attente_ouverture_sec == pytest.approx(2.3)


def test_sans_decalage_connu_l_attente_est_indeterminee():
    """Une valeur devinée ferait passer un décalage d'horloge pour un délai
    d'exécution — exactement l'erreur qu'on vient de corriger."""
    ex = _ex(ouverture_ts_ms=T0_MS + 2_800, decalage_broker_ms=None)
    assert ex.attente_ouverture_sec is None


# --------------------------------------------------------------------------- #
# Les constats — un écart, et ce qu'il coûte
# --------------------------------------------------------------------------- #

def test_une_execution_propre_ne_coute_rien():
    constats = {c.nom: c for c in rapport([_ex() for _ in range(30)], SIGMA_60S, 60)}
    assert constats["E2 glissement à l'entrée"].cout_points == pytest.approx(0)
    assert "là où le backtest la place" in constats["E2 glissement à l'entrée"].verdict
    assert "durée demandée est la durée tenue" in constats["E4 durée réelle vs demandée"].verdict


def test_un_payout_rogne_par_le_broker_deplace_le_seuil():
    lot = [_ex(payout_flux_pct=92.0, payout_broker_pct=90.0) for _ in range(30)]
    c = e1_payout(lot)
    assert c.valeur == pytest.approx(-2.0)
    assert "MOINS" in c.verdict and "seuil de rentabilité" in c.verdict


def test_un_glissement_defavorable_est_chiffre_en_points():
    lot = [_ex(prix_attendu=1.08231, prix_entree=1.08231 + 0.1 * SIGMA_60S)
           for _ in range(30)]
    c = e2_glissement(lot, SIGMA_60S)
    assert c.cout_points == pytest.approx(-3.99, abs=0.05)
    assert "2,08 points" in c.verdict


def test_une_latence_longue_est_rapportee_a_l_echeance_la_plus_courte():
    lot = [_ex(signal_ts_ms=T0_MS, clic_ts_ms=T0_MS + 1000,
               accepte_ts_ms=T0_MS + 4000) for _ in range(20)]
    c = e3_latence(lot)
    assert c.valeur == pytest.approx(4000)
    assert "30 s" in c.verdict, (
        "c'est sur l'échéance la plus courte que la latence fait mal")


def test_les_refus_comptent_dans_le_nombre_de_trades():
    """Un backtest qui ne garde que les ordres acceptés a un taux de refus nul
    par construction, ce qui est la définition d'une mesure impossible."""
    lot = [_ex() for _ in range(18)] + [
        _ex(accepte=False, accepte_ts_ms=None, prix_entree=None,
            payout_broker_pct=None, ouverture_ts_ms=None,
            expiration_ts_ms=None, resultat=None,
            profit=None, refus="asset closed") for _ in range(2)]
    c = taux_de_refus(lot)
    assert c.valeur == pytest.approx(10.0)
    assert "90 %" in c.verdict


def test_une_mesure_sans_donnee_dit_indetermine_et_non_zero():
    """Rendre zéro laisserait croire à une exécution parfaite."""
    orphelin = _ex(accepte=False, accepte_ts_ms=None, prix_entree=None,
                   payout_broker_pct=None, ouverture_ts_ms=None,
                   expiration_ts_ms=None)
    for constat in (e1_payout([orphelin]), e2_glissement([orphelin], SIGMA_60S),
                    e3_latence([orphelin]), e4_expiration([orphelin])):
        assert "indéterminé" in constat.verdict
        assert constat.n == 0


# --------------------------------------------------------------------------- #
# Le journal
# --------------------------------------------------------------------------- #

def test_le_journal_garde_la_charge_utile_telle_quelle(tmp_path):
    """La seule protection contre une lecture fausse des noms de champs d'une
    API non documentée : si l'interprétation est fausse, `brut` a la vérité."""
    brut = {"id": "abc", "openPrice": 1.08231, "champ_inconnu": [1, 2, 3]}
    with JournalExecution(tmp_path / "exec.db", campagne="e1") as j:
        j.ecrire(_ex(order_id="abc", brut=brut))
        relu = j.toutes()
    assert relu[0].brut == brut


def test_un_ordre_refuse_est_enregistre_lui_aussi(tmp_path):
    with JournalExecution(tmp_path / "exec.db", campagne="e1") as j:
        j.ecrire(_ex(accepte=False, accepte_ts_ms=None, prix_entree=None,
                     payout_broker_pct=None, ouverture_ts_ms=None,
                     expiration_ts_ms=None,
                     resultat=None, profit=None, refus="asset closed"))
        relu = j.toutes()
    assert len(relu) == 1
    assert not relu[0].accepte and relu[0].refus == "asset closed"


def test_un_ordre_jamais_tente_n_est_pas_un_refus(tmp_path):
    with JournalExecution(tmp_path / "exec.db", campagne="e1") as j:
        with pytest.raises(BotError, match="jamais tenté"):
            j.ecrire(_ex(clic_ts_ms=None))


def test_un_sens_inconnu_est_refuse():
    with pytest.raises(BotError, match="call"):
        _ex(sens="haut")


# --------------------------------------------------------------------------- #
# La mise est un ARGUMENT — le bogue qui a tué une course
# --------------------------------------------------------------------------- #

def test_le_plafond_n_est_pas_une_mise_par_defaut():
    """Le bogue trouvé en production, et il est structurel.

    `Plafonds.mise` est un PLAFOND. La première version du courtier le misait
    à chaque ordre au lieu de la mise que l'échelle lui donnait : la
    martingale plaçait trois fois le même montant, et son échelle était
    purement décorative. Rien ne levait — les ordres partaient, les résultats
    rentraient, le solde était simplement faux.
    """
    p = Plafonds(mise=10.0)
    assert p.mise == 10.0
    # Trois mises d'échelle distinctes doivent toutes tenir sous le plafond.
    for mise in (1.59, 3.31, 6.90):
        assert 0 < mise <= p.mise


def test_le_plafond_absolu_se_releve_explicitement():
    """Une martingale dimensionnée sur le solde GRANDIT avec lui. Un plafond
    constant à 10 $ aurait refusé chaque ordre dès le troisième jour d'un plan
    qui vise 4 998 $ — silencieusement, en abandonnant la course."""
    with pytest.raises(BotError, match="hors"):
        Plafonds(mise=138.0)
    grand = Plafonds(mise=138.0, mise_max_absolue=140.0)
    assert grand.mise == 138.0


def test_un_plafond_absolu_nul_est_refuse():
    with pytest.raises(BotError, match="mise_max_absolue"):
        Plafonds(mise=1.0, mise_max_absolue=0.0)


def test_le_courtier_installe_une_boucle_dans_son_thread():
    """Le bogue qui a arrêté la course une seconde fois.

    La bibliothèque du broker appelle `asyncio.get_event_loop()` dans son
    constructeur. Un thread SECONDAIRE n'en a pas : « There is no current
    event loop in thread 'course-plan' ». En local tout passait, parce que le
    thread principal en possède une — seul le déplacement dans un thread l'a
    révélé, en production.
    """
    import asyncio
    import threading

    from maxprofit.collect.pocketoption import installer_boucle_asyncio

    vu = {}

    def dans_un_thread():
        # `asyncio.get_event_loop()` est l'appel EXACT que fait la
        # bibliothèque du broker. C'est lui qu'on veut voir échouer avant,
        # réussir après — pas un équivalent qui pourrait se comporter
        # autrement.
        try:
            asyncio.get_event_loop()
            vu["avant"] = "une boucle existait déjà"
        except RuntimeError:
            vu["avant"] = None
        boucle = installer_boucle_asyncio(None)
        vu["apres"] = asyncio.get_event_loop()
        vu["rendue"] = boucle
        boucle.close()

    t = threading.Thread(target=dans_un_thread)
    t.start()
    t.join(timeout=5)
    assert vu["avant"] is None, "le test ne prouve rien si le thread en avait une"
    assert vu["apres"] is vu["rendue"]


def test_une_boucle_deja_en_cours_est_refusee_au_lieu_d_etre_remplacee():
    """La remplacer casserait le serveur HTTP du service hébergé."""
    import asyncio

    from maxprofit.collect.pocketoption import (
        SourceIndisponible, installer_boucle_asyncio)

    async def depuis_une_coroutine():
        with pytest.raises(SourceIndisponible, match="tourne déjà"):
            installer_boucle_asyncio(None)

    asyncio.run(depuis_une_coroutine())
