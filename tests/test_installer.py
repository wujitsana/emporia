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
