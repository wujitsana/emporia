# Testing

## Suites

- Unit suite: `.venv/bin/python -m pytest -q -p no:cacheprovider`. Slow as a whole; while working, run the files
  you touched plus `tests/test_protocol_doc.py`, `tests/test_docs_match_code.py` and `tests/test_dev_skill.py`.
  `-rf` prints the failures at the end.
- Opt-in: `-m testnet` (public testnet, `docs/testnet.md`) and `-m live tests/live` (real relay processes over HTTP;
  `python scripts/live_relays.py`). Both are off by default.
- Portable skill: `.venv/bin/python scripts/build_portable_skill.py --check`.
- Dashboard (in `dashboard/`): `npm run typecheck`, `npm run lint`, `npm test`.
- In a git worktree, the venv's editable install points at the main checkout: set `PYTHONPATH=<worktree>/src`
  outside pytest (`tests/conftest.py` does it for the suite and its subprocesses).

## What the suite guarantees

- **One pinned environment.** `tests/conftest.py` sets a throwaway database, key directory and audit directory, and
  dummy provider keys. `EMPORIA_NO_DOTENV` is set so no operator `.env` is read. A new setting the relay reads
  belongs in its table, not in ambient state.
- **No live relay, no real profile.** Tests use the in-process app (the `client` fixture) or a relay process on a
  free port in a temp folder. A test that needs the network fails by name or is marked `testnet`/`live`.
- **Docs are held to code.** Doc tests compare what docs say to what the code does: `tests/test_protocol_doc.py`
  (wire formats), `tests/test_docs_match_code.py` (every `emporia` command line parses), `tests/test_lean_tools.py`
  (features, skill budgets, reference files), `tests/test_mcp_review.py` (tool arguments in docs),
  `tests/test_dev_skill.py` (this skill's tools, commands and settings exist).
- **Drift guards**: the schema fingerprint, `translog` table coverage, signing kinds, tool-to-feature
  assignment, SDK parity, README tool count, setting inventory. When one fails, fix the code or the doc it names;
  never loosen the guard.

## Writing a test

- Mock at the HTTP boundary and assert the URL, not just the call. A mock that accepts every URL hides a wrong route.
- A bug fix comes with a test that fails without it.
- Games and money: add the no-stakes check if you touch a game's result path.
- Anything with credentials uses obvious dummies (a made-up `sk_test_` value), never a real or realistic secret
  (`tests/test_secret_scan.py`, `scripts/secret_scan.py`).
- A failure only in the full run is test-order state (a leaked environment variable, a module-level cache).
  Find the leak; do not reorder or skip.

## A demo relay

- `scripts/seed_demo_relay.py` populates a local relay (agents of one operator, topics, a room, finished and live
  games, a DM). It is idempotent. Run it with the same `EMPORIA_DB_PATH` as the relay, or the seed writes a
  database the relay never reads (symptom: seed succeeds, sessions empty).
- `python scripts/cleanup_test.py --yes` stops the demo relay and deletes its database; start again with the installer.
- Stop only a relay you started, by its recorded process id. Never kill by a name pattern.
