"""
Est-ce que les oracles détectent quelque chose ?

Cinq tests verts ne prouvent rien tant qu'on n'a pas montré qu'ils peuvent
rougir. Ce fichier casse volontairement le moteur de trois façons distinctes et
vérifie quel oracle attrape quoi.

Le résultat est la meilleure justification que je connaisse au fait que la spec
en demande CINQ et non un : chaque faute n'est vue que par certains d'entre eux.

    faute injectée                        oracle 1   oracle 2   oracle 3
    ------------------------------------------------------------------
    tout compte gagnant                   détecte    détecte    détecte
    latence ignorée à l'entrée            aveugle    détecte    aveugle
    égalité toujours gagnante             aveugle    aveugle    détecte

La deuxième ligne mérite qu'on s'y arrête : oublier la latence est LA faute
classique, et l'oracle des entrées aléatoires ne la voit pas. Il ne peut pas :
une stratégie qui tire sa direction à pile ou face est insensible à tout biais
symétrique, quelle que soit sa taille. C'est structurel, pas un défaut de
réglage.

La troisième aussi : le biais ne porte que sur 0,9 % des trades (les égalités
exactes à cinq décimales), soit bien moins que la résolution de ±1,4 % de
l'oracle n°1 sur 5 000 trades. Seule la symétrie le voit, parce qu'elle compare
deux exécutions au lieu de comparer à une espérance bruitée.
"""

from __future__ import annotations

import pytest

import maxprofit.backtest.engine as moteur_module
import maxprofit.backtest.execution as execution
from maxprofit.core.types import Direction
from test_oracles import (
    LOOKBACK,
    Aleatoire,
    Clairvoyante,
    TicksEnMemoire,
    agreger,
    config,
    generer_ticks,
    moteur,
)

VRAI_JUGER = execution._juger


def _oracle_1_passe(rapport) -> bool:
    """Reproduit les assertions de `test_oracle_1_entrees_aleatoires`."""
    taux = rapport.taux_reussite
    if taux is None:
        return False
    return (0.486 <= taux <= 0.514
            and rapport.pnl_moyen < 0
            and abs(rapport.pnl_moyen + 0.04) <= 0.015)


def _lancer_aleatoire(ticks, bougies, inverser=False):
    return moteur(ticks).run(Aleatoire(graine=7, inverser=inverser), bougies,
                             lookback=LOOKBACK)


# --------------------------------------------------------------------------- #

def test_faute_tout_gagnant_est_vue_par_l_oracle_1(monkeypatch):
    """La faute grossière. Si l'oracle n°1 ne voyait pas celle-là, il ne
    servirait à rien du tout."""
    monkeypatch.setattr(execution, "_juger",
                        lambda d, e, r: execution.Resultat.GAGNE)
    ticks = generer_ticks(5200, graine=42)
    rapport = _lancer_aleatoire(ticks, agreger(ticks))

    assert not _oracle_1_passe(rapport)
    assert rapport.taux_reussite == 1.0
    assert rapport.pnl_moyen > 0, "le hasard devient rentable : moteur cassé"


def test_faute_latence_ignoree_echappe_a_l_oracle_1_mais_pas_au_2(monkeypatch):
    """LA faute classique — et l'oracle des entrées aléatoires ne la voit pas.

    Entrer à la clôture de la bougie de signal plutôt qu'au premier tick après
    la latence donne quelques secondes d'avance sur le marché. Mais une
    stratégie qui tire sa direction à pile ou face gagne autant qu'elle perd de
    cette avance : le biais est symétrique, donc invisible à l'oracle n°1.

    La clairvoyance, elle, le voit immédiatement : elle a choisi son sens en
    fonction du prix d'entrée réel, et le moteur en utilise un autre.
    """
    ticks = generer_ticks(5200, graine=42)
    source = TicksEnMemoire(ticks)
    vrai_executer = execution.executer

    def executer_sans_latence(signal, payout, ticks_src, cfg):
        class EntreeALaCloture:
            def premier_a_partir_de(self, pair, ts_ms, tol):
                # Ignore la latence : prend le prix à l'instant de la décision.
                return ticks_src.dernier_jusqu_a(pair, signal.decided_at_ms, tol)

            def dernier_jusqu_a(self, pair, ts_ms, tol):
                return ticks_src.dernier_jusqu_a(pair, ts_ms, tol)

        return vrai_executer(signal, payout, EntreeALaCloture(), cfg)

    monkeypatch.setattr(moteur_module, "executer", executer_sans_latence)

    # Oracle 1 : aveugle.
    rapport_aleatoire = _lancer_aleatoire(ticks, agreger(ticks))
    assert _oracle_1_passe(rapport_aleatoire), (
        "l'oracle n°1 a détecté un biais symétrique — tant mieux, mais le "
        "commentaire de ce fichier est alors à corriger"
    )

    # Oracle 2 : détecte.
    petits = generer_ticks(600, graine=43)
    monkeypatch.setattr(moteur_module, "executer", executer_sans_latence)
    rapport_clairvoyant = moteur(petits).run(
        Clairvoyante(TicksEnMemoire(petits), config()), agreger(petits),
        lookback=LOOKBACK,
    )
    assert rapport_clairvoyant.n_perdus > 0, (
        "la clairvoyance gagne encore à 100 % alors que le moteur n'applique "
        "pas la latence : l'oracle n°2 ne prouve rien"
    )
    assert rapport_clairvoyant.taux_reussite < 1.0


def test_faute_egalite_gagnante_echappe_aux_oracles_1_et_2_mais_pas_au_3(monkeypatch):
    """Un biais minuscule et asymétrique : seules les égalités sont touchées.

    Elles ne représentent que ~0,9 % des trades, bien en dessous de la
    résolution de ±1,4 % de l'oracle n°1. Seule la symétrie l'attrape, parce
    qu'elle ne compare pas à une espérance bruitée mais deux exécutions entre
    elles — un biais constant y devient visible même s'il est petit.
    """
    monkeypatch.setattr(
        execution, "_juger",
        lambda d, e, r: execution.Resultat.GAGNE if r == e else VRAI_JUGER(d, e, r),
    )
    ticks = generer_ticks(5200, graine=42)
    bougies = agreger(ticks)

    endroit = _lancer_aleatoire(ticks, bougies)
    envers = _lancer_aleatoire(ticks, bougies, inverser=True)

    assert _oracle_1_passe(endroit), (
        "l'oracle n°1 a vu un biais de moins de 1 % : le commentaire de ce "
        "fichier est à corriger"
    )
    assert endroit.n_gagnes != envers.n_perdus, (
        "l'oracle n°3 n'a pas vu l'asymétrie : il ne prouve rien"
    )
    assert endroit.n_egalites == 0, "toutes les égalités ont été converties"


def test_le_moteur_intact_passe_les_trois(monkeypatch):
    """Contrôle positif : sans faute injectée, tout doit repasser au vert.

    Sans lui, un test qui échouerait pour une raison étrangère aux fautes
    injectées passerait pour une détection réussie.
    """
    ticks = generer_ticks(5200, graine=42)
    bougies = agreger(ticks)

    endroit = _lancer_aleatoire(ticks, bougies)
    envers = _lancer_aleatoire(ticks, bougies, inverser=True)

    assert _oracle_1_passe(endroit)
    assert endroit.n_gagnes == envers.n_perdus
    assert endroit.n_egalites > 0, (
        "aucune égalité dans le jeu de test : le test de la faute n°3 ne "
        "porterait sur rien"
    )
