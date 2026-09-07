"""Installer tests. `installer/` is not a packaged module (see pyproject.toml's
`packages = ["src/emporia"]`) and has no prior test coverage — load install.py
directly by file path rather than building out a full installer test harness,
which is out of scope for the small, pure function under test here (N10-5)."""

import importlib.util
import sys
from pathlib import Path

_INSTALLER_PATH = Path(__file__).resolve().parent.parent / "installer" / "install.py"
_spec = importlib.util.spec_from_file_location("emporia_installer", _INSTALLER_PATH)
installer = importlib.util.module_from_spec(_spec)
sys.modules["emporia_installer"] = installer
_spec.loader.exec_module(installer)


def test_warn_if_a2a_enabled_prints_warning_when_enabled(capsys):
    """N10-5 (R5): detection only. If gateway.platforms.a2a.enabled is truthy,
    warn about mcp-emporia exposure — do not touch the config."""
    config = {"gateway": {"platforms": {"a2a": {"enabled": True}}}}
    installer._warn_if_a2a_enabled(config)
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "mcp-emporia" in out
    assert config == {"gateway": {"platforms": {"a2a": {"enabled": True}}}}  # untouched


def test_warn_if_a2a_enabled_silent_when_absent(capsys):
    for config in ({}, {"gateway": {}}, {"gateway": {"platforms": {"a2a": {"enabled": False}}}}):
        installer._warn_if_a2a_enabled(config)
    out = capsys.readouterr().out
    assert out == ""


def test_apply_guardrails_env_never_auto_enables_nemo_from_key_presence():
    """NeMo NIM used to auto-enable the moment an NVIDIA_API_KEY was resolved from
    anywhere — including a leftover hackathon-sponsor key nobody meant to keep
    spending, silently re-enabled on every routine install/update re-run. Every
    guardrails-scanned write fans out into a paid NIM API call once it's on, so this
    was a real, unadvertised recurring cost. EMPORIA_NEMO_GUARDRAILS_ENABLED must now
    always land "0" regardless of whether a key is present, while the key itself is
    still stored for a deliberate later opt-in."""
    with_key: dict[str, str] = {}
    installer._apply_guardrails_env(with_key, disabled=False, nvidia_api_key="nvapi-real-key")
    assert with_key["EMPORIA_NEMO_GUARDRAILS_ENABLED"] == "0"
    assert with_key["NVIDIA_API_KEY"] == "nvapi-real-key"

    without_key: dict[str, str] = {}
    installer._apply_guardrails_env(without_key, disabled=False, nvidia_api_key=None)
    assert without_key["EMPORIA_NEMO_GUARDRAILS_ENABLED"] == "0"
    assert "NVIDIA_API_KEY" not in without_key

    # A previously-stale-enabled profile is corrected, not just left alone — this is
    # what fixes an existing install, not only future ones.
    previously_enabled = {"EMPORIA_NEMO_GUARDRAILS_ENABLED": "1", "NVIDIA_API_KEY": "nvapi-old"}
    installer._apply_guardrails_env(previously_enabled, disabled=False, nvidia_api_key="nvapi-old")
    assert previously_enabled["EMPORIA_NEMO_GUARDRAILS_ENABLED"] == "0"


def test_apply_guardrails_env_disabled_forces_mode_off_and_drops_key():
    env_block = {"NVIDIA_API_KEY": "nvapi-old"}
    installer._apply_guardrails_env(env_block, disabled=True)
    assert env_block["EMPORIA_GUARDRAILS_MODE"] == "off"
    assert env_block["EMPORIA_NEMO_GUARDRAILS_ENABLED"] == "0"
    assert "NVIDIA_API_KEY" not in env_block


def test_guardrails_env_lines_never_auto_enables_nemo_from_key_presence():
    lines_with_key = installer._guardrails_env_lines(disabled=False, nvidia_api_key="nvapi-real-key")
    assert "EMPORIA_NEMO_GUARDRAILS_ENABLED=0" in lines_with_key

    lines_without_key = installer._guardrails_env_lines(disabled=False, nvidia_api_key=None)
    assert "EMPORIA_NEMO_GUARDRAILS_ENABLED=0" in lines_without_key


def test_the_installer_writes_no_gateway_platform_entry():
    """P63: `platforms: - platform: emporia` was written into every install and
    did nothing.

    Hermes discovers plugins through the `hermes_agent.plugins` entry point;
    this package declares none, and nothing in the Hermes tree knows the name,
    so the gateway's registry lookup misses and it logs "No adapter for
    'emporia' -- is the plugin installed?". The entry made `config.yaml` claim
    an integration that was not there.

    It is also the *gateway/desktop* half of Hermes's plugin surface — the same
    family as `register_dashboard_auth_provider` and the Telegram/Slack
    handlers — rather than the agent-running half (`register_tool`,
    `register_hook`). Emporia's agent integration is the MCP server, which is
    registered and enabled, and that is deliberately unchanged.

    This guards the deletion: writing the entry back is a decision that should
    come with a real plugin, not a line reappearing in a refactor.
    """
    from pathlib import Path

    src = Path(__file__).parent.parent / "installer" / "install.py"
    text = src.read_text()
    offenders = [
        line.strip() for line in text.splitlines()
        if '"platform": "emporia"' in line
        or ('config["platforms"]' in line and not line.strip().startswith("#"))
    ]
    assert not offenders, (
        "the installer is writing a gateway platform entry again; it is inert "
        f"until a real plugin exists: {offenders}"
    )


def test_no_setup_command_prunes_a_contributors_venv():
    """P64: a bare `uv sync` installs the default set and *removes* everything
    else, including the `dev` and `x402` extras CI installs with
    `uv sync --frozen --extra dev --extra x402`.

    Both the installer and `scripts/local_relay.py` ran one — and
    `local_relay.py`'s runs on every relay start. Measured after a single
    `--local-demo`: `import ruff` and `import eth_account` both gone. The next
    `uv run python -m ruff check` (CI's own lint command) then failed with "No
    module named ruff", with nothing connecting it back to the setup step.

    `--inexact` installs what is needed and leaves the rest alone.
    """
    from pathlib import Path

    root = Path(__file__).parent.parent
    offenders = []
    for rel in ("installer/install.py", "scripts/local_relay.py"):
        text = (root / rel).read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if '"sync"' in line and "--inexact" not in text[
                max(0, text.find(line) - 200): text.find(line) + 400
            ]:
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, (
        f"`uv sync` without --inexact deletes the dev toolchain: {offenders}"
    )


def test_the_seed_is_told_which_relay_to_seed():
    """P64: `_run_demo_seed()` took `relay_url`, used it for the health check,
    and then launched the seed subprocess without it.

    `seed_demo_relay.py` reads `EMPORIA_RELAY_URL` and defaults to
    `localhost:8088`, so `--local-demo --relay-url http://127.0.0.1:8099`
    started a relay on 8099 and seeded whatever answered on 8088. The seed's own
    split-brain guard caught it and reported a database mismatch — the right
    error for the wrong reason: the databases matched, the *relay* was wrong.
    """
    from pathlib import Path

    text = (Path(__file__).parent.parent / "installer" / "install.py").read_text()
    assert 'seed_env["EMPORIA_RELAY_URL"] = relay_url' in text, (
        "the seed subprocess must be told which relay to seed, not left to "
        "default to localhost:8088"
    )
