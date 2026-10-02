"""
L'image de chaque ordre : les bougies M1, la zone tradée, l'entrée.

Demandée le 2026-10-02 : « est-ce que je peux recevoir un screenshot de la
zone tradée en M1 pour chaque ordre ». Elle permet de juger d'un coup d'œil
ce que le bot a vu — comme la capture AUD/CHF qui a montré une vente prise
contre l'élan des acheteurs.

--- Sans dépendance ----------------------------------------------------------

Le projet n'installe que le strict nécessaire (voir requirements.txt) :
matplotlib pèserait des dizaines de mégaoctets pour une image par ordre. Le
PNG est donc dessiné à la main — des rectangles et des lignes sur un tampon
de pixels, compressé par `zlib` — avec une police de chiffres 3 × 5.
"""

from __future__ import annotations

import struct
import zlib
from datetime import datetime, timezone
from typing import Sequence

LARGEUR, HAUTEUR = 960, 540
GAUCHE, DROITE, HAUT, BAS = 12, 96, 16, 34

FOND = (19, 23, 34)
GRILLE = (36, 41, 54)
VERT = (38, 166, 154)
ROUGE = (239, 83, 80)
ZONE = (255, 193, 7)
BLANC = (225, 228, 235)
GRIS = (130, 136, 150)

#: Chiffres 3 × 5, une chaîne par ligne.
POLICE = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    ".": ("000", "000", "000", "000", "010"),
    ":": ("000", "010", "000", "010", "000"),
    " ": ("000", "000", "000", "000", "000"),
}


class _Toile:
    def __init__(self, largeur: int, hauteur: int, fond):
        self.l, self.h = largeur, hauteur
        self.px = bytearray(bytes(fond) * (largeur * hauteur))

    def point(self, x: int, y: int, c, alpha: float = 1.0) -> None:
        if 0 <= x < self.l and 0 <= y < self.h:
            i = 3 * (y * self.l + x)
            if alpha >= 1.0:
                self.px[i:i + 3] = bytes(c)
            else:
                for k in range(3):
                    self.px[i + k] = int(self.px[i + k] * (1 - alpha)
                                         + c[k] * alpha)

    def rect(self, x0, y0, x1, y1, c, alpha: float = 1.0) -> None:
        x0, x1 = sorted((int(x0), int(x1)))
        y0, y1 = sorted((int(y0), int(y1)))
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.l - 1, x1), min(self.h - 1, y1)
        if alpha >= 1.0:
            ligne = bytes(c) * (x1 - x0 + 1)
            for y in range(y0, y1 + 1):
                i = 3 * (y * self.l + x0)
                self.px[i:i + len(ligne)] = ligne
            return
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.point(x, y, c, alpha)

    def hligne(self, x0, x1, y, c, pointille: int = 0) -> None:
        for x in range(int(x0), int(x1) + 1):
            if not pointille or (x // pointille) % 2 == 0:
                self.point(x, int(y), c)

    def texte(self, x: int, y: int, chaine: str, c, echelle: int = 2) -> None:
        for n, car in enumerate(chaine):
            motif = POLICE.get(car)
            if motif is None:
                continue
            for ly, rang in enumerate(motif):
                for lx, bit in enumerate(rang):
                    if bit == "1":
                        self.rect(x + n * 4 * echelle + lx * echelle,
                                  y + ly * echelle,
                                  x + n * 4 * echelle + (lx + 1) * echelle - 1,
                                  y + (ly + 1) * echelle - 1, c)

    def png(self) -> bytes:
        brut = b"".join(
            b"\x00" + bytes(self.px[3 * y * self.l:3 * (y + 1) * self.l])
            for y in range(self.h))

        def bloc(nom: bytes, donnees: bytes) -> bytes:
            return (struct.pack(">I", len(donnees)) + nom + donnees
                    + struct.pack(">I", zlib.crc32(nom + donnees) & 0xFFFFFFFF))

        return (b"\x89PNG\r\n\x1a\n"
                + bloc(b"IHDR", struct.pack(">IIBBBBB", self.l, self.h, 8, 2,
                                            0, 0, 0))
                + bloc(b"IDAT", zlib.compress(brut, 6))
                + bloc(b"IEND", b""))


def _prix(v: float) -> str:
    return f"{v:.5f}" if v < 100 else f"{v:.3f}"


def graphique_m1(bougies: Sequence, niveau: float | None, marge: float,
                 call: bool) -> bytes:
    """Le PNG des dernières bougies M1, de la zone et de l'entrée.

    `bougies` : les bougies vues par la stratégie au signal, la dernière
    étant celle de l'entrée. `niveau`, `marge` : la zone tradée (prix et
    demi-largeur), ou `None`. `call` : le sens de l'ordre.
    """
    if not bougies:
        raise ValueError("aucune bougie à dessiner")
    toile = _Toile(LARGEUR, HAUTEUR, FOND)
    bas = min(b.low for b in bougies)
    haut = max(b.high for b in bougies)
    if niveau is not None:
        bas, haut = min(bas, niveau - marge), max(haut, niveau + marge)
    ecart = (haut - bas) or abs(haut) * 1e-4 or 1e-4
    bas, haut = bas - 0.08 * ecart, haut + 0.08 * ecart
    zone_h = HAUTEUR - HAUT - BAS
    zone_l = LARGEUR - GAUCHE - DROITE

    def y_de(p: float) -> int:
        return int(HAUT + (haut - p) / (haut - bas) * zone_h)

    pas = zone_l / len(bougies)
    corps_l = max(1, int(pas * 0.6))

    reserves = [y_de(bougies[-1].close)] + (
        [y_de(niveau)] if niveau is not None else [])
    for k in range(1, 5):                       # grille horizontale
        p = bas + (haut - bas) * k / 5
        toile.hligne(GAUCHE, LARGEUR - DROITE, y_de(p), GRILLE)
        if all(abs(y_de(p) - r) > 16 for r in reserves):
            toile.texte(LARGEUR - DROITE + 6, y_de(p) - 5, _prix(p), GRIS)

    if niveau is not None:                      # la zone
        toile.rect(GAUCHE, y_de(niveau + marge), LARGEUR - DROITE,
                   y_de(niveau - marge), ZONE, alpha=0.22)
        toile.hligne(GAUCHE, LARGEUR - DROITE, y_de(niveau), ZONE)
        toile.rect(LARGEUR - DROITE + 2, y_de(niveau) - 7, LARGEUR - 2,
                   y_de(niveau) + 6, ZONE)
        toile.texte(LARGEUR - DROITE + 6, y_de(niveau) - 5, _prix(niveau),
                    FOND)

    for n, b in enumerate(bougies):             # les bougies
        x = int(GAUCHE + n * pas + pas / 2)
        c = VERT if b.close >= b.open else ROUGE
        toile.rect(x, y_de(b.high), x, y_de(b.low), c)
        toile.rect(x - corps_l // 2, y_de(max(b.open, b.close)),
                   x + corps_l // 2, y_de(min(b.open, b.close)), c)
        if n % 15 == 0:                          # heure UTC
            heure = datetime.fromtimestamp(b.ts_sec, timezone.utc)
            toile.texte(x - 18, HAUTEUR - BAS + 10, f"{heure:%H:%M}", GRIS)

    derniere = bougies[-1]                       # l'entrée
    x = int(GAUCHE + (len(bougies) - 1) * pas + pas / 2)
    y = y_de(derniere.close)
    c = VERT if call else ROUGE
    toile.hligne(GAUCHE, LARGEUR - DROITE, y, BLANC, pointille=6)
    toile.rect(LARGEUR - DROITE + 2, y - 7, LARGEUR - 2, y + 6, c)
    toile.texte(LARGEUR - DROITE + 6, y - 5, _prix(derniere.close), BLANC)
    # La flèche : sous la bougie, pointe en haut, pour un achat ; au-dessus,
    # pointe en bas, pour une vente.
    pointe = (y_de(derniere.low) + 8) if call else (y_de(derniere.high) - 8)
    for d in range(16):
        yy = pointe + d if call else pointe - d
        toile.hligne(x - d // 2, x + d // 2, yy, c)
    return toile.png()
