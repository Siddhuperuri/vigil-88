"""L3 persistence: SQLite in WAL mode behind repository interfaces (D-006, 05 §5).

Nothing above this layer writes SQL. The repositories are the seam that makes "Postgres is a
later swap, not a rewrite" true, and `mappers.py` is the only module that knows both a domain
type and a database row (02 §4).
"""
