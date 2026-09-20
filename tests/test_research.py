"""
Le protocole de recherche : ce sont les REFUS qui comptent.

Un framework de recherche ne se juge pas sur ce qu'il calcule — n'importe quel
script calcule un taux de réussite. Il se juge sur ce qu'il empêche :

    mélanger l'OTC et le non-OTC
    conclure une expérience qu'on n'a pas déclarée
    conclure deux fois la même
    ouvrir le sceau sans laisser de trace
    appeler « hypothèse » une étiquette comme « test_3 »

Chacun de ces tests correspond à une façon documentée de se tromper soi-même.
"""

from __future__ import annotations

import pytest

from maxprofit.core.errors import BotError
from maxprofit.research import (
    Decoupage,
    Periode,
    Registre,
    Resultat,
    Univers,
    benjamini_hochberg,
    esperance,
    seuil_de_rentabilite_pct,
    signaux_pour_etablir,
)

JOUR = 86400
T0 = 1_757_376_000          # 2025-09-09, début de campagne


@pytest.fixture
def registre(tmp_path):
    with Registre(tmp_path / "research.db", campagne="essai") as r:
        yield r


def _declarer(registre, **kw) -> int:
    defauts = dict(
        famille="baseline", nom="momentum_10",
        hypothese="Après dix minutes de hausse, la bougie suivante monte.",
        univers="EURUSD_otc|60", echeance_sec=60,
        periode_debut_sec=T0, periode_fin_sec=T0 + 10 * JOUR)
    return registre.pre_enregistrer(**{**defauts, **kw})


# --------------------------------------------------------------------------- #
# Métriques : le taux de réussite ne dit rien tout seul
# --------------------------------------------------------------------------- #

def test_le_seuil_de_rentabilite_suit_le_payout():
    assert seuil_de_rentabilite_pct(92) == pytest.approx(52.0833, abs=1e-4)
    assert seuil_de_rentabilite_pct(88) == pytest.approx(53.1915, abs=1e-4)
    assert seuil_de_rentabilite_pct(72) == pytest.approx(58.1395, abs=1e-4)


def test_le_meilleur_taux_de_reussite_peut_etre_la_pire_esperance():
    """Le résultat mesuré qui justifie tout ce module.

    À payout bas on gagne PLUS SOUVENT et on perd BEAUCOUP PLUS. Un tableau de
    bord qui n'afficherait que le taux de réussite désignerait le pire moment
    de la journée comme le meilleur.
    """
    bon_taux = Resultat(signaux=415, gains=221, payout_moyen_pct=24.8)
    mauvais_taux = Resultat(signaux=17279, gains=8727, payout_moyen_pct=92.0)

    assert bon_taux.reussite_pct > mauvais_taux.reussite_pct
    assert bon_taux.esperance < mauvais_taux.esperance
    assert not bon_taux.rentable and not mauvais_taux.rentable


def test_l_esperance_est_nulle_exactement_au_seuil():
    assert esperance(seuil_de_rentabilite_pct(92), 92) == pytest.approx(0, abs=1e-12)
    assert esperance(53.0, 92) > 0
    assert esperance(52.0, 92) < 0


def test_sans_signal_il_n_y_a_pas_de_taux_de_reussite():
    """Rendre 0 ou 50 laisserait croire à une mesure là où il n'y a rien."""
    vide = Resultat(signaux=0, gains=0, payout_moyen_pct=92)
    with pytest.raises(BotError, match="Aucun signal"):
        _ = vide.reussite_pct


def test_le_sigma_se_compte_au_seuil_et_non_au_hasard():
    """Battre le hasard ne rapporte rien ; battre le seuil oui."""
    r = Resultat(signaux=1149, gains=610, payout_moyen_pct=92.0)
    assert r.reussite_pct == pytest.approx(53.09, abs=0.01)
    assert r.sigma_au_seuil == pytest.approx(0.68, abs=0.05)
    # Contre le hasard, le même résultat paraîtrait deux fois plus fort.
    contre_le_hasard = (r.reussite_pct - 50) / r.ecart_type_pct
    assert contre_le_hasard > 2 * r.sigma_au_seuil


def test_un_avantage_fort_se_tranche_en_quelques_signaux():
    """Le calcul qui dit OÙ chercher : rare et précis, pas fréquent et tiède."""
    assert signaux_pour_etablir(75, 88) <= 40
    assert signaux_pour_etablir(65, 88) < 200
    assert signaux_pour_etablir(55, 88) > 5000


def test_une_precision_sous_le_seuil_ne_s_etablit_jamais():
    with pytest.raises(BotError, match="aucun nombre de signaux"):
        signaux_pour_etablir(52, 92)


def test_benjamini_hochberg_est_monotone_et_borne():
    brutes = [0.001, 0.01, 0.03, 0.2, 0.9]
    corrigees = benjamini_hochberg(brutes)
    assert all(c >= b for c, b in zip(corrigees, brutes))
    assert all(0 <= c <= 1 for c in corrigees)
    assert corrigees == sorted(corrigees)


def test_la_meilleure_de_mille_hypotheses_ne_survit_pas():
    """Sur du bruit, la meilleure de mille affiche p = 0,001. C'est attendu.

    Corrigée, elle remonte à 0,5 — pas à 1. La méthode est un step-up : la p
    corrigée d'un rang ne peut pas dépasser celle des rangs suivants, et les
    999 autres plafonnent à 0,5. Le chiffre exact importe moins que le fait
    qu'il soit dix fois au-dessus du seuil de 0,05.
    """
    p_values = [0.001] + [0.5] * 999
    corrigee = benjamini_hochberg(p_values)[0]
    assert corrigee == pytest.approx(0.5)
    assert corrigee > 0.05


# --------------------------------------------------------------------------- #
# Univers : l'interdit qui est du code et pas de la discipline
# --------------------------------------------------------------------------- #

def test_un_univers_ne_melange_pas_l_otc_et_le_reel():
    """Kurtosis 2,9 contre 5-20 : moyenner les deux produit un résultat qui
    n'appartient à aucun des deux marchés, et rien ne le signale en sortie."""
    with pytest.raises(BotError, match="ne mélange pas"):
        Univers(paires=("EURUSD_otc", "EURUSD"), echeances_sec=(60,))


def test_un_univers_homogene_connait_sa_famille():
    assert Univers(("EURUSD_otc", "AUDCAD_otc"), (60, 300)).otc
    assert Univers(("EURUSD", "GBPUSD"), (60,)).famille == "réel"


def test_une_echeance_que_la_plateforme_ne_vend_pas_est_refusee():
    """Un avantage mesuré sur une durée inexécutable n'est pas un avantage."""
    with pytest.raises(BotError, match="ne vend pas"):
        Univers(("EURUSD_otc",), echeances_sec=(60, 47))


def test_la_signature_ne_depend_pas_de_l_ordre():
    """Sinon la même expérience compterait deux fois dans la correction."""
    a = Univers(("AUDCAD_otc", "EURUSD_otc"), (300, 60))
    b = Univers(("EURUSD_otc", "AUDCAD_otc"), (60, 300))
    assert a.signature() == b.signature()


def test_une_paire_repetee_est_refusee():
    with pytest.raises(BotError, match="répétée"):
        Univers(("EURUSD_otc", "EURUSD_otc"), (60,))


# --------------------------------------------------------------------------- #
# Le sceau
# --------------------------------------------------------------------------- #

def test_le_sceau_couvre_la_fin_de_la_periode():
    """La fin, et pas un bloc au milieu : la seule question qui se pose en
    trading est « aurait-elle tenu sur ce qui est venu APRÈS ? »."""
    d = Decoupage.depuis_la_fin(T0, T0 + 12 * JOUR, part_scellee=0.25)
    assert d.scelle.fin_sec == T0 + 12 * JOUR
    assert d.recherche.fin_sec == d.scelle.debut_sec
    assert d.scelle.jours == pytest.approx(3.0, abs=0.01)


def test_recherche_et_sceau_ne_peuvent_pas_se_chevaucher():
    with pytest.raises(BotError, match="chevauchent"):
        Decoupage(Periode(T0, T0 + 10 * JOUR),
                  Periode(T0 + 9 * JOUR, T0 + 12 * JOUR))


def test_le_sceau_ne_peut_pas_preceder_la_recherche():
    """Valider sur le passé d'une règle mise au point sur le futur est un
    look-ahead à l'échelle de l'expérience entière."""
    with pytest.raises(BotError, match="précède"):
        Decoupage(Periode(T0 + 5 * JOUR, T0 + 12 * JOUR),
                  Periode(T0, T0 + 5 * JOUR))


def test_le_sceau_intact_ne_produit_aucun_avertissement():
    d = Decoupage.depuis_la_fin(T0, T0 + 12 * JOUR)
    assert d.scelle_intact
    assert d.avertissement() is None


def test_ouvrir_le_sceau_sans_raison_est_refuse():
    d = Decoupage.depuis_la_fin(T0, T0 + 12 * JOUR)
    with pytest.raises(BotError, match="sans raison"):
        d.ouvrir_le_sceau("   ")
    assert d.scelle_intact, "un refus ne doit pas compter comme une ouverture"


def test_la_deuxieme_ouverture_est_signalee_plus_fort_que_la_premiere():
    """Une fois, c'est l'examen final. Deux fois, il n'y a plus d'examen."""
    d = Decoupage.depuis_la_fin(T0, T0 + 12 * JOUR)
    d.ouvrir_le_sceau("examen final de la stratégie retour_mediane")
    premier = d.avertissement()
    assert premier is not None and "une fois" in premier

    d.ouvrir_le_sceau("juste pour vérifier le chargement")
    second = d.avertissement()
    assert "OUVERT 2 FOIS" in second
    assert "juste pour vérifier" in second, (
        "la raison doit rester lisible : c'est elle qui dit si l'ouverture "
        "était l'examen ou une curiosité")


# --------------------------------------------------------------------------- #
# Le registre : ce sont les échecs qu'il doit garder
# --------------------------------------------------------------------------- #

def test_une_etiquette_n_est_pas_une_hypothese(registre):
    with pytest.raises(BotError, match="pas une hypothèse"):
        _declarer(registre, hypothese="test_3")


def test_on_ne_conclut_pas_ce_qu_on_n_a_pas_declare(registre):
    with pytest.raises(BotError, match="inconnue"):
        registre.conclure(404, Resultat(10, 6, 92))


def test_on_ne_conclut_pas_deux_fois(registre):
    """Réécrire effacerait la première du compte, et c'est le compte qui fait
    toute la valeur du registre."""
    i = _declarer(registre)
    registre.conclure(i, Resultat(100, 51, 92))
    with pytest.raises(BotError, match="déjà conclue"):
        registre.conclure(i, Resultat(100, 58, 92))


def test_une_experience_abandonnee_reste_visible(registre):
    """Quelqu'un a lancé une mesure et ne l'a pas rapportée. C'est une
    information, pas un défaut."""
    _declarer(registre, nom="abandonnee")
    i = _declarer(registre, nom="conclue")
    registre.conclure(i, Resultat(100, 51, 92))

    compte = registre.compte()
    assert compte == {"declarees": 2, "conclues": 1, "abandonnees": 1,
                      "avec_p": 0, "sur_scelle": 0}


def test_la_correction_porte_sur_toute_la_campagne_pas_sur_le_script(registre):
    """LE point du registre.

    Une hypothèse à p = 0,002 a l'air solide. Déclarée après quarante-neuf
    autres, elle ne l'est plus — et c'est vrai même si les quarante-neuf ont
    été mesurées la semaine précédente, dans un autre script.
    """
    gagnante = _declarer(registre, nom="prometteuse")
    registre.conclure(gagnante, Resultat(300, 170, 92), p_permutation=0.002)

    seule = {e.id: p for e, p in registre.corriger()}
    assert seule[gagnante] == pytest.approx(0.002), (
        "seule de sa campagne, elle n'a rien à corriger")

    for k in range(49):
        i = _declarer(registre, nom=f"ratee_{k}")
        registre.conclure(i, Resultat(300, 150, 92), p_permutation=0.6)

    corrigees = {e.id: p for e, p in registre.corriger()}
    assert corrigees[gagnante] == pytest.approx(0.1, abs=1e-9)
    assert corrigees[gagnante] > 0.05, (
        "corrigée par les 50 expériences réelles, elle ne passe plus")


def test_le_registre_survit_a_la_fermeture(tmp_path):
    """Durable, sinon le compte repart à zéro à chaque session — et un compte
    qui repart à zéro ne corrige rien."""
    chemin = tmp_path / "research.db"
    with Registre(chemin, campagne="essai") as r:
        i = _declarer(r)
        r.conclure(i, Resultat(100, 55, 92), p_permutation=0.04)

    with Registre(chemin, campagne="essai") as r:
        toutes = r.toutes()
        assert len(toutes) == 1
        assert toutes[0].resultat.gains == 55
        assert toutes[0].p == pytest.approx(0.04)
        assert toutes[0].conclue


def test_les_campagnes_ne_se_melangent_pas(tmp_path):
    """Corriger une campagne OTC par les expériences d'une campagne sur du
    réel gonflerait le compte sans raison."""
    chemin = tmp_path / "research.db"
    with Registre(chemin, campagne="otc") as r:
        _declarer(r, nom="a")
    with Registre(chemin, campagne="reel") as r:
        _declarer(r, nom="b")
        assert r.compte()["declarees"] == 1
    with Registre(chemin, campagne="otc") as r:
        assert r.compte()["declarees"] == 1


def test_une_campagne_sans_nom_est_refusee(tmp_path):
    with pytest.raises(BotError, match="sans nom"):
        Registre(tmp_path / "research.db", campagne="  ")
