"""La connexion du courtier : la cascade de points d'accès.

Ce fichier n'existait pas. Il naît d'un défaut précis : la cascade qui essaie
les deux adresses démo du broker avait été posée dans `PocketOptionSource`, qui
sert à COLLECTER, et le courtier de la course a son propre code de connexion.
Corriger un chemin sur deux ne corrige rien — la collecte repartait et la course
restait au sol, sur « timed out during opening handshake ».
"""

import sys
import types

import pytest

from maxprofit.collect.pocketoption import SourceIndisponible


class _ClientFactice:
    """Le client de la bibliothèque, réduit à ce que `connecter` lui demande.

    `region_qui_ouvre` décide laquelle des adresses répond : c'est tout le sujet
    du test, et le faire dépendre de la région plutôt que de l'ordre d'appel
    évite un test qui passerait même si la cascade ne changeait pas d'adresse.
    """

    region_qui_ouvre = "B"
    tentatives: list[str] = []

    def __init__(self, demo, ssid):
        self.demo, self.ssid = demo, ssid
        self.fermetures = 0

    def connect(self):
        _ClientFactice.tentatives.append(_region_courante["nom"])

    def close(self):
        self.fermetures += 1

    def check_connect(self):
        return _region_courante["nom"] == _ClientFactice.region_qui_ouvre

    def get_balance(self):
        return 250.0

    def change_symbol(self, pair, periode):
        pass

    def GetTicks(self, pair):           # noqa: N802 — nom de la bibliothèque
        return [{"price": 1.2345}]


_region_courante: dict[str, str | None] = {"nom": None}

#: Un jeton bien FORMÉ. `connecter()` vérifie sa forme avant d'ouvrir quoi que
#: ce soit — un jeton tronqué ne doit pas donner lieu à une connexion qui aura
#: l'air de marcher — donc un faux jeton ferait échouer ces tests pour une
#: raison qui n'a rien à voir avec les points d'accès.
SSID_VALIDE = (
    '42["auth",{"session":"a3f9c1e7b5d2486a0c7f19e3b8d4a6521f0e9c7d","isDemo":1,"uid":122847706,'
    '"platform":2,"isFastHistory":true}]')


@pytest.fixture
def courtier_factice(monkeypatch):
    """Installe une fausse bibliothèque et rend la classe du courtier."""
    from maxprofit.execution import courtier as mod

    _ClientFactice.tentatives = []
    _region_courante["nom"] = None

    # `balance` non nul : le solde est le SEUL signal d'authentification côté
    # broker, et le courtier refuse de continuer sans lui. Le laisser à None
    # ferait échouer ces tests sur l'authentification au lieu des points
    # d'accès — pour une raison juste, mais pas celle qu'on mesure.
    faux_globals = types.SimpleNamespace(
        check_websocket_if_error=False, websocket_error_reason="",
        balance=250.0, balance_updated=True, pairs={}, order_data={})
    stable = types.ModuleType("pocketoptionapi.stable_api")
    stable.PocketOption = _ClientFactice
    paquet = types.ModuleType("pocketoptionapi")
    paquet.global_value = faux_globals
    paquet.stable_api = stable
    monkeypatch.setitem(sys.modules, "pocketoptionapi", paquet)
    monkeypatch.setitem(sys.modules, "pocketoptionapi.stable_api", stable)

    monkeypatch.setattr(mod, "points_d_acces", lambda demo: ("A", "B"))
    monkeypatch.setattr(
        mod, "_forcer_region",
        lambda demo, nom=None: _region_courante.__setitem__("nom", nom))
    monkeypatch.setattr(mod, "installer_boucle_asyncio", lambda b=None: b)
    monkeypatch.setattr(mod, "DELAI_CONNEXION_SEC", 0.3)
    monkeypatch.setattr(mod, "exiger_un_compte_demo", lambda *a, **k: None)
    return mod


def test_le_courtier_BASCULE_sur_la_seconde_adresse(courtier_factice):
    """La première adresse ne s'ouvre pas ; la connexion doit aboutir quand
    même. Sans la cascade, `connecter()` levait ici et la course abandonnait
    après cinq essais identiques."""
    mod = courtier_factice
    _ClientFactice.region_qui_ouvre = "B"
    c = mod.CourtierDemo(ssid=SSID_VALIDE, plafonds=None)
    c.connecter()
    assert _ClientFactice.tentatives == ["A", "B"], (
        "les deux adresses doivent être essayées, dans l'ordre")
    assert _region_courante["nom"] == "B"


def test_la_PREMIERE_adresse_suffit_quand_elle_repond(courtier_factice):
    """On ne se disperse pas : une adresse qui répond arrête la cascade."""
    _ClientFactice.region_qui_ouvre = "A"
    c = courtier_factice.CourtierDemo(ssid=SSID_VALIDE, plafonds=None)
    c.connecter()
    assert _ClientFactice.tentatives == ["A"]


def test_aucune_adresse_qui_repond_leve_en_les_NOMMANT(courtier_factice):
    """« Aucun point d'accès n'a répondu » sans dire lesquels obligerait à
    relire le code pour savoir ce qui a été tenté."""
    _ClientFactice.region_qui_ouvre = "AUCUNE"
    c = courtier_factice.CourtierDemo(ssid=SSID_VALIDE, plafonds=None)
    with pytest.raises(SourceIndisponible) as leve:
        c.connecter()
    message = str(leve.value)
    assert "A" in message and "B" in message
    assert _ClientFactice.tentatives == ["A", "B"]


def test_une_adresse_qui_ouvre_SANS_authentifier_est_un_echec_d_adresse(
        courtier_factice, monkeypatch):
    """Les deux adresses demo ne sont pas equivalentes.

    `try-demo-eu` ouvre le socket et sert le catalogue des actifs — qui est
    public — sans authentifier la session. La cascade basculait dessus quand
    `demo-api-eu` expirait depuis l'hebergeur, puis levait `SessionExpiree` en
    accusant le jeton — lequel authentifiait parfaitement depuis un poste
    local, solde recu a l'appui.

    J'avais place la verification d'authentification HORS de la boucle, en
    raisonnant qu'« un jeton refuse ne se repare pas en changeant d'adresse ».
    Vrai pour un jeton perime. Faux pour une adresse qui n'authentifie pas.
    """
    mod = courtier_factice
    _ClientFactice.region_qui_ouvre = "TOUTES"
    monkeypatch.setattr(_ClientFactice, "check_connect", lambda self: True)

    # Seule la SECONDE adresse donne un solde.
    faux = sys.modules["pocketoptionapi"].global_value
    faux.balance = None
    faux.balance_updated = False

    vraie = mod.CourtierDemo._attendre_le_socket

    def socket_puis_solde(self):
        vraie(self)
        if _region_courante["nom"] == "B":
            faux.balance = 250.0

    monkeypatch.setattr(mod.CourtierDemo, "_attendre_le_socket",
                        socket_puis_solde)
    c = mod.CourtierDemo(ssid=SSID_VALIDE, plafonds=None)
    c.connecter()
    assert _ClientFactice.tentatives == ["A", "B"], (
        "A ouvre mais n'authentifie pas : il faut passer a B")


def test_aucune_adresse_authentifiante_accuse_le_JETON(courtier_factice,
                                                       monkeypatch):
    """Quand toutes les adresses ouvrent sans authentifier, c'est le jeton
    qu'il faut recapturer — et le message doit le dire, pas parler de reseau."""
    from maxprofit.collect.pocketoption import SessionExpiree

    mod = courtier_factice
    monkeypatch.setattr(_ClientFactice, "check_connect", lambda self: True)
    faux = sys.modules["pocketoptionapi"].global_value
    faux.balance = None
    faux.balance_updated = False

    c = mod.CourtierDemo(ssid=SSID_VALIDE, plafonds=None)
    with pytest.raises(SessionExpiree) as leve:
        c.connecter()
    message = str(leve.value)
    assert "recapturez le jeton" in message.lower()
    assert "A" in message and "B" in message


def test_une_adresse_qui_ne_repond_pas_est_ABANDONNEE(courtier_factice,
                                                      monkeypatch):
    """`PocketOption` n'a pas de `close()` : l'appel levait, l'erreur etait
    avalee, et le client rate continuait de rappeler le broker a vie."""
    from maxprofit.collect.pocketoption import SessionExpiree

    clients = []
    vrai_init = _ClientFactice.__init__

    def init_avec_api(self, demo, ssid):
        vrai_init(self, demo, ssid)
        self.api = types.SimpleNamespace(websocket_client=types.SimpleNamespace())
        clients.append(self)

    monkeypatch.setattr(_ClientFactice, "__init__", init_avec_api)
    _ClientFactice.region_qui_ouvre = "AUCUNE"
    c = courtier_factice.CourtierDemo(ssid=SSID_VALIDE, plafonds=None)
    with pytest.raises(SessionExpiree):
        c.connecter()
    assert len(clients) == 2
    assert all(k.api.websocket_client._maxprofit_abandon.is_set()
               for k in clients), "chaque client rate doit etre arrete"


@pytest.mark.parametrize("profit", [1.46, 3.05])
def test_un_ordre_d_une_AUTRE_instance_se_denoue_par_l_historique(profit):
    """`check_win` ne connaît que les ordres de son propre client : pour
    celui de l'ancienne instance, il rend « unknown » sur-le-champ, et un
    ordre GAGNÉ aurait été compté pour sa mise perdue."""
    from types import SimpleNamespace

    from maxprofit.execution.journal import Execution
    from maxprofit.execution.courtier import CourtierDemo

    deal = {"id": "X1", "asset": "EURUSD_otc", "amount": 1.59, "command": 0,
            "openTimestamp": 1000.0, "closeTimestamp": 1900.0,
            "openPrice": 1.1, "closePrice": 1.2, "profit": profit,
            "percentProfit": 92}

    class _Client:
        api = SimpleNamespace(GetClosedDeals=lambda: [])

        def check_win(self, order_id):
            return None, "unknown"

        def get_async_order(self, order_id):
            raise KeyError("deals")

    c = CourtierDemo(ssid=SSID_VALIDE, plafonds=None)
    c._client = _Client()
    c._globals = SimpleNamespace(closed_orders=[{"deals": [deal]}])
    c._attendre_l_echeance = lambda e: None
    ex = Execution(pair="EURUSD_otc", sens="call", mise=1.59, signal_ts_ms=1,
                   prix_attendu=1.1, payout_flux_pct=92.0, expiration_sec=900,
                   clic_ts_ms=1, accepte_ts_ms=1, accepte=True, order_id="X1")
    resolu = c.denouer(ex)
    assert resolu.resultat == "win"
    assert resolu.profit == pytest.approx(1.46), "le gain NET, pas le retour"
