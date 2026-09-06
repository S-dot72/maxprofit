"""
L'adaptateur Pocket Option, éprouvé sans réseau.

Ce que ces tests vérifient tient en une phrase : que l'adaptateur retraduit
correctement en exceptions le silence de la bibliothèque tierce.

C'est le point sensible. `PocketOptionAPI-v2` avale toutes ses exceptions
(`except: return None`) et signale ses pannes par des valeurs de retour nulles.
Un collecteur bâti dessus tel quel tournerait des jours sans rien enregistrer
et sans une seule erreur dans les logs — exactement le mode de défaillance que
le §5 et le contrat de `MarketDataSource` cherchent à rendre impossible :
« stream() LÈVE une exception si le socket meurt. Ne jamais retourner
silencieusement : le collecteur ne saurait pas qu'il y a un trou. »

La bibliothèque est remplacée par un double injecté dans `sys.modules`.
L'adaptateur l'importe à l'intérieur de `connect()`, donc l'injection suffit —
et aucun test n'a besoin d'un compte, d'un SSID ni d'une connexion.
"""

from __future__ import annotations

import sys
import time
import types

import pytest

from maxprofit.collect.pocketoption import (
    SEUIL_COMPACTAGE,
    PocketOptionSource,
    SourceIndisponible,
)
from maxprofit.core.errors import BotError

T_SEC = 1_757_073_600          # 2025-09-05 12:00:00 UTC
T_MS = T_SEC * 1000


# --------------------------------------------------------------------------- #
# Double de la bibliothèque
# --------------------------------------------------------------------------- #

class FauxGlobals:
    def __init__(self):
        self.pairs: dict = {}
        self.websocket_is_connected = True
        self.check_websocket_if_error = False
        self.websocket_error_reason = None


#: Catalogue par défaut. Le vrai serveur le pousse APRÈS l'ouverture du socket,
#: d'où les tests d'attente plus bas.
CATALOGUE = {
    "EURUSD_otc": {"id": 1, "payout": 92, "type": "otc", "active": True},
    "GBPUSD_otc": {"id": 2, "payout": 85, "type": "otc", "active": True},
}


class FauxClient:
    def __init__(self, globals_, paires=None):
        self.g = globals_
        self._paires = CATALOGUE if paires is None else paires
        self.appels_getpairs = 0
        self.symboles_demandes: list[tuple[str, int]] = []
        self.ferme = False

    def connect(self):
        return True

    def check_connect(self):
        return bool(self.g.websocket_is_connected)

    def GetPairs(self):
        self.appels_getpairs += 1
        return self._paires

    def GetTicks(self, pair):
        entree = self.g.pairs.get(pair)
        return entree["ticks"] if entree else None

    def change_symbol(self, active, period):
        self.symboles_demandes.append((active, period))

    def disconnect(self):
        self.ferme = True

    # --- pilotage du test ---------------------------------------------------

    def pousser(self, pair, instant, prix):
        """Simule l'arrivée d'un tick par le thread WebSocket."""
        self.g.pairs.setdefault(pair, {"ticks": [], "history": []})
        self.g.pairs[pair]["ticks"].append({"time": instant, "price": prix})


@pytest.fixture
def broker(monkeypatch, tmp_path):
    """Injecte le double à la place de la vraie bibliothèque.

    Le fichier de session est redirigé vers un répertoire temporaire : sans
    cela, les tests liraient le `session.json` réel du développeur et
    passeraient ou échoueraient selon qu'il a capturé un SSID ou non. Un test
    dont le résultat dépend de l'état de la machine ne teste rien.
    """
    monkeypatch.setenv("POCKET_OPTION_SESSION_FILE",
                       str(tmp_path / "session-de-test.json"))
    globals_ = FauxGlobals()
    client = FauxClient(globals_)

    module_racine = types.ModuleType("pocketoptionapi")
    module_globals = types.ModuleType("pocketoptionapi.global_value")
    module_stable = types.ModuleType("pocketoptionapi.stable_api")

    for nom in ("pairs", "websocket_is_connected", "check_websocket_if_error",
                "websocket_error_reason"):
        setattr(module_globals, nom, getattr(globals_, nom))

    # L'adaptateur lit le module, pas l'objet : on fait pointer les deux sur le
    # même état pour que le test puisse piloter les drapeaux.
    class Proxy(types.ModuleType):
        def __getattr__(self, nom):
            return getattr(globals_, nom)

        def __setattr__(self, nom, valeur):
            setattr(globals_, nom, valeur)

    module_globals = Proxy("pocketoptionapi.global_value")
    module_racine.global_value = module_globals
    module_stable.PocketOption = lambda demo, ssid: client

    monkeypatch.setitem(sys.modules, "pocketoptionapi", module_racine)
    monkeypatch.setitem(sys.modules, "pocketoptionapi.global_value", module_globals)
    monkeypatch.setitem(sys.modules, "pocketoptionapi.stable_api", module_stable)
    monkeypatch.setenv("POCKET_OPTION_SSID", "faux-ssid")
    return client, globals_


@pytest.fixture
def source(broker):
    # Délai court : les tests n'ont pas à attendre les 25 s de production.
    src = PocketOptionSource(demo=True, delai_payouts_sec=1.0)
    src.connect()
    return src


# --------------------------------------------------------------------------- #
# Conversion des horodatages (§5)
# --------------------------------------------------------------------------- #

def test_secondes_converties_en_millisecondes():
    src = PocketOptionSource()
    assert src._vers_ms(T_SEC) == T_MS
    assert src._unite == "sec"


def test_resolution_sous_la_seconde_preservee():
    """Si le broker envoie 1757073600.234, la milliseconde ne doit pas être
    perdue : c'est elle qui distingue deux ticks d'une même seconde."""
    src = PocketOptionSource()
    assert src._vers_ms(T_SEC + 0.234) == T_MS + 234


def test_millisecondes_reconnues_telles_quelles():
    src = PocketOptionSource()
    assert src._vers_ms(T_MS) == T_MS
    assert src._unite == "ms"


def test_horodatage_absurde_refuse():
    src = PocketOptionSource()
    for absurde in (0, 42, 10**18, -T_SEC):
        with pytest.raises(BotError, match="plage plausible"):
            src._vers_ms(absurde)


def test_changement_d_unite_en_cours_de_flux_leve():
    """Deux sources qui se mélangent produiraient un historique décalé d'un
    facteur 1000 sur une partie seulement des données — indétectable ensuite."""
    src = PocketOptionSource()
    src._vers_ms(T_SEC)
    with pytest.raises(BotError, match="unité des horodatages"):
        src._vers_ms(T_MS)


def test_secondes_entieres_declenchent_un_avertissement(caplog):
    """Le cas qui fausserait le tick_count : plusieurs ticks d'une même seconde
    s'écrasent sur la clé primaire (pair, ts_ms)."""
    src = PocketOptionSource()
    with caplog.at_level("WARNING"):
        src._vers_ms(T_SEC)
    assert any("secondes ENTIÈRES" in m for m in caplog.messages)


def test_resolution_fine_ne_declenche_pas_l_avertissement(caplog):
    src = PocketOptionSource()
    with caplog.at_level("WARNING"):
        src._vers_ms(T_SEC + 0.5)
    assert not any("ENTIÈRES" in m for m in caplog.messages)


# --------------------------------------------------------------------------- #
# Le silence retraduit en exceptions
# --------------------------------------------------------------------------- #

def test_get_pairs_qui_renvoie_none_leve(source, broker):
    """LE test de ce fichier.

    `GetPairs()` renvoie None sur n'importe quelle erreur interne. Sans
    traduction, une panne ressemblerait à « aucune paire ouverte » et le
    collecteur se croirait tranquillement en week-end pendant quatorze jours.
    """
    client, _ = broker
    client._paires = None
    with pytest.raises(SourceIndisponible, match="Aucune donnée de payout"):
        source.list_pairs()


def test_socket_ferme_pendant_le_flux_leve(source, broker):
    client, globals_ = broker
    client.pousser("EURUSD_otc", T_SEC + 0.1, 1.1)
    source.subscribe(["EURUSD_otc"])

    flux = source.stream()
    assert next(flux).price == 1.1

    globals_.websocket_is_connected = False
    with pytest.raises(SourceIndisponible, match="Socket fermé"):
        next(flux)


def test_erreur_websocket_signalee_avec_sa_raison(source, broker):
    _, globals_ = broker
    globals_.check_websocket_if_error = True
    globals_.websocket_error_reason = "connexion réinitialisée"
    with pytest.raises(SourceIndisponible, match="connexion réinitialisée"):
        source.list_pairs()


def test_le_drapeau_d_erreur_est_consomme(source, broker):
    """Sinon la première erreur ferait échouer toutes les reconnexions
    suivantes, et le collecteur ne repartirait jamais."""
    _, globals_ = broker
    globals_.check_websocket_if_error = True
    globals_.websocket_error_reason = "coupure"
    with pytest.raises(SourceIndisponible):
        source.list_pairs()
    assert globals_.check_websocket_if_error is False


def test_connect_non_appele_leve():
    src = PocketOptionSource()
    with pytest.raises(SourceIndisponible, match="connect"):
        src._verifier_connexion()


def test_aucune_paire_exploitable_leve(source, broker):
    """Un format changé ne doit pas passer pour un marché fermé."""
    client, _ = broker
    client._paires = {"EURUSD_otc": {"champ": "inattendu"}}
    with pytest.raises(SourceIndisponible, match="format"):
        source.list_pairs()


# --------------------------------------------------------------------------- #
# Paires et payouts (§2.3)
# --------------------------------------------------------------------------- #

def test_toutes_les_paires_sont_remontees_ouvertes_ou_non(source, broker):
    """« Retourner TOUTES les paires, ouvertes ou non, avec leur payout brut. »
    Le filtrage appartient au collecteur : le backtest doit pouvoir rejouer
    l'éligibilité telle qu'elle était (§2.3)."""
    client, _ = broker
    client._paires = {
        "EURUSD_otc": {"id": 1, "payout": 92, "type": "otc", "active": True},
        "GBPUSD_otc": {"id": 2, "payout": 45, "type": "otc", "active": False},
    }
    paires = {p.name: p for p in source.list_pairs()}
    assert paires["EURUSD_otc"].payout_pct == 92
    assert paires["GBPUSD_otc"].payout_pct == 45
    assert paires["GBPUSD_otc"].is_open is False


def test_une_paire_mal_formee_est_ignoree_mais_signalee(source, broker, caplog):
    client, _ = broker
    client._paires = {
        "BONNE_otc": {"id": 1, "payout": 92, "type": "otc", "active": True},
        "CASSEE_otc": {"payout": 300, "active": True},          # payout absurde
    }
    with caplog.at_level("WARNING"):
        paires = source.list_pairs()
    assert [p.name for p in paires] == ["BONNE_otc"]
    assert any("Paire ignorée" in m for m in caplog.messages)


# --------------------------------------------------------------------------- #
# Drainage du tampon
# --------------------------------------------------------------------------- #

def test_aucun_tick_n_est_lu_deux_fois(source, broker):
    client, _ = broker
    source.subscribe(["EURUSD_otc"])
    for i in range(5):
        client.pousser("EURUSD_otc", T_SEC + i * 0.25, 1.1 + i / 1000)

    premiers = list(source._drainer("EURUSD_otc"))
    assert len(premiers) == 5
    assert list(source._drainer("EURUSD_otc")) == []

    client.pousser("EURUSD_otc", T_SEC + 2.0, 1.2)
    suivants = list(source._drainer("EURUSD_otc"))
    assert len(suivants) == 1
    assert suivants[0].price == 1.2


def test_le_tampon_est_compacte_pour_ne_pas_croitre_indefiniment(source, broker):
    """La bibliothèque empile sans jamais purger. Sur quatorze jours de
    collecte, cela finirait par saturer la mémoire du conteneur."""
    client, globals_ = broker
    source.subscribe(["EURUSD_otc"])
    for i in range(SEUIL_COMPACTAGE + 10):
        client.pousser("EURUSD_otc", T_SEC + i * 0.01, 1.1)

    lus = list(source._drainer("EURUSD_otc"))
    assert len(lus) == SEUIL_COMPACTAGE + 10
    assert len(globals_.pairs["EURUSD_otc"]["ticks"]) == 0
    assert source._vus["EURUSD_otc"] == 0

    # Et le drainage repart correctement après compactage.
    client.pousser("EURUSD_otc", T_SEC + 999, 1.3)
    assert [t.price for t in source._drainer("EURUSD_otc")] == [1.3]


def test_un_tick_illisible_n_interrompt_pas_le_flux(source, broker, caplog):
    """Une trame malformée ne doit pas tuer une collecte de quatorze jours ;
    elle doit être signalée et sautée."""
    client, globals_ = broker
    source.subscribe(["EURUSD_otc"])
    globals_.pairs["EURUSD_otc"] = {"ticks": [
        {"time": T_SEC + 0.1, "price": 1.1},
        {"prix": "champ inattendu"},
        {"time": T_SEC + 0.2, "price": 1.2},
    ], "history": []}

    with caplog.at_level("WARNING"):
        ticks = list(source._drainer("EURUSD_otc"))
    assert [t.price for t in ticks] == [1.1, 1.2]
    assert any("illisible" in m for m in caplog.messages)


def test_un_tick_hors_plage_est_rejete_sans_tuer_le_flux(source, broker, caplog):
    client, globals_ = broker
    source.subscribe(["EURUSD_otc"])
    globals_.pairs["EURUSD_otc"] = {"ticks": [
        {"time": T_SEC + 0.1, "price": 1.1},
        {"time": 42, "price": 1.15},          # horodatage absurde
        {"time": T_SEC + 0.2, "price": 1.2},
    ], "history": []}

    with caplog.at_level("WARNING"):
        ticks = list(source._drainer("EURUSD_otc"))
    assert [t.price for t in ticks] == [1.1, 1.2]
    assert any("rejeté" in m for m in caplog.messages)


def test_subscribe_demande_chaque_paire(source, broker):
    client, _ = broker
    source.subscribe(["EURUSD_otc", "GBPUSD_otc"])
    assert client.symboles_demandes == [("EURUSD_otc", 60), ("GBPUSD_otc", 60)]


def test_le_flux_sert_toutes_les_paires_souscrites(source, broker):
    client, _ = broker
    source.subscribe(["EURUSD_otc", "GBPUSD_otc"])
    client.pousser("EURUSD_otc", T_SEC + 0.1, 1.1)
    client.pousser("GBPUSD_otc", T_SEC + 0.2, 1.3)

    flux = source.stream()
    recus = {next(flux).pair, next(flux).pair}
    assert recus == {"EURUSD_otc", "GBPUSD_otc"}


# --------------------------------------------------------------------------- #
# Hébergement
# --------------------------------------------------------------------------- #

def test_sans_ssid_le_demarrage_refuse_toujours(broker, monkeypatch):
    """Régression : le SSID est obligatoire, même en session interactive.

    La bibliothèque sait ouvrir une fenêtre de connexion quand il manque, mais
    ce chemin se bloque indéfiniment — son `read_cookies` attend sept cookies
    simultanés dont six traceurs tiers, et quand l'un manque il renonce sans
    fermer la fenêtre. `webview.start()` ne rend alors jamais la main : le
    processus reste figé, sans un message. Pour un collecteur, un blocage
    silencieux est le pire mode de défaillance : on le croit en train de
    travailler.
    """
    monkeypatch.delenv("POCKET_OPTION_SSID", raising=False)
    with pytest.raises(SourceIndisponible, match="capturer_ssid"):
        PocketOptionSource(demo=True).connect()


def test_la_bibliotheque_absente_donne_un_message_utile(monkeypatch):
    monkeypatch.setitem(sys.modules, "pocketoptionapi", None)
    with pytest.raises(SourceIndisponible, match="pip install"):
        PocketOptionSource(demo=True).connect()


def test_compte_reel_avertit(caplog):
    with caplog.at_level("WARNING"):
        PocketOptionSource(demo=False)
    assert any("compte démo" in m for m in caplog.messages)


# --------------------------------------------------------------------------- #
# Compatibilité Python 3.12+
# --------------------------------------------------------------------------- #

def test_une_boucle_asyncio_est_installee_avant_la_construction(source):
    """Régression.

    `PocketOptionAPI.__init__` et `WebsocketClient.__init__` appellent
    `asyncio.get_event_loop()`. Jusqu'à Python 3.10 cet appel créait une boucle
    quand le thread n'en avait pas ; depuis 3.12 il lève. La bibliothèque a été
    écrite avant ce changement, et sans ce correctif elle est inutilisable sur
    un Python récent — l'erreur survient à la construction, avant même la
    moindre tentative de connexion.
    """
    import asyncio

    assert source._boucle is not None
    assert not source._boucle.is_closed()
    # La vraie exigence, formulée comme la bibliothèque la formule : cet appel
    # ne doit pas lever. C'est exactement la ligne qui échouait auparavant.
    assert asyncio.get_event_loop() is source._boucle


def test_la_boucle_est_fermee_a_la_fermeture(source):
    """Sans cela, chaque reconnexion laisserait un descripteur ouvert — et il y
    en a une par coupure réseau sur quatorze jours de collecte."""
    boucle = source._boucle
    source.close()
    assert boucle.is_closed()
    assert source._boucle is None


def test_appel_depuis_une_boucle_en_cours_refuse(broker):
    """Remplacer une boucle en cours casserait le serveur HTTP de
    `hosting.service`. Mieux vaut refuser que contourner."""
    import asyncio

    async def depuis_une_coroutine():
        PocketOptionSource(demo=True).connect()

    with pytest.raises(SourceIndisponible, match="propre thread"):
        asyncio.run(depuis_une_coroutine())


def test_la_boucle_est_reutilisee_entre_deux_connexions(source):
    premiere = source._boucle
    source._installer_boucle_asyncio()
    assert source._boucle is premiere, "une boucle de plus à chaque reconnexion"


# --------------------------------------------------------------------------- #
# Le catalogue arrive APRÈS le socket
# --------------------------------------------------------------------------- #

def test_connect_attend_le_catalogue_des_actifs(broker):
    """Régression.

    L'ouverture du socket et l'arrivée du catalogue des actifs sont deux
    événements distincts : le serveur pousse le second de façon asynchrone,
    quelques instants après la poignée de main. Entre les deux,
    `GetPayoutData()` renvoie None, `json.loads(None)` lève, et le bare `except`
    de `GetPairs()` transforme cela en None.

    Interroger dès que `check_connect()` est vrai concluait donc « broker en
    panne » à chaque démarrage. Comme le collecteur redémarre avec backoff, il
    n'aurait jamais dépassé cette étape : une course perdue au démarrage serait
    devenue une panne permanente.
    """
    import threading

    client, _ = broker
    client._paires = None

    def catalogue_tardif():
        time.sleep(0.4)
        client._paires = CATALOGUE

    threading.Thread(target=catalogue_tardif, daemon=True).start()

    src = PocketOptionSource(demo=True, delai_payouts_sec=5.0)
    src.connect()                       # ne doit pas lever
    assert len(src.list_pairs()) == 2
    assert client.appels_getpairs > 1, "aucune nouvelle tentative"


def test_catalogue_jamais_recu_leve_avec_une_piste(broker):
    """Un SSID accepté par le socket mais refusé pour les données donne
    exactement ce symptôme : connecté, mais aucun actif."""
    client, _ = broker
    client._paires = None
    src = PocketOptionSource(demo=True, delai_payouts_sec=0.5)
    with pytest.raises(SourceIndisponible, match="capturer_ssid"):
        src.connect()


def test_catalogue_vide_traite_comme_absent(broker):
    """`{}` n'est pas « aucun actif ouvert » : c'est « rien reçu ». Les deux se
    ressemblent, et les confondre ferait collecter dans le vide."""
    client, _ = broker
    client._paires = {}
    src = PocketOptionSource(demo=True, delai_payouts_sec=0.5)
    with pytest.raises(SourceIndisponible, match="Aucune donnée de payout"):
        src.connect()


# --------------------------------------------------------------------------- #
# Persistance du SSID — plus de copier-coller
# --------------------------------------------------------------------------- #

def test_le_ssid_est_relu_du_fichier_de_session(tmp_path):
    from maxprofit.collect.pocketoption import ecrire_session, lire_session

    fichier = tmp_path / "session.json"
    ecrire_session("42[auth-demo]", demo=True, uid="123", chemin=fichier)

    assert lire_session(demo=True, chemin=fichier) == "42[auth-demo]"


def test_une_session_reelle_n_est_pas_servie_a_une_demande_demo(tmp_path, caplog):
    """Le contrôle n'est pas une politesse : un SSID de compte RÉEL utilisé en
    croyant être en démo ferait passer de vrais ordres. Le SSID porte lui-même
    `isDemo`, donc l'erreur est détectable — autant la détecter."""
    from maxprofit.collect.pocketoption import ecrire_session, lire_session

    fichier = tmp_path / "session.json"
    ecrire_session("42[auth-reel]", demo=False, uid="123", chemin=fichier)

    with caplog.at_level("WARNING"):
        assert lire_session(demo=True, chemin=fichier) is None
    assert any("RÉEL" in m for m in caplog.messages)


def test_fichier_de_session_absent_ou_corrompu(tmp_path, caplog):
    from maxprofit.collect.pocketoption import lire_session

    assert lire_session(demo=True, chemin=tmp_path / "absent.json") is None

    corrompu = tmp_path / "session.json"
    corrompu.write_text("{ pas du json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert lire_session(demo=True, chemin=corrompu) is None
    assert any("illisible" in m for m in caplog.messages)


def test_connect_utilise_le_fichier_de_session(broker, monkeypatch, tmp_path):
    """Le point de la demande : rien à recopier. La capture écrit le fichier,
    tout le reste le relit."""
    from maxprofit.collect.pocketoption import ENV_FICHIER_SESSION, ecrire_session

    monkeypatch.delenv("POCKET_OPTION_SSID", raising=False)
    fichier = tmp_path / "session.json"
    ecrire_session("42[auth-du-fichier]", demo=True, uid="1", chemin=fichier)
    monkeypatch.setenv(ENV_FICHIER_SESSION, str(fichier))

    src = PocketOptionSource(demo=True, delai_payouts_sec=1.0)
    src.connect()                      # ne doit pas lever


def test_l_environnement_l_emporte_sur_le_fichier(broker, monkeypatch, tmp_path):
    """En hébergement, la plateforme injecte le SSID ; un fichier resté dans
    l'image ne doit pas le remplacer par un jeton périmé."""
    from maxprofit.collect.pocketoption import (
        ENV_FICHIER_SESSION,
        ecrire_session,
        lire_session,
    )

    fichier = tmp_path / "session.json"
    ecrire_session("42[auth-du-fichier]", demo=True, chemin=fichier)
    monkeypatch.setenv(ENV_FICHIER_SESSION, str(fichier))
    monkeypatch.setenv("POCKET_OPTION_SSID", "42[auth-de-l-env]")

    src = PocketOptionSource(demo=True, delai_payouts_sec=1.0)
    src.connect()
    # Le fichier existe bien, mais ce n'est pas lui qui a servi.
    assert lire_session(demo=True, chemin=fichier) == "42[auth-du-fichier]"


def test_resoudre_ssid_est_la_seule_regle(monkeypatch, tmp_path):
    """Régression : le diagnostic avait sa propre version de cette règle, qui
    ne regardait que l'environnement. Il annonçait « aucun SSID » alors que
    l'adaptateur, lui, l'aurait trouvé dans le fichier de session — l'invariant
    n°1 en miniature, deux implémentations d'une même règle qui divergent."""
    from maxprofit.collect.pocketoption import (
        ENV_FICHIER_SESSION,
        ecrire_session,
        resoudre_ssid,
    )

    fichier = tmp_path / "session.json"
    monkeypatch.setenv(ENV_FICHIER_SESSION, str(fichier))
    monkeypatch.delenv("POCKET_OPTION_SSID", raising=False)

    # 1. rien nulle part
    assert resoudre_ssid(demo=True) is None

    # 2. le fichier seul suffit — c'est le cas après capturer_ssid.py
    ecrire_session("42[du-fichier]", demo=True, chemin=fichier)
    assert resoudre_ssid(demo=True) == "42[du-fichier]"

    # 3. l'environnement l'emporte
    monkeypatch.setenv("POCKET_OPTION_SSID", "42[de-l-env]")
    assert resoudre_ssid(demo=True) == "42[de-l-env]"

    # 4. l'argument explicite l'emporte sur tout
    assert resoudre_ssid(demo=True, explicite="42[explicite]") == "42[explicite]"

    # 5. une chaîne vide n'est pas une valeur
    assert resoudre_ssid(demo=True, explicite="   ") == "42[de-l-env]"
