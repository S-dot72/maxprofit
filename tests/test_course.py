

def test_une_erreur_avalee_dit_OU_elle_s_est_produite():
    """Sans emplacement, une erreur avalee est indiagnosticable.

    Le superviseur attrape TOUT — regle assumee, parce que laisser remonter
    tuerait la collecte, qui vaut plus que la course. Mais `/etat` n'affichait
    que le type et le message, et « TypeError: unsupported operand type(s) for
    -: 'str' and 'str' » ne dit ni ou ni sur quoi. Il a fallu inspecter la
    base, les types rendus par le pilote et trois modules pour ne rien trouver,
    faute de savoir quelle ligne accusait.
    """
    from maxprofit.core.payout import payout_applique_pct
    from maxprofit.hosting.course import _ou
    try:
        payout_applique_pct("pas un nombre")
    except Exception as erreur:              # noqa: BLE001
        ou = _ou(erreur)
    assert "payout.py:" in ou, ou
    assert "payout_applique_pct()" in ou
    assert "maxprofit/maxprofit" not in ou, "le chemin ne doit pas etre double"


def test_le_cadre_le_plus_profond_du_PROJET_est_prefere():
    """Un cadre de bibliotheque designe presque toujours son appelant : c'est le
    code du projet qu'on veut voir accuse."""
    from maxprofit.core.payout import au_plafond
    from maxprofit.hosting.course import _ou
    try:
        sorted([1, "deux"], key=au_plafond)
    except Exception as erreur:              # noqa: BLE001
        ou = _ou(erreur)
    assert "maxprofit/" in ou, ou


def test_une_erreur_SANS_trace_ne_fait_pas_tomber_le_rapport():
    from maxprofit.hosting.course import _ou
    assert _ou(ValueError("nue")) == "origine inconnue"
