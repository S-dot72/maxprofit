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
        expiration_ts_ms=T0_MS + 380 + 60_000, resultat="win", profit=0.92)
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


def test_l_ecart_d_expiration_se_compte_depuis_l_acceptation():
    """L'horloge du contrat part quand le broker accepte, pas quand on clique."""
    ex = _ex(accepte_ts_ms=T0_MS + 500,
             expiration_ts_ms=T0_MS + 500 + 63_000, expiration_sec=60)
    assert ex.ecart_expiration_sec == pytest.approx(3.0)


# --------------------------------------------------------------------------- #
# Les constats — un écart, et ce qu'il coûte
# --------------------------------------------------------------------------- #

def test_une_execution_propre_ne_coute_rien():
    constats = {c.nom: c for c in rapport([_ex() for _ in range(30)], SIGMA_60S)}
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
            payout_broker_pct=None, expiration_ts_ms=None, resultat=None,
            profit=None, refus="asset closed") for _ in range(2)]
    c = taux_de_refus(lot)
    assert c.valeur == pytest.approx(10.0)
    assert "90 %" in c.verdict


def test_une_mesure_sans_donnee_dit_indetermine_et_non_zero():
    """Rendre zéro laisserait croire à une exécution parfaite."""
    orphelin = _ex(accepte=False, accepte_ts_ms=None, prix_entree=None,
                   payout_broker_pct=None, expiration_ts_ms=None)
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
                     payout_broker_pct=None, expiration_ts_ms=None,
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
