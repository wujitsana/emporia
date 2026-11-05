# For your review

What is waiting for you. Good-confidence work is merged into `hackathon-v2`; anything debatable is
on its own branch. Nothing is pushed or submitted. Rewritten 11-05 against the actual branch state,
after the independent review found this file out of date.

## Merged into hackathon-v2 (verified at ff37fc6)

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
| `scripts/secret_scan.py` (3e74a92) | Installed as the `pre-push` hook. Pushed history was checked 11-05 (615 commits on the public repo): no secret files or values ever pushed. |

Also done on 11-05: the old 0.3.0 tab was removed from the root home; the profile repo ignores
`backups/` and no longer tracks `auth.json` (3ad52711, the file kept on disk). A backup bundle of
every branch is in `../backups/emporia/`.

**Note for the branches below:** before the review pass, subprocess tests in worktrees ran the
main checkout's code. Each branch reruns its tests when it is merged.

## Built on branches, waiting for you

**1. Catalog compliance is merged (see above). For the release, yours:**
- **README wording:** the plugin README's "Testnet first" paragraph mentions the person-only
  unlock for real-money networks. Keep or cut?
- **Release steps:** run `scripts/pin_plugin_runtime.py` and commit (it records sha and tree);
  put the sha in `docs/catalog/emporia.yaml` and the screenshot URLs; add the screenshots;
  re-validate at that commit. The PR text is `docs/catalog/PR.md`.
- **Version labels:** `desktop-plugin` sets the dashboard manifest to 1.0.0, matching
  plugin.yaml; pyproject is still 0.2.0. Align it?

**2. Dashboard UI: `retromod`, then `retromod-ux`.**
- **`retromod`:** the redesign. Phone list/detail views, grouped navigation, section headings,
  plus fixes for dead styles, hotkeys firing while typing, and one view's crash blanking the app.
- **`retromod-ux`:**
  - sections Start / Play / Market / Talk / Agents / Help;
  - a live set-play view (commit → reveal → winner);
  - "Your agent now" on the Overview;
  - testnet wording;
  - colour fades;
  - capture mode;
  - text instead of emoji that render as boxes;
  - 262 tests.
- **Ideas it did not build, for you to decide:** Talk with sub-tabs, a "You" section, a bottom
  tab bar on phones, and an address for every detail view.
- **To decide:** merge both (you wanted to look first).

**3. Experimental "animatic" style (`style-experimental`, on `retromod-ux`).**
- **What it is:** the animatic's informational cards as a switchable style — the `s` key,
  Settings › Look, or `?style=animatic`. The current look stays the default.
- **To pick:** from the 15-item "promote to default?" list in `docs/ui-style-experimental.md` §6.
  The first items are readability fixes the default needs (some secondary text is at
  1.3–2.4:1 contrast).

**4. Chat with your agent in the dashboard (`dashboard-chat`, on `retromod-ux`).**
- **What it is:** through Hermes's own chat API, with streaming, tool and approval cards, 130
  slash commands, a session switcher for this profile only, and "ask agent" prefill. It works in
  the Hermes tab only.
- **To decide on approvals:** (a) as built — Emporia key, wallet, limit and role changes get
  "allow once" or "deny" only; (b) chat without approvals; (c) no chat.
- **After that:** a real-model test on a test profile.

**5. Console scoping (`console-scope`).**
- **What it is:** the local console serves only its own profile, behind a per-start socket
  secret.
- **Trade-offs:** it is breaking (`--uds` needs `--profile`); there is one console per profile
  opened; options are in `docs/proposals/console-scope.md`.
- **Order:** merge after the catalog work.

**6. Season-1 gamify (`gamify-season1`).**
- **What it adds:**
  - debriefs (the "Explain a game" quest);
  - weekly expeditions;
  - "called it";
  - adopted ideas, a signed operator verdict worth 10 points;
  - three feat badges;
  - all of it in the Gamify tab.
- **What it changes:** schema 31 → 32 (backed up first), and a new signed payload
  `emporia:idea-verdict:v1`. It also fixes "a game against the house never completed its quest".
- **Yours:**
  - merge?
  - should key-only agents count as operators for feats?
  - should ideas get their own gamify part?

**7. Hermes Desktop half (`desktop-plugin`, on `retromod-ux`, built).**
- **What it is:** the whole dashboard as a native Desktop page, built from the same source as the
  tab with one `npm run build:plugin`. It adds a sidebar row, 8 palette commands, a "your turn" or
  unread status chip, and an opt-in notification. It is off until enabled in
  Capabilities ▸ Plugins.
- **Shared with the tab:** a test fails if either bundle is stale; the version (1.0.0) comes from
  one source.
- **Checks:** Hermes's desktop-surface lint passes with 0 hits, and validate passes. 17 checks
  pass against a real throwaway Webapp backend; 279 tests pass.
- **Not seen yet:** the sandbox's browser won't start, so there are no screenshots. A real
  Desktop session is needed (checklist in `docs/manual-testing.md`, about half a day,
  likely some CSS tuning).
- **Order:** it merges after the UI branches.

## The demo video

- **Rough cut v3 (`demo-video-3`, scratchpad `animatic/v3/`).** 81 s, square, on the beat grid,
  green on white, the kaleidoscope run, ending on quests. The intro and end card show the official
  Nous girl mark unaltered on her white tile, with a truthful "built for the … hackathon" credit.
- **The first AI shot** (Pixverse through the Nous gateway, $0.26): the style is right; the camera
  and the vignette need fixing. Kling is not billable on the gateway yet.
- **Your decisions:**
  1. The Nous mark: keep it as a credit, ask Nous first, or use the text-credit version (built).
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
