"""
Spec §5 : « Absence de configuration = arrêt, pas valeur par défaut. »

Ce test existe parce que la tentation de remettre un défaut « juste pour que ça
tourne en local » réapparaît à chaque refactor.
"""

from __future__ import annotations

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
