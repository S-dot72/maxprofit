"""Le laboratoire juge chaque variante sur des jours qu'elle n'a pas vus."""

from __future__ import annotations

import re

from maxprofit.apprentissage.historique import _au_dela_de_la_fenetre
from maxprofit.apprentissage.lecons import Exemple
from maxprofit.live.laboratoire import (
    Mesure, _heures_perdantes, _seconde_chance, laboratoire, texte)

T0 = 1_790_000_000


def _ex(i, gagne, **ctx):
    base = {"heure_utc": float((i // 4) % 24), "bougies_contre": 1.0,
            "meche_rejet": 0.0, "tendance_4h": -1.0,
            "entrees_deja_offertes": 0.0}
    base.update(ctx)
    return Exemple(ts_sec=T0 + i * 1800, pair=f"P{i % 3}_otc",
                   contexte=base, gagne=gagne)


def _mesures(resultat):
    return {d["nom"]: Mesure.from_dict(d) for d in resultat["mesures"]}


def test_une_variante_vraiment_meilleure_est_dite_prometteuse():
    # Les signaux « bougie de rejet » gagnent 4 fois sur 5, les autres 1 sur 2.
    exemples = []
    for i in range(400):
        rejet = i % 2 == 0
        gagne = (i % 5 != 0) if rejet else (i % 4 < 2)
        exemples.append(_ex(i, gagne, bougies_contre=0.0 if rejet else 2.0))
    m = _mesures(laboratoire(exemples, None, lambda e: True))
    assert m["1. Bougie de rejet"].verdict.startswith("✅")
    assert m["3. Tendance 4 h alignée"].n_validation == 0


def test_une_variante_au_hasard_n_est_pas_retenue():
    exemples = [_ex(i, i % 2 == 0, bougies_contre=float(i % 3 == 0))
                for i in range(400)]
    m = _mesures(laboratoire(exemples, None, lambda e: True))
    assert not m["1. Bougie de rejet"].verdict.startswith("✅")


def test_la_reference_est_la_strategie_telle_qu_en_direct():
    exemples = [_ex(i, True, mouvement_heure=-5.0 if i % 2 else 0.0)
                for i in range(200)]
    resultat = laboratoire(
        exemples, None, lambda e: e.contexte["mouvement_heure"] >= -3.3)
    ref = _mesures(resultat)["ZoneH1 en direct"]
    assert ref.n_etalonnage + ref.n_validation == 100


def test_les_heures_ne_s_apprennent_que_sur_l_etalonnage():
    vieux = [_ex(i, False, heure_utc=1.0) for i in range(12)] + \
            [_ex(i, True, heure_utc=9.0) for i in range(12)]
    assert _heures_perdantes(vieux) == {0}


def test_seconde_chance_seulement_apres_un_gain_connu():
    zone = {"niveau": 1.1}
    signaux = [
        Exemple(T0, "A", {**zone, "entrees_deja_offertes": 2.0}, True),
        Exemple(T0 + 1800, "A", {**zone, "entrees_deja_offertes": 3.0}, False),
        Exemple(T0 + 3600, "A", {**zone, "entrees_deja_offertes": 4.0}, True),
        Exemple(T0 + 3700, "B", {**zone, "entrees_deja_offertes": 3.0}, True),
    ]
    gardes = _seconde_chance(signaux)
    # Le 2e suit un gain : gardé. Le 3e suit une perte : écarté. Le 4e n'a
    # pas d'antécédent sur SA paire : écarté.
    assert [e.ts_sec for e in gardes] == [T0, T0 + 1800]


def test_trop_peu_de_signaux_pour_juger():
    assert "note" in laboratoire([_ex(i, True) for i in range(20)], None,
                                 lambda e: True)


def test_le_texte_ne_casse_pas_le_html_de_telegram():
    exemples = [_ex(i, i % 3 != 0, bougies_contre=float(i % 2))
                for i in range(300)]
    for r in (laboratoire(exemples, exemples, lambda e: True), None,
              {"note": "x"}):
        assert not re.search(r"<(?!/?b>)", texte(r))


def test_la_tendance_4h_est_lue_dans_le_sens_du_trade():
    heure = T0 // 3600 * 3600
    closes = {heure - 60: 1.2, heure - 4 * 3600 - 60: 1.0}
    assert _au_dela_de_la_fenetre(closes, heure - 60, True, {})[
        "tendance_4h"] > 0
    assert _au_dela_de_la_fenetre(closes, heure - 60, False, {"niveau": 1.1}
                                  ) == {"tendance_4h": -2000.0, "niveau": 1.1}


def test_la_baisse_recente_est_attribuee_aux_nouvelles_paires():
    # Les paires A et B jouent tout le mois à 70 % ; C n'apparaît qu'à la
    # fin, à 30 %.
    exemples = []
    for i in range(300):
        paire = "A_otc" if i % 2 else "B_otc"
        exemples.append(Exemple(T0 + i * 1800, paire, {"heure_utc": 1.0},
                                i % 10 < 7))
    for i in range(280, 300):
        exemples.append(Exemple(T0 + i * 1800 + 60, "C_otc",
                                {"heure_utc": 1.0}, i % 10 < 3))
    resultat = laboratoire(exemples, None, lambda e: True)
    assert resultat["paires"]["C_otc"][0] == 0
    sortie = texte(resultat)
    assert "NOUVELLES paires" in sortie
    assert "C 🆕 : 30%" in sortie


def test_une_autre_strategie_est_jugee_face_a_la_reference():
    exemples = [_ex(i, i % 2 == 0) for i in range(300)]
    autre = [_ex(i, i % 5 != 0) for i in range(300)]
    m = _mesures(laboratoire(exemples, None, lambda e: True, 900,
                             {"6. Zone H1, confirmation M1": ("x", autre)}))
    assert m["6. Zone H1, confirmation M1"].verdict.startswith("✅")


def test_une_baisse_des_paires_d_origine_n_est_pas_mise_sur_les_nouvelles():
    """Le cas du 02/10 : anciennes 64 % → 53 %, nouvelles 50 %."""
    from maxprofit.live.laboratoire import _texte_paires
    sortie = "\n".join(_texte_paires(
        {"A_otc": [342, 220, 108, 57], "C_otc": [0, 0, 40, 20]}, "30/09"))
    assert "ne vient PAS seulement" in sortie
    assert "NOUVELLES paires qui tirent" not in sortie


def test_la_confirmation_m1_est_jugee_avec_son_entree_decalee():
    exemples = []
    for i in range(300):
        confirme = i % 2 == 0
        exemples.append(_ex(i, i % 2 == 0, retournement_meme=float(confirme),
                            retournement_suivant=float(confirme),
                            gagne_suivant=float(i % 5 != 0)))
    m = _mesures(laboratoire(exemples, None, lambda e: True))
    assert m["7. Confirmation M1, même bougie"].verdict.startswith("✅")
    huit = m["8. Confirmation M1, bougie suivante"]
    assert huit.n_etalonnage + huit.n_validation == 150
    assert huit.verdict.startswith("✅")


def test_le_rejeu_note_la_confirmation_de_la_bougie_suivante():
    from maxprofit.apprentissage.historique import _confirmation_m1
    from maxprofit.core.types import Candle

    def b(i, o, c):
        return Candle(pair="A", tf_sec=60, ts_sec=T0 // 60 * 60 + 60 * i,
                      open=o,
                      high=max(o, c), low=min(o, c), close=c, tick_count=1,
                      complete=True)
    serie = [b(0, 1.0002, 1.0001), b(1, 1.0001, 1.0003),
             b(2, 1.0003, 1.0010)] + [b(i, 1.0010, 1.0012)
                                      for i in range(3, 20)]
    closes = {c.ts_sec: c.close for c in serie}
    sortie = _confirmation_m1(serie, 1, True, closes, 900)
    assert sortie["retournement_meme"] == 1.0
    assert sortie["retournement_suivant"] == 1.0
    # Entrée à 1,0010 (clôture de la bougie suivante), sortie 15 min plus
    # tard à 1,0012 : gagné pour un achat.
    assert sortie["gagne_suivant"] == 1.0
    courte = serie[:10]
    assert "gagne_suivant" not in _confirmation_m1(
        courte, 1, True, {c.ts_sec: c.close for c in courte}, 900), \
        "sans bougie à l'échéance, pas d'issue"
