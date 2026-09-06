"""
L'annuaire des opérateurs : qui peut parler au bot, et faire quoi.

Ce fichier protège une seule chose, mais elle est décisive : **installer un
jeton de session revient à choisir quel compte de courtier est collecté**. Sans
rôles, « plusieurs utilisateurs » signifierait que quiconque obtient le code —
et un code finit toujours par circuler, dans une capture d'écran ou un message
transféré — pourrait détourner la collecte.
"""

from __future__ import annotations

import json

import pytest

from maxprofit.hosting.operateurs import (
    ENV_CHAT_HISTORIQUE,
    ENV_CODE_ADMIN,
    ENV_CODE_OBSERVATEUR,
    Annuaire,
    Role,
)


@pytest.fixture(autouse=True)
def env_propre(monkeypatch):
    for cle in (ENV_CODE_ADMIN, ENV_CODE_OBSERVATEUR, ENV_CHAT_HISTORIQUE):
        monkeypatch.delenv(cle, raising=False)


@pytest.fixture
def annuaire(tmp_path):
    return Annuaire(tmp_path / "operateurs.json")


# --------------------------------------------------------------------------- #
# Rôles
# --------------------------------------------------------------------------- #

def test_seul_un_admin_peut_installer_un_jeton():
    """LE test de ce fichier."""
    assert Role.ADMIN.peut_installer_jeton is True
    assert Role.OBSERVATEUR.peut_installer_jeton is False


def test_un_role_inconnu_retombe_sur_le_moins_permissif(tmp_path):
    """Un annuaire corrompu ou écrit par une version future ne doit pas
    accorder plus de droits qu'il n'en accordait."""
    fichier = tmp_path / "operateurs.json"
    fichier.write_text(json.dumps({"42": {"role": "superviseur-supreme"}}),
                       encoding="utf-8")
    assert Annuaire(fichier).role("42") is Role.OBSERVATEUR


# --------------------------------------------------------------------------- #
# Codes d'accès
# --------------------------------------------------------------------------- #

def test_sans_code_configure_personne_ne_s_inscrit(annuaire):
    assert annuaire.role_pour_code("n'importe quoi") is None
    assert annuaire.role_pour_code("") is None


def test_chaque_code_donne_son_role(annuaire, monkeypatch):
    monkeypatch.setenv(ENV_CODE_ADMIN, "code-admin")
    monkeypatch.setenv(ENV_CODE_OBSERVATEUR, "code-lecture")

    assert annuaire.role_pour_code("code-admin") is Role.ADMIN
    assert annuaire.role_pour_code("code-lecture") is Role.OBSERVATEUR
    assert annuaire.role_pour_code("code-invente") is None


def test_un_code_vide_configure_n_ouvre_rien(annuaire, monkeypatch):
    """Une variable définie à la chaîne vide est une erreur de script, pas une
    autorisation universelle."""
    monkeypatch.setenv(ENV_CODE_ADMIN, "   ")
    assert annuaire.role_pour_code("") is None
    assert annuaire.role_pour_code("   ") is None


# --------------------------------------------------------------------------- #
# Inscription et persistance
# --------------------------------------------------------------------------- #

def test_l_inscription_survit_a_un_redemarrage(tmp_path):
    fichier = tmp_path / "operateurs.json"
    Annuaire(fichier).inscrire("111", Role.ADMIN, "denis")

    repris = Annuaire(fichier)
    assert repris.role("111") is Role.ADMIN
    assert repris.est_inscrit("111")


def test_la_revocation_est_immediate_et_persistee(tmp_path):
    fichier = tmp_path / "operateurs.json"
    a = Annuaire(fichier)
    a.inscrire("111", Role.ADMIN)
    assert a.revoquer("111") is True
    assert a.role("111") is None
    assert Annuaire(fichier).role("111") is None


def test_revoquer_un_inconnu_ne_ment_pas(annuaire):
    assert annuaire.revoquer("inexistant") is False


def test_un_annuaire_illisible_ne_bloque_pas_le_demarrage(tmp_path, caplog):
    """Perdre des inscriptions se répare en une commande ; ne pas démarrer, non.
    Le bot doit tourner même sur un fichier corrompu."""
    fichier = tmp_path / "operateurs.json"
    fichier.write_text("{ pas du json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        a = Annuaire(fichier)
    assert len(a) == 0
    assert any("illisible" in m for m in caplog.messages)


# --------------------------------------------------------------------------- #
# Compatibilité et diffusion
# --------------------------------------------------------------------------- #

def test_l_ancien_reglage_reste_honore(tmp_path, monkeypatch):
    """Sans cela, la toute première alerte — celle qui dit que la collecte n'a
    pas démarré — n'aurait personne à qui parler tant que personne ne s'est
    inscrit. C'est précisément le moment où l'on a besoin d'être prévenu."""
    monkeypatch.setenv(ENV_CHAT_HISTORIQUE, "999")
    a = Annuaire(tmp_path / "operateurs.json")
    assert a.role("999") is Role.ADMIN


def test_les_alertes_vont_a_tous_les_inscrits(annuaire):
    """Une panne n'est pas confidentielle pour qui a déjà le droit de consulter
    l'état : la restreindre aux admins ferait manquer l'essentiel à ceux qui
    surveillent."""
    annuaire.inscrire("111", Role.ADMIN)
    annuaire.inscrire("222", Role.OBSERVATEUR)
    assert set(annuaire.destinataires()) == {"111", "222"}


def test_lister_donne_role_et_nom(annuaire):
    annuaire.inscrire("111", Role.ADMIN, "denis")
    (identifiant, role, nom), = annuaire.lister()
    assert (identifiant, role, nom) == ("111", Role.ADMIN, "denis")


# --------------------------------------------------------------------------- #
# Plafond d'administrateurs
# --------------------------------------------------------------------------- #

def test_trois_administrateurs_au_maximum(annuaire):
    """Un code d'accès finit toujours par circuler. Plafonner limite les dégâts
    et rend visible le moment où quelqu'un de plus s'inscrit."""
    from maxprofit.hosting.operateurs import MAX_ADMINS, TropDAdmins

    for i in range(MAX_ADMINS):
        annuaire.inscrire(f"admin{i}", Role.ADMIN)

    with pytest.raises(TropDAdmins, match="maximum"):
        annuaire.inscrire("admin-de-trop", Role.ADMIN)
    assert annuaire.role("admin-de-trop") is None


def test_le_plafond_ne_bloque_pas_les_observateurs(annuaire):
    from maxprofit.hosting.operateurs import MAX_ADMINS

    for i in range(MAX_ADMINS):
        annuaire.inscrire(f"admin{i}", Role.ADMIN)
    annuaire.inscrire("lecteur", Role.OBSERVATEUR)
    assert annuaire.role("lecteur") is Role.OBSERVATEUR


def test_reinscrire_un_admin_existant_ne_compte_pas_double(annuaire):
    from maxprofit.hosting.operateurs import MAX_ADMINS

    for i in range(MAX_ADMINS):
        annuaire.inscrire(f"admin{i}", Role.ADMIN)
    annuaire.inscrire("admin0", Role.ADMIN, "nouveau nom")   # ne doit pas lever
    assert annuaire.compter_admins() == MAX_ADMINS


def test_une_place_se_libere_par_revocation(annuaire):
    from maxprofit.hosting.operateurs import MAX_ADMINS, TropDAdmins

    for i in range(MAX_ADMINS):
        annuaire.inscrire(f"admin{i}", Role.ADMIN)
    with pytest.raises(TropDAdmins):
        annuaire.inscrire("suivant", Role.ADMIN)

    annuaire.revoquer("admin0")
    annuaire.inscrire("suivant", Role.ADMIN)
    assert annuaire.role("suivant") is Role.ADMIN


def test_le_plafond_est_configurable(annuaire, monkeypatch):
    from maxprofit.hosting.operateurs import ENV_MAX_ADMINS, TropDAdmins

    monkeypatch.setenv(ENV_MAX_ADMINS, "1")
    annuaire.inscrire("seul", Role.ADMIN)
    with pytest.raises(TropDAdmins):
        annuaire.inscrire("second", Role.ADMIN)


# --------------------------------------------------------------------------- #
# Demandes d'accès
# --------------------------------------------------------------------------- #

def test_une_demande_ne_donne_aucun_droit(annuaire):
    """Approuver doit rester un geste qui change quelque chose ; sinon
    l'approbation devient une formalité qu'on expédie sans regarder."""
    annuaire.demander_acces("inconnu", "curieux")
    role = annuaire.role("inconnu")
    assert role is Role.EN_ATTENTE
    assert role.peut_consulter is False
    assert role.peut_installer_jeton is False


def test_une_demande_ne_recoit_pas_les_alertes(annuaire):
    """Recevoir les alertes d'une installation à laquelle on n'a pas accès
    serait déjà y avoir accès."""
    annuaire.inscrire("111", Role.OBSERVATEUR)
    annuaire.demander_acces("222")
    assert annuaire.destinataires() == ["111"]


def test_une_demande_n_ecrase_jamais_un_role(annuaire):
    """Un administrateur qui taperait /start par distraction se rétrograderait
    lui-même."""
    annuaire.inscrire("chef", Role.ADMIN)
    assert annuaire.demander_acces("chef") is False
    assert annuaire.role("chef") is Role.ADMIN


def test_l_approbation_donne_le_role_observateur(annuaire):
    annuaire.demander_acces("nouveau", "alice")
    annuaire.inscrire("nouveau", Role.OBSERVATEUR)
    assert annuaire.role("nouveau") is Role.OBSERVATEUR
    assert annuaire.en_attente() == []


def test_les_demandes_en_attente_sont_listables(annuaire):
    annuaire.inscrire("111", Role.ADMIN)
    annuaire.demander_acces("222", "bob")
    assert annuaire.en_attente() == [("222", "bob")]
    assert annuaire.administrateurs() == ["111"]
