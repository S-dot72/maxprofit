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

import threading
import time
from pathlib import Path
from typing import Iterator, List, Sequence

import pytest

from maxprofit.collect.collector import CandleAggregator, Collector, Config
from maxprofit.collect.sources import MarketDataSource
from maxprofit.core.errors import BotError
from maxprofit.core.types import PairInfo, Tick
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
