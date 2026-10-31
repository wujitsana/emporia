"""The relay database's schema version (P61), in a module of its own (P139) so
the `emporia` command can compare a running relay with this checkout without
importing the relay. relay_server re-exports it."""

# Bump it whenever the schema changes — a test computes a fingerprint of every
# table and column and fails if the shape moved without this number moving.
SCHEMA_VERSION = 17
