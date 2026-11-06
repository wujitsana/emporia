# For your review

What is waiting for you. Good-confidence work is merged into `hackathon-v2`; anything debatable is
on its own branch. Nothing is pushed or submitted. Rewritten 11-05 against the actual branch state,
after the independent review found this file out of date.

## Start here: `preview-2` (being built 11-06)

`preview-2` is hackathon-v2 + `gamify-all` + `ui-v2`: everything waiting, in one branch, loaded
into the sandbox (`sandbox/try-1105`, see its TRY-ME.md). Review it there; the branches below
are its parts and can still be taken one at a time. `preview-all` is superseded by it.

## Merged into hackathon-v2 (latest verified at 274f96a)

At ff37fc6: the full suite passes (2407), the secret scan over all branches is clean, the
end-to-end run passes (twice), and `hermes plugins validate` passes with one documented warning.

| Merge | What it brought |
|---|---|
| End-to-end work (da15e9c and the fixes before it) | `scripts/e2e.py`, the check before every push, about 4–10 min with no model calls. Also: `hermes emporia <any command>` pass-through; hints that a plugin-only user can run; clear tool errors; doctor checking each agent's own relay; cleanup of a half-made profile on setup failure; `update` saying "nothing to do"; Renown `why`; quieter house logs; test runs that no longer leave gateway services behind. Fixes found by the run: the playbook refused every game as a purchase; an illegal blotto commit was accepted; `validate_turn` crashed; doctor said "not registered" for keys in `.env`; my regression in 470d039, which gave new agents the developer role. |
| `security-audit` (62ac1fc) | A hostile relay could turn a sealed DM conversation into plaintext. Other agents' words appeared in trusted relay notices. An agent's terminal could reach the local console. `--new-profile` could reset an agent. `/season` was quadratic. Revealed bug reports were unchecked. Installs leaked secrets to git, uv and pip. |
| `security-followups` (7e08ccb) | Visited relays get their own sender id and filtered ids. The playbook's stake limit is checked against the creator's signed stake. `/relay/activity` runs in SQL. Like times are compared as times. The console has a CSP. The pip fallback is hash-locked. |
| `mcp-review` (d8253d5) | A path-traversal guard on tool ids. Sign-in signing is limited to home and visited relays. Bounded chain walks. `list_dm_threads` works again. Agent-facing tool descriptions. Every answer has `success`. Commit and reveal are retry-safe. DMs, rooms and the inbox page. Twelve larger proposals are in `docs/proposals/mcp-review.md`. |
| `review-pass` (9df9bbd) | A DM the agent required sealed could still go out in plaintext on first contact or cross-relay (high). Subprocess tests in worktrees tested the main checkout's code. The secret scan missed binary and empty secret files. The abandoned-game notice. Hint wording. Five medium proposals are in `docs/review-pass-2026-11-05.md`. |
| Catalog compliance (`catalog-compliance` and `catalog-rule11`, merged at 574832a, plus 99b967a, 60f077b, ff37fc6) | Every one of the 18 catalog rules passes. Profile `.env` access goes only through Hermes; the MCP server gets its settings through `${NAME}`; no `auth.json` handling; the key guard blocks reads of process environments; `--runtime-from` is removed; the runtime is verified file by file against the pinned tree; Nous verification is opt-in; dependencies are bounded. The integration review fixed four more problems: tampered or untracked runtime files passing verification; a refused setting write counting as success; a stale cache; a key linked by name not reaching the bridge. `scripts/e2e.py` now installs a release of HEAD made in its own folder, with no product flag. |
| `review-followups` (f2a4063, plus 8329310) | The SDK keeps a DM thread's peer (M3); write-once commits (M5); the sign-in names its relay (M4; relays reached by another name need `EMPORIA_RELAY_SIGN_IN_ORIGINS`); Renown v2 (swaps judged on all-time likes, rings of three count a quarter); `join_session` says a session cannot be read (L9); the pre-push hook works on branches without the scanner (L8); setup checks profile names (L7) with a mirrored copy of Hermes's rule, tested against Hermes, so no core import (catalog rule 9). Verified at 8329310: 2450 tests, the secret scan, e2e twice (19/19), `hermes plugins validate`. |
| `operator` (176dfaa) | The Operator section: `GET /operator/whoami` (signed, only the relay's `EMPORIA_AGENT_ID`), `/operator/status` (bug and idea queues, expedition, house agents, health, season), `/operator/snapshot`; a dashboard view (copy-only commands, key `o`); `emporia operator`, `house start/stop/status`; an `operator` playbook preset; README "Run your own relay"; manual-testing sitting 10. Idea `adopt` commands appear once the launch additions merge. When `ui-v2` merges, add `operator` to More after Relay. |
| `signer-phase2` (274f96a) | Signer 2b (opens only sealed messages addressed to its own agent) and 2c (`signer add --make`: keys made in the signer; rotation, recovery and encryption-key replacement inside it; sealed backup/restore, person-only). Back ends: local, 1Password/Bitwarden (signer-side `--key-source`), AWS KMS, Privy, CDP — the last four tested against fakes only and labelled so; your own accounts needed (manual-testing sitting 7). KMS Ed25519 signs ≤ 4096 bytes. boto3 is an optional `kms` extra. Verified at 274f96a: full suite 2545 passed (main checkout), e2e 23/23 on the branch, secret scan clean. |
| `signer-metamask` (merged after 274f96a) | MetaMask Agent Wallet as a signer back end, through its `mm` command: x402 and escrow typed data and making wallets only; Guard Mode required; its own limits (USDC transfer types, Base Sepolia, the USDC contract, an optional max) because MetaMask's outflow limit does not count signatures yet. Tested against a fake `mm`; your own sign-in needed (sitting 7). |
| `scripts/secret_scan.py` (3e74a92) | Installed as the `pre-push` hook. Pushed history was checked 11-05 (615 commits on the public repo): no secret files or values ever pushed. |

Also done on 11-05: the old 0.3.0 tab was removed from the root home; the profile repo ignores
`backups/` and no longer tracks `auth.json` (3ad52711, the file kept on disk). A backup bundle of
every branch is in `../backups/emporia/`.

**Note for the branches below:** before the review pass, subprocess tests in worktrees ran the
main checkout's code. Each branch reruns its tests when it is merged.

## Built on branches, waiting for you

**1. `ui-v2` — the dashboard (contains `preview-all`: retromod, retromod-ux, the animatic style,
dashboard-chat, desktop-plugin, gamify-season1, console-scope).**
- **From your sandbox test:** the ASGI handshake error (the tab no longer dials sockets where
  Hermes's plugin host carries none); chat composer always visible and slim, full-page chat has a
  back button and menu; the app is one contained frame, so nothing spills into Hermes; the animatic
  look is the only style (classic, `s`, Settings › Look removed); EMPORIA decodes from E#P0R!A.
- **Navigation:** six destinations Home · Play · Market · Talk · Season · More (`docs/ui-nav-v2.md`);
  keys 1–6 and `[` `]`; every old route still opens.
- **Badges:** pixel art in the theme colour on quests, feats, boards, agent and game rows; gallery at
  More › Badges.
- **Yours:** the look in a real browser (no screenshots possible here); chat approvals (a) as built,
  (b) chat without approvals, (c) no chat; console-scope's breaking change (`--uds` needs
  `--profile`); a real Desktop check (`docs/manual-testing.md`).

**2. `gamify-all` — launch additions + calls/Insight + Bargain + pledges/whats_next.**
- Integrated on hackathon-v2; full suite 2673 passed, e2e 24/24. Schema 31 → 33 (backed up first),
  set plays `:v2` (`:v1` refused at create; running games finish), `quests:v3`.
- **Launch additions** (was "season 1"): debriefs, weekly expeditions, called it, adopted ideas
  (a signed operator verdict, person-only), feats.
- **Calls and Insight:** a forecast sealed with the first move, scored against the rating; an
  Insight board.
- **Bargain:** a negotiation set play with private values dealt fairly; no stakes.
- **Pledges and `whats_next`:** signed promises with a track record (not in Trust for season-0);
  one ranked to-do call that the skill starts with.
- **Fixed on the way:** idea verdicts posted from an agent tool (now a person's terminal); a merge
  that dropped a schema line; pledges that only knew `:v1`; one unreadable session stopping the
  deadline sweep.
- **Your decisions (19, deduplicated):** in the gamify-all report and each proposal doc
  (`gamify-calls.md`, `gamify-bargain.md` §10, `gamify-pledges.md` §4,
  `gamify-launch-additions.md`). Every one works today with the built default.
- **Live relay:** this changes the agent↔relay protocol (`:v2`, schema 33). Restart the relay
  first, then the gateway.

**3. `purse` (v0.1) and `purse-solana-pay` (v0.2) — a separate catalog entry, go/no-go Nov 9.**
- A self-custody testnet wallet for any Hermes agent: Base Sepolia, then Solana devnet with
  Solana Pay links. Caps $1 a payment / $5 a day across both, approval above a threshold, every
  other network refused in code. Validator safe; 207 tests.
- **Yours:** the go/no-go; the two testnet proofs (real testnet transactions, free faucet funds;
  commands in the conversation); the catalog PR.

**4. Release steps (catalog) — yours.**
- README "Testnet first" paragraph: keep or cut? Version labels: plugin.yaml/dashboard 1.0.0,
  pyproject 0.2.0 — align?
- Run `scripts/pin_plugin_runtime.py` and commit; the sha into `docs/catalog/emporia.yaml` and the
  screenshot URLs; screenshots; re-validate; the PR text is `docs/catalog/PR.md`.

## The demo video

- **Rough cut v3 (`demo-video-3`, scratchpad `animatic/v3/`).** 81 s, square, on the beat grid,
  green on white, the kaleidoscope run, ending on quests. The intro and end card show the official
  Nous girl mark unaltered on her white tile, with a truthful "built for the … hackathon" credit.
- **The first AI shot** (Pixverse through the Nous gateway, $0.26): the style is right; the camera
  and the vignette need fixing. Kling is not billable on the gateway yet.
- **Your decisions:**
  1. ~~The Nous mark~~ — decided 11-06: no Nous logo; the text-credit intro and end card are used.
     MIT covers the file, not the trademark.
  2. A disclaimer in the post: "independent entry, not affiliated with or endorsed by Nous
     Research".
  3. The full AI set: 7 clips, about $1.6 on Pixverse.
  4. Music: Voltaic (CC BY, credited) or a no-credit track.
  5. The agents' names (ledger and magpie?) and the opening (the chat event?).
  6. Terminal capture: rendered panels (no install), OBS, or asciinema.
- **Order:** dashboard and chat shots are filmed after the UI merges, including chat in the tab
  if `dashboard-chat` lands.

## Other decisions

- **Permissions:** a safe allowlist is ready to apply with one `cp`. The command is in the
  conversation, and the proposed file is in the scratchpad and the backups.
- **Explorer agents (layer 2):** model-driven test agents on the Nous subscription
  (`deepseek/deepseek-v4-flash-0731`). Ready to set up now that layer 1 exists. I ask before
  each run.
- **Gamify research** (`gamify-research`): the season-2 ideas, such as calls and Bargain.
- **Proposals not built:**
  - MCP: role-scoped tool lists, tool merges, `whats_next`, pooled HTTP clients;
  - review pass: dashboard sign-in signature scope, in-flight commit retries, SDK participant
    trust;
  - security: key-only sybils in Renown.

## Housekeeping (inert, clear when convenient)

- `/run/service` holds nine leftover test gateway services: `gateway-judge`, `-shop`, `-shop2`,
  `-house`, `-house2`, `-house3`, `-e2e-player`, `-e2e-merchant`, `-e2e-house`. All are down and
  never started. `scripts/e2e.py` no longer leaves these behind.
- `/opt/data/profiles/demo-director/state.db`: delete it after the next routine gateway restart.
- After merging `dashboard-chat`, rebuild the tab bundle; each UI branch rebuilds it.
