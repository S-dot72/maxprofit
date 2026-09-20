"""
Le chemin des ticks d'une minute, encodé sans perte dans quelques centaines
d'octets.

--- Pourquoi ce module existe ----------------------------------------------

Une ligne par tick était la façon évidente de faire, et elle a été mesurée :

    620 003 ticks/jour, soit 8 680 038 lignes sur les quatorze jours
    ~1 Go en PostgreSQL une fois l'index compté, contre 0,5 Go de quota

Les ticks ont donc été coupés (`STOCKER_TICKS=0`), et avec eux toute analyse
sous la minute. C'était le bon arbitrage à ce moment-là ; ce n'en est plus un,
parce qu'on n'a pas besoin d'une LIGNE par tick.

Une minute de ticks est une suite d'instants très rapprochés et de prix très
proches les uns des autres. Encodée en écarts successifs puis compressée, elle
tient dans quelques centaines d'octets :

    ~125 ticks/minute, ~5 000 minutes/jour toutes paires confondues
    ~70 000 lignes et ~20 Mo pour quatorze jours, SANS PERTE

Le facteur est d'environ 40, et l'on récupère les résolutions sous la minute :
1 s, 5 s, 15 s, 30 s se ré-agrègent depuis le chemin.

--- ⚠ « Sans perte » : ce que ça veut dire ICI, exactement ------------------

Le prix est converti en ENTIER avant d'être encodé — un flottant ne se
delta-encode pas sans perdre des bits. L'échelle est cherchée, jamais
supposée : on prend la plus petite puissance de dix qui rend TOUS les prix de
la minute entiers, à `TOLERANCE_GRILLE` près. Si aucune ne convient jusqu'à
`ECHELLE_MAX`, on lève.

La conséquence à énoncer plutôt qu'à taire : **le prix rendu est celui de la
grille décimale, pas le double d'origine.** Un broker qui cote à cinq
décimales envoie 1.08231, et l'aller-retour le rend identique. Mais un double
qui s'est écarté de la grille par du bruit d'arithmétique — `1.1 + 11/10000`
vaut `1.1011000000000002` — revient à `1.1011`.

C'est voulu, et c'est une NORMALISATION, pas un arrondi de confort : l'écart
absorbé vaut au plus 10⁻¹¹ en relatif, soit mille fois moins que le dernier
chiffre coté. Le seuil est assez serré pour refuser un vrai prix à six
décimales pris pour un prix à cinq : celui-là lève.

C'est la règle §5 appliquée à un encodage. Arrondir largement en silence
produirait des prix faux que rien ne signalerait, et un backtest sous la
minute mesurerait un marché qui n'a pas existé. Mieux vaut refuser d'écrire —
et, pour le cas limite qu'on accepte, l'écrire ici et le tester.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Sequence

from maxprofit.core.errors import BotError
from maxprofit.core.types import Tick

#: Durée couverte par un chemin. Une minute, alignée sur la bougie M1 : le
#: chemin et la bougie décrivent alors exactement le même intervalle, et l'on
#: peut vérifier l'un par l'autre (`tick_count`, open, high, low, close).
MINUTE_SEC = 60

#: Plus grande puissance de dix admise pour rendre les prix entiers.
#: Huit couvre les cinq décimales du forex, les deux des indices et garde de
#: la marge. Au-delà, ce n'est plus une cotation mais du bruit de flottant, et
#: l'encoder ne ferait que graver une erreur d'arrondi.
ECHELLE_MAX = 8

#: Écart admis, en unités de la grille entière, entre `prix × 10^échelle` et
#: l'entier le plus proche.
#:
#: Ce n'est pas une marge de confort, c'est la largeur du bruit d'arithmétique
#: flottante. À l'échelle 5, 10⁻⁶ d'une unité de grille vaut 10⁻¹¹ de prix —
#: mille fois moins que le dernier chiffre coté. Un vrai prix à six décimales
#: rate la grille de 0,1 unité et sera donc REFUSÉ à l'échelle 5, puis accepté
#: à 6 : c'est exactement le tri qu'on veut.
TOLERANCE_GRILLE = 1e-6

#: Niveau zlib. Six est le défaut de la bibliothèque ; mesuré sur des minutes
#: réelles, neuf gagne moins de 3 % pour trois fois le temps de calcul, dans
#: une boucle qui tourne à chaque flush du collecteur.
NIVEAU_COMPRESSION = 6


@dataclass(frozen=True, slots=True)
class CheminTicks:
    """Une minute de ticks, prête à écrire ou fraîchement lue.

    `echelle` voyage avec le chemin : c'est elle qui permet de rendre les prix
    d'origine. La stocker à côté des octets plutôt que la supposer à la lecture
    est ce qui rend l'encodage réversible des années plus tard.
    """

    pair: str
    minute_sec: int
    n_ticks: int
    echelle: int
    octets: bytes

    def __post_init__(self) -> None:
        if not self.pair:
            raise BotError("CheminTicks sans paire")
        if self.minute_sec % MINUTE_SEC:
            raise BotError(
                f"minute_sec doit être aligné sur la minute : "
                f"{self.minute_sec}")
        if self.n_ticks < 1:
            raise BotError(
                f"Un chemin vide ne s'écrit pas : {self.n_ticks} tick(s). "
                f"Une minute sans tick est l'ABSENCE d'une ligne, pas une "
                f"ligne vide — c'est ce qui distingue « marché immobile » de "
                f"« collecte interrompue ».")
        if not (0 <= self.echelle <= ECHELLE_MAX):
            raise BotError(f"echelle hors [0,{ECHELLE_MAX}] : {self.echelle}")


def _varint(n: int, sortie: bytearray) -> None:
    """Entier non signé, sept bits par octet. Les petits écarts coûtent un
    octet, et ce sont eux qui dominent : deux ticks consécutifs sont séparés
    de quelques centaines de millisecondes et de quelques unités de prix."""
    while True:
        octet = n & 0x7F
        n >>= 7
        sortie.append(octet | (0x80 if n else 0))
        if not n:
            return


def _lire_varint(octets: bytes, i: int) -> tuple[int, int]:
    n, decalage = 0, 0
    while True:
        if i >= len(octets):
            raise BotError(
                "Chemin de ticks tronqué : un entier commencé n'est pas fini.")
        octet = octets[i]
        i += 1
        n |= (octet & 0x7F) << decalage
        if not octet & 0x80:
            return n, i
        decalage += 7


def _zigzag(n: int) -> int:
    """Signé -> non signé, en gardant les petites valeurs petites.
    Un écart de prix vaut aussi souvent -1 que +1 ; sans cette transformation,
    -1 s'encoderait comme un très grand entier."""
    return (n << 1) ^ (n >> 63)


def _dezigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _echelle_exacte(prix: Sequence[float]) -> int:
    """La plus petite puissance de dix qui rend TOUS ces prix entiers.

    Cherchée, pas supposée. Une échelle codée en dur marcherait sur l'EURUSD à
    cinq décimales et arrondirait un indice en silence.
    """
    for echelle in range(ECHELLE_MAX + 1):
        facteur = 10 ** echelle
        if all(abs(p * facteur - round(p * facteur)) < TOLERANCE_GRILLE
               for p in prix):
            return echelle
    raise BotError(
        f"Aucune échelle jusqu'à 10^{ECHELLE_MAX} ne rend ces prix entiers : "
        f"{sorted(set(prix))[:5]}… Encoder quand même arrondirait des prix en "
        f"silence, et le backtest sous la minute mesurerait un marché qui n'a "
        f"pas existé.")


def minute_de(ts_ms: int) -> int:
    """La minute UTC, en secondes, qui contient cet instant."""
    return (ts_ms // 1000) // MINUTE_SEC * MINUTE_SEC


def encoder(ticks: Sequence[Tick]) -> CheminTicks:
    """Les ticks d'UNE minute et d'UNE paire, en un bloc compressé.

    Les ticks sont triés ici plutôt qu'exigés triés : ils arrivent d'un flux
    réseau, et un appelant qui doit garantir un ordre finit par l'oublier.
    Deux ticks au même instant sont conservés tous les deux — c'est le marché
    qui le dit, pas nous.
    """
    if not ticks:
        raise BotError("encoder() sans tick : il n'y a rien à écrire.")
    paires = {t.pair for t in ticks}
    if len(paires) > 1:
        raise BotError(f"Un chemin ne mélange pas les paires : {paires}")
    minutes = {minute_de(t.ts_ms) for t in ticks}
    if len(minutes) > 1:
        raise BotError(
            f"Un chemin ne couvre qu'UNE minute, reçu : {sorted(minutes)}. "
            f"Grouper avant d'encoder est la responsabilité de l'appelant, "
            f"parce que lui seul sait quelles minutes sont closes.")

    ordonnes = sorted(ticks, key=lambda t: t.ts_ms)
    minute = minutes.pop()
    echelle = _echelle_exacte([t.price for t in ordonnes])
    facteur = 10 ** echelle

    brut = bytearray()
    _varint(echelle, brut)
    base_ms = minute * 1000
    ms_precedent = base_ms
    prix_precedent = 0
    for i, tick in enumerate(ordonnes):
        _varint(tick.ts_ms - ms_precedent, brut)
        entier = round(tick.price * facteur)
        if i == 0:
            _varint(entier, brut)
        else:
            _varint(_zigzag(entier - prix_precedent), brut)
        ms_precedent = tick.ts_ms
        prix_precedent = entier

    return CheminTicks(
        pair=ordonnes[0].pair,
        minute_sec=minute,
        n_ticks=len(ordonnes),
        echelle=echelle,
        octets=zlib.compress(bytes(brut), NIVEAU_COMPRESSION),
    )


def decoder(chemin: CheminTicks) -> list[Tick]:
    """Rend exactement les ticks passés à `encoder()`, dans l'ordre du temps.

    L'échelle lue dans les octets est comparée à celle de la ligne : une
    divergence signalerait deux enregistrements mélangés, et il vaut mieux
    lever que rendre des prix faux d'un facteur dix.
    """
    try:
        brut = zlib.decompress(chemin.octets)
    except zlib.error as erreur:
        raise BotError(
            f"Chemin de ticks illisible pour {chemin.pair} à "
            f"{chemin.minute_sec} : {erreur}") from erreur

    echelle, i = _lire_varint(brut, 0)
    if echelle != chemin.echelle:
        raise BotError(
            f"Échelle incohérente pour {chemin.pair} à {chemin.minute_sec} : "
            f"{echelle} dans les octets, {chemin.echelle} sur la ligne.")
    facteur = 10 ** echelle

    sortie: list[Tick] = []
    ms = chemin.minute_sec * 1000
    entier = 0
    for rang in range(chemin.n_ticks):
        ecart, i = _lire_varint(brut, i)
        ms += ecart
        valeur, i = _lire_varint(brut, i)
        entier = valeur if rang == 0 else entier + _dezigzag(valeur)
        sortie.append(Tick(pair=chemin.pair, ts_ms=ms, price=entier / facteur))
    if i != len(brut):
        raise BotError(
            f"Chemin de ticks plus long que ses {chemin.n_ticks} ticks "
            f"annoncés : {len(brut) - i} octet(s) en trop.")
    return sortie


def grouper_par_minute(
        ticks: Sequence[Tick]) -> dict[tuple[str, int], list[Tick]]:
    """Range des ticks mêlés en paquets (paire, minute), prêts à encoder."""
    paquets: dict[tuple[str, int], list[Tick]] = {}
    for tick in ticks:
        paquets.setdefault((tick.pair, minute_de(tick.ts_ms)), []).append(tick)
    return paquets
