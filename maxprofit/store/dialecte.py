"""
Traduire la SQL de SQLite vers PostgreSQL, et rien de plus.

**Pourquoi ce module existe.** Tout le stockage est écrit en SQLite : c'est la
bonne décision, elle donne un fichier inspectable, des migrations simples et des
tests qui tournent sans serveur. Mais un hébergement gratuit sans disque impose
une base distante, et les fournisseurs qui tiennent la durée sont en PostgreSQL.

**Ce que ce module ne fait pas.** Il ne prétend pas traduire SQL en général —
ce serait un compilateur, et il se tromperait. Il traduit LE dialecte que ce
projet écrit, dont la surface est petite, connue et énumérée ci-dessous. Toute
construction hors de cette liste doit être écrite de façon portable à la source
plutôt qu'ajoutée ici : une règle de traduction est une dette, une SQL portable
n'en est pas une.

**Ce qui a été rendu portable plutôt que traduit.** `INSERT OR IGNORE` et
`INSERT OR REPLACE` sont des inventions de SQLite ; `ON CONFLICT … DO NOTHING`
et `ON CONFLICT (…) DO UPDATE` sont du SQL standard que SQLite comprend aussi
depuis 3.24. Les écritures sont donc écrites une fois, dans la forme que les
deux moteurs acceptent, et ce module n'a pas à les connaître.

--- Les différences qui restent --------------------------------------------

    ?                  -> %s               (marqueurs de paramètres)
    REAL               -> DOUBLE PRECISION (REAL est SIMPLE précision en PG :
                                            un prix y perdrait des décimales)
    WITHOUT ROWID      -> retiré           (optimisation propre à SQLite)
    MAX(a, b)          -> GREATEST(a, b)   (en PG, MAX est un agrégat)
    MIN(a, b)          -> LEAST(a, b)
    PRAGMA …           -> ignoré           (n'existe pas)

Le piège du `REAL` mérite d'être souligné : PostgreSQL l'accepte sans broncher,
mais c'est du 32 bits. Un prix comme 1.23456 y survivrait ; 1.234567 non. La
collecte s'exécuterait sans une erreur en dégradant silencieusement chaque
prix — exactement le mode de défaillance que ce projet passe son temps à
éliminer.

Celui du `MAX` est du même genre, à l'envers : `MAX(ts_sec)` est un AGRÉGAT,
identique dans les deux moteurs. Le traduire en `GREATEST` casserait chaque
requête d'agrégation du projet. Seule la forme à deux arguments est propre à
SQLite, et les distinguer demande d'équilibrer les parenthèses — ce qu'une
expression régulière ne sait pas faire.
"""

from __future__ import annotations

import re

#: Repère un appel `MAX(` / `MIN(`. Le découpage des arguments est fait à la
#: main juste après : `MAX(a, MAX(b, c))` met en échec toute expression
#: régulière, et silencieusement.
_APPEL = re.compile(r"\b(MAX|MIN)\s*\(", re.IGNORECASE)

_SANS_ROWID = re.compile(r"\s+WITHOUT\s+ROWID", re.IGNORECASE)
_REAL = re.compile(r"\bREAL\b", re.IGNORECASE)
_PRAGMA = re.compile(r"^\s*PRAGMA\b", re.IGNORECASE)


def est_pragma(sql: str) -> bool:
    """Un `PRAGMA` n'a pas d'équivalent : il est ignoré, pas traduit."""
    return bool(_PRAGMA.match(sql))


def marqueurs(sql: str) -> str:
    """`?` -> `%s`, sans toucher aux `?` dans les chaînes littérales.

    Le projet n'en écrit aucune, mais un remplacement global est le genre de
    raccourci qui se retourne contre soi le jour où quelqu'un écrit
    `WHERE nom = 'pourquoi ?'`. Le coût d'un balayage caractère par caractère
    est nul devant le prix d'un bug de ce genre.
    """
    sortie = []
    quote = None
    for c in sql:
        if quote:
            if c == quote:
                quote = None
            sortie.append(c)
        elif c in "'\"":
            quote = c
            sortie.append(c)
        elif c == "?":
            sortie.append("%s")
        else:
            sortie.append(c)
    return "".join(sortie)


def _fin_et_virgule(sql: str, ouvrante: int) -> tuple[int | None, int | None]:
    """Position de la parenthèse fermante, et de la virgule de PREMIER niveau.

    `virgule` vaut `None` quand l'appel n'a qu'un argument — c'est exactement
    ce qui distingue l'agrégat de la fonction à deux arguments.
    """
    profondeur = 0
    virgule = None
    for j in range(ouvrante, len(sql)):
        c = sql[j]
        if c == "(":
            profondeur += 1
        elif c == ")":
            profondeur -= 1
            if profondeur == 0:
                return j, virgule
        elif c == "," and profondeur == 1 and virgule is None:
            virgule = j
    return None, None


def _traduire_extrema(sql: str) -> str:
    """`MAX(a, b)` -> `GREATEST(a, b)`, en laissant l'agrégat tranquille."""
    sortie = []
    i = 0
    while True:
        trouve = _APPEL.search(sql, i)
        if trouve is None:
            sortie.append(sql[i:])
            return "".join(sortie)

        ouvrante = trouve.end() - 1
        fermante, virgule = _fin_et_virgule(sql, ouvrante)
        if fermante is None or virgule is None:
            # Un seul argument (agrégat), ou parenthèse non fermée : on ne
            # touche à rien et l'on reprend après le nom de la fonction.
            sortie.append(sql[i:trouve.end()])
            i = trouve.end()
            continue

        gauche = _traduire_extrema(sql[ouvrante + 1:virgule].strip())
        droite = _traduire_extrema(sql[virgule + 1:fermante].strip())
        nom = "GREATEST" if trouve.group(1).upper() == "MAX" else "LEAST"
        sortie.append(sql[i:trouve.start()])
        sortie.append(f"{nom}({gauche}, {droite})")
        i = fermante + 1


def vers_postgres(sql: str) -> str:
    """La traduction complète d'une instruction."""
    sql = _SANS_ROWID.sub("", sql)
    sql = _REAL.sub("DOUBLE PRECISION", sql)
    return marqueurs(_traduire_extrema(sql))
