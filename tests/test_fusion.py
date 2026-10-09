"""La fusion d'une base dans une autre qui a déjà des données.

Deux bases SQLite jouent la source et la cible : le code est le même pour
PostgreSQL, seule l'introspection change de dialecte.
"""

import pytest

from maxprofit.store.fusion import fusionner


@pytest.fixture
def deux_bases(tmp_path, monkeypatch):
    # Sans cela `open_read_write` ouvrirait la base PostgreSQL du `.env` au lieu
    # des fichiers du test — et écrirait dedans.
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)
    from maxprofit.store.db import open_read_write
    source = open_read_write(tmp_path / "source.db")
    cible = open_read_write(tmp_path / "cible.db")
    yield source, cible
    source.close()
    cible.close()


def _bougie(conn, pair, ts, close=1.1):
    conn.execute(
        "INSERT INTO candles (pair, tf_sec, ts_sec, open, high, low, close, "
        "tick_count, complete) VALUES (?, 60, ?, 1.1, 1.2, 1.0, ?, 30, 1)",
        (pair, ts, close))


def _ordre(conn, campagne, pair, ts_ms, resultat="win"):
    conn.execute(
        "INSERT INTO executions (campagne, pair, sens, mise, signal_ts_ms, "
        "prix_attendu, payout_flux_pct, expiration_sec, clic_ts_ms, accepte, "
        "resultat, profit, brut) VALUES (?, ?, 'call', 1.5, ?, 1.1, 92, 900, "
        "?, 1, ?, 1.38, '{}')", (campagne, pair, ts_ms, ts_ms, resultat))


def _compte(conn, table, where="1=1", params=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}",
                        params).fetchone()[0]


def test_a_BLANC_rien_n_est_ecrit(deux_bases):
    source, cible = deux_bases
    _bougie(source, "EURUSD_otc", 1790000040)
    bilans = fusionner(source, cible, appliquer=False, journal=lambda *_: None)
    b = {x.table: x for x in bilans}
    assert b["candles"].a_inserer == 1
    assert _compte(cible, "candles") == 0, "un essai à blanc n'écrit rien"


def test_les_donnees_de_la_cible_sont_GARDEES(deux_bases):
    """La cible a déjà un historique : on ajoute, on n'écrase pas."""
    source, cible = deux_bases
    _bougie(cible, "EURUSD_otc", 1790000040, close=1.111)      # déjà là
    _bougie(source, "EURUSD_otc", 1790000040, close=9.999)     # même clé
    _bougie(source, "EURUSD_otc", 1790000100)                  # nouvelle
    fusionner(source, cible, appliquer=True, journal=lambda *_: None)
    assert _compte(cible, "candles") == 2
    close = cible.execute(
        "SELECT close FROM candles WHERE ts_sec = 1790000040").fetchone()[0]
    assert close == pytest.approx(1.111), "la ligne de la cible est intacte"


def test_relancer_la_fusion_ne_DUPLIQUE_rien(deux_bases):
    source, cible = deux_bases
    _bougie(source, "EURUSD_otc", 1790000040)
    _ordre(source, "plan-demo-v2", "EURUSD_otc", 1790000040000)
    for _ in range(3):
        fusionner(source, cible, appliquer=True, journal=lambda *_: None)
    assert _compte(cible, "candles") == 1
    assert _compte(cible, "executions") == 1


def test_les_ordres_sont_dedoublonnes_sur_ce_qui_les_IDENTIFIE(deux_bases):
    """Les deux bases ont chacune leur « ordre n°1 », et ce ne sont pas les
    mêmes. L'`id` ne doit ni être copié, ni servir de clé."""
    source, cible = deux_bases
    _ordre(cible, "plan-demo-v1", "AUDUSD_otc", 1789000000000)   # id 1 cible
    _ordre(source, "plan-demo-v2", "EURUSD_otc", 1790000000000)  # id 1 source
    _ordre(source, "plan-demo-v1", "AUDUSD_otc", 1789000000000)  # déjà là
    bilans = fusionner(source, cible, appliquer=True, journal=lambda *_: None)
    b = {x.table: x for x in bilans}["executions"]
    assert (b.source, b.deja, b.a_inserer) == (2, 1, 1)
    assert _compte(cible, "executions") == 2
    assert _compte(cible, "executions", "campagne = ?", ("plan-demo-v2",)) == 1


def test_l_etat_de_campagne_le_PLUS_RECENT_l_emporte(deux_bases):
    """C'est l'état d'une campagne : le plus récent est le vrai."""
    from maxprofit.live.plan_demo import sauver_etat  # noqa: F401
    source, cible = deux_bases
    for conn, maj, solde in ((cible, 100, 250.0), (source, 200, 261.81)):
        conn.execute(
            "INSERT INTO plan_etat (campagne, maj_ts_sec, jour, solde, "
            "solde_ouverture, sessions_jouees, sessions_perdues_daffilee, "
            "jour_utc, reancrages, derniere_bougie, session_pas_joues, "
            "session_engagees, session_gain_vise) VALUES "
            "('plan-demo-v2', ?, 2, ?, 250, 0, 0, 0, '[]', '{}', 0, '[]', 0)",
            (maj, solde))
    bilans = fusionner(source, cible, appliquer=True, journal=lambda *_: None)
    assert {x.table: x for x in bilans}["plan_etat"].a_remplacer == 1
    solde = cible.execute("SELECT solde FROM plan_etat").fetchone()[0]
    assert solde == pytest.approx(261.81)


def test_un_etat_PLUS_ANCIEN_ne_remplace_pas_le_recent(deux_bases):
    source, cible = deux_bases
    for conn, maj, solde in ((cible, 300, 270.0), (source, 200, 261.81)):
        conn.execute(
            "INSERT INTO plan_etat (campagne, maj_ts_sec, jour, solde, "
            "solde_ouverture, sessions_jouees, sessions_perdues_daffilee, "
            "jour_utc, reancrages, derniere_bougie, session_pas_joues, "
            "session_engagees, session_gain_vise) VALUES "
            "('plan-demo-v2', ?, 2, ?, 250, 0, 0, 0, '[]', '{}', 0, '[]', 0)",
            (maj, solde))
    fusionner(source, cible, appliquer=True, journal=lambda *_: None)
    solde = cible.execute("SELECT solde FROM plan_etat").fetchone()[0]
    assert solde == pytest.approx(270.0)


def test_l_etat_du_broker_n_est_JAMAIS_recopie(deux_bases):
    """La pénalité du broker d'une base ne concerne pas l'autre : la recopier
    ferait attendre un service sans raison."""
    source, cible = deux_bases
    bilans = fusionner(source, cible, appliquer=False, journal=lambda *_: None)
    assert "etat_broker" not in {x.table for x in bilans}
    assert "_schema_version" not in {x.table for x in bilans}
