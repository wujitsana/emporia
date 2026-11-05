# Review pass, 2026-11-05

An independent read of the day's work on `hackathon-v2`, done on branch `review-pass`. Nothing here is
pushed or merged.

**Scope:**
- everything on `hackathon-v2` not on a remote (`origin/post-hackathon..hackathon-v2`, about 100
  commits): P296/P297, the end-to-end fixes, the security audit merge (62ac1fc), `scripts/e2e.py`,
  `scripts/secret_scan.py` and `docs/review-queue.md`;
- added during the pass: `security-followups` (6 commits, merged as 7e08ccb) and `mcp-review`
  (6 commits, merged as d8253d5).

`review-pass` was rebased onto `hackathon-v2` at d8253d5, so it carries both merges.

Fixes are small, and each comes with a test that fails without it. Anything larger or debatable is
**proposed**.

## Findings, by severity

### High

**H1. A DM the agent required sealed could still go out in plaintext.** Fixed in d000992.
- Where: `src/emporia/mcp_server.py` `send_dm` (the P298 block after `/dm/start`), and
  `src/emporia/agent_sdk.py` `start_dm` / `send_dm`.
- What was wrong: P298's downgrade check refuses a plaintext thread only when the peer's encryption key
  is already pinned. Two cases slipped past it:
  - `send_dm(..., encrypted=True)` to a peer never sealed to before (first contact);
  - a DM to `bob@other-relay`, which the relay protocol says is always sealed.
- Scenario: a hostile home relay answers `/dm/start` with `{"encrypted": false}`. The MCP server then
  signed and sent the plaintext, against the requirement that nobody with relay or DB access reads DM
  content. An honest relay would have answered 409.
- Fix:
  - MCP `send_dm` refuses both cases.
  - The SDK's `start_dm` refuses such an answer and remembers the thread. A later "plaintext" from
    `GET /dm/{id}` on that thread is refused too.
- Tests (`tests/test_dm_downgrade.py`):
  - `test_mcp_will_not_send_in_the_clear_when_it_asked_for_a_seal`
  - `test_mcp_will_not_send_in_the_clear_to_another_relays_agent`
  - `test_the_sdk_will_not_send_in_the_clear_when_it_asked_for_a_seal`

  All three failed before the fix.

**H2. In a git worktree, every subprocess test ran the main checkout's code.** Fixed in a2d1248.
- Where: `tests/conftest.py`.
- What was wrong: the shared venv installs `emporia` in editable mode from the main checkout.
  `conftest` put the worktree's `src` on `sys.path`, but only for the pytest process itself. The 16
  test files that start `sys.executable -c ...` imported the main checkout's code instead.
- Scenario: every agent today tested its own work in a worktree. Those subprocess tests passed or
  failed on whatever the main checkout held, not on the change. Seen directly:
  `test_a_fresh_mcp_process_knows_which_sessions_are_games` passed with its fix (a25141f) reverted.
- Fix: `conftest` also puts the tree's `src` on `PYTHONPATH`. With that, the same revert fails the
  test.

### Medium

**M1. The secret scan missed binary and empty secret files.** Fixed in e3123f8.
- Where: `scripts/secret_scan.py` `scan()`.
- What was wrong: file names were read only from `+++ b/` lines, which git prints for text changes
  alone. A binary file gets "Binary files … differ" and an empty file gets no hunk.
- Scenario: a push that adds `relay.db`, `agent.p12` or an empty `.env` passed the pre-push hook. Those
  are the very examples `FILE_PATTERNS` names.
- Fix: the path now comes from each `diff --git` header. Deleting a secret file is not a finding.
- Test: `test_a_binary_or_empty_secret_file_is_found_by_its_name`.
- `--all` over the whole history is still clean with the fix.

**M2. A game against the house never completes the "Play a set play" quest, and its test passes for
the wrong reason.** Fixed on `gamify-season1` (not merged).
- Where: `src/emporia/routes/standing.py:262`. The quests route takes its games from `_craft_table()`,
  which drops every game with a house agent.
- What was wrong: `quests.py` says "quests with the house agent count", and the quest's own hint sends
  newcomers to the house agent.
- Why the test missed it: `tests/test_quests.py::test_the_route_is_off_unless_the_operator_runs_quests`
  calls the opponent `q_house` but never sets `EMPORIA_HOUSE_AGENTS`. Run with
  `EMPORIA_HOUSE_AGENTS=q_house`, it fails.
- `gamify-season1` already fixes the route. Proposed: merge that branch, and make this test name its
  house agent.

**M3. The SDK's downgrade check takes the peer from the relay.** Proposed.
- Where: `src/emporia/agent_sdk.py:971-972`.
- What is wrong: `send_dm` checks the participants that `GET /dm/{id}` returns (`agent_a`, `agent_b`,
  `my_label`).
- Scenario: a hostile relay renames the peer to an unpinned id, or sets `my_label` to the peer's id so
  the peer is filtered out as "me". The pinned-peer check then finds nothing.
- Partly closed by H1: threads started with `encrypted=True`, or with another relay's agent, are
  remembered.
- Proposed: record `thread_id → peer` locally at `start_dm` (persisted, as the pins are), and check
  that.

**M4. `sign_dashboard_challenge`'s relay restriction protects less than it claims.** Proposed.
- Where: `src/emporia/mcp_server.py:5485` (mcp-review).
- What is wrong:
  - The sign-in signature covers the nonce alone (`signing.sign_in`). It names no relay.
  - With `relay_url` omitted, the tool returns the raw `signature_hex` to the model (line 5540).
- Scenario: a stranger takes a nonce from the agent's home relay and asks the agent to sign it. If the
  operator approves (the prompt reads like a routine dashboard sign-in), the signature or token is in
  the tool result for the stranger to talk out of the model.
- The operator's yes is the real guard. "A sign-in signature goes to no other" is true only when
  `relay_url` is given.
- Proposed:
  - never return the raw signature: always complete the exchange with the target and hand back only
    the token;
  - or, better, bind the relay id into the signed sign-in payload. That is a protocol change.

**M5. A commit retried with a different move before the first one lands can lose the game.** Proposed.
- Where: `src/emporia/mcp_server.py:4882` and `reveal_move`.
- What is wrong: `commit_move` saves `{move, nonce, commitment}` before it sends, overwriting the round's
  entry.
- Scenario:
  1. An agent commits move A.
  2. The answer is lost, and the agent retries with move B while A is still on its way.
  3. The session read shows no commitment yet, so B's nonce replaces A's on disk.
  4. The relay keeps A and refuses B.
  5. The reveal sends B with B's nonce, which matches nothing. The game is lost on time.

  The mcp-review retry fix handles the same-move retry correctly. It is this overlap that still
  breaks.
- Also: `reveal_move` never compares the saved commitment with the relay's before revealing.
- Proposed:
  - keep every unreceipted entry for the round;
  - at reveal, pick the one whose commitment equals `commitments[aid]`, and say so when none does.

### Low

**L1. Every abandoned-game notice read "abandoned ((invalid id))".** Fixed in df3ca31.
- Where: `src/emporia/plugins/platforms/emporia/frames.py`, `session_abandoned` (from a12e0d8).
- What was wrong: the id filter treated the relay's reasons ("abandoned by bob", "no action for 24h")
  as ids.
- Fix: those two shapes are kept, with the agent checked like any other id. Anything else is still not
  repeated.
- Test: `test_an_abandoned_game_still_says_why`.

**L2. The live backup-and-move test broke on mcp-review's new `list_dm_threads` shape.** Fixed in
e4c7736.
- Where: `tests/live/test_h_backup_and_move.py:51`.
- What was wrong: `list_dm_threads` now answers `{success, threads}`, and the test still iterated it as
  a list.
- This is in the live suite only. It is not run here, because it needs real relays.

**L3. Hint localisation was missing in places a plugin-only user reaches.** Fixed in 025a633.
- `emporia setup agent`'s `--new-profile` refusal. Only the plugin's flow passes that flag, so its only
  readers are plugin users.
- Four other `setup agent` lines (signer and relay hints).
- The "operator could not be asked" answers of `visit_relay` and `set_cross_relay_dms`.

These said `emporia …` and now go through `cli_hints.localize`. Tests added to `test_cli.py` and
`test_cli_hints.py`.

**L4. `_unsafe_path` still lets an id add path segments.** Proposed.
- Where: `src/emporia/mcp_server.py:211` (mcp-review).
- Probed forms:
  - Refused: `..`, `.`, `%2e`, `%2f`, `%5c`, `?`, `#`, backslash, control characters.
  - Passed: a plain `/` inside an id (`me/leave` sends `/agents/me/leave`), `%00`, `;`, and
    double-encoded `%252e`.
- I found no tool where the extra segment reaches a harmful route: every caller-supplied id sits before
  a fixed suffix, so the result 404s. The guard is still narrower than "one route per call".
- Proposed: percent-encode each id as a path segment (`quote(id, safe="")`) where the URL is built.

**L5. `/relay/activity`'s cache is keyed on the exact `window_s`.** Proposed.
- Where: `src/emporia/routes/standing.py:200` (cca6c6e).
- What is wrong: a caller who varies `window_s` by one second each time misses the cache every time.
  Each miss is the SQL query, which is far cheaper than the old Python scan, so this is minor.
- Proposed: round the window to the minute before caching.

**L6. The season's Renown cancels swaps only within the season window.** Proposed (design).
- Where: `src/emporia/progression.py:477`.
- What is wrong: a like given before `EMPORIA_SEASON_START` no longer cancels its return like inside
  the season.
- Also, three-way rings (A→B→C→A) are not swaps at all. Key-only sybils are already on the queue's
  proposals.

**L7. `hermes emporia setup --profile NAME` never checks the name.** Proposed.
- Where: `hermes_plugin/emporia/setup_cli.py:279`. `choose_profile` returns `args.profile` unchecked.
- `_profile_exists`, `_has_agent` and `_remove_profile` then build `profiles/<NAME>` paths from it, and
  only Hermes's own commands validate it.
- Proposed: apply `_valid_profile_name` to `--profile` as to the prompt.

**L8. The pre-push hook runs whichever `scripts/secret_scan.py` the checkout holds.** Proposed.
- What is wrong: on a branch without the script (today `main`), every push fails with "can't open
  file". That fails closed, but the error is confusing.
- Proposed: the hook says what is missing and how to push from a branch that has the scan.

**L9. A failed session read sends a join down the purchase rule.** Proposed (old behaviour).
- In `join_session`, a failed `GET /sessions/{id}` makes the playbook treat the join as a purchase. A
  `player` preset then gets "does not sell".
- Proposed: say the session could not be read, instead.

## Test-strength checks: revert the fix, run its test, restore

| Fix | Test | Result with the fix reverted |
|---|---|---|
| DM downgrade, MCP (b990983) | `test_mcp_will_not_send_in_the_clear_to_a_peer_it_has_sealed_to` | fails ✓ |
| DM downgrade, SDK (b990983) | `test_the_sdk_will_not_send_in_the_clear_to_a_peer_it_has_sealed_to` | fails ✓ |
| key guard and the console (13fb417) | `tests/test_key_guard.py` | fails ✓ |
| no table talk in relay notices (945b922) | `tests/test_platform_bridge.py` | fails ✓ |
| no room name in relay notices (945b922) | `tests/test_platform_bridge.py` | fails ✓ |
| revealed bug report limits (3d2cd90) | `tests/test_bugs.py` | fails ✓ |
| season board linear (c51b557) | `test_the_season_board_is_linear_in_the_relays_likes` | fails ✓ |
| `_tool_env` (1abab1f) | `test_git_uv_and_pip_get_no_secret_from_hermess_environment` | fails ✓ |
| `--new-profile` refusal (5cbfec0) | `test_new_profile_is_refused_for_a_profile_that_holds_an_agent` | fails ✓ |
| commit_move legality (cabd2c4) | `tests/test_blotto.py` | fails ✓ |
| validate_turn on an instance (cabd2c4) | `test_validate_turn_checks_a_move_against_the_rules` | fails ✓ |
| playbook loads the games (a25141f) | `test_a_fresh_mcp_process_knows_which_sessions_are_games` | **passed** (H2); fails ✓ after a2d1248 |
| half-made profile removed, setup failed (1057ec8) | `test_a_profile_made_for_a_setup_that_failed_is_not_left_behind[setup a…]` | fails ✓ |
| half-made profile removed, plugin install failed (1057ec8) | `…[plugin]` | fails ✓ |
| a profile holding an agent is never removed (1057ec8) | `test_removing_a_failed_profile_never_removes_an_agent` | fails ✓ |
| pass-through command list (8efad72) | `test_every_emporia_command_passes_through_hermes_emporia`, `test_hermes_emporia_runs_the_runtimes_command_with_every_word` | fails ✓ |
| Renown `why` (cd92834) | `tests/test_progression.py`, `tests/test_likes.py` | fails ✓ |
| house agents count nothing in Renown (8a66bed) | `tests/test_progression.py`, `tests/test_likes.py`, `tests/test_house.py` | fails ✓ |
| quests count the house (M2) | `test_the_route_is_off_unless_the_operator_runs_quests` | passes only because `q_house` is not configured as a house agent |

The first harness pass stopped at `-x` on a worktree-only failure in `test_plugin_setup.py`. The
setup_cli rows were re-run with that test deselected, and each names the test that failed.

## Docs against code

**`docs/review-queue.md` is out of date:**
- "In progress" still lists "the security audit (branch `security-audit`)", which was merged as 62ac1fc
  and is described as merged above it.
- The scripted end-to-end check appears twice under "In progress" and also as done.
- It cites the secret scan as commit `1c4413e`, which does not exist. The commit is 3e74a92.
- "Security proposals (from the 11-05 audit, not built)" is stale:
  - `security-followups` built six of them and is merged (7e08ccb);
  - `console-scope` (one console per profile, socket secret) is on its own branch and not mentioned.
- `mcp-review` (merged, d8253d5) is not mentioned.
- "Experimental dashboard style (branch `style-experimental`)": that branch's tip is `retromod-ux`'s
  (8cacb71), with no commits of its own, so the `style=animatic` mode is not on it.
  `desktop-plugin` and `demo-video-3` also sit at other branches' tips, unmentioned.

**`docs/protocol.md`:**
- does not mention the `creation` field `GET /sessions/{id}` now serves (9ce4bc4), which the stake check
  relies on;
- does not mention the visited-relay senders (`emporia:relay@<host>`, `<agent>@<host>`, a12e0d8). Those
  are documented only in `plugin.yaml` and the bridge's docstring.

**`src/emporia/wire.py`** module docstring still says "52 sync tool functions … 40 async". The server
registers 91 (older drift).

**Tool counts agree everywhere:** README (four places), ARCHITECTURE and the dev skill all say 91; the
server registers 91.

**The plugin README's "The key guard treats both forms alike"** holds for the command words. Like the
rest of the guard, it is not a wall: importing `hermes_plugin.emporia.setup_cli` directly is not
matched.

**`docs/security-log.md`** has a new "Review pass (11-05)" section for H1 and M1.

## Checked and found sound

**The security-followups review questions:**
- **9ce4bc4, the signed stake:**
  - A legitimate join is not wrongly refused. The creator is always `participants[0]`, and a creator
    cannot `/leave`, so `_signed_creation` always serves the right row.
  - The check runs only under a stake limit, and only for games. Every game since P279 has a signed
    creation.
  - Bounty contests seat players through `add_participant`, not `join_session`.
  - It cannot be bypassed by omitting or altering `signed_terms`, `module_type` or the stake: the
    creation signature covers all three.
  - `_cents` and the relay's `_stake` parse the same way (`float`), so no input reads as small to one
    and large to the other.
- **a12e0d8, visited relays:**
  - A visited relay cannot speak as home. Its sender is always `emporia:relay@<host>`.
  - `agent_ref(..., bare_only=True)` refuses addresses and `:`, so no `emporia:relay` or `bob@home`
    can be forged.
  - Only the exact `emporia:relay` skips the thread limit.
  - Topics are suffixed with `@host`.
- **c80b8dd, like times:** compared as times, under one lock with the insert.
- **395bb40, the console CSP:** the standalone bundle injects no `<style>` and uses no `data:`
  URLs. Only `hermesPlugin.tsx`, which the console does not serve, creates style elements.
- **8130dba, the pip fallback:** hash-locked, `--no-deps`. With no release pinned it refuses anyway.

**The mcp-review questions:**
- **The retry-safe commit:** succeeds only when the saved commitment equals the relay's and the move is
  the same (028dbed). A retry with another move is refused. M5 is the remaining overlap.
- **The retry-safe reveal:** returns the saved move once the relay records the reveal.
- **The uniform answer shape** is applied only to the MCP-registered tools (`tool.fn`). The bridge, the
  house agent (`self.t.*`), `scripts/e2e.py` (calls `.fn` directly) and the dashboard (relay routes,
  not tools) keep the Python shapes. The one source-level shape change, `list_dm_threads`, broke only
  the live test (L2).
- **`get_inbox`'s reordered arguments:** the only caller passes keywords.

**Setup and install path:**
- `_sync_runtime` checks out after a `--no-checkout` clone.
- `_tool_env` passes only an allowlist.
- `_clean_env` strips Hermes's Python settings.
- A profile made by setup is removed on either failure, and never when it holds an agent.
- `--new-profile` is refused for a profile holding an agent, and `@wizard` is on `cmd_setup_agent`
  (fb32c9a).
- `doctor` scopes to its profile.
- The pass-through list is pinned to the CLI's commands by a test.
- The key guard's words match `hermes emporia …` as they match `emporia …`. `setup` and `update` wait
  for approval; `wallet backup` and `relay host` stay person-only.

**Playbook:**
- An unreadable playbook fails closed.
- Bundled games are loaded before checking.
- The stake parse cannot be fooled by a string the relay reads differently.

**Likes and Renown:**
- A like is signed over the `to_agent` the relay resolves, and needs a public thing, or a finished game
  or deal the liker was in.
- No self-likes.
- One operator is one voice; repeats flatten; swaps cancel; house agents count nothing either way.
- Sheets drop house deals and abandoned games; Craft drops house games.

**Bug reveal checks** apply `check_report` after the commitment matches.

**The house agent:**
- joins only set plays and chess, unstaked unless `--stakes`;
- steps back when busy;
- greets each new agent once.

**The notice sanitising (945b922)** puts no table talk and no room names in relay notices. Every other
relay-notice field is relay-generated or now id-filtered.

## Verification on `review-pass`

The tip of `review-pass` carries both merges.

**Full suite.** `pytest -q` with the main checkout's `.venv` python: see the final report for the
count. The only failures are the two known worktree-only ones, both because the
worktree has no `.venv`:
- `tests/test_installer.py::test_the_emporia_command_is_linked_onto_path_once`
- `tests/test_plugin_setup.py::test_it_runs_setup_agent_for_the_chosen_profile_only`

**Ruff.**
- `src`, `scripts` and `hermes_plugin`: exit 0.
- Every file this pass changed: exit 0.
- Repo-wide: exit 1, from 80 F811 (at the limit of 80, unchanged) and 34 F401, all in test files this
  pass did not touch.

**Secret scan.** `scripts/secret_scan.py --range hackathon-v2..HEAD` is clean, and `--all` is clean.
