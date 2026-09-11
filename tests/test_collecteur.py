"""
Le collecteur, et en particulier les deux défauts que seuls un démarrage réel
et une panne réelle font apparaître.

Les deux premiers tests sont des régressions : ils ont été écrits après avoir
observé les pannes, et ils échouent sur le code d'avant.

1. `test_ouvre_sa_base_dans_le_thread_qui_l_utilise` — un objet
   `sqlite3.Connection` n'est utilisable que dans le thread qui l'a créé. Le
   collecteur est instancié par le thread principal du service et exécuté dans
   un thread secondaire : ouvrir la base dans `__init__` rendait toute écriture
   impossible.

2. `test_une_erreur_de_programmation_n_est_pas_reessayee` — le `except
   Exception` d'origine traitait n'importe quelle exception comme une perte de
   connexion et réessayait avec backoff. Une erreur de programmation prenait
   donc l'apparence d'un broker instable : le journal répétait « connexion
   perdue » toutes les minutes, et l'on découvrait au bout de quatorze jours
   que la base était vide.
"""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from typing import Iterator, List, Sequence

import pytest

from maxprofit.collect.collector import CandleAggregator, Collector, Config
from maxprofit.collect.sources import MarketDataSource
from maxprofit.core.errors import BotError
from maxprofit.core.types import PairInfo, Tick
from dataclasses import replace

from maxprofit.store.db import open_read_only
from maxprofit.store.market import MarketReader

T0_SEC = 1_704_067_200
T0_MS = T0_SEC * 1000


class SourceScriptee(MarketDataSource):
    """Source déterministe : joue une liste de ticks, puis lève ce qu'on lui a
    demandé de lever. Aucune horloge, aucun aléatoire."""

    def __init__(self, ticks: Sequence[Tick], *, lever=None,
                 lever_sur_list_pairs=None, echecs_connexion: int = 0):
        self.ticks = list(ticks)
        self.lever = lever
        self.lever_sur_list_pairs = lever_sur_list_pairs
        self.echecs_connexion = echecs_connexion
        self.connexions = 0
        self.passages_stream = 0
        self.abonnements: List[List[str]] = []
        self.collecteur: Collector | None = None

    def connect(self) -> None:
        self.connexions += 1
        if self.connexions <= self.echecs_connexion:
            raise ConnectionError("socket fermé par le broker")

    def list_pairs(self) -> List[PairInfo]:
        if self.lever_sur_list_pairs is not None:
            raise self.lever_sur_list_pairs
        return [PairInfo("EURUSD_otc", True, 92), PairInfo("GBPUSD_otc", True, 70)]

    def subscribe(self, pairs) -> None:
        # Chaque appel est retenu : un abonnement vit sur le SERVEUR, donc le
        # nombre d'appels est ce qui compte, pas la liste finale.
        self.abonnements.append(list(pairs))

    def stream(self) -> Iterator[Tick]:
        self.passages_stream += 1
        for tick in self.ticks:
            yield tick
        if self.lever is not None:
            raise self.lever
        # Plus rien à jouer : on arrête proprement le collecteur pour que le
        # test se termine, au lieu de le laisser boucler.
        if self.collecteur is not None:
            self.collecteur.stop()
        return


def _config(tmp_path: Path, **kw) -> Config:
    defauts = dict(db=tmp_path / "market.db", min_payout=92,
                   flush_sec=0.0, heartbeat_sec=0, max_backoff_sec=1)
    return Config(**{**defauts, **kw})


def _ticks(n: int) -> list[Tick]:
    return [Tick("EURUSD_otc", T0_MS + i * 250, 1.1 + i / 10_000) for i in range(n)]


def _lire(tmp_path: Path) -> dict:
    reader = MarketReader(open_read_only(tmp_path / "market.db"))
    try:
        return reader.counts()
    finally:
        reader.close()


# --------------------------------------------------------------------------- #
# Régressions
# --------------------------------------------------------------------------- #

def test_ouvre_sa_base_dans_le_thread_qui_l_utilise(tmp_path):
    """Instancié dans un thread, exécuté dans un autre — comme le fait le
    service hébergé. Doit écrire normalement."""
    source = SourceScriptee(_ticks(20))
    collecteur = Collector(source, _config(tmp_path))
    source.collecteur = collecteur

    assert collecteur.conn is None, "la base ne doit pas être ouverte à la construction"

    erreurs: list[BaseException] = []

    def _tourner():
        try:
            collecteur.run()
        except BaseException as e:  # noqa: BLE001 - on veut tout remonter au test
            erreurs.append(e)

    thread = threading.Thread(target=_tourner)
    thread.start()
    thread.join(timeout=20)

    assert not thread.is_alive(), "le collecteur ne s'est pas arrêté"
    assert not erreurs, f"exception dans le thread : {erreurs}"
    assert _lire(tmp_path)["ticks"] == 20


def test_une_erreur_de_programmation_n_est_pas_reessayee(tmp_path):
    """Réessayer ne peut rien réparer : le collecteur doit mourir bruyamment."""
    source = SourceScriptee([], lever_sur_list_pairs=BotError("bug de programmation"))
    collecteur = Collector(source, _config(tmp_path))

    debut = time.monotonic()
    with pytest.raises(BotError, match="bug de programmation"):
        collecteur.run()

    assert time.monotonic() - debut < 5, "le collecteur a réessayé au lieu d'échouer"
    assert source.connexions == 1, (
        f"{source.connexions} tentatives de connexion : l'erreur a été prise "
        f"pour une perte de connexion"
    )


def test_une_perte_de_connexion_est_bien_reessayee(tmp_path):
    """L'inverse : une vraie déconnexion doit être absorbée, pas fatale. Un
    collecteur qui meurt à la première coupure réseau ne collecte rien."""
    source = SourceScriptee(_ticks(10), echecs_connexion=2)
    collecteur = Collector(source, _config(tmp_path))
    source.collecteur = collecteur

    collecteur.run()

    assert source.connexions == 3, "le collecteur n'a pas retenté"
    assert _lire(tmp_path)["ticks"] == 10


def test_les_ticks_recus_avant_une_panne_sont_conserves(tmp_path):
    """Les ticks déjà reçus sont des données acquises. Rien ne justifie de les
    perdre parce que la suite s'est mal passée."""
    source = SourceScriptee(_ticks(15), lever=BotError("panne au milieu"))
    collecteur = Collector(source, _config(tmp_path))

    with pytest.raises(BotError):
        collecteur.run()

    assert _lire(tmp_path)["ticks"] == 15


# --------------------------------------------------------------------------- #
# Comportement nominal
# --------------------------------------------------------------------------- #

def test_l_historique_des_payouts_contient_les_paires_non_eligibles(tmp_path):
    """§2.3 : le backtest doit pouvoir rejouer l'éligibilité telle qu'elle était.
    Le filtre `min_payout` choisit à quoi s'abonner, il ne filtre pas la table."""
    source = SourceScriptee(_ticks(5))
    collecteur = Collector(source, _config(tmp_path))
    source.collecteur = collecteur
    collecteur.run()

    # Le relevé de payouts est horodaté à l'horloge réelle (c'est un
    # instantané pris maintenant), alors que les ticks du test sont datés de
    # 2024. On interroge donc « maintenant », pas la date des ticks.
    maintenant = int(time.time()) + 1
    reader = MarketReader(open_read_only(tmp_path / "market.db"))
    try:
        # GBPUSD est à 70 %, sous le seuil de 92 : non souscrite, mais présente.
        assert reader.payout_at("GBPUSD_otc", maintenant).payout_pct == 70
        assert reader.payout_at("EURUSD_otc", maintenant).payout_pct == 92
    finally:
        reader.close()


def test_un_seul_releve_de_payouts_au_demarrage(tmp_path):
    """Deux relevés à une seconde d'intervalle au démarrage donneraient
    l'illusion d'un changement de payout qui n'a pas eu lieu."""
    source = SourceScriptee(_ticks(5))
    collecteur = Collector(source, _config(tmp_path))
    source.collecteur = collecteur
    collecteur.run()

    conn = open_read_only(tmp_path / "market.db")
    try:
        instants = conn.execute("SELECT COUNT(DISTINCT ts_sec) AS n FROM payouts").fetchone()["n"]
        assert instants == 1, f"{instants} relevés de payouts au démarrage"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Agrégation en bougies
# --------------------------------------------------------------------------- #

def test_bougie_close_sur_franchissement_de_minute():
    agg = CandleAggregator(tf_sec=60)
    for ms, prix in ((T0_MS, 1.0), (T0_MS + 30_000, 1.5), (T0_MS + 59_999, 1.2)):
        agg.add(Tick("EURUSD_otc", ms, prix))
    assert agg.drain_closed() == [], "bougie close avant la fin de la minute"

    agg.add(Tick("EURUSD_otc", T0_MS + 60_000, 1.3))  # minute suivante
    (bougie,) = agg.drain_closed()
    assert bougie.ts_sec == T0_SEC
    assert (bougie.open, bougie.high, bougie.low, bougie.close) == (1.0, 1.5, 1.0, 1.2)
    assert bougie.tick_count == 3
    assert bougie.complete is True


def test_un_tick_en_retard_ne_modifie_pas_une_bougie_deja_close():
    """Réouvrir une bougie close fausserait son OHLC après coup, alors que le
    backtest a peut-être déjà décidé dessus."""
    agg = CandleAggregator(tf_sec=60)
    agg.add(Tick("EURUSD_otc", T0_MS, 1.0))
    agg.add(Tick("EURUSD_otc", T0_MS + 60_000, 1.3))
    agg.drain_closed()

    agg.add(Tick("EURUSD_otc", T0_MS + 10_000, 99.0))  # tick de la minute d'avant
    assert agg.drain_closed() == []
    (en_cours,) = agg.drain_all()
    assert en_cours.high == 1.3, "un tick en retard a été absorbé dans la bougie en cours"


def test_drain_all_marque_les_bougies_en_cours_comme_incompletes():
    """À la déconnexion, la minute n'a pas été observée en entier. Mieux vaut
    une donnée étiquetée douteuse qu'une donnée manquante silencieusement."""
    agg = CandleAggregator(tf_sec=60)
    agg.add(Tick("EURUSD_otc", T0_MS, 1.0))
    (bougie,) = agg.drain_all()
    assert bougie.complete is False
    assert agg.drain_all() == [], "la bougie a été rendue deux fois"


def test_les_paires_sont_agregees_independamment():
    agg = CandleAggregator(tf_sec=60)
    agg.add(Tick("EURUSD_otc", T0_MS, 1.0))
    agg.add(Tick("GBPUSD_otc", T0_MS, 2.0))
    agg.add(Tick("EURUSD_otc", T0_MS + 60_000, 1.1))

    (close,) = agg.drain_closed()
    assert close.pair == "EURUSD_otc"
    assert {b.pair for b in agg.drain_all()} == {"EURUSD_otc", "GBPUSD_otc"}


def test_l_abonnement_est_limite_mais_pas_l_historique_des_payouts(tmp_path):
    """Régression : un abonnement à 32 paires d'un coup faisait fermer le socket
    par le broker au bout de 20 secondes, sans qu'un seul tick n'arrive. Deux
    heures de collecte pour zéro ligne.

    La limite ne porte QUE sur l'abonnement. Les payouts de toutes les paires
    restent enregistrés, sans quoi le backtest ne pourrait plus rejouer
    l'éligibilité telle qu'elle était (§2.3).
    """
    class SourceLarge(SourceScriptee):
        def list_pairs(self):
            return [PairInfo(f"P{i:02d}_otc", True, 90 + (i % 6))
                    for i in range(30)]

    source = SourceLarge(_ticks(5))
    collecteur = Collector(source, _config(tmp_path, max_paires=4))
    source.collecteur = collecteur
    collecteur.run()

    assert len(collecteur.subscribed) == 4, "la limite n'a pas été appliquée"

    conn = open_read_only(tmp_path / "market.db")
    try:
        enregistrees = conn.execute(
            "SELECT COUNT(DISTINCT pair) FROM payouts").fetchone()[0]
    finally:
        conn.close()
    assert enregistrees == 30, "l'historique des payouts a été amputé"


def test_les_meilleurs_payouts_sont_prioritaires(tmp_path):
    """S'il faut se limiter, autant que ce soit sur les paires qui rapportent
    le plus."""
    class SourceVariee(SourceScriptee):
        def list_pairs(self):
            return [PairInfo("FAIBLE_otc", True, 90),
                    PairInfo("MOYEN_otc", True, 93),
                    PairInfo("FORT_otc", True, 96)]

    source = SourceVariee(_ticks(5))
    collecteur = Collector(source, _config(tmp_path, min_payout=90, max_paires=2))
    source.collecteur = collecteur
    collecteur.run()

    assert set(collecteur.subscribed) == {"FORT_otc", "MOYEN_otc"}


# --------------------------------------------------------------------------- #
# La pause qui survit au processus
# --------------------------------------------------------------------------- #
#
# Quand le broker refuse, le collecteur meurt : c'est la seule facon d'arreter
# le thread de la bibliotheque tierce. L'hebergeur le relance aussitot, et l'on
# rappelle le broker quarante secondes plus tard. Ce cycle EMPECHE une
# limitation de debit d'expirer -- on se maintient soi-meme en penitence.
#
# La dette d'attente est donc ecrite en base, la seule chose qui survive a un
# redemarrage, et purgee AVANT que le client du broker n'existe.

def test_la_dette_d_attente_est_purgee_avant_d_appeler_le_broker(tmp_path):
    """Aucun appel au broker tant que le silence du au refus n'est pas fini."""
    from maxprofit.store import etat_broker
    from maxprofit.store.db import open_read_write

    base = tmp_path / "market.db"
    conn = open_read_write(base)
    while etat_broker.attente_requise(conn) <= 0:
        etat_broker.noter_echec(conn, "poignee de main expiree")
    conn.close()

    src = SourceScriptee(_ticks(3))
    src.collecteur = collecteur = Collector(src, _config(tmp_path))

    # Le collecteur est arrete pendant l'attente : il doit en sortir sans avoir
    # touche au broker. Sans la pause, `connect()` serait deja appele.
    threading.Timer(0.3, collecteur.stop).start()
    collecteur.run()

    assert src.connexions == 0, "le broker a ete rappele malgre la pause"


def test_une_connexion_reussie_efface_la_dette(tmp_path):
    from maxprofit.store import etat_broker
    from maxprofit.store.db import open_read_only

    src = SourceScriptee(_ticks(3))
    src.collecteur = collecteur = Collector(src, _config(tmp_path))
    collecteur.run()

    assert src.connexions == 1
    conn = open_read_only(tmp_path / "market.db")
    try:
        echecs, _, _ = etat_broker.lire(conn)
        assert echecs == 0
        assert etat_broker.attente_requise(conn) == 0.0
    finally:
        conn.close()


def test_un_refus_du_broker_est_inscrit_pour_le_processus_suivant(tmp_path):
    """Sans cette trace, le processus relance repartirait l'ardoise vierge.

    C'est exactement ce qui faisait rappeler le broker toutes les quarante
    secondes pendant des heures.
    """
    from maxprofit.collect.pocketoption import BrokerInjoignable
    from maxprofit.store import etat_broker
    from maxprofit.store.db import open_read_only

    src = SourceScriptee(_ticks(0), lever=BrokerInjoignable("aucune poignee"))
    collecteur = Collector(src, _config(tmp_path))
    with pytest.raises(BrokerInjoignable):
        collecteur.run()

    conn = open_read_only(tmp_path / "market.db")
    try:
        echecs, _, raison = etat_broker.lire(conn)
        assert echecs == 1
        assert "poignee" in raison
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Regression : un marche muet suspendait TOUT
# --------------------------------------------------------------------------- #
#
# `stream()` dort quand aucun tick n'arrive. Les taches periodiques etant
# ecrites dans la boucle `for tick in ...`, elles ne tournaient plus du tout :
# ni battement de coeur, ni synchronisation, ni la moindre requete sur la base.
# Le flux Hrana vers Turso, laisse inactif, etait jete par le serveur au bout
# d'une vingtaine de secondes -- « stream not found » -- alors que le processus
# avait l'air parfaitement sain.

class SourceMuette(SourceScriptee):
    """Connectee, abonnee, et qui ne recoit rien. L'etat le plus couteux."""

    def __init__(self, tours: int):
        super().__init__([])
        self.tours = tours

    def stream(self):
        self.passages_stream += 1
        for _ in range(self.tours):
            yield None
        if self.collecteur is not None:
            self.collecteur.stop()


def test_les_taches_periodiques_tournent_meme_sans_un_seul_tick(tmp_path):
    src = SourceMuette(tours=5)
    src.collecteur = collecteur = Collector(src, _config(tmp_path))
    collecteur.run()

    compteurs = _lire(tmp_path)
    assert compteurs["ticks"] == 0
    # Le battement de coeur est la preuve de vie ET la requete qui garde le
    # flux distant ouvert. Sans lui, la sonde passe au rouge et la connexion
    # a la base meurt en silence.
    assert compteurs["uptime"] > 0, "aucun battement de coeur sans tick"


def test_un_none_n_est_pas_pris_pour_un_tick(tmp_path):
    """`None` veut dire « rien pour l'instant », pas « voici une donnee »."""
    src = SourceMuette(tours=3)
    src.collecteur = collecteur = Collector(src, _config(tmp_path))
    collecteur.run()

    assert collecteur.buf == []
    assert _lire(tmp_path)["ticks"] == 0


def test_une_panne_de_la_base_n_est_pas_imputee_au_broker(tmp_path):
    """Le flux vers Turso expire ; le broker n'y est pour rien.

    Les compter pareil mettrait la collecte en pause pendant des heures en
    cessant d'appeler le seul acteur qui fonctionne.
    """
    from maxprofit.store import etat_broker
    from maxprofit.store.db import open_read_only

    src = SourceScriptee(_ticks(0), lever=RuntimeError("stream not found"))
    src.collecteur = collecteur = Collector(src, _config(tmp_path))

    # La base ne repond plus, tour apres tour : c'est elle qui est en panne,
    # et le broker ne doit pas payer pour elle.
    collecteur._base_repond = lambda: False
    threading.Timer(1.5, collecteur.stop).start()
    collecteur.run()

    conn = open_read_only(tmp_path / "market.db")
    try:
        echecs, _, _ = etat_broker.lire(conn)
        assert echecs == 0, "une panne de stockage a ete comptee contre le broker"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Regression : connecte, souscrit a rien, et muet
# --------------------------------------------------------------------------- #
#
# Un abonnement vit sur le SERVEUR. Quand le broker ferme le socket et que la
# bibliotheque en rouvre un, il ne reste rien de l'autre cote -- mais
# `subscribed` contenait toujours les memes noms, et `refresh_pairs()` ne
# renvoyait `subscribe()` que si la liste avait change. On restait donc
# connecte, abonne a rien, sans une seule erreur pour le dire : sonde verte,
# battement frais, zero tick, pendant des heures.

class SourceQuiCoupeUneFois(SourceScriptee):
    def __init__(self):
        super().__init__([])
        self.coupe = False

    def stream(self):
        self.passages_stream += 1
        if not self.coupe:
            self.coupe = True
            raise ConnectionError("socket ferme par le broker")
        if self.collecteur is not None:
            self.collecteur.stop()
        return
        yield                                    # pragma: no cover


def test_on_se_reabonne_apres_chaque_reconnexion(tmp_path):
    src = SourceQuiCoupeUneFois()
    src.collecteur = collecteur = Collector(src, _config(tmp_path))
    collecteur.run()

    assert src.connexions == 2, "le test doit bien avoir provoque une reconnexion"
    assert len(src.abonnements) == 2, (
        "le second socket n'a recu aucun abonnement : on ecoute dans le vide"
    )
    assert src.abonnements[0] == src.abonnements[1]


def test_un_silence_prolonge_declenche_un_reabonnement(tmp_path):
    """La bibliotheque peut rouvrir son socket sans que rien ne leve ici.

    Le serveur a alors oublie nos abonnements et personne ne s'en apercoit.
    Se plaindre dans le journal ne suffit pas : il faut renvoyer l'abonnement.
    """
    src = SourceMuette(tours=4)
    src.collecteur = collecteur = Collector(
        src, _config(tmp_path, silence_alerte_sec=0))
    collecteur.run()

    assert len(src.abonnements) > 1, "aucun reabonnement malgre le silence"


def test_sans_payout_minimal_le_collecteur_refuse_de_demarrer(tmp_path,
                                                              monkeypatch):
    """Pas de valeur par defaut sur ce qui touche a l'argent (spec 5)."""
    from maxprofit.collect import collector as mod

    # Depuis un repertoire vide : `main()` charge le `.env` du repertoire
    # courant, et celui du projet n'a rien a faire dans un test.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MIN_PAYOUT_PCT", raising=False)
    assert mod.main(["--source", "sim"]) == 2


def test_le_payout_minimal_peut_venir_de_l_environnement(monkeypatch):
    from maxprofit.collect import collector as mod

    monkeypatch.setenv("MIN_PAYOUT_PCT", "88")
    assert mod._min_payout_env() == 88


def test_un_payout_illisible_vaut_absence(monkeypatch):
    from maxprofit.collect import collector as mod

    monkeypatch.setenv("MIN_PAYOUT_PCT", "quatre-vingt-douze")
    assert mod._min_payout_env() is None


def test_l_intervalle_de_synchronisation_est_reglable(monkeypatch, tmp_path):
    """Le quota de synchronisations de Turso etait consomme a 77 % avant meme
    que la collecte n'ait commence. Il faut pouvoir l'espacer sans toucher au
    code."""
    from maxprofit.collect import collector as mod

    args = argparse.Namespace(db=str(tmp_path / "m.db"), min_payout=92,
                              max_paires=4, sync_sec=900)
    assert mod.build_config(args).sync_sec == 900


def test_un_intervalle_absent_garde_le_defaut(tmp_path):
    """Zero ferait synchroniser a chaque tour de boucle : c'est l'inverse du
    but recherche."""
    from maxprofit.collect import collector as mod

    args = argparse.Namespace(db=str(tmp_path / "m.db"), min_payout=92,
                              max_paires=4, sync_sec=0)
    assert mod.build_config(args).sync_sec == Config.sync_sec
    # `sync()` tire les changements distants, il ne pousse rien : pour le seul
    # ecrivain de la base, le faire souvent ne fait que bruler du quota.
    assert Config.sync_sec >= 3600


def test_vider_les_tampons_ne_synchronise_pas(tmp_path, monkeypatch):
    """Regression de quota.

    `_vider_tampons` est appelee a CHAQUE sortie de boucle, donc a chaque
    reconnexion. Elle y appelait `turso.synchroniser`, premier consommateur du
    quota -- pour un geste qui ne pousse aucune donnee : les ecritures partent
    au `commit()`.
    """
    from maxprofit.collect import collector as mod

    appels = []
    monkeypatch.setattr(mod.turso, "synchroniser",
                        lambda conn, **kw: appels.append(1))

    src = SourceScriptee(_ticks(3))
    src.collecteur = collecteur = Collector(src, _config(tmp_path))
    collecteur.run()

    assert appels == [], "une synchronisation a ete declenchee sans necessite"


# --------------------------------------------------------------------------- #
# Les ticks bruts : 97,6 % du volume ecrit
# --------------------------------------------------------------------------- #
#
# A 4 paires et 2 ticks/s : 9,9 millions de lignes sur quatorze jours contre
# 238 000 pour tout le reste. C'est ce qui epuise un plan gratuit, chez
# n'importe quel fournisseur. Les couper ne coute rien au §2 : le backtest
# travaille sur les bougies M1, et `tick_count` est porte par la bougie.

def test_sans_ticks_les_bougies_restent_completes(tmp_path):
    src = SourceScriptee(_ticks(120))
    cfg = replace(_config(tmp_path), stocker_ticks=False)
    src.collecteur = collecteur = Collector(src, cfg)
    collecteur.run()

    compteurs = _lire(tmp_path)
    assert compteurs["ticks"] == 0, "des ticks ont ete ecrits malgre le reglage"
    assert compteurs["candles"] > 0, "les bougies ont disparu avec les ticks"


def test_sans_ticks_le_tick_count_reste_exact(tmp_path):
    """Le critere de qualite du §2.4 est porte par la bougie, pas par les
    ticks : il doit etre identique avec et sans ecriture des ticks."""
    avec = tmp_path / "avec"
    sans = tmp_path / "sans"
    avec.mkdir(); sans.mkdir()

    resultats = []
    for dossier, stocker in ((avec, True), (sans, False)):
        src = SourceScriptee(_ticks(120))
        cfg = replace(_config(dossier), stocker_ticks=stocker)
        src.collecteur = c = Collector(src, cfg)
        c.run()
        conn = open_read_only(dossier / "market.db")
        resultats.append(conn.execute(
            "SELECT ts_sec, tick_count, complete FROM candles ORDER BY ts_sec"
        ).fetchall())
        conn.close()

    assert resultats[0] == resultats[1], (
        "les bougies different selon qu'on ecrit les ticks ou non"
    )


def test_par_defaut_on_n_ecarte_rien_en_silence(tmp_path):
    """Jeter des donnees doit etre un choix explicite."""
    assert Config(db=tmp_path / "m.db", min_payout=92).stocker_ticks is True


# --------------------------------------------------------------------------- #
# Paires epinglees : la continuite avant le meilleur payout
# --------------------------------------------------------------------------- #
#
# Suivre le classement des payouts a donne 18 paires hachees en tranches de
# quelques heures au lieu de 4 series continues. Une autocorrelation mesuree sur
# un morceau de deux heures ne veut rien dire, et le §2 demande quatorze jours
# CONTINUS.

class SourceCatalogue(SourceScriptee):
    """Catalogue fixe, pour verifier a QUOI on s'abonne."""

    def __init__(self, paires):
        super().__init__([])
        self._catalogue = paires

    def list_pairs(self):
        return self._catalogue

    def stream(self):
        self.passages_stream += 1
        if self.collecteur is not None:
            self.collecteur.stop()
        return
        yield                                    # pragma: no cover


def _collecte(tmp_path, catalogue, **cfg):
    src = SourceCatalogue(catalogue)
    src.collecteur = c = Collector(src, replace(_config(tmp_path), **cfg))
    c.run()
    return src, c


def test_les_paires_epinglees_l_emportent_sur_le_classement(tmp_path):
    catalogue = [
        PairInfo("EURUSD_otc", True, 80),      # epinglee, payout mediocre
        PairInfo("EXOTIQUE_otc", True, 96),    # meilleur payout, non epinglee
    ]
    src, _ = _collecte(tmp_path, catalogue,
                       paires_fixes=("EURUSD_otc",), min_payout=92)
    assert src.abonnements[-1] == ["EURUSD_otc"]


def test_un_payout_sous_le_seuil_ne_troue_pas_la_serie(tmp_path):
    """Le payout minimal dit ou l'on mettrait de l'argent ; epingler dit ou
    l'on veut une serie continue. Les confondre ferait un trou de deux heures
    dans l'historique chaque fois que le payout baisse."""
    catalogue = [PairInfo("EURUSD_otc", True, 60)]
    src, _ = _collecte(tmp_path, catalogue,
                       paires_fixes=("EURUSD_otc",), min_payout=92)
    assert src.abonnements[-1] == ["EURUSD_otc"]


def test_une_paire_fermee_est_simplement_omise(tmp_path):
    catalogue = [PairInfo("EURUSD_otc", False, 92),
                 PairInfo("GBPUSD_otc", True, 92)]
    src, _ = _collecte(tmp_path, catalogue,
                       paires_fixes=("EURUSD_otc", "GBPUSD_otc"))
    assert src.abonnements[-1] == ["GBPUSD_otc"]


def test_un_nom_inconnu_est_signale_fort(tmp_path, caplog):
    """Une faute de frappe collecterait silencieusement moins de paires que
    demande, et l'on s'en apercevrait au moment d'analyser."""
    catalogue = [PairInfo("EURUSD_otc", True, 92)]
    with caplog.at_level("ERROR"):
        src, _ = _collecte(tmp_path, catalogue,
                           paires_fixes=("EURUSD_otc", "EURSUD_otc"))
    assert src.abonnements[-1] == ["EURUSD_otc"]
    assert any("EURSUD_otc" in m for m in caplog.messages)


def test_sans_epinglage_le_classement_reste_la_regle(tmp_path):
    catalogue = [PairInfo("A_otc", True, 96), PairInfo("B_otc", True, 93)]
    src, _ = _collecte(tmp_path, catalogue, min_payout=92, max_paires=1)
    assert src.abonnements[-1] == ["A_otc"]


def test_la_liste_est_lue_de_l_environnement(monkeypatch):
    from maxprofit.collect.collector import _paires_fixes_env

    monkeypatch.setenv("PAIRES_FIXES", " EURUSD_otc , GBPUSD_otc ,EURUSD_otc, ")
    assert _paires_fixes_env("") == ("EURUSD_otc", "GBPUSD_otc")
    # L'argument explicite l'emporte.
    assert _paires_fixes_env("X_otc") == ("X_otc",)
