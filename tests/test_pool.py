"""Le pool de bases : choisir l'active, tenir la relève à jour, basculer."""

import pytest

from maxprofit.store.pool import (
    ENV_RELEVE, EtatBase, bases_du_pool, choisir, choisir_au_demarrage,
    synchroniser)

U = "postgresql://u:p@{}.neon.tech/db"


# --- le choix ---------------------------------------------------------------

def test_le_pool_reunit_DATABASE_URL_et_ses_variantes_sans_doublon():
    env = {"DATABASE_URL": U.format("a"), "DATABASE_URL_AUTUMN": U.format("b"),
           "DATABASE_URL_COPIE": U.format("a"), "TURSO_DATABASE_URL": "libsql://x",
           ENV_RELEVE: U.format("c"), "AUTRE": U.format("d")}
    assert [n for n, _ in bases_du_pool(env)] == [
        "DATABASE_URL", "DATABASE_URL_AUTUMN"]


def test_l_active_est_la_joignable_la_PLUS_A_JOUR():
    """C'est là que la production écrivait. Une base bloquée est écartée."""
    etats = [EtatBase("A", "a", False, quota_epuise=True),
             EtatBase("B", "b", True, derniere_ts=100),
             EtatBase("C", "c", True, derniere_ts=300),
             EtatBase("D", "d", True, derniere_ts=None)]
    active, releve = choisir(etats)
    assert active.nom == "C"
    assert releve.nom == "B", "la relève : la plus à jour des autres"


def test_a_egalite_on_ne_bascule_pas_sans_raison():
    etats = [EtatBase("A", "a", True, derniere_ts=100),
             EtatBase("B", "b", True, derniere_ts=100)]
    assert choisir(etats, actuelle="b")[0].nom == "B"


def test_aucune_base_joignable_ne_touche_a_rien():
    env = {"DATABASE_URL": U.format("a"), "DATABASE_URL_B": U.format("b")}
    sonde = lambda n, u: EtatBase(n, u, False, quota_epuise=True)
    active, releve, _ = choisir_au_demarrage(env, sonde)
    assert active is None and env["DATABASE_URL"] == U.format("a")


def test_au_demarrage_la_base_EPUISEE_est_remplacee_par_la_releve():
    """Le scénario visé : la base active a atteint son quota, le service
    redémarre, et c'est la relève — la plus à jour des joignables — qui prend
    la place, sans qu'un humain change quoi que ce soit."""
    env = {"DATABASE_URL": U.format("epuisee"),
           "DATABASE_URL_AUTUMN": U.format("releve"),
           "DATABASE_URL_VIEILLE": U.format("vieille")}
    def sonde(nom, url):
        if "epuisee" in url:
            return EtatBase(nom, url, False, quota_epuise=True)
        return EtatBase(nom, url, True,
                        derniere_ts=900 if "releve" in url else 100)
    active, releve, _ = choisir_au_demarrage(env, sonde)
    assert env["DATABASE_URL"] == U.format("releve")
    assert env[ENV_RELEVE] == U.format("vieille")


def test_sans_pool_rien_ne_change():
    env = {"DATABASE_URL": U.format("a")}
    assert choisir_au_demarrage(env, sonde=lambda *a: 1 / 0)[0] is None
    assert env == {"DATABASE_URL": U.format("a")}


# --- la synchronisation -----------------------------------------------------

@pytest.fixture
def deux_bases(tmp_path, monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)
    from maxprofit.store.db import open_read_write
    a = open_read_write(tmp_path / "active.db")
    b = open_read_write(tmp_path / "releve.db")
    yield a, b
    a.close()
    b.close()


def _bougie(conn, ts, close=1.1, complete=1):
    conn.execute(
        "INSERT INTO candles (pair, tf_sec, ts_sec, open, high, low, close, "
        "tick_count, complete) VALUES ('EURUSD_otc', 60, ?, 1.1, 1.2, 1.0, ?, "
        "30, ?) ON CONFLICT (pair, tf_sec, ts_sec) DO UPDATE SET "
        "close = excluded.close, complete = excluded.complete",
        (ts, close, complete))


def _ordre(conn, ts_ms, resultat=None, profit=None):
    conn.execute(
        "INSERT INTO executions (campagne, pair, sens, mise, signal_ts_ms, "
        "prix_attendu, payout_flux_pct, expiration_sec, clic_ts_ms, accepte, "
        "resultat, profit, brut) VALUES ('c', 'EURUSD_otc', 'call', 1.5, ?, "
        "1.1, 92, 900, ?, 1, ?, ?, '{}')", (ts_ms, ts_ms, resultat, profit))


def _n(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_une_RELEVE_vide_recoit_tout(deux_bases):
    active, releve = deux_bases
    for k in range(5):
        _bougie(active, 1790000040 + 86400 * k)      # sur cinq jours
    synchroniser(active, releve, journal=lambda *_: None)
    assert _n(releve, "candles") == 5


def test_les_passes_suivantes_ne_versent_que_le_NOUVEAU(deux_bases):
    active, releve = deux_bases
    _bougie(active, 1790000040)
    synchroniser(active, releve, journal=lambda *_: None)
    _bougie(active, 1790000100)
    synchroniser(active, releve, journal=lambda *_: None)
    synchroniser(active, releve, journal=lambda *_: None)
    assert _n(releve, "candles") == 2, "sans doublon"


def test_une_bougie_COMPLETEE_apres_coup_est_mise_a_jour(deux_bases):
    """Une bougie s'écrit par morceaux pendant sa minute : la relève ne doit
    pas garder la version incomplète."""
    active, releve = deux_bases
    _bougie(active, 1790000040, close=1.10, complete=0)
    synchroniser(active, releve, journal=lambda *_: None)
    _bougie(active, 1790000040, close=1.15, complete=1)
    synchroniser(active, releve, journal=lambda *_: None)
    close, complete = releve.execute(
        "SELECT close, complete FROM candles").fetchone()
    assert (close, complete) == (pytest.approx(1.15), 1)


def test_un_ordre_DENOUE_apres_coup_est_mis_a_jour(deux_bases):
    """Un ordre est écrit avant son départ, puis complété à son dénouement :
    une relève qui ne recopierait que les nouveaux ordres garderait un ordre
    sans résultat — et le solde, qui se dérive du journal, serait faux."""
    active, releve = deux_bases
    _ordre(active, 1790000000000)
    synchroniser(active, releve, journal=lambda *_: None)
    active.execute("UPDATE executions SET resultat = 'win', profit = 1.38")
    synchroniser(active, releve, journal=lambda *_: None)
    assert _n(releve, "executions") == 1
    assert tuple(releve.execute(
        "SELECT resultat, profit FROM executions").fetchone()) == (
        "win", pytest.approx(1.38))


def test_la_configuration_de_campagne_suit(deux_bases):
    active, releve = deux_bases
    active.execute("INSERT INTO reglages (cle, valeur, maj_ts_sec) VALUES "
                   "('configuration', '{\"campagne\": \"x\"}', 10)")
    synchroniser(active, releve, journal=lambda *_: None)
    assert '"x"' in releve.execute("SELECT valeur FROM reglages").fetchone()[0]


# --- le signal de quota -----------------------------------------------------

def test_un_refus_pour_QUOTA_declenche_le_rappel(monkeypatch):
    from maxprofit.store import postgres
    vus = []
    monkeypatch.setattr(postgres, "QUAND_QUOTA_EPUISE",
                        lambda url, texte: vus.append(url))
    postgres._signaler_quota(Exception(
        "ERROR: Your account or project has exceeded the quota."), "u1")
    postgres._signaler_quota(Exception("connection refused"), "u2")
    assert vus == ["u1"], "seul le refus de quota déclenche la bascule"


# --- la rotation, vue du service ---------------------------------------------

from maxprofit.hosting.rotation import Rotation  # noqa: E402


def _rotation(releve_joignable=True, **kw):
    active = EtatBase("DATABASE_URL", U.format("active"), True, derniere_ts=9)
    releve = EtatBase("DATABASE_URL_B", U.format("releve"), True, derniere_ts=5)
    sonde = lambda n, u: EtatBase(n, u, releve_joignable)
    rot = Rotation(active, releve, sonde=sonde, **kw)
    rot.alertes, rot.arrets = [], []
    rot.alerter = rot.alertes.append
    rot.arreter = lambda: rot.arrets.append(1)
    return rot


def test_le_quota_de_l_ACTIVE_fait_basculer_UNE_fois():
    rot = _rotation()
    for _ in range(3):              # plusieurs fils voient le même refus
        rot.quota_epuise(rot.active.url, "exceeded the quota")
    assert rot.arrets == [1]
    assert len(rot.alertes) == 1 and "Bascule" in rot.alertes[0]


def test_sans_releve_JOIGNABLE_on_ne_s_arrete_pas():
    """Redémarrer retomberait sur la base bloquée, en boucle."""
    rot = _rotation(releve_joignable=False)
    rot.quota_epuise(rot.active.url, "exceeded the quota")
    assert rot.arrets == [] and "aucune base de relève" in rot.alertes[0]


def test_le_quota_de_la_RELEVE_alerte_sans_basculer():
    rot = _rotation()
    rot.quota_epuise(rot.releve.url, "exceeded the quota")
    rot.quota_epuise(rot.releve.url, "exceeded the quota")
    assert rot.arrets == [] and len(rot.alertes) == 1


def test_une_synchro_en_echec_ne_leve_pas_et_se_voit_dans_etat():
    def ouvrir(url, ecriture):
        raise RuntimeError("timeout")
    rot = _rotation(ouvrir=ouvrir)
    assert rot.synchroniser_une_fois() is False
    assert "synchro en échec" in rot.resume()


def test_une_synchro_reussie_se_date(deux_bases):
    a, b = deux_bases

    class _Sans:                    # le test garde la main sur la fermeture
        def __init__(self, c): self.c = c
        def __getattr__(self, n): return getattr(self.c, n)
        def close(self): pass
    t = [1000.0]
    rot = _rotation(ouvrir=lambda url, ecriture: _Sans(
        a if "active" in url else b), horloge=lambda: t[0])
    _bougie(a, 1790000040)
    assert rot.synchroniser_une_fois() is True
    t[0] += 600
    assert "synchro il y a 10 min" in rot.resume()
    assert _n(b, "candles") == 1


def test_au_demarrage_la_bascule_est_annoncee():
    env = {"DATABASE_URL": U.format("epuisee"),
           "DATABASE_URL_B": U.format("neuve"),
           "DATABASE_URL_C": U.format("vieille")}
    import maxprofit.store.pool as pool_mod
    def sonde(nom, url):
        if "epuisee" in url:
            return EtatBase(nom, url, False, quota_epuise=True)
        return EtatBase(nom, url, True, derniere_ts=9 if "neuve" in url else 1)
    ancien = pool_mod.sonder
    pool_mod.sonder = sonde
    try:
        rot = Rotation.au_demarrage(env)
    finally:
        pool_mod.sonder = ancien
    assert rot.active.hote == "neuve" and rot.releve.hote == "vieille"
    assert "epuisee → <b>neuve</b>" in rot.message_de_demarrage()
