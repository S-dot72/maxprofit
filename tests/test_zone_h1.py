

def _bougie(ts, o, h, b, c, pair="X_otc"):
    from maxprofit.core.types import Candle
    return Candle(pair=pair, tf_sec=60, ts_sec=ts, open=o, high=h, low=b,
                  close=c, tick_count=30, complete=True)


def test_une_zone_DEJA_JOUEE_ne_donne_plus_de_signal():
    """La condition la plus discriminante mesuree, et elle vient de
    l'utilisateur : « zone intacte et jamais tradee », « une seule entree par
    zone ».

    Mesure : 312 signaux a 51,9 % sans filtre ; 114 a 60,5 % en limitant a deux
    occasions par zone, soit une esperance qui passe de -0,0031 a +0,1621 par
    dollar mise. Une zone s'epuise a etre testee.
    """
    from dataclasses import replace

    import pytest

    from maxprofit.core.errors import BotError
    from maxprofit.strategies.zone_h1 import PARAMETRES_PRE_INSCRITS

    p = PARAMETRES_PRE_INSCRITS
    assert p.entrees_max_par_zone == 3, (
        "trois : le debit fait partie du cahier des charges (18 sessions en "
        "12 h)")
    # Une limite de zero n'a pas de sens : aucune zone ne serait jouable.
    with pytest.raises(BotError, match="entrees_max_par_zone"):
        replace(p, entrees_max_par_zone=0)


def test_le_comptage_des_entrees_NE_MEMORISE_RIEN():
    """Il se recalcule depuis la fenetre, et c'est l'invariant n° 1.

    Compter les entrees passees demande de savoir ce qui s'est produit avant, et
    la tentation est de tenir un registre des zones jouees. Ce serait de l'etat :
    il vivrait dans la course et pas dans le backtest, et les deux divergeraient
    des le premier redemarrage.
    """
    from maxprofit.core.market_view import SequenceMarketView
    from maxprofit.strategies.zone_h1 import ZoneH1

    s = ZoneH1()
    # Deux evaluations de la MEME fenetre doivent rendre le meme verdict, meme
    # apres avoir evalue autre chose entre les deux.
    base = 1789999980
    fenetre = tuple(
        _bougie(base + k * 60, 1.0, 1.001, 0.999, 1.0) for k in range(301))
    autre = tuple(
        _bougie(base + k * 60, 1.5, 1.502, 1.498, 1.5) for k in range(301))
    premier = s.evaluer(SequenceMarketView("X_otc", fenetre))
    s.evaluer(SequenceMarketView("X_otc", autre))
    second = s.evaluer(SequenceMarketView("X_otc", fenetre))
    assert (premier.signal is None) == (second.signal is None)
    assert [c.validee for c in premier.conditions] == \
           [c.validee for c in second.conditions]


def test_la_condition_decrit_la_zone_RETENUE_pas_celles_ecartees():
    """Une zone plus ancienne peut etre refusee pour usure avant qu'on arrive a
    une zone jouable. Garder la trace de ce refus faisait rendre « condition
    echouee » avec un signal en main — ce que `Evaluation` refuse a juste titre,
    la porte ET et le signal devant venir du meme calcul.

    C'est l'invariant du paragraphe 3 qui a attrape l'incoherence, pas un test
    ecrit d'avance.
    """
    from maxprofit.core.types import Evaluation
    assert "conditions" in Evaluation.__dataclass_fields__
