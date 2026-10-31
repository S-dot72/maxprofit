"""
Correction du 31/10 : le serveur regroupe les ticks de toutes les paires dans
un même message, et la bibliothèque n'en gardait que le premier élément.
`pocketoption.lecture_du_lot_entier` lit le lot entier ; tout le reste de la
bibliothèque est inchangé.

Le client factice reprend TELLE QUELLE la logique de la bibliothèque pour les
messages de ticks (pocketoptionapi `ws/client.py` l. 199-222 et 247-277,
commit c94c2b0) : c'est elle que la correction enveloppe.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import maxprofit.collect.pocketoption as po
from maxprofit.collect import observation_flux as of

ARME = '451-["updateStream",{"_placeholder":true,"num":0}]'
CLOS = '451-["updateClosedDeals",{"_placeholder":true,"num":0}]'


def _client(corrige: bool, pairs: dict | None = None):
    gv = SimpleNamespace(pairs={} if pairs is None else pairs)

    class Client:
        def __init__(self):
            self.updateStream = False
            self.updateClosedDeals = False
            self.api = SimpleNamespace(time_sync=SimpleNamespace(
                server_timestamp=None))
            self.balance = None
            self.closed_deals = None

        async def websocket_listener(self, ws):
            async for message in ws:
                await self.on_message(message)

        async def on_message(self, message):
            if type(message) is bytes:
                message = json.loads(message.decode("utf-8"))
                if "balance" in message:
                    self.balance = message["balance"]
                elif ("requestId" in message
                      and message["requestId"] == "buy"):
                    pass
                elif self.updateClosedDeals and isinstance(message, list):
                    self.closed_deals = message
                    self.updateClosedDeals = False
                elif self.updateStream and isinstance(message, list):
                    self.updateStream = False
                    if len(message[0]) == 3:
                        self.api.time_sync.server_timestamp = message[0][1]
                        if not message[0][0] in gv.pairs:
                            gv.pairs[message[0][0]] = {"ticks": [],
                                                       "history": []}
                        h = {"time": message[0][1], "price": message[0][2]}
                        gv.pairs[message[0][0]]["ticks"].append(h)
                return None
            if message.startswith("451-["):
                contenu = json.loads(message.split("-", 1)[1])
                if contenu[0] == "updateStream":
                    self.updateStream = True
                elif contenu[0] == "updateClosedDeals":
                    self.updateClosedDeals = True
            return None

    if corrige:
        Client.on_message = po.lecture_du_lot_entier(Client.on_message, gv)
    return Client(), gv


def _lot(*elements) -> bytes:
    return json.dumps([list(e) for e in elements]).encode()


def _recevoir(client, messages):
    async def tout():
        for m in messages:
            await client.on_message(m)
    asyncio.run(tout())


def _ticks(gv, paire):
    return gv.pairs.get(paire, {}).get("ticks", [])


def test_sans_correction_seule_la_tete_du_lot_est_gardee():
    client, gv = _client(corrige=False)
    _recevoir(client, [ARME, _lot(("A_otc", 10, 1.1), ("B_otc", 11, 2.2),
                                  ("C_otc", 12, 3.3))])
    assert [len(_ticks(gv, p)) for p in ("A_otc", "B_otc", "C_otc")] == \
        [1, 0, 0]


def test_le_lot_entier_est_ajoute_au_tampon():
    client, gv = _client(corrige=True)
    _recevoir(client, [ARME, _lot(("A_otc", 10, 1.1), ("B_otc", 12.5, 2.2),
                                  ("C_otc", 11, 3.3))])
    assert _ticks(gv, "A_otc") == [{"time": 10, "price": 1.1}]
    assert _ticks(gv, "B_otc") == [{"time": 12.5, "price": 2.2}]
    assert _ticks(gv, "C_otc") == [{"time": 11, "price": 3.3}]
    assert client.updateStream is False, "drapeau remis à faux"
    assert client.api.time_sync.server_timestamp == 12.5, "le plus récent"


def test_un_message_d_un_seul_tick_reste_traite_comme_avant():
    avant, gv_avant = _client(corrige=False)
    apres, gv_apres = _client(corrige=True)
    messages = [ARME, _lot(("A_otc", 10, 1.1)), ARME, _lot(("A_otc", 11, 1.2))]
    _recevoir(avant, messages)
    _recevoir(apres, messages)
    assert gv_avant.pairs == gv_apres.pairs
    assert (avant.api.time_sync.server_timestamp
            == apres.api.time_sync.server_timestamp == 11)


def test_les_autres_messages_restent_a_la_bibliotheque():
    client, gv = _client(corrige=True)
    _recevoir(client, [b'{"balance": 75.3, "isDemo": 1}'])
    assert client.balance == 75.3
    # Les ordres clos passent AVANT les ticks, comme dans la bibliothèque.
    _recevoir(client, [ARME, CLOS, _lot(("A_otc", 10, 1.1),
                                        ("B_otc", 11, 2.2))])
    assert client.closed_deals == [["A_otc", 10, 1.1], ["B_otc", 11, 2.2]]
    assert gv.pairs == {}
    # Un lot sans drapeau est ignoré, comme avant.
    client.updateStream = False
    _recevoir(client, [_lot(("A_otc", 10, 1.1))])
    assert gv.pairs == {}


def test_un_message_illisible_leve_comme_avant():
    client, _ = _client(corrige=True)
    client.updateStream = True
    with pytest.raises(json.JSONDecodeError):
        _recevoir(client, [b"pas du json"])


def test_les_elements_de_forme_inattendue_sont_ignores():
    client, gv = _client(corrige=True)
    _recevoir(client, [ARME, _lot(
        ("A_otc", 10, 1.1), ("B_otc", 10), (1, 2, 3), ("C_otc", "x", 1.0),
        ("D_otc", 10, 1.1, 9), ("E_otc", True, 1.0), ("F_otc", 11, 1.3))])
    assert sorted(gv.pairs) == ["A_otc", "F_otc"]
    ajoutes, ignores, recent = po.ajouter_les_ticks_du_lot(
        [["A_otc", 1, 1.0], "texte", 7, ["B_otc", 2, None]], {})
    assert (ajoutes, ignores, recent) == (1, 3, 1)


def test_le_tampon_reste_la_meme_liste():
    """L'adaptateur garde sa position de lecture dans CETTE liste."""
    existante = [{"time": 1, "price": 1.0}]
    client, gv = _client(corrige=True, pairs={
        "A_otc": {"ticks": existante, "history": []},
        "B_otc": {"history": [1, 2]}})
    _recevoir(client, [ARME, _lot(("A_otc", 2, 1.1), ("B_otc", 2, 2.2),
                                  ("A_otc", 3, 1.2))])
    assert gv.pairs["A_otc"]["ticks"] is existante
    assert [t["time"] for t in existante] == [1, 2, 3], "dans l'ordre du lot"
    assert gv.pairs["B_otc"] == {"history": [1, 2],
                                 "ticks": [{"time": 2, "price": 2.2}]}


def test_la_correction_ne_s_installe_qu_une_fois():
    client, gv = _client(corrige=True)
    assert getattr(type(client).on_message, "_maxprofit", False)


def test_de_bout_en_bout_recus_egale_transmis():
    """Le relais, la bibliothèque corrigée et l'adaptateur réel : tout ce
    qui est reçu est transmis, et le tampon n'est pas entamé."""
    client, gv = _client(corrige=True)
    observation = of.ObservationFlux()
    base = int(time.time())      # l'adaptateur vérifie l'horloge du broker
    paires = ["A_otc", "B_otc", "C_otc"]
    messages = []
    for i in range(20):
        messages += [ARME, _lot(*((p, base + i + k / 10, 1.1 + k)
                                  for k, p in enumerate(paires)))]

    class _Socket:
        def __aiter__(self):
            return self._iterer()

        async def _iterer(self):
            for m in messages:
                yield m

    asyncio.run(client.websocket_listener(
        of.TramesObservees(_Socket(), client, observation)))
    source = po.PocketOptionSource(demo=True)
    source._client = SimpleNamespace(
        GetTicks=lambda nom: gv.pairs.get(nom, {}).get("ticks"),
        check_connect=lambda: True)
    source._souscrites = list(paires)
    source._decalage_sec = 0
    source._unite = "sec"
    ancien, po.OBSERVATION = po.OBSERVATION, observation
    try:
        lus = {p: len(list(source._drainer(p))) for p in paires}
    finally:
        po.OBSERVATION = ancien
    assert lus == {p: 20 for p in paires}
    obs = observation.instantane(0, 2 ** 40)
    for p in paires:
        assert obs["paires"][p]["recus"] == obs["paires"][p]["transmis"] == 20
    assert obs["messages"]["plusieurs_paires"] == 20
