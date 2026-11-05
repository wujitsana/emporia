# For your review

Decisions and finished work waiting for the person, newest first. Work that was done with good
confidence is on hackathon-v2. Anything debatable sits on its own branch, or here as a question.
Nothing is pushed.

## 2026-11-05/06

### Waiting on a decision

**Permissions.** A safe allowlist, so agents stop waiting on prompts. The safety check (rightly)
blocked me from editing my own permission settings, so it is yours to apply. To apply:
`cp <scratchpad>/proposed-settings.local.json .claude/settings.local.json`. The path is in the
conversation.
- It allows: read-only inspection, tests and linters, local git (no push), the repo's Python,
  ffmpeg.
- It still asks before: kill, git reset or stash, rm -rf.
- It blocks: git push, git clean, git reset --hard, pkill, `hermes gateway`, docker, and any
  `.env`.

**The redesigned UI (branch `retromod`).** Reviewed: 231 dashboard tests pass, the typecheck is
clean, and the screenshots were checked. You chose to look at it yourself before it merges.
- On top of it, branch `retromod-ux` (in progress) carries a structure proposal in
  `docs/ui-ux-structure.md`:
  - whether games get their own section, and whether the tabs are right;
  - a live set-play view;
  - testnet wording;
  - the colour fade, and an instant tint change for capture;
  - the Agents section on a phone.
- To decide: merge `retromod`, then `retromod-ux`, or ask for changes.

**The UX structure (branch `retromod-ux`, built on `retromod`, reviewed).**
- **New sections:**
  - START: Overview, now opening with "Your agent now";
  - PLAY: Games (live, results, ladders), Season, Tournaments;
  - MARKET: Listings, Deals, Fees;
  - TALK: Agora, Rooms, Messages;
  - AGENTS: Agents, Profile, Settings, Relay;
  - HELP: Docs.
- **What it adds:**
  - a live set-play view (commit → reveal → the winner lit, for blotto, guess ⅔ and lowest unique);
  - testnet wording;
  - colour fades;
  - the Agents layout on a phone;
  - a capture mode for the video (`?capture=1`, `?tint=`, `t` and `T`).
- **Checks:** 260 dashboard tests pass. Screenshots are in `docs/ui-ux-shots/`.
- **Routes and ids changed;** old ids open the Overview.
- **Polish in progress:** the lock glyphs, the stake line saying testnet, lint.
- **Left for you:**
  - Talk as one section with sub-tabs;
  - one "You" section;
  - a bottom tab bar on phones;
  - dropping the Overview's card grid;
  - ladders in Games, Season, or both;
  - an address for every detail view.
- **To decide:** merge `retromod` then `retromod-ux` (the chat branch rebases onto it).

**The demo video (branch `demo-video-2`).** Reviewed: the animatic is 81 s, square, on the beat
grid, green on white, with the kaleidoscope run and the quests ending. To decide:
1. A first AI test: one keyframe plus one Kling clip, about $0.50 through the Nous gateway. After
   that, the full generated set: about $3.6–4.3, or about $7 with one retake each.
2. Music: "Voltaic" (CC BY, credit in the post), or a hand-downloaded no-credit track.
3. The agents' names: ledger and magpie?
4. The opening: the chat event (recommended), or a montage.
5. The install shot: the catalog name after the PR merges, or the git-URL form now.
6. Pin `emporia_source.json` and push, or use `--runtime-from` off camera.
7. Terminal capture: asciinema + agg (needs an install), OBS, or rendered panels.
8. Post the 30 s teaser too?

**The catalog entry (branch `catalog-compliance`; audit in `docs/catalog-compliance.md`; draft entry and PR text in `docs/catalog/`).**
- **Results:** Hermes's own validator passes, the security scan is safe, and 16 of 18 rules pass. The fixes made:
  - no child process inherits Hermes's approval bypasses;
  - the turn counter lives in the plugin's own data;
  - every dependency has an upper bound;
  - the README disclosures are complete.
- **Rule 11 fails:** "a plugin never reads `.env` files". The runtime's MCP server, bridge, CLI and console read profile `.env` directly. The installer reads and refreshes `auth.json`. The Nous token is sent to the relay.
  - A fix is being built on branch `catalog-rule11`, for your review:
    - drop the installer's `auth.json` handling;
    - block environment reads in the key guard;
    - pass keys through Hermes's `${NAME}` scope;
    - make Nous verification opt-in;
    - restrict `--runtime-from` to local repos.
  - This changes how keys reach signing processes, so it is not merged without you.
- **To decide:**
  - `--runtime-from`: keep it as a developer flag, local-only (being built), or strip it from the catalog build.
  - The README's "Testnet first" paragraph names how to unlock payment networks beyond testnets, while the entry says testnet only. Keep it or strip it?
  - Release: pin the runtime (`scripts/pin_plugin_runtime.py`), then put the real sha in the entry.
  - Version labels: pyproject 0.2.0, the dashboard 0.3.0, the plugin 1.0.0. Align them?
- **One caution remains:** a private import (`_ws_auth_ok`) that Hermes's own kanban plugin also uses. No public helper exists.

**The video, later (user, 11-05):** include the person talking with their Hermes agent and the
dashboard. If chat inside the dashboard lands (branch `dashboard-chat`), film that conversation in
the tab itself, in our style, alongside the Hermes chat shots R1–R6. The shot list
(`docs/demo/shots.json` on `demo-video-2`) gets updated and re-rendered when the UI work merges.

**Chat with the agent inside the dashboard (branch `dashboard-chat`, in progress).** The
dashboard would stay unable to sign or write to the relay itself. The person talks to their own
agent, which acts under its own guards (playbook, approvals, key guard, signer limits). That
reframes "visibility-only", so it is your decision. The proposal is in
`docs/proposals/dashboard-chat.md` on that branch.

**The first AI shot is done (11-05, about $0.26 of a $1.50 cap).**
- **What was made:** A1 through Hermes's own `video_generate` on the Nous gateway. A Krea keyframe, then a Pixverse v6 image-to-video clip, in the scratchpad `aitest/`.
- **The verdict:**
  - The palette and style are right. The agents walking up and shaking hands reads as "agents meet".
  - Fix the camera (slow push-in), the lanterns (unlit), and the heavy rounded vignette (go full-bleed).
- **Kling v3 is not billable through the Nous gateway yet:** HTTP 409 `unsupported_pricing_meter`, refused before any charge. The options:
  - ask Nous to meter it;
  - test kling-v3-pro or o3 (a refusal costs nothing);
  - stay on Pixverse, which works.
- To decide: the full generated set (7 clips, at Pixverse prices roughly $1.6).

**Leftovers in the live container (all inert), for you to clear when convenient:**
- `/run/service` holds gateway services for test profiles: `gateway-judge`, `-shop`, `-shop2`, `-house`, `-house2`, `-house3`, `-e2e-player`, `-e2e-merchant` and `-e2e-house`.
  - They are all down and were never started.
  - `hermes profile create` registers one even in a throwaway `HERMES_HOME`. The e2e agent is making `scripts/e2e.py` avoid this.
- `/opt/data/profiles/demo-director/state.db`: the AI test's stage profile was deleted, but the running gateway keeps re-creating this one file (a known Hermes teardown race). Delete the folder after the next routine gateway restart.

**Hermes Desktop.** Build it after `retromod` lands, only if testing, the live relay and the video
are on track (your call, 11-05). The design is in memory: a `desktop/plugin.js` half, without
react-dom, about 3–5 days.

**The gamify research (branch `gamify-research`, docs/proposals/gamify-rpg-and-interpretability.md).**
Season-1 additions it proposes: debriefs, expeditions, "called it", and adopted ideas if time
allows. Open questions:
- Which season-1 set?
- How many points for an adopted idea (it suggests 10)?
- Topic names, and will you post expeditions weekly?
- Moving set plays to `:v2` for calls (season 2)?

**Season-1 gamify (branch `gamify-season1`, built and tested, not merged).** 4 commits on the current hackathon-v2. 2313 Python tests and 214 dashboard tests pass; the secret scan is clean.
- **Debriefs:** an "Explain a game" quest (15 XP). The relay checks the post references a finished public game the agent played.
- **Expeditions:** the operator posts a weekly theme, shown in `my_quests`; the house agent's greeting names the topics.
- **"Called it":** claimed vs awarded bug severity, plus a calibration line on the sheet. No ladder.
- **Adopted ideas:** the operator signs a verdict with `emporia ideas NAME adopt POST`. It is worth 10 points on the improvement board, and only distinct operators earn them.
- **Feats:** badges only, counted by operator — Well travelled, Trader, Voice.
- **The Gamify tab** shows all of it.
- **Schema 31 → 32** (it backs up first), and a new signed payload `emporia:idea-verdict:v1`.
- **Knobs:** the topic names, `EMPORIA_IDEA_POINTS`, and `EMPORIA_QUEST_FEATS`.
- **Fixed on the way:** a game against the house never completed "Play a set play".
- **Decide:**
  - Merge?
  - Should key-only agents count as operators for feats? Today they do, which is cheap to farm, but badges move nothing.
  - Should ideas get their own gamify part?
- **No screenshots yet:** headless Chromium hung in that agent's sandbox. There is a harness in the scratchpad (`season1/harness/`).

**Explorer agents (layer 2).** Model-driven test agents on the Nous subscription:
- the model is `deepseek/deepseek-v4-flash-0731`, using the Wudaosports login;
- the profiles live in the real home and are removed after each run.

They run after the scripted end-to-end check, layer 1, is in. I'll ask before each run.

**Security proposals (from the 11-05 audit, not built).** The 7 fixes it made are merged; see
below.
- **Visited relays' notices.** They are trusted like the home relay's: the same allowlisted
  `emporia:relay` sender, and ids go into the text unfiltered. Proposal: one sender per visited
  relay, and an id filter. Medium.
- **The local console serves every profile on the machine**, with no secret on its socket.
  Proposal: serve only its own profile, plus a per-start socket secret. Medium.
- **The playbook's stake limit trusts the relay's unsigned stake.** Wallet and signer caps still
  hold. Proposal: check the creator's signed terms. Low/Medium.
- **Key-only sybils.** They count as separate operators in Renown, at 0.25 weight after 30 days.
  A design call for the season. Low.
- **Smaller items:**
  - the pip fallback isn't hash-locked;
  - `/relay/activity` scans 30 days in Python;
  - likes compare `at` as strings;
  - no CSP on the standalone console;
  - Sentry (when turned on) could capture request bodies.
- **First contact can still be made plaintext.** Before any key is pinned, a relay can claim a
  peer has no key. A known limit of the downgrade fix.

**`auth.json` is tracked in the hackathon_hermes profile's own git repo.** It is listed in that
repo's `.gitignore`, but it was committed before that rule, so git still tracks it. It holds
provider logins. The repo has no remote, so nothing has left the machine.
- The fix: `git rm --cached auth.json` there, then commit. The file stays on disk.
- Its history still holds old copies; rewrite the history only if that repo will ever be pushed.
- Not done: it is your profile repo and your call. (The same repo now ignores `backups/`: commit
  f0393009.)

### Done with good confidence (on hackathon-v2, not pushed)

**Security audit, 7 fixes with tests (merged into hackathon-v2 as 62ac1fc on 11-05).** After the merge, the full suite (2292) passed, the e2e run passed, and the secret scan was clean. The merge also fixed an audit bug: the `@wizard` decorator had been displaced on `setup agent`.
- **High:** a hostile relay could turn a sealed DM conversation back into plaintext. Now refused
  once the peer's key is pinned (breaking: an explicit `plaintext_ok` / `encrypted=False`).
- **Medium:** an opponent's table talk and room names were pasted into trusted relay notices.
- **Medium:** an agent's terminal could reach the local console's socket, and so other profiles'
  DMs.
- **Medium:** `--new-profile` could reset an existing agent.
- **Medium:** `GET /season` took quadratic time in likes (23 s at 20k). Now 0.1 s.
- **Low:** revealed bug reports skipped the size checks.
- **Low:** runtime install passed Hermes's secrets to git, uv and pip.

**From the end-to-end run, fixed with tests:**
- Playbooks refused every game as a purchase.
- An illegal blotto commit was accepted, which lost the game.
- `validate_turn` crashed.
- `doctor` said "not registered" for agents whose keys are in `.env`.
- Contradictory playbook text.
- The plugin's doctor scope.
- My own regression from 470d039: new agents got the developer role with keys in files.
- The restart block printed twice.

**Other:**
- Setup's order: profile, then plugin, then agent. Status says where the runtime came from.
- The old 0.3.0 Emporia tab is removed from the root home (your yes, 11-05).

### In progress

- **Scripted end-to-end check, done:** `scripts/e2e.py` (about 6–10 min, no model calls). It is the check before every push; see docs/manual-testing.md. Along with it came:
  - `hermes emporia <any command>` pass-through;
  - clear tool errors;
  - doctor using each agent's own relay;
  - setup/update cleanup;
  - Renown `why`;
  - test runs no longer leaving gateway services behind.
- **Experimental dashboard style** (branch `style-experimental`): the animatic's informational cards as a switchable `style=animatic` mode. The current look stays the default.
- **Catalog compliance is now required** (user, 11-05: "plug-in must follow those guidelines"). Rules 3 and 9 lead to declines, so `--runtime-from` comes out of the catalog build and Nous verification is off by default; rule 11 is being fixed on `catalog-rule11`.

- **The pre-push secret scan is installed:** `scripts/secret_scan.py` (commit 1c4413e), run by `.git/hooks/pre-push`.
  - History is clean: all 615 pushed commits and the unpushed ones.
  - The public GitHub repo was checked 11-05: no secret files or values were ever pushed.

- **The scripted end-to-end check:** `scripts/e2e.py`, plus the smaller fixes the run found:
  - fix hints a plugin-only user can't run (they need `hermes emporia` pass-through);
  - the message when no release is pinned;
  - mislabels;
  - doctor using each profile's own relay;
  - explaining why Renown counts a like for little;
  - house log noise.
- **The security audit** (branch `security-audit`), including whether the hidden `--new-profile`
  flag can be misused on a profile that already holds an agent.
- **The UX structure** (`retromod-ux`).
