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
