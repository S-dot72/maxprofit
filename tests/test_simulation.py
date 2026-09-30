"""Le plan simulé : martingale en 3 pas, en 2 pas, ou sans."""

from __future__ import annotations

import pytest

from maxprofit.apprentissage.lecons import Exemple
from maxprofit.live.simulation import CAPITAL, comparer, simuler, texte

T0 = 1_790_000_040


def _ex(i, gagne, pair=None, pas_min=16):
    # Un signal toutes les `pas_min` minutes, sur des paires alternées :
    # la règle d'indépendance (autre paire, 15 min) est donc respectée.
    return Exemple(T0 + 60 * pas_min * i, pair or ("A", "B")[i % 2], {},
                   gagne)


def test_trois_pertes_puis_un_gain_selon_la_martingale():
    exemples = [_ex(i, g) for i, g in enumerate([False, False, False, True])]
    trois, deux, sans = comparer(exemples, 900)
    assert (trois.sessions_perdues, trois.sessions_gagnees) == (1, 1)
    assert (deux.sessions_perdues, deux.sessions_gagnees) == (1, 1)
    assert (sans.sessions_perdues, sans.sessions_gagnees) == (3, 1)
    # À 250 $ : 3 pas perdus coûtent ~11,80 $, 2 pas ~4,90 $, 3 mises ~4,77 $.
    assert CAPITAL - trois.solde > CAPITAL - deux.solde
    assert trois.creux == pytest.approx(11.8, abs=0.1)
    # En 2 pas, la 3e perte ouvre une NOUVELLE session : 4,90 $ + ~1,55 $.
    assert deux.creux == pytest.approx(6.45, abs=0.1)


def test_un_seul_ordre_a_la_fois_et_le_pas_suivant_sur_une_autre_paire():
    # Deux signaux à la même minute : un seul est joué.
    simultanes = [Exemple(T0, "A", {}, True), Exemple(T0, "B", {}, True)]
    assert simuler(simultanes, echeance_sec=900, pas_max=3).ordres == 1
    # Après une perte sur A, un signal sur A n'est pas le pas suivant.
    meme_paire = [Exemple(T0, "A", {}, False),
                  Exemple(T0 + 1000, "A", {}, True),
                  Exemple(T0 + 2000, "B", {}, True)]
    r = simuler(meme_paire, echeance_sec=900, pas_max=3)
    assert r.ordres == 2 and r.sessions_gagnees == 1


def test_le_filtre_s_applique_comme_en_direct():
    exemples = [Exemple(T0 + 1000 * i, "AB"[i % 2],
                        {"mouvement_heure": -5.0 if i == 0 else 0.0}, i != 0)
                for i in range(3)]
    garder = lambda e: e.contexte["mouvement_heure"] >= -3.3
    assert simuler(exemples, echeance_sec=900, pas_max=3,
                   garder=garder).sessions_perdues == 0


def test_le_texte_dit_chaque_reglage():
    exemples = [_ex(i, i % 3 != 0) for i in range(30)]
    t = texte({"ZoneH1": {"signaux": 30, "resultats": [
        r.to_dict() for r in comparer(exemples, 900)]}}, 30)
    assert "3 pas" in t and "2 pas" in t and "sans martingale" in t
    assert "sur 30 jours" in t
    assert "<" not in t.replace("<b>", "").replace("</b>", "")
    assert "pas encore calculée" in texte({})
