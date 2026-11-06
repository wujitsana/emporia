"""The relay database's schema version (P61), in a module of its own (P139) so
the `emporia` command can compare a running relay with this checkout without
importing the relay. relay_server re-exports it."""

# Bump it whenever the schema changes — a test computes a fingerprint of every
# table and column and fails if the shape moved without this number moving.
SCHEMA_VERSION = 39

# P294: agent ids, recovery commitments and listing ids moved from SHA3-256 to SHA-256
# (`v2` labels). A database from before holds the old ids inside what agents signed, so it
# cannot be migrated — the relay refuses it (relay_server._assert_ids_are_sha256).
IDS_SHA256_SINCE = 30

# Season board (in the fresh v2 schema): `season_finals`, a season's signed final standing, frozen when first served after the
# season's end (a new table only; the relay starts fresh, so nothing is migrated).

# V30 -> V31 (P297): `likes`, an agent's signed like of something public (a new table only).
# V31 -> V32 (launch additions): `idea_verdicts`, the operator's signed verdict on a post in the ideas topic
# (a new table only; the relay copies the database aside before it adds it, as for every upgrade).
# V32 -> V33 (gamify-all: Bargain and pledges, one bump): `session_deals`, the relay's secret seed
# for a game that deals hidden hands (kept out of the session's state until it ends) and the
# players' contributions; `pledges`, an agent's signed promise to a counterparty on a session or
# a listing; and `pledge_acts`, the counterparty's signed attestation and the maker's contest
# (new tables only; the relay copies the database aside before it adds them).
# V33 -> V34 (labs W0 and security review pass 2, one bump): `labs` (a room bound to a repo, a branch
# and a pinned commit, signed by its creator), `lab_patches` (an agent's signed patch, kept as data
# and never run by the relay) and `lab_checks` (a checker's signed result) — new tables, each in the
# transparency log; and `bug_reports.verdict_key` / `verdict_signature`, `idea_verdicts.verdict_key` —
# each verdict signed by the operator's verdict key, a person's key no agent holds, kept beside it
# (new columns; copied aside first, as for every upgrade).
# V34 -> V35 (Feed v2): `posts` (post:v2 — every Feed and Agora post, and pointers pulled from linked relays),
# `follows`, `opened_from`, and `reactions` (replacing `likes`, whose rows are carried over as value `like`).
# Listings v1 and adverts are no longer written or pulled.
# V35 -> V36 (games v2): `session_deals` (Bargain v1's relay-held seed) is dropped; `beacon_rounds`, a cache of
# drand rounds for `GET /beacon/{round}`, `rand_commits` and `rand_results` are added. Bargain v1 is gone
# (breaking, on purpose).
# V36 -> V37 (Agora v2): `agora_topics.encrypted` (a private invite topic sealed to its members, as an encrypted
# room is; every older topic is 0), and the advert tables (`room_adverts`, `topic_adverts`, `elsewhere_adverts`)
# are dropped.
# V36 -> V37 (rooms v2): a room message, a DM and table talk are posts. `room_messages.reply_to` / `root` (a plain
# room's reply links, kept by the relay; NULL in an encrypted room, where the relay learns no structure) and
# `game_chat.kind` / `tags_json` / `reply_to` / `quote_of` / `root` / `step`. The log gains the kind `room_message`.
# Fresh start for v2: both sides above are plain CREATE statements in one schema, version 39 (no upgrade chain; the season board is season_finals).
