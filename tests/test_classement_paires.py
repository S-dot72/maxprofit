"""Les N meilleures paires : classées sur le temps passé au payout maximum."""

from __future__ import annotations

import time

from maxprofit.collect.classement import (
    classer, completer, est_une_paire_flottante, les_meilleures)
from maxprofit.store.db import open_read_write

TETE = ("EURUSD_otc", "AUDUSD_otc", "GBPAUD_otc", "AUDCAD_otc")


def test_seules_les_paires_de_devises_flottantes_concourent():
    assert est_une_paire_flottante("EURJPY_otc")
    assert est_une_paire_flottante("NZDCHF_otc")
    for nom in ("BTCUSD_otc", "AEDCNY_otc", "XAUUSD_otc", "EURUSD",
                "AAPL_otc", "USDUSD_otc", "USDTRY_otc"):
        assert not est_une_paire_flottante(nom), nom


def _base(tmp_path, parts: dict[str, float], releves=100):
    conn = open_read_write(tmp_path / "m.db")
    t0 = int(time.time()) - 3600
    for pair, part in parts.items():
        au_max = int(part * releves)
        for i in range(releves):
            conn.execute("INSERT INTO payouts VALUES (?, ?, ?, 1)",
                         (t0 + i, pair, 92 if i < au_max else 70))
    conn.commit()
    return conn


def test_le_classement_suit_le_temps_passe_au_payout_maximum(tmp_path):
    conn = _base(tmp_path, {"EURJPY_otc": 0.8, "CADJPY_otc": 0.3,
                            "BTCUSD_otc": 1.0, "NZDUSD_otc": 0.5})
    classement = classer(conn, int(time.time()) - 86400)
    assert [p for p, _, _ in classement] == [
        "EURJPY_otc", "NZDUSD_otc", "CADJPY_otc"], "la crypto est écartée"
    conn.close()


def test_la_tete_reste_en_tete_et_l_on_complete_jusqu_au_total():
    classement = [("EURJPY_otc", 0.8, 100), ("EURUSD_otc", 0.7, 100),
                  ("CADJPY_otc", 0.3, 100), ("NZDUSD_otc", 0.0, 100)]
    assert completer(TETE, classement, 6) == TETE + ("EURJPY_otc",
                                                      "CADJPY_otc")
    assert completer(TETE, classement, 20)[-1] == "CADJPY_otc", (
        "une paire JAMAIS au plafond ne rapporterait rien")


def test_sans_releves_on_ne_collecte_pas_moins(tmp_path):
    conn = open_read_write(tmp_path / "vide.db")
    assert les_meilleures(conn, TETE, 20) is None
    conn.close()


def test_le_service_retient_les_meilleures_et_garde_la_tete(tmp_path,
                                                             monkeypatch):
    from maxprofit.collect.collector import Config
    from maxprofit.hosting.service import choisir_les_paires

    parts = {f"{a}{b}_otc": 0.1 * i for i, (a, b) in enumerate(
        [("EUR", "JPY"), ("CAD", "JPY"), ("NZD", "USD"), ("GBP", "JPY"),
         ("EUR", "CHF")], start=1)}
    conn = _base(tmp_path, parts)
    cfg = Config(db=tmp_path / "m.db", min_payout=80, max_paires=4,
                 paires_fixes=("AUDNZD_otc",))
    monkeypatch.setenv("PAIRES_TOTAL", "7")
    choisi = choisir_les_paires(cfg, ouvrir=lambda: conn)
    assert choisi.paires_fixes[:4] == TETE
    assert choisi.paires_fixes[4:] == ("EURCHF_otc", "GBPJPY_otc",
                                       "NZDUSD_otc")
    assert choisi.paires_socle >= 4
    monkeypatch.setenv("PAIRES_TOTAL", "0")
    assert choisir_les_paires(cfg, ouvrir=lambda: conn) is cfg
