"""Le relevé heure par heure, et l'alerte quand aucun ordre ne part."""

from __future__ import annotations

import time

from maxprofit.execution.journal import JournalExecution
from maxprofit.live.plan_demo import CoursePlanDemo, Etat, texte_activite

from test_plan_demo import CourtierFactice, LecteurFactice, _plan


def test_les_compteurs_sont_ranges_par_heure_et_cumules():
    e = Etat(plan=_plan(), solde=250.0)
    maintenant = int(time.time())
    e.noter("vues", 5, ts_sec=maintenant)
    e.noter("vues", 3, ts_sec=maintenant - 3600)
    e.noter("contre_heure", 1, ts_sec=maintenant - 3600)
    e.noter("vues", 100, ts_sec=maintenant - 20 * 3600)
    e.noter_plafond(7)
    e.noter_plafond(4)
    a = e.activite_depuis(12)
    assert a["vues"] == 8 and a["contre_heure"] == 1
    assert (a["plafond_min"], a["plafond_max"]) == (4, 7)
    texte = texte_activite(a, 12)
    assert "actifs au plafond 4 à 7" in texte and "8 bougies vues" in texte
    assert "<" not in texte


def test_les_vieilles_heures_sont_oubliees():
    e = Etat(plan=_plan(), solde=250.0)
    e.noter("vues", ts_sec=int(time.time()) - 60 * 3600)
    e.noter("vues")
    assert len(e.activite) == 1


def test_un_silence_de_trois_heures_est_signale_une_seule_fois(tmp_path):
    messages = []
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice([]),
                       JournalExecution(tmp_path / "j.db", campagne="s"),
                       _plan(), ("EURUSD_otc",), alerter=messages.append)
    c.chercher_un_signal = lambda: None
    c.etat.dernier_trade = ("EURUSD_otc", int(time.time()) - 4 * 3600)
    c.etat.noter("vues", 42)
    c.tour()
    c.tour()
    silences = [m for m in messages if "Aucun ordre" in m]
    assert len(silences) == 1 and "42 bougies vues" in silences[0]


def test_pas_d_alerte_quand_un_ordre_est_recent(tmp_path):
    messages = []
    c = CoursePlanDemo(LecteurFactice(), CourtierFactice([]),
                       JournalExecution(tmp_path / "j.db", campagne="r"),
                       _plan(), ("EURUSD_otc",), alerter=messages.append)
    c.chercher_un_signal = lambda: None
    c.etat.dernier_trade = ("EURUSD_otc", int(time.time()) - 600)
    c.tour()
    assert not [m for m in messages if "Aucun ordre" in m]


def test_le_releve_survit_au_redeploiement(tmp_path):
    from maxprofit.live.plan_demo import charger_etat, sauver_etat
    from maxprofit.store.db import open_read_write
    e = Etat(plan=_plan(), solde=250.0)
    e.ouvrir_la_journee()
    e.noter("vues", 7)
    conn = open_read_write(tmp_path / "e.db")
    sauver_etat(conn, "x", e, 1)
    relu, _ = charger_etat(conn, "x", e.plan)
    assert relu.activite_depuis(1)["vues"] == 7


def _course_lente(tmp_path, ecart_sec):
    """Une paire dont la dernière bougie évaluée date de `ecart_sec`."""
    from test_plan_demo import LecteurAvecBougies
    frais = int(time.time()) // 60 * 60 - 60
    c = CoursePlanDemo(LecteurAvecBougies(fin_ts=frais), CourtierFactice([]),
                       JournalExecution(tmp_path / f"l{ecart_sec}.db",
                                        campagne=f"l{ecart_sec}"),
                       _plan(), ("EURUSD_otc",))
    c.univers = lambda: ["EURUSD_otc"]
    vues = []
    c.strategie.on_bar = lambda vue: vues.append(vue.now_ms) or None
    c.etat.derniere_bougie["EURUSD_otc"] = frais - ecart_sec
    return c, vues


def test_une_bougie_encore_fraiche_est_rattrapee(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIRMATION_M1", "0")
    # Selon la seconde de la minute, la bougie sautée a entre 60 et 120 s :
    # la fraîcheur est fixée pour que le test ne dépende pas de l'horloge.
    monkeypatch.setattr("maxprofit.live.plan_demo.FRAICHEUR_MAX_SEC", 150)
    c, vues = _course_lente(tmp_path, 120)
    c.chercher_un_signal()
    assert len(vues) == 2, "la bougie sautée et la dernière"


def test_une_bougie_trop_vieille_est_comptee_manquee(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIRMATION_M1", "0")
    c, vues = _course_lente(tmp_path, 300)
    c.chercher_un_signal()
    a = c.etat.activite_depuis(1)
    assert a.get("manquees", 0) >= 2
    assert "MANQUÉES" in texte_activite(a, 1)
