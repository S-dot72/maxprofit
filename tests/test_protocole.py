"""Le protocole de simulation OTC : le rapport, le gel et le test aveugle."""

from __future__ import annotations

import json
import random
import re

from maxprofit.apprentissage.indicateurs import Indicateurs
from maxprofit.apprentissage.lecons import Exemple
from maxprofit.apprentissage.qualite import Qualite
from maxprofit.core.types import Candle
from maxprofit.live import protocole as mod
from maxprofit.live.protocole import (
    SEUIL_RENTABLE, _verdict, date_de_gel, marquer_qualite, protocole, texte)

JOUR = 86400
T0 = 1_790_000_000 // JOUR * JOUR
PAIRES = ("EURUSD_otc", "GBPUSD_otc")


def _bougies(jours: int, paire: str, graine: int,
             sauter: tuple[int, ...] = ()) -> list[Candle]:
    rng = random.Random(graine)
    prix, sortie = 1.1, []
    for i in range(jours * 1440):
        ouverture = prix
        prix *= 1 + rng.gauss(0, 0.0006)
        if i in sauter:
            continue
        sortie.append(Candle(
            pair=paire, tf_sec=60, ts_sec=T0 + 60 * i, open=ouverture,
            high=max(ouverture, prix) * 1.0001,
            low=min(ouverture, prix) * 0.9999, close=prix, tick_count=40,
            complete=True))
    return sortie


def _ex(t: int, gagne: bool, paire: str = "EURUSD_otc", **ctx) -> Exemple:
    base = {"mouvement_heure": 0.0, "retournement_suivant": 1.0,
            "gagne_suivant": float(gagne), "attente_confirmation": 1.0,
            "gagne_confirme": float(gagne), "e_distance_niveau": 1.5}
    base.update(ctx)
    return Exemple(ts_sec=t, pair=paire, contexte=base, gagne=gagne)


def _mesures(jours: int, gel: int | None):
    debut, fin = T0, T0 + jours * JOUR
    coupure = debut + int(0.7 * (fin - debut))
    qualite = Qualite(debut, fin, coupure)
    indicateurs = Indicateurs(debut, coupure, gel)
    for k, paire in enumerate(PAIRES):
        bougies = _bougies(jours, paire, k + 1, sauter=(500, 501))
        qualite.ajouter(bougies)
        indicateurs.ajouter(bougies)
    return qualite, indicateurs


def _exemples(jours: int) -> list[Exemple]:
    sortie = []
    for i in range(jours * 12):
        t = T0 + i * 7200 + 300
        sortie.append(_ex(t, i % 3 != 0, PAIRES[i % 2],
                          e_distance_niveau=float(i % 3)))
    return sortie


EXPERIENCES = {"coupure": T0 + 3 * JOUR, "variables": [
    {"cle": "distance_niveau", "libelle": "entrée tardive",
     "coupes": [0.5, 1.5], "pire": 0, "valeurs": None, "tiers": [],
     "gradient": False, "verdict": "❌ pas mieux que la référence"}]}


def _rapport(jours, gel, ancien=None, experiences=EXPERIENCES,
             exemples=None, version="ZoneH1:abcd1234"):
    qualite, indicateurs = _mesures(jours, gel)
    exemples = _exemples(jours) if exemples is None else exemples
    marquer_qualite(exemples, qualite, 900)
    r = protocole(
        exemples, lambda e: True, 1, 900, qualite=qualite,
        indicateurs=indicateurs, exclusions={"signaux sans bougie à "
                                             "l'échéance": 3},
        couverture=None, experiences=experiences, ancien=ancien,
        version_regles=version, version_experiences=6, debut=T0,
        fin=T0 + jours * JOUR, gel=gel, jours=jours)
    # Le résultat est sauvé avec l'état : il doit traverser le JSON.
    return json.loads(json.dumps(r))


def test_date_de_gel(monkeypatch):
    monkeypatch.delenv("DATE_GEL", raising=False)
    assert date_de_gel() == 1_791_936_000            # 2026-10-14 0 h UTC
    monkeypatch.setenv("DATE_GEL", "2026-11-02")
    assert date_de_gel() == 1_793_577_600
    monkeypatch.setenv("DATE_GEL", "aucune")
    assert date_de_gel() is None
    monkeypatch.setenv("DATE_GEL", "demain")
    assert date_de_gel() == 1_791_936_000


def test_verdicts_des_paris():
    assert _verdict([20, 12, 20, 12], (0.5, 0.7)).startswith("indécise")
    assert _verdict([100, 60, 100, 60], None).startswith("indécise")
    assert _verdict([100, 60, 100, 58], (0.53, 0.66)).startswith("✅")
    assert _verdict([100, 40, 100, 41], (0.33, 0.47)).startswith("↩")
    assert _verdict([1000, 515, 1000, 512], (0.505, 0.52)).startswith("≈")
    assert _verdict([100, 52, 100, 47], (0.42, 0.58)).startswith("❌")
    assert _verdict([100, 50, 100, 51], (0.45, 0.56), True).startswith("✅")
    assert _verdict([100, 70, 100, 70], (0.6, 0.8), True).startswith("⚠")
    assert 0.5208 < SEUIL_RENTABLE < 0.5209


def test_le_drapeau_de_qualite_suit_le_signal():
    qualite = Qualite(T0, T0 + JOUR, T0 + JOUR)
    qualite.ajouter(_bougies(1, "EURUSD_otc", 1, sauter=(500,)))
    loin = _ex(T0 + 60 * 300, True)                 # 200 min avant le trou
    avant = _ex(T0 + 60 * 490, True)                # le trou est en chemin
    marquer_qualite([loin, avant], qualite, 900)
    assert loin.contexte["donnees_douteuses"] == 0.0
    assert avant.contexte["donnees_douteuses"] == 1.0


def test_rapport_complet_et_son_texte():
    r = _rapport(6, None)
    q = r["exp001"]
    assert q["paires"]["EURUSD_otc"]["manquantes"] == 2
    assert q["exclusions"] == {"signaux sans bougie à l'échéance": 3}
    assert q["signaux"]["touchees"][0] + q["signaux"]["touchees"][2] >= 1
    ids = [p["id"] for p in r["exp002"]["paris"]]
    assert ids == ["H2-RSI", "H2-MACD", "H2-CROISEMENT"]
    temoin = r["exp002"]["temoins"][0]
    n = temoin["periodes"][0] + temoin["periodes"][2]
    assert n > 10_000
    sources = r["exp003"]["sources"]
    assert sources[0]["n"] == len(_exemples(6))
    assert len(sources) == 4 and all(s["semaines"] for s in sources)
    assert r["exp004"]["note"].startswith("désactivé")
    assert r["reproductibilite"]["empreinte"]
    assert {"VAR distance_niveau", "H2-RSI", "TÉMOIN temoin"} <= set(
        r["journal"])
    sortie = texte(r)
    for titre in ("EXP-001", "EXP-002", "EXP-003", "EXP-004",
                  "Journal des hypothèses", "Reproductibilité"):
        assert titre in sortie
    assert "trous de collecte" in sortie
    assert not re.search(r"<(?!/?(b|code)>)", sortie)


def test_gel_fige_puis_test_aveugle_final():
    gel = T0 + 4 * JOUR
    avant = _rapport(3, gel)
    assert avant["exp004"]["note"] == "pas encore commencé"

    pendant = _rapport(6, gel, ancien=avant)
    gele = pendant["exp004"]["gele"]
    assert gele["variables"]["distance_niveau"][:3] == [0.5, 1.5, 0]
    assert gele["reference"][0] == len([e for e in _exemples(6)
                                        if e.ts_sec < gel])
    en_cours = pendant["exp004"]["en_cours"]
    assert en_cours["jours"] == 2.0
    n_apres = len([e for e in _exemples(6) if e.ts_sec >= gel])
    assert en_cours["reference"][0] == n_apres
    # Sans le pire tiers figé (distance < 0,5, donc i % 3 == 0) : les
    # deux tiers restent, et ils gagnent tous.
    (var,) = en_cours["variables"]
    assert var["sans_pire"][0] == var["sans_pire"][1] > 0
    assert all(p["avant"] is not None for p in en_cours["paris"])

    # De nouvelles coupes au rejeu suivant ne changent RIEN au gel.
    autres = {**EXPERIENCES, "variables": [
        {**EXPERIENCES["variables"][0], "coupes": [9.0, 10.0], "pire": 2}]}
    encore = _rapport(6, gel, ancien=pendant, experiences=autres)
    assert encore["exp004"]["gele"] == gele

    # La fenêtre aveugle finie, le résultat est figé…
    fini = _rapport(19, gel, ancien=encore)
    final = fini["exp004"]["final"]
    assert final["fige_le"] == T0 + 19 * JOUR
    # … et ne bouge plus, même si les données de la fenêtre changent.
    apres = _rapport(19, gel, ancien=fini, exemples=_exemples(19)[:5])
    assert apres["exp004"]["final"] == final
    assert not apres["exp004"]["regles_modifiees"]
    change = _rapport(19, gel, ancien=fini, version="ZoneH1:ffff0000")
    assert change["exp004"]["regles_modifiees"]
    assert "plus aveugle" in texte(change)
    assert "résultat FINAL" in texte(change)


def test_le_journal_garde_les_verdicts_d_un_rejeu_a_l_autre():
    premier = _rapport(3, None)
    second = _rapport(4, None, ancien=premier)
    verdicts = second["journal"]["VAR distance_niveau"]["verdicts"]
    assert [v[1] for v in verdicts] == ["❌", "❌"]
    assert second["journal"]["VAR distance_niveau"]["premiere"] == \
        T0 + 3 * JOUR
    # Une hypothèse qui disparaît d'un rejeu reste au journal.
    sans = _rapport(5, None, ancien=second,
                    experiences={"coupure": None, "variables": []})
    assert "VAR distance_niveau" in sans["journal"]
    assert "VAR distance_niveau" not in {h[0] for h in sans["hypotheses"]}


def test_le_commit_vient_de_l_hebergeur(monkeypatch):
    for nom in mod.VARIABLES_COMMIT:
        monkeypatch.delenv(nom, raising=False)
    assert mod.commit() is None
    monkeypatch.setenv("RENDER_GIT_COMMIT", "0123456789abcdef")
    assert mod.commit() == "0123456"
