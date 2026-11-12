"""A profile's settings and keys, read and written through Hermes (catalog rule 11).

Hermes owns a profile's `.env`: it builds the profile's secret scope from it (and from
the secret sources the person set up), and it writes it under its own lock. Emporia's
runtime runs in its own interpreter, outside Hermes, so it asks Hermes:

* read:  only the names Emporia declares (`agent_env`: its settings, its keys, the vault
  variables of a linked key source, and any key linked as `env:NAME`), each with
  `agent.secret_scope.get_secret` inside the profile's scope, in Hermes's interpreter.
  The rest of the profile (provider keys, bot tokens) is never returned to Emporia;
* write: a name on the explicit PLAIN_SETTINGS list (known not to hold a secret) goes by
  `hermes -p <profile> config set -- NAME VALUE` / `config unset -- NAME`, Hermes's own command,
  with a minimal environment and no shell. EVERY OTHER name (keys, tokens, phrases, any URL that may
  carry a provider key, DSNs, sessions, key-source references, anything unknown) goes on stdin to
  Hermes's `save_env_value` / `remove_env_value`: a value in argv is readable by any process on the
  machine (`ps`, /proc), and keys are written lazily while the gateway runs. The one place Hermes's Python function is called directly instead of its command.

Nothing here opens a `.env` file. `available()` says whether Hermes is installed here; a
relay host or a checkout without Hermes has no route, and the caller decides (a runtime
installed from the catalog always has Hermes beside it, so it never falls back).

Hermes is found by EMPORIA_HERMES_BIN, else `hermes` on PATH, and
the interpreter of that install.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_TIMEOUT_S = 60
# What a child of this module is given: where to find things, the network, and the profile.
_ENV_NAMES = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TERM",
              "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
              "http_proxy", "https_proxy", "no_proxy", "HERMES_REAL_HOME")
# The settings known to hold no secret: only these go in argv (`hermes config set`). Anything not listed
# is treated as a secret and goes on stdin, so a new setting is safe until it is added here on purpose.
# Relay URLs (home, backup, visit) and the visit deny list are not listed: a URL may carry credentials, so they
# go on stdin.
PLAIN_SETTINGS = frozenset({
    "EMPORIA_AGENT_ID", "EMPORIA_DISPLAY_NAME", "EMPORIA_KEYS_DIR", "EMPORIA_PYTHON",
    "EMPORIA_DOTENV", "EMPORIA_PROFILE_ID", "EMPORIA_ALLOWED_USERS", "EMPORIA_KEY_STORAGE",
    "EMPORIA_ABOUT", "EMPORIA_CLIENT", "EMPORIA_CLIENT_VERSION", "EMPORIA_MODEL",
    "EMPORIA_FEATURES", "EMPORIA_WAKE", "EMPORIA_X_LINK", "EMPORIA_PLAYBOOK", "EMPORIA_GAMES_BY",
    "EMPORIA_PLAYBOOK_GAMES", "EMPORIA_PLAYBOOK_ANSWERS", "EMPORIA_PLAYBOOK_SELLS", "EMPORIA_PLAYBOOK_BUYS",
    "EMPORIA_PLAYBOOK_RUNS", "EMPORIA_PLAYBOOK_PLEDGES", "EMPORIA_PLAYBOOK_AUTONOMOUS_SPEND_CENTS_PER_DAY",
    "EMPORIA_PLAYBOOK_GAMES_PER_OPERATOR_PER_DAY", "EMPORIA_DECLARE_MODEL", "EMPORIA_THREAD_LIMIT",
    "EMPORIA_DAILY_TURNS", "EMPORIA_RELAY_TURN_TOOLS", "EMPORIA_UNTRACKED_TURNS", "EMPORIA_SIGN_LIMITS",
    "EMPORIA_X402_MAX_PAYMENT_CENTS", "EMPORIA_X402_MAX_DAILY_CENTS", "EMPORIA_PAYMENT_APPROVAL_CENTS",
    "EMPORIA_MAX_TOTAL_SPEND_CENTS", "EMPORIA_X402_PAY_NETWORKS", "EMPORIA_X402_NETWORKS", "EMPORIA_X402_ENABLED",
    "EMPORIA_X402_MAINNET", "EMPORIA_VISIT_POLICY",
    "EMPORIA_PUBLISH_VISITS",
})
# The settings whose value may point at another name (`env:NAME`): the only values followed on a read.
SOURCE_NAMES = ("EMPORIA_IDENTITY_KEY_SOURCE", "EMPORIA_ENCRYPTION_KEY_SOURCE", "EMPORIA_X402_EVM_KEY_SOURCE",
                "EMPORIA_X402_SOLANA_KEY_SOURCE", "EMPORIA_X402_PHRASE_SOURCE")
# {"names": [...], "sources": [...]} on stdin, the scope of the profile at argv[1]: each name's value by
# get_secret, and the names an `env:NAME` value of one of the `sources` points at. Only these are printed.
_READ = ("import json, re, sys\n"
         "from pathlib import Path\n"
         "from agent.secret_scope import build_profile_secret_scope, get_secret, reset_secret_scope, set_secret_scope\n"
         "want = json.load(sys.stdin)\n"
         "scope = build_profile_secret_scope(Path(sys.argv[1]))\n"
         "tok = set_secret_scope(scope, profile_home=sys.argv[1])\n"
         "out = {}\n"
         "for n in want['names']:\n"
         "    v = get_secret(n) or scope.get(n)\n"      # process-global names (API_SERVER_HOST) are os.environ's to Hermes
         "    if v not in (None, ''):\n"
         "        out[n] = v\n"
         "for n in want['sources']:\n"
         "    m = re.fullmatch(r'\\s*env:\\s*([A-Z][A-Z0-9_]*)\\s*', out.get(n, ''))\n"
         "    if m and m.group(1) not in out and get_secret(m.group(1)) not in (None, ''):\n"
         "        out[m.group(1)] = get_secret(m.group(1))\n"
         "reset_secret_scope(tok)\n"
         "sys.stdout.write(json.dumps(out))\n")
# {"names": {name: minimum length}} on stdin: whether Hermes holds each name (with at least that many
# characters), as booleans. A value is never printed.
_PRESENT = ("import json, sys\n"
            "from pathlib import Path\n"
            "from agent.secret_scope import build_profile_secret_scope, get_secret, reset_secret_scope, set_secret_scope\n"
            "want = json.load(sys.stdin)\n"
            "scope = build_profile_secret_scope(Path(sys.argv[1]))\n"
            "tok = set_secret_scope(scope, profile_home=sys.argv[1])\n"
            "out = {n: len(get_secret(n) or scope.get(n) or '') >= max(1, m) for n, m in want['names'].items()}\n"
            "reset_secret_scope(tok)\n"
            "sys.stdout.write(json.dumps(out))\n")
# A secret's write, its value on stdin (never argv, which `ps` shows).
_WRITE_SECRET = ("import json, sys\n"
                 "from hermes_cli.config import remove_env_value, save_env_value\n"
                 "d = json.load(sys.stdin)\n"
                 "for k, v in d.get('set', {}).items():\n"
                 "    save_env_value(k, v)\n"
                 "for k in d.get('unset', []):\n"
                 "    remove_env_value(k)\n")


class SettingsUnavailable(RuntimeError):
    """Hermes is not installed here, or could not answer; the message is safe to show."""


_cache: dict[str, dict[str, str]] = {}


def _hermes() -> tuple[Path, str] | None:
    """Hermes's own interpreter and import path, as its `hermes` launcher sets them. None when Hermes
    is not installed here."""
    launcher = os.getenv("EMPORIA_HERMES_BIN", "").strip() or shutil.which("hermes")
    if not launcher:
        return None
    root = Path(os.path.realpath(launcher)).parent.parent
    python = root / ".venv" / "bin" / "python"
    sites = sorted(root.glob(".venv/lib/python3*/site-packages"))
    if not python.is_file() or not sites:
        return None
    return python, os.pathsep.join([str(root), str(sites[-1])])


def available() -> bool:
    return _hermes() is not None


def _env(home: Path, python_path: str = "") -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_NAMES or k.startswith("LC_")}
    env["HERMES_HOME"] = str(home)
    if python_path:
        env["PYTHONPATH"] = python_path
    return env


def _run(code: str, home: Path, *args: str, stdin: str = "") -> str:
    found = _hermes()
    if found is None:
        raise SettingsUnavailable("Hermes is not installed here, so a profile's settings cannot be read "
                                  "or written through it")
    python, pythonpath = found
    try:
        out = subprocess.run([str(python), "-c", code, *args], input=stdin, env=_env(home, pythonpath),
                             capture_output=True, text=True, timeout=_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SettingsUnavailable(f"Hermes did not answer for {home.name}: {type(e).__name__}") from e
    if out.returncode != 0:
        last = (out.stderr.strip().splitlines() or ["?"])[-1][-300:]
        raise SettingsUnavailable(f"Hermes could not do it for {home.name}: {last}")
    return out.stdout


#: Not Emporia's, and not secrets: the api_server switches the doctor's hardening check reads.
READABLE_EXTRA = ("API_SERVER_HOST", "API_SERVER_ENABLED")


def present(home: Path, names: dict[str, int] | list[str]) -> dict[str, bool]:
    """Whether the profile at `home` holds each name (with at least the given number of characters, when
    `names` maps a name to one), asked of Hermes: booleans only, never a value. For a check of something
    Emporia has no business reading, a provider key."""
    home = Path(home).expanduser()
    want = {str(n): int(m) for n, m in (names.items() if isinstance(names, dict) else ((n, 1) for n in names))}
    data = json.loads(_run(_PRESENT, home, str(home), stdin=json.dumps({"names": want})) or "{}")
    return {n: bool(data.get(n)) for n in want}


def read_named(home: Path, names: list[str]) -> dict[str, str]:
    """Exactly `names`, as Hermes holds them: for the one thing a person asked Emporia to move or import by
    name (`wallet import --from-env NAME`, a secret in config.yaml being moved into the .env). Nothing else."""
    return _ask(Path(home).expanduser(), [str(n) for n in names])


def declared_names() -> list[str]:
    """Every name Emporia may read from a profile, without the vault variables (`_fetch` adds those of
    the vault CLI a linked key source names): what its runtime reads, its network-built names, its keys,
    and the gateway adapter's own settings."""
    from emporia import agent_env
    return sorted({*agent_env.known_with_relay(), *agent_env.network_names(), *agent_env.key_names(),
                   *agent_env.GATEWAY_ONLY, *agent_env.LITERAL_IN_ENTRY, *READABLE_EXTRA} - set(agent_env.VAULT_NAMES))


def _ask(home: Path, names: list[str]) -> dict[str, str]:
    want = {"names": names, "sources": list(SOURCE_NAMES)}
    data = json.loads(_run(_READ, home, str(home), stdin=json.dumps(want)) or "{}")
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _fetch(home: Path, names: list[str], vault: bool = True) -> dict[str, str]:
    """`names` as Hermes holds them, and (with `vault`) the variables of the vault CLI a linked key source
    runs, and no other vault's."""
    got = _ask(home, names)
    if vault:
        from emporia import agent_env
        fam = agent_env.vault_prefixes(got)
        if fam:
            got.update(_ask(home, [n for n in agent_env.VAULT_NAMES if n.startswith(fam)]))
    return got


def read(home: Path, *, fresh: bool = False) -> dict[str, str]:
    """The declared names Hermes holds for the profile at `home` (its .env, its secret sources),
    the key a source links by `env:NAME`, and the vault variables of a linked vault. Nothing else."""
    home = Path(home).expanduser()
    key = str(home.resolve()) if home.exists() else str(home)
    if fresh or key not in _cache:
        _cache[key] = _fetch(home, declared_names())
    return dict(_cache[key])


def _hermes_config(home: Path, *args: str) -> subprocess.CompletedProcess:
    """`hermes -p <profile> config ARGS`, always with a profile selector (`default` for the root home, as
    Hermes's own gateway does), so a sticky `active_profile` never redirects the write."""
    launcher = os.getenv("EMPORIA_HERMES_BIN", "").strip() or shutil.which("hermes")
    if not launcher:
        raise SettingsUnavailable("Hermes is not installed here, so a profile's settings cannot be written through it")
    profile = home.name if home.parent.name == "profiles" else "default"
    argv = [launcher, "-p", profile, "config", *args]
    try:
        return subprocess.run(argv, env=_env(home), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SettingsUnavailable(f"Hermes did not answer for {home.name}: {type(e).__name__}") from e


def write(home: Path, updates: dict[str, str] | None = None, unset=()) -> None:
    """Set `updates` and remove `unset` in the profile at `home`, through Hermes: a name on PLAIN_SETTINGS
    by `hermes -p <profile> config set|unset -- NAME [VALUE]`, every other name on stdin. A value the
    profile already holds is not written again (each `hermes` launch takes seconds). Then read back: Hermes
    skips a write its .env lock refuses, so what was asked is checked against what the profile now holds,
    and a setting that did not land fails the write by name only, never its value. Hermes keeps a value
    without line breaks and non-ASCII characters."""
    home = Path(home).expanduser()
    sets = {str(k): str(v) for k, v in (updates or {}).items()}
    unsets = [str(n) for n in unset]
    unsets += [k for k, v in sets.items() if v == ""]          # an empty value is no value: Hermes holds none
    sets = {k: v for k, v in sets.items() if v != ""}
    if not sets and not unsets:
        return
    cache_key = str(home.resolve()) if home.exists() else str(home)
    kept = lambda v: v.replace("\n", "").replace("\r", "").encode("ascii", "ignore").decode("ascii")  # noqa: E731
    try:
        held = _fetch(home, sorted({*sets, *unsets}), vault=False)
        sets = {k: v for k, v in sets.items() if held.get(k) != kept(v)}
        unsets = [n for n in unsets if n in held]
        plain = lambda n: n in PLAIN_SETTINGS  # noqa: E731
        secret = {"set": {k: v for k, v in sets.items() if not plain(k)}, "unset": [n for n in unsets if not plain(n)]}
        if secret["set"] or secret["unset"]:
            try:
                _run(_WRITE_SECRET, home, stdin=json.dumps(secret))
            except SettingsUnavailable:       # Hermes's own message may quote what it was given: say only the names
                raise SettingsUnavailable(f"Hermes could not write {', '.join(sorted({*secret['set'], *secret['unset']}))} "
                                          f"for {home.name}") from None
        for k, v in sets.items():
            if plain(k):
                r = _hermes_config(home, "set", "--", k, v)
                if r.returncode != 0:
                    raise SettingsUnavailable(f"Hermes could not set {k} for {home.name}")
        not_in_env, failed = set(), []
        for n in unsets:
            if not plain(n):
                continue
            r = _hermes_config(home, "unset", "--", n)
            if r.returncode == 0:
                continue
            if "not set" in f"{r.stdout}\n{r.stderr}".lower():       # Hermes: "Config key not set": not in the .env
                not_in_env.add(n)
            else:
                failed.append(n)
        if failed:
            raise SettingsUnavailable(f"Hermes could not unset {', '.join(sorted(failed))} for {home.name}")
        have = _fetch(home, sorted({*sets, *unsets}), vault=False)
        for n in sorted(not_in_env & set(have)):
            print(f"  note: {n} comes from one of Hermes's secret sources, not the profile's .env; it was left as it is.",
                  file=sys.stderr)
        bad = sorted([k for k, v in sets.items() if have.get(k) != kept(v)]
                     + [n for n in unsets if n in have and n not in not_in_env])
        if bad:
            raise SettingsUnavailable(f"Hermes could not do it for {home.name}: the profile did not take {', '.join(bad)}")
    finally:
        _cache.pop(cache_key, None)


def forget() -> None:
    """Drop what was read (tests; a long process after the person changed a setting)."""
    _cache.clear()
