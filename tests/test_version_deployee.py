"""
Savoir quelle version tourne.

On a perdu deux echanges a discuter d'un correctif en regardant le journal
d'une image anterieure. Rien, ni dans le journal ni dans `/etat`, ne permettait
de trancher.
"""

from __future__ import annotations

import pytest

from maxprofit.hosting import version


@pytest.fixture(autouse=True)
def _sans_variables(monkeypatch):
    for nom in version.VARIABLES:
        monkeypatch.delenv(nom, raising=False)


def test_la_variable_de_l_hebergeur_gagne(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "62b92aa9f3c1d4e5a6b7c8d9")
    assert version.commit() == "62b92aa"


def test_l_ordre_de_preference_est_respecte(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "aaaaaaa")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "bbbbbbb")
    assert version.commit() == "bbbbbbb"


def test_une_variable_vide_ne_compte_pas(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "   ")
    monkeypatch.setenv("GIT_COMMIT", "ccccccc")
    assert version.commit() == "ccccccc"


def test_sans_rien_on_dit_qu_on_ne_sait_pas(monkeypatch):
    """Ne pas savoir n'est pas une panne, mais ca doit se dire.

    Inventer un numero serait pire que l'absence : on croirait avoir verifie.
    """
    monkeypatch.setattr(version, "_commit_local", lambda: None)
    assert version.commit() is None
    assert "inconnue" in version.resume()


def test_le_resume_tient_sur_une_ligne(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "62b92aa")
    texte = version.resume()
    assert "\n" not in texte
    assert "62b92aa" in texte
