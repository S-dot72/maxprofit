"""
Observation passive des messages du broker (diagnostic du 31/10) :
- le relais remet à l'écouteur les MÊMES messages, dans le même ordre, et
  laisse remonter les erreurs du socket ;
- les trois situations (plusieurs ticks, plusieurs paires, même paire
  répétée) sont comptées à part ;
- reçus → transmis : un élément reçu hors tête n'est pas transmis par une
  bibliothèque qui ne garde que la tête, et l'observation le montre ;
- l'adaptateur compte ce qu'il lit sans rien retirer du tampon ;
- une erreur désactive l'observation sans jamais remonter.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from maxprofit.collect import observation_flux as of
from maxprofit.collect.pocketoption import PocketOptionSource
from maxprofit.core.types import Candle
from maxprofit.live import diagnostic_paires as dp

T0 = 1_793_400_000


class _Horloge:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _trame(*elements) -> bytes:
    return json.dumps([list(e) for e in elements]).encode()


class _Socket:
    """Un socket factice : rend ses messages, puis lève s'il le faut."""

    def __init__(self, messages, erreur=None):
        self.messages = list(messages)
        self.erreur = erreur
        self.attribut = "délégué"

    def __aiter__(self):
        return self._iterer()

    async def _iterer(self):
        for m in self.messages:
            yield m
        if self.erreur is not None:
            raise self.erreur


class _ClientBibliotheque:
    """La logique de la bibliothèque pour les ticks, recopiée telle quelle
    (pocketoptionapi `ws/client.py` l. 215-222 et 269-270) : seul le premier
    élément d'un message est ajouté au tampon."""

    def __init__(self):
        self.updateStream = False
        self.updateClosedDeals = False
        self.pairs: dict[str, dict] = {}
        self.vus = []

    async def websocket_listener(self, ws):
        async for message in ws:
            self.vus.append(message)
            await self.on_message(message)

    async def on_message(self, message):
        if type(message) is bytes:
            message = json.loads(message.decode("utf-8"))
            if self.updateStream and isinstance(message, list):
                self.updateStream = False
                if len(message[0]) == 3:
                    paire = message[0][0]
                    self.pairs.setdefault(paire, {"ticks": []})
                    self.pairs[paire]["ticks"].append(
                        {"time": message[0][1], "price": message[0][2]})
        elif message.startswith('451-["updateStream"'):
            self.updateStream = True


def _ecouter(client, messages, observation, erreur=None):
    ws = _Socket(messages, erreur)
    asyncio.run(client.websocket_listener(
        of.TramesObservees(ws, client, observation)))
    return ws


ARME = '451-["updateStream",{"_placeholder":true,"num":0}]'


def test_le_relais_rend_les_memes_messages_dans_le_meme_ordre():
    observation = of.ObservationFlux(_Horloge())
    client = _ClientBibliotheque()
    messages = [ARME, _trame(("A_otc", T0, 1.1)), b'{"balance": 3}', "2"]
    _ecouter(client, messages, observation)
    assert len(client.vus) == len(messages)
    assert all(a is b for a, b in zip(client.vus, messages))
    relais = of.TramesObservees(_Socket([]), client, observation)
    assert relais.attribut == "délégué"


def test_le_relais_laisse_remonter_les_erreurs_du_socket():
    observation = of.ObservationFlux(_Horloge())
    client = _ClientBibliotheque()
    with pytest.raises(ConnectionError):
        _ecouter(client, [ARME, _trame(("A_otc", T0, 1.1))], observation,
                 erreur=ConnectionError("fermé"))
    assert len(client.vus) == 2 and observation.actif


def test_les_trois_situations_sont_comptees_a_part():
    horloge = _Horloge()
    observation = of.ObservationFlux(horloge)
    client = _ClientBibliotheque()
    _ecouter(client, [
        ARME, _trame(("A_otc", T0, 1.1)),                       # un tick
        ARME, _trame(("A_otc", T0, 1.1), ("B_otc", T0, 2.2)),   # deux paires
        ARME, _trame(("C_otc", T0, 3.3), ("C_otc", T0 + 1, 3.4)),  # répétée
    ], observation)
    obs = observation.instantane(T0 - 60, T0 + 60)
    m = obs["messages"]
    assert (m["messages"], m["plusieurs_ticks"], m["plusieurs_paires"],
            m["meme_paire_repetee"], m["max_elements"]) == (3, 2, 1, 1, 2)
    assert obs["paires"]["A_otc"] == {"recus": 2, "messages": 2,
                                      "en_tete": 2, "transmis": 0}
    assert obs["paires"]["B_otc"] == {"recus": 1, "messages": 1,
                                      "en_tete": 0, "transmis": 0}
    assert obs["paires"]["C_otc"]["recus"] == 2
    assert obs["paires"]["C_otc"]["en_tete"] == 1
    assert obs["sockets"] == 1


def test_recus_contre_transmis_montre_ce_que_la_bibliotheque_ne_garde_pas():
    """Le cas que le diagnostic doit trancher : B est reçue, jamais en tête,
    et l'adaptateur ne la trouve jamais dans le tampon."""
    observation = of.ObservationFlux(_Horloge())
    client = _ClientBibliotheque()
    base = int(time.time())      # l'adaptateur vérifie l'horloge du broker
    messages = []
    for i in range(10):
        messages += [ARME, _trame(("A_otc", base + i, 1.1),
                                  ("B_otc", base + i, 2.2))]
    _ecouter(client, messages, observation)
    source = PocketOptionSource(demo=True)
    source._client = SimpleNamespace(
        GetTicks=lambda nom: client.pairs.get(nom, {}).get("ticks"),
        check_connect=lambda: True)
    source._souscrites = ["A_otc", "B_otc"]
    source._decalage_sec = 0
    source._unite = "sec"
    import maxprofit.collect.pocketoption as po
    ancien, po.OBSERVATION = po.OBSERVATION, observation
    try:
        lus = {p: len(list(source._drainer(p))) for p in ("A_otc", "B_otc")}
    finally:
        po.OBSERVATION = ancien
    assert lus == {"A_otc": 10, "B_otc": 0}
    obs = observation.instantane(T0 - 60, T0 + 60)
    assert obs["paires"]["A_otc"]["transmis"] == 10
    assert obs["paires"]["B_otc"] == {"recus": 10, "messages": 10,
                                      "en_tete": 0, "transmis": 0}
    # Le tampon de la bibliothèque n'a rien perdu par l'observation.
    assert len(client.pairs["A_otc"]["ticks"]) == 10


def test_un_message_hors_drapeau_est_compte_et_signale():
    observation = of.ObservationFlux(_Horloge())
    client = _ClientBibliotheque()
    _ecouter(client, [_trame(("A_otc", T0, 1.1))], observation)
    obs = observation.instantane(T0 - 60, T0 + 60)
    assert obs["messages"]["hors_drapeau"] == 1
    assert obs["paires"]["A_otc"]["en_tete"] == 0
    assert "A_otc" not in client.pairs


def test_les_messages_qui_ne_sont_pas_des_ticks_sont_ignores():
    observation = of.ObservationFlux(_Horloge())
    client = _ClientBibliotheque()
    _ecouter(client, [b'{"balance": 3}', "40", b"[1, 2]"], observation)
    assert observation.instantane(T0 - 60, T0 + 60)["messages"][
        "messages"] == 0
    client.updateClosedDeals = True
    observation.trame(client, _trame(("A_otc", T0, 1.1)))
    assert observation.instantane(T0 - 60, T0 + 60)["messages"][
        "messages"] == 0, "lu comme des ordres par la bibliothèque"


def test_une_erreur_desactive_l_observation_sans_remonter():
    observation = of.ObservationFlux(_Horloge())
    client = SimpleNamespace(updateStream=True, updateClosedDeals=False)
    observation.trame(client, b"pas du json")
    assert not observation.actif and "JSONDecodeError" in observation.erreur
    observation.trame(client, _trame(("A_otc", T0, 1.1)))
    observation.lus("A_otc", 3)
    assert observation.instantane(T0 - 60, T0 + 60)["paires"] == {}
    texte = dp.texte_observation(observation.instantane(T0, T0 + 60), {},
                                 ["A_otc"])
    assert "DÉSACTIVÉE" in texte


def test_la_memoire_est_bornee():
    horloge = _Horloge()
    observation = of.ObservationFlux(horloge)
    client = SimpleNamespace(updateStream=True, updateClosedDeals=False)
    for k in range(200):
        horloge.t = T0 + 60 * k
        observation.trame(client, _trame(("A_otc", horloge.t, 1.1)))
    assert len(observation._minutes) <= of.MINUTES_GARDEES + 1
    assert observation.instantane(0, 2 ** 40)["cumul_paires"]["A_otc"][
        "recus"] == 200


def test_le_texte_de_l_etat_compare_les_trois_niveaux():
    observation = of.ObservationFlux(_Horloge(T0))
    client = SimpleNamespace(updateStream=True, updateClosedDeals=False)
    for i in range(5):
        observation.trame(client, _trame(("A_otc", T0, 1.1),
                                         ("B_otc", T0, 2.2)))
    observation.lus("A_otc", 5)
    obs = observation.instantane(T0 // 60 * 60, T0 // 60 * 60 + 60)
    texte = dp.texte_observation(obs, {"A_otc": 5, "B_otc": 0},
                                 ["A_otc", "B_otc"])
    assert ("- B_otc : reçus 5 (dans 5 messages, en tête 0) → transmis 0 → "
            "enregistrés 0 · ⚠ 5 reçus non transmis") in texte
    assert "- A_otc : reçus 5 (dans 5 messages, en tête 5) → transmis 5 → " \
        "enregistrés 5" in texte
    assert "plusieurs paires : 5 (100 %)" in texte
    assert texte.index("B_otc") < texte.index("- A_otc")


def test_la_fenetre_et_les_ticks_enregistres():
    debut, fin = dp.fenetre_d_observation(T0 + 30, depuis=T0 - 7200)
    assert fin == (T0 + 30) // 60 * 60 - 120 and fin - debut == 3600
    debut, fin = dp.fenetre_d_observation(T0 + 30, depuis=T0 - 600)
    assert debut == ((T0 - 600) // 60 + 1) * 60
    cache = {"A_otc": [Candle(pair="A_otc", tf_sec=60, ts_sec=debut + 60 * k,
                              open=1, high=1, low=1, close=1, tick_count=7,
                              complete=True) for k in range(3)]}
    assert dp.enregistres(cache, ["A_otc", "B_otc"], debut, fin) == {
        "A_otc": 21, "B_otc": 0}


def test_l_observation_de_l_etat_ne_fait_jamais_tomber_etat():
    from maxprofit.hosting.service import _observation_des_messages
    assert _observation_des_messages(SimpleNamespace(_course=None)) is None
    course = SimpleNamespace(_course=SimpleNamespace(
        paires_collectees=("A_otc",), _cache_bougies={}))
    texte = _observation_des_messages(course)
    assert texte is not None and "Messages de ticks du broker" in texte


def test_le_cout_par_message_reste_negligeable():
    observation = of.ObservationFlux()
    client = SimpleNamespace(updateStream=True, updateClosedDeals=False)
    message = _trame(("A_otc", T0, 1.1), ("B_otc", T0, 2.2))
    debut = time.perf_counter()
    for _ in range(5000):
        observation.trame(client, message)
    assert (time.perf_counter() - debut) / 5000 < 2e-4, "moins de 0,2 ms"
