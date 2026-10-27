"""Emporia installer — wire Hermes profiles for MCP auto-registration.

Usage (run from emporia repo root or inside a Hermes profile):

    # ── Hermes: wire the profile you are running as (agent self-install) ──
    python installer/install.py --install-profile
    python installer/install.py --install-profile --agent-id my_agent --relay-url http://127.0.0.1:8088
    # → syncs Python deps, patches config.yaml + MCP, symlinks skill, optional local seed
    # → then in Hermes: /reload-mcp

    # ── Full hackathon demo (multi-agent profiles + seed) ──
    python installer/install.py --bootstrap-test
    # → creates alpha/beta/stripe_escrow_bot profiles, starts relay, seeds

    # ── Relay + dashboard + seed only (no Hermes profile changes) ──
    python installer/install.py --local-demo
    # Same as: --build-dashboard --start-relay --seed-only

    python installer/install.py --seed-only          # re-populate demo content (starts relay if needed)
    python installer/install.py --start-relay        # uv sync + background uvicorn
    python installer/install.py --build-dashboard    # npm install + build:embedded → /ui/

    # ── Other ──
    python installer/install.py --create-profile scout_agent
    python installer/install.py --install-profile --dry-run

Low-level scripts (same behavior, for automation):
    scripts/local_relay.py          ensure_relay_running / start_relay
    scripts/seed_demo_relay.py      demo agents, games, rooms, Agoras, DMs, events

See README.md § Operations and docs/RUNBOOK.md.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Make emporia importable when run directly
_EMPORIA_DIR = Path(__file__).parent.parent
_src = str(_EMPORIA_DIR / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)


# P108: the one dotenv loader (profile .env, then the repo's), not a second
# copy. This one ran at import, so the test suite's import of this module
# loaded the operator's live .env into every test after it.
from emporia.config import load_profile_dotenv  # noqa: E402

load_profile_dotenv()

DEFAULT_RELAY_URL = os.getenv("EMPORIA_RELAY_URL", "http://localhost:8088")
MCP_TOOL_NAME = "emporia"

# Which .env keys are safe to inherit (never copy raw secrets by default)
_SAFE_ENV_KEYS = {
    "EMPORIA_RELAY_URL",
    "HERMES_AGENT_ID",
    "OPENROUTER_API_KEY",
    "STRIPE_PROFILE_ID",
    "STRIPE_API_VERSION",
    "EMPORIA_MPP_TEMPO_ENABLED",
    "EMPORIA_MAX_TOTAL_SPEND_CENTS",
    "EMPORIA_GUARDRAILS_MODE",
}
def _valid_stripe_profile_id(value: str | None) -> bool:
    if not value:
        return False
    value = value.strip()
    return value.startswith("profile_") or value.startswith("profile_test_")


_SECRET_ENV_KEYS = {
    "STRIPE_SECRET_KEY",
    "NVIDIA_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "NOUS_API_KEY",
    "EMPORIA_NOUS_JWT",
}

_PROVIDER_ENV_KEYS = ("STRIPE_SECRET_KEY", "STRIPE_PROFILE_ID")

# The relay's Stripe settings. An agent profile never needs them: its MCP
# server and platform plugin read no Stripe variable, and every Stripe call goes
# through the relay, which uses its own key. An agent pays with an SPT (via
# Link) or its x402 wallet. P107: create_profile() copied these into every
# agent profile it made or updated, which put the relay's secret key (live, on
# this deployment) in two more places with no use. Never written to an agent
# profile now, and stripped from one that already has them.
_RELAY_ONLY_ENV_KEYS = frozenset({
    "STRIPE_SECRET_KEY", "STRIPE_PROFILE_ID", "STRIPE_API_VERSION",
    "STRIPE_WEBHOOK_SECRET", "EMPORIA_MPP_TEMPO_ENABLED",
})


def _apply_guardrails_env(env_block: dict[str, str], *, disabled: bool = False) -> None:
    """The regex firewall: enforce, or off. P151 removed the NVIDIA-hosted
    semantic layer; its old setting is dropped from any profile set up again."""
    env_block.pop("EMPORIA_NEMO_GUARDRAILS_ENABLED", None)
    if disabled:
        env_block["EMPORIA_GUARDRAILS_MODE"] = "off"
        return
    env_block.setdefault("EMPORIA_GUARDRAILS_MODE", "enforce")


def _guardrails_env_lines(*, disabled: bool = False) -> list[str]:
    return ["EMPORIA_GUARDRAILS_MODE=off" if disabled else "EMPORIA_GUARDRAILS_MODE=enforce"]


def _find_auth_json(profile_dir: Path | None = None) -> Path | None:
    """Walk common locations to find auth.json.

    `hermes auth add nous` writes the live credential into the *active Hermes
    profile's own* auth.json (`<profile_dir>/auth.json`) — not a data-root or
    home-root file. That profile dir varies with `home_mode`: it can be
    `/opt/data/profiles/<name>`, `~/profiles/<name>`, or `~/.hermes/profiles/<name>`.
    Check the profile-scoped file first (most specific, most likely to be fresh),
    then fall back to the legacy data-root/home-root locations.
    """
    if profile_dir is None:
        profile_dir = find_profile_dir_from_cwd()
        if profile_dir is None:
            env_name = os.getenv("HERMES_PROFILE") or os.getenv("HERMES_AGENT_ID")
            if env_name:
                profile_dir = find_profile_dir_by_name(env_name)

    candidates = []
    if profile_dir:
        candidates.append(profile_dir / "auth.json")
    candidates += [
        Path(os.getenv("HERMES_DATA_DIR", "")) / "auth.json",
        Path("/opt/data/auth.json"),
        Path.home() / "auth.json",
    ]
    # P108: the newest file, not the first that exists. `hermes auth add nous`
    # run without `-p` writes the fresh login to the data root, leaving an
    # expired profile file first in line — and an expired token stopped the
    # search, so agents registered key_only despite a valid login on disk.
    found = [p for p in dict.fromkeys(candidates) if p.is_file() and p.stat().st_size > 10]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _try_refresh_nous(nous: dict, auth_path: Path) -> str | None:
    """Attempt silent OAuth2 refresh-token grant. Returns new access_token or None."""
    refresh_token = nous.get("refresh_token", "")
    client_id = nous.get("client_id", "hermes-cli")
    portal_base = nous.get("portal_base_url", "https://portal.nousresearch.com").rstrip("/")
    if not refresh_token:
        return None
    try:
        # OIDC discovery → token_endpoint
        try:
            with urllib.request.urlopen(
                f"{portal_base}/.well-known/openid-configuration", timeout=5
            ) as r:
                token_url = json.loads(r.read()).get("token_endpoint", "")
        except Exception:
            token_url = ""
        if not token_url:
            token_url = f"{portal_base}/api/oauth/token"

        payload = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }).encode()
        req = urllib.request.Request(
            token_url, data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())

        new_access = resp.get("access_token", "")
        if not new_access:
            return None

        # Write new tokens back to auth.json so Hermes stays in sync
        try:
            auth_data = json.loads(auth_path.read_text())
            n = auth_data.setdefault("providers", {}).setdefault("nous", {})
            n["access_token"] = new_access
            if "refresh_token" in resp:
                n["refresh_token"] = resp["refresh_token"]
            if "expires_in" in resp:
                exp = (datetime.datetime.now(datetime.timezone.utc)
                       + datetime.timedelta(seconds=int(resp["expires_in"])))
                n["expires_at"] = exp.isoformat()
            auth_path.write_text(json.dumps(auth_data, indent=2))
        except Exception:
            pass  # auth.json update is best-effort
        return new_access
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error_description", e.reason)
        except Exception:
            detail = e.reason
        print(f"  Refresh failed: {e.code} {detail}")
        return None
    except Exception as e:
        print(f"  Refresh failed: {type(e).__name__}: {e}")
        return None


def _nous_access_token_expired(token: str, leeway_sec: int = 30) -> bool:
    """True if JWT exp is in the past (signature not verified — expiry check only)."""
    try:
        import jwt

        payload = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
        exp = payload.get("exp")
        if exp is None:
            return False
        return datetime.datetime.now(datetime.timezone.utc).timestamp() >= float(exp) - leeway_sec
    except Exception:
        return True


def resolve_nous_token(
    explicit: str | None = None, profile_dir: Path | None = None
) -> str | None:
    """
    Return a valid Nous JWT.

    Priority: --nous-token arg → non-expired EMPORIA_NOUS_JWT env → auth.json (with
    silent refresh if expired). Returns None if no token is available and
    prints a clear message so the caller can warn about key_only mode.

    `profile_dir`: the active Hermes profile dir, if already known by the
    caller (checked first by `_find_auth_json` — see its docstring for why).
    """
    # 1. Explicit CLI arg
    if explicit:
        return explicit

    # 2. Env var only if not expired (stale profile .env must not block refresh)
    env_tok = os.getenv("EMPORIA_NOUS_JWT", "").strip()
    if env_tok and not _nous_access_token_expired(env_tok):
        return env_tok

    # 3. auth.json
    auth_path = _find_auth_json(profile_dir)
    if not auth_path:
        return None
    try:
        auth_data = json.loads(auth_path.read_text())
        nous = auth_data.get("providers", {}).get("nous", {})
        access = nous.get("access_token", "")
        if not access:
            return None

        expires_at = nous.get("expires_at", "")
        now = datetime.datetime.now(datetime.timezone.utc)
        if expires_at:
            try:
                exp = datetime.datetime.fromisoformat(expires_at)
                if now < exp - datetime.timedelta(seconds=30):
                    return access  # still valid
            except Exception:
                pass

        # Expired — try silent refresh
        print("  Nous token expired — attempting silent refresh…")
        refreshed = _try_refresh_nous(nous, auth_path)
        if refreshed:
            print("  Nous token refreshed.")
            return refreshed

        print("  Nous token expired and silent refresh failed (refresh_token itself may")
        print("  be expired/revoked — this requires a human to re-approve, not something")
        print("  this installer can do unattended). To restore nous_verified trust:")
        print("    hermes auth add nous --type oauth --no-browser --manual-paste")
        print("  This prints a URL + device code; open it, approve, then re-run the")
        print("  installer (or `scripts/seed_demo_relay.py`) — no extra flags needed,")
        print("  the fresh token will be picked up automatically.")
        return None
    except Exception:
        return None


def _hermes_profile_exists(name: str) -> bool:
    try:
        r = subprocess.run(
            ["hermes", "profile", "show", name],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _hermes_profile_create(name: str) -> None:
    """Create the Hermes profile skeleton (idempotent via existence check).

    Uses --no-skills: Emporia profiles should carry only the skills they
    actually need (emporia + payment skills), not the full bundled set —
    see _install_payment_skill_links() for what gets provisioned instead.
    """
    if _hermes_profile_exists(name):
        return
    try:
        subprocess.run(
            ["hermes", "profile", "create", name, "--no-skills"],
            check=True, capture_output=True, timeout=15,
        )
        print(f"  Created Hermes profile '{name}' (--no-skills)")
    except Exception as e:
        print(f"  Warning: hermes profile create failed: {e} — continuing")


# ─────────────────────────────────────────────────────────────────────────────
# Profile discovery
# ─────────────────────────────────────────────────────────────────────────────

def find_profile_dir_from_cwd() -> Path | None:
    """Walk up from CWD looking for a Hermes config.yaml (up to 4 levels)."""
    current = Path.cwd().resolve()
    for _ in range(4):
        if (current / "config.yaml").exists():
            return current
        current = current.parent
    return None


def find_profile_dir_by_name(name: str) -> Path | None:
    """Search known profile base dirs for a named profile."""
    for base in _profile_base_dirs():
        p = base / name
        if (p / "config.yaml").exists():
            return p
    return None


def _profile_base_dirs() -> list[Path]:
    """Ordered list of directories that may contain profiles."""
    candidates = [
        Path("/opt/data/profiles"),
        Path.home() / "profiles",
        Path.home() / ".hermes" / "profiles",
        Path.cwd(),
    ]
    return [p for p in candidates if p.exists()]


def detect_profiles_base(current_profile_dir: Path) -> Path:
    """Return the directory that contains the current profile (its parent)."""
    return current_profile_dir.parent


def _venv_python() -> str:
    """Return path to the emporia venv python if it exists, else sys.executable."""
    p = _EMPORIA_DIR / ".venv" / "bin" / "python"
    if p.exists():
        return str(p)
    return sys.executable


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────

_YAML_CACHE: dict[tuple[str, int], dict] = {}


def load_yaml(path: Path) -> dict:
    """Parsed YAML. P174: cached by path and modification time — doctor and the
    setup overview read the same large config.yaml many times. Returns a copy,
    so a caller that edits it and saves does not change the cache."""
    import copy
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        key = None
    if key in _YAML_CACHE:
        return copy.deepcopy(_YAML_CACHE[key])
    try:
        import yaml
        data = yaml.safe_load(path.read_text()) or {}
        if key:
            _YAML_CACHE[key] = data
        return copy.deepcopy(data)
    except ImportError:
        import json
        return json.loads(path.read_text()) if path.suffix == ".json" else {}


def _write_private(path: Path, text: str) -> None:
    """Write `path` owner-only (P201): 0600 from the instant it exists, and an
    existing file is narrowed too. write_text() made a new .env 0644."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def save_yaml(path: Path, data: dict) -> None:
    try:
        import yaml
        _write_private(path, yaml.dump(data, default_flow_style=False, sort_keys=False))
    except ImportError:
        _write_private(path, json.dumps(data, indent=2))


# P201: a profile's secrets live in its .env and nowhere else. Hermes's file
# tools refuse to open a .env; config.yaml they read like any file, and older
# installs left it 0644. Hermes never reads a top-level `env:` block, so a
# secret there was only a copy waiting to be read.
_SECRET_MARKERS = ("SECRET", "TOKEN", "JWT", "PASSWORD", "PHRASE", "PRIVATE")


def is_secret_name(name: str) -> bool:
    n = name.upper()
    return n != "EMPORIA_PUBLIC_KEY" and (n.endswith("_KEY") or any(m in n for m in _SECRET_MARKERS))


def secure_profile(profile_dir: Path, dry_run: bool = False) -> list[str]:
    """Move every secret out of the profile's config.yaml into its .env, and
    make both owner-only. Top-level `env:` / `environment:` entries are dropped
    (Hermes reads neither); an MCP server's entry becomes `${NAME}`, which
    Hermes fills from this profile's .env. Where the .env already holds a
    different value, the .env's is kept: it is the one Hermes uses. Returns the
    names moved, never values."""
    config_path, env_path = profile_dir / "config.yaml", profile_dir / ".env"
    if not config_path.exists():
        return []
    config = load_yaml(config_path)
    have = harvest_env(profile_dir)
    to_env: dict[str, str] = {}
    moved: list[str] = []

    def take(block: dict, keep_ref: bool) -> None:
        for k in [k for k in block if is_secret_name(str(k))]:
            value = _yaml_env_scalar(block[k])
            if value.startswith("${"):
                continue
            if value and not have.get(k):
                to_env.setdefault(str(k), value)
            if keep_ref:
                block[k] = f"${{{k}}}"
            else:
                del block[k]
            moved.append(str(k))

    for name in ("env", "environment"):
        if isinstance(config.get(name), dict):
            take(config[name], keep_ref=False)
    for server in (config.get("mcp_servers") or {}).values():
        if isinstance(server, dict) and isinstance(server.get("env"), dict):
            take(server["env"], keep_ref=True)
    if dry_run:
        return sorted(set(moved))
    if to_env:
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        _write_private(env_path, "\n".join(lines + [f"{k}={v}" for k, v in to_env.items()]) + "\n")
    if moved:
        save_yaml(config_path, config)
    for path in (config_path, env_path):
        if path.exists():
            os.chmod(path, 0o600)
    return sorted(set(moved))


def is_multiplexed(profile_dir: Path) -> bool:
    """Whether this profile is served by the root's gateway, in one process with
    other profiles. Then Hermes gives it only its own .env's credentials, never
    the process environment (docker-compose). Only `gateway.standalone: true`
    keeps a named profile's own gateway."""
    if profile_dir.parent.name != "profiles":
        return False   # the root (default) profile: the container's environment is its own
    gateway = load_yaml(profile_dir / "config.yaml").get("gateway") or {}
    return gateway.get("standalone") is not True


def _ask_provider_key(name: str, where: str, must_copy: bool) -> str | None:
    """In a terminal: the standard key, another, or none. None means skip."""
    import getpass
    note = (" It is copied into this agent's .env: each profile here gets only its own .env's keys."
            if must_copy else "")
    print(f"\n  The model needs {name}. Found in {where}.{note}")
    options = [("standard", "Use that standard key"), ("other", "Enter another key for this agent"),
               ("skip", "Skip — add it to its .env later")]
    try:   # P212: the arrow-key menus when there is a terminal for them
        from emporia import menus
        picked = menus.select("  Which key?", [t for _, t in options]) if menus.available() else None
        answer = None if picked is None else "skip" if picked is menus.BACK else options[picked][0]
    except ImportError:
        answer = None
    if answer is None:
        raw = input("  Use that standard key? [Y/n, or o for another] ").strip().lower()
        answer = "standard" if raw in ("", "y", "yes") else "other" if raw.startswith("o") else "skip"
    if answer == "standard":
        return "standard"
    if answer == "other":
        other = getpass.getpass(f"  {name} for this agent (hidden): ").strip()
        return other or None
    return None


def _provider_key_line(model_block: dict | None, *sources: dict[str, str],
                       profile_dir: Path | None = None, labels: list[str] | None = None) -> str | None:
    """`KEY=value` for the model's provider, to go in the new profile's .env.

    From the first .env-type source that has it; else from this process's
    environment (a key docker-compose passes the container). That one is copied
    only when the profile cannot read the environment itself (multiplexed, P201);
    in a terminal the person chooses the standard key, another, or none."""
    name = _PROVIDER_KEYS.get(str((model_block or {}).get("provider") or "").lower())
    if not name:
        return None
    if profile_dir is not None and harvest_env(profile_dir).get(name):
        return f"{name}={harvest_env(profile_dir)[name]}"
    # P216: say which file — "another profile's .env" was wrong when it was the main one.
    value, where = next(((src[name], (labels or [])[i] if i < len(labels or []) else "another .env")
                         for i, src in enumerate(sources) if src.get(name)), ("", ""))
    if not value and os.environ.get(name, "").strip():
        value = os.environ[name].strip()
        where = "the container's environment (docker-compose)" if _in_container() else "this shell's environment"
    if not value:
        print(f"  WARNING: the model's provider needs {name}, and none was found to copy")
        return None
    from_env = where.endswith("environment") or "environment (" in where
    must_copy = profile_dir is None or is_multiplexed(profile_dir)
    if from_env and not must_copy:
        print(f"  {name}: this profile runs its own gateway and reads it from {where} — nothing copied")
        return None
    if sys.stdin.isatty():
        choice = _ask_provider_key(name, where, must_copy)
        if choice is None:
            print(f"  {name} not set — add it to {profile_dir / '.env' if profile_dir else 'the .env'} before it runs")
            return None
        if choice != "standard":
            return f"{name}={choice}"
    else:
        print(f"  {name}: copied from {where}" + (" (each profile here reads only its own .env)" if from_env else ""))
    return f"{name}={value}"


def hermes_home_of(profile_dir: Path) -> Path:
    """The Hermes home a profile lives under (…/profiles/<name> → …)."""
    return profile_dir.parent.parent if profile_dir.parent.name == "profiles" else profile_dir


def _in_container() -> bool:
    return any(Path(p).exists() for p in ("/.dockerenv", "/run/.containerenv"))


def harvest_env(profile_dir: Path) -> dict[str, str]:
    """Read .env from profile dir."""
    env_file = profile_dir / ".env"
    result: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _yaml_env_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def harvest_config_environment(profile_dir: Path) -> dict[str, str]:
    """Hermes config.yaml `environment:` / top-level `env:` (non-empty values only)."""
    path = profile_dir / "config.yaml"
    if not path.exists():
        return {}
    data = load_yaml(path)
    out: dict[str, str] = {}
    for block_name in ("environment", "env"):
        raw = data.get(block_name) or {}
        if not isinstance(raw, dict):
            continue
        for k, v in raw.items():
            s = _yaml_env_scalar(v)
            if s:
                out[str(k)] = s
    return out


def collect_upstream_env(profile_dir: Path) -> dict[str, str]:
    """Walk parent directories for .env keys; closer ancestors override farther ones."""
    profile_dir = profile_dir.resolve()
    merged: dict[str, str] = {}
    chain: list[Path] = []
    current = profile_dir.parent
    for _ in range(14):
        if current == current.parent:
            break
        chain.append(current)
        current = current.parent
    for d in reversed(chain):
        merged.update({k: v for k, v in harvest_env(d).items() if v})
    merged.update(harvest_config_environment(profile_dir))
    emporia_root = profile_dir / "emporia"
    if emporia_root.is_dir():
        merged.update({k: v for k, v in harvest_env(emporia_root).items() if v})
    for k in _PROVIDER_ENV_KEYS:
        if os.environ.get(k) and k not in merged:
            merged[k] = os.environ[k].strip()
    return merged


def _prompt_optional_secret(label: str, hint: str) -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        import getpass

        val = getpass.getpass(f"  {label} ({hint}; Enter to skip): ")
        val = val.strip()
        return val or None
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _prompt_optional_line(label: str, hint: str) -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        val = input(f"  {label} ({hint}; Enter to skip): ").strip()
        return val or None
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def resolve_provider_secrets(
    profile_dir: Path,
    *,
    stripe_secret_key: str | None = None,
    stripe_profile_id: str | None = None,
    interactive: bool = True,
    dry_run: bool = False,
) -> tuple[str | None, str | None]:
    """Resolve the relay's Stripe settings from CLI, profile .env, parent .env, config, then prompt."""
    local = harvest_env(profile_dir)
    cfg_env = harvest_config_environment(profile_dir)
    for k in _PROVIDER_ENV_KEYS:
        if not local.get(k) and cfg_env.get(k):
            local[k] = cfg_env[k]
    upstream = collect_upstream_env(profile_dir)

    def pick(key: str, cli: str | None) -> str | None:
        if cli and cli.strip():
            return cli.strip()
        if local.get(key):
            return local[key]
        if upstream.get(key):
            return upstream[key]
        return None

    stripe = pick("STRIPE_SECRET_KEY", stripe_secret_key)
    profile_cli = stripe_profile_id
    if (profile_cli or "").strip().lower() == "auto":
        profile_cli = None
    profile = pick("STRIPE_PROFILE_ID", profile_cli)

    if stripe and not profile:
        api_ver = (
            local.get("STRIPE_API_VERSION")
            or upstream.get("STRIPE_API_VERSION")
            or os.getenv("STRIPE_API_VERSION", "2026-04-22.preview")
        )
        try:
            from emporia.stripe_profile_discovery import resolve_stripe_profile_id

            resolved, note = resolve_stripe_profile_id(
                stripe, profile_dir, api_version=api_ver
            )
            if resolved:
                profile = resolved
                print(f"  STRIPE_PROFILE_ID: auto-discovered ({note})")
        except Exception as e:
            print(f"  STRIPE_PROFILE_ID: auto-discover skipped ({type(e).__name__})")

    if interactive and not dry_run:
        if not stripe:
            print("  STRIPE_SECRET_KEY: not found — paid Stripe sessions stay off.")
            stripe = _prompt_optional_secret(
                "STRIPE_SECRET_KEY",
                "enables Stripe escrow / MPP on the relay",
            )
        if stripe and not profile:
            print("  STRIPE_PROFILE_ID: not found — Stripe PI works; MPP seller mode needs profile_…")
            profile = _prompt_optional_line(
                "STRIPE_PROFILE_ID",
                "Stripe Machine Payments profile id",
            )

    if profile and not _valid_stripe_profile_id(profile):
        print(f"  WARNING: ignoring invalid STRIPE_PROFILE_ID={profile!r}")
        profile = None

    return stripe, profile


def _print_provider_status(
    stripe_secret_key: str | None,
    stripe_profile_id: str | None,
) -> None:
    if stripe_secret_key:
        if stripe_profile_id:
            print("  Stripe relay payments: on (secret key + STRIPE_PROFILE_ID — MPP seller ready)")
        else:
            print("  Stripe relay payments: on (secret key; PI only — no STRIPE_PROFILE_ID for MPP seller)")
            try:
                from emporia.stripe_profile_discovery import stripe_mpp_admin_notice

                admin = stripe_mpp_admin_notice(stripe_secret_key, profile_ready=False)
                if admin:
                    print(f"  WARNING: {admin}")
            except Exception:
                pass
    else:
        print("  Stripe relay payments: off (no STRIPE_SECRET_KEY)")


def _model_block(config: dict) -> dict | None:
    return config.get("model")


# P117: the API key each model provider reads. A profile is created bare
# (`hermes profile create --no-skills`, which Hermes will not combine with
# --clone-from) and gets the model plus exactly this key — the minimum it needs
# to answer. Without it Hermes greets the user with "No inference provider is
# configured". Nous needs no key: it signs in (`hermes -p <name> auth add nous`).
_PROVIDER_KEYS = {
    "openrouter": "OPENROUTER_API_KEY", "nvidia": "NVIDIA_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
}


# ─────────────────────────────────────────────────────────────────────────────
# MCP + platform entry builders
# ─────────────────────────────────────────────────────────────────────────────

def _profile_runtime_env(profile_dir: Path) -> dict[str, str]:
    """The key directory and database for this profile — the one answer every
    Emporia process for it uses: the MCP server (its config.yaml env), and the
    relay, the Hermes bridge and the CLI tools (the profile .env).

    P102: these used to disagree. The MCP server got the profile-home paths, but
    the .env got `~/.hermes/...` (wherever HOME pointed), and EMPORIA_KEYS_DIR
    only when the directory already existed. The relay, bridge and CLI then used
    a second key directory, whose copies of the profile keys turned out to be
    different keys from the registered ones — an agent signing as itself with a
    key its relay did not know.
    """
    home = profile_dir / "home" / ".hermes"
    return {"EMPORIA_KEYS_DIR": str(home / "keys"), "EMPORIA_DB_PATH": str(home / "emporia.sqlite3")}


def _ensure_keys_dir(runtime_env: dict[str, str], dry_run: bool) -> None:
    """Create the profile's key directory owner-only, so the first key written
    there is never in a world-readable directory."""
    if not dry_run:
        Path(runtime_env["EMPORIA_KEYS_DIR"]).mkdir(mode=0o700, parents=True, exist_ok=True)


def _mcp_entry(relay_url: str, agent_id: str = "", display_name: str = "", profile_dir: Path | None = None) -> dict[str, Any]:
    env: dict[str, str] = {
        "EMPORIA_RELAY_URL": relay_url,
        "EMPORIA_MCP_TRANSPORT": "stdio",
    }
    if agent_id:
        env["EMPORIA_AGENT_ID"] = agent_id
    if display_name:
        env["EMPORIA_DISPLAY_NAME"] = display_name
    if profile_dir:
        env.update(_profile_runtime_env(profile_dir))
        # P109: this profile's own .env, not the one beside the code.
        env["EMPORIA_DOTENV"] = str(profile_dir / ".env")
    # P114: where Hermes is, so the MCP server can ask it for a current Nous
    # token whatever PATH Hermes gives it (it lives in ~/.local/bin here).
    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        env["EMPORIA_HERMES_BIN"] = hermes_bin
    return {
        "command": _venv_python(),
        "args": ["-m", "emporia.mcp_server"],
        "env": env,
    }


# P63: `_platform_entry()` used to be written into `config.yaml` as
# `platforms: - platform: emporia`. It never did anything, and it could not:
#
#   * Hermes discovers plugins through the `hermes_agent.plugins` entry point
#     (`hermes_cli/plugins.py`), and this package declares none — `hermes
#     plugins list` shows no emporia.
#   * Nothing in the Hermes tree mentions emporia, so there is no built-in
#     adapter branch either.
#   * With neither, the gateway's registry lookup misses and it logs
#     "No adapter for 'emporia' -- is the plugin installed?".
#
# `register_platform()` is also the *gateway/desktop* half of Hermes's plugin
# surface — the same family as `register_dashboard_auth_provider` and the
# Telegram/Slack handlers — not the agent-running half (`register_tool`,
# `register_hook`, `register_middleware`). Emporia's agent integration is the
# MCP server, which `hermes -p <profile> mcp list` reports as enabled with all
# tools, and that has been the working path all along.
#
# Writing an inert entry made the config claim an integration that was not
# there, so it is gone. P92 built the adapter for real — a `plugin.yaml`, a
# `register(ctx)` and a `BasePlatformAdapter` subclass in `hermes_plugin/emporia/`
# — and `install_platform_plugin()` below places and enables it.


# ─────────────────────────────────────────────────────────────────────────────
# Keypair generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_keypair(profile_id: str, dry_run: bool = False, *,
                     keys_dir: Path | None = None) -> tuple[str, str]:
    """Generate or load the Ed25519 keypair, in `keys_dir`. Returns (pub_hex, key_path_str).

    P102: this used `Path.home()/.hermes/keys` and identity's default KEY_DIR,
    while the profile's MCP server signs from `<profile>/home/.hermes/keys`. The
    installer generated one key, the MCP server generated and registered another,
    and the installer's copy sat beside the real one claiming the same name.
    `keys_dir` is the profile's key directory, the one every process now uses.
    """
    from emporia import identity

    if keys_dir is not None:
        identity.KEY_DIR = Path(keys_dir)
    key_path = identity.KEY_DIR / f"{profile_id}.priv"
    if dry_run:
        print(f"  [dry-run] Would generate keypair at {key_path}")
        return ("dryrun_pubkey_hex_placeholder", str(key_path))
    _, pub_bytes = identity.generate_or_load_keypair(profile_id)
    pub_hex = pub_bytes.hex()
    if key_path.exists():
        print(f"  keypair: {key_path} (existing)")
    else:
        print(f"  + Ed25519 keypair: {key_path}")
    return (pub_hex, str(key_path))


# ─────────────────────────────────────────────────────────────────────────────
# --install-profile
# ─────────────────────────────────────────────────────────────────────────────

def write_dashboard_env(
    relay_url: str,
    agent_id: str,
    dry_run: bool = False,
) -> None:
    """Write VITE_AGENT_ID + VITE_RELAY_URL to dashboard/.env.local so dev builds
    pick up the owner agent without a code change. Also used by `npm run build`."""
    dashboard_dir = _EMPORIA_DIR / "dashboard"
    if not dashboard_dir.exists():
        return
    env_local = dashboard_dir / ".env.local"
    lines = [
        f"VITE_AGENT_ID={agent_id}",
        f"VITE_RELAY_URL={relay_url}",
    ]
    content = "\n".join(lines) + "\n"
    if dry_run:
        print(f"  [dry-run] Would write {env_local}: VITE_AGENT_ID={agent_id}")
        return
    env_local.write_text(content)
    print(f"  + dashboard/.env.local  (VITE_AGENT_ID={agent_id})")
    print("    Rebuild dashboard to bake in: cd dashboard && npm run build")


def _warn_if_a2a_enabled(config: dict) -> None:
    """Detection only — never sets, clears, or defaults any A2A_* key or
    gateway.platforms.a2a entry. A2A posture is the operator's decision; the
    installer's only job is to make sure they know it interacts with what it's
    about to wire up.

    If Hermes's bundled A2A platform is enabled on this profile, Emporia's MCP
    tools (registered as toolset mcp-emporia) are advertised on the agent's A2A
    card by default (A2A_ADVERTISED_TOOLSETS unset => all toolsets), so an
    authenticated A2A peer can drive payment-capable Emporia tools through this
    agent. See docs/archive/roadmap-hackathon-era.md's Interop section.
    """
    a2a_enabled = (
        config.get("gateway", {}).get("platforms", {}).get("a2a", {}).get("enabled")
    )
    if a2a_enabled:
        print(
            "  WARNING: this profile has A2A inbound enabled "
            "(gateway.platforms.a2a.enabled). Emporia's MCP tools register as "
            "toolset 'mcp-emporia' and are advertised on this agent's A2A card "
            "by default — authenticated A2A peers can drive payment-capable "
            "Emporia tools through this agent. Scope it with A2A_PEER_TOKENS / "
            "A2A_TRUSTED_PEERS / A2A_ADVERTISED_TOOLSETS if that's not intended."
        )


def install_into_profile(
    profile_dir: Path,
    relay_url: str,
    agent_id: str,
    stripe_secret_key: str | None = None,
    stripe_profile_id: str | None = None,
    stripe_api_version: str | None = None,
    max_budget_cents: int | None = None,
    nous_token: str | None = None,
    no_guardrails: bool = False,
    dry_run: bool = False,
) -> bool:
    config_path = profile_dir / "config.yaml"
    if not config_path.exists():
        print(f"  ERROR: no config.yaml at {config_path}")
        return False

    # Generate keypair first (idempotent)
    pub_hex, _ = generate_keypair(
        agent_id, dry_run=dry_run,
        keys_dir=Path(_profile_runtime_env(profile_dir)["EMPORIA_KEYS_DIR"]),
    )

    config = load_yaml(config_path)
    changed = False

    _warn_if_a2a_enabled(config)

    # MCP entry
    mcp_servers = config.setdefault("mcp_servers", {})
    runtime_env = _profile_runtime_env(profile_dir)
    if MCP_TOOL_NAME not in mcp_servers:
        mcp_servers[MCP_TOOL_NAME] = _mcp_entry(relay_url, agent_id=agent_id, profile_dir=profile_dir)
        changed = True
        print(f"  + mcp_servers.{MCP_TOOL_NAME}  (agent_id={agent_id!r})")
    else:
        existing_env = dict(mcp_servers[MCP_TOOL_NAME].get("env", {}))
        if agent_id and existing_env.get("EMPORIA_AGENT_ID") != agent_id:
            existing_env["EMPORIA_AGENT_ID"] = agent_id
            changed = True
            print(f"  ~ mcp_servers.{MCP_TOOL_NAME}.env.EMPORIA_AGENT_ID = {agent_id!r}")
        for k, v in runtime_env.items():
            if existing_env.get(k) != v:
                existing_env[k] = v
                changed = True
                print(f"  ~ mcp_servers.{MCP_TOOL_NAME}.env.{k}")
        mcp_servers[MCP_TOOL_NAME]["env"] = existing_env

    # P63: no platform entry is written — see _platform_entry's removal note.
    # An existing one from an older install is left alone rather than deleted:
    # removing keys from an operator's config is not the installer's call, and
    # the entry is inert either way.

    # Env block
    # P201: secrets (the Stripe key, the Nous token) go only to the .env below.
    env_block = config.setdefault("env", {})
    if stripe_profile_id:
        if not _valid_stripe_profile_id(stripe_profile_id):
            print(f"  WARNING: ignoring invalid STRIPE_PROFILE_ID={stripe_profile_id!r} (expected profile_... or profile_test_...)")
            stripe_profile_id = None
        elif env_block.get("STRIPE_PROFILE_ID") != stripe_profile_id:
            env_block["STRIPE_PROFILE_ID"] = stripe_profile_id
            changed = True
            print("  + env.STRIPE_PROFILE_ID")
    elif "STRIPE_PROFILE_ID" in env_block and not stripe_secret_key:
        env_block.pop("STRIPE_PROFILE_ID", None)
        changed = True
    if stripe_api_version and env_block.get("STRIPE_API_VERSION") != stripe_api_version:
        env_block["STRIPE_API_VERSION"] = stripe_api_version
        changed = True
        print("  + env.STRIPE_API_VERSION")
    if env_block.pop("EMPORIA_MPP_TEMPO_ENABLED", None) is not None:  # P152: no longer a setting
        changed = True
    if max_budget_cents is not None:
        if max_budget_cents > 0:
            if env_block.get("EMPORIA_MAX_TOTAL_SPEND_CENTS") != str(max_budget_cents):
                env_block["EMPORIA_MAX_TOTAL_SPEND_CENTS"] = str(max_budget_cents)
                changed = True
                print(f"  + total spend limit = {max_budget_cents} cents")
        elif "EMPORIA_MAX_TOTAL_SPEND_CENTS" in env_block:
            env_block.pop("EMPORIA_MAX_TOTAL_SPEND_CENTS", None)
            changed = True
            print("  ~ removed total spend limit (unlimited)")
    if nous_token:
        print("  + EMPORIA_NOUS_JWT in .env  (Nous identity verified)")
    if no_guardrails:
        _apply_guardrails_env(env_block, disabled=True)
        changed = True
        print("  + guardrails disabled (EMPORIA_GUARDRAILS_MODE=off)")
    else:
        before = dict(env_block)
        _apply_guardrails_env(env_block, disabled=False)
        if env_block != before:
            changed = True
            print("  + guardrails: regex firewall, enforce")

    if changed:
        if not dry_run:
            save_yaml(config_path, config)
            print(f"  Saved: {config_path}")
        else:
            print(f"  [dry-run] Would update: {config_path}")
    else:
        print("  Already up to date — no changes needed")

    # .env file: ensure EMPORIA_AGENT_ID and optionally Nous JWT are present
    env_file = profile_dir / ".env"
    env_lines: list[str] = []
    if env_file.exists():
        env_lines = env_file.read_text().splitlines()

    def _set_env_line(lines: list[str], key: str, value: str) -> tuple[list[str], bool]:
        for i, ln in enumerate(lines):
            if ln.startswith(f"{key}=") or ln.startswith(f"{key} ="):
                if lines[i] == f"{key}={value}":
                    return lines, False
                lines[i] = f"{key}={value}"
                return lines, True
        lines.append(f"{key}={value}")
        return lines, True

    # P102: the same paths the MCP entry got above, so every process agrees.
    _ensure_keys_dir(runtime_env, dry_run)
    db_path = runtime_env["EMPORIA_DB_PATH"]
    env_lines, c1 = _set_env_line(env_lines, "EMPORIA_AGENT_ID", agent_id)
    env_lines, c2 = _set_env_line(env_lines, "EMPORIA_RELAY_URL", relay_url)
    env_lines, c4 = _set_env_line(env_lines, "EMPORIA_DB_PATH", db_path)
    env_lines, c5 = _set_env_line(env_lines, "EMPORIA_KEYS_DIR", runtime_env["EMPORIA_KEYS_DIR"])
    env_lines, _ = _set_env_line(env_lines, "EMPORIA_DOTENV", str(profile_dir / ".env"))  # P109
    if _is_local_relay(relay_url):
        # Single-operator co-located deployment — the case EMPORIA_TRUST_LOCAL_HEADER_AUTH
        # exists for. Without it, the embedded dashboard's baked-in VITE_AGENT_ID header auth
        # (write_dashboard_env(), below) gets refused by the relay's default-off trust set.
        # Never written for a remote relay_url — that's someone else's relay to configure.
        env_lines, _ = _set_env_line(env_lines, "EMPORIA_TRUST_LOCAL_HEADER_AUTH", "1")
    env_lines, _ = _set_env_line(env_lines, "STRIPE_API_VERSION", stripe_api_version or os.getenv("STRIPE_API_VERSION", "2026-04-22.preview"))
    if stripe_secret_key:
        env_lines, _ = _set_env_line(env_lines, "STRIPE_SECRET_KEY", stripe_secret_key)
    else:
        env_lines = [ln for ln in env_lines if not ln.startswith("STRIPE_SECRET_KEY=")]
    if stripe_profile_id:
        env_lines, _ = _set_env_line(env_lines, "STRIPE_PROFILE_ID", stripe_profile_id)
    elif not stripe_secret_key:
        env_lines = [ln for ln in env_lines if not ln.startswith("STRIPE_PROFILE_ID=")]
    env_lines = [ln for ln in env_lines if not ln.startswith("EMPORIA_NEMO_GUARDRAILS_ENABLED=")]
    env_lines = [ln for ln in env_lines if not ln.startswith("EMPORIA_MPP_TEMPO_ENABLED=")]
    if max_budget_cents is not None:
        if max_budget_cents > 0:
            env_lines, _ = _set_env_line(env_lines, "EMPORIA_MAX_TOTAL_SPEND_CENTS", str(max_budget_cents))
        else:
            env_lines = [ln for ln in env_lines if not ln.startswith("EMPORIA_MAX_TOTAL_SPEND_CENTS=")]
    if nous_token:
        env_lines, c3 = _set_env_line(env_lines, "EMPORIA_NOUS_JWT", nous_token)
    else:
        c3 = False
    env_dirty = False
    for line in _guardrails_env_lines(disabled=no_guardrails):
        key, _, val = line.partition("=")
        env_lines, ch = _set_env_line(env_lines, key, val)
        env_dirty = env_dirty or ch
    # Export to current process so seed and any downstream scripts see it immediately
    os.environ["EMPORIA_AGENT_ID"] = agent_id
    os.environ["EMPORIA_RELAY_URL"] = relay_url
    os.environ["EMPORIA_DB_PATH"] = db_path
    os.environ["EMPORIA_KEYS_DIR"] = runtime_env["EMPORIA_KEYS_DIR"]
    if nous_token:
        os.environ["EMPORIA_NOUS_JWT"] = nous_token
    if stripe_secret_key:
        os.environ["STRIPE_SECRET_KEY"] = stripe_secret_key
    elif "STRIPE_SECRET_KEY" in os.environ:
        os.environ.pop("STRIPE_SECRET_KEY", None)
    for line in _guardrails_env_lines(disabled=no_guardrails):
        key, _, val = line.partition("=")
        os.environ[key] = val

    if c1 or c2 or c3 or c4 or c5 or env_dirty:
        if not dry_run:
            _write_private(env_file, "\n".join(env_lines) + "\n")
            print(f"  Updated: {env_file}")
        else:
            print(f"  [dry-run] Would update: {env_file}")
    if not dry_run and (moved := secure_profile(profile_dir)):
        print(f"  Moved from config.yaml to .env: {', '.join(moved)}")

    write_dashboard_env(relay_url, agent_id, dry_run=dry_run)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# --create-profile
# ─────────────────────────────────────────────────────────────────────────────

def _merge_profile_env(env_path: Path, managed_lines: list[str], keep_relay: bool = False) -> list[str]:
    """The installer's lines, then every line of the existing .env it does not
    manage (P115). It used to write the file whole, dropping anything the
    operator had added — and the relay's Stripe settings, which an agent
    profile must not hold, are still dropped (_RELAY_ONLY_ENV_KEYS)."""
    managed = {ln.split("=", 1)[0].strip() for ln in managed_lines if "=" in ln}
    kept: list[str] = []
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
            if key and (key in managed or (key in _RELAY_ONLY_ENV_KEYS and not keep_relay)):
                continue
            if line.strip():
                kept.append(line)
    return managed_lines + kept


def create_profile(
    name: str,
    relay_url: str,
    current_profile_dir: Path | None,
    max_budget_cents: int | None = None,
    nous_token: str | None = None,
    inherit_env: bool = True,
    dry_run: bool = False,
    update_dashboard: bool = False,
    dev_skills: bool = False,
    x402_pay_networks: str = "",
    x402_max_payment_cents: int | None = None,
    x402_max_daily_cents: int | None = None,
    sync_model: bool = False,
) -> None:
    """Create a new Hermes agent profile with emporia pre-wired.

    Takes no Stripe settings (P107): those belong to the relay's profile, never
    an agent's — see _RELAY_ONLY_ENV_KEYS."""

    # Determine where to create it
    if current_profile_dir:
        profiles_base = detect_profiles_base(current_profile_dir)
    else:
        profiles_base = _profile_base_dirs()[0] if _profile_base_dirs() else Path.cwd()

    profile_dir = profiles_base / name
    config_path = profile_dir / "config.yaml"

    updating = config_path.exists()
    print(f"\n{'Updating' if updating else 'Creating'} profile: {profile_dir}")

    if not dry_run:
        if not updating:
            _hermes_profile_create(name)
        profile_dir.mkdir(parents=True, exist_ok=True)

    # Keypair
    pub_hex, _ = generate_keypair(
        name, dry_run=dry_run,
        keys_dir=Path(_profile_runtime_env(profile_dir)["EMPORIA_KEYS_DIR"]),
    )

    # On update, start from the existing config so we don't clobber other settings.
    existing_config: dict[str, Any] = load_yaml(config_path) if updating else {}

    # Inherit model block from current profile (or keep existing)
    model_block: dict | None = existing_config.get("model")
    inherited_env: dict[str, str] = dict(existing_config.get("env", {}))
    if current_profile_dir:
        cur_config = load_yaml(current_profile_dir / "config.yaml")
        # P116: --sync-model makes an existing profile follow this one's model;
        # without it an existing profile keeps its own (a new one always copies).
        if not model_block or sync_model:
            model_block = _model_block(cur_config) or model_block
        if inherit_env:
            env_from_file = harvest_env(current_profile_dir)
            env_from_config = cur_config.get("env", {})
            upstream = collect_upstream_env(current_profile_dir)
            for k in _SAFE_ENV_KEYS:
                if k not in inherited_env:
                    for src in (env_from_file, env_from_config, upstream):
                        if k in src:
                            inherited_env[k] = src[k]
                            break
            for k in _SECRET_ENV_KEYS:
                if k not in inherited_env:
                    for src in (env_from_file, env_from_config, upstream):
                        if k in src and src[k]:
                            inherited_env[k] = src[k]
                            break

    # Build config (merge over existing)
    config: dict[str, Any] = dict(existing_config)
    if model_block:
        config["model"] = model_block
        if not updating:
            print(f"  + model: {model_block.get('default', model_block.get('provider', '?'))}")

    config["mcp_servers"] = {
        # P109: profile_dir, so the entry carries this profile's key dir,
        # database and .env. Without it the MCP server fell back to the .env
        # beside the code — another profile's.
        MCP_TOOL_NAME: _mcp_entry(relay_url, agent_id=name, profile_dir=profile_dir),
    }
    config["toolsets"] = ["hermes-cli"]
    config["agent"] = {"max_turns": 150}
    config["terminal"] = {"backend": "local", "home_mode": "auto"}

    # Env block. P217: the relay's own profile (set up from itself) keeps the
    # relay's settings — they are what an *agent* profile must not hold. Setting
    # hackathon_hermes up as an agent once dropped its relay's Stripe key.
    own_relay = current_profile_dir is not None and profile_dir.resolve() == current_profile_dir.resolve()
    env_block: dict[str, str] = {**inherited_env}
    env_block["EMPORIA_RELAY_URL"] = relay_url
    env_block["EMPORIA_AGENT_ID"] = name
    for k in () if own_relay else _RELAY_ONLY_ENV_KEYS:
        env_block.pop(k, None)
    if max_budget_cents is not None and max_budget_cents > 0:
        env_block["EMPORIA_MAX_TOTAL_SPEND_CENTS"] = str(max_budget_cents)
    else:
        env_block.pop("EMPORIA_MAX_TOTAL_SPEND_CENTS", None)
    if nous_token:
        env_block["EMPORIA_NOUS_JWT"] = nous_token
    _apply_guardrails_env(env_block, disabled=False)
    # P54: EMPORIA_PUBLIC_KEY used to be written here. Nothing in this repo,
    # in Hermes, or anywhere else ever read it — it was a key-shaped name in an
    # env block that changed nothing when edited, and the value is derivable
    # from the keypair this installer just generated. Removed rather than
    # shimmed (no backcompat is owed here); the public key is available from
    # `emporia.identity.get_public_key_hex(agent_id)` and from the agent's own
    # profile on the relay.
    # P201: secrets go to the .env below, never config.yaml.
    config["env"] = {k: v for k, v in env_block.items() if not is_secret_name(k)}

    if dry_run:
        print(f"  [dry-run] Would write {config_path}")
        print(f"  [dry-run] env keys: {list(env_block.keys())}")
    else:
        save_yaml(config_path, config)
        print(f"  Saved: {config_path}")

    # Write .env file. P102: the profile's own key dir and DB, the same paths
    # its MCP entry gets, so every process for this profile agrees.
    runtime_env = _profile_runtime_env(profile_dir)
    _ensure_keys_dir(runtime_env, dry_run)
    env_path = profile_dir / ".env"
    safe_env_lines = [
        f"EMPORIA_RELAY_URL={relay_url}",
        f"EMPORIA_AGENT_ID={name}",
        f"EMPORIA_DB_PATH={runtime_env['EMPORIA_DB_PATH']}",
        f"EMPORIA_KEYS_DIR={runtime_env['EMPORIA_KEYS_DIR']}",
        f"HERMES_AGENT_ID={name}",
        # P109: for the platform bridge, which inherits the gateway's env.
        f"EMPORIA_DOTENV={env_path}",
    ]
    # P109: the agent wallet's spending policy (agent_wallet.py). Written here
    # because this function rewrites the .env whole — a hand-added line would
    # not survive the next run.
    if x402_pay_networks:
        safe_env_lines.append(f"EMPORIA_X402_PAY_NETWORKS={x402_pay_networks}")
    if x402_max_payment_cents is not None:
        safe_env_lines.append(f"EMPORIA_X402_MAX_PAYMENT_CENTS={x402_max_payment_cents}")
    if x402_max_daily_cents is not None:
        safe_env_lines.append(f"EMPORIA_X402_MAX_DAILY_CENTS={x402_max_daily_cents}")
    # P117: the one key this profile's model needs, into the .env Hermes reads it from.
    provider_line = _provider_key_line(
        model_block, env_block,
        *((harvest_env(current_profile_dir), collect_upstream_env(current_profile_dir))
          if current_profile_dir else ()), profile_dir=profile_dir,
        labels=["its own settings",
                *((f"{current_profile_dir.name}'s .env", f"Hermes's main .env ({hermes_home_of(current_profile_dir)})")
                  if current_profile_dir else ())])
    if provider_line:
        safe_env_lines.append(provider_line)
    if max_budget_cents is not None and max_budget_cents > 0:
        safe_env_lines.append(f"EMPORIA_MAX_TOTAL_SPEND_CENTS={max_budget_cents}")
    if nous_token:
        safe_env_lines.append(f"EMPORIA_NOUS_JWT={nous_token}")
    safe_env_lines.extend(_guardrails_env_lines(disabled=False))
    # Every inherited secret (P201: config.yaml no longer holds any), once.
    written = {ln.split("=", 1)[0] for ln in safe_env_lines}
    for k, v in env_block.items():
        if ((is_secret_name(k) or k in _SECRET_ENV_KEYS) and v and k not in written
                and (own_relay or k not in _RELAY_ONLY_ENV_KEYS)):
            safe_env_lines.append(f"{k}={v}")

    # Export to current process so subsequent steps see the vars immediately
    os.environ["EMPORIA_AGENT_ID"] = name
    os.environ["EMPORIA_RELAY_URL"] = relay_url
    os.environ["EMPORIA_DB_PATH"] = runtime_env["EMPORIA_DB_PATH"]
    os.environ["EMPORIA_KEYS_DIR"] = runtime_env["EMPORIA_KEYS_DIR"]
    if nous_token:
        os.environ["EMPORIA_NOUS_JWT"] = nous_token

    if dry_run:
        print(f"  [dry-run] Would write {env_path}")
    else:
        _write_private(env_path, "\n".join(_merge_profile_env(env_path, safe_env_lines, keep_relay=own_relay)) + "\n")
        print(f"  Saved: {env_path}")
        if moved := secure_profile(profile_dir):
            print(f"  Moved from config.yaml to .env: {', '.join(moved)}")

    if update_dashboard:
        write_dashboard_env(relay_url, name, dry_run=dry_run)

    # Install agent skill so it loads on start (dev skills optional)
    _install_skill_link(profile_dir, dry_run=dry_run, dev_skills=dev_skills)
    install_platform_plugin(profile_dir, dry_run=dry_run)

    print(f"\n  Agent ID : {name}")
    print(f"  Relay    : {relay_url}")
    print(f"  Profile  : {profile_dir}")
    if model_block:
        print(f"  Model    : {model_block.get('default', model_block.get('provider', '?'))}")
    print(f"  The agent registers as '{name}' when its MCP server starts.")


# ─────────────────────────────────────────────────────────────────────────────
# --bootstrap-test
# ─────────────────────────────────────────────────────────────────────────────

def _start_relay(relay_url: str) -> bool:
    """Start the relay in the background. Returns True if healthy within ~12s."""
    scripts = str(_EMPORIA_DIR / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from local_relay import ensure_relay_running

    return ensure_relay_running(relay_url)


def _ensure_project_dependencies(*, dry_run: bool = False) -> None:
    """Sync emporia .venv (chess, fastapi, etc.) — idempotent."""
    if dry_run:
        print("  [dry-run] Would sync Python dependencies")
        return
    py = _venv_python()
    uv = shutil.which("uv")
    if uv:
        # P64: `--inexact`, because this same command runs in a contributor's
        # checkout. A bare `uv sync` installs the default set and *prunes*
        # everything else, so running the documented operator setup
        # (`--local-demo`, which calls this) silently deleted `ruff` and
        # `eth-account` from the venv — the `dev` and `x402` extras CI installs
        # with `uv sync --frozen --extra dev --extra x402`. The next
        # `uv run python -m ruff check` (CI's own lint command) then failed with
        # "No module named ruff", with nothing connecting it to the setup step
        # that caused it. Measured, not guessed: both imports were gone after a
        # local-demo run.
        subprocess.run(
            [uv, "sync", "--inexact", "--directory", str(_EMPORIA_DIR)],
            cwd=str(_EMPORIA_DIR), check=False,
        )
    else:
        subprocess.run(
            [py, "-m", "pip", "install", "-e", str(_EMPORIA_DIR)],
            cwd=str(_EMPORIA_DIR),
            check=False,
        )
    chk = subprocess.run([py, "-c", "import chess"], capture_output=True)
    if chk.returncode != 0:
        subprocess.run([py, "-m", "pip", "install", "chess>=1.10.0"], check=False)
    link_cli()


def link_cli(bin_dir: Path | None = None) -> Path | None:
    """P210: `emporia` on PATH. The command lives in this checkout's .venv, and
    every doc says `emporia …`; a first install left it unreachable by that
    name. Never over another `emporia`. Returns the link, or None when it says
    how instead.

    P211: where. Hermes's data (HERMES_HOME) is often a volume — /opt/data in
    the official image, ~/.hermes on a host — and in a container only what is
    on it survives the container being recreated. So a folder on PATH inside
    HERMES_HOME comes first, then ~/.local/bin; and the link is relative when
    it and the command share that folder, so it still points right where the
    same volume is mounted at another path."""
    exe = _EMPORIA_DIR / ".venv" / "bin" / "emporia"
    if not exe.exists() or shutil.which("emporia"):
        return None
    on_path = [Path(d) for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    hermes = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser().resolve()
    if bin_dir is not None:
        target_dir = bin_dir if bin_dir in on_path else None
    else:
        inside = [d for d in on_path if d.resolve().is_relative_to(hermes)]
        home_bin = Path.home() / ".local" / "bin"
        target_dir = (inside[0] if inside else home_bin if home_bin in on_path else None)
    link = target_dir / "emporia" if target_dir else None
    if link is None or link.exists() or link.is_symlink():
        print(f"  `emporia` is not on PATH. Run it as {exe}, or: ln -s {exe} <a folder on your PATH>/emporia")
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        if target_dir.resolve().is_relative_to(hermes) and exe.resolve().is_relative_to(hermes):
            link.symlink_to(os.path.relpath(exe.resolve(), target_dir.resolve()))
        else:
            link.symlink_to(exe)
    except OSError as e:
        print(f"  Could not link `emporia` ({e}); run it as {exe}")
        return None
    print(f"  `emporia` → {link}")
    in_box = any(Path(m).exists() for m in ("/.dockerenv", "/run/.containerenv"))
    if in_box and not target_dir.resolve().is_relative_to(hermes):
        print(f"  ! {target_dir} is not on Hermes's data volume ({hermes}): the link goes when the container is "
              f"recreated — run the installer again then, or add a folder inside {hermes} to PATH.")
    return link


def _build_dashboard_embedded(*, dry_run: bool = False, relay_url: str = "") -> None:
    """npm install + build:embedded so relay serves /ui/."""
    dash = _EMPORIA_DIR / "dashboard"
    if not dash.is_dir():
        print("  dashboard/ not found — skip build")
        return
    if dry_run:
        print("  [dry-run] Would run: cd dashboard && npm install && npm run build:embedded")
        return
    npm = shutil.which("npm")
    if not npm:
        print("  npm not on PATH — install Node.js or run: cd dashboard && npm run build:embedded")
        return
    print("Building embedded dashboard (relay /ui/)…")
    subprocess.run([npm, "install"], cwd=str(dash), check=False)
    r = subprocess.run([npm, "run", "build:embedded"], cwd=str(dash), check=False)
    if r.returncode == 0:
        # P64: the URL the caller asked for, not the default. `--local-demo
        # --relay-url http://127.0.0.1:8099` printed "open http://localhost:8088/ui/"
        # and then, three lines later, the correct 8099 — so the first thing an
        # operator reads is the one address that does not work.
        print("  Dashboard built → open {}/ui/ after relay is up".format(
            (relay_url or DEFAULT_RELAY_URL).rstrip("/")))
    else:
        print("  Dashboard build failed — see output above")


def _run_demo_seed(relay_url: str, *, dry_run: bool = False) -> None:
    seed_script = _EMPORIA_DIR / "scripts" / "seed_demo_relay.py"
    if not seed_script.exists() or dry_run:
        if dry_run:
            print("\n  [dry-run] Would run seed_demo_relay.py")
        return
    print("\nSeeding relay with demo content…")
    if not _relay_healthy(relay_url):
        print(f"  Relay not running — starting on {relay_url}…")
        if not _start_relay(relay_url):
            print(f"  Relay failed to start — run manually: python {seed_script}")
            return
    # P64: the seed reads EMPORIA_RELAY_URL and defaults to localhost:8088, so
    # `--local-demo --relay-url http://127.0.0.1:8099` started a relay on 8099
    # and then seeded whatever was answering on 8088. The seed's own split-brain
    # guard caught it — "wrote 6 challenges directly to <db>, but the relay at
    # http://localhost:8088 went from 6 to 6" — which is the right error for the
    # wrong reason: the databases matched, the *relay* was the wrong one.
    seed_env = os.environ.copy()
    seed_env["EMPORIA_RELAY_URL"] = relay_url
    result = subprocess.run(
        [_venv_python(), str(seed_script)],
        capture_output=True,
        text=True,
        env=seed_env,
        cwd=str(_EMPORIA_DIR),
    )
    if result.returncode == 0:
        try:
            import json as _json

            summary = _json.loads(result.stdout)
            print(f"  Seeded: {summary.get('seeded', {})}")
        except Exception:
            print(result.stdout.strip())
    else:
        print(f"  Seed failed: {result.stderr[-500:]}")


def _relay_healthy(relay_url: str, timeout: float = 3) -> bool:
    try:
        urllib.request.urlopen(f"{relay_url.rstrip('/')}/health", timeout=timeout)
        return True
    except Exception:
        return False


def _is_local_relay(relay_url: str) -> bool:
    try:
        host = urllib.parse.urlparse(relay_url).hostname or ""
    except Exception:
        return False
    return host.lower() in ("localhost", "127.0.0.1", "::1", "")


def bootstrap_test_profiles(
    relay_url: str,
    current_profile_dir: Path | None,
    stripe_secret_key: str | None = None,
    stripe_profile_id: str | None = None,
    stripe_api_version: str | None = None,
    max_budget_cents: int | None = None,
    nous_token: str | None = None,
    dry_run: bool = False,
    dev_skills: bool = False,
) -> None:
    """Create the test agent profiles (alpha, beta, stripe_escrow_bot), then seed."""
    if stripe_secret_key:
        os.environ["STRIPE_SECRET_KEY"] = stripe_secret_key
    elif not stripe_secret_key and "STRIPE_SECRET_KEY" not in os.environ:
        os.environ.pop("STRIPE_SECRET_KEY", None)
    for name in ("alpha", "beta", "stripe_escrow_bot"):
        print(f"\nBootstrapping: {name}")
        create_profile(
            name=name,
            relay_url=relay_url,
            current_profile_dir=current_profile_dir,
            max_budget_cents=max_budget_cents,
            nous_token=nous_token,
            inherit_env=False,  # P117: bare — the model's key is copied on its own
            dry_run=dry_run,
            update_dashboard=False,  # don't overwrite operator dashboard env
            dev_skills=dev_skills,
        )

    # Restore dashboard env to the operator profile (not the last demo agent)
    if current_profile_dir:
        operator_id = current_profile_dir.name
        print(f"\nRestoring dashboard env: VITE_AGENT_ID={operator_id}")
        write_dashboard_env(relay_url, operator_id, dry_run=dry_run)

    # Seed the relay with demo content
    _run_demo_seed(relay_url, dry_run=dry_run)


# ─────────────────────────────────────────────────────────────────────────────
# Skill symlink
# ─────────────────────────────────────────────────────────────────────────────

# Optional dev skills (repo → profile relative paths under skills/)
_DEV_SKILL_SYMLINKS: list[tuple[str, str]] = [
    ("dev/emporia-dev", "software-development/emporia-dev"),
    ("dev/srcl-terminal-ui", "creative/srcl-terminal-ui"),
]

# Payment skills every Emporia profile needs (mpp-agent, stripe-link-cli, stripe-projects).
# `hermes skills install <name>` is registry-bound and can hang unattended — these are already
# vendored locally under the Hermes install's optional-skills catalog, so symlink directly
# (same idempotent pattern as the emporia/dev skill links below).
_PAYMENT_SKILL_NAMES: list[str] = ["mpp-agent", "stripe-link-cli", "stripe-projects"]


def _hermes_optional_skills_root() -> Path | None:
    """Locate the Hermes install's optional-skills catalog (…/opt/hermes/optional-skills)."""
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        return None
    # .venv/bin/hermes -> install root is three parents up
    root = Path(hermes_bin).resolve().parent.parent.parent
    candidate = root / "optional-skills"
    return candidate if candidate.is_dir() else None


# The Hermes platform plugin key: user plugins under `plugins/platforms/<dir>`
# are keyed `platforms/<dir>`, which is what `plugins.enabled` lists.
PLATFORM_PLUGIN_KEY = "platforms/emporia"
# The relay's own reports (your turn, results) are attributed to this sender by
# frames.RELAY_SENDER; it is allowlisted so they reach the agent. Other agents'
# messages pass only if the operator adds them (or sets EMPORIA_ALLOW_ALL_USERS).
RELAY_SENDER = "emporia:relay"


def _tree_digest(path: Path) -> str | None:
    """A digest of a plugin directory's files, or None if it is not there."""
    import hashlib
    if not path.is_dir():
        return None
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(str(f.relative_to(path)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


# P119: set when this run installed or changed the gateway-side plugin, which
# Hermes loads only when its (shared) gateway process starts.
_GATEWAY_RESTART_NEEDED = False
_TOUCHED_PROFILES: list[str] = []


_DASHBOARD_RESTART_NEEDED = False


def _git_exclude(repo_child: Path) -> str | None:
    """Exclude `repo_child` via its git repo's local .git/info/exclude — never a
    tracked file — when it sits inside one. Returns the pattern added, if any."""
    try:
        top = subprocess.run(["git", "-C", str(repo_child.parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    pattern = "/" + str(repo_child.resolve().relative_to(Path(top).resolve())) + "/"
    exclude = Path(top) / ".git" / "info" / "exclude"
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if pattern not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join([*lines, pattern]) + "\n")
    return pattern


def install_dashboard_plugin(profile_dir: Path, relay_url: str, hermes_home: Path,
                             dry_run: bool = False) -> None:
    """The Emporia tab in the Hermes dashboard (P126; docs/local-console.md).

    Copies hermes_plugin/emporia_dashboard and the built bundle
    (dashboard/dist-plugin, from `npm run build:hermes-plugin`) to
    <hermes_home>/plugins/emporia, writes its emporia.json (this venv's python,
    the relay, where the profiles' keys are, a 0700 runtime directory for the
    console's socket), keeps it out of any git repo it lands in, and enables it
    with `hermes plugins enable` — Hermes runs a user plugin's backend only once
    it is enabled. No port: the tab is served by the Hermes dashboard itself.
    """
    global _DASHBOARD_RESTART_NEEDED
    src = _EMPORIA_DIR / "hermes_plugin" / "emporia_dashboard"
    bundle = _EMPORIA_DIR / "dashboard" / "dist-plugin"
    dst = hermes_home / "plugins" / "emporia"
    if dry_run:
        print(f"  [dry-run] Would install the Emporia dashboard tab at {dst}")
        return
    npm = shutil.which("npm")
    if npm:
        subprocess.run([npm, "run", "-s", "build:hermes-plugin"], cwd=_EMPORIA_DIR / "dashboard",
                       check=True, capture_output=True, timeout=600)
    if not (bundle / "index.js").exists():
        raise SystemExit("  The dashboard bundle is missing: run `npm run build:hermes-plugin` in "
                         "dashboard/ (npm was not found), then re-run the installer.")
    before = _tree_digest(dst)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(bundle, dst / "dashboard" / "dist")
    runtime = profile_dir / "home" / ".hermes" / "emporia-console"
    (dst / "dashboard" / "emporia.json").write_text(json.dumps({
        "python": _venv_python(),
        "relay_url": relay_url,
        "profiles_root": str(profile_dir.parent),
        "runtime_dir": str(runtime),
    }, indent=2) + "\n")
    if _tree_digest(dst) != before:
        _DASHBOARD_RESTART_NEEDED = True
    print(f"  + Hermes dashboard tab: {dst}")
    pattern = _git_exclude(dst)
    if pattern:
        print(f"  + kept out of git via .git/info/exclude: {pattern}")
    env = {**os.environ, "HERMES_HOME": str(hermes_home)}
    r = subprocess.run(["hermes", "plugins", "enable", "emporia"], env=env,
                       capture_output=True, text=True, timeout=60)
    print("  + plugins.enabled: emporia" if r.returncode == 0
          else f"  WARNING: `hermes plugins enable emporia` failed: {(r.stderr or r.stdout)[-300:]}")


def print_safety_next_steps(name: str) -> None:
    """P172: the installer wires a profile; the choices that keep its keys out
    of its own tools' reach are made by `emporia setup agent`. Say so."""
    print()
    print(f"  Next — choose what {name} may do and where its keys and money live:")
    print(f"      emporia setup agent {name}     (role, key storage, wallets, limits, approval)")
    print(f"      emporia role {name} marketplace   (if it will hold money: no terminal, files or code)")
    print("      emporia doctor                    (checks all of it)")
    print("  Guide: docs/agent-security-setup.md")


def print_restart_instructions(profiles: list[str]) -> None:
    """Tell the user exactly which restart their change needs (P119). A
    per-profile restart respawns the bridge and MCP server but keeps the plugin
    code the gateway loaded at startup; there is no plugin reload."""
    if not (_DASHBOARD_RESTART_NEEDED or _GATEWAY_RESTART_NEEDED or profiles):
        return
    try:     # P220: a heading like the rest of the CLI's output
        from emporia.menus import heading
        heading("Next")
    except ImportError:
        print()
    if _DASHBOARD_RESTART_NEEDED:
        print("  RESTART THE HERMES DASHBOARD — the Emporia tab was installed or changed.")
        print("  It loads plugins only when it starts: restart the `hermes dashboard` process,")
        print("  then open the dashboard and pick the Emporia tab.")
    if _GATEWAY_RESTART_NEEDED:
        print("  RESTART REQUIRED — the Emporia gateway plugin was installed or changed.")
        print("  Hermes loads plugins only when the gateway process starts, and one process")
        print("  serves every profile, so restart it once (no -p):")
        print("      hermes gateway restart")
        print("  This briefly interrupts every profile's gateway, including any cron jobs it runs.")
    elif profiles:
        print("  To pick up the changes, in each profile:")
        for name in profiles:
            print(f"      hermes -p {name} gateway restart")
        print("  and run /reload-mcp in its chat.")


def install_platform_plugin(profile_dir: Path, dry_run: bool = False) -> None:
    """Install the Hermes platform adapter into a profile (P92).

    P63 removed the inert `platforms: - platform: emporia` entry and recorded
    what a real adapter needs: a `plugin.yaml`, `register(ctx)`, and a
    `BasePlatformAdapter` subclass. `hermes_plugin/emporia/` is that; this puts
    it where Hermes's discovery looks for a profile's own plugins, enables it,
    and writes the three settings it reads from the profile's `.env`.

    Copied, not symlinked: Hermes's plugin scan applies containment rules a link
    out of the profile could fail. Re-running the installer refreshes the copy.
    """
    src = _EMPORIA_DIR / "hermes_plugin" / "emporia"
    dst = profile_dir / "plugins" / "platforms" / "emporia"
    if dry_run:
        print(f"  [dry-run] Would install the Emporia platform plugin at {dst}")
        return
    global _GATEWAY_RESTART_NEEDED
    if profile_dir.name not in _TOUCHED_PROFILES:
        _TOUCHED_PROFILES.append(profile_dir.name)
    before = _tree_digest(dst)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    if _tree_digest(dst) != before:
        _GATEWAY_RESTART_NEEDED = True
        print(f"  + Hermes platform plugin: {dst} (new or changed — needs a gateway restart)")
    else:
        print(f"  = Hermes platform plugin: {dst} (unchanged)")

    config_path = profile_dir / "config.yaml"
    config = load_yaml(config_path) if config_path.exists() else {}
    plugins = config.setdefault("plugins", {})
    enabled = plugins.get("enabled")
    enabled = list(enabled) if isinstance(enabled, list) else []
    if PLATFORM_PLUGIN_KEY not in enabled:
        plugins["enabled"] = [*enabled, PLATFORM_PLUGIN_KEY]
        save_yaml(config_path, config)
        print(f"  + plugins.enabled: {PLATFORM_PLUGIN_KEY}")

    env_file = profile_dir / ".env"
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    values = dict(ln.split("=", 1) for ln in lines if "=" in ln and not ln.startswith("#"))
    allowed = [a for a in (values.get("EMPORIA_ALLOWED_USERS") or "").split(",") if a.strip()]
    wanted = {
        "EMPORIA_PYTHON": _venv_python(),
        "EMPORIA_ALLOWED_USERS": ",".join(allowed if RELAY_SENDER in allowed
                                          else [*allowed, RELAY_SENDER]),
    }
    changed = False
    for key, value in wanted.items():
        for i, ln in enumerate(lines):
            if ln.startswith(f"{key}="):
                if ln != f"{key}={value}":
                    lines[i], changed = f"{key}={value}", True
                break
        else:
            lines.append(f"{key}={value}")
            changed = True
    if changed:
        env_file.write_text("\n".join(lines) + "\n")
        print(f"  + {env_file}: EMPORIA_PYTHON, EMPORIA_ALLOWED_USERS")


def _install_skill_link(
    profile_dir: Path,
    dry_run: bool = False,
    *,
    dev_skills: bool = False,
) -> None:
    """Symlink Emporia skills from the repo into the profile skills dir.

    Profile paths are symlinks to the repo — edits under emporia/skills/ are visible
    to Hermes immediately (no re-copy). Re-run installer to fix broken links after moves.

    Always links the agent skill (`emporia`) plus the payment skills Emporia needs
    (mpp-agent, stripe-link-cli, stripe-projects) — self-heals on `--install-profile` too.
    Dev skills only when dev_skills=True (--dev-skills).
    """
    skills_dir = profile_dir / "skills"
    links: list[tuple[Path, Path]] = [
        (_EMPORIA_DIR / "skills" / "emporia.md", skills_dir / "emporia.md"),
        (_EMPORIA_DIR / "skills" / "emporia", skills_dir / "emporia"),
        # P127: how to install, check and repair Emporia (the `emporia` command).
        (_EMPORIA_DIR / "skills" / "emporia-setup", skills_dir / "emporia-setup"),
    ]
    optional_root = _hermes_optional_skills_root()
    if optional_root:
        for skill_name in _PAYMENT_SKILL_NAMES:
            links.append(
                (optional_root / "payments" / skill_name, skills_dir / "payments" / skill_name),
            )
    if dev_skills:
        for src_rel, dst_rel in _DEV_SKILL_SYMLINKS:
            links.append(
                (_EMPORIA_DIR / "skills" / src_rel, skills_dir / dst_rel),
            )
    for skill_src, skill_dst in links:
        if not skill_src.exists():
            continue
        if dry_run:
            print(f"  [dry-run] Would link skill: {skill_dst} → {skill_src}")
            continue
        skills_dir.mkdir(exist_ok=True)
        if skill_dst.parent != skills_dir:
            skill_dst.parent.mkdir(parents=True, exist_ok=True)
        if skill_dst.exists() or skill_dst.is_symlink():
            if skill_dst.is_symlink() and skill_dst.resolve() == skill_src.resolve():
                continue
            if not skill_dst.is_symlink():
                # Real directory/file already there (e.g. a hub-installed copy) — already
                # present, leave it alone rather than unlink() a non-empty directory.
                continue
            skill_dst.unlink()
        skill_dst.symlink_to(skill_src)
        print(f"  + skill: {skill_dst} → {skill_src}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Emporia installer — wire Hermes profiles with auto-registering MCP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--install-profile", action="store_true",
                        help="Patch the current profile's config.yaml (auto-detected from CWD)")
    parser.add_argument("--create-profile", metavar="NAME",
                        help="Create a new agent profile named NAME")
    parser.add_argument("--bootstrap-test", action="store_true",
                        help="Create alpha/beta/stripe_escrow_bot profiles + seed")
    parser.add_argument("--seed-only", action="store_true",
                        help="Start local relay if needed and run scripts/seed_demo_relay.py")
    parser.add_argument("--start-relay", action="store_true",
                        help="Sync Python deps and start local relay (background uvicorn)")
    parser.add_argument("--build-dashboard", action="store_true",
                        help="npm install + build:embedded for relay /ui/")
    parser.add_argument("--x402-pay-networks", default="",
                        help="With --create-profile: CAIP-2 networks the agent's x402 wallet may "
                             "pay on, e.g. eip155:84532 (unset = pays on none)")
    parser.add_argument("--x402-max-payment-cents", type=int, default=None,
                        help="With --create-profile: largest single x402 payment it signs")
    parser.add_argument("--x402-max-daily-cents", type=int, default=None,
                        help="With --create-profile: most the x402 wallet signs in any 24 hours")
    parser.add_argument("--sync-model", action="store_true",
                        help="With --create-profile on an existing profile: switch it to this "
                             "profile's model (a new profile always copies it)")
    parser.add_argument("--hermes-dashboard", action="store_true",
                        help="Install or refresh the Emporia tab in the Hermes dashboard (P126)")
    parser.add_argument("--hermes-home", default="",
                        help="With --hermes-dashboard: the Hermes home the dashboard runs from "
                             "(default: $HERMES_HOME, else ~/.hermes)")
    parser.add_argument("--no-seed", action="store_true",
                        help="With --install-profile on a local relay: skip the demo seed")
    parser.add_argument("--local-demo", action="store_true",
                        help="Shorthand: --build-dashboard --start-relay --seed-only (no profile changes)")
    parser.add_argument("--relay-url", default=DEFAULT_RELAY_URL,
                        help=f"Relay URL (default: {DEFAULT_RELAY_URL})")
    parser.add_argument("--agent-id", default="",
                        help="Agent ID for --install-profile (default: profile dir name or HERMES_AGENT_ID)")
    parser.add_argument("--display-name", default="",
                        help="Display name for the agent (defaults to agent-id)")
    parser.add_argument("--stripe-secret-key", default=None,
                        help="Stripe secret key (else profile/parent .env, config, or prompt)")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Do not prompt for missing Stripe keys (leave features off)")
    parser.add_argument("--stripe-profile-id", default=None,
                        help="Stripe Machine Payments profile ID (profile_... / profile_test_...); use 'auto' to discover from env files or Stripe API (sk_* keys only)")
    parser.add_argument("--stripe-api-version", default=os.getenv("STRIPE_API_VERSION", "2026-04-22.preview"),
                        help="Stripe API version for SPT/MPP preview endpoints")
    parser.add_argument("--max-budget-cents", type=int, default=int(os.getenv("EMPORIA_MAX_TOTAL_SPEND_CENTS", "0")),
                        help="Optional total cumulative spend limit per agent in cents (0 = unlimited)")
    parser.add_argument("--nous-token", default=None, metavar="JWT",
                        help="Nous access JWT — stored as EMPORIA_NOUS_JWT; "
                             "enables nous_verified trust level on this relay")
    parser.add_argument("--no-guardrails", action="store_true",
                        help="Set EMPORIA_GUARDRAILS_MODE=off (testing only)")
    parser.add_argument("--no-inherit-env", action="store_true",
                        help="Don't copy env vars from current profile when creating new profile")
    parser.add_argument("--inherit-env", action="store_true",
                        help="With --create-profile: also copy this profile's other settings "
                             "(provider keys, budgets). Default is bare: the model "
                             "and the one key it needs")
    parser.add_argument("--dev-skills", action="store_true",
                        help="Symlink dev skills from repo (emporia-dev, srcl-terminal-ui)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    args = parser.parse_args()

    if args.local_demo:
        args.build_dashboard = True
        args.start_relay = True
        args.seed_only = True

    ops_mode = any([args.seed_only, args.start_relay, args.build_dashboard])
    profile_mode = any([args.install_profile, args.create_profile, args.bootstrap_test,
                        args.hermes_dashboard])

    if not ops_mode and not profile_mode:
        parser.print_help()
        return

    print(f"Emporia installer  relay={args.relay_url}")
    if args.dry_run:
        print("DRY RUN — no files will be written")
    print()

    if ops_mode and not profile_mode:
        if not args.dry_run:
            _ensure_project_dependencies(dry_run=False)
        if args.build_dashboard:
            _build_dashboard_embedded(dry_run=args.dry_run, relay_url=args.relay_url)
        if args.start_relay:
            if args.dry_run:
                print("  [dry-run] Would start relay")
            elif _start_relay(args.relay_url):
                print(f"  Relay ready: {args.relay_url.rstrip('/')}/health")
                print(f"  Dashboard:   {args.relay_url.rstrip('/')}/ui/")
            else:
                print("  Relay failed to start")
                sys.exit(1)
        if args.seed_only:
            _run_demo_seed(args.relay_url, dry_run=args.dry_run)
        return

    if not args.dry_run:
        _ensure_project_dependencies(dry_run=False)

    # Detect current profile first — Nous auth.json lives inside it (see
    # _find_auth_json), so token resolution needs this to check the right file.
    # 1. Walk up from CWD
    current_profile_dir = find_profile_dir_from_cwd()
    # 2. Fall back to HERMES_PROFILE env var
    if not current_profile_dir:
        env_name = os.getenv("HERMES_PROFILE") or os.getenv("HERMES_AGENT_ID")
        if env_name:
            current_profile_dir = find_profile_dir_by_name(env_name)
    if current_profile_dir:
        print(f"Detected profile: {current_profile_dir}")
    else:
        print("No profile detected from CWD — using relay URL defaults")

    # Resolve Nous JWT once up front — all operations share the same token.
    # Falls back to auth.json with silent refresh; warns if unavailable.
    nous_token = resolve_nous_token(args.nous_token, profile_dir=current_profile_dir)
    if nous_token:
        print("  Nous token: ready (agents will register as nous_verified)")
    else:
        print("  Nous token: not available — agents will register as key_only (read-only trust)")
        print("  To enable nous_verified: hermes auth add nous --type oauth --no-browser --manual-paste")
    print()

    interactive = not args.non_interactive
    secrets_dir = current_profile_dir or Path.cwd()

    if args.install_profile:
        if not current_profile_dir:
            print("ERROR: cannot find a config.yaml above CWD.")
            print("Either set HERMES_PROFILE, run from inside a profile dir,")
            print("or run from inside the emporia/ dir that lives in a profile.")
            sys.exit(1)

        agent_id = (
            args.agent_id
            or os.getenv("HERMES_AGENT_ID")
            or current_profile_dir.name
        )
        print(f"Installing into: {current_profile_dir}  agent_id={agent_id!r}")

        stripe_key, stripe_profile_id = resolve_provider_secrets(
            current_profile_dir,
            stripe_secret_key=args.stripe_secret_key,
            stripe_profile_id=args.stripe_profile_id,
            interactive=interactive,
            dry_run=args.dry_run,
        )
        _print_provider_status(stripe_key, stripe_profile_id)

        install_into_profile(
            profile_dir=current_profile_dir,
            relay_url=args.relay_url,
            agent_id=agent_id,
            stripe_secret_key=stripe_key,
            stripe_profile_id=stripe_profile_id,
            stripe_api_version=args.stripe_api_version,
            max_budget_cents=args.max_budget_cents,
            nous_token=nous_token,
            no_guardrails=args.no_guardrails,
            dry_run=args.dry_run,
        )
        _install_skill_link(
            current_profile_dir,
            dry_run=args.dry_run,
            dev_skills=args.dev_skills,
        )
        install_platform_plugin(current_profile_dir, dry_run=args.dry_run)
        if not args.dry_run:
            print(f"\nThe agent registers as '{agent_id}' when its MCP server starts.")
            # P108: --no-seed, for a relay you are about to test by hand —
            # demo activity in the database blurs what the test itself did.
            if _is_local_relay(args.relay_url) and not args.no_seed:
                _run_demo_seed(args.relay_url)

    if args.create_profile:
        stripe_key, stripe_profile_id = resolve_provider_secrets(
            secrets_dir,
            stripe_secret_key=args.stripe_secret_key,
            stripe_profile_id=args.stripe_profile_id,
            interactive=interactive,
            dry_run=args.dry_run,
        )
        _print_provider_status(stripe_key, stripe_profile_id)
        create_profile(
            name=args.create_profile,
            relay_url=args.relay_url,
            current_profile_dir=current_profile_dir,
            x402_pay_networks=args.x402_pay_networks,
            x402_max_payment_cents=args.x402_max_payment_cents,
            x402_max_daily_cents=args.x402_max_daily_cents,
            sync_model=args.sync_model,
            max_budget_cents=args.max_budget_cents,
            nous_token=nous_token,
            # P117: bare by default — the model and its key, nothing else from
            # this profile; --inherit-env restores copying its other settings.
            inherit_env=args.inherit_env and not args.no_inherit_env,
            dry_run=args.dry_run,
            dev_skills=args.dev_skills,
        )

    if args.bootstrap_test:
        stripe_key, stripe_profile_id = resolve_provider_secrets(
            secrets_dir,
            stripe_secret_key=args.stripe_secret_key,
            stripe_profile_id=args.stripe_profile_id,
            interactive=interactive,
            dry_run=args.dry_run,
        )
        _print_provider_status(stripe_key, stripe_profile_id)
        bootstrap_test_profiles(
            relay_url=args.relay_url,
            current_profile_dir=current_profile_dir,
            stripe_secret_key=stripe_key,
            stripe_profile_id=stripe_profile_id,
            stripe_api_version=args.stripe_api_version,
            max_budget_cents=args.max_budget_cents,
            nous_token=nous_token,
            dry_run=args.dry_run,
            dev_skills=args.dev_skills,
        )

    if args.hermes_dashboard:
        home = Path(args.hermes_home or os.getenv("HERMES_HOME") or Path.home() / ".hermes")
        profile = current_profile_dir or find_profile_dir_from_cwd()
        if profile is None:
            raise SystemExit("  --hermes-dashboard needs the profile holding this checkout")
        install_dashboard_plugin(profile, args.relay_url, home, dry_run=args.dry_run)
    if not args.dry_run:   # P172: the safety choices live in `emporia setup agent`
        wired = ([args.create_profile] if args.create_profile else []) + (
            [current_profile_dir.name] if args.install_profile and current_profile_dir else [])
        for name in wired:
            print_safety_next_steps(name)
    if (_TOUCHED_PROFILES or _DASHBOARD_RESTART_NEEDED) and not args.dry_run:
        print_restart_instructions(_TOUCHED_PROFILES)
    print("\nDone.")


if __name__ == "__main__":
    main()
