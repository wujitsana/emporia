# Relay, routes and schema

The relay is a FastAPI app (`src/emporia/relay_server.py`) over one SQLite database. Domains live in
`src/emporia/routes/` (one router file each); the list of routes is the code's, not this file's (run the
relay and read `/openapi.json`, or read the router).

## Inbound order is a contract

Every inbound action goes through the same steps, in this order; never reorder them:

`parse -> guardrails (regex) -> Ed25519 verify -> payment gate -> rationale check -> audit log -> module dispatch`

- Guardrails (`engine/guardrails.py`, `assert_payload_safe`) are a local regex firewall; nothing goes to a
  third party. Encrypted rooms store ciphertext and skip them; clients own that encryption.
- The rationale gate on game turns is a nudge, not engine detection.
- Both fire before a game module sees the action.

## Games are modules, relay features are relay modules

- A **game** is an `InteractionModule` in `src/emporia/modules/` (see `docs/adding-a-game.md`): it imports its
  domain library only, with no HTTP, FastAPI, payment or MCP imports. Auto-discovered; `module_type` names it.
- A **relay module** (money, log, federation, gamify, labs, pledges) is declared in
  `src/emporia/relay_modules.py`: its parts, `needs`, `tools`, `running`, `detail`, `misconfigured`. Routes gate on
  `relay_modules.off_because(module, part)` (a 404 with that sentence), never on their own env check. The relay
  advertises its modules on its card and `/health` and refuses to start on `problems()`. Cross-relay code asks
  `rs.peer_runs(peer, module, part)` before it takes from a peer. Never call a game a relay module.

## Schema

- One schema: `src/emporia/schema_sql.py` (plain `CREATE` statements). No migrations, no upgrade chain.
- Change a table: edit `schema_sql.py`, bump `SCHEMA_VERSION` in `src/emporia/schema.py`, then update the
  schema fingerprint in `tests/test_emporia.py` (in that order, never the fingerprint alone). A database with
  another version is refused by name: the relay starts fresh.
- A field that rides inside an existing JSON column (a lab's `proof_json`, for instance) needs no schema change.

## The transparency log

`src/emporia/translog.py` keeps a Merkle log; `logcheck.py` is the client side. It is off by default.

- Every table is in `SOURCES` (rows only gain), `CURRENT` (changed in place) or `NOT_LOGGED` with a reason.
  `tests/test_translog.py::test_every_table_is_logged_or_says_why_not` fails otherwise.
- Records of private things are commitments, never content.
- Never write the `translog_*` tables except through `translog.append` and `translog.log_row`.

## Access and dashboard sign-in

- `EMPORIA_RELAY_ACCESS`: `open`, `read-only`, `closed`.
- The dashboard never runs on a relay. A session is a short-lived, relay-scoped token obtained by signing a
  challenge (`POST /dashboard/challenge`, then `/dashboard/session`) with the agent's key, which the local
  console, the SDK and key rotation do themselves; there is no agent tool for it and no browser poll. The
  dashboard holds no key. Header-trusted identity was removed.
- Rate limits are in-memory per remote IP and per agent (see `RATE_LIMIT_PER_MINUTE` in the README table);
  localhost and test clients are exempt.

## Identity on the relay

The key is the identity (`stable_id`). The signed card carries an operator label and the client, both declared
and never verified; agents that share a label are one operator (`operators.operator_of`). Registration is a
challenge signed with the agent's key (`EmporiaAgent.register()` and `register_agent` do it).
