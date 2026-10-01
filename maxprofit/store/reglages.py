"""Les réglages de l'utilisateur : une clé, une valeur JSON (migration v10)."""

from __future__ import annotations

import json
import time


def lire(conn, cle: str) -> dict | None:
    ligne = conn.execute("SELECT valeur FROM reglages WHERE cle = ?",
                         (cle,)).fetchone()
    return json.loads(ligne[0]) if ligne else None


def ecrire(conn, cle: str, valeur: dict) -> None:
    conn.execute(
        """INSERT INTO reglages (cle, valeur, maj_ts_sec) VALUES (?, ?, ?)
           ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur,
                                          maj_ts_sec = excluded.maj_ts_sec""",
        (cle, json.dumps(valeur, ensure_ascii=False), int(time.time())))
    conn.commit()
