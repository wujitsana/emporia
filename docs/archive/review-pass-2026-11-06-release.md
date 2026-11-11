# Release review, pass 2: 2026-11-06

An independent release-readiness read of the hackathon entry: **Emporia** (`hermes_plugin/emporia`,
branch `preview-2` at `2eeb6f1`) and **purse** (`hermes_plugin/purse`, branch `purse-solana-pay` at
`b0527a1`). Fixes are on `review2-release` (from `preview-2`) and `review2-purse-release` (from
`purse-solana-pay`). Nothing is pushed, merged or submitted. Security, gamify correctness and the
dashboard UI were reviewed by others and are not repeated here.

**Method.**
- The catalog rules were re-read today from the submission guide
  (`hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission`) and
  `plugin-catalog/README.md` on hermes-agent `main`. Upstream's `scripts/validate_plugin_catalog.py` is
  byte-identical to the one in this machine's Hermes (0.22.6). **Hermes v0.23.0 was released on 11-05**;
  this machine still runs 0.22.6.
- `hermes plugins validate` on both plugins (throwaway `HERMES_HOME`), and the catalog validator on
  both entries.
- A first run as a new user, in a throwaway `HOME` and `HERMES_HOME`: the plugin installed from a local
  clone of `preview-2`, the checkout route from a `git archive` of `preview-2`, a relay of its own on
  `127.0.0.1:18931`, one agent set up and registered. No model call, no transaction.
- purse: its dependencies resolved under Hermes's exact pins in a scratch Python 3.14 venv (Hermes's
  interpreter). The live Hermes environment was not touched, so `--install-deps` itself was not run.

## 1. Catalog rules, rule by rule

**Validator output (both pass, no failures).**
- Emporia: every check passes, `loadable — entry: __init__.py, desktop/plugin.js`, `desktop surface`
  passes; one reviewer warning (rule 9, below) and the platform-adapter isolation note.
- purse: every check passes, `python dependencies — 5 requirements from pyproject`, no warnings.
- `validate_plugin_catalog.py docs/catalog/emporia.yaml purse.yaml`: `OK: 2 file(s) valid`.
- Neither name is taken upstream (`plugin-catalog/emporia.yaml` and `purse.yaml` are 404 on `main`).

| # | Rule | Emporia | purse |
|---|------|---------|-------|
| 1 | Human-merged gate | pass (process) | pass (process) |
| 2 | Exact SHA pins | **risk / release blocker**: the entry's `sha` and the screenshot URLs are placeholders, and `emporia_source.json` pins nothing, so `hermes emporia setup` from the plugin refuses ("pins no release of the runtime yet") until `scripts/pin_plugin_runtime.py` runs and both commits are pushed | **risk / release blocker**: placeholder `sha` |
| 3 | No self-updating code | pass: the runtime is cloned or moved only by `hermes emporia setup`/`update`, run by a person, to the pinned commit, verified by commit, tree and file bytes; the runtime's own `emporia update` refuses a catalog runtime; the Desktop half has no updater | pass: no subprocess, no dynamic import (`importlib.util.find_spec` only checks that a dependency exists) |
| 4 | SHA bumps are new PRs | pass (process) | pass; both plugins share the repo, so one bump's range covers both subdirs |
| 5 | Owner submits | pass | pass |
| 6 | Capabilities match | pass (validate: tools, hooks, middleware match) | pass |
| 7 | Install scanner | pass: `safe` | pass: `safe` |
| 8 | Desktop SDK surface | pass, checked by hand too: imports only `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`; no `eval`/`new Function`/dynamic `import()`/script tags; no `data-slot`/`data-testid` reads or body observers; the one `.prototype.` write is the bundled PEG parser's own class (allowed); `document` mouse listeners only for a drag | n/a |
| 9 | No core overrides | pass, **one reviewer warning**: `dashboard/plugin_api.py:245` imports `hermes_cli.web_server_chat._ws_auth_ok`. Still private on upstream `main` today, and the bundled kanban plugin still uses it; fails closed; disclosed; issue drafted | pass, no warning |
| 10 | Dependency policy | pass: the plugin declares none; the runtime's are bounded and locked (its own venv) | pass: five bounded ranges; resolve under Hermes's pins (eth-account 0.14.0, eth-abi 6.0.0, eth-utils 6.0.0, solders 0.27.1; pydantic, httpx, pycryptodome unchanged) and import on Python 3.14 |
| 11 | Credentials via Hermes | pass, with the disclosed known issues (agent-side `cmd:` key source; opt-in Nous token) | pass: `get_secret` / `save_env_value` only |
| 12 | Approvals, unattended | pass | pass; disclosed that approvals-off/yolo skip the threshold prompt while the caps hold |
| 13 | Disclosure | **was a gap, fixed**: the entry and PR text did not mention the Desktop page; both now do | pass (README and entry word for word, tested) |
| 14 | Compatibility metadata | pass: floor `>=0.22.6` in both places, below the current v0.23.0; `version` 1.0.0 = plugin.yaml = dashboard manifest = Desktop page. **Risk**: never run on 0.23.0. The runtime package says 0.2.0 (decision D3) | pass: 0.2.0 everywhere; same 0.23.0 risk |
| 15 | No skins of bundled | pass | pass |
| 16 | One listing per lineage | pass | pass: "Extracted from Emporia's money code (same repository)" is in the entry |
| 17 | No unscannable payloads | pass: no binaries or bytecode; two committed bundles (tab, Desktop), unminified, each with a source digest a test checks. The bundles carry srcl's MIT stylesheet, so the plugin now ships `LICENSE-srcl` | pass: source only |
| 18 | Password managers | n/a | n/a |

## 2. First run as a new user

Fixed (commits below):
1. **`hermes emporia setup --profile NAME` never reaches setup.** Hermes takes `--profile NAME`,
   `--profile=NAME` and `-p NAME` anywhere on its command line as its own profile switch. The documented
   non-interactive form failed with `Error: Profile 'shop' does not exist`. Now `--agent-profile`.
   The same switch ate `hermes emporia login --profile NAME`, and `hermes -p NAME emporia login` signed
   in the profile that holds the shared runtime, not NAME.
2. **The relay does not start after the README's checkout install.** `uv sync` / `pip install -e .`
   leave out the `x402` extra; the relay imports `eth_abi` at start (money module):
   `ModuleNotFoundError: No module named 'eth_abi'`. `emporia update`'s bare `uv sync --frozen` also
   removed the extra from a venv that had it.
3. **The checkout route wired the Hermes root as agent `.hermes`.** A profile fresh from
   `hermes profile create` has no `config.yaml`, so `install.py --install-profile`, run from a clone inside
   it (as the README says), walked up to `~/.hermes`. Its dry run then crashed on the missing file.
4. The README's checkout steps assumed a profile already exists, and gave no way to use a port other
   than 8088 (a second relay on a machine silently joins the first: `relay: already up`).

Seen, not changed (small, or a decision):
- `emporia setup agent` on a brand-new profile prints `keypair: … (existing)` for the key it just made.
- `hermes emporia doctor` ends with "Every check: `emporia doctor --all`" — under the plugin that is
  `hermes emporia doctor --all`.
- The installer seeds a demo relay by default (`--no-seed` to skip); a first-time user is not told.
- The restart advice says one gateway process serves every profile (`hermes gateway restart`, no `-p`).
  That is this machine's multiplexed setup; whether it is Hermes's default for every install is worth one
  check before the video.
- `hermes emporia status` on a checkout from an archive shows `@ ?` (no git): harmless.
- purse: a non-interactive `hermes plugins install` skips its dependencies and leaves it disabled
  (Hermes's own consent rule; `--yes-deps` answers it). Its README had no install steps (added on
  `review2-purse-release`). purse's CLI was not run here: enabling it would install its dependencies
  into the live Hermes environment.

## 3. Docs

Fixed:
- Catalog entry and PR text: the Desktop page, 91 → **95 tools**, the rule-9 warning's line
  (`plugin_api.py:245`), `npm run build:hermes-plugin` (gone: `build:tab`, `build:desktop`,
  `build:plugin`), Hermes v0.23.0 as the current release.
- `docs/catalog-compliance.md`: rule 8 was "n/a, no `desktop/plugin.js`"; it now applies and passes. The
  stale branch note (`catalog-rule11`) is gone.
- Plugin README: commands a plugin-only install cannot run (`emporia wallet mainnet`, `emporia signer
  host`, `emporia relay host`, `emporia agent delete`, …) now read `hermes emporia …`.
- `emporia-setup` skill: `../../docs/agent-security-setup.md` is outside the installed plugin; it links the
  repository.
- README: "Status — October 2026", 1,927 tests and P1-P250 → November, about 2,700, P299; the checkout
  steps (x402 extra, `hermes profile create`, port).
- RUNBOOK: the x402 extra. manual-testing: a spend script that is not in this repo.
- Hard-coded `/opt/data`: the installer's profile search, `scripts/cleanup_test.py`, the dev skill's `cd`
  lines and its seed reference. What stays is the Hermes image's in-container mount point
  (`docker-compose.yml`, `relay_host.py`, `signer_host.py`) and comments saying so: those are container
  paths, not this machine's.
- "Season 1": `docs/archive/preview-all.md` linked `docs/proposals/gamify-season1.md` (renamed to
  `gamify-launch-additions.md`). Branch names `gamify-season1` are left as they are.
- HANDOFF (11-03) now says the entry is built on `preview-2`, and points here.

Checked and fine:
- The removed `s` key / style mode / classic: only in history notes that say so
  (`ui-style-experimental.md` opens with "History"), and a test that asserts there is no `s` key.
- Every `emporia X Y` command in the user docs exists in the CLI; every `hermes X` the docs name exists in
  Hermes 0.22.6.
- Every `EMPORIA_*` setting in README, plugin README, skills, protocol, RUNBOOK, testnet, payments is read
  by the code (`emporia.agent_env` is kept complete by its own test).
- No wording promises rewards on real-money networks; "history kept" for `season-0` promises nothing.
- The tool count is 95 wherever it is current (README, ARCHITECTURE, dev skill); HANDOFF's older counts
  are dated changelog lines.

## 4. Decisions for you

- **D1. Root `LICENSE`.** `plugin.yaml`, `pyproject.toml` and both catalog entries say MIT, but the
  repository has no `LICENSE` file (only `dashboard/src/ui/kit/LICENSE-srcl`, now also in the plugin).
  A reviewer reading the pin will look for one. Add an MIT `LICENSE` naming the copyright holder you want.
- **D2. Screenshots.** The entry lists six `docs/catalog/screenshots/*.png` that do not exist yet, at a
  placeholder sha. Pick the six (blotto live, "your agent now", quests, sealed messages, results, phone
  menu are the drafted names), add them in the release commit, and pin the URLs to the entry's sha. An
  `image:` (2:1, e.g. 1200x600) card image is optional and would help the catalog card.
- **D3. One version.** The plugin, its dashboard manifest and the Desktop page are 1.0.0; the runtime
  package (`pyproject.toml`, `emporia.__version__`, which every relay reports in `/health`) is 0.2.0 (also
  open in `docs/review-queue.md`). Recommended: 1.0.0 for both (pyproject, `__init__`, and `uv.lock`'s
  `emporia` entry), or say in the PR why they differ.
- **D4. Test on Hermes v0.23.0** before the PR: the floor stays 0.22.6, but judges will run 0.23.x, and
  the tab's private `_ws_auth_ok` import and the platform adapter are the parts most likely to move.
- **D5. Release sequence** (needs your yes to push): `scripts/pin_plugin_runtime.py` (commit 1 = code, commit
  2 = `emporia_source.json`), push both, put commit 2 in `docs/catalog/emporia.yaml` and the screenshot URLs,
  re-run `hermes plugins validate` at that commit from a fresh clone, then the PR text. Until then the
  plugin route stops at "pins no release of the runtime yet".
- **D6. Which branch the README's checkout route clones.** It says `-b post-hackathon` ("main until it
  merges"); the entry is `preview-2`. Whatever you push for judging should be the branch the README
  names.
- **D7. purse as a second entry** (section 5).

## 5. Judging fit

**Usefulness.** Strong idea: a marketplace where agents play, trade, talk sealed and pay each other,
with a person's limits instead of per-move approvals. The catch is that it needs a second agent and a
relay to be useful, which raises first-run cost. The plugin route (`hermes plugins install emporia` →
`hermes emporia setup`) is the right shape; until the runtime is pinned and pushed it does not work at
all from the catalog.

**Reliability.** The suite is large (about 2,700 unit tests, live multi-relay scenarios), and setup
verifies what it installs. But the first run found three breakages a judge would have hit in the first
ten minutes (the `--profile` flag, the relay's missing extra, the root wired as `.hermes`), all in
paths the unit tests reach only through mocks. An end-to-end run of the README, from a clean home, on
Hermes 0.23, is the most valuable test left.

**Polish.** Docs are thorough and mostly current; the catalog drafts are detailed and honest
(disclosures, known issues). The Desktop page and the dashboard tab are more than most entries ship.
Weak spots: no LICENSE, placeholder screenshots, a version split, and a README whose first screen is a
status changelog rather than "what you see in 60 seconds".

**Creativity.** High: commit-reveal games for stakes, escrowed service deals, a relay-wide season with
quests, sealed DMs the relay cannot read, and a transparency log. "More than a prompt or a skill" is
clearly met (platform adapter, hooks, MCP server, dashboard tab, Desktop page, CLI).

**Top 5 before Nov 11.**
1. **Pin and push the release** (D5) and run the catalog route end to end from a fresh clone on Hermes
   0.23.0: install, setup two agents, a DM and a Blotto game between them. This is the judge's path, and
   today it stops at the first command.
2. **Screenshots and a card image** (D2), and an X video that shows the same two-agent flow.
3. **Add `LICENSE`** (D1) and settle the version (D3).
4. **Rewrite the README's top screen for a judge**: one paragraph, one screenshot, the three-command
   plugin quick start, then the status history further down.
5. **Re-run the first-run checklist above on the fixes** in a clean home (the fixes are tested in unit
   tests and were re-checked by dry run here, but the full flow with the gateway and the dashboard tab was
   not run in this pass: it needs a gateway restart and model calls).

**Should purse be a second entry?** Only if it is cheap to finish: it is a clean, general-purpose wallet
plugin (nine native tools, hooks, a CLI, its own tests), any agent can use it without a relay, and it
passes every rule with no warnings. Against it: both testnet proofs (`purse_testnet_proof.py` and the
Solana devnet one) have not run, Solana Pay has never touched devnet, a second entry needs its own video
and form, and it competes for attention with Emporia, whose money code it is. Recommendation: enter it
only if both proofs run green with your yes (testnet funds) by 11-09; otherwise keep it out of this
round and mention it in Emporia's PR as a companion still in review.

## 6. Checks

See the report in the commit history of `review2-release` and `review2-purse-release`; each branch was
checked with the full Python suite, ruff on changed files (F811 at 80), `scripts/secret_scan.py --range`
and `hermes plugins validate` on the changed plugin.
