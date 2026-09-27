"""La réparation du schéma PostgreSQL après un réimport de dump.

Le 2026-09-27, la base réimportée portait le numéro de schéma 9 — donc « à
jour » — avec `candles.open` en `json` et plus aucune clé primaire. Chaque
écriture de bougies échouait, et les prix relus revenaient en texte.

Les tests de planification tournent partout. Le test de bout en bout exige un
vrai PostgreSQL : `MAXPROFIT_PG_TEST_URL=postgresql://...`.
"""

from __future__ import annotations

import os

import pytest

from maxprofit.store.reparation import SCHEMA_ATTENDU, planifier


def _base_saine():
    colonnes = {t: dict(c) for t, (c, _) in SCHEMA_ATTENDU.items()}
    uniques = {t: [frozenset(cle)] for t, (_, cle) in SCHEMA_ATTENDU.items()}
    return colonnes, uniques


def test_une_base_saine_ne_demande_AUCUNE_reparation():
    colonnes, uniques = _base_saine()
    assert planifier(colonnes, uniques, True) == []


def test_un_prix_en_JSON_est_reconverti_en_nombre():
    colonnes, uniques = _base_saine()
    colonnes["candles"]["open"] = "json"
    plan = planifier(colonnes, uniques, True)
    assert len(plan) == 1
    description, (sql,) = plan[0]
    assert "candles.open" in description
    assert "TYPE double precision" in sql and "#>> '{}'" in sql


def test_une_cle_primaire_PERDUE_est_retablie_apres_dedoublonnage():
    colonnes, uniques = _base_saine()
    uniques["candles"] = []
    (description, instructions), = planifier(colonnes, uniques, True)
    assert "clé primaire" in description
    assert instructions[-1].startswith("ALTER TABLE candles ADD PRIMARY KEY")
    assert any("a.ctid < b.ctid" in sql for sql in instructions)


def test_des_millisecondes_en_32_bits_sont_elargies_mais_pas_les_secondes():
    colonnes, uniques = _base_saine()
    colonnes["executions"]["clic_ts_ms"] = "integer"
    colonnes["candles"]["ts_sec"] = "integer"
    plan = planifier(colonnes, uniques, True)
    assert [d for d, _ in plan] == ["executions.clic_ts_ms : integer -> bigint"]


def test_la_numerotation_des_ordres_perdue_est_retablie():
    colonnes, uniques = _base_saine()
    (description, instructions), = planifier(colonnes, uniques, False)
    assert "executions.id" in description
    assert any("SET DEFAULT nextval" in sql for sql in instructions)


URL = os.environ.get("MAXPROFIT_PG_TEST_URL", "")


@pytest.mark.skipif(not URL, reason="MAXPROFIT_PG_TEST_URL non défini")
def test_de_bout_en_bout_sur_une_base_CASSEE_comme_en_production(
        monkeypatch, tmp_path):
    from maxprofit.core.types import Candle
    from maxprofit.store.db import open_read_write
    from maxprofit.store.market import MarketReader, MarketWriter

    monkeypatch.setenv("DATABASE_URL", URL)
    conn = open_read_write(tmp_path / "x")
    for table in SCHEMA_ATTENDU:
        conn.execute(f"TRUNCATE {table}")
    # L'état de la production : prix en json, clés primaires perdues,
    # doublons, numérotation des ordres perdue, solde du plan en json.
    for table in ("candles", "payouts", "executions", "plan_etat"):
        conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT {table}_pkey")
    for colonne in ("open", "high", "low", "close"):
        conn.execute(f'ALTER TABLE candles ALTER COLUMN "{colonne}" '
                     f'TYPE json USING to_json("{colonne}")')
    conn.execute("ALTER TABLE executions ALTER COLUMN id DROP DEFAULT")
    for _ in range(2):
        conn.execute(
            "INSERT INTO candles VALUES ('EURUSD_otc', 60, 1790000040, "
            "'1.5'::json, '\"1.75\"'::json, '1.25'::json, '1.5'::json, 12, 1)")

    rouverte = open_read_write(tmp_path / "x")

    MarketWriter(rouverte).upsert_candles([Candle(
        pair="EURUSD_otc", tf_sec=60, ts_sec=1790000100, open=1.5, high=1.6,
        low=1.4, close=1.55, tick_count=10, complete=True)])
    bougies = MarketReader(rouverte).candles(
        "EURUSD_otc", 60, 1789990000, 1790010000)
    assert [b.ts_sec for b in bougies] == [1790000040, 1790000100], "doublon retiré"
    assert bougies[0].high - bougies[0].low == pytest.approx(0.5)
    rouverte.execute(
        "INSERT INTO executions (campagne, pair, sens, mise, signal_ts_ms, "
        "clic_ts_ms, prix_attendu, payout_flux_pct, expiration_sec, accepte, "
        "brut) VALUES ('t', 'EURUSD_otc', 'call', 1.0, 1, 2, 1.1, 92, 900, 1, "
        "'{}')")
    from maxprofit.store.reparation import _lire_la_base, planifier as plan
    assert plan(*_lire_la_base(rouverte)) == [], "plus rien à réparer"


@pytest.mark.skipif(not URL, reason="MAXPROFIT_PG_TEST_URL non défini")
def test_un_compteur_d_ordres_EN_RETARD_est_recale(monkeypatch, tmp_path):
    """Production, 2026-09-28 : « duplicate key value violates unique
    constraint executions_pkey — Key (id)=(3) already exists ». Les lignes
    avaient survécu au réimport, la séquence était repartie de 1."""
    from maxprofit.store.db import open_read_write

    monkeypatch.setenv("DATABASE_URL", URL)
    conn = open_read_write(tmp_path / "x")
    conn.execute("TRUNCATE executions")
    insertion = (
        "INSERT INTO executions (id, campagne, pair, sens, mise, "
        "signal_ts_ms, clic_ts_ms, prix_attendu, payout_flux_pct, "
        "expiration_sec, accepte, brut) VALUES (?, 't', 'EURUSD_otc', "
        "'call', 1.0, 1, 2, 1.1, 92, 900, 1, '{}')")
    for i in (1, 2, 3):
        conn.execute(insertion, (i,))
    sequence = conn.execute(
        "SELECT pg_get_serial_sequence('executions', 'id')").fetchone()[0]
    conn.execute(f"SELECT setval('{sequence}', 1, false)")

    rouverte = open_read_write(tmp_path / "x")
    rouverte.execute(
        "INSERT INTO executions (campagne, pair, sens, mise, signal_ts_ms, "
        "clic_ts_ms, prix_attendu, payout_flux_pct, expiration_sec, accepte, "
        "brut) VALUES ('t', 'EURUSD_otc', 'call', 1.0, 1, 2, 1.1, 92, 900, 1, "
        "'{}')")
    assert rouverte.execute("SELECT MAX(id) FROM executions").fetchone()[0] == 4
    from maxprofit.store.reparation import recaler_la_numerotation
    assert recaler_la_numerotation(rouverte) is None, "rien de plus à faire"


@pytest.mark.skipif(not URL, reason="MAXPROFIT_PG_TEST_URL non défini")
def test_la_ligne_d_etat_broker_et_les_index_perdus_sont_recrees(
        monkeypatch, tmp_path):
    from maxprofit.store import etat_broker
    from maxprofit.store.db import open_read_write

    monkeypatch.setenv("DATABASE_URL", URL)
    conn = open_read_write(tmp_path / "x")
    conn.execute("DELETE FROM etat_broker")
    conn.execute("DROP INDEX IF EXISTS idx_payouts_pair")
    rouverte = open_read_write(tmp_path / "x")
    etat_broker.noter_echec(rouverte, "essai")
    assert etat_broker.lire(rouverte)[0] == 1, (
        "sans la ligne, l'échec n'était écrit nulle part")
    assert rouverte.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_payouts_pair'"
    ).fetchone() is not None
    etat_broker.noter_succes(rouverte)
