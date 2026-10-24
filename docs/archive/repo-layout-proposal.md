# Emporia Repo Layout

This document proposes cleanup steps that improve structure without losing important files.

## Current high-level layout

- `src/emporia/`: Python package and primary product code
- `tests/`: Python tests
- `dashboard/`: Vite/React frontend
- `relay/`: relay entrypoint wrapper and static relay assets
- `scripts/`: local development and demo helpers
- `installer/`: installation bootstrap code
- `docs/`: runbooks and design notes
- `logs/`: runtime output currently tracked in git
- `emporia-hackathon.mp4`: demo media currently tracked in git

## Safe cleanup policy

For the first pass:

- Do not delete tracked files.
- Do not move referenced entrypoints such as `relay/server.py` until all docs and scripts are updated.
- Prefer additive changes: ignore rules, layout documentation, and new destination directories.
- When moving tracked files later, use `git mv` so history is preserved.

## Recommended target layout

```text
emporia/
  src/emporia/           # product code
  tests/                 # automated tests
  apps/dashboard/        # frontend app
  relay/                 # compatibility wrapper until references are migrated
  scripts/               # thin dev helpers only
  installer/             # install/bootstrap path
  docs/                  # docs and runbooks
  assets/demo/           # screenshots, video, static demo media
  .local/ or var/        # logs, caches, local envs, build output
```

## Staged migration plan

1. Keep `relay/server.py` in place for now.
2. Move future runtime output from `logs/` to `.local/logs/` or `var/logs/`.
3. Stop tracking `logs/*.jsonl` in a separate cleanup commit once retention needs are confirmed.
4. Move `emporia-hackathon.mp4` to `assets/demo/` if the file should remain in git.
5. Move `dashboard/` to `apps/dashboard/` only after updating docs, scripts, and CI references.
6. Consolidate Python dev dependencies in `pyproject.toml` so there is one canonical contributor setup.

## Notes

- Adding ignore rules for `logs/*.jsonl` does not remove already tracked logs.
- If the tracked logs are important for demo reproducibility, archive them under `docs/fixtures/` or `assets/demo/` before untracking them.
- If the demo video is important for submissions, keep it in git or Git LFS, but place it under a directory that signals its purpose.
