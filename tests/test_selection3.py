"""
Étape 3 — `/micro_selection` de bout en bout, sur deux bases SQLite écrites
par le vrai agrégateur (la relève et l'active) :
- aucune des deux bases ne change (lecture seule) ;
- la copie est vérifiée paire × jour : un paire-jour divergent est lu sur
  l'active, et le document du gel n'en dépend pas ;
- deux passes rendent le même document, et son empreinte survit au JSON ;
- un arrêt du plan (VALID < 80 %) empêche le document du gel ;
- les hypothèses de la passe sont celles du document gelé.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.collect.collector import CandleAggregator
from maxprofit.core.types import Direction, PairInfo, Signal, Tick
from maxprofit.live import selection3
from maxprofit.store.chemin_ticks import encoder
from maxprofit.store.db import open_read_only, open_read_write
from maxprofit.store.market import MarketWriter

P = "EURUSD_otc"
DEBUT = em.FIN_EXPLORATION - 2 * 86400          # 19/10 00:00 UTC
FIN = em.FIN_EXPLORATION + 2 * 86400            # 23/10 00:00 UTC
APRES = em.FIN_VALIDATION + 86400


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def _flux(debut: int, fin: int, pas_ms: int) -> list[Tick]:
    alea = random.Random(11)
    prix, ticks = 1.08000, []
    for ms in range(debut * 1000, fin * 1000, pas_ms):
        prix = round(prix + alea.choice((-2, -1, 0, 1, 2)) * 0.00001, 5)
        ticks.append(Tick(P, ms + 137, prix))
    return ticks


class _Strategie:
    """Un signal toutes les 31 minutes, CALL ou PUT selon la minute, niveau
    juste du côté de la zone."""

    def __init__(self, p) -> None:
        self.p = p

    def on_bar(self, vue):
        derniere = vue.candles(1)[-1]
        minute = derniere.ts_sec // 60
        if minute % 31:
            return None
        call = bool(minute % 2)
        ecart = -0.00005 if call else 0.00005
        return Signal(pair=vue.pair,
                      direction=Direction.CALL if call else Direction.PUT,
                      decided_at_ms=vue.now_ms, expiry_sec=900,
                      features={"niveau": round(derniere.close + ecart, 5),
                                "touches": float(minute % 3),
                                "entrees_deja_offertes": 0.0})


def _base(chemin: Path, pas_ms: int = 3000) -> Path:
    conn = open_read_write(chemin)
    ecrivain = MarketWriter(conn)
    agg = CandleAggregator(garder_ticks=True)
    for t in _flux(DEBUT - 6 * 3600, FIN + 2 * 3600, pas_ms):
        agg.add(t)
    ecrivain.upsert_candles(agg.drain_all())
    ecrivain.insert_tick_paths([encoder(p) for p in agg.drain_ticks()])
    for k, pct in enumerate((92, 90, 92, 88)):
        ecrivain.insert_payouts(DEBUT - 6 * 3600 + k * 86400,
                                [PairInfo(P, True, pct)])
    conn.commit()
    conn.close()
    return chemin


@pytest.fixture(scope="module")
def bases(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("selection")
    active = _base(dossier / "active.db")
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    return active, releve


def _course(chemin):
    from maxprofit.apprentissage.historique import exemples_historiques
    from maxprofit.live.protocole import marquer_qualite
    from maxprofit.store.market import MarketReader
    p = SimpleNamespace(lookback=30, expiry_sec=900, tolerance_pct=0.02)
    conn = open_read_only(chemin)
    try:
        bougies = MarketReader(conn).candles(P, 60, 0, 4_000_000_000)
    finally:
        conn.close()
    niveaux, sans_issue = {}, []
    exemples = exemples_historiques({P: bougies}, _Strategie(p),
                                    echeance_sec=900, niveaux=niveaux,
                                    sans_issue=sans_issue)
    marquer_qualite(exemples, SimpleNamespace(touche=lambda *a: False), 900)
    return SimpleNamespace(strategie=_Strategie(p), paires_collectees=(P,),
                           rejeu_en_memoire=(exemples, None, 1, "v"),
                           niveaux_en_memoire=niveaux,
                           sans_issue_en_memoire=sans_issue)


def _empreinte(chemin) -> str:
    return hashlib.sha256(Path(chemin).read_bytes()).hexdigest()


def _passe(releve, active, course=None, **options):
    course = course or _course(active)
    a = open_read_only(releve)
    b = open_read_only(active) if active is not None else None
    try:
        return selection3.selection(a, b, course, maintenant=APRES,
                                    respirer=lambda: None, **options)
    finally:
        a.close()
        if b is not None:
            b.close()


def _document(fichiers) -> dict | None:
    for contenu, nom, _ in fichiers:
        if nom == "parametres_gel2.json":
            return json.loads(contenu.decode("utf-8"))
    return None


@pytest.fixture(scope="module")
def premiere_passe(bases):
    active, releve = bases
    avant = (_empreinte(active), _empreinte(releve))
    rapport, fichiers = _passe(releve, active)
    return avant, rapport, fichiers


def test_la_passe_de_bout_en_bout_ne_touche_pas_les_bases(bases,
                                                          premiere_passe):
    active, releve = bases
    avant, rapport, fichiers = premiere_passe
    assert (_empreinte(active), _empreinte(releve)) == avant
    noms = [nom for _, nom, _ in fichiers]
    assert noms == ["selection_signaux.csv", "selection_retrait.csv",
                    "selection_registre.csv", "selection_exploratoire.csv",
                    "parametres_gel2.json"]
    assert "identique ✅" in rapport
    assert "anti-fuite" in rapport and "requêtes refusées : 0" in rapport
    assert "Rien n'a été écrit en base." in rapport


def test_le_document_du_gel_et_son_empreinte(premiere_passe):
    _, rapport, fichiers = premiere_passe
    doc = _document(fichiers)
    assert doc is not None
    # L'empreinte publiée est celle du fichier relu : elle survit au JSON.
    assert em.empreinte(doc) in rapport
    assert doc["gel_hypotheses"] == em.EMPREINTE_GEL_HYPOTHESES
    assert doc["donnees"]["copie_verifiee"] is True
    assert doc["donnees"]["lus_sur"] == {"relève": 6}
    assert set(doc["hypotheses"]) == {"H1", "H2"}
    for h in ("H1", "H2"):
        x = doc["hypotheses"][h]
        assert x["statut"] == "testée"
        assert len(x["terciles"]) == 6, "une paire de bornes par mesure"
        assert x["signe_concordant"] == (x["coefficient"] * -1 > 0)
    modeles = doc["modeles"]
    # M0 et chaque hypothèse × 6 mesures × 2 règles d'égalité, plus H1 + H2.
    assert len(modeles) == 2 + 2 * 6 * 2 + 1
    m = modeles["M_H1|distincts|k0|exclues"]
    assert m["colonnes"][0] == "interception"
    assert "A7_penetration@300" in m["colonnes"]
    assert len(m["coefficients"]) == len(m["colonnes"])
    # Les signaux à égalité entrent dans la variante « perdues ».
    assert modeles["M0|perdues"]["n"] >= modeles["M0|exclues"]["n"]
    # La préparation ne vient que de l'exploration.
    assert doc["population"]["exploration"]["signaux"] > 30


def test_les_signaux_n_utilisent_que_la_periode_de_selection(premiere_passe):
    _, _, fichiers = premiere_passe
    texte = fichiers[0][0].decode("utf-8").splitlines()
    entete = texte[0].split(";")
    periodes = {ligne.split(";")[entete.index("periode")]
                for ligne in texte[1:]}
    assert periodes == {em.EXPLORATION, em.VALIDATION}
    decisions = [int(ligne.split(";")[entete.index("decision")])
                 for ligne in texte[1:]]
    assert max(decisions) < em.FIN_VALIDATION


def test_une_copie_divergente_est_lue_sur_l_active_sans_changer_le_gel(
        bases, premiere_passe, tmp_path):
    active, releve = bases
    _, _, fichiers = premiere_passe
    abimee = tmp_path / "releve_abimee.db"
    shutil.copy(releve, abimee)
    conn = open_read_write(abimee)
    minute = em.FIN_EXPLORATION + 12 * 3600          # 21/10 midi
    conn.execute("DELETE FROM tick_paths WHERE minute_sec = ?", (minute,))
    conn.commit()
    conn.close()
    rapport, autres = _passe(abimee, active)
    doc, reference = _document(autres), _document(fichiers)
    assert "résumé(s) différent(s)" in rapport
    # La veille, le jour et le lendemain du 21/10 sont lus sur l'active.
    assert doc["donnees"]["lus_sur"] == {"active": 3, "relève": 3}
    for cle_ in ("hypotheses", "preparation", "modeles", "retrait"):
        assert doc[cle_] == reference[cle_], cle_


def test_une_releve_qui_commence_plus_tard_n_ecourte_pas_la_passe(
        bases, premiere_passe, tmp_path):
    active, releve = bases
    _, _, fichiers = premiere_passe
    tardive = tmp_path / "releve_tardive.db"
    shutil.copy(releve, tardive)
    conn = open_read_write(tardive)
    for table, colonne in (("tick_paths", "minute_sec"), ("candles", "ts_sec"),
                           ("payouts", "ts_sec")):
        conn.execute(f"DELETE FROM {table} WHERE {colonne} < ?", (DEBUT,))
    conn.commit()
    conn.close()
    _, autres = _passe(tardive, active)
    doc, reference = _document(autres), _document(fichiers)
    assert doc["donnees"]["premiere_minute_de_ticks"] == \
        reference["donnees"]["premiere_minute_de_ticks"]
    assert doc["donnees"]["lus_sur"].get("active", 0) >= 1
    assert doc["modeles"] == reference["modeles"]


def test_deux_passes_rendent_le_meme_document(bases, premiere_passe):
    active, releve = bases
    _, _, fichiers = premiere_passe
    _, autres = _passe(releve, active)
    a, b = _document(fichiers), _document(autres)
    a.pop("calcule_le"), b.pop("calcule_le")
    assert em.empreinte(a) == em.empreinte(b)


def test_sans_active_la_copie_est_dite_non_verifiee(bases, premiere_passe):
    active, releve = bases
    _, _, fichiers = premiere_passe
    rapport, autres = _passe(releve, None, course=_course(active))
    assert "copie NON vérifiée" in rapport
    doc = _document(autres)
    assert doc["donnees"]["copie_verifiee"] is False
    assert doc["modeles"] == _document(fichiers)["modeles"]


def test_trop_peu_de_fenetres_valid_arrete_le_gel(tmp_path):
    # Un tick toutes les 12 s : aucun écart sous 10 s, aucune fenêtre VALID.
    chemin = _base(tmp_path / "creuse.db", pas_ms=12_000)
    rapport, fichiers = _passe(chemin, chemin)
    assert _document(fichiers) is None
    assert "NON produit" in rapport and "fenêtres VALID" in rapport


def test_sans_signaux_sans_issue_en_memoire_la_passe_attend(bases):
    active, releve = bases
    course = _course(active)
    course.sans_issue_en_memoire = None
    rapport, fichiers = _passe(releve, active, course=course)
    assert rapport == selection3.ATTENDRE_LE_REJEU and fichiers == []


def test_les_hypotheses_de_la_passe_sont_celles_du_gel():
    racine = Path(__file__).resolve().parent.parent
    doc = json.loads((racine / "docs" / "etape3_preinscription.json")
                     .read_text(encoding="utf-8"))
    gelees = {h["id"]: (h["variable"], h["fenetre_sec"], h["sens"],
                        h["tercile"]) for h in doc["hypotheses"]}
    assert gelees == selection3.HYPOTHESES
    assert [list(m) for m in selection3.MESURES] == [
        [t, k] for t in doc["sensibilites"]["traitement"]
        for k in doc["sensibilites"]["troncature_sec"]]
    assert list(selection3.EGALITES) == doc["sensibilites"]["egalites"]


def test_le_service_refuse_sans_releve_et_n_ecrit_rien(tmp_path):
    from maxprofit.hosting.service import _micro_selection
    course = SimpleNamespace()
    texte, fichiers = _micro_selection(tmp_path / "x.db", course, "",
                                       environ={})
    assert "Aucune relève" in texte and fichiers == []
    assert _micro_selection(tmp_path / "x.db", None, "") == (
        "Course indisponible.", [])


def test_la_copie_compare_chaque_table_par_paire_et_par_jour():
    a = {("candles", P, 1): (1, 2), ("tick_paths", P, 1): (3, 4)}
    b = {("candles", P, 1): (1, 2), ("tick_paths", P, 1): (3, 5),
         ("payouts", P, 4): (1, 92, 1)}
    divergents = selection3.divergences(a, b)
    assert divergents == {("tick_paths", P, 1), ("payouts", P, 4)}
    assert not selection3.lu_sur_la_releve(divergents, P, 0)
    assert not selection3.lu_sur_la_releve(divergents, P, 2)
    assert selection3.lu_sur_la_releve({("tick_paths", P, 1)}, P, 3)
    assert not selection3.lu_sur_la_releve({("payouts", P, 4)}, P, 3)
    assert selection3.lu_sur_la_releve({("payouts", P, 4)}, P, 2)


def test_le_payout_est_celui_connu_avant_la_decision():
    releves = [(100, 92), (200, 90), (300, 88)]
    assert selection3.payout_a(releves, 50) is None
    assert selection3.payout_a(releves, 200) == 92.0
    assert selection3.payout_a(releves, 201) == 90.0
