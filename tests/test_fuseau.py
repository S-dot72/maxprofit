"""
Heure locale (Haïti) — et pourquoi un décalage fixe serait faux.

Le stockage reste intégralement en UTC (spec §5). Ces fonctions ne servent
qu'à l'affichage et à la segmentation horaire du §3.2.

Le point important : Haïti applique l'heure d'été. UTC−5 (EST) en hiver,
UTC−4 (EDT) de mars à novembre. Un décalage codé en dur serait donc juste la
moitié de l'année, et faux l'autre moitié — sans que rien ne le signale. Dans
une segmentation par heure, cela déplacerait d'une heure la moitié des données,
ce qui suffit à faire apparaître un edge horaire qui n'existe pas ou à en
effacer un qui existe.
"""

from __future__ import annotations

import pytest

from maxprofit.core.errors import TimebaseError
from maxprofit.core.timebase import (
    decalage_local_heures,
    format_local_sec,
    heure_locale,
    utc_from_sec,
)

# 2026-01-17 12:00 UTC — hiver, EST
HIVER_SEC = 1_768_651_200
# 2025-09-05 12:00 UTC — été, EDT
ETE_SEC = 1_757_073_600


def test_l_heure_d_ete_haitienne_est_appliquee():
    assert decalage_local_heures(HIVER_SEC) == -5.0
    assert decalage_local_heures(ETE_SEC) == -4.0


def test_un_decalage_fixe_serait_faux_la_moitie_de_l_annee():
    """Le test qui justifie l'usage d'un fuseau nommé plutôt que d'un -5."""
    assert decalage_local_heures(HIVER_SEC) != decalage_local_heures(ETE_SEC)


def test_heure_locale():
    assert utc_from_sec(HIVER_SEC).hour == 12
    assert heure_locale(HIVER_SEC) == 7      # 12 - 5
    assert utc_from_sec(ETE_SEC).hour == 12
    assert heure_locale(ETE_SEC) == 8        # 12 - 4


def test_le_format_affiche_le_fuseau():
    assert format_local_sec(HIVER_SEC).endswith("EST")
    assert format_local_sec(ETE_SEC).endswith("EDT")


def test_fuseau_configurable(monkeypatch):
    monkeypatch.setenv("TIMEZONE_AFFICHAGE", "UTC")
    assert heure_locale(HIVER_SEC) == 12
    assert decalage_local_heures(HIVER_SEC) == 0.0


def test_un_decalage_fixe_est_refuse(monkeypatch):
    """« UTC-5 » n'est pas un fuseau : c'est un décalage, et il ignore l'heure
    d'été. Le refuser évite de croire qu'on a configuré Haïti."""
    monkeypatch.setenv("TIMEZONE_AFFICHAGE", "UTC-5")
    with pytest.raises(TimebaseError, match="heure d'été"):
        heure_locale(HIVER_SEC)


def test_le_stockage_reste_en_utc():
    """Garde-fou : rien dans le noyau ne doit convertir un horodatage stocké.
    `utc_from_sec` est la référence, l'heure locale n'en est qu'une vue."""
    assert utc_from_sec(HIVER_SEC).tzinfo is not None
    assert utc_from_sec(HIVER_SEC).utcoffset().total_seconds() == 0
