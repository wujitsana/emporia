"""Emporia installer — wire Hermes profiles for MCP auto-registration.

Usage (run from anywhere inside or above a profile):

    # Install into the profile you're currently running as
    python installer/install.py --install-profile

    # Same, but explicit agent ID and relay
    python installer/install.py --install-profile --agent-id my_agent --relay-url http://localhost:8088

    # Create a brand-new agent profile, inheriting model + env from current profile
    python installer/install.py --create-profile scout_agent

    # Bootstrap two test profiles (alpha buyer + beta vendor)
    python installer/install.py --bootstrap-test

    # Preview without writing
    python installer/install.py --install-profile --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Make emporia importable when run directly
_EMPORIA_DIR = Path(__file__).parent.parent
_src = str(_EMPORIA_DIR / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)


def _load_dotenv() -> None:
    """Walk up from the installer to find and load the nearest .env file."""
    for parent in Path(__file__).resolve().parents:
        env_file = parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break

_load_dotenv()

DEFAULT_RELAY_URL = os.getenv("EMPORIA_RELAY_URL", "http://localhost:8088")
MCP_TOOL_NAME = "emporia"

# Which .env keys are safe to inherit (never copy raw secrets by default)
_SAFE_ENV_KEYS = {
    "EMPORIA_RELAY_URL",
    "HERMES_AGENT_ID",
    "NVIDIA_API_KEY",
    "OPENROUTER_API_KEY",
}
_SECRET_ENV_KEYS = {
    "STRIPE_SECRET_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "NOUS_API_KEY",
    "EMPORIA_NOUS_JWT",
}


# ─────────────────────────────────────────────────────────────────────────────
# Nous token resolution
# ─────────────────────────────────────────────────────────────────────────────

def _find_auth_json() -> Path | None:
    """Walk common locations to find auth.json."""
    candidates = [
        Path(os.getenv("HERMES_DATA_DIR", "")) / "auth.json",
        Path("/opt/data/auth.json"),
        Path.home() / "auth.json",
    ]
    for p in candidates:
        if p.exists() and p.stat().st_size > 10:
            return p
    return None


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
    except Exception:
        return None


def resolve_nous_token(explicit: str | None = None) -> str | None:
    """
    Return a valid Nous JWT.

    Priority: --nous-token arg → EMPORIA_NOUS_JWT env → auth.json (with
    silent refresh if expired). Returns None if no token is available and
    prints a clear message so the caller can warn about key_only mode.
    """
    # 1. Explicit CLI arg
    if explicit:
        return explicit

    # 2. Env var (already refreshed/written by a previous run)
    env_tok = os.getenv("EMPORIA_NOUS_JWT", "").strip()
    if env_tok:
        return env_tok

    # 3. auth.json
    auth_path = _find_auth_json()
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

        print("  Nous token expired and silent refresh failed.")
        print("  Run: hermes auth add nous")
        print("  Then re-run installer to get nous_verified trust level.")
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
    """Create the Hermes profile skeleton (idempotent via existence check)."""
    if _hermes_profile_exists(name):
        return
    try:
        subprocess.run(
            ["hermes", "profile", "create", name],
            check=True, capture_output=True, timeout=15,
        )
        print(f"  Created Hermes profile '{name}'")
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

def load_yaml(path: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load(path.read_text()) or {}
    except ImportError:
        import json
        return json.loads(path.read_text()) if path.suffix == ".json" else {}


def save_yaml(path: Path, data: dict) -> None:
    try:
        import yaml
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    except ImportError:
        path.write_text(json.dumps(data, indent=2))


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


def _model_block(config: dict) -> dict | None:
    return config.get("model")


# ─────────────────────────────────────────────────────────────────────────────
# MCP + platform entry builders
# ─────────────────────────────────────────────────────────────────────────────

def _mcp_entry(relay_url: str, agent_id: str = "", display_name: str = "") -> dict[str, Any]:
    env: dict[str, str] = {
        "EMPORIA_RELAY_URL": relay_url,
        "EMPORIA_MCP_TRANSPORT": "stdio",
    }
    if agent_id:
        env["EMPORIA_AGENT_ID"] = agent_id
    if display_name:
        env["EMPORIA_DISPLAY_NAME"] = display_name
    return {
        "command": _venv_python(),
        "args": ["-m", "emporia.mcp_server"],
        "env": env,
    }


def _platform_entry(relay_url: str, agent_id: str) -> dict[str, Any]:
    return {
        "platform": "emporia",
        "relay_url": relay_url,
        "agent_id": agent_id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Keypair generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_keypair(profile_id: str, dry_run: bool = False) -> tuple[str, str]:
    """Generate or load Ed25519 keypair. Returns (pub_hex, key_path_str)."""
    key_path = Path.home() / ".hermes" / "keys" / f"{profile_id}.priv"
    if dry_run:
        print(f"  [dry-run] Would generate keypair at {key_path}")
        return ("dryrun_pubkey_hex_placeholder", str(key_path))
    from emporia.identity import generate_or_load_keypair
    _, pub_bytes = generate_or_load_keypair(profile_id)
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


def install_into_profile(
    profile_dir: Path,
    relay_url: str,
    agent_id: str,
    stripe_secret_key: str | None = None,
    nous_token: str | None = None,
    no_guardrails: bool = False,
    dry_run: bool = False,
) -> bool:
    config_path = profile_dir / "config.yaml"
    if not config_path.exists():
        print(f"  ERROR: no config.yaml at {config_path}")
        return False

    # Generate keypair first (idempotent)
    pub_hex, _ = generate_keypair(agent_id, dry_run=dry_run)

    config = load_yaml(config_path)
    changed = False

    # MCP entry
    mcp_servers = config.setdefault("mcp_servers", {})
    if MCP_TOOL_NAME not in mcp_servers:
        mcp_servers[MCP_TOOL_NAME] = _mcp_entry(relay_url, agent_id=agent_id)
        changed = True
        print(f"  + mcp_servers.{MCP_TOOL_NAME}  (agent_id={agent_id!r})")
    else:
        # Update agent_id in existing entry without clobbering rest of config
        existing_env = mcp_servers[MCP_TOOL_NAME].get("env", {})
        if agent_id and existing_env.get("EMPORIA_AGENT_ID") != agent_id:
            existing_env["EMPORIA_AGENT_ID"] = agent_id
            mcp_servers[MCP_TOOL_NAME]["env"] = existing_env
            changed = True
            print(f"  ~ mcp_servers.{MCP_TOOL_NAME}.env.EMPORIA_AGENT_ID = {agent_id!r}")

    # Platform entry
    platforms = config.setdefault("platforms", [])
    if not any(p.get("platform") == "emporia" for p in platforms):
        platforms.append(_platform_entry(relay_url, agent_id))
        changed = True
        print("  + platforms.emporia")

    # Env block
    env_block = config.setdefault("env", {})
    if stripe_secret_key and "STRIPE_SECRET_KEY" not in env_block:
        env_block["STRIPE_SECRET_KEY"] = stripe_secret_key
        changed = True
        print("  + env.STRIPE_SECRET_KEY")
    if nous_token and env_block.get("EMPORIA_NOUS_JWT") != nous_token:
        env_block["EMPORIA_NOUS_JWT"] = nous_token
        changed = True
        print("  + env.EMPORIA_NOUS_JWT  (Nous identity verified)")
    if no_guardrails:
        env_block["HERMES_PTGS_GUARDRAILS_MODE"] = "off"
        changed = True
        print("  + env.HERMES_PTGS_GUARDRAILS_MODE=off")

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
    env_vars = harvest_env(profile_dir)
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

    db_path = os.path.expanduser("~/.hermes/emporia.sqlite3")
    env_lines, c1 = _set_env_line(env_lines, "EMPORIA_AGENT_ID", agent_id)
    env_lines, c2 = _set_env_line(env_lines, "EMPORIA_RELAY_URL", relay_url)
    env_lines, _ = _set_env_line(env_lines, "EMPORIA_DB_PATH", db_path)
    if nous_token:
        env_lines, c3 = _set_env_line(env_lines, "EMPORIA_NOUS_JWT", nous_token)
    else:
        c3 = False
    # Export to current process so seed and any downstream scripts see it immediately
    os.environ["EMPORIA_AGENT_ID"] = agent_id
    os.environ["EMPORIA_RELAY_URL"] = relay_url
    os.environ["EMPORIA_DB_PATH"] = db_path
    if nous_token:
        os.environ["EMPORIA_NOUS_JWT"] = nous_token

    if c1 or c2 or c3:
        if not dry_run:
            env_file.write_text("\n".join(env_lines) + "\n")
            print(f"  Updated: {env_file}")
        else:
            print(f"  [dry-run] Would update: {env_file}")

    write_dashboard_env(relay_url, agent_id, dry_run=dry_run)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# --create-profile
# ─────────────────────────────────────────────────────────────────────────────

def create_profile(
    name: str,
    relay_url: str,
    current_profile_dir: Path | None,
    stripe_secret_key: str | None = None,
    nous_token: str | None = None,
    inherit_env: bool = True,
    dry_run: bool = False,
    update_dashboard: bool = False,
    dev_skills: bool = False,
) -> None:
    """Create a new Hermes agent profile with emporia pre-wired."""

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
    pub_hex, _ = generate_keypair(name, dry_run=dry_run)

    # On update, start from the existing config so we don't clobber other settings.
    existing_config: dict[str, Any] = load_yaml(config_path) if updating else {}

    # Inherit model block from current profile (or keep existing)
    model_block: dict | None = existing_config.get("model")
    inherited_env: dict[str, str] = dict(existing_config.get("env", {}))
    if current_profile_dir:
        cur_config = load_yaml(current_profile_dir / "config.yaml")
        if not model_block:
            model_block = _model_block(cur_config)
        if inherit_env:
            env_from_file = harvest_env(current_profile_dir)
            env_from_config = cur_config.get("env", {})
            for k in _SAFE_ENV_KEYS:
                if k not in inherited_env:
                    for src in (env_from_file, env_from_config):
                        if k in src:
                            inherited_env[k] = src[k]
                            break
            for k in _SECRET_ENV_KEYS:
                if k not in inherited_env:
                    for src in (env_from_file, env_from_config):
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
        MCP_TOOL_NAME: _mcp_entry(relay_url, agent_id=name),
    }
    config["platforms"] = [_platform_entry(relay_url, name)]
    config["toolsets"] = ["hermes-cli"]
    config["agent"] = {"max_turns": 150}
    config["terminal"] = {"backend": "local", "home_mode": "auto"}

    # Env block
    env_block: dict[str, str] = {**inherited_env}
    env_block["EMPORIA_RELAY_URL"] = relay_url
    env_block["EMPORIA_AGENT_ID"] = name
    if stripe_secret_key:
        env_block["STRIPE_SECRET_KEY"] = stripe_secret_key
    elif "STRIPE_SECRET_KEY" not in env_block:
        env_block["STRIPE_SECRET_KEY"] = ""
    if nous_token:
        env_block["EMPORIA_NOUS_JWT"] = nous_token
    env_block["EMPORIA_PUBLIC_KEY"] = pub_hex
    config["env"] = env_block

    if dry_run:
        print(f"  [dry-run] Would write {config_path}")
        print(f"  [dry-run] env keys: {list(env_block.keys())}")
    else:
        save_yaml(config_path, config)
        print(f"  Saved: {config_path}")

    # Write .env file
    db_path = os.path.expanduser("~/.hermes/emporia.sqlite3")
    env_path = profile_dir / ".env"
    safe_env_lines = [
        f"EMPORIA_RELAY_URL={relay_url}",
        f"EMPORIA_AGENT_ID={name}",
        f"EMPORIA_DB_PATH={db_path}",
        f"HERMES_AGENT_ID={name}",
    ]
    if nous_token:
        safe_env_lines.append(f"EMPORIA_NOUS_JWT={nous_token}")
    for k in _SECRET_ENV_KEYS:
        if k in env_block and env_block[k] and k != "EMPORIA_NOUS_JWT":
            safe_env_lines.append(f"{k}={env_block[k]}")

    # Export to current process so subsequent steps see the vars immediately
    os.environ["EMPORIA_AGENT_ID"] = name
    os.environ["EMPORIA_RELAY_URL"] = relay_url
    os.environ["EMPORIA_DB_PATH"] = db_path
    if nous_token:
        os.environ["EMPORIA_NOUS_JWT"] = nous_token

    if dry_run:
        print(f"  [dry-run] Would write {env_path}")
    else:
        env_path.write_text("\n".join(safe_env_lines) + "\n")
        print(f"  Saved: {env_path}")

    if update_dashboard:
        write_dashboard_env(relay_url, name, dry_run=dry_run)

    # Install agent skill so it loads on start (dev skills optional)
    _install_skill_link(profile_dir, dry_run=dry_run, dev_skills=dev_skills)

    print(f"\n  Agent ID : {name}")
    print(f"  Relay    : {relay_url}")
    print(f"  Profile  : {profile_dir}")
    if model_block:
        print(f"  Model    : {model_block.get('default', model_block.get('provider', '?'))}")
    print("\n  Next: start Hermes with this profile and run /reload-mcp")
    print(f"  The agent will register automatically as '{name}' on first MCP load.")


# ─────────────────────────────────────────────────────────────────────────────
# --bootstrap-test
# ─────────────────────────────────────────────────────────────────────────────

def _start_relay(relay_url: str) -> bool:
    """Start the relay in the background. Returns True if healthy within 8s."""
    import time
    src = _EMPORIA_DIR / "src"
    port = relay_url.rstrip("/").rsplit(":", 1)[-1] if ":" in relay_url else "8088"
    env = os.environ.copy()
    log = open("/tmp/relay.log", "w")
    subprocess.Popen(
        [_venv_python(), "-m", "uvicorn",
         "emporia.relay_server:app",
         "--host", "0.0.0.0", "--port", port,
         "--app-dir", str(src)],
        cwd=str(_EMPORIA_DIR), env=env, stdout=log, stderr=log,
    )
    for _ in range(8):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"{relay_url}/health", timeout=2)
            print(f"  Relay started on {relay_url}")
            return True
        except Exception:
            pass
    return False


def bootstrap_test_profiles(
    relay_url: str,
    current_profile_dir: Path | None,
    stripe_secret_key: str | None = None,
    nous_token: str | None = None,
    dry_run: bool = False,
    dev_skills: bool = False,
) -> None:
    """Create all demo agent profiles (alpha, beta, nemotron_strategist, stripe_escrow_bot), then seed."""
    for name in ("alpha", "beta", "nemotron_strategist", "stripe_escrow_bot"):
        print(f"\nBootstrapping: {name}")
        create_profile(
            name=name,
            relay_url=relay_url,
            current_profile_dir=current_profile_dir,
            stripe_secret_key=stripe_secret_key,
            nous_token=nous_token,
            inherit_env=True,
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
    seed_script = _EMPORIA_DIR / "scripts" / "seed_demo_relay.py"
    if not seed_script.exists() or dry_run:
        if dry_run:
            print("\n  [dry-run] Would run seed_demo_relay.py")
        return
    print("\nSeeding relay with demo content…")

    # Start relay if not running
    try:
        urllib.request.urlopen(f"{relay_url}/health", timeout=3)
    except Exception:
        print(f"  Relay not running — starting on {relay_url}…")
        if not _start_relay(relay_url):
            print(f"  Relay failed to start — run manually: python {seed_script}")
            return
    result = subprocess.run(
        [_venv_python(), str(seed_script)],
        capture_output=True, text=True,
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


# ─────────────────────────────────────────────────────────────────────────────
# Skill symlink
# ─────────────────────────────────────────────────────────────────────────────

# Optional dev skills (repo → profile relative paths under skills/)
_DEV_SKILL_SYMLINKS: list[tuple[str, str]] = [
    ("dev/emporia-dev", "software-development/emporia-dev"),
    ("dev/srcl-terminal-ui", "creative/srcl-terminal-ui"),
]


def _install_skill_link(
    profile_dir: Path,
    dry_run: bool = False,
    *,
    dev_skills: bool = False,
) -> None:
    """Symlink Emporia skills from the repo into the profile skills dir.

    Profile paths are symlinks to the repo — edits under emporia/skills/ are visible
    to Hermes immediately (no re-copy). Re-run installer to fix broken links after moves.

    Always links the agent skill (`emporia`). Dev skills when dev_skills=True (--dev-skills).
    """
    skills_dir = profile_dir / "skills"
    links: list[tuple[Path, Path]] = [
        (_EMPORIA_DIR / "skills" / "emporia.md", skills_dir / "emporia.md"),
        (_EMPORIA_DIR / "skills" / "emporia", skills_dir / "emporia"),
    ]
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
                        help="Create alpha + beta demo agent profiles")
    parser.add_argument("--relay-url", default=DEFAULT_RELAY_URL,
                        help=f"Relay URL (default: {DEFAULT_RELAY_URL})")
    parser.add_argument("--agent-id", default="",
                        help="Agent ID for --install-profile (default: profile dir name or HERMES_AGENT_ID)")
    parser.add_argument("--display-name", default="",
                        help="Display name for the agent (defaults to agent-id)")
    parser.add_argument("--stripe-secret-key", default=None,
                        help="Stripe secret key to add to config env block")
    parser.add_argument("--nous-token", default=None, metavar="JWT",
                        help="Nous access JWT — stored as EMPORIA_NOUS_JWT; "
                             "enables nous_verified trust level on this relay")
    parser.add_argument("--no-guardrails", action="store_true",
                        help="Set HERMES_PTGS_GUARDRAILS_MODE=off (testing only)")
    parser.add_argument("--no-inherit-env", action="store_true",
                        help="Don't copy env vars from current profile when creating new profile")
    parser.add_argument("--dev-skills", action="store_true",
                        help="Symlink dev skills from repo (emporia-dev, srcl-terminal-ui)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    args = parser.parse_args()

    if not any([args.install_profile, args.create_profile, args.bootstrap_test]):
        parser.print_help()
        return

    print(f"Emporia installer  relay={args.relay_url}")
    if args.dry_run:
        print("DRY RUN — no files will be written")
    print()

    # Resolve Nous JWT once up front — all operations share the same token.
    # Falls back to auth.json with silent refresh; warns if unavailable.
    nous_token = resolve_nous_token(args.nous_token)
    if nous_token:
        print("  Nous token: ready (agents will register as nous_verified)")
    else:
        print("  Nous token: not available — agents will register as key_only (read-only trust)")
        print("  To get nous_verified: run 'hermes auth add nous', then re-run installer")
    print()

    # Detect current profile (needed by all commands)
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

    if args.install_profile:
        if not current_profile_dir:
            print("ERROR: cannot find a config.yaml above CWD.")
            print("Either set HERMES_PROFILE, run from inside a profile dir,")
            print("or run from inside the emporia/ dir that lives in a profile.")
            sys.exit(1)

        # Default agent-id to the profile dir name or env var
        agent_id = (args.agent_id
                    or os.getenv("HERMES_AGENT_ID")
                    or current_profile_dir.name)
        print(f"Installing into: {current_profile_dir}  agent_id={agent_id!r}")

        # Harvest Stripe key from .env if not passed
        stripe_key = args.stripe_secret_key
        if not stripe_key:
            env_vars = harvest_env(current_profile_dir)
            stripe_key = env_vars.get("STRIPE_SECRET_KEY") or None

        install_into_profile(
            profile_dir=current_profile_dir,
            relay_url=args.relay_url,
            agent_id=agent_id,
            stripe_secret_key=stripe_key,
            nous_token=nous_token,
            no_guardrails=args.no_guardrails,
            dry_run=args.dry_run,
        )
        _install_skill_link(
            current_profile_dir,
            dry_run=args.dry_run,
            dev_skills=args.dev_skills,
        )
        if not args.dry_run:
            print("\nDone. Run /reload-mcp in Hermes to activate.")
            print(f"The agent will register as '{agent_id}' on next MCP load.")

    if args.create_profile:
        create_profile(
            name=args.create_profile,
            relay_url=args.relay_url,
            current_profile_dir=current_profile_dir,
            stripe_secret_key=args.stripe_secret_key,
            nous_token=nous_token,
            inherit_env=not args.no_inherit_env,
            dry_run=args.dry_run,
            dev_skills=args.dev_skills,
        )

    if args.bootstrap_test:
        bootstrap_test_profiles(
            relay_url=args.relay_url,
            current_profile_dir=current_profile_dir,
            stripe_secret_key=args.stripe_secret_key,
            nous_token=nous_token,
            dry_run=args.dry_run,
            dev_skills=args.dev_skills,
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
