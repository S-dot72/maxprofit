"""ZoneH1 plus confirmation M1 : la bougie de retournement avant l'entrée."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from maxprofit.core.types import Candle, Direction, Signal
from maxprofit.execution.journal import JournalExecution
from maxprofit.live.graphique import graphique_m1
from maxprofit.live.plan_demo import CoursePlanDemo, retournement_m1

from test_plan_demo import CourtierFactice, LecteurFactice, _plan

T0 = 1_790_000_000 // 60 * 60


def _b(i, o, c, haut=None, bas=None):
    return Candle(pair="EURUSD_otc", tf_sec=60, ts_sec=T0 + 60 * i, open=o,
                  high=haut if haut is not None else max(o, c),
                  low=bas if bas is not None else min(o, c), close=c,
                  tick_count=10, complete=True)


def _signal(i, call=True):
    return Signal(pair="EURUSD_otc",
                  direction=Direction.CALL if call else Direction.PUT,
                  decided_at_ms=(T0 + 60 * i + 60) * 1000, expiry_sec=900,
                  features={"niveau": 1.1000}, reason="zone")


def _vue(i):
    return SimpleNamespace(now_ms=(T0 + 60 * i + 60) * 1000)


@pytest.fixture
def course(tmp_path, monkeypatch):
    def fabriquer(mode):
        monkeypatch.setenv("CONFIRMATION_M1", mode)
        return CoursePlanDemo(LecteurFactice(), CourtierFactice([]),
                              JournalExecution(tmp_path / f"{mode}.db",
                                               campagne=mode),
                              _plan(), ("EURUSD_otc",))
    return fabriquer


def test_le_retournement_casse_l_extreme_de_la_precedente():
    prec = _b(0, 1.1005, 1.1000, haut=1.1006, bas=1.0999)
    assert retournement_m1(True, _b(1, 1.1000, 1.1008), prec)
    assert not retournement_m1(True, _b(1, 1.1000, 1.1004), prec), \
        "haussière mais sous le plus haut précédent"
    assert not retournement_m1(True, _b(1, 1.1009, 1.1007), prec), \
        "au-dessus mais baissière"
    assert retournement_m1(False, _b(1, 1.1000, 1.0995), prec)


def test_suivante_le_signal_attend_sa_bougie_puis_part(course):
    c = course("suivante")
    signal_ = _b(0, 1.1003, 1.1001, haut=1.1004, bas=1.0999)
    assert c._confirmer_en_m1("EURUSD_otc", _signal(0), [signal_], _vue(0)) \
        is None, "pas d'ordre sur la bougie du signal"
    confirmation = _b(1, 1.1001, 1.1007)
    joue = c._confirmer_en_m1("EURUSD_otc", None, [signal_, confirmation],
                              _vue(1))
    assert joue is not None and joue.direction is Direction.CALL
    assert joue.decided_at_ms == _vue(1).now_ms, "daté de la confirmation"
    assert c.etat.activite_depuis(48)["confirmes"] == 1


def test_suivante_sans_retournement_le_signal_est_abandonne(course):
    c = course("suivante")
    signal_ = _b(0, 1.1003, 1.1001, haut=1.1004, bas=1.0999)
    c._confirmer_en_m1("EURUSD_otc", _signal(0), [signal_], _vue(0))
    # Le cas AUD/CHF retourné : on voulait acheter, la bougie suivante baisse.
    suivante = _b(1, 1.1001, 1.0996)
    assert c._confirmer_en_m1("EURUSD_otc", None, [signal_, suivante],
                              _vue(1)) is None
    assert c._en_attente == {}


def test_suivante_une_bougie_manquante_annule_l_attente(course):
    c = course("suivante")
    signal_ = _b(0, 1.1003, 1.1001, haut=1.1004, bas=1.0999)
    c._confirmer_en_m1("EURUSD_otc", _signal(0), [signal_], _vue(0))
    trop_tard = _b(2, 1.1001, 1.1010)
    assert c._confirmer_en_m1("EURUSD_otc", None, [signal_, trop_tard],
                              _vue(2)) is None


def test_meme_bougie(course):
    c = course("meme")
    prec = _b(0, 1.1005, 1.1000, haut=1.1006, bas=1.0999)
    forte = _b(1, 1.1000, 1.1008)
    faible = _b(1, 1.1000, 1.1003)
    assert c._confirmer_en_m1("EURUSD_otc", _signal(1), [prec, forte],
                              _vue(1)) is not None
    assert c._confirmer_en_m1("EURUSD_otc", _signal(1), [prec, faible],
                              _vue(1)) is None


def test_sans_confirmation_rien_ne_change(course):
    c = course("0")
    s = _signal(0)
    assert c._confirmer_en_m1("EURUSD_otc", s, [_b(0, 1.1, 1.1)], _vue(0)) is s


def test_l_image_de_l_ordre_est_un_png():
    bougies = [_b(i, 1.1 + 0.0001 * (i % 7), 1.1 + 0.0001 * ((i + 3) % 7))
               for i in range(90)]
    png = graphique_m1(bougies, 1.1002, 0.0002, call=False)
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) > 1000


def test_l_image_part_avec_l_ordre(course):
    c = course("0")
    envoyees = []
    c._envoyer_image = lambda png, legende: envoyees.append(legende)
    c._bougies_du_signal = [_b(i, 1.1, 1.1001) for i in range(30)]
    c.chercher_un_signal = lambda: _signal(29)
    c.courtier.resultats = ["win"]
    c.tour()
    assert envoyees and "EURUSD_otc" in envoyees[0] and "zone 1.10000" \
        in envoyees[0]
    assert "<" not in envoyees[0].replace("<b>", "").replace("</b>", "")


def test_vagues_min_se_lit_dans_l_environnement(monkeypatch):
    from maxprofit.live.plan_demo import vagues_min_minutes
    monkeypatch.delenv("VAGUES_MIN", raising=False)
    assert vagues_min_minutes() == 0.0
    monkeypatch.setenv("VAGUES_MIN", "15")
    assert vagues_min_minutes() == 15.0
    monkeypatch.setenv("VAGUES_MIN", "n'importe quoi")
    assert vagues_min_minutes() == 0.0
