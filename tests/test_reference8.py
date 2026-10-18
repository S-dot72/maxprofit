"""La mission Reference 8 : mesures du rejeu, rapport, fenêtre, verdicts."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

from maxprofit.apprentissage import historique
from maxprofit.apprentissage.historique import exemples_historiques
from maxprofit.apprentissage.lecons import Exemple
from maxprofit.apprentissage.qualite import Qualite
from maxprofit.core.types import Candle, Direction, Signal
from maxprofit.live import reference8 as r8
from maxprofit.live.preinscriptions import ouverture_r8

JOUR = 86400
PAIRES = ("A_otc", "B_otc", "C_otc", "D_otc")


def _b(i: int, o: float, h: float, b: float, c: float,
       t0: int = 1_790_000_040, paire: str = "X_otc") -> Candle:
    return Candle(pair=paire, tf_sec=60, ts_sec=t0 + 60 * i, open=o, high=h,
                  low=b, close=c, tick_count=20, complete=True)


# --------------------------------------------------------------------------- #
# Les mesures ajoutées au rejeu
# --------------------------------------------------------------------------- #

def _trajet() -> list[Candle]:
    """70 minutes d'amplitude 0,0010, puis : +1,1 amplitude à la 2e
    minute, −1,1 à la 5e."""
    serie = [_b(i, 1.1, 1.1005, 1.0995, 1.1) for i in range(70)]
    suite = [(1.1, 1.1004, 1.0999, 1.1002), (1.1002, 1.1011, 1.1001, 1.1008),
             (1.1008, 1.1009, 1.1001, 1.1003), (1.1003, 1.1004, 1.0995, 1.0997),
             (1.0997, 1.0998, 1.0989, 1.0990)]
    suite += [(1.099, 1.0995, 1.0985, 1.0990)] * 10
    return serie + [_b(70 + k, *v) for k, v in enumerate(suite)]


def test_le_suivi_note_les_temps_et_le_chemin_net():
    serie = _trajet()
    s = historique._excursions(serie, 69, True, 1.0992)
    assert s["t_pour"] == 2.0 and s["t_contre"] == 5.0
    assert s["net_4"] == round((1.0997 - 1.1) / 0.001, 3)
    assert s["net_15"] == round((1.099 - 1.1) / 0.001, 3)
    assert s["zone_cassee"] == 1.0
    # Vu en vente, le même chemin : contre d'abord à la 2e minute.
    v = historique._excursions(serie, 69, False)
    assert v["t_contre"] == 2.0 and v["t_pour"] == 5.0
    assert v["net_15"] == -s["net_15"]
    # Un suivi incomplet ne mesure rien.
    assert historique._excursions(serie[:80], 69, True) == {}


class _ToujoursCall:
    p = SimpleNamespace(lookback=25, expiry_sec=120)

    def on_bar(self, vue):
        return Signal(pair=vue.pair, direction=Direction.CALL,
                      decided_at_ms=vue.now_ms, expiry_sec=120,
                      features={"niveau": 1.0990})


def test_le_rejeu_suit_aussi_l_entree_immediate():
    exemples = exemples_historiques({"X_otc": _trajet()}, _ToujoursCall(),
                                    echeance_sec=120)
    avec = [e for e in exemples if "imm_mfe_15" in e.contexte]
    assert avec, "les signaux suivis 15 min ont leur trajectoire immédiate"
    e = avec[0]
    assert {"imm_t_pour", "imm_t_contre", "imm_net_15",
            "imm_zone_cassee"} <= set(e.contexte)
    assert "imm_prix_entree" not in e.contexte


def test_la_couverture_par_paire_heure():
    t0 = 1_790_002_800 // 3600 * 3600
    q = Qualite(t0, t0 + 3 * 3600, t0 + 3600)
    q.ajouter([_b(i, 1.1, 1.1005, 1.0995, 1.1, t0=t0, paire="A_otc")
               for i in range(90)])
    assert q.minutes_par_paire_heure() == {
        "A_otc": {t0 // 3600: 60, t0 // 3600 + 1: 30}}


# --------------------------------------------------------------------------- #
# Les petits outils
# --------------------------------------------------------------------------- #

def test_mcnemar_et_strates():
    assert r8._mcnemar(0, 0) is None
    assert r8._mcnemar(5, 0) == 0.0625
    assert r8._mcnemar(3, 3) == 1.0
    assert r8.strate(1.5, 0.6, 1.0) == "C"
    assert r8.strate(1.5, 0.2, 1.0) == "D"
    assert r8.strate(1.5, None, 99.0) == "B"
    assert r8.strate(None, None, None) == "reste"


def test_la_regle_d_echeance_apprise():
    def x(g240, g600, g900):
        return {240: g240, 600: g600, 900: g900}
    entrees = ([("C", x(True, False, False))] * 20
               + [("C", x(False, False, False))] * 10
               + [("D", x(True, True, False))] * 30      # égalité 4 / 10
               + [("B", x(True, True, True))] * 5)       # trop peu
    regle = r8.apprendre_echeances(entrees)
    assert regle == {"C": 240, "D": 600, "B": 900, "reste": 900}


# --------------------------------------------------------------------------- #
# Le rapport sur des signaux synthétiques
# --------------------------------------------------------------------------- #

def _signal(k: int, t0: int) -> Exemple:
    """Le k-ième signal : confirmé sauf k % 5 = 0 ; l'entrée immédiate gagne
    sauf k % 3 = 0 ; la confirmée perd en plus quand k % 4 = 0 (ce sont les
    entrées tardives, C ≥ 0,46) ; les grandes bougies (k % 6 = 1) gagnent."""
    confirme = k % 5 != 0
    imm = {60: k % 2 == 0, 240: k % 3 != 0, 600: k % 3 != 0,
           900: k % 3 != 0}
    g900 = k % 3 != 0 and k % 4 != 0
    ctx = {"sens_call": float(k % 2), "mouvement_heure": 0.0,
           "distance_niveau": 0.5, "taille_bougie": 1.0,
           "part_parcourue": 0.2, "espace_obstacle": 3.0,
           "imm_mfe_15": 1.8, "imm_mae_15": 0.5, "imm_t_mfe": 4.0,
           "imm_zone_cassee": 0.0,
           "retournement_suivant": float(confirme),
           # Confirmée à la bougie suivante, l'entrée « suivante » EST
           # l'entrée confirmée : même issue, comme au rejeu.
           "gagne_suivant": float(g900 if confirme
                                  else k % 3 != 0 and k % 7 != 0)}
    if confirme:
        ctx.update({
            "attente_confirmation": 1.0, "gagne_confirme": float(g900),
            "gagne_confirme_60": float(k % 2 == 1),
            "gagne_confirme_240": float(k % 5 in (1, 2, 3)),
            "gagne_confirme_600": float(k % 3 != 0),
            "gagne_confirme_900": float(g900),
            "e_mouvement_heure": -10.0 if k % 50 == 7 else 0.0,
            # Une entrée sur onze poussée au-delà de 1,55 (le seuil de M4).
            "e_distance_niveau": 1.8 if k % 11 == 0 else 1.2,
            "e_taille_bougie": 1.5 if k % 6 == 1 else 1.0,
            "e_part_parcourue": 0.6 if k % 4 == 0 else 0.2,
            "e_espace_obstacle": (1.0 if k % 8 == 0 else
                                  99.0 if k % 10 == 1 else 3.0),
            "mfe_15": 1.5 if g900 else 0.4, "mae_15": 0.3 if g900 else 1.6,
            "t_mfe": 3.0, "zone_cassee": 0.0 if g900 else 1.0,
            "t_pour": (8.0 if k % 9 == 0 else 2.0) if g900 else 0.0,
            "t_contre": 0.0 if g900 else 3.0,
            "net_4": 0.5, "net_10": 0.8, "net_15": 1.0 if g900 else -1.0})
    else:
        ctx["attente_confirmation"] = 0.0
    return Exemple(ts_sec=t0 + k * 3600 + 60, pair=PAIRES[k % 4],
                   contexte=ctx, gagne=imm[900], issues=imm)


def _en_direct(e: Exemple) -> bool:
    return e.contexte.get("mouvement_heure", 0.0) >= -3.0


class _FausseQualite:
    """Toutes les paires écoutées en entier, sauf B_otc au tiers."""

    def __init__(self, t0: int, heures: int):
        self.h = {p: {t0 // 3600 + h: (20 if p == "B_otc" else 60)
                      for h in range(heures)} for p in PAIRES}

    def minutes_par_paire_heure(self):
        return self.h


T0 = 1_790_000_000 // JOUR * JOUR


def _rapport(n: int = 400, t0: int = T0, **kw) -> dict:
    exemples = [_signal(k, t0) for k in range(n)]
    r = r8.reference8(exemples, _en_direct, 1,
                      qualite=_FausseQualite(t0, n + 24), debut=t0,
                      fin=t0 + (n + 24) * 3600, **kw)
    return json.loads(json.dumps(r))


def test_populations_et_disponibilite():
    r = _rapport()
    a = r["audit"]
    assert a["signaux_zoneh1"] == a["joues_au_signal"] == 400
    assert a["confirmes_au_signal"] == 320
    # La règle de l'heure relue à l'entrée écarte 8 confirmés (k % 50 = 7).
    assert a["confirmes_ecartes_a_l_entree"] == 8
    assert r["n"] == a["base"] == 312
    assert a["champ_libre"] == sum(1 for k in range(400)
                                   if k % 5 and k % 50 != 7
                                   and k % 10 == 1 and k % 8)
    assert a["disponibles"] == {"B": 312, "C": 312, "D": 312}
    assert a["autres_lectures"] == {"B entre 1,27 et 1,31": 0,
                                    "D entre 1,94 et 1,95": 0}


def test_livrable_a_table_croisee_et_decomposition():
    r = _rapport()
    a = r["a"]
    h15 = next(h for h in a["horizons"] if h["sec"] == 900)
    ks = [k for k in range(400) if k % 5]
    attendu = [sum(1 for k in ks if k % 3 and k % 4),
               sum(1 for k in ks if k % 3 and not k % 4), 0,
               sum(1 for k in ks if not k % 3)]
    assert h15["table"] == attendu and h15["n"] == 320
    # Sélection + retard = effet total, au même horizon.
    assert abs(h15["selection"] + h15["retard"] - h15["total"]) < 1e-3
    assert h15["retard"] < 0                 # la confirmation coûte ici
    assert h15["mcnemar"] is not None and h15["mcnemar"] < 0.001
    d = a["distance"]
    assert d["ecart"] == [0.7, 0.7, 0.7] and d["plus_loin"] == 320
    # Le retard pousse au-delà du seuil de M4 les entrées k % 11 = 0.
    assert d["entrent_m4"] == sum(1 for k in ks if k % 11 == 0)
    # B, C, D : les entrées que le retard fait basculer dans l'exclusion.
    v = {x["lettre"]: x for x in a["variables"]}
    assert v["C"]["entrent"] == sum(1 for k in ks if k % 4 == 0)
    assert v["D"]["entrent"] == sum(1 for k in ks if k % 8 == 0)
    assert v["B"]["entrent"] == sum(1 for k in ks if k % 6 == 1)
    assert a["excursions"]["mfe_15"]["immediate"][1] == 1.8


def test_livrable_b_modeles_chevauchement_et_valeurs():
    r = _rapport()
    b = r["b"]
    m = {x["id"]: x for x in b["modeles"]}
    assert [x["id"] for x in b["modeles"]] == ["M0", "M1", "M2", "M3", "M4",
                                               "D", "C+D"]
    assert m["M0"]["n"] == 312 and m["M0"]["exclus"] == 0
    assert m["M2"]["ev"] > m["M0"]["ev"]          # C écarte des perdantes
    assert m["M2"]["ecart"] > 0 and m["M1"]["ecart"] < 0
    c = b["chevauchement"]
    assert c["C∩D"] == c["D"] and c["D_aussi_C"] == 1.0
    assert c["seulement"]["D"] == [0, 0]
    pred = {x["lettre"]: x for x in b["predictive"]}
    assert pred["C"]["ecart"] > 0 and pred["B"]["ecart"] < 0
    # M4 : la distance, seule puis au-delà de B.
    joues = [k for k in range(400) if k % 5 and k % 50 != 7]
    loin = [k for k in joues if k % 11 == 0]
    assert m["M4"]["exclus"] == len(loin)
    dist = b["distance"]
    assert dist["ecartees"][0] == len(loin)
    assert dist["aussi_b"] == sum(1 for k in loin if k % 6 == 1)
    assert dist["au_dela_de_b"]["retirees"][0] == sum(
        1 for k in loin if k % 6 != 1)
    aj = {x["lettre"]: x for x in b["ajoutee"]}
    # D n'écarte rien que C n'écarte déjà : aucune valeur ajoutée.
    assert aj["D"]["retirees"] == [0, 0] and aj["D"]["ecart"] == 0
    assert aj["B"]["ecart"] < 0


def test_livrable_c_echeances_trajectoires_et_selection():
    r = _rapport()
    c = r["c"]
    assert c["apparies"] == 312
    comp = {(x["a"], x["b"]): x for x in c["comparaisons"]}
    assert sum(comp[(240, 900)]["table"]) == 312
    cl = {x["classe"]: x["n"] for x in c["classes"]}
    gagnees = sum(1 for k in range(400) if k % 5 and k % 50 != 7
                  and k % 3 and k % 4)
    assert cl["rapide"] + cl["lente"] == gagnees
    assert cl["invalidee"] == 312 - gagnees
    assert c["prealables"]["perdues, zone cassée"]["n"] == 312 - gagnees
    ad = c["adaptatif"]
    assert set(ad["regle"]) == {"C", "D", "B", "reste"}
    assert ad["n"] > 0


def test_couverture_et_sensibilite():
    r = _rapport()
    cv = r["couverture"]
    assert cv["paires"]["B_otc"]["heures"] == 0
    assert cv["paires"]["A_otc"]["couverture"] > 0.9
    # Les signaux de B_otc tombent hors des paire-heures exploitables.
    assert cv["signaux_hors"] == 100
    assert cv["sensibilite"]["faible"]["n"] == sum(
        1 for k in range(400) if k % 4 == 1 and k % 5 and k % 50 != 7)


def test_verdicts_regles_par_hypothese():
    r = _rapport()
    v = {i: verdict for i, _h, verdict in r8.hypotheses(r)}
    assert v["R8-A3"].startswith("✅")             # le retard coûte
    assert v["R8-A4"].startswith("✅")             # entrée plus loin
    # Favorable sur les données qui ont inspiré le seuil : indéterminée.
    assert v["R8-B-C"].startswith("≈") and "inspiré" in v["R8-B-C"]
    # Défavorable même là : non soutenue.
    assert v["R8-B-B"].startswith("❌") and v["R8-B+B"].startswith("❌")
    for ident in ("R8-M1", "R8-M2", "R8-M3", "R8-D", "R8-C+D", "R8-H"):
        assert v[ident].startswith("≈")
    assert {"R8-A1", "R8-A2", "R8-C4", "R8-C10", "R8-Q"} <= set(v)
    for verdict in v.values():
        assert verdict.split()[0] in ("✅", "❌", "≈")


def test_le_texte_est_du_html_telegram_propre():
    r = _rapport()
    sortie = r8.texte(r)
    for titre in ("0. Populations", "1. Couverture", "2. Livrable A",
                  "3. Livrable B", "4. Fenêtre", "5. Livrable C",
                  "6. Décisions"):
        assert titre in sortie
    nu = re.sub(r"</?(b|pre)>", "", sortie)
    assert not re.search(r"[<>&]", nu)
    assert r8.texte(None).startswith("🧭")


# --------------------------------------------------------------------------- #
# La fenêtre indépendante
# --------------------------------------------------------------------------- #

def test_la_fenetre_fige_son_plan_et_garde_son_registre(monkeypatch):
    ouv = ouverture_r8()
    t0 = ouv - 10 * JOUR
    monkeypatch.delenv("P2_GAIN_MIN_POINTS", raising=False)
    r = _rapport(t0=t0)
    f = r["fenetre"]
    apres = sum(1 for k in range(400)
                if k % 5 and k % 50 != 7 and t0 + k * 3600 + 120 >= ouv)
    assert f["n"] == apres > 0
    assert f["plan"]["fige_le"] and "gain" not in f["plan"]
    assert f["modeles"]["M1"]["verdict"].startswith("≈ indéterminée — "
                                                    "volume cible non fixé")
    # Le gain fixé ensuite est lu une fois ; le plan, lui, ne bouge pas.
    monkeypatch.setenv("P2_GAIN_MIN_POINTS", "60")
    exemples = [_signal(k, t0) for k in range(400)]
    deux = json.loads(json.dumps(r8.reference8(
        exemples[200:], _en_direct, 1, ancien=r, debut=t0,
        fin=t0 + 424 * 3600)))
    f2 = deux["fenetre"]
    assert f2["plan"]["fige_le"] == f["plan"]["fige_le"]
    assert f2["plan"]["p"] == f["plan"]["p"] and f2["plan"]["gain"] == 60
    # Les entrées sorties du rejeu restent au registre.
    assert f2["n"] == apres
    # 60 points : un volume minuscule, déjà atteint — verdict figé.
    m1 = f2["modeles"]["M1"]
    assert m1["cible"] is not None and m1["cible"] <= apres
    assert m1["verdict"].split()[0] in ("✅", "❌", "≈")
    assert "M1" in f2["finals"]
    # R8-H : figée dès 100 entrées, et relue telle quelle ensuite.
    assert f2["echeance"]["n"] >= 100 and "R8-H" in f2["finals"]
    trois = r8.reference8(exemples[300:], _en_direct, 1, ancien=deux,
                          debut=t0, fin=t0 + 430 * 3600)
    assert trois["fenetre"]["echeance"] == f2["echeance"]
    assert trois["fenetre"]["modeles"]["M1"] == m1


def test_peu_d_entrees_garde_le_registre():
    r = _rapport()
    maigre = r8.reference8([_signal(k, T0) for k in range(20)], _en_direct,
                           1, ancien=r)
    assert "note" in maigre and maigre["fenetre"] == r["fenetre"]
    assert r8.hypotheses(maigre) == []
    assert r8.texte(maigre).startswith("🧭 Seulement")


def test_le_journal_du_protocole_compte_la_mission():
    from maxprofit.apprentissage.indicateurs import Indicateurs
    from maxprofit.live.protocole import protocole
    r = _rapport()
    debut = T0
    p = protocole(
        [], lambda e: True, 1, 900,
        qualite=Qualite(debut, debut + 10 * JOUR, debut + 5 * JOUR),
        indicateurs=Indicateurs(debut, debut + 5 * JOUR), exclusions={},
        couverture=None, experiences={"coupure": None, "variables": []},
        ancien=None, version_regles="ZoneH1:abcd", version_experiences=10,
        debut=debut, fin=debut + 10 * JOUR, gel=None, jours=10,
        reference8=r)
    ids = {h[0] for h in p["hypotheses"]}
    assert {"R8-A1", "R8-M3", "R8-H"} <= ids
    assert "R8-A1" in p["journal"]


def test_un_registre_et_un_plan_d_avant_m4_sont_completes(monkeypatch):
    """Les fiches écrites avant l'ajout de la distance sont remplacées par
    leur version complète ; un plan figé sans M4 reçoit sa part, mesurée
    sur les mêmes entrées d'avant l'ouverture."""
    ouv = ouverture_r8()
    t0 = ouv - 10 * JOUR
    monkeypatch.setenv("P2_GAIN_MIN_POINTS", "10")
    r = _rapport(t0=t0)
    ancien = json.loads(json.dumps(r))
    f = ancien["fenetre"]
    f["registre"] = [x[:9] for x in f["registre"]]
    part_m4 = f["plan"]["parts"].pop("M4")
    f["finals"] = {k: v for k, v in f["finals"].items() if k != "M4"}
    exemples = [_signal(k, t0) for k in range(400)]
    deux = r8.reference8(exemples, _en_direct, 1, ancien=ancien, debut=t0,
                         fin=t0 + 424 * 3600)
    f2 = deux["fenetre"]
    assert all(len(x) == 10 for x in f2["registre"])
    assert f2["plan"]["parts"]["M4"] == part_m4
    assert f2["plan"]["fige_le"] == f["plan"]["fige_le"]
    assert "R8-M4" in {i for i, _h, _v in r8.hypotheses(deux)}
