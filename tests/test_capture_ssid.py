"""
L'outil de capture du SSID, sans ouvrir de fenêtre.

Un outil qu'on ne peut éprouver qu'en se connectant au broker n'est pas
éprouvé : on découvre ses fautes de frappe une par une, à raison d'un
aller-retour par erreur. Ces tests remplacent pywebview par un double et
vérifient les deux choses qui cassent en pratique.

**Les arguments passés à pywebview.** L'erreur qui a motivé ce fichier :
`private_mode` avait été passé à `create_window()` alors qu'il appartient à
`start()`. Rien ne le signale avant l'exécution, et l'exécution suppose une
connexion. `test_les_arguments_pywebview_sont_valides` lie les appels aux
signatures de la bibliothèque RÉELLEMENT installée, sans rien ouvrir.

**La construction du SSID.** Extraction de `demoSessionId` et `uid` depuis la
page du cabinet, et le cas où la page arrive sans eux — c'est-à-dire une
session non authentifiée, qu'il ne faut surtout pas confondre avec un succès.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import types
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]


def _charger_outil():
    """Charge le script comme un module. Il n'est pas dans un paquet : c'est un
    outil, pas une bibliothèque."""
    chemin = RACINE / "outils" / "capturer_ssid.py"
    spec = importlib.util.spec_from_file_location("capturer_ssid", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


outil = _charger_outil()


# --------------------------------------------------------------------------- #
# Les appels à pywebview
# --------------------------------------------------------------------------- #

pywebview_present = importlib.util.find_spec("webview") is not None


@pytest.mark.skipif(not pywebview_present,
                    reason="pywebview n'est installé qu'avec l'extra [pocketoption]")
def test_les_arguments_pywebview_sont_valides(monkeypatch, tmp_path):
    """Régression : `private_mode` passé à `create_window()` au lieu de `start()`.

    On lie les appels réels aux signatures de la pywebview installée. Une faute
    de frappe dans un mot-clé échoue ici, en une seconde, au lieu d'échouer
    devant l'utilisateur après l'ouverture d'une fenêtre.
    """
    import webview

    appels: dict[str, dict] = {}

    def create_window(*args, **kwargs):
        inspect.signature(webview.create_window).bind(*args, **kwargs)
        appels["create_window"] = kwargs
        return types.SimpleNamespace(destroy=lambda: None, get_cookies=lambda: [])

    def start(*args, **kwargs):
        inspect.signature(webview.start).bind(*args, **kwargs)
        appels["start"] = kwargs

    faux = types.ModuleType("webview")
    faux.create_window = create_window
    faux.start = start
    monkeypatch.setitem(sys.modules, "webview", faux)
    monkeypatch.setattr(outil, "ecrire_session", lambda *a, **k: tmp_path / "s.json",
                        raising=False)

    # Aucun SSID capturé : le script sort en échec, ce qui nous convient — le
    # test porte sur les appels, pas sur le résultat.
    assert outil.main(["--delai", "1"]) == 1

    assert "private_mode" not in appels["create_window"], (
        "private_mode appartient à start(), pas à create_window()"
    )
    assert appels["start"]["private_mode"] is False


@pytest.mark.skipif(not pywebview_present, reason="pywebview absent")
def test_nouvelle_session_ouvre_en_mode_prive(monkeypatch):
    """`private_mode=True` = aucun cookie conservé, donc page de connexion
    vierge. C'est ce qui permet de changer de compte ou de rafraîchir une
    session expirée."""
    import webview

    appels: dict[str, dict] = {}

    faux = types.ModuleType("webview")
    faux.create_window = lambda *a, **k: types.SimpleNamespace(
        destroy=lambda: None, get_cookies=lambda: [])
    def start(*args, **kwargs):
        inspect.signature(webview.start).bind(*args, **kwargs)
        appels["start"] = kwargs
    faux.start = start
    monkeypatch.setitem(sys.modules, "webview", faux)

    outil.main(["--delai", "1", "--nouvelle-session"])
    assert appels["start"]["private_mode"] is True


# --------------------------------------------------------------------------- #
# Construction du SSID
# --------------------------------------------------------------------------- #

PAGE_CONNECTEE = (
    '<html><script>var x = {"uid":123456,"demoSessionId":"abc123def",'
    '"balance":10000}</script></html>'
)
PAGE_ANONYME = "<html>Veuillez vous connecter</html>"


class FausseReponse:
    def __init__(self, texte, code=200):
        self.text = texte
        self.status_code = code


def test_ssid_construit_depuis_la_page_du_cabinet(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: FausseReponse(PAGE_CONNECTEE))
    ssid = outil._construire_ssid({"ci_session": "jeton"}, demo=True)

    assert ssid is not None
    assert '"session":"abc123def"' in ssid
    assert '"isDemo":1' in ssid
    assert '"uid":123456' in ssid


def test_page_sans_session_n_est_pas_un_succes(monkeypatch, capsys):
    """Le cas dangereux : la page répond 200 mais l'utilisateur n'est pas
    connecté. Retourner un SSID bâti sur du vide donnerait un jeton que le
    broker refuserait, et le diagnostic accuserait le socket."""
    import requests

    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: FausseReponse(PAGE_ANONYME))
    assert outil._construire_ssid({"ci_session": "jeton"}, demo=True) is None
    assert "connexion incomplète" in capsys.readouterr().out


def test_code_http_non_200_signale(monkeypatch, capsys):
    import requests

    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: FausseReponse("", code=403))
    assert outil._construire_ssid({"ci_session": "jeton"}, demo=True) is None
    assert "403" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Lecture des cookies
# --------------------------------------------------------------------------- #

class FauxCookie:
    def __init__(self, ligne):
        self._ligne = ligne

    def output(self):
        return self._ligne


def test_les_cookies_sont_lus_quel_que_soit_le_prefixe():
    """La bibliothèque fait `split("-Cookie: ")[1]`, qui lève sur toute
    variation de format du moteur de rendu. On découpe défensivement."""
    fenetre = types.SimpleNamespace(get_cookies=lambda: [
        FauxCookie("Set-Cookie: ci_session=abc; Path=/; HttpOnly"),
        FauxCookie("ci_autre=def; Path=/"),
        FauxCookie("  Set-Cookie: _scid = xyz ; Secure"),
    ])
    cookies = outil._cookies_de(fenetre)
    assert cookies["ci_session"] == "abc"
    assert cookies["ci_autre"] == "def"
    assert cookies["_scid"] == "xyz"


def test_une_fenetre_sans_cookies_ne_leve_pas():
    fenetre = types.SimpleNamespace(get_cookies=lambda: None)
    assert outil._cookies_de(fenetre) == {}

    def explose():
        raise RuntimeError("moteur pas prêt")

    assert outil._cookies_de(types.SimpleNamespace(get_cookies=explose)) == {}


def test_le_ssid_est_masque_a_l_affichage():
    """L'outil ne doit pas cracher un jeton de session complet dans un terminal
    dont l'historique traîne."""
    ssid = '42["auth",{"session":"tres-long-jeton-secret","isDemo":1,"uid":1}]'
    masque = outil._masquer(ssid)
    assert "tres-long-jeton-secret" not in masque
    assert masque.startswith('42["auth"')
    assert str(len(ssid)) in masque
