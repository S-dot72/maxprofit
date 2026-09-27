"""Le rejeu du PLAN : chaque règle du direct, vérifiée une par une."""

from __future__ import annotations

from maxprofit.core.types import Candle
from maxprofit.research.rejeu import (
    FENETRE_SEC, IndexBougies, IndexPayouts, SignalRejoue, generer_signaux,
    rejouer)

T0 = 1_790_000_040          # aligné sur la minute


def _bougie(pair, ts, close):
    return Candle(pair=pair, tf_sec=60, ts_sec=ts, open=close, high=close,
                  low=close, close=close, tick_count=30, complete=True)


def _marche(prix_par_paire):
    """{paire: [prix minute par minute depuis T0]} -> bougies."""
    return {p: [_bougie(p, T0 + 60 * i, c) for i, c in enumerate(prix)]
            for p, prix in prix_par_paire.items()}


def _sig(pair, minute, call=True):
    ts = T0 + 60 * minute
    return SignalRejoue(fin_sec=ts + 60, pair=pair, call=call, ts_bougie=ts)


TOUJOURS_AU_MAX = IndexPayouts([(0, p, 84, True) for p in ("A", "B", "C")])


def test_un_signal_pendant_un_ordre_en_vol_est_PERDU():
    # A monte : le premier ordre (5 min) gagne ; B, une minute plus tard,
    # tombe pendant l'ordre.
    bougies = IndexBougies(_marche({"A": [1 + i for i in range(30)],
                                    "B": [1] * 30}))
    r = rejouer([_sig("A", 0), _sig("B", 1)], bougies, TOUJOURS_AU_MAX,
                echeance_sec=300)
    assert len(r.trades) == 1 and r.pendant_un_ordre == 1
    assert r.sessions == [(T0 + 60, "gagnee")]


def test_le_pas_2_exige_un_AUTRE_actif_et_quinze_minutes_apres_l_ENTREE():
    # A baisse : pas 1 (CALL) perdu. Un signal sur A (même actif) et un sur
    # B trop tôt sont sautés ; B à +15 min passe.
    bougies = IndexBougies(_marche({"A": [100 - i for i in range(60)],
                                    "B": [1 + i for i in range(60)]}))
    signaux = [_sig("A", 0), _sig("A", 6), _sig("B", 7), _sig("B", 15)]
    r = rejouer(signaux, bougies, TOUJOURS_AU_MAX, echeance_sec=300,
                independance_sec=900)
    assert [(p, k, res) for _, p, k, res in r.trades] == [
        ("A", 1, "loose"), ("B", 2, "win")]
    assert r.sautes_independance == 2


def test_trois_pertes_font_une_session_PERDUE():
    bougies = IndexBougies(_marche({p: [100 - i for i in range(90)]
                                    for p in ("A", "B", "C")}))
    signaux = [_sig("A", 0), _sig("B", 16), _sig("C", 32)]
    r = rejouer(signaux, bougies, TOUJOURS_AU_MAX, echeance_sec=300)
    assert r.sessions == [(T0 + 60, "perdue")]
    assert r.precision() == 0.0


def test_une_session_qui_attend_trop_est_INTERROMPUE():
    bougies = IndexBougies(_marche({"A": [100 - i for i in range(200)],
                                    "B": [1 + i for i in range(200)]}))
    r = rejouer([_sig("A", 0), _sig("B", 150)], bougies, TOUJOURS_AU_MAX,
                echeance_sec=300, attente_max_sec=7200)
    assert [i for _, i in r.sessions] == ["interrompue", "gagnee"]


def test_hors_payout_maximum_on_n_entre_pas():
    payouts = IndexPayouts([(0, "A", 80, True)])
    bougies = IndexBougies(_marche({"A": [1 + i for i in range(10)]}))
    r = rejouer([_sig("A", 0)], bougies, payouts, echeance_sec=60)
    assert not r.trades and r.hors_payout == 1


def test_le_payout_est_celui_EN_VIGUEUR_au_signal():
    payouts = IndexPayouts([(0, "A", 84, True), (T0 + 120, "A", 70, True)])
    assert payouts.au_maximum("A", T0 + 60)
    assert not payouts.au_maximum("A", T0 + 180)
    assert not payouts.au_maximum("A", 0 - 1)


def test_une_egalite_compte_comme_un_pas_perdu():
    bougies = IndexBougies(_marche({"A": [1] * 10, "B": [1 + i for i in
                                                          range(40)]}))
    r = rejouer([_sig("A", 0), _sig("B", 15)], bougies, TOUJOURS_AU_MAX,
                echeance_sec=60)
    assert [k for _, _, k, _ in r.trades] == [1, 2]


def test_les_sessions_se_comptent_par_fenetre_de_douze_heures():
    bougies = IndexBougies(_marche({"A": [1 + i for i in range(1500)]}))
    signaux = [_sig("A", m) for m in (0, 10, 800)]
    r = rejouer(signaux, bougies, TOUJOURS_AU_MAX, echeance_sec=60)
    assert r.par_fenetre(T0, T0 + 2 * FENETRE_SEC) == [2, 1]
    assert r.par_fenetre(T0, T0 + 2 * FENETRE_SEC, {1}) == [1]


def test_les_signaux_sont_ceux_de_la_strategie_sur_la_fenetre_du_direct():
    """La stratégie reçoit les `lookback` dernières minutes, comme en direct,
    et la garde de calibration peut écarter une paire."""
    vues = []

    class Espion:
        p = type("P", (), {"fenetre_pique": 1, "lookback": 5})()

        def on_bar(self, vue):
            vues.append(len(vue.candles(1000)))
            return None

    marche = _marche({"A": [1.0] * 20, "B": [1.0] * 20})
    generer_signaux(marche, Espion(), lambda pair, f: pair == "A")
    assert max(vues) == 6, "fenêtre [t - lookback min, t], bornes comprises"
    assert len(vues) == 20 - 3, "B écartée, et 4 bougies au minimum"


def _figure_zone_h1():
    """Un creux à 1,1000 (bougie rouge puis verte), une première touche à la
    minute 200, une seconde à la 299 en tendance H1 haussière : la figure
    exacte de ZoneH1. Puis le prix monte : un CALL gagne."""
    def c(i, o, cl):
        return Candle(pair="EURUSD_otc", tf_sec=60, ts_sec=T0 + 60 * i,
                      open=o, high=max(o, cl) + 0.00005,
                      low=min(o, cl) - 0.00005, close=cl, tick_count=30,
                      complete=True)
    niveau = lambda i: 1.1010 + 0.000001 * i          # noqa: E731
    bougies = []
    for i in range(330):
        if i == 100:
            bougies.append(c(i, 1.1010, 1.1000))
        elif i == 101:
            bougies.append(c(i, 1.1001, 1.1010))
        elif i in (200, 299):
            bougies.append(c(i, niveau(i), 1.1002))
        elif i > 299:
            bougies.append(c(i, 1.1002 + 0.0001 * (i - 300),
                             1.1003 + 0.0001 * (i - 300)))
        else:
            bougies.append(c(i, niveau(i), niveau(i)))
    return bougies


def test_la_VRAIE_strategie_donne_le_signal_du_direct_et_il_se_resout():
    from types import SimpleNamespace

    from maxprofit.live.plan_demo import CoursePlanDemo
    from maxprofit.strategies.zone_h1 import ZoneH1

    strategie = ZoneH1()
    garde = SimpleNamespace(strategie=strategie)
    marche = {"EURUSD_otc": _figure_zone_h1()}
    signaux = generer_signaux(
        marche, strategie,
        lambda p, f: CoursePlanDemo._dans_sa_plage_de_calibration(garde, p, f))
    # 299, puis 300 : la bougie suivante touche encore la zone.
    assert [(s.ts_bougie - T0) // 60 for s in signaux] == [299, 300]
    assert all(s.call for s in signaux)
    r = rejouer(signaux, IndexBougies(marche),
                IndexPayouts([(0, "EURUSD_otc", 84, True)]), echeance_sec=300)
    assert [res for _, _, _, res in r.trades] == ["win"]
    assert r.pendant_un_ordre == 1, "le second tombe pendant l'ordre en vol"
