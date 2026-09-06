"""
Le superviseur : redémarrer la collecte sur jeton neuf.

Ce qui est vérifié ici, c'est la distinction entre les trois façons dont le
collecteur peut s'arrêter — et surtout que la bonne réponse est donnée à
chacune. Les confondre coûte cher :

    arrêt demandé          -> on sort, c'est voulu
    session expirée        -> on ATTEND un jeton, sans sortir
    autre panne            -> on sort, l'hébergeur redémarrera

Sortir sur une session expirée serait le pire des trois : l'hébergeur
relancerait le conteneur, qui repartirait avec le même jeton mort, en boucle.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from maxprofit.collect.collector import Config
from maxprofit.collect.pocketoption import ENV_FICHIER_SESSION, SessionExpiree
from maxprofit.core.errors import BotError
from maxprofit.hosting.superviseur import Superviseur

JETON = '42["auth",{"session":"abc","isDemo":1,"uid":1,"platform":2}]'
JETON_REEL = '42["auth",{"session":"abc","isDemo":0,"uid":1,"platform":2}]'


class FauxCollecteur:
    """Remplace le vrai collecteur : `run()` fait ce que le test demande."""

    def __init__(self, comportements):
        self.comportements = list(comportements)
        self.executions = 0
        self.arrets = 0

    def run(self):
        self.executions += 1
        action = self.comportements.pop(0) if self.comportements else "attendre"
        if isinstance(action, BaseException):
            raise action
        if action == "attendre":
            import time
            for _ in range(200):
                if self.arrets:
                    return
                time.sleep(0.01)

    def stop(self):
        self.arrets += 1


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_FICHIER_SESSION, str(tmp_path / "session.json"))
    monkeypatch.delenv("POCKET_OPTION_SSID", raising=False)
    return Config(db=tmp_path / "market.db", min_payout=92)


def _superviseur(cfg, comportements, alertes=None):
    faux = FauxCollecteur(comportements)
    sup = Superviseur(lambda: None, cfg,
                      alerter=(alertes.append if alertes is not None else None))
    sup._creer = lambda: faux
    return sup, faux


def _patch(sup, faux, monkeypatch):
    import maxprofit.hosting.superviseur as module
    monkeypatch.setattr(module, "Collector", lambda source, cfg: faux)


def test_une_session_expiree_ne_fait_pas_sortir(cfg, monkeypatch):
    """Sortir ferait redémarrer le conteneur par l'hébergeur, qui repartirait
    avec le même jeton mort — une boucle qui consomme des minutes de calcul
    sans jamais collecter."""
    alertes: list[str] = []
    sup, faux = _superviseur(cfg, [SessionExpiree("jeton mort")], alertes)
    _patch(sup, faux, monkeypatch)

    async def scenario():
        tache = asyncio.create_task(sup.boucler())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if sup.attente_de_jeton:
                break
        assert sup.attente_de_jeton, "le superviseur n'attend pas de jeton"
        assert not tache.done(), "le superviseur est sorti au lieu d'attendre"

        message = await sup.installer_jeton(JETON)
        assert "installé" in message.lower()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if faux.executions >= 2:
                break
        sup.arreter()
        await asyncio.wait_for(tache, timeout=5)
        return faux.executions

    executions = asyncio.run(scenario())
    assert executions >= 2, "la collecte n'a pas redémarré après le jeton"
    assert any("expiré" in a for a in alertes)


def test_une_panne_non_recuperable_fait_sortir(cfg, monkeypatch):
    alertes: list[str] = []
    sup, faux = _superviseur(cfg, [BotError("base corrompue")], alertes)
    _patch(sup, faux, monkeypatch)

    with pytest.raises(BotError, match="base corrompue"):
        asyncio.run(sup.boucler())
    assert faux.executions == 1, "une panne fatale a été réessayée"
    assert any("arrêtée" in a for a in alertes)


def test_un_arret_demande_sort_sans_alerte(cfg, monkeypatch):
    alertes: list[str] = []
    sup, faux = _superviseur(cfg, ["attendre"], alertes)
    _patch(sup, faux, monkeypatch)

    async def scenario():
        tache = asyncio.create_task(sup.boucler())
        await asyncio.sleep(0.1)
        sup.arreter()
        await asyncio.wait_for(tache, timeout=5)

    asyncio.run(scenario())
    assert alertes == []


# --------------------------------------------------------------------------- #
# Validation du jeton
# --------------------------------------------------------------------------- #

def test_un_jeton_mal_forme_est_refuse(cfg):
    sup, _ = _superviseur(cfg, [])
    for mauvais in ("", "coucou", '{"session":"x"}', '42["autre",{}]'):
        with pytest.raises(BotError):
            asyncio.run(sup.installer_jeton(mauvais))


def test_un_jeton_tronque_est_refuse(cfg):
    sup, _ = _superviseur(cfg, [])
    with pytest.raises(BotError, match="tronqué"):
        asyncio.run(sup.installer_jeton('42["auth",{"isDemo":1}]'))


def test_le_prefixe_de_variable_est_tolere(cfg):
    """L'outil de capture affiche `POCKET_OPTION_SSID=42[...]`. Coller la ligne
    entière est l'erreur la plus probable : autant l'accepter."""
    sup, _ = _superviseur(cfg, [])
    message = asyncio.run(sup.installer_jeton(f"POCKET_OPTION_SSID={JETON}"))
    assert "DÉMO" in message


def test_le_jeton_est_ecrit_a_cote_de_la_base(cfg):
    """Sur un conteneur, la racine du projet est éphémère : le jeton doit vivre
    sur le volume persistant, donc à côté de la base."""
    import os
    os.environ.pop(ENV_FICHIER_SESSION, None)
    sup, _ = _superviseur(cfg, [])
    asyncio.run(sup.installer_jeton(JETON))
    assert (cfg.db.parent / "session.json").is_file()


def test_le_type_de_compte_est_reconnu(cfg):
    sup, _ = _superviseur(cfg, [])
    assert "RÉEL" in asyncio.run(sup.installer_jeton(JETON_REEL))


def test_un_redemarrage_requis_fait_sortir_avec_un_message_calme(cfg, monkeypatch):
    """Le processus doit mourir — c'est la seule façon de repartir sans laisser
    derrière soi un thread qui continuerait d'appeler le broker — mais ce n'est
    pas une panne : la collecte reprend seule au redémarrage, et les données
    sont chez Turso."""
    from maxprofit.collect.pocketoption import RedemarrageRequis

    alertes: list[str] = []
    sup, faux = _superviseur(cfg, [RedemarrageRequis("coupure trop longue")],
                             alertes)
    _patch(sup, faux, monkeypatch)

    with pytest.raises(RedemarrageRequis):
        asyncio.run(sup.boucler())

    assert faux.executions == 1, "un redémarrage requis a été réessayé"
    message = "".join(alertes)
    assert "Redémarrage" in message
    assert "rien n'est perdu" in message.lower()
    assert "❌" not in message, "un redémarrage attendu ne doit pas alarmer"
