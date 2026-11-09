# Settings

Every `EMPORIA_*` setting is a row in the README's **Environment variables** table (`README.md`). That table is
the one list; this file does not repeat it. `tests/test_emporia.py` fails when the code reads a variable no table
row names, and when a row names one the code no longer reads.

## Adding or changing a setting

1. Read it in one place (`env_config.py`, `relay_config.py` or the module that owns it) and add its README row:
   default, who reads it (relay side or agent side) and what it does.
2. A relay-side setting that changes what a relay runs belongs to a relay module (`relay_modules.py`:
   `running`, `misconfigured`). `emporia relay config` edits relay settings one at a time, checked.
3. An agent-side setting is a person's choice: `emporia setup`, `emporia features` and the other `emporia`
   commands write it; an agent's tools do not change it.
4. A setting the test suite must pin goes in `tests/conftest.py`'s table.
5. A secret has a default of unset, is read from the environment or a secret source, is never logged, and is never
   given a default that points at real money (a facilitator URL stays blank, mainnet stays locked).

## Where settings live

- A relay: its `.env`. An agent: its Hermes profile's `.env`, handed to the MCP server through Hermes's config.
- `EMPORIA_NO_DOTENV=1` loads no `.env` (the suite sets it). `EMPORIA_DOTENV` names the one file to load.
- Credentials reach Emporia only through Hermes. Do not read a profile's `.env` from code a contributor runs.
