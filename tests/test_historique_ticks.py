"""
`/historique_ticks` de bout en bout, sur deux bases SQLite (la relève et
l'active) :
- une paire saine, une dégradée APRÈS le 28/10, une dégradée AVANT ;
- une heure sans aucun battement est « sans collecte », jamais un silence ;
- ticks bruts et distincts (doublons de la double réception) ;
- une copie divergente est reprise de l'active, et le dit ;
- les deux bases restent inchangées.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from maxprofit.apprentissage import etude_micro as em
from maxprofit.core.types import Tick
from maxprofit.live import historique_ticks as ht
from maxprofit.store.chemin_ticks import encoder
from maxprofit.store.db import open_read_only, open_read_write
from maxprofit.store.market import MarketWriter

DEBUT = em.FIN_VALIDATION - 86400          # 27/10 00:00 UTC
FIN = em.FIN_VALIDATION + 86400            # 29/10 00:00 UTC
MAINTENANT = FIN + ht.RETARD_ECRITURE_SEC
COUPURE = em.FIN_VALIDATION + 6 * 3600     # 28/10 06:00 : plus de battement
B_SE_TAIT = em.FIN_VALIDATION + 12 * 3600  # 28/10 12:00
C_SE_TAIT = em.FIN_VALIDATION - 10 * 3600  # 27/10 14:00


@pytest.fixture(autouse=True)
def _sans_postgres(monkeypatch):
    for nom in ("DATABASE_URL", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(nom, raising=False)


def _base(chemin: Path) -> Path:
    conn = open_read_write(chemin)
    ecrivain = MarketWriter(conn)
    for ts in range(DEBUT, FIN, 30):
        if not COUPURE <= ts < COUPURE + 3600:
            ecrivain.heartbeat(ts, 3)
    paquets = []
    for minute in range(DEBUT, FIN, 60):
        if COUPURE <= minute < COUPURE + 3600:
            continue
        for paire, se_tait in (("A_otc", None), ("B_otc", B_SE_TAIT),
                               ("C_otc", C_SE_TAIT)):
            if se_tait is not None and minute >= se_tait:
                continue
            ms = minute * 1000
            ticks = [Tick(paire, ms + 1000, 1.10001),
                     Tick(paire, ms + 31000, 1.10002)]
            if paire == "A_otc":           # doublon exact : double réception
                ticks.append(Tick(paire, ms + 31000, 1.10002))
            paquets.append(encoder(ticks))
    ecrivain.insert_tick_paths(paquets)
    conn.commit()
    conn.close()
    return chemin


@pytest.fixture(scope="module")
def bases(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("historique")
    active = _base(dossier / "active.db")
    releve = dossier / "releve.db"
    shutil.copy(active, releve)
    return active, releve


def _empreinte(chemin) -> str:
    return hashlib.sha256(Path(chemin).read_bytes()).hexdigest()


def _passe(lue, active):
    a = open_read_only(lue)
    b = open_read_only(active) if active is not None else None
    try:
        return ht.historique(a, b, ["A_otc", "B_otc", "C_otc"],
                             maintenant=MAINTENANT, jours=2,
                             respirer=lambda: None)
    finally:
        a.close()
        if b is not None:
            b.close()


def _heures(fichiers) -> list[dict]:
    texte = fichiers[0][0].decode("utf-8").splitlines()
    entete = texte[0].split(";")
    return [dict(zip(entete, ligne.split(";"))) for ligne in texte[1:]]


def _silences(fichiers) -> list[dict]:
    texte = fichiers[1][0].decode("utf-8").splitlines()
    entete = texte[0].split(";")
    return [dict(zip(entete, ligne.split(";"))) for ligne in texte[1:]]


@pytest.fixture(scope="module")
def passe(bases):
    active, releve = bases
    avant = (_empreinte(active), _empreinte(releve))
    rapport, fichiers = _passe(releve, active)
    return avant, rapport, fichiers


def test_les_bases_ne_changent_pas_et_la_copie_est_verifiee(bases, passe):
    active, releve = bases
    avant, rapport, _ = passe
    assert (_empreinte(active), _empreinte(releve)) == avant
    assert "identique ✅" in rapport
    assert "requêtes refusées : 0" in rapport
    assert "Rien n'a été écrit en base." in rapport


def test_les_dates_de_degradation_avant_et_apres_le_28(passe):
    _, rapport, _ = passe
    assert ("1 paire(s) dégradée(s) en continu depuis une date APRÈS le "
            "28/10, 1 depuis AVANT, 1 non dégradée(s)") in rapport
    assert "- B_otc : dégradée en continu depuis 28/10 12:00" in rapport
    assert "- C_otc : dégradée en continu depuis 27/10 14:00" in rapport
    assert "- A_otc : active à la fin de la fenêtre" in rapport


def test_une_heure_sans_battement_est_sans_collecte_pas_un_silence(passe):
    _, _, fichiers = passe
    coupure = [h for h in _heures(fichiers)
               if h["heure_utc"] == "2026-10-28 06:00"]
    assert {h["statut"] for h in coupure} == {ht.SANS_COLLECTE}
    assert {h["minutes_collecte"] for h in coupure} == {"0"}
    a = next(h for h in _heures(fichiers) if h["paire"] == "A_otc"
             and h["heure_utc"] == "2026-10-28 05:00")
    assert a["statut"] == ht.ACTIVE and a["minutes_avec_tick"] == "60"
    b = next(h for h in _heures(fichiers) if h["paire"] == "B_otc"
             and h["heure_utc"] == "2026-10-28 13:00")
    assert b["statut"] == ht.SILENCE and b["minutes_collecte"] == "60"


def test_ticks_bruts_et_distincts(passe):
    _, _, fichiers = passe
    a = next(h for h in _heures(fichiers) if h["paire"] == "A_otc"
             and h["heure_utc"] == "2026-10-27 10:00")
    assert (a["ticks_bruts"], a["ticks_distincts"]) == ("180", "120")
    assert a["premier_tick_utc"] == "2026-10-27 10:00:01"
    assert a["dernier_tick_utc"] == "2026-10-27 10:59:31"


def test_les_silences_et_leur_nature(passe):
    _, _, fichiers = passe
    silences = _silences(fichiers)
    # A : seulement l'heure sans collecte, qui n'est pas un silence.
    a = [s for s in silences if s["paire"] == "A_otc"]
    assert len(a) == 1 and a[0]["nature"] == "sans collecte"
    assert a[0]["duree_min"] == "60" and a[0]["reprise"] == "oui"
    # B : silencieuse depuis midi, collecte continue, en cours.
    b = [s for s in silences if s["paire"] == "B_otc"]
    assert b[-1]["debut_utc"] == "2026-10-28 12:00"
    assert b[-1]["nature"] == "silence (collecte continue)"
    assert b[-1]["reprise"] == "non (en cours)"
    # C : silencieuse depuis le 27 à 14 h ; la coupure tombe dedans.
    c = [s for s in silences if s["paire"] == "C_otc"]
    assert len(c) == 1 and c[0]["debut_utc"] == "2026-10-27 14:00"
    assert c[0]["minutes_sans_battement"] == "60"
    assert c[0]["nature"].startswith("silence")


def test_une_copie_divergente_est_reprise_de_l_active(bases, passe, tmp_path):
    active, releve = bases
    _, _, fichiers = passe
    abimee = tmp_path / "releve_abimee.db"
    shutil.copy(releve, abimee)
    conn = open_read_write(abimee)
    heure = DEBUT + 10 * 3600
    conn.execute("DELETE FROM tick_paths WHERE pair = 'A_otc' AND "
                 "minute_sec >= ? AND minute_sec < ?", (heure, heure + 1800))
    conn.commit()
    conn.close()
    rapport, autres = _passe(abimee, active)
    assert "1 paire(s) × heure(s)" in rapport
    ligne = next(h for h in _heures(autres) if h["paire"] == "A_otc"
                 and h["heure_utc"] == "2026-10-27 10:00")
    assert ligne["copie"].startswith("divergente")
    assert ligne["minutes_avec_tick"] == "60" and ligne["ticks_bruts"] == "180"
    assert ligne["ticks_distincts"] == ""
    # Le reste est identique à la passe sur une copie intacte.
    assert [h for h in _heures(autres) if h["heure_utc"] != "2026-10-27 10:00"] \
        == [h for h in _heures(fichiers)
            if h["heure_utc"] != "2026-10-27 10:00"]


def test_sans_active_la_copie_est_dite_non_verifiee(bases, passe):
    _, releve = bases
    _, _, fichiers = passe
    rapport, autres = _passe(releve, None)
    assert "copie NON vérifiée" in rapport
    assert _heures(autres) == _heures(fichiers)


def test_les_jours_demandes():
    assert ht.analyser_jours("") == 7
    assert ht.analyser_jours("21") == 21
    for mauvais in ("0", "22", "x"):
        with pytest.raises(ValueError):
            ht.analyser_jours(mauvais)


def test_le_service_refuse_sans_releve(tmp_path):
    from types import SimpleNamespace

    from maxprofit.hosting.service import _historique_ticks
    texte, fichiers = _historique_ticks(tmp_path / "x.db", SimpleNamespace(),
                                        "", environ={})
    assert "Aucune relève" in texte and fichiers == []
    texte, _ = _historique_ticks(tmp_path / "x.db", None, "99", environ={})
    assert "Jours illisibles" in texte
