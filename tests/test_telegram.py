"""
Le bot d'exploitation et le renouvellement du jeton, sans réseau.

Trois choses comptent ici, et ce sont les trois qu'un test doit protéger.

**La liste blanche.** `TELEGRAM_CHAT_ID` n'est pas une destination, c'est une
autorisation. Sans elle, quiconque découvre le bot peut lui injecter un SSID —
c'est-à-dire détourner la collecte vers un autre compte — ou lire l'état de
l'infrastructure. C'est la faille la plus grave que ce module puisse avoir.

**L'effacement du jeton.** Un SSID envoyé par Telegram reste dans l'historique
de la conversation. Le bot doit effacer le message, et l'effacer AVANT de
répondre.

**La reprise de la collecte.** Tout ceci ne sert à rien si un jeton reçu ne
relance pas effectivement le collecteur.
"""

from __future__ import annotations

import asyncio

import pytest

from maxprofit.core.errors import BotError
from pathlib import Path

from maxprofit.hosting.telegram import BotExploitation, ClientTelegram

CHAT = "12345"
INTRUS = "99999"
JETON_VALIDE = '42["auth",{"session":"abc","isDemo":1,"uid":1,"platform":2}]'


class FauxClient:
    """Enregistre ce qui aurait été envoyé, sans réseau."""

    def __init__(self):
        self.envoyes: list[tuple[str, str]] = []
        self.effaces: list[tuple[str, int]] = []

    async def envoyer(self, chat_id, texte, clavier=None):
        self.envoyes.append((str(chat_id), texte))

    async def effacer(self, chat_id, message_id):
        self.effaces.append((str(chat_id), message_id))


def _message(texte: str, chat: str = CHAT, message_id: int = 7) -> dict:
    return {"message": {"chat": {"id": chat}, "text": texte,
                        "message_id": message_id}}


def _annuaire(tmp, inscrits=((CHAT, "admin"),)):
    from maxprofit.hosting.operateurs import Annuaire, Role

    a = Annuaire(tmp / "operateurs.json")
    for chat, role in inscrits:
        a.inscrire(chat, Role(role))
    return a


def _bot(client, *, etat="état", installer=None, annuaire=None, paires=None):
    async def _etat():
        return etat

    async def _installer(jeton):
        if installer is None:
            return f"installé:{jeton[:12]}"
        return await installer(jeton)

    async def _paires():
        return paires or "paires"

    if annuaire is None:
        import tempfile
        annuaire = _annuaire(Path(tempfile.mkdtemp()))
    return BotExploitation(client, annuaire, etat=_etat,
                           installer_jeton=_installer, paires=_paires)


def _traiter(bot, message):
    asyncio.run(bot._traiter(message["message"]))


# --------------------------------------------------------------------------- #
# Liste blanche
# --------------------------------------------------------------------------- #

def test_un_chat_non_autorise_est_ignore(caplog):
    """LE test de ce fichier.

    Sans cette vérification, n'importe qui découvrant le bot pourrait lui
    envoyer un SSID et détourner la collecte vers son propre compte."""
    client = FauxClient()
    bot = _bot(client)

    with caplog.at_level("WARNING"):
        _traiter(bot, _message(f"/ssid {JETON_VALIDE}", chat=INTRUS))

    # Il reçoit la marche à suivre pour s'inscrire, et RIEN d'autre : ni état,
    # ni paires, ni indice sur ce que fait le bot.
    assert len(client.envoyes) == 1
    destinataire, texte = client.envoyes[0]
    assert destinataire == INTRUS
    assert "code d'accès" in texte or "Code" in texte
    assert "collecte" not in texte.lower() or "privée" in texte
    assert client.effaces == []
    assert any("n'est pas inscrit" in m for m in caplog.messages)


def test_un_intrus_ne_peut_pas_lire_l_etat():
    client = FauxClient()
    _traiter(_bot(client, etat="🟢 secret"), _message("/etat", chat=INTRUS))
    assert all("secret" not in t for _, t in client.envoyes)


def test_le_chat_autorise_est_servi():
    client = FauxClient()
    _traiter(_bot(client, etat="🟢 tout va bien"), _message("/etat"))
    assert client.envoyes == [(CHAT, "🟢 tout va bien")]


# --------------------------------------------------------------------------- #
# Le jeton ne traîne pas
# --------------------------------------------------------------------------- #

def test_le_message_portant_le_jeton_est_efface():
    """Un SSID reste sinon dans l'historique de la conversation. L'effacer ne
    l'ôte pas des serveurs de Telegram — raison de plus pour n'utiliser qu'un
    compte de démonstration — mais il ne doit pas rester affiché."""
    client = FauxClient()
    _traiter(_bot(client), _message(f"/ssid {JETON_VALIDE}", message_id=42))
    assert client.effaces == [(CHAT, 42)]


def test_le_jeton_n_est_jamais_renvoye_dans_une_reponse():
    client = FauxClient()
    _traiter(_bot(client), _message(f"/ssid {JETON_VALIDE}"))
    for _, texte in client.envoyes:
        assert "abc" not in texte, "le jeton a été renvoyé en clair"


def test_un_jeton_refuse_donne_la_raison():
    async def _refuser(jeton):
        raise BotError("Jeton sans champ « session » : il est tronqué.")

    client = FauxClient()
    _traiter(_bot(client, installer=_refuser), _message("/ssid 42[tronque"))
    assert "tronqué" in client.envoyes[-1][1]


def test_ssid_sans_argument_affiche_les_instructions():
    client = FauxClient()
    _traiter(_bot(client), _message("/ssid"))
    assert "capturer_ssid" in client.envoyes[-1][1]


# --------------------------------------------------------------------------- #
# Menu
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("commande", ["/start", "/aide", "bonjour", "❓ Aide"])
def test_le_menu_repond_toujours_quelque_chose(commande):
    client = FauxClient()
    _traiter(_bot(client), _message(commande))
    assert client.envoyes, f"aucune réponse à {commande!r}"


def test_le_bouton_etat_equivaut_a_la_commande():
    client = FauxClient()
    bot = _bot(client, etat="🟢 ok")
    _traiter(bot, _message("📊 État"))
    assert client.envoyes[-1][1] == "🟢 ok"


def test_le_bouton_renouveler_explique_la_marche_a_suivre():
    client = FauxClient()
    _traiter(_bot(client), _message("🔑 Renouveler le jeton"))
    assert "capturer_ssid" in client.envoyes[-1][1]


def test_un_message_vide_ne_fait_rien():
    client = FauxClient()
    asyncio.run(_bot(client)._traiter({"chat": {"id": CHAT}}))
    assert client.envoyes == []


# --------------------------------------------------------------------------- #
# Alertes
# --------------------------------------------------------------------------- #

def test_une_alerte_part_vers_TOUS_les_operateurs(tmp_path):
    """Une panne de collecte n'est pas confidentielle pour qui a déjà le droit
    de consulter l'état : restreindre les alertes aux administrateurs ferait
    manquer l'essentiel à ceux qui surveillent."""
    client = FauxClient()
    annuaire = _annuaire(tmp_path, [("111", "admin"), ("222", "observateur")])
    asyncio.run(_bot(client, annuaire=annuaire).alerter("panne"))
    assert {c for c, _ in client.envoyes} == {"111", "222"}


def test_un_destinataire_injoignable_n_empeche_pas_les_autres(tmp_path):
    """Un compte bloqué ou supprimé ne doit pas priver les autres de l'alerte."""
    class ClientPartiel(FauxClient):
        async def envoyer(self, chat_id, texte, clavier=None):
            if str(chat_id) == "111":
                raise RuntimeError("bot bloqué par l'utilisateur")
            await super().envoyer(chat_id, texte, clavier)

    client = ClientPartiel()
    annuaire = _annuaire(tmp_path, [("111", "admin"), ("222", "observateur")])
    asyncio.run(_bot(client, annuaire=annuaire).alerter("panne"))
    assert [c for c, _ in client.envoyes] == ["222"]


def test_une_alerte_sans_destinataire_est_signalee(tmp_path, caplog):
    """Personne d'inscrit = personne à prévenir. Le message serait perdu en
    silence, y compris celui annonçant l'expiration du jeton."""
    client = FauxClient()
    annuaire = _annuaire(tmp_path, [])
    with caplog.at_level("WARNING"):
        asyncio.run(_bot(client, annuaire=annuaire).alerter("collecte arrêtée"))
    assert client.envoyes == []
    assert any("sans destinataire" in m for m in caplog.messages)


def test_une_alerte_qui_echoue_n_emporte_pas_le_processus():
    """Une alerte qui ne part pas ne doit pas tuer le processus qu'elle
    signale : ce serait remplacer une panne visible par une panne muette."""
    class ClientCasse(FauxClient):
        async def envoyer(self, *a, **k):
            raise RuntimeError("Telegram injoignable")

    bot = _bot(ClientCasse())
    asyncio.run(bot.alerter("panne"))     # ne doit pas lever


def test_le_client_signale_un_refus_de_telegram():
    """L'API répond 200 avec `ok: false` : sans contrôle, une erreur passerait
    pour un envoi réussi."""
    class FausseReponse:
        content_type = "application/json"

        async def json(self):
            return {"ok": False, "description": "chat not found"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FausseSession:
        def post(self, *a, **k):
            return FausseReponse()

    client = ClientTelegram("jeton", FausseSession())
    with pytest.raises(RuntimeError, match="chat not found"):
        asyncio.run(client.envoyer(CHAT, "coucou"))


# --------------------------------------------------------------------------- #
# Inscription et rôles
# --------------------------------------------------------------------------- #

def test_un_inconnu_s_inscrit_avec_le_bon_code(tmp_path, monkeypatch):
    from maxprofit.hosting.operateurs import ENV_CODE_ADMIN, Role

    monkeypatch.setenv(ENV_CODE_ADMIN, "sesame")
    annuaire = _annuaire(tmp_path, [])
    client = FauxClient()

    _traiter(_bot(client, annuaire=annuaire),
             _message("/start sesame", chat=INTRUS, message_id=5))

    assert annuaire.role(INTRUS) is Role.ADMIN
    assert "Inscrit" in client.envoyes[-1][1]
    # Le code est effacé comme un jeton : il finirait sinon transféré.
    assert client.effaces == [(INTRUS, 5)]


def test_un_mauvais_code_n_inscrit_personne(tmp_path, monkeypatch, caplog):
    from maxprofit.hosting.operateurs import ENV_CODE_ADMIN

    monkeypatch.setenv(ENV_CODE_ADMIN, "sesame")
    annuaire = _annuaire(tmp_path, [])
    client = FauxClient()

    with caplog.at_level("WARNING"):
        _traiter(_bot(client, annuaire=annuaire),
                 _message("/start ouvre-toi", chat=INTRUS))

    assert not annuaire.est_inscrit(INTRUS)
    assert "invalide" in client.envoyes[-1][1]
    assert any("refusé" in m for m in caplog.messages)


def test_un_observateur_ne_peut_pas_installer_de_jeton(tmp_path):
    """LE test des rôles. Installer un jeton, c'est choisir quel compte de
    courtier est collecté."""
    installes = []

    async def _installer(jeton):
        installes.append(jeton)
        return "installé"

    annuaire = _annuaire(tmp_path, [("777", "observateur")])
    client = FauxClient()

    _traiter(_bot(client, annuaire=annuaire, installer=_installer),
             _message(f"/ssid {JETON_VALIDE}", chat="777", message_id=9))

    assert installes == [], "un observateur a pu changer le compte collecté"
    assert "administrateurs" in client.envoyes[-1][1]
    # Le jeton est quand même effacé : il ne doit pas rester affiché.
    assert client.effaces == [("777", 9)]


def test_un_observateur_peut_consulter(tmp_path):
    annuaire = _annuaire(tmp_path, [("777", "observateur")])
    client = FauxClient()
    _traiter(_bot(client, annuaire=annuaire, etat="🟢 ok"),
             _message("/etat", chat="777"))
    assert client.envoyes[-1][1] == "🟢 ok"


def test_un_observateur_ne_voit_pas_la_liste_des_operateurs(tmp_path):
    annuaire = _annuaire(tmp_path, [("777", "observateur"), ("111", "admin")])
    client = FauxClient()
    _traiter(_bot(client, annuaire=annuaire), _message("/operateurs", chat="777"))
    assert "administrateurs" in client.envoyes[-1][1]
    assert "111" not in client.envoyes[-1][1]


def test_un_admin_liste_et_revoque(tmp_path):
    annuaire = _annuaire(tmp_path, [(CHAT, "admin"), ("222", "observateur")])
    client = FauxClient()
    bot = _bot(client, annuaire=annuaire)

    _traiter(bot, _message("/operateurs"))
    assert "222" in client.envoyes[-1][1]

    _traiter(bot, _message("/revoquer 222"))
    assert not annuaire.est_inscrit("222")


def test_un_admin_ne_peut_pas_se_revoquer_lui_meme(tmp_path):
    """Se retirer soi-même pourrait laisser le bot sans aucun administrateur,
    donc sans personne pour renouveler le jeton."""
    annuaire = _annuaire(tmp_path, [(CHAT, "admin")])
    client = FauxClient()
    _traiter(_bot(client, annuaire=annuaire), _message(f"/revoquer {CHAT}"))
    assert annuaire.est_inscrit(CHAT)
    assert "propre accès" in client.envoyes[-1][1]


def test_start_sans_code_rappelle_le_role_a_un_inscrit(tmp_path):
    annuaire = _annuaire(tmp_path, [(CHAT, "admin")])
    client = FauxClient()
    _traiter(_bot(client, annuaire=annuaire), _message("/start"))
    assert "admin" in client.envoyes[-1][1]
