"""The relay database's schema version (P61), in a module of its own (P139) so
the `emporia` command can compare a running relay with this checkout without
importing the relay. relay_server re-exports it."""

# Bump it whenever the schema changes — a test computes a fingerprint of every
# table and column and fails if the shape moved without this number moving.
SCHEMA_VERSION = 31

# P294: agent ids, recovery commitments and listing ids moved from SHA3-256 to SHA-256
# (`v2` labels). A database from before holds the old ids inside what agents signed, so it
# cannot be migrated — the relay refuses it (relay_server._assert_ids_are_sha256).
IDS_SHA256_SINCE = 30

# V30 -> V31 (P297): `likes`, an agent's signed like of something public (a new table only).
