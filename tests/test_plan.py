"""
Le mode plan : la progression, les gardes, et le planning.

Les valeurs attendues viennent des DEUX feuilles, pas d'un calcul refait ici :
`Wealth Warriors Trade Manager v3.0.12` pour l'échelle, `30-Day Compounder v1.2`
pour la courbe. Un test qui recalcule ce qu'il vérifie ne vérifie rien.
"""

from __future__ import annotations

import pytest

from maxprofit.core.errors import BotError
from maxprofit.plan import (
    Arret,
    Echelle,
    Journee,
    Mode,
    PlanCapital,
    ecart_au_plan,
    projeter,
    solde_projete,
)

#: Les réglages exacts des deux feuilles.
CAPITAL = 250.0
PAYOUT = 92
GAIN_PCT = 0.58
SESSIONS = 6
JOURS = 30

#: Les sept mises lues dans la feuille, à l'affichage près.
MISES_FEUILLE = (1.59, 3.31, 6.90, 14.41, 30.07, 62.76, 130.97)


def plan(**surcharges) -> PlanCapital:
    reglages = dict(
        capital_initial=CAPITAL,
        gain_par_session_pct=GAIN_PCT,
        payout_pct=PAYOUT,
        sessions_par_jour=SESSIONS,
        jours=JOURS,
    )
    reglages.update(surcharges)
    return PlanCapital(**reglages)


# --------------------------------------------------------------------------- #
# L'échelle — reproduire la feuille, au centime près
# --------------------------------------------------------------------------- #

def test_l_echelle_reproduit_les_sept_mises_de_la_feuille():
    """Le gain visé de 1,46 $ est celui que la feuille affiche.

    Tolérance RELATIVE, et non au centime : la feuille arrondit chaque mise à
    l'affichage puis calcule la suivante sur la valeur arrondie, si bien que
    l'écart s'accumule — 0,03 $ au 5ᵉ pas, 0,14 $ au 7ᵉ, soit 0,1 % à chaque
    fois. Exiger le centime reviendrait à reproduire les arrondis d'un tableur
    plutôt que sa règle. C'est la règle qui compte, et elle est vérifiée
    exactement par `test_chaque_mise_rembourse_tout_le_passe`.
    """
    e = Echelle(payout_pct=PAYOUT, gain_vise=1.46, pas_max=7)
    for rang, (calcule, feuille) in enumerate(zip(e.mises(), MISES_FEUILLE), 1):
        assert calcule == pytest.approx(feuille, rel=0.005), (
            f"pas {rang} : {calcule:.2f} calculé contre {feuille:.2f} lu"
        )


def test_les_sept_pas_consomment_exactement_le_capital():
    """C'est ce qui fait du 7e pas une liquidation et non un stop."""
    e = Echelle(payout_pct=PAYOUT, gain_vise=1.46, pas_max=7)
    assert e.exposition() == pytest.approx(CAPITAL, abs=0.5)
    assert e.part_du_capital(CAPITAL) == pytest.approx(100, abs=0.5)


def test_chaque_mise_rembourse_tout_le_passe_et_rajoute_le_meme_gain():
    """La règle de la feuille, vérifiée pas à pas plutôt que sur le total."""
    e = Echelle(payout_pct=PAYOUT, gain_vise=1.46, pas_max=7)
    cumul = 0.0
    for mise in e.mises():
        assert mise * e.payout - cumul == pytest.approx(1.46, abs=0.02)
        cumul += mise


# --------------------------------------------------------------------------- #
# ⚠ L'invariance — ce que la progression NE change pas
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pas", [1, 2, 3, 5, 7, 12])
def test_le_seuil_de_rentabilite_ne_depend_PAS_de_la_profondeur(pas):
    """Le test le plus important du module.

    Une martingale redistribue les résultats ; elle ne deplace pas
    l'esperance. Si ce test rougissait un jour, ce serait que quelqu'un croit
    avoir amelioré les chances en changeant la mise — et il faudrait l'arrêter
    là.
    """
    e = Echelle(payout_pct=PAYOUT, gain_vise=1.46, pas_max=pas)
    assert e.seuil_de_rentabilite_pct() == pytest.approx(52.0833, abs=1e-3)


@pytest.mark.parametrize("pas", [2, 3, 7])
def test_au_seuil_exact_l_esperance_est_nulle_quelle_que_soit_l_echelle(pas):
    e = Echelle(payout_pct=PAYOUT, gain_vise=1.46, pas_max=pas)
    seuil = e.seuil_de_rentabilite_pct()
    assert e.esperance_par_session(seuil) == pytest.approx(0.0, abs=0.01)


def test_sous_le_seuil_l_esperance_est_negative_meme_a_deux_pas():
    """49,11 % est le taux MESURÉ sur la collecte au 20 septembre.

    Le garde-fou borne la perte ; il ne la supprime pas.
    """
    e = Echelle(payout_pct=PAYOUT, gain_vise=1.46, pas_max=2)
    assert e.esperance_par_session(49.11) < 0


def test_le_garde_fou_divise_l_exposition_par_cinquante():
    """Ce que les deux pas achètent vraiment."""
    deux = Echelle(payout_pct=PAYOUT, gain_vise=1.46, pas_max=2)
    sept = Echelle(payout_pct=PAYOUT, gain_vise=1.46, pas_max=7)
    assert deux.part_du_capital(CAPITAL) == pytest.approx(1.96, abs=0.05)
    assert sept.part_du_capital(CAPITAL) > 99
    assert sept.exposition() / deux.exposition() > 50


def test_une_echelle_refuse_un_payout_absurde():
    with pytest.raises(BotError, match="payout_pct"):
        Echelle(payout_pct=0, gain_vise=1.46)
    with pytest.raises(BotError, match="payout_pct"):
        Echelle(payout_pct=192, gain_vise=1.46)


# --------------------------------------------------------------------------- #
# La configuration — rien d'implicite sur ce qui touche à l'argent (§5)
# --------------------------------------------------------------------------- #

def test_le_garde_fou_vaut_deux_par_defaut():
    """Un défaut est admis ici parce qu'il va dans le sens sûr : se tromper
    vers 2 coûte des gains manqués, se tromper vers 7 coûte le compte."""
    assert plan().pas_max == 2


def test_le_capital_et_le_payout_n_ont_aucun_defaut():
    with pytest.raises(TypeError):
        PlanCapital(gain_par_session_pct=0.58, payout_pct=92,
                    sessions_par_jour=6, jours=30)      # capital manquant


@pytest.mark.parametrize("mauvais", [
    {"capital_initial": 0},
    {"capital_initial": -10},
    {"gain_par_session_pct": 0},
    {"payout_pct": 0},
    {"sessions_par_jour": 0},
    {"jours": 0},
    {"sessions_perdues_max": 0},
    {"perte_journaliere_max_pct": 0},
])
def test_une_configuration_absurde_est_refusee(mauvais):
    with pytest.raises(BotError):
        plan(**mauvais)


def test_le_gain_vise_suit_le_solde_courant():
    """C'est le bouton « COPY BALANCE » de la feuille : la mise est
    recalculée sur le solde, pas figée sur le capital initial."""
    p = plan()
    assert p.gain_vise(250.0) == pytest.approx(1.45, abs=0.02)
    assert p.gain_vise(500.0) == pytest.approx(2.90, abs=0.02)


def test_le_ratio_journalier_reproduit_la_feuille_et_dit_l_ecart():
    p = plan()
    assert p.ratio_journalier_pct() == pytest.approx(3.48, abs=0.01)
    # Ce qui se produit réellement, les sessions misant sur le solde courant.
    assert p.ratio_journalier_pct(compose=True) == pytest.approx(3.53, abs=0.01)


# --------------------------------------------------------------------------- #
# Les gardes — vérifiées AVANT d'engager, pas après
# --------------------------------------------------------------------------- #

def test_une_journee_neuve_peut_ouvrir_une_session():
    assert Journee(plan(), CAPITAL).peut_ouvrir_une_session() is None


def test_la_journee_s_arrete_apres_les_sessions_prevues():
    j = Journee(plan(), CAPITAL)
    for _ in range(SESSIONS):
        j.enregistrer(gagnee=True, montant=1.46)
    assert j.peut_ouvrir_une_session() is Arret.SESSIONS_EPUISEES


def test_la_journee_s_arrete_sur_les_pertes_consecutives():
    """Le garde-fou demandé : on ne laisse pas la série se poursuivre."""
    j = Journee(plan(sessions_perdues_max=2), CAPITAL)
    j.enregistrer(gagnee=False, montant=-4.90)
    assert j.peut_ouvrir_une_session() is None, "une seule perte n'arrête rien"
    j.enregistrer(gagnee=False, montant=-4.90)
    assert j.peut_ouvrir_une_session() is Arret.SESSIONS_PERDUES


def test_un_gain_remet_le_compteur_de_pertes_a_zero():
    j = Journee(plan(sessions_perdues_max=2), CAPITAL)
    j.enregistrer(gagnee=False, montant=-4.90)
    j.enregistrer(gagnee=True, montant=1.46)
    j.enregistrer(gagnee=False, montant=-4.90)
    assert j.peut_ouvrir_une_session() is None


def test_la_perte_journaliere_maximale_arrete_la_journee():
    j = Journee(plan(perte_journaliere_max_pct=3.0, sessions_perdues_max=99),
                CAPITAL)
    j.enregistrer(gagnee=False, montant=-4.90)     # 1,96 %
    assert j.peut_ouvrir_une_session() is None
    j.enregistrer(gagnee=False, montant=-4.90)     # 9,80 $ = 3,92 %
    assert j.peut_ouvrir_une_session() is Arret.PERTE_MAXIMALE


def test_l_objectif_atteint_arrete_aussi():
    """Un bon jour ne doit pas se transformer en mauvais."""
    j = Journee(plan(objectif_journalier_pct=3.48), CAPITAL)
    j.enregistrer(gagnee=True, montant=CAPITAL * 0.0348)
    assert j.peut_ouvrir_une_session() is Arret.OBJECTIF_ATTEINT


def test_une_echelle_trop_profonde_est_refusee_a_la_CONSTRUCTION():
    """La garde qui manquait à la feuille — son « Stop Loss 20 % » était
    franchi par sa propre progression dès le 5ᵉ pas.

    Refusée à la construction et non en cours de journée : la mise étant
    proportionnelle au solde, l'exposition est une fraction CONSTANTE. Une
    échelle à 7 pas engage ~100 % du solde quel que soit ce solde — donc une
    garde évaluée en séance constaterait à chaque session ce qui pouvait être
    empêché une fois pour toutes.
    """
    with pytest.raises(BotError, match="engage"):
        plan(pas_max=7)


def test_une_echelle_profonde_reste_possible_si_on_le_dit_explicitement():
    """Ce n'est pas une interdiction : c'est un refus du silence."""
    p = plan(pas_max=7, exposition_max_pct=100.0)
    assert p.echelle(CAPITAL).part_du_capital(CAPITAL) > 99


def test_l_exposition_est_une_fraction_constante_du_solde():
    """Le fait qui rend la vérification à la construction légitime."""
    p = plan()
    petit = p.echelle(100.0).part_du_capital(100.0)
    grand = p.echelle(10_000.0).part_du_capital(10_000.0)
    assert petit == pytest.approx(grand, rel=1e-9)


def test_le_resultat_de_la_journee_est_signe():
    j = Journee(plan(), CAPITAL)
    j.enregistrer(gagnee=True, montant=1.46)
    assert j.resultat == pytest.approx(1.46)
    assert j.resultat_pct == pytest.approx(0.584, abs=0.01)


# --------------------------------------------------------------------------- #
# Le planning — la courbe de la seconde feuille
# --------------------------------------------------------------------------- #

def test_la_projection_reproduit_le_30_day_compounder():
    """Trois points lus dans la feuille : jours 1, 15 et 30."""
    lignes = projeter(plan())
    attendus = {1: 258.70, 15: 417.62, 30: 697.64}
    for jour, solde in attendus.items():
        assert lignes[jour - 1].solde == pytest.approx(solde, abs=0.05), (
            f"jour {jour}"
        )


def test_le_gain_du_premier_jour_est_celui_de_la_feuille():
    assert projeter(plan())[0].gain_du_jour == pytest.approx(8.70, abs=0.02)


def test_le_cumul_du_dernier_jour_est_celui_de_la_feuille():
    assert projeter(plan())[-1].gain_cumule == pytest.approx(447.64, abs=0.05)


def test_la_composition_intrajournaliere_donne_un_peu_plus():
    """La feuille somme les sessions au lieu de les composer : elle
    sous-estime de 0,05 point par jour. L'écart va dans le sens optimiste,
    et une projection comparée au réel doit dire laquelle elle calcule."""
    simple = projeter(plan())[-1].solde
    compose = projeter(plan(), compose=True)[-1].solde
    assert compose > simple
    assert compose / simple == pytest.approx(1.015, abs=0.01)


def test_le_jour_zero_est_le_capital():
    assert solde_projete(plan(), 0) == CAPITAL


def test_un_jour_hors_du_plan_est_refuse():
    with pytest.raises(BotError, match="31"):
        solde_projete(plan(), 31)
    with pytest.raises(BotError, match="négatif"):
        solde_projete(plan(), -1)


def test_l_ecart_au_plan_dit_l_avance_et_le_retard():
    e = ecart_au_plan(plan(), jour=1, solde_reel=300.0)
    assert e.en_avance
    assert "en avance" in e.resume()

    r = ecart_au_plan(plan(), jour=1, solde_reel=200.0)
    assert not r.en_avance
    assert r.ecart == pytest.approx(200.0 - 258.70, abs=0.05)
    assert "en retard" in r.resume()


# --------------------------------------------------------------------------- #
# Les deux modes
# --------------------------------------------------------------------------- #

def test_les_deux_modes_existent_et_se_nomment():
    assert str(Mode.TRADE_SEUL) == "trade_seul"
    assert str(Mode.PLAN) == "plan"


def test_le_module_plan_n_importe_aucune_strategie():
    """La frontière qui rend le backtest lisible : un dimensionnement capable
    d'influencer un signal ferait mesurer la stratégie *plus* la mise."""
    import ast
    from pathlib import Path

    paquet = Path(__file__).resolve().parents[1] / "maxprofit" / "plan"
    for fichier in paquet.rglob("*.py"):
        arbre = ast.parse(fichier.read_text(encoding="utf-8"))
        for noeud in ast.walk(arbre):
            noms = []
            if isinstance(noeud, ast.Import):
                noms = [a.name for a in noeud.names]
            elif isinstance(noeud, ast.ImportFrom):
                noms = [noeud.module or ""]
            for nom in noms:
                assert "strategies" not in nom, f"{fichier.name} importe {nom}"


# --------------------------------------------------------------------------- #
# La session — le pont entre les signaux et le plan
# --------------------------------------------------------------------------- #

from maxprofit.plan import EtatSession, Session      # noqa: E402


def echelle_de_test(pas: int = 2) -> Echelle:
    return Echelle(payout_pct=PAYOUT, gain_vise=1.46, pas_max=pas)


def test_une_session_gagnee_au_premier_pas_rend_le_gain_vise():
    s = Session(echelle_de_test())
    assert s.enregistrer(gagne=True) is EtatSession.GAGNEE
    assert s.montant == pytest.approx(1.46)
    assert s.engage == pytest.approx(1.59, abs=0.02)


def test_une_session_gagnee_au_second_pas_rend_le_MEME_gain():
    """C'est toute la promesse de l'échelle : le pas gagnant rembourse le passé
    et laisse exactement le gain visé, quel que soit le rang."""
    s = Session(echelle_de_test())
    s.enregistrer(gagne=False)
    assert s.enregistrer(gagne=True) is EtatSession.GAGNEE
    assert s.montant == pytest.approx(1.46)


def test_une_session_perdue_coute_exactement_l_exposition():
    s = Session(echelle_de_test())
    s.enregistrer(gagne=False)
    assert s.enregistrer(gagne=False) is EtatSession.PERDUE
    assert s.montant == pytest.approx(-s.echelle.exposition())
    assert s.montant == pytest.approx(-4.90, abs=0.05)


def test_les_mises_suivent_l_echelle():
    s = Session(echelle_de_test())
    assert s.mise_courante() == pytest.approx(1.59, abs=0.02)
    s.enregistrer(gagne=False)
    assert s.mise_courante() == pytest.approx(3.31, abs=0.03)


def test_une_session_terminee_refuse_d_enregistrer_encore():
    s = Session(echelle_de_test())
    s.enregistrer(gagne=True)
    with pytest.raises(BotError, match="gagnée"):
        s.enregistrer(gagne=False)
    with pytest.raises(BotError, match="gagnée"):
        s.mise_courante()


def test_une_session_ouverte_n_a_pas_encore_de_resultat():
    """Rendre zéro se propagerait en silence dans un calcul de solde."""
    with pytest.raises(BotError, match="ouverte"):
        Session(echelle_de_test()).montant


def test_une_session_interrompue_ne_coute_QUE_ce_qui_fut_engage():
    """La distinction qui compte : interrompue n'est pas perdue.

    La compter comme perdue surestimerait la perte de l'exposition entière ;
    la compter comme gagnée l'effacerait.
    """
    s = Session(echelle_de_test())
    s.enregistrer(gagne=False)                     # un seul pas joué
    assert s.interrompre() is EtatSession.INTERROMPUE
    assert s.montant == pytest.approx(-1.59, abs=0.02)
    assert s.montant > -s.echelle.exposition(), "moins que la perte totale"


def test_interrompre_une_session_terminee_ne_change_rien():
    s = Session(echelle_de_test())
    s.enregistrer(gagne=True)
    assert s.interrompre() is EtatSession.GAGNEE


def test_le_pont_alimente_la_journee():
    """Le bout en bout : des sessions résolues font avancer le solde."""
    j = Journee(plan(), CAPITAL)
    for gagne in (True, False, True):
        s = Session(plan().echelle(j.solde))
        if gagne:
            s.enregistrer(gagne=True)
        else:
            s.enregistrer(gagne=False)
            s.enregistrer(gagne=False)
        j.enregistrer(gagnee=s.etat is EtatSession.GAGNEE, montant=s.montant)

    assert j.sessions_jouees == 3
    assert j.sessions_perdues_daffilee == 0        # la dernière est gagnée
    assert j.solde < CAPITAL + 3 * 1.46            # la perte a bien mordu


def test_la_session_ne_produit_aucun_signal():
    """La frontière : forcer un pas pour « finir » la martingale ferait
    prendre un trade que la stratégie n'a jamais demandé.

    Vérifié par l'AST et non par une recherche de texte : `Signal` DOIT
    apparaître dans la documentation du module — c'est là qu'on explique
    pourquoi il n'y est pas dans le code. Une première version de ce test
    cherchait la chaîne et rougissait sur le paragraphe qui énonce la règle.
    """
    import ast
    from pathlib import Path

    fichier = (Path(__file__).resolve().parents[1]
               / "maxprofit" / "plan" / "session.py")
    arbre = ast.parse(fichier.read_text(encoding="utf-8"))

    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Name):
            assert noeud.id != "Signal", "le module référence Signal"
        if isinstance(noeud, ast.Attribute):
            assert noeud.attr != "Signal", "le module référence Signal"
        noms = []
        if isinstance(noeud, ast.Import):
            noms = [a.name for a in noeud.names]
        elif isinstance(noeud, ast.ImportFrom):
            noms = [a.name for a in noeud.names] + [noeud.module or ""]
        assert "Signal" not in noms, f"le module importe {noms}"
