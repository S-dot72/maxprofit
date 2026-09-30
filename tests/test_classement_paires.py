"""Les N meilleures paires : classées sur le temps passé au payout maximum."""

from __future__ import annotations

import time

from maxprofit.collect.classement import (
    classer, completer, est_une_paire_flottante)
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


def test_sans_releves_rien_n_est_propose(tmp_path):
    from maxprofit.collect.collector import Config
    from maxprofit.hosting.service import proposer_les_paires

    conn = open_read_write(tmp_path / "vide.db")
    cfg = Config(db=tmp_path / "vide.db", min_payout=80, max_paires=4)
    assert "Aucun relevé" in proposer_les_paires(cfg, "", ouvrir=lambda: conn)


def test_le_classement_PROPOSE_une_liste_fixe_sans_rien_imposer(tmp_path):
    """Appliqué à chaque démarrage, le classement faisait entrer et sortir
    des paires à chaque déploiement : des séries fragmentées. Il propose
    désormais la chaîne à coller une fois dans PAIRES_FIXES."""
    from maxprofit.collect.collector import Config
    from maxprofit.hosting.service import proposer_les_paires

    parts = {f"{a}{b}_otc": 0.1 * i for i, (a, b) in enumerate(
        [("EUR", "JPY"), ("CAD", "JPY"), ("NZD", "USD"), ("GBP", "JPY"),
         ("EUR", "CHF")], start=1)}
    conn = _base(tmp_path, parts)
    cfg = Config(db=tmp_path / "m.db", min_payout=80, max_paires=4,
                 paires_fixes=TETE + ("CADJPY_otc", "AUDNZD_otc"))
    texte = proposer_les_paires(cfg, "7", ouvrir=lambda: conn)
    assert ("<code>" + ",".join(TETE + ("EURCHF_otc", "GBPJPY_otc",
                                         "NZDUSD_otc")) + "</code>") in texte
    assert "EURCHF 50 %  🆕" in texte
    assert "Sortiraient de la liste actuelle : CADJPY (20 %), AUDNZD (0 %)" \
        in texte
    assert cfg.paires_fixes == TETE + ("CADJPY_otc", "AUDNZD_otc"), (
        "rien n'est appliqué")


def test_le_demarrage_n_applique_plus_aucun_classement():
    import inspect

    from maxprofit.hosting import service
    assert "choisir_les_paires" not in inspect.getsource(service._servir)
