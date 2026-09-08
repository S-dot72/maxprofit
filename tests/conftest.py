"""Fixtures partagées. Le noyau n'a aucune dépendance : ces fabriques
construisent des séries à la main, dont le résultat est calculable de tête
(spec §5, dernier point)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maxprofit.core.types import Candle  # noqa: E402

#: 2024-01-01T00:00:00Z, aligné sur la minute. Toutes les séries de test
#: partent d'ici pour que les horodatages restent lisibles à l'œil.
T0_SEC = 1_704_067_200
TF_SEC = 60


def make_candle(i: int, close: float, *, pair: str = "TEST_otc",
                tick_count: int = 10, complete: bool = True,
                t0_sec: int = T0_SEC, tf_sec: int = TF_SEC) -> Candle:
    """Bougie plate de valeur `close`, i-ième minute après `t0_sec`."""
    return Candle(
        pair=pair,
        tf_sec=tf_sec,
        ts_sec=t0_sec + i * tf_sec,
        open=close,
        high=close,
        low=close,
        close=close,
        tick_count=tick_count,
        complete=complete,
    )


@pytest.fixture
def series() -> list[Candle]:
    """10 bougies de clôture 100, 101, ... 109. Le prix vaut 100 + son index :
    toute erreur d'indexation devient visible d'un coup d'œil."""
    return [make_candle(i, 100.0 + i) for i in range(10)]


@pytest.fixture(autouse=True)
def _environnement_isole(monkeypatch):
    """Aucun test ne doit laisser de trace dans l'environnement du suivant.

    Un point d'entrée qui charge `.env` — et ils le font tous, c'est leur rôle —
    injecte les variables du développeur dans le processus pytest pour de bon.
    Il a suffi d'un test appelant `main()` depuis la racine pour que
    `TURSO_DATABASE_URL` devienne définie partout, et que trente tests sans
    rapport tentent d'ouvrir une réplique distante.

    `monkeypatch` restaure `os.environ` après chaque test, à condition que les
    modifications passent par lui. Ici elles viennent de code applicatif, d'où
    la sauvegarde explicite.
    """
    avant = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(avant)
