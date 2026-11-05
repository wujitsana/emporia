"""A profile's settings and keys, read and written through Hermes (catalog rule 11).

Hermes owns a profile's `.env`: it builds the profile's secret scope from it (and from
the secret sources the person set up), and it writes it under its own lock. Emporia's
runtime runs in its own interpreter, outside Hermes, so it asks Hermes's interpreter to
do both, with Hermes's public functions:

* read:  ``agent.secret_scope.load_env_file(home / ".env")`` — what the profile's `.env`
  holds, parsed by Hermes, as JSON on a pipe;
* write: ``hermes_cli.config.save_env_value`` / ``remove_env_value`` under that profile's
  HERMES_HOME, the values on stdin (never in argv, which `ps` shows).

Nothing here opens a `.env` file. `available()` says whether Hermes is installed here; a
relay host or a checkout without Hermes has no route, and the caller decides (a runtime
installed from the catalog always has Hermes beside it, so it never falls back).

Hermes is found as `nous_token` finds it: EMPORIA_HERMES_BIN, else `hermes` on PATH, and
the interpreter of that install.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_TIMEOUT_S = 60
# What the profile's .env holds, parsed by Hermes (its own dotenv rules). Not the whole
# secret scope: a value a secret source supplies must never be copied into the .env by a
# rewrite built on what was read.
_READ = ("import json, sys\n"
         "from pathlib import Path\n"
         "from agent.secret_scope import load_env_file\n"
         "sys.stdout.write(json.dumps(load_env_file(Path(sys.argv[1]) / '.env')))\n")
# Then read back: Hermes skips a write its .env lock refuses (it prints why and returns), so
# what was asked is checked against what the file now holds, and a setting that did not land
# (a spending limit, a key) fails the write — by name only, never its value. Hermes keeps a
# value without line breaks and without non-ASCII characters, so that is what is compared.
_WRITE = ("import json, sys\n"
          "from agent.secret_scope import load_env_file\n"
          "from hermes_cli.config import get_env_path, remove_env_value, save_env_value\n"
          "d = json.load(sys.stdin)\n"
          "for k, v in d.get('set', {}).items():\n"
          "    save_env_value(k, v)\n"
          "for k in d.get('unset', []):\n"
          "    remove_env_value(k)\n"
          "have = load_env_file(get_env_path())\n"
          "kept = lambda v: v.replace('\\n', '').replace('\\r', '').encode('ascii', 'ignore').decode('ascii')\n"
          "bad = sorted([k for k, v in d.get('set', {}).items() if have.get(k) != kept(v)]\n"
          "             + [k for k in d.get('unset', []) if k in have])\n"
          "if bad:\n"
          "    sys.exit('the profile did not take ' + ', '.join(bad))\n")


class SettingsUnavailable(RuntimeError):
    """Hermes is not installed here, or could not answer; the message is safe to show."""


_cache: dict[str, dict[str, str]] = {}


def _hermes() -> tuple[Path, str] | None:
    from emporia import nous_token
    return nous_token._hermes_python()


def available() -> bool:
    return _hermes() is not None


def _run(code: str, home: Path, *args: str, stdin: str = "") -> str:
    found = _hermes()
    if found is None:
        raise SettingsUnavailable("Hermes is not installed here, so a profile's settings cannot be read "
                                  "or written through it")
    python, pythonpath = found
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
    env.update(PYTHONPATH=pythonpath, HERMES_HOME=str(home))
    try:
        out = subprocess.run([str(python), "-c", code, *args], input=stdin, env=env, capture_output=True,
                             text=True, timeout=_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SettingsUnavailable(f"Hermes did not answer for {home.name}: {type(e).__name__}") from e
    if out.returncode != 0:
        last = (out.stderr.strip().splitlines() or ["?"])[-1][-300:]
        raise SettingsUnavailable(f"Hermes could not do it for {home.name}: {last}")
    return out.stdout


def read(home: Path, *, fresh: bool = False) -> dict[str, str]:
    """Every value Hermes holds for the profile at `home` (its .env, its secret sources)."""
    home = Path(home).expanduser()
    key = str(home.resolve()) if home.exists() else str(home)
    if fresh or key not in _cache:
        data = json.loads(_run(_READ, home, str(home)) or "{}")
        _cache[key] = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    return dict(_cache[key])


def write(home: Path, updates: dict[str, str] | None = None, unset=()) -> None:
    """Set `updates` and remove `unset` in the profile at `home`, through Hermes."""
    home = Path(home).expanduser()
    payload = {"set": {str(k): str(v) for k, v in (updates or {}).items()}, "unset": [str(n) for n in unset]}
    if not payload["set"] and not payload["unset"]:
        return
    _run(_WRITE, home, stdin=json.dumps(payload))
    _cache.pop(str(home.resolve()) if home.exists() else str(home), None)


def forget() -> None:
    """Drop what was read (tests; a long process after the person changed a setting)."""
    _cache.clear()
