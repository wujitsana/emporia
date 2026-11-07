"""The relay database's schema version, in a module of its own so the `emporia` command can compare a
running relay with this checkout without importing the relay. relay_server re-exports it.

The relay starts fresh: there is one schema (`schema_sql.SCHEMA_SQL`, plain CREATE statements) and no
upgrade chain. A database stamped with any other version is refused by name ("this relay starts fresh: move
the old database aside"), never migrated. Change a table in `schema_sql.py`, bump this number, and update
the fingerprint in `tests/test_emporia.py`: a test computes a fingerprint of every table and column and fails
when the shape moves without this number moving.
"""

SCHEMA_VERSION = 46
