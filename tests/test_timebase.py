"""
La confusion secondes / millisecondes est « le bug le plus fréquent de ce type
de projet » (spec §5). Ces tests vérifient qu'elle est détectée, pas subie.
"""

from __future__ import annotations

import pytest

from maxprofit.core.errors import TimebaseError
from maxprofit.core.timebase import (
    bucket_of_ms,
    ensure_ms,
    ensure_sec,
    floor_to_tf_sec,
    format_ms,
    ms_to_sec,
    sec_to_ms,
)

T_SEC = 1_704_067_200          # 2024-01-01T00:00:00Z
T_MS = 1_704_067_200_000


def test_conversions_aller_retour():
    assert sec_to_ms(T_SEC) == T_MS
    assert ms_to_sec(T_MS) == T_SEC


def test_ms_vers_sec_tronque_vers_le_passe():
    # 00:00:00.999 appartient à la seconde 00:00:00, jamais à la suivante.
    assert ms_to_sec(T_MS + 999) == T_SEC
    assert ms_to_sec(T_MS + 1000) == T_SEC + 1


def test_secondes_passees_pour_des_millisecondes_levent():
    # Le bug : on passe un epoch en secondes à une API qui attend des ms.
    with pytest.raises(TimebaseError, match="multiplier par 1000"):
        ensure_ms(T_SEC)


def test_millisecondes_passees_pour_des_secondes_levent():
    with pytest.raises(TimebaseError, match="diviser par 1000"):
        ensure_sec(T_MS)


def test_float_refuse():
    # Un float perd la milliseconde et rend les comparaisons instables.
    with pytest.raises(TimebaseError):
        ensure_ms(float(T_MS))
    with pytest.raises(TimebaseError):
        ensure_sec(float(T_SEC))


def test_bool_refuse():
    # bool est un int en Python : sans garde explicite, `ensure_sec(True)`
    # passerait la vérification de type.
    with pytest.raises(TimebaseError):
        ensure_sec(True)


def test_zero_et_valeurs_absurdes_refusees():
    for bad in (0, -1, 1, 10**18):
        with pytest.raises(TimebaseError):
            ensure_sec(bad)
        with pytest.raises(TimebaseError):
            ensure_ms(bad)


def test_floor_to_tf_sec():
    assert floor_to_tf_sec(T_SEC + 59, 60) == T_SEC
    assert floor_to_tf_sec(T_SEC + 60, 60) == T_SEC + 60
    assert floor_to_tf_sec(T_SEC + 61, 60) == T_SEC + 60


def test_bucket_of_ms():
    assert bucket_of_ms(T_MS + 59_999, 60) == T_SEC
    assert bucket_of_ms(T_MS + 60_000, 60) == T_SEC + 60


def test_tf_sec_invalide_leve():
    with pytest.raises(TimebaseError):
        floor_to_tf_sec(T_SEC, 0)


def test_format_est_toujours_utc():
    assert format_ms(T_MS) == "2024-01-01 00:00:00.000 UTC"
