"""
Le plan rejoué : martingale en 3 pas, en 2 pas ou sans martingale, et chaque
stratégie dont on dispose — sur les MÊMES signaux rejoués.

Demandé le 2026-09-30, après une troisième session perdue en trois pas :
trois jours, environ 55 ordres, 61 % de réussite… et un solde revenu à son
point de départ. La question n'est plus « quel filtre ajouter après cette
perte », c'est « quel réglage de la martingale, et quelle stratégie, rendent
le plan rentable sur la durée » — à trancher en chiffres.

Les règles sont celles de la course : un seul ordre à la fois ; dans une
session, le pas suivant sur une AUTRE paire, au moins `independance_sec`
après l'entrée du précédent ; la mise de chaque pas sort de l'échelle,
dimensionnée sur le solde du moment ; une session s'arrête au premier pas
gagné ou après `pas_max` pertes.

⚠ Rejeu M1 : l'entrée est la clôture de la bougie de signal. Sur une option
de 15 min, les quelques secondes du clic réel pèsent peu ; sur une option
d'UNE minute, elles pèsent lourd — les stratégies à 1 min sont les plus
flattées par ce rejeu.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Mapping, Sequence

from maxprofit.apprentissage.lecons import Exemple
from maxprofit.plan import Echelle

CAPITAL = 250.0
GAIN_PAR_SESSION_PCT = 0.5834
PAYOUT_PCT = 92
PAS_COMPARES = (3, 2, 1)


@dataclass(frozen=True)
class Resultat:
    pas_max: int
    ordres: int
    sessions_gagnees: int
    sessions_perdues: int
    solde: float
    creux: float                # plus forte baisse depuis un sommet, en $

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping) -> "Resultat":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


def simuler(exemples: Sequence[Exemple], *, echeance_sec: int, pas_max: int,
            independance_sec: int = 900,
            garder: Callable[[Exemple], bool] = lambda e: True) -> Resultat:
    """Le plan joué signal après signal, dans l'ordre du temps."""
    solde = sommet = CAPITAL
    creux = 0.0
    libre_a = 0
    pas = 0
    echelle = None
    dernier: tuple[str, int] | None = None
    ordres = gagnees = perdues = 0
    for e in sorted(exemples, key=lambda x: x.ts_sec):
        t = e.ts_sec
        if t < libre_a or not garder(e):
            continue
        if pas and dernier is not None and (
                e.pair == dernier[0] or t - dernier[1] < independance_sec):
            continue
        if pas == 0:
            echelle = Echelle(payout_pct=PAYOUT_PCT, pas_max=pas_max,
                              gain_vise=GAIN_PAR_SESSION_PCT / 100 * solde)
        mise = echelle.mises()[pas]
        ordres += 1
        libre_a = t + echeance_sec
        if e.gagne:
            solde += mise * PAYOUT_PCT / 100
            gagnees += 1
            pas, dernier = 0, None
        else:
            solde -= mise
            pas += 1
            dernier = (e.pair, t)
            if pas >= pas_max:
                perdues += 1
                pas, dernier = 0, None
        sommet = max(sommet, solde)
        creux = max(creux, sommet - solde)
    return Resultat(pas_max, ordres, gagnees, perdues, round(solde, 2),
                    round(creux, 2))


def comparer(exemples: Sequence[Exemple], echeance_sec: int,
             garder: Callable[[Exemple], bool] = lambda e: True
             ) -> list[Resultat]:
    # L'indépendance entre deux pas suit l'échéance jouée : un pas suivant
    # « au moins une échéance plus tard », comme en direct.
    return [simuler(exemples, echeance_sec=echeance_sec, pas_max=p,
                    independance_sec=echeance_sec, garder=garder)
            for p in PAS_COMPARES]


def texte(simulations: Mapping[str, Mapping], jours: int | None = None) -> str:
    """`/simulation` : chaque stratégie, chaque réglage de martingale."""
    if not simulations:
        return ("🎲 Simulation pas encore calculée : elle accompagne le "
                "rejeu quotidien de l'apprentissage.")
    lignes = [f"🎲 <b>Simulation du plan</b> — mêmes signaux rejoués"
              + (f" sur {jours} jours" if jours else "")
              + f", départ {CAPITAL:.0f} $, mises du plan"]
    for nom, bloc in simulations.items():
        lignes.append(f"\n<b>{nom}</b> — {bloc.get('signaux', 0)} signaux")
        for d in bloc.get("resultats", []):
            r = Resultat.from_dict(d)
            reglage = ("sans martingale" if r.pas_max == 1
                       else f"{r.pas_max} pas")
            gain = r.solde - CAPITAL
            lignes.append(
                f"  {reglage} : <b>{gain:+.2f} $</b> (solde {r.solde:.2f} $), "
                f"plus gros creux −{r.creux:.2f} $, sessions "
                f"{r.sessions_gagnees} gagnées / {r.sessions_perdues} "
                f"perdues, {r.ordres} ordres")
    lignes.append(
        "\n⚠ Rejeu M1 : entrée à la clôture de la bougie de signal. Les "
        "stratégies à 1 min en sont les plus flattées — quelques secondes de "
        "clic comptent beaucoup sur une option d'une minute.")
    return "\n".join(lignes)
