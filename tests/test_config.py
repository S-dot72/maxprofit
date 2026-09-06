"""
Spec §5 : « Absence de configuration = arrêt, pas valeur par défaut. »

Ce test existe parce que la tentation de remettre un défaut « juste pour que ça
tourne en local » réapparaît à chaque refactor.
"""

from __future__ import annotations

import os

import pytest

from maxprofit.core import config
from maxprofit.core.errors import ConfigurationError


def test_variable_absente_leve(monkeypatch):
    monkeypatch.delenv("UNE_VARIABLE_QUI_N_EXISTE_PAS", raising=False)
    with pytest.raises(ConfigurationError, match="absente ou vide"):
        config.require_env("UNE_VARIABLE_QUI_N_EXISTE_PAS")


def test_variable_vide_traitee_comme_absente(monkeypatch):
    monkeypatch.setenv("X_VIDE", "   ")
    with pytest.raises(ConfigurationError):
        config.require_env("X_VIDE")


def test_pas_de_fonction_avec_defaut():
    # Il n'existe volontairement aucun `get_env(name, default=...)` : une
    # fonction qui n'existe pas ne peut pas être appelée par réflexe.
    for nom in dir(config):
        fn = getattr(config, nom)
        if callable(fn) and not nom.startswith("_"):
            import inspect
            try:
                sig = inspect.signature(fn)
            except (TypeError, ValueError):
                continue
            for p in sig.parameters.values():
                if p.name in ("default", "fallback"):
                    pytest.fail(f"config.{nom} expose un paramètre {p.name}")


def test_bornes(monkeypatch):
    monkeypatch.setenv("N", "5")
    assert config.require_int_env("N", minimum=1, maximum=10) == 5
    with pytest.raises(ConfigurationError, match="inférieur"):
        config.require_int_env("N", minimum=6)
    with pytest.raises(ConfigurationError, match="supérieur"):
        config.require_int_env("N", maximum=4)


def test_valeur_non_numerique(monkeypatch):
    monkeypatch.setenv("N", "quatre-vingt-douze")
    with pytest.raises(ConfigurationError, match="entier"):
        config.require_int_env("N")


def test_db_path_absent_leve(monkeypatch):
    monkeypatch.delenv(config.ENV_DB_PATH, raising=False)
    with pytest.raises(ConfigurationError, match=config.ENV_DB_PATH):
        config.db_path()


def test_db_path_relatif_refuse(monkeypatch):
    # C'est exactement le défaut du collecteur actuel ("market_data.db") :
    # la base se crée à côté du code et un déploiement l'écrase (§1.1).
    monkeypatch.setenv(config.ENV_DB_PATH, "market_data.db")
    with pytest.raises(ConfigurationError, match="relatif"):
        config.db_path()


def test_db_path_repertoire_inexistant_refuse(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_DB_PATH, str(tmp_path / "faute_de_frappe" / "m.db"))
    with pytest.raises(ConfigurationError, match="n'existe pas"):
        config.db_path()


def test_db_path_valide(monkeypatch, tmp_path):
    cible = tmp_path / "market.db"
    monkeypatch.setenv(config.ENV_DB_PATH, str(cible))
    assert config.db_path() == cible
    assert config.backups_dir() == tmp_path / "backups"


# --------------------------------------------------------------------------- #
# Chargement de .env
# --------------------------------------------------------------------------- #

def test_charge_les_cles_du_fichier(tmp_path, monkeypatch):
    fichier = tmp_path / ".env"
    fichier.write_text(
        "# un commentaire\n"
        "\n"
        "MIN_PAYOUT_PCT=92\n"
        "  TRADING_DB_PATH = /data/market.db  \n"
        'POCKET_OPTION_SSID="42[\\"auth\\",{}]"\n',
        encoding="utf-8",
    )
    for cle in ("MIN_PAYOUT_PCT", "TRADING_DB_PATH", "POCKET_OPTION_SSID"):
        monkeypatch.delenv(cle, raising=False)

    definies = config.charger_env_local(fichier)

    assert set(definies) == {"MIN_PAYOUT_PCT", "TRADING_DB_PATH", "POCKET_OPTION_SSID"}
    assert os.environ["MIN_PAYOUT_PCT"] == "92"
    assert os.environ["TRADING_DB_PATH"] == "/data/market.db"
    assert os.environ["POCKET_OPTION_SSID"] == '42[\\"auth\\",{}]'


def test_l_environnement_reel_l_emporte_sur_le_fichier(tmp_path, monkeypatch):
    """En hébergement, la plateforme injecte ses propres valeurs. Un `.env`
    resté dans l'image ne doit pas les écraser en silence — c'est le genre
    d'erreur qui fait pointer la base sur un chemin périmé."""
    fichier = tmp_path / ".env"
    fichier.write_text("MIN_PAYOUT_PCT=50\n", encoding="utf-8")
    monkeypatch.setenv("MIN_PAYOUT_PCT", "92")

    assert config.charger_env_local(fichier) == []
    assert os.environ["MIN_PAYOUT_PCT"] == "92"


def test_fichier_absent_n_est_pas_une_erreur(tmp_path):
    """En production il n'y a pas de `.env` : tout vient de l'environnement."""
    assert config.charger_env_local(tmp_path / "absent") == []


def test_ligne_malformee_leve(tmp_path):
    """Une ligne ignorée en silence, c'est une variable qu'on croit définie."""
    fichier = tmp_path / ".env"
    fichier.write_text("MIN_PAYOUT_PCT 92\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="sans '='"):
        config.charger_env_local(fichier)


# --------------------------------------------------------------------------- #
# Le conseil dépend de l'endroit
# --------------------------------------------------------------------------- #

def test_sur_un_poste_le_conseil_est_de_creer_le_repertoire(monkeypatch):
    for cle in ("RENDER", "RENDER_SERVICE_ID", "RAILWAY_ENVIRONMENT",
                "FLY_APP_NAME", "KUBERNETES_SERVICE_HOST"):
        monkeypatch.delenv(cle, raising=False)
    monkeypatch.setattr(config.Path, "exists", lambda self: False)

    message = config._message_repertoire_absent(config.Path("/home/x/trading_data"))
    assert "mkdir" in message
    assert "CONTENEUR" not in message


def test_dans_un_conteneur_le_conseil_est_de_monter_un_disque(monkeypatch):
    """Régression : le message disait « créez-le vous-même », ce qui n'a aucun
    sens sur une plateforme sans shell — et surtout, ce répertoire n'est PAS à
    créer. C'est le point de montage d'un disque persistant : son absence
    signifie que le disque n'est pas attaché. Le créer ferait écrire la collecte
    sur le système de fichiers du conteneur, effacé au déploiement suivant."""
    monkeypatch.setenv("RENDER", "true")
    message = config._message_repertoire_absent(config.Path("/data"))

    assert "disque persistant" in message
    assert "n'est pas attaché" in message
    assert "Add Disk" in message
    assert "offre gratuite" in message
    assert "mkdir" not in message, "conseil inapplicable sans shell"


def test_le_message_dit_pourquoi_creer_le_repertoire_ne_reglerait_rien(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    message = config._message_repertoire_absent(config.Path("/data"))
    assert "disparaîtrait au déploiement suivant" in message


@pytest.mark.parametrize("marqueur", ["RENDER", "RAILWAY_ENVIRONMENT",
                                      "FLY_APP_NAME", "KUBERNETES_SERVICE_HOST"])
def test_les_plateformes_courantes_sont_reconnues(monkeypatch, marqueur):
    monkeypatch.setenv(marqueur, "peu importe")
    assert config._dans_un_conteneur()
