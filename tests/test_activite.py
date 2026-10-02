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
