"""
`/etat` — diagnostic des paires collectées, lu dans la mémoire de la course :
- une paire aux cotations rares n'est pas déclarée muette ;
- une paire muette l'est seulement si d'autres cotent ;
- un arrêt global ne rend aucune paire muette, l'en-tête le dit ;
- une reprise est dite spontanée ou consécutive à un redémarrage ;
- rien n'est modifié dans la mémoire de la course.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

from maxprofit.core.types import Candle
from maxprofit.live import diagnostic_paires as dp

T = 1_793_300_400                      # début de la fenêtre, minute entière
FIN = T + 300 * 60                     # fin de la dernière minute cotée


def _bougies(paire, minutes, ticks=60, complete=True):
    return [Candle(pair=paire, tf_sec=60, ts_sec=T + 60 * m, open=1.0,
                   high=1.0, low=1.0, close=1.0,
                   tick_count=ticks(m) if callable(ticks) else ticks,
                   complete=complete) for m in minutes]


def _fiche(diag, paire):
    return next(f for f in diag["paires"] if f["paire"] == paire)


def _diag(cache, maintenant=FIN + 20, **options):
    return dp.diagnostiquer(cache, list(cache), maintenant=maintenant,
                            **options)


def test_les_quatre_statuts():
    cache = {
        "ACTIVE_otc": _bougies("ACTIVE_otc", range(300)),
        # Plus rien depuis 47 minutes, alors que les autres cotent.
        "MUETTE_otc": _bougies("MUETTE_otc", range(253)),
        # Cote une minute sur deux : rare, pas muette.
        "RARE_otc": _bougies("RARE_otc", range(0, 300, 2)),
        # Rythme habituel 60 ticks/min, tombé à 5 sur les 15 dernières.
        "LENTE_otc": _bougies("LENTE_otc", range(300),
                              ticks=lambda m: 5 if m >= 285 else 60),
        # Revenue depuis 10 minutes après un long silence.
        "REVENUE_otc": (_bougies("REVENUE_otc", range(5))
                        + _bougies("REVENUE_otc", range(290, 300))),
    }
    diag = _diag(cache)
    statuts = {f["paire"]: f["statut"] for f in diag["paires"]}
    assert statuts == {"ACTIVE_otc": dp.ACTIVE, "MUETTE_otc": dp.MUETTE,
                       "RARE_otc": dp.RALENTIE, "LENTE_otc": dp.RALENTIE,
                       "REVENUE_otc": dp.INSUFFISANT}
    muette = _fiche(diag, "MUETTE_otc")
    assert muette["silence_sec"] == 47 * 60
    assert muette["ticks_15"] == 0 and muette["minutes_60"] == 13
    revenue = _fiche(diag, "REVENUE_otc")
    assert revenue["completes"] == 15
    assert [t["duree_sec"] for t in revenue["trous"]] == [285 * 60]
    # Les problèmes d'abord.
    assert [f["statut"] for f in diag["paires"]][:2] == [dp.MUETTE,
                                                         dp.INSUFFISANT]


def test_un_silence_de_moins_de_quinze_minutes_n_est_pas_muet():
    cache = {"A_otc": _bougies("A_otc", range(300)),
             "B_otc": _bougies("B_otc", range(286)),
             "C_otc": _bougies("C_otc", range(297))}
    diag = _diag(cache)
    # Quatorze minutes sans tick : ralentie, pas muette.
    assert _fiche(diag, "B_otc")["statut"] == dp.RALENTIE
    # Trois minutes : rien d'anormal.
    assert _fiche(diag, "C_otc")["statut"] == dp.ACTIVE


def test_un_arret_global_ne_rend_aucune_paire_muette():
    cache = {"A_otc": _bougies("A_otc", range(300)),
             "B_otc": _bougies("B_otc", range(300))}
    diag = _diag(cache, maintenant=FIN + 40 * 60)
    assert diag["globale_perimee"]
    assert {f["statut"] for f in diag["paires"]} == {dp.ACTIVE}
    texte = dp.texte(diag)
    assert "Aucune paire n'a de bougie depuis 40 min" in texte


def test_une_paire_absente_de_la_memoire_est_muette():
    cache = {"A_otc": _bougies("A_otc", range(300))}
    diag = dp.diagnostiquer(cache, ["A_otc", "VIDE_otc"],
                            maintenant=FIN + 5)
    vide = _fiche(diag, "VIDE_otc")
    assert vide["statut"] == dp.MUETTE and vide["silence_sec"] is None
    assert "aucune bougie sur les 300 dernières minutes" in dp.texte(diag)


def test_les_bougies_incompletes_ne_comptent_pas_pour_l_historique():
    cache = {"A_otc": _bougies("A_otc", range(300)),
             "B_otc": (_bougies("B_otc", range(280), complete=False)
                       + _bougies("B_otc", range(280, 300)))}
    b = _fiche(_diag(cache), "B_otc")
    assert b["completes"] == 20 and b["statut"] == dp.INSUFFISANT


def test_une_reprise_est_dite_spontanee_ou_apres_redemarrage():
    cache = {"A_otc": _bougies("A_otc", range(300)),
             "B_otc": (_bougies("B_otc", range(100))
                       + _bougies("B_otc", range(160, 300)))}
    pendant = T + 150 * 60
    diag = _diag(cache, demarrage_collecteur=pendant)
    assert _fiche(diag, "B_otc")["trous"][0]["apres_redemarrage"] is True
    assert "(après un redémarrage du collecteur)" in dp.texte(diag)
    diag = _diag(cache, demarrage_collecteur=T + 250 * 60)
    assert _fiche(diag, "B_otc")["trous"][0]["apres_redemarrage"] is False
    assert "(sans redémarrage du collecteur)" in dp.texte(diag)
    diag = _diag(cache)
    assert _fiche(diag, "B_otc")["trous"][0]["apres_redemarrage"] is None


def test_le_texte_donne_chaque_paire_et_le_plafond():
    cache = {"A_otc": _bougies("A_otc", range(300)),
             "B_otc": _bougies("B_otc", range(253))}
    diag = _diag(cache, plafond={"B_otc"})
    texte = dp.texte(diag, non_collectees=["BTCUSD_otc"])
    assert "🔴 B_otc★ MUETTE · 253/300 complètes" in texte
    assert "silence en cours depuis 47 min" in texte
    assert "🟢 A_otc ACTIVE · 300/300 complètes" in texte
    assert "Au plafond mais non collectées" in texte and "BTCUSD_otc" in texte
    assert "🔴 1 muette" in texte and "🟢 1 active" in texte


def test_depuis_la_course_ne_modifie_rien():
    cache = {"A_otc": _bougies("A_otc", range(300)),
             "B_otc": _bougies("B_otc", range(253))}
    course = SimpleNamespace(
        _cache_bougies=cache, paires_collectees=("A_otc", "B_otc"),
        _univers=["B_otc", "BTCUSD_otc"],
        strategie=SimpleNamespace(p=SimpleNamespace(lookback=300,
                                                    fenetre_pique=12)))
    avant = copy.deepcopy(cache)
    texte = dp.depuis_la_course(course, maintenant=FIN + 20,
                                demarrage_collecteur=T)
    assert course._cache_bougies is cache and cache == avant
    assert "B_otc★ MUETTE" in texte and "BTCUSD_otc" in texte
    assert "Dernier démarrage du collecteur : il y a 5 h 00" in texte
    assert dp.depuis_la_course(SimpleNamespace(), maintenant=FIN) is None
    vide = SimpleNamespace(_cache_bougies={}, paires_collectees=("A_otc",))
    assert "aucune bougie en mémoire" in dp.depuis_la_course(
        vide, maintenant=FIN)


def test_le_diagnostic_de_l_etat_ne_fait_jamais_tomber_etat():
    from maxprofit.hosting.service import _diagnostic_des_paires
    assert _diagnostic_des_paires(SimpleNamespace(_course=None), None) is None

    class Cassee:
        @property
        def _cache_bougies(self):
            raise RuntimeError("illisible")

    assert _diagnostic_des_paires(SimpleNamespace(_course=Cassee()),
                                  None) is None
    course = SimpleNamespace(_course=SimpleNamespace(
        _cache_bougies={"A_otc": _bougies("A_otc", range(300))},
        paires_collectees=("A_otc",)))
    assert "A_otc" in _diagnostic_des_paires(
        course, SimpleNamespace(dernier_demarrage_ts=None))
