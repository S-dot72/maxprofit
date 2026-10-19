"""Les filtres de qualité des signaux (`/filtres qualite`, 19/10)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from maxprofit.core.types import Candle, Direction, Signal
from maxprofit.execution.journal import JournalExecution
from maxprofit.hosting.pilotage import signature
from maxprofit.live import plan_demo
from maxprofit.live.configuration import (
    MOUVEMENT_HEURE_MAX, TAILLE_BOUGIE_MAX, Configuration)
from maxprofit.live.plan_demo import CoursePlanDemo
from maxprofit.live.preinscriptions import (
    EXCLUSION_B, EXCLUSION_HEURE, MODELES_R8)

from test_plan_demo import CourtierFactice, LecteurFactice, _plan

T0 = 1_790_000_000 // 60 * 60


def _bougies(n=30):
    return [Candle(pair="EURUSD_otc", tf_sec=60, ts_sec=T0 + 60 * i,
                   open=1.1, high=1.1004, low=1.0996, close=1.1001,
                   tick_count=10, complete=True) for i in range(n)]


@pytest.fixture
def course(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIRMATION_M1", "0")
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice([]),
                       JournalExecution(tmp_path / "q.db", campagne="q"),
                       _plan(), ("EURUSD_otc",))
    c.strategie = SimpleNamespace(on_bar=lambda vue: Signal(
        pair="EURUSD_otc", direction=Direction.CALL,
        decided_at_ms=vue.now_ms, expiry_sec=900,
        features={"niveau": 1.0998}, reason="zone"))
    monkeypatch.setattr(c, "_payout_au_maximum", lambda paire: True)
    return c


def test_la_course_ecarte_la_bougie_excessive_et_l_heure_consommee(
        course, monkeypatch):
    ctx = {"taille_bougie": 1.5, "mouvement_heure": -1.0}
    monkeypatch.setattr(plan_demo, "contexte_du_signal",
                        lambda *a, **k: dict(ctx))
    bougies = _bougies()
    # Sans filtres (le défaut), le signal part.
    assert course._evaluer_une_bougie("EURUSD_otc", bougies) is not None
    course.taille_bougie_max = TAILLE_BOUGIE_MAX
    course.mouvement_heure_max = MOUVEMENT_HEURE_MAX
    assert course._evaluer_une_bougie("EURUSD_otc", bougies) is None
    ctx.update(taille_bougie=1.0, mouvement_heure=0.4)
    assert course._evaluer_une_bougie("EURUSD_otc", bougies) is None
    ctx.update(mouvement_heure=-0.5)
    assert course._evaluer_une_bougie("EURUSD_otc", bougies) is not None
    activite = course.etat.activite_depuis(48)
    assert activite["bougie_excessive"] == 1
    assert activite["heure_consommee"] == 1


def test_le_reglage_filtres():
    cfg = Configuration()
    assert cfg.filtres == "aucun" and cfg.seuils_de_qualite() == (None, None)
    qualite = cfg.modifiee("filtres", "qualité")
    assert qualite.seuils_de_qualite() == (1.31, 0.0)
    assert qualite.modifiee("filtres", "aucun").filtres == "aucun"
    # Un changement de filtres change le réglage : /demarrer relance.
    assert signature(qualite) != signature(cfg)
    assert "Filtres de qualité : <b>actifs</b>" in qualite.resume()
    assert "/filtres qualite" in cfg.resume()
    # Une configuration enregistrée avant le 19/10 se relit sans filtres.
    assert Configuration.from_dict({"capital": 300}).filtres == "aucun"
    with pytest.raises(Exception):
        cfg.modifiee("filtres", "peut-être")


def test_le_rejeu_juge_les_memes_filtres():
    """Le modèle Q de /reference8 : exactement les seuils de la course."""
    q = next(m for m in MODELES_R8 if m[0] == "Q")
    assert q[2] == (EXCLUSION_B, EXCLUSION_HEURE)
    assert EXCLUSION_B[2] == TAILLE_BOUGIE_MAX
    assert EXCLUSION_HEURE[2] == MOUVEMENT_HEURE_MAX
