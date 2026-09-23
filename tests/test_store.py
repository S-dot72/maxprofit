

def test_INTEGER_devient_BIGINT_car_PG_est_en_32_bits():
    """⚠ Le même piège que le `REAL`, et il a coûté une course.

    `INTEGER` vaut 64 bits en SQLite et 32 en PostgreSQL — maximum
    2 147 483 647. Un horodatage en MILLISECONDES vaut 1,79 × 10¹², mille
    fois trop. PostgreSQL a refusé l'écriture avec « integer out of range »
    au premier ordre passé ; en local, sur SQLite, tout passait.
    """
    from maxprofit.store.dialecte import vers_postgres

    traduit = vers_postgres("CREATE TABLE t (ts_ms INTEGER NOT NULL)")
    assert "BIGINT" in traduit and "INTEGER" not in traduit


def test_la_cle_auto_est_traduite_AVANT_l_entier():
    """L'ordre des deux règles n'est pas indifférent.

    Si `INTEGER -> BIGINT` passait en premier, la clé deviendrait
    « BIGINT PRIMARY KEY AUTOINCREMENT » — que le motif de la clé auto ne
    reconnaîtrait plus. La table naîtrait alors sans génération de clé et
    refuserait chaque insertion, au premier ordre et pas avant.
    """
    from maxprofit.store.dialecte import vers_postgres

    traduit = vers_postgres(
        "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, n INTEGER)")
    assert "BIGSERIAL PRIMARY KEY" in traduit
    assert "AUTOINCREMENT" not in traduit
    assert "n BIGINT" in traduit
