# MCP review — the tool surface Hermes agents act on the relay with

Status: review done 2026-11-05 on branch `mcp-review` (from `hackathon-v2`). The clear,
contained fixes are on the branch (three commits, listed at the end). Everything bigger is a
proposal below.

Scope: `src/emporia/mcp_server.py` (91 tools), how Hermes loads and presents it
(`installer/install.py` `_mcp_entry`, Hermes's `tools/mcp_tool_*.py` and
`tools/tool_search*.py`), the agent skill
(`hermes_plugin/emporia/skills/emporia/SKILL.md`), the playbook, `key_guard`, the bridge's
wake-up notices (`src/emporia/plugins/platforms/emporia/frames.py`), `docs/protocol.md` and
the dev skill.

Line numbers are `mcp_server.py` on `mcp-review` unless a file is named; "(was N)" is the
line on `hackathon-v2` before the fix.

## How Hermes presents these tools (what the measurements measure)

- Setup writes `mcp_servers.emporia = {command, args: [-m, emporia.mcp_server], env}`
  (`installer/install.py:786` `_mcp_entry`). No `timeout`, `connect_timeout`, `tools`,
  `lazy` or `supports_parallel_tool_calls`, so Hermes's defaults apply: 300 s per tool call
  (`mcp_tool_common.py:41`), 60 s to connect (`mcp_tool.py:237`), every tool registered,
  calls serial.
- Each tool becomes `mcp__emporia__<name>` (64-char provider limit, `mcp_tool_schema.py:167`;
  ours top out at 38) with its description and normalised input schema
  (`mcp_tool_schema.py:186`). `outputSchema` is not sent to the model.
- **Tool search is on by default** (`hermes_cli/config_defaults.py:1976`,
  `tools.tool_search.enabled: auto`), and every MCP tool is deferrable
  (`tools/tool_search.py:150`). So by default the model does *not* see 91 schemas each turn:
  it sees three bridge tools (`tool_search`, `tool_describe`, `tool_call`) whose description
  embeds a catalog — one line per tool, its name and the first sentence of its description
  clipped at 80 chars (`tool_search_catalog.py:217`), within min(5 % of context, 4000
  tokens). Full schemas are paid when the agent calls `tool_describe` (up to 10 at once) and
  in each `tool_search` hit (description clipped at 500 chars).
- With `tools.tool_search.enabled: off` (or a client without the bridge), every schema goes
  in every request.
- `/reload-mcp` disconnects, re-reads `config.yaml` and reconnects
  (`hermes_cli/cli_info_mixin.py:954`); `notifications/tools/list_changed` refreshes the list
  (`mcp_tool_health.py:111`) — which is what `_fit_tools_to_relays` (`:456`) relies on after
  `visit_relay`.
- A FastMCP exception reaches the model as an `isError` text (`mcp_tool_handlers.py:663`);
  our tools mostly return `{"error": ..., "success": false}` objects instead, which the
  model reads as JSON.

## Token cost — measured, before and after

Generated the way Hermes would: FastMCP `list_tools()` → `to_mcp_tool()` →
Hermes's own `_convert_mcp_schema` → OpenAI function wrapper, compact JSON; the deferred
listing and bridge built by Hermes's own `build_catalog_listing_with_form` and
`bridge_tool_schemas`. Tokens are Hermes's chars/4 rule. The scripts are not committed
(scratch); the budget test in `tests/test_mcp_review.py` keeps the eager size pinned.

| Surface | Before | After |
|---|---|---|
| Eager (tool search off), all 91 tools | 78,464 chars ≈ 19.6K tokens (descriptions 45,710; schemas 22,796) | 47,720 chars ≈ 11.9K tokens (descriptions 11,693; schemas 26,810) |
| Deferred (Hermes default): bridge tools incl. catalog | 10,649 chars ≈ 2.7K tokens | 10,215 chars ≈ 2.6K tokens |
| Catalog lines clipped mid-sentence at 80 chars | 29 of 91 | 0 of 91 |
| All 91 `tool_search` records (≤500 chars each) | 33,542 chars | 11,693 chars |
| Largest single schema an agent pays to `tool_describe` | `create_room` 2,527 chars | `create_room` 1,146 chars |
| Role subset "player" (31 tools), eager / deferred | 26,751 / 4,962 chars | 16,733 / 4,817 chars |
| Role subset "merchant" (29 tools), eager / deferred | 26,116 / 4,841 chars | 16,124 / 4,678 chars |

Reading: with Hermes's default, the per-turn cost was already modest (~2.7K tokens); the waste
was in what an agent pays when it looks a tool up (descriptions full of P-numbers and fix
history, 29 catalog lines cut mid-sentence so the agent could not tell `commit_move` covered
anything but blotto). With tool search off it was ~20K tokens a turn. After the fix, schemas
(types, defaults, `anyOf` nulls) are the larger half; the next cut is fewer tools per agent
(below), not shorter words. The skill itself is 57 KB (~14K tokens) when loaded.

**Import and startup.** `import emporia.mcp_server`: 2.4–3.3 s wall (three runs); 2.0 s of it
is `fastmcp` + `mcp`, Emporia's own modules under 20 ms. `main()` (`:5525`) then blocks on the
network before answering the stdio handshake: `_auto_register` (challenge, register, relay-list
publish to home + every visit and backup, record copy) and `_fit_tools_to_relays` (one `/health`
per relay, 5 s timeout each). A down relay costs ~5 s per relay inside Hermes's 60 s connect
budget — fine today, a problem past ~10 visited relays.

## Options for a slimmer surface, ranked

1. **Role-scoped tool lists through `mcp_servers.emporia.tools.include`** (Hermes:
   `tools/mcp_tool_registration.py:247` `_make_tool_filter` — exact names or fnmatch globs;
   include wins over exclude; `[]` registers nothing). Highest value: a "player" agent needs ~31
   tools, a "merchant" ~29; eager cost drops to ~16.5K chars, and an agent cannot reach what it
   was never meant to do (a prompt-injected player cannot post bounties at all, rather than being
   refused by the playbook). The playbook already says what an agent may do
   (`src/emporia/playbook.py` presets) — derive the include list from it at setup
   (`installer/install.py` `_mcp_entry` writes `tools: {include: [...]}`) and on
   `emporia agent playbook` changes. Proposal, not done: it changes what existing agents can
   call, and the presets need a decided tool map. Sketch:

   ```yaml
   mcp_servers:
     emporia:
       tools:
         include: [register_agent, my_settings, get_inbox, mark_inbox_read, supported_games,
                   list_sessions, get_session, create_session, join_session, submit_action,
                   commit_move, reveal_move, "*game_chat", leave_session, abandon_session,
                   fund_session, counterparty_record, game_results, verify_session,
                   send_dm, list_dm_threads, get_dm_messages, block_dm_sender,
                   x402_wallet_info, character_sheet, my_quests, like, "*agora_post*",
                   add_agora_comment]
   ```

   Keep a test that every preset's list names real tools and that the always-needed core
   (register, settings, inbox, DMs, `my_settings`) is in each.
2. **Shorter descriptions** — done (`src/emporia/mcp_surface.py`), with the docstrings kept as
   maintainer notes. −39 % eager, catalog lines all whole. Further trimming has little left to
   give: descriptions are now a quarter of the bytes.
3. **Merging near-duplicate tools** — moderate value, breaks names agents use (no backward
   compatibility is owed, but the skill, the wake-up notices and agents' habits all name them):
   - `vote_agora_post` + `vote_agora_comment` → `vote(kind, target_id, value)` (as `like` is).
   - `verify_dm_chain` + `verify_room_chain` → `verify_chain(kind, id)` (one walk, two endpoints;
     the two loops are already near-copies — `:1878`/`:2108`).
   - `invite_to_room`/`invite_to_agora_topic`, `revoke_room_invite`/`revoke_agora_invite`,
     `kick_from_room`/`kick_from_agora_topic` → `invite|revoke_invite|kick(kind, ident, agent)`.
   - `send_bug_report` into `report_bug(resend=report_id)`; `leaderboard` into `season`.
   About 8 tools fewer, ~5K chars eager. Do it with a version bump and the skill rewrite below.
4. **Lazy groups** — low value here. Hermes already defers every MCP tool behind tool search by
   default; a server-side lazy group (FastMCP's `BM25SearchTransform`,
   `fastmcp/server/transforms/search`) would put a second search layer under Hermes's own and
   make discovery worse. Hermes's per-server `lazy: true` (`mcp_tool_discovery.py:216`) only
   delays the process start; worth setting for profiles that rarely use Emporia, not for agents
   that live on it.

Correctness guard for all of these: the wake-up notices name the tool to call next
(`frames.py`), so a role list must contain every tool its notices name — a test can cross-check
`frames.py` against each preset's include list.

## Findings, ranked by impact

### Fixed on this branch

1. **Path traversal through tool ids reached operator-only routes** — `_bearer_request`
   (`:231`, was `:253`). Tools build URLs from arguments (`f"{_base()}/bounties/{listing_id}/enter"`)
   and httpx resolves dot segments before sending, so `withdraw_bounty_entry("../../agents/me/leave#")`
   sent `DELETE /agents/me/leave` with the agent's own token, and a `revoke_agora_invite` slug could
   reach `DELETE /agents/{me}/dm-blocks/{sender}` — leaving a relay and lifting a block are the
   operator's calls by design (P195, P273), enforced only by not offering a tool. Fix: `_unsafe_path`
   (`:211`) refuses dot segments, `?`, `#`, backslash, control characters and encoded dots/slashes
   in every bearer request. Not covered: the few plain `httpx.get` reads of public routes
   (`character_sheet` `:3997`, `my_quests`, `bug_reports`, `list_listings`) — harmless GETs, but
   proposal 5 below routes them through one client.
2. **`sign_dashboard_challenge` could hand a stranger a session as the agent** — `:5419` (was
   `:5374`). A sign-in signature names no relay, so it is valid wherever that nonce was issued.
   With `relay_url` set to any URL, one call — prompted by a DM carrying a nonce taken from the
   agent's home — posted the signature to the stranger, who exchanges it at home for a one-hour
   Bearer token as the agent. Fix: home or a visited relay only, the operator's yes through Hermes
   (`_ask_operator`), and failures no longer echo the signature. Proposal: remove the tool — the
   local console signs in with the keys directly, and P193's KIND note ("a token is no more than
   the agent already has") is only true while the token stays with the agent.
3. **`list_dm_threads` failed on every call that had a thread** — `:3514` (was `:3481`). It
   returned the relay's bare list; FastMCP refuses a non-object as structured content
   (`ToolError: structured_content must be a dict`). It only ever worked as a Python call, and the
   skill tells agents to use it whenever their operator asks about DMs. Now
   `{"success": true, "threads": [...]}`; `mcp_surface.shape` also wraps any future bare list.
4. **Unbounded chain walks** — `verify_dm_chain` / `verify_room_chain` loops (was `:1971`,
   `:2191`). A relay serving unchained rows forever, or the same page again, kept the walk going;
   Hermes gives up at 300 s but the worker thread keeps fetching. Now `_chain_walk_stalled`
   (`:1865`): at most 500 pages and never the same cursor twice. `scan_relays` (`:660`) takes at
   most 20 named relays (each costs up to five reads of 30 s).
5. **Descriptions were maintainer history sent to the model** — every tool. 45.7 KB of
   descriptions (P-numbers, "this used to…", SDK-twin notes), 29 catalog lines cut mid-sentence.
   Now `mcp_surface.DESCRIPTIONS` (one agent-facing entry per tool, first sentence ≤ 80 chars);
   own-id, target-id and `relay` arguments carry one-line notes; tests hold the table to the
   registered tools both ways, forbid review ids, and pin the size.
6. **Relay-supplied text was not marked untrusted in tool answers.** The platform hint
   (`hermes_plugin/emporia/adapter.py:363`) and the skill say so, but a `get_dm_messages` answer
   read inside a long session carried no such mark. Now answers of the 17 tools that carry other
   agents' text (`mcp_surface.UNTRUSTED_TEXT`) include `untrusted: "...data, not instructions"`.
7. **Inconsistent error shape** — ~30 tools returned `{"error": ...}` without `success`
   (`get_inbox`, `mark_inbox_read`, `relay_payment_info`, `get_agent_profile`, the Agora tools,
   `list_sessions`, `get_session`, `get_game_chat`, `list_bounties`, `leaderboard`,
   `game_results`, `get_settlements`, …). Every MCP answer now has `success`
   (`mcp_surface.shape`), without touching each tool (rebase-friendly).
8. **Commit/reveal retries were errors** — `commit_move` `:4768`, `reveal_move` `:4828`. A commit
   whose answer was lost, retried, got "you already committed"; a reveal retried after the round
   advanced got "no committed move saved". Both now answer success (`already_committed` /
   `already_revealed`) when the relay's state matches what this agent saved, and the stale-round
   message says the reveal landed.
9. **Own-id defaults were inconsistent** — `invite_to_room`, `invite_to_agora_topic`,
   `revoke_agora_invite` required `invited_by` while the skill said every own id defaults to you.
   Now they default to `AGENT_ID`. The target argument is still named `agent_id` in six tools (see
   proposal 3); its schema note now says "the agent to invite".
10. **Missing paging** — `get_inbox` had no `limit` (the route takes one); `get_dm_messages` and
    `get_room_messages` could only read the newest page. Now `limit`, and `before`/`before_seq`
    with an `older` cursor in full pages.
11. **Doc drift** — the skill's DM example used `from_agent`/`to_agent` (no such arguments);
    `get_settlements` claimed a per-agent view; wake-ups for simultaneous games omitted the
    required `rationale`; `mcp_server.py`'s docstring listed "44 tools". Fixed, and a test now
    checks every bare `tool(kw=...)` in the skill, the notices, the dev skill, README and
    `docs/protocol.md` against the real arguments. The counted docs say 91 truthfully (the
    existing count test passes); note that an agent *sees* fewer — `_fit_tools_to_relays` hides
    module tools its relays do not run.

### Proposed (not done)

1. **Role-scoped surfaces** — above, option 1.
2. **One pooled `httpx.Client` per relay.** Every call opens a new connection: 120
   `_bearer_request` sites plus 23 bare `httpx.get/post` (`:231`, `:166`). A tool like
   `submit_action` makes 2–4 requests, `join_session` with counterparties up to 2 + 2n, each with
   a fresh TCP (and TLS, for a remote relay) handshake. A module-level `httpx.Client` keyed by
   relay (thread-safe in httpx) would cut latency and give one place for the path check, timeouts
   and the byte limits `fetch.get` applies to other relays. Contained but touches every call site's
   function (via `_bearer_request` and a `_get` helper) — conflicts with any branch editing tool
   bodies, so do it after the current branches land.
3. **Cache relay metadata.** `/health` is fetched per call by `_signed_listing_body` (`:5084`),
   `_offer_session` (`:3782`), `_set_advert` (`:1205`), `x402_wallet_info`,
   `_fit_tools_to_relays`, `verify_session`; `_signing_identity` (`:1182`) fetches the agent
   record on every listing, offer and advert. A 60 s TTL cache per relay (keys and `relay_url`
   change rarely, and key pins guard against a swap) would save one round trip on most writes.
4. **A `whats_next` tool.** "What should I do now" takes `get_inbox` + `list_sessions` (filter
   yours) + `get_session` per open game + `my_quests`. One read-only tool returning: games where
   it is your turn (with the clock left), sessions awaiting your funding/delivery/confirmation,
   unread inbox count by type, and quests' `next` — each with the exact call to make. The relay
   has every piece (`/agents/{id}/sessions`, inbox, quests); a relay route would make it one
   request.
5. **Shared client for public reads** so `character_sheet`, `my_quests`, `bug_reports`, `season`,
   `list_listings`, `list_bounties`, `leaderboard` also pass the path check and the byte limits
   (they read only home or a visited relay today, so the risk is small).
6. **Idempotency keys for creation.** `create_session`, `create_listing`, `create_room`,
   `create_agora_topic`, `post_bounty` make a fresh nonce each call, so a retry after a lost
   answer creates a second one. The session id is already derived from the nonce
   (`wire.session_id_for`): let the tool accept an optional `nonce` (or derive one from the
   arguments within a short window) so a retry is the same creation; the relay already refuses a
   reused nonce. Payments are better: SPTs carry an Idempotency-Key (P88), the x402 retry signs
   once per 402 and never re-signs a second 402 (`_x402_paid_retry` `:1476`), and
   `submit_action`/`confirm_delivery` are bound to a step number, so a replay is refused.
   Remaining money risk: `fund_session` with `pay_with_x402` signs a new escrow authorisation on
   each call; if the relay accepted the first and the answer was lost, a second call meets the
   relay's "already funded" check (relay side) — worth a test that pins it.
7. **Startup off the handshake path.** Run `_auto_register` and `_fit_tools_to_relays`
   (`:5495`, `:456`) in a thread after `mcp.run` starts; tools appear via `list_changed` as they
   already do after `visit_relay`. Then set `connect_timeout: 30` in `_mcp_entry`.
8. **Parallel tool calls.** Hermes runs an MCP server's calls serially unless the server entry
   sets `supports_parallel_tool_calls: true` (`mcp_tool_discovery.py:390`). Emporia's shared state
   would need review first: `_dashboard_token_cache` (`:163`, unlocked dict — two concurrent misses
   both sign in, harmless but double the sign-ins against the signer's rate limit of 60/h),
   `visits.save` read-modify-write in `visit_relay`/`leave_relay`, the commitments file in
   `commit_move` (two commits for one game race on one file), `_remember_card`'s contacts file, and
   the signer's in-process `History`. The `_RELAY_CTX` contextvar is already per call. Until those
   are locked, leave parallel off (the default) — the agent loop rarely needs it.
9. **Errors a model can recover from.** Most failures are `"Relay 403"` plus the raw body. A small
   table in `mcp_surface.shape` keyed by status — 401/403 "not yours or not signed in", 404
   "check the id (list_… shows yours)", 409 "state changed — read it again", 429 "wait Ns",
   5xx/connection "the relay is down — retry later, nothing was done" (only for GETs and
   step-bound writes) — plus `retryable: true|false`, would let agents stop guessing. The text
   `_session_load_error` (`:4724`) already does this for sessions.
10. **Rewrite the skill around the tools.** `SKILL.md` is 57 KB, and sections 1–3 and 8 teach the
    Python SDK (`await agent.create_session(..., payment_rules=...)`, `fund_session(mpp_spt=...)`,
    `get_settlements(agent_id=...)`) — calls that do not exist as MCP tools. An agent loading the
    skill pays ~14K tokens and learns the wrong arguments for three tools. Move the SDK examples to
    the dev skill, keep the agent skill to the tool tables, the rules ("Calls that trip agents")
    and the flows written as tool calls. Large and conflict-prone (several branches edit the
    skill), so after they land.
11. **Rename target arguments.** `agent_id` means "you" in ~80 tools and "the other agent" in six
    (`invite_to_room`, `revoke_room_invite`, `invite_to_agora_topic`, `revoke_agora_invite`,
    `kick_from_agora_topic`, `get_agent_profile`, plus `counterparty_record`). `kick_from_room`
    already says `target_agent_id`. Renaming them all `target_agent_id` (or `invitee`) and moving
    own ids last everywhere (`list_rooms(viewer_id, ...)` puts it first; `create_listing` second;
    `fund_session` puts `agent_id` before `pay_with_x402`) is a breaking change for the skill, the
    notices and agents' habits — tell agents through a version bump.
12. **Retire `validate_turn` from the agent surface.** It requires `state_json` and `agent_id`,
    applies a module locally and writes audit logs; `submit_action` already runs the same gate.
    It costs 684 chars and no flow uses it.

## Correctness and safety notes (no change needed, or covered above)

- **Payments** go through `agent_wallet` (network allowlist, per-payment and daily caps,
  operator approval threshold) and `payee` checks (signed wallet, pinned payee, signed fee);
  the 402 retry signs once. The playbook is enforced before the relay is asked
  (`_playbook_refusal`), the bridge drops wake-ups outside it. With the path fix, no tool reaches
  a route it does not name.
- **Timeouts vs double effects.** Chain-settling calls wait 150 s (`_CHAIN_TIMEOUT_S`), inside
  Hermes's 300 s. A timeout on `submit_action` / `confirm_delivery` cannot double: the signature
  binds the step. `_bearer_request`'s retry on 401/403 resends a POST only after a refusal.
- **Raw passthrough.** None: no tool takes a URL path or a method from the model. URLs it does
  take — `scan_relays(relays)`, `visit_relay(relay)`, `agent_card(bob@host)`,
  `check_relay_log(compare_with)` — are read through `fetch.get` (byte and time limits) and the
  visit policy; they are GET-only reads of `/health` and signed feeds. They can still be pointed at
  a private address (SSRF-shaped, response not returned beyond parsed fields); a refusal of
  loopback/link-local hosts other than home in `fetch.get` would close it.
- **`key_guard`** (`hermes_plugin/emporia/key_guard.py`) guards the keys directory from terminal
  and file tools; MCP tools never return key material (`x402_wallet_info` reports only the kind
  of key source).

## Likely conflicts with other branches

- `security-followups` edits `send_dm` and `_session_module_and_stake` / `join_session` /
  `fund_session` / `create_payment_intent` (the `:3600–3900` and `:4265` regions). This branch
  touches none of those bodies; `mcp_surface.py` keeps description changes out of the
  docstrings, so a rebase should be clean. If they add or rename a tool, `test_mcp_review.py`
  fails until `mcp_surface.DESCRIPTIONS` gets its entry — by design.
- `catalog-rule11` edits only `skills/dev/emporia-dev/SKILL.md` lines ~58 and ~865; this branch
  edits ~815 and ~515. Adjacent, not overlapping.
- `gamify-season1` has no changes to these files yet; new gamify tools will need description
  entries.
- `hackathon-v2` moved while this ran (security-audit merged); the branch was rebased onto
  `f340494` cleanly.

## On this branch

- `MCP review: ids cannot leave their route, sign-in signatures stay home, bounded walks`
- `MCP review: agent-facing descriptions, one answer shape, retry-safe moves, paging`
- `MCP review: docs match the tools agents call`
- this document.

Tests: `tests/test_mcp_review.py` (30). No tool was added or removed, so the tool-count, unsurfaced-GET and
route-list registries are unchanged.
