"""L'apprentissage : ce que les pertes enseignent, et seulement ce qu'elles
prouvent (SPEC §3.4, §3.5)."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from maxprofit.apprentissage.contexte import contexte
from maxprofit.apprentissage.historique import exemples_historiques
from maxprofit.apprentissage.lecons import (
    COUVERTURE_MAX, SEUIL, Apprentissage, Exemple, Tranche, apprendre,
    autopsie, wilson)
from maxprofit.core.types import Candle, Direction, Signal

T0 = 1_790_000_040


def _bougie(i, o, c, pair="EURUSD_otc"):
    return Candle(pair=pair, tf_sec=60, ts_sec=T0 + 60 * i, open=o,
                  high=max(o, c) + 0.0001, low=min(o, c) - 0.0001, close=c,
                  tick_count=10, complete=True)


def _serie(mouvements, depart=1.1):
    prix, sortie = depart, []
    for i, m in enumerate(mouvements):
        sortie.append(_bougie(i, round(prix, 5), round(prix + m, 5)))
        prix += m
    return sortie


# --- le contexte ------------------------------------------------------------

def test_le_contexte_est_ORIENTE_dans_le_sens_du_trade():
    """Une même règle doit valoir pour un CALL et pour un PUT : un marché qui
    baisse est « contre » un CALL et « avec » un PUT."""
    baisse = _serie([-0.0002] * 300)
    call, put = contexte(baisse, True), contexte(baisse, False)
    assert call["elan_15m"] < 0 < put["elan_15m"]
    assert call["tendance_h1"] < 0 < put["tendance_h1"]
    assert call["elan_15m"] == pytest.approx(-put["elan_15m"])
    assert call["bougies_contre"] == 300 and put["bougies_contre"] == 0


def test_le_contexte_reprend_ce_que_la_strategie_a_vu():
    serie = _serie([0.0001, -0.0001] * 150)
    ctx = contexte(serie, True, {"niveau": 1.1, "touches": 3.0,
                                 "entrees_deja_offertes": 1.0})
    assert ctx["touches"] == 3.0 and ctx["entrees_deja_offertes"] == 1.0
    assert "distance_niveau" in ctx


# --- les leçons -------------------------------------------------------------

def _exemples(n, regle_perdante=True, graine=7):
    """Des signaux où « élan très contraire » perd 30 % du temps et le reste
    gagne 60 % — sur TOUTE la période, étalonnage et validation."""
    alea = random.Random(graine)
    sortie = []
    for i in range(n):
        elan = alea.uniform(-3, 3)
        perdant = regle_perdante and elan < -1.8
        gagne = alea.random() < (0.30 if perdant else 0.60)
        sortie.append(Exemple(T0 + 60 * i, "EURUSD_otc",
                              {"elan_15m": elan,
                               "heure_utc": float(i % 24)}, gagne))
    return sortie


def test_un_contexte_qui_perd_VRAIMENT_devient_une_lecon():
    a = apprendre(_exemples(1500))
    assert a.regles, a.resume()
    regle = a.regles[0]
    assert regle.tranche.caracteristique == "elan_15m"
    assert regle.tranche.haut is not None and regle.tranche.haut < -1
    assert regle.taux < SEUIL and regle.taux_validation < SEUIL
    assert a.taux_validation_apres > a.taux_validation_avant
    assert a.ecarte({"elan_15m": -2.5}) is not None
    assert a.ecarte({"elan_15m": 1.0}) is None


def test_le_HASARD_ne_devient_pas_une_lecon():
    """Aucun contexte ne perd : aucune leçon, et c'est un résultat."""
    a = apprendre(_exemples(1500, regle_perdante=False))
    assert a.regles == ()
    assert "Aucune leçon active" in a.resume()


def test_trop_peu_de_signaux_pour_apprendre():
    a = apprendre(_exemples(80))
    assert a.regles == () and "il en faut" in a.note


def test_une_lecon_qui_ne_se_CONFIRME_pas_reste_candidate():
    """Perdante sur l'étalonnage, gagnante ensuite : du hasard, pas une
    leçon."""
    exemples = _exemples(1500)
    coupure = int(len(exemples) * 0.7)
    retournes = exemples[:coupure] + [
        Exemple(e.ts_sec, e.pair, e.contexte,
                True if e.contexte["elan_15m"] < -1.8 else e.gagne)
        for e in exemples[coupure:]]
    a = apprendre(retournes)
    assert all(r.tranche.caracteristique != "elan_15m" for r in a.regles)
    assert any(r.tranche.caracteristique == "elan_15m" for r in a.candidates)


def test_les_lecons_n_ecartent_jamais_plus_que_le_plafond():
    """Un filtre qui ne laisse rien passer ne perd jamais — et ne fait plus
    aucune session."""
    alea = random.Random(3)
    exemples = [Exemple(T0 + 60 * i, "X", {"elan_15m": alea.uniform(-3, 3)},
                        alea.random() < 0.40) for i in range(2000)]
    a = apprendre(exemples)
    ecartes = sum(a.ecarte(e.contexte) is not None for e in exemples)
    assert ecartes / len(exemples) <= COUVERTURE_MAX + 1e-9


def test_wilson_encadre_la_proportion():
    bas, haut = wilson(30, 100)
    assert bas < 0.30 < haut
    assert wilson(0, 0) == (0.0, 1.0)


def test_l_apprentissage_survit_a_la_serialisation():
    a = apprendre(_exemples(1500))
    relu = Apprentissage.from_dict(a.to_dict())
    assert relu.to_dict() == a.to_dict()
    assert relu.ecarte({"elan_15m": -2.5}) is not None


def test_tranche_libelle_lisible():
    t = Tranche("elan_15m", None, -1.2)
    assert "élan" in t.libelle() and "-1.20" in t.libelle()
    assert "13 h" in Tranche("heure_utc", 12.0, 15.0).libelle().replace(
        "12 h", "13 h")


# --- l'autopsie ---------------------------------------------------------------

def test_l_autopsie_dit_quand_c_est_la_VARIANCE():
    texte = autopsie([("EURUSD_otc", "call", {"elan_15m": 0.5})] * 3, None)
    assert "variance" in texte and "3 pertes d'affilée" in texte


def test_l_autopsie_repere_un_POINT_COMMUN_sans_en_faire_une_regle():
    a = apprendre(_exemples(1500))
    a.regles = ()                        # établi, mais pas encore actif
    pas = [("EURUSD_otc", "call", {"elan_15m": -2.6})] * 3
    texte = autopsie(pas, a)
    assert "point commun" in texte
    assert "seule session" in texte, "une session ne fait pas une règle"


# --- le rejeu de l'historique --------------------------------------------------

class _StrategieToujoursCall:
    p = SimpleNamespace(lookback=25, expiry_sec=120)

    def on_bar(self, vue):
        return Signal(pair=vue.pair, direction=Direction.CALL,
                      decided_at_ms=vue.now_ms, expiry_sec=120,
                      features={"touches": 2.0})


def test_le_rejeu_juge_chaque_signal_a_l_echeance():
    serie = _serie([0.0001] * 40 + [-0.0001] * 40)
    exemples = exemples_historiques({"EURUSD_otc": serie},
                                    _StrategieToujoursCall(), echeance_sec=120)
    assert exemples, "des signaux ont été émis"
    assert exemples[0].gagne is True, "le marché montait à l'échéance"
    assert exemples[-1].gagne is False, "il baissait à la fin"
    assert all(e.contexte.get("touches") == 2.0 for e in exemples)
    # Sans bougie à l'échéance, pas d'exemple : on ne devine pas l'issue.
    assert max(e.ts_sec for e in exemples) <= serie[-1].ts_sec - 120 + 60


def test_le_fil_reapprend_depuis_la_base_et_le_DIT(tmp_path, monkeypatch):
    import time as _t

    from maxprofit.live import apprenti as mod
    from maxprofit.store.db import open_read_write
    from maxprofit.store.market import MarketWriter

    class Strategie(_StrategieToujoursCall):
        def __init__(self, p=None):
            pass

    maintenant = int(_t.time()) // 60 * 60
    conn = open_read_write(tmp_path / "m.db")
    bougies = [Candle(pair="EURUSD_otc", tf_sec=60,
                      ts_sec=maintenant - 60 * (200 - i), open=1.1,
                      high=1.1003, low=1.0997,
                      close=round(1.1 + 0.0001 * ((i * 7) % 5 - 2), 5),
                      tick_count=10, complete=True) for i in range(200)]
    MarketWriter(conn).upsert_candles(bougies)
    messages = []
    course = SimpleNamespace(
        strategie=Strategie(), etat=SimpleNamespace(apprentissage=None),
        _prevenir=messages.append)
    course.strategie.p = SimpleNamespace(lookback=25, expiry_sec=120,
                                         tolerance_pct=0.02)
    monkeypatch.setattr(
        "maxprofit.live.plan_demo.dans_la_plage_de_calibration",
        lambda tol, f: True)
    apprenti = mod.Apprenti(course, lambda: open_read_write(tmp_path / "m.db"),
                            ("EURUSD_otc",))
    apprenti.apprendre_une_fois()
    assert course.etat.apprentissage is not None
    assert course.etat.apprentissage.n > 0
    assert messages and "Apprentissage" in messages[-1]
    conn.close()


def test_aucune_lecon_ne_se_fait_PAS_passer_pour_une_absence_de_lien():
    """276 signaux : seul un contexte perdant deux fois sur trois pouvait
    être établi, et le message disait « c'est ce qu'il a mesuré »."""
    from maxprofit.apprentissage.lecons import taux_detectable

    a = apprendre(_exemples(276, regle_perdante=False))
    texte = a.resume()
    assert "Ce n'est PAS la preuve que les pertes sont sans lien" in texte
    assert taux_detectable(193) <= 0.31
    assert taux_detectable(2000) > 0.44, "plus d'historique, plus de finesse"
