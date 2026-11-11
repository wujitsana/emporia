"""`emporia relay use` moves the dashboard tab too: its console reads the relay from the tab's own setting."""
from __future__ import annotations

import json

from emporia import cli


def test_relay_use_updates_the_dashboard_tab_setting(tmp_path):
    profile = tmp_path / "alpha"
    (profile / "plugin-data" / "emporia").mkdir(parents=True)
    (profile / "config.yaml").write_text("mcp_servers:\n  emporia:\n    env: {}\n")
    tab = profile / "plugin-data" / "emporia" / "emporia.json"
    tab.write_text(json.dumps({"python": "/x/python", "relay_url": "http://localhost:8088"}))
    tab.chmod(0o600)
    cli._set_agent_relay(profile, "https://relay.example.org")
    data = json.loads(tab.read_text())
    assert data == {"python": "/x/python", "relay_url": "https://relay.example.org"}
    assert oct(tab.stat().st_mode & 0o777) == "0o600"


def test_the_tab_restarts_its_console_when_the_relay_changes():
    src = (cli.REPO / "hermes_plugin" / "emporia" / "dashboard" / "plugin_api.py").read_text()
    assert "console.relay_url == wanted" in src and "console.relay_url = cfg.get(\"relay_url\"" in src
