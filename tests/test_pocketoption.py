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

import maxprofit.collect.pocketoption as po
from maxprofit.collect.pocketoption import (
    SEUIL_COMPACTAGE,
    PocketOptionSource,
    SourceIndisponible,
)
from maxprofit.core.errors import BotError

#: Jeton bien forme : prefixe correct, session de longueur credible, uid
#: non nul. Aucune valeur reelle -- seule la FORME compte ici.
@pytest.fixture(autouse=True)
def _sans_attente_d_authentification(monkeypatch):
    """Aucun test n'a a attendre les vingt secondes de production.

    Le delai est resolu a l'appel, donc patcher la constante suffit : le figer
    dans la signature l'aurait rendu impatchable.
    """
    monkeypatch.setattr(po, "DELAI_AUTHENTIFICATION_SEC", 0.0)


SSID_PLAUSIBLE = (
    '42["auth",{"session":"' + "a" * 40
    + '","isDemo":1,"uid":123456,"platform":2}]'
)

#: Un second jeton bien forme, DISTINCT du premier : sert a montrer lequel des
#: deux a servi quand l'environnement et le fichier en proposent chacun un.
SSID_PLAUSIBLE_ENV = (
    '42["auth",{"session":"' + "b" * 40
    + '","isDemo":1,"uid":654321,"platform":2}]'
)

#: Les faux ticks sont datés de MAINTENANT, pas d'une date figée : l'adaptateur
#: mesure désormais le décalage d'horloge du broker contre l'UTC réel, et un
#: horodatage figé vieillit — au bout de quelques mois il serait rejeté, et les
#: tests se mettraient à échouer sans que rien n'ait changé dans le code.
def maintenant_sec(decalage: float = 0.0) -> float:
    return time.time() + decalage


T_SEC = 1_757_073_600          # utilisé seulement là où l'horloge n'entre pas en jeu
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
        self.connexions = 0
        self.symboles_demandes: list[tuple[str, int]] = []
        self.ferme = False

    def connect(self):
        self.connexions += 1
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
    # Un jeton de la MEME FORME qu'un vrai : depuis qu'un gabarit installe
    # en production a coute trois jours de collecte muette, `connect()`
    # refuse ce qui ne peut pas authentifier.
    monkeypatch.setenv("POCKET_OPTION_SSID", SSID_PLAUSIBLE)
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
    """Ces tests portent sur l'UNITÉ, pas sur le fuseau : ils utilisent donc
    l'instant courant, pour que la correction d'horloge mesure zéro et ne se
    mêle pas de ce qu'on cherche à vérifier."""
    src = PocketOptionSource()
    maintenant = time.time()
    assert abs(src._vers_ms(maintenant) - maintenant * 1000) < 1000
    assert src._unite == "sec"


def test_resolution_sous_la_seconde_preservee():
    """Si le broker envoie ...600.234, la milliseconde ne doit pas être perdue :
    c'est elle qui distingue deux ticks d'une même seconde."""
    src = PocketOptionSource()
    base = float(int(time.time()))
    assert src._vers_ms(base + 0.234) == round(base * 1000) + 234


def test_millisecondes_reconnues_telles_quelles():
    src = PocketOptionSource()
    maintenant_ms = round(time.time() * 1000)
    assert abs(src._vers_ms(maintenant_ms) - maintenant_ms) < 1000
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
    src._vers_ms(time.time())
    with pytest.raises(BotError, match="unité des horodatages"):
        src._vers_ms(round(time.time() * 1000))


def test_secondes_entieres_declenchent_un_avertissement(caplog):
    """Le cas qui fausserait le tick_count : plusieurs ticks d'une même seconde
    s'écrasent sur la clé primaire (pair, ts_ms)."""
    src = PocketOptionSource()
    with caplog.at_level("WARNING"):
        src._vers_ms(float(int(time.time())))
    assert any("secondes ENTIÈRES" in m for m in caplog.messages)


def test_resolution_fine_ne_declenche_pas_l_avertissement(caplog):
    src = PocketOptionSource()
    with caplog.at_level("WARNING"):
        src._vers_ms(int(time.time()) + 0.5)
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
    # SessionExpiree hérite de SourceIndisponible : le silence de GetPairs est
    # bien traduit en exception, et qualifié — ce n'est pas une coupure réseau.
    with pytest.raises(SourceIndisponible, match="SSID est expiré"):
        source.list_pairs()


def test_socket_ferme_pendant_le_flux_leve(source, broker):
    client, globals_ = broker
    client.pousser("EURUSD_otc", maintenant_sec(0.1), 1.1)
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
        client.pousser("EURUSD_otc", maintenant_sec(i * 0.25), 1.1 + i / 1000)

    premiers = list(source._drainer("EURUSD_otc"))
    assert len(premiers) == 5
    assert list(source._drainer("EURUSD_otc")) == []

    client.pousser("EURUSD_otc", maintenant_sec(2.0), 1.2)
    suivants = list(source._drainer("EURUSD_otc"))
    assert len(suivants) == 1
    assert suivants[0].price == 1.2


def test_le_tampon_est_compacte_pour_ne_pas_croitre_indefiniment(source, broker):
    """La bibliothèque empile sans jamais purger. Sur quatorze jours de
    collecte, cela finirait par saturer la mémoire du conteneur."""
    client, globals_ = broker
    source.subscribe(["EURUSD_otc"])
    for i in range(SEUIL_COMPACTAGE + 10):
        client.pousser("EURUSD_otc", maintenant_sec(i * 0.01), 1.1)

    lus = list(source._drainer("EURUSD_otc"))
    assert len(lus) == SEUIL_COMPACTAGE + 10
    assert len(globals_.pairs["EURUSD_otc"]["ticks"]) == 0
    assert source._vus["EURUSD_otc"] == 0

    # Et le drainage repart correctement après compactage.
    client.pousser("EURUSD_otc", maintenant_sec(999), 1.3)
    assert [t.price for t in source._drainer("EURUSD_otc")] == [1.3]


def test_un_tick_illisible_n_interrompt_pas_le_flux(source, broker, caplog):
    """Une trame malformée ne doit pas tuer une collecte de quatorze jours ;
    elle doit être signalée et sautée."""
    client, globals_ = broker
    source.subscribe(["EURUSD_otc"])
    globals_.pairs["EURUSD_otc"] = {"ticks": [
        {"time": maintenant_sec(0.1), "price": 1.1},
        {"prix": "champ inattendu"},
        {"time": maintenant_sec(0.2), "price": 1.2},
    ], "history": []}

    with caplog.at_level("WARNING"):
        ticks = list(source._drainer("EURUSD_otc"))
    assert [t.price for t in ticks] == [1.1, 1.2]
    assert any("illisible" in m for m in caplog.messages)


def test_un_tick_hors_plage_est_rejete_sans_tuer_le_flux(source, broker, caplog):
    client, globals_ = broker
    source.subscribe(["EURUSD_otc"])
    globals_.pairs["EURUSD_otc"] = {"ticks": [
        {"time": maintenant_sec(0.1), "price": 1.1},
        {"time": 42, "price": 1.15},          # horodatage absurde
        {"time": maintenant_sec(0.2), "price": 1.2},
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
    client.pousser("EURUSD_otc", maintenant_sec(0.1), 1.1)
    client.pousser("GBPUSD_otc", maintenant_sec(0.2), 1.3)

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
    with pytest.raises(SourceIndisponible, match="refuse les données"):
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
    ecrire_session(SSID_PLAUSIBLE, demo=True, uid="1", chemin=fichier)
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
        resoudre_ssid,
    )

    fichier = tmp_path / "session.json"
    ecrire_session(SSID_PLAUSIBLE, demo=True, chemin=fichier)
    monkeypatch.setenv(ENV_FICHIER_SESSION, str(fichier))
    monkeypatch.setenv("POCKET_OPTION_SSID", SSID_PLAUSIBLE_ENV)

    src = PocketOptionSource(demo=True, delai_payouts_sec=1.0)
    src.connect()
    # Le fichier existe bien, mais ce n'est pas lui qui a servi.
    assert lire_session(demo=True, chemin=fichier) == SSID_PLAUSIBLE
    assert resoudre_ssid(demo=True) == SSID_PLAUSIBLE_ENV


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


# --------------------------------------------------------------------------- #
# L'horloge du broker n'est pas l'UTC
# --------------------------------------------------------------------------- #

def test_le_decalage_de_fuseau_est_mesure_et_corrige():
    """Régression, découverte sur une connexion réelle.

    Pocket Option envoie du UTC+2. L'horodatage reste un epoch parfaitement
    plausible — `ensure_ms` ne peut pas l'attraper — mais il est faux de deux
    heures. Comme le collecteur horodate `payouts` et `uptime` avec l'horloge
    système, en vrai UTC, la jointure du §2.3 irait chercher pour chaque trade
    un payout relevé jusqu'à deux heures APRÈS. C'est du look-ahead.
    """
    src = PocketOptionSource()
    maintenant = time.time()

    corrige_ms = src._vers_ms(maintenant + 2 * 3600)

    assert src.decalage_horloge_heures == 2.0
    assert abs(corrige_ms / 1000 - maintenant) < 1.0


def test_un_broker_en_utc_ne_subit_aucune_correction():
    src = PocketOptionSource()
    maintenant = time.time()
    assert abs(src._vers_ms(maintenant) / 1000 - maintenant) < 1.0
    assert src.decalage_horloge_heures == 0.0


def test_decalage_negatif_aussi(caplog):
    src = PocketOptionSource()
    with caplog.at_level("WARNING"):
        src._vers_ms(time.time() - 5 * 3600)
    assert src.decalage_horloge_heures == -5.0


def test_un_residu_trop_grand_refuse():
    """Un fuseau est un nombre entier d'heures. Un résidu de plusieurs minutes
    signifie que l'horloge du poste est fausse, ou que le broker fait autre
    chose que ce qu'on croit — dans les deux cas, écrire quand même produirait
    un historique décalé dont rien ne signalerait l'erreur."""
    src = PocketOptionSource()
    with pytest.raises(BotError, match="Horloge incompréhensible"):
        src._vers_ms(time.time() + 2 * 3600 + 600)


def test_la_latence_reseau_ne_fausse_pas_la_mesure():
    """Arrondir à l'heure entière évite d'inscrire dans les données le hasard
    du premier tick reçu."""
    src = PocketOptionSource()
    src._vers_ms(time.time() + 2 * 3600 - 1.7)
    assert src.decalage_horloge_heures == 2.0


def test_un_changement_d_heure_du_broker_est_detecte(caplog, monkeypatch):
    """Si l'horloge du broker suit l'heure d'été européenne, elle passe de +2 h
    à +1 h fin octobre — au milieu d'une collecte de quatorze jours."""
    import maxprofit.collect.pocketoption as module

    monkeypatch.setattr(module, "INTERVALLE_VERIF_HORLOGE_SEC", 0)
    src = PocketOptionSource()
    src._vers_ms(time.time() + 2 * 3600)
    assert src.decalage_horloge_heures == 2.0

    with caplog.at_level("WARNING"):
        corrige_ms = src._vers_ms(time.time() + 1 * 3600)

    assert src.decalage_horloge_heures == 1.0
    assert any("changement d'heure" in m for m in caplog.messages)
    assert abs(corrige_ms / 1000 - time.time()) < 1.0


def test_la_mesure_n_est_pas_refaite_a_chaque_tick(monkeypatch):
    """Une mesure par tick coûterait un appel horloge à chaque cotation, et
    ferait surtout osciller la correction au gré de la latence."""
    src = PocketOptionSource()
    src._vers_ms(time.time() + 2 * 3600)
    prochaine = src._prochaine_verif_horloge

    src._vers_ms(time.time() + 2 * 3600)
    assert src._prochaine_verif_horloge == prochaine


def test_un_horodatage_perime_est_refuse():
    """Découvert en corrigeant les tests d'unité : un horodatage vieux d'un an
    donne, après arrondi à l'heure entière, un résidu de 78 secondes — sous la
    tolérance. Sans borne sur le décalage, il aurait été accepté et « corrigé »,
    c'est-à-dire réécrit à l'heure courante. Un horodatage périmé rejoué serait
    devenu une donnée d'apparence normale."""
    src = PocketOptionSource()
    with pytest.raises(BotError, match="impossible"):
        src._vers_ms(time.time() - 365 * 86400)


def test_une_horloge_incoherente_arrete_le_flux(source, broker):
    """Régression, découverte parce qu'un test s'est mis à tourner sans fin.

    Une horloge incompréhensible concerne TOUS les ticks. Si `_vers_tick` la
    rattrapait comme un tick malformé, `stream()` boucherait indéfiniment en
    n'écrivant que des avertissements : vivant aux yeux de la sonde, et pas une
    ligne en base. C'est le mode de défaillance que tout ce projet combat, et il
    s'était glissé ici.
    """
    from maxprofit.collect.pocketoption import HorlogeIncoherente

    client, globals_ = broker
    source.subscribe(["EURUSD_otc"])
    client.pousser("EURUSD_otc", maintenant_sec(-365 * 86400), 1.1)

    with pytest.raises(HorlogeIncoherente, match="impossible"):
        next(source.stream())


def test_un_flux_devenu_illisible_finit_par_lever(source, broker):
    """Un tick malformé isolé se saute. Cent d'affilée, c'est que le format a
    changé — et continuer reviendrait à collecter dans le vide."""
    from maxprofit.collect.pocketoption import REJETS_AVANT_ALERTE

    client, globals_ = broker
    source.subscribe(["EURUSD_otc"])
    globals_.pairs["EURUSD_otc"] = {"ticks": [
        {"time": maintenant_sec(), "price": -1.0}      # prix invalide
        for _ in range(REJETS_AVANT_ALERTE + 5)
    ], "history": []}

    with pytest.raises(SourceIndisponible, match="consécutifs rejetés"):
        list(source._drainer("EURUSD_otc"))


def test_un_tick_valide_remet_le_compteur_a_zero(source, broker):
    """Sinon quelques anomalies éparses sur quatorze jours finiraient par
    déclencher une fausse alerte."""
    client, globals_ = broker
    source.subscribe(["EURUSD_otc"])
    globals_.pairs["EURUSD_otc"] = {"ticks": [
        {"time": maintenant_sec(), "price": -1.0},
        {"time": maintenant_sec(0.1), "price": 1.1},
        {"time": maintenant_sec(0.2), "price": -1.0},
    ], "history": []}

    ticks = list(source._drainer("EURUSD_otc"))
    assert [t.price for t in ticks] == [1.1]
    assert source._rejets_consecutifs == 1


def test_une_reconnexion_n_ouvre_pas_de_second_client(broker):
    """Régression du pire incident de la mise en production.

    `PocketOptionAPI.start_websocket()` se termine par `loop.run_forever()` : le
    thread ne revient jamais, porte sa PROPRE boucle de reconnexion, et
    `disconnect()` ne l'arrête pas — il vise une autre boucle. Ouvrir un second
    client n'ajoutait donc pas une connexion : il ajoutait un concurrent, puis
    un troisième.

    Observé : au bout de quelques cycles, une dizaine de tentatives simultanées
    et « timed out during opening handshake » en boucle, le broker ne répondant
    plus à personne. Une coupure d'une seconde condamnait la collecte.

    Le client existant se rétablissant seul, la bonne action est d'attendre.
    """
    client, _ = broker
    src = PocketOptionSource(demo=True, delai_payouts_sec=1.0)
    src.connect()
    premier = src._client

    src.connect()      # une « reconnexion » : ne doit rien créer
    assert src._client is premier, "un second client a été ouvert"
    assert client.connexions == 1, "la bibliothèque a été relancée"


def test_une_coupure_durable_reclame_un_processus_neuf(broker, monkeypatch):
    """Régression du pire incident de la mise en production.

    `PocketOptionAPI.start_websocket()` se termine par `loop.run_forever()` : le
    thread ne revient jamais et porte sa propre boucle de reconnexion, que
    `disconnect()` n'arrête pas — il vise une autre boucle. Chaque reconnexion
    en interne ajoutait donc un thread qui n'arrêterait plus jamais d'appeler le
    broker.

    Observé : au bout de quelques cycles, une dizaine de tentatives simultanées
    et « timed out during opening handshake » en boucle, le broker ne répondant
    plus à personne. Une coupure réseau d'une seconde condamnait la collecte.

    Mieux vaut mourir et laisser l'hébergeur relancer un processus propre.
    """
    import maxprofit.collect.pocketoption as module
    from maxprofit.collect.pocketoption import RedemarrageRequis

    client, globals_ = broker
    src = PocketOptionSource(demo=True, delai_payouts_sec=0.5)
    src.connect()

    # Le broker ne revient pas, et le budget d'attente est court pour le test.
    monkeypatch.setattr(module, "DELAI_RETABLISSEMENT_SEC", 0.5)
    globals_.websocket_is_connected = False

    with pytest.raises(RedemarrageRequis, match="Ouvrir un second client"):
        src.connect()


def test_un_redemarrage_requis_est_fatal_pour_le_collecteur():
    """Il ne doit PAS être rattrapé comme une indisponibilité passagère : la
    boucle de reconnexion est précisément ce qui creuse le trou."""
    from maxprofit.collect.collector import FATALES
    from maxprofit.collect.pocketoption import RedemarrageRequis, SourceIndisponible

    assert not issubclass(RedemarrageRequis, SourceIndisponible)
    assert any(issubclass(RedemarrageRequis, f) for f in FATALES)


def test_une_indisponibilite_passagere_est_reessayee_pas_fatale():
    """Régression : `SourceIndisponible` hérite de `BotError`, qui figure dans
    `FATALES`. Sans un cas explicite AVANT, une indisponibilité passagère du
    broker tuait la collecte au lieu d'être absorbée par le backoff — l'inverse
    exact de ce que sa docstring promet.

    `SessionExpiree`, elle, doit rester fatale : réessayer avec un jeton mort ne
    répare rien, il faut un humain."""
    from maxprofit.collect.collector import FATALES
    from maxprofit.collect.pocketoption import SessionExpiree, SourceIndisponible

    assert issubclass(SessionExpiree, SourceIndisponible)
    assert issubclass(SourceIndisponible, BotError)
    # BotError est dans FATALES : c'est bien l'ORDRE des `except` qui protège,
    # et c'est pourquoi ce test existe.
    assert any(issubclass(SourceIndisponible, f) for f in FATALES)


# --------------------------------------------------------------------------- #
# Le tampon de la bibliotheque peut etre REMPLACE, pas seulement rallonge
# --------------------------------------------------------------------------- #
#
# `global_value.pairs[actif] = {'ticks': ...}` : la bibliotheque refait la liste
# quand elle recharge l'historique d'un actif. Notre position de lecture pointe
# alors au-dela de la fin, et la comparaison `fin <= vus` nous faisait ignorer ce
# tampon POUR TOUJOURS -- muets, sans une seule erreur.

class _ClientATampon:
    def __init__(self):
        self.tampons: dict[str, list] = {}

    def GetTicks(self, nom):
        return self.tampons.get(nom)

    def check_connect(self):
        return True


def _source_branchee(client, souscrites):
    source = PocketOptionSource(demo=True)
    source._client = client
    source._souscrites = list(souscrites)
    source._decalage_sec = 0
    source._unite = "sec"
    return source


def test_un_tampon_remplace_est_relu_depuis_le_debut():
    client = _ClientATampon()
    source = _source_branchee(client, ["EURUSD_otc"])
    base = int(time.time())      # l'horloge du broker est verifiee contre la notre

    client.tampons["EURUSD_otc"] = [
        {"time": base + i, "price": 1.1} for i in range(5)
    ]
    premiers = list(source._drainer("EURUSD_otc"))
    assert len(premiers) == 5
    assert source._vus["EURUSD_otc"] == 5

    # La bibliotheque remplace la liste par une neuve, plus courte.
    client.tampons["EURUSD_otc"] = [
        {"time": base + 100 + i, "price": 1.2} for i in range(2)
    ]
    suivants = list(source._drainer("EURUSD_otc"))
    assert len(suivants) == 2, (
        "le tampon remplace a ete ignore : la collecte serait muette pour de bon"
    )
    assert [t.price for t in suivants] == [1.2, 1.2]


def test_le_diagnostic_dit_ce_que_la_bibliotheque_a_recu(monkeypatch):
    """La mesure qui tranche : panne chez nous, ou broker silencieux."""
    from pocketoptionapi import global_value

    client = _ClientATampon()
    source = _source_branchee(client, ["EURUSD_otc", "GBPUSD_otc"])
    monkeypatch.setattr(
        global_value, "pairs",
        {"EURUSD_otc": {"ticks": [1, 2, 3], "history": []}}, raising=False)

    etat = source.diagnostic()
    assert etat["connecte"] is True
    assert etat["cles_bibliotheque"] == 1
    assert etat["tampons"]["EURUSD_otc"]["ticks"] == 3
    # Une paire souscrite dont la bibliotheque ne sait rien doit apparaitre a
    # zero, pas disparaitre : c'est justement le cas qu'on cherche a voir.
    assert etat["tampons"]["GBPUSD_otc"]["ticks"] == 0


def test_le_diagnostic_survit_a_un_client_absent():
    source = PocketOptionSource(demo=True)
    etat = source.diagnostic()
    assert etat["connecte"] is None
    assert etat["tampons"] == {}


# --------------------------------------------------------------------------- #
# Le point d'acces : un seul essaye, deux publies
# --------------------------------------------------------------------------- #

def test_sans_variable_on_ne_touche_a_rien(monkeypatch):
    from pocketoptionapi.constants import REGION

    monkeypatch.delenv(po.ENV_REGION, raising=False)
    avant = dict(REGION.REGIONS)
    assert po._forcer_region(demo=True) is None
    assert REGION.REGIONS == avant


def test_un_point_d_acces_inconnu_est_refuse_avec_la_liste(monkeypatch):
    """Pas de repli silencieux : une faute de frappe dans une variable
    d'environnement doit se voir au demarrage, pas se traduire par des heures
    de collecte muette sur le point d'acces par defaut."""
    monkeypatch.setenv(po.ENV_REGION, "EUROPPA")
    with pytest.raises(BotError) as capture:
        po._forcer_region(demo=True)
    assert "DEMO_2" in str(capture.value)


def test_le_point_d_acces_demande_ecrase_celui_que_la_bibliotheque_lira(
        monkeypatch):
    from pocketoptionapi import global_value
    from pocketoptionapi.constants import REGION

    monkeypatch.setattr(global_value, "DEMO", True, raising=False)
    monkeypatch.setattr(REGION, "REGIONS", dict(REGION.REGIONS))
    monkeypatch.setenv(po.ENV_REGION, "demo_2")

    assert "try-demo-eu" in po._forcer_region(demo=True)
    # La bibliotheque ne lit que REGIONS["DEMO"] pour un compte demo : c'est
    # cette entree, et pas une autre, qui doit avoir change.
    assert REGION.REGIONS["DEMO"] == REGION.REGIONS["DEMO_2"]
    assert "try-demo-eu" in REGION.REGIONS["DEMO"]


def test_un_compte_demo_ne_touche_jamais_l_entree_reelle(monkeypatch):
    """La regression qui a coute un aller-retour.

    `_forcer_region` lisait `global_value.DEMO`, qui vaut None tant que le
    client n'est pas construit : bool(None) est faux, on ecrasait EUROPA, et le
    client demo lisait DEMO restee intacte. Le journal annoncait pourtant
    « point d'acces force ».
    """
    from pocketoptionapi import global_value
    from pocketoptionapi.constants import REGION

    monkeypatch.setattr(REGION, "REGIONS", dict(REGION.REGIONS))
    monkeypatch.setattr(global_value, "DEMO", None, raising=False)
    monkeypatch.setenv(po.ENV_REGION, "DEMO_2")
    europa_avant = REGION.REGIONS["EUROPA"]

    po._forcer_region(demo=True)

    assert "try-demo-eu" in REGION.REGIONS["DEMO"], (
        "l'entree que la bibliotheque lira n'a pas ete changee"
    )
    assert REGION.REGIONS["EUROPA"] == europa_avant, (
        "l'entree du compte reel a ete ecrasee a la place"
    )


# --------------------------------------------------------------------------- #
# Le jeton factice : trois jours de collecte muette
# --------------------------------------------------------------------------- #
#
# Le jeton installe en production contenait un champ `session` de TROIS
# caracteres et `uid: 0`. Le broker acceptait la trame, diffusait son catalogue
# public -- 183 actifs, tout avait l'air normal -- et n'authentifiait rien. Zero
# tick, sans une seule erreur, depuis l'hebergeur comme depuis le poste.

GABARIT = '42["auth",{"session":"...","isDemo":1,"uid":0,"platform":2}]'


def test_un_gabarit_de_jeton_est_refuse():
    with pytest.raises(po.SessionExpiree, match="gabarit"):
        po.verifier_ssid(GABARIT)


def test_un_uid_nul_est_refuse():
    """« uid: 0 » veut dire : rattache a aucun compte."""
    jeton = '42["auth",{"session":"' + "a" * 40 + '","isDemo":1,"uid":0}]'
    with pytest.raises(po.SessionExpiree, match="uid"):
        po.verifier_ssid(jeton)


def test_un_jeton_sans_champ_session_est_refuse():
    with pytest.raises(po.SessionExpiree, match="tronqué"):
        po.verifier_ssid('42["auth",{"isDemo":1,"uid":7}]')


def test_un_jeton_bien_forme_passe():
    po.verifier_ssid(SSID_PLAUSIBLE)          # ne leve pas


def test_connect_refuse_un_gabarit_avant_d_ouvrir_le_socket(broker, monkeypatch):
    """Refuser AVANT d'ouvrir : une connexion qui a l'air de marcher est pire
    qu'un refus, parce qu'on la laisse tourner des jours."""
    client, _ = broker
    monkeypatch.setenv("POCKET_OPTION_SSID", GABARIT)
    src = PocketOptionSource(demo=True, delai_payouts_sec=1.0)

    with pytest.raises(po.SessionExpiree):
        src.connect()
    assert src._client is None, "le socket a ete ouvert malgre un jeton invalide"


def test_le_diagnostic_dit_si_le_compte_est_authentifie(broker, monkeypatch):
    """Le catalogue est public ; le solde ne l'est pas.

    C'est le seul signal qui distingue « connecte » de « connecte ET reconnu ».
    """
    client, globaux = broker
    monkeypatch.setenv("POCKET_OPTION_SSID", SSID_PLAUSIBLE)
    src = PocketOptionSource(demo=True, delai_payouts_sec=1.0)
    src.connect()

    assert src.diagnostic()["authentifie"] is False

    globaux.balance_updated = True
    assert src.authentifie() is True
    assert src.diagnostic()["authentifie"] is True


def test_l_absence_de_jeton_n_est_pas_une_panne_du_broker(broker, monkeypatch,
                                                          tmp_path):
    """Une configuration manquante ne doit pas etre reessayee en boucle.

    Traitee comme une indisponibilite passagere, elle faisait boucler le
    collecteur avec un backoff croissant -- et inscrivait des « echecs de
    connexion » au compte du broker, mis en penitence pour un fichier manquant
    chez nous. `SessionExpiree` remonte au lieu d'etre reessayee.
    """
    monkeypatch.delenv("POCKET_OPTION_SSID", raising=False)
    monkeypatch.setenv(po.ENV_FICHIER_SESSION, str(tmp_path / "absent.json"))
    src = PocketOptionSource(demo=True, delai_payouts_sec=1.0)

    with pytest.raises(po.SessionExpiree, match="capturer_ssid"):
        src.connect()
