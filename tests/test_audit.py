"""L'audit : une fiche par signal, et d'où viennent les pertes."""

from __future__ import annotations

import csv
import io
import re

from maxprofit.apprentissage.lecons import Exemple
from maxprofit.live.audit import audit, cause, csv_du_rejeu, texte, _tardive

T0 = 1_790_000_000 // 60 * 60


def _ex(i, gagne, **ctx):
    base = {"mouvement_heure": 0.0, "retournement_suivant": 1.0,
            "gagne_suivant": float(gagne), "attente_confirmation": 1.0,
            "gagne_confirme": float(gagne), "prix_signal": 1.1,
            "niveau": 1.0995, "sens_call": 1.0, "mfe_15": 2.0,
            "mae_15": 1.0, "t_mfe": 10.0, "zone_cassee": 0.0,
            "e_part_parcourue": 0.3, "prix_entree": 1.1002,
            "prix_15": 1.1004 if gagne else 1.1000}
    base.update(ctx)
    return Exemple(ts_sec=T0 + i * 1800 + 60, pair="EURUSD_otc",
                   contexte=base, gagne=gagne,
                   issues={900: gagne, 180: gagne})


def test_chaque_perte_recoit_sa_cause_principale():
    exemples = [_ex(i, True) for i in range(20)] + [
        _ex(20, False, zone_cassee=1.0),
        _ex(21, False, mfe_15=0.2),
        _ex(22, False, t_mfe=3.0, gagne_confirme_180=1.0),
        _ex(23, False, e_part_parcourue=0.9),
        _ex(24, False)]
    # « gagnant puis retourné » : l'issue à 3 min de l'entrée confirmée.
    exemples[22] = Exemple(exemples[22].ts_sec, "EURUSD_otc",
                           exemples[22].contexte, False,
                           issues={180: True, 900: False})
    r = audit(exemples, lambda e: True, 1)
    assert r["causes"] == {"zone cassée": 1, "pas de réaction": 1,
                           "gagnant puis retourné": 1, "entrée tardive": 1,
                           "autre": 1}
    sortie = texte(r, avec_fichier=True)
    assert "D'où viennent les 5 pertes" in sortie
    assert "connus À L'ENTRÉE" in sortie and "constatés APRÈS" in sortie
    assert not re.search(r"<(?!/?b>)", sortie)


def test_la_fiche_csv_a_une_ligne_par_signal():
    exemples = [_ex(i, i % 3 != 0) for i in range(12)]
    exemples.append(_ex(12, False, attente_confirmation=0.0,
                        retournement_suivant=0.0))
    contenu = csv_du_rejeu(exemples, lambda e: True, "zone_h1:abcd1234", 1)
    lignes = list(csv.reader(io.StringIO(contenu.decode("utf-8-sig")),
                             delimiter=";"))
    entetes, rangs = lignes[0], lignes[1:]
    assert len(rangs) == 13
    assert {"id", "version_regles", "gagne_entree_15m", "mfe_15",
            "cause_perte", "entree_distance_niveau"} <= set(entetes)
    premier = dict(zip(entetes, rangs[0]))
    assert premier["version_regles"] == "zone_h1:abcd1234"
    assert premier["sens"] == "CALL" and premier["confirme_8"] == "1"
    non_confirme = dict(zip(entetes, rangs[-1]))
    assert non_confirme["confirme_8"] == "0"
    assert non_confirme["entree_utc"] == "" and non_confirme["cause_perte"] == ""


def test_l_entree_tardive_est_le_tiers_le_plus_avance():
    base = [_ex(i, True, part_parcourue=i / 30) for i in range(30)]
    # Tiers haut : au-delà de 20/30.
    tardive = _tardive(base)
    assert tardive(base[-1]) and not tardive(base[0])
    assert cause(_ex(0, False, part_parcourue=0.99), tardive) == \
        "entrée tardive"
