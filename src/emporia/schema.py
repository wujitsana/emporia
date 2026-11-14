"""The relay database's schema version, in a module of its own so the `emporia` command can compare a
running relay with this checkout without importing the relay. relay_server re-exports it.

There is one schema (`schema_sql.SCHEMA_SQL`, plain CREATE statements) and no migration chain. A database stamped
with another version is refused at start-up by name; `emporia relay upgrade` (`schema_upgrade.py`) carries an
older one forward by diffing it against `SCHEMA_SQL` (additive changes only). Change a table in `schema_sql.py`, bump this number, and update
the fingerprint in `tests/test_emporia.py`: a test computes a fingerprint of every table and column and fails
when the shape moves without this number moving.
"""

SCHEMA_VERSION = 56
