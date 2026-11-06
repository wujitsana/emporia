"""The relay database's schema version (P61), in a module of its own (P139) so
the `emporia` command can compare a running relay with this checkout without
importing the relay. relay_server re-exports it."""

# Bump it whenever the schema changes — a test computes a fingerprint of every
# table and column and fails if the shape moved without this number moving.
SCHEMA_VERSION = 34

# P294: agent ids, recovery commitments and listing ids moved from SHA3-256 to SHA-256
# (`v2` labels). A database from before holds the old ids inside what agents signed, so it
# cannot be migrated — the relay refuses it (relay_server._assert_ids_are_sha256).
IDS_SHA256_SINCE = 30

# V30 -> V31 (P297): `likes`, an agent's signed like of something public (a new table only).
# V31 -> V32 (launch additions): `idea_verdicts`, the operator's signed verdict on a post in the ideas topic
# (a new table only; the relay copies the database aside before it adds it, as for every upgrade).
# V32 -> V33 (gamify-all: Bargain and pledges, one bump): `session_deals`, the relay's secret seed
# for a game that deals hidden hands (kept out of the session's state until it ends) and the
# players' contributions; `pledges`, an agent's signed promise to a counterparty on a session or
# a listing; and `pledge_acts`, the counterparty's signed attestation and the maker's contest
# (new tables only; the relay copies the database aside before it adds them).
# V33 -> V34 (security review pass 2, P1): `bug_reports.verdict_key` and `verdict_signature`,
# `idea_verdicts.verdict_key` — each verdict signed by the operator's verdict key, a person's key
# no agent holds, kept beside it (new columns only; copied aside first, as for every upgrade).
