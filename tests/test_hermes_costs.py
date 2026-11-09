"""Hermes 0.23 price declarations on the MCP entry: the paying tools, priced by the agent's own limits."""
import re
from pathlib import Path

import yaml

from emporia import cli
from emporia.cli import installer

MCP_SERVER = Path(__file__).resolve().parents[1] / "src" / "emporia" / "mcp_server.py"


def test_every_priced_tool_is_a_tool_the_server_offers():
    src = MCP_SERVER.read_text()
    offered = set(re.findall(r"@mcp\.tool\(\)\n(?:@\w+.*\n)*def (\w+)\(", src))
    assert set(installer().MONEY_TOOLS) <= offered


def test_the_declaration_is_the_cap_as_a_range_and_the_daily_limit_per_chat():
    d = installer().mcp_cost_policy({"EMPORIA_X402_MAX_PAYMENT_CENTS": "700", "EMPORIA_X402_MAX_DAILY_CENTS": "2500"})
    assert d["spend_limit"] == 2500 and d["cost_unit"] == "cents"
    assert set(d["tool_costs"]) == set(installer().MONEY_TOOLS)
    for cost in d["tool_costs"].values():
        assert cost["credits"] == [0, 700] and "700" in cost["note"]


def test_the_defaults_are_the_wallets_defaults():
    from emporia import agent_wallet
    d = installer().mcp_cost_policy({})
    assert d["spend_limit"] == agent_wallet.DEFAULT_MAX_DAILY_CENTS
    assert d["tool_costs"]["fund_session"]["credits"][1] == agent_wallet.DEFAULT_MAX_PAYMENT_CENTS


def test_a_zero_or_unreadable_limit_declares_nothing_hermes_could_not_count():
    assert installer().mcp_cost_policy({"EMPORIA_X402_MAX_PAYMENT_CENTS": "0"}) == {}
    assert installer().mcp_cost_policy({"EMPORIA_X402_MAX_DAILY_CENTS": "lots"}) == {}


def test_apply_adds_updates_and_removes_it_without_touching_the_rest():
    inst = installer()
    entry = {"command": "py", "args": ["-m", "emporia.mcp_server"], "env": {"A": "1"}, "tool_costs": {"old": 1}}
    assert inst.apply_mcp_costs(entry, {"EMPORIA_X402_MAX_PAYMENT_CENTS": "300"}) is True
    assert set(entry["tool_costs"]) == set(inst.MONEY_TOOLS) and entry["env"] == {"A": "1"}
    assert inst.apply_mcp_costs(entry, {"EMPORIA_X402_MAX_PAYMENT_CENTS": "300"}) is False     # idempotent
    assert inst.apply_mcp_costs(entry, {"EMPORIA_X402_MAX_PAYMENT_CENTS": "0"}) is True
    assert not {"tool_costs", "spend_limit", "cost_unit"} & set(entry) and entry["command"] == "py"


def test_sync_rewrites_config_yaml_only_when_the_limits_changed(tmp_path, monkeypatch):
    inst = installer()
    profile = tmp_path / "shop"
    profile.mkdir()
    cfg = profile / "config.yaml"
    cfg.write_text(yaml.safe_dump({"model": "m", "mcp_servers": {"emporia": {"command": "py", "env": {}}}}))
    env = {"EMPORIA_X402_MAX_PAYMENT_CENTS": "400", "EMPORIA_X402_MAX_DAILY_CENTS": "900"}
    monkeypatch.setattr(inst, "harvest_env", lambda p: env)
    assert inst.sync_mcp_costs(profile) is True
    data = yaml.safe_load(cfg.read_text())
    assert data["model"] == "m" and data["mcp_servers"]["emporia"]["spend_limit"] == 900
    assert inst.sync_mcp_costs(profile) is False
    env["EMPORIA_X402_MAX_DAILY_CENTS"] = "1200"
    assert inst.sync_mcp_costs(profile) is True
    assert yaml.safe_load(cfg.read_text())["mcp_servers"]["emporia"]["spend_limit"] == 1200


def test_a_profile_without_the_entry_is_left_alone(tmp_path, monkeypatch):
    inst = installer()
    (tmp_path / "config.yaml").write_text("model: m\n")
    monkeypatch.setattr(inst, "harvest_env", lambda p: {})
    assert inst.sync_mcp_costs(tmp_path) is False
    assert (tmp_path / "config.yaml").read_text() == "model: m\n"


def test_a_new_entry_carries_the_prices(tmp_path, monkeypatch):
    inst = installer()
    monkeypatch.setattr(inst, "harvest_env", lambda p: {"EMPORIA_X402_MAX_PAYMENT_CENTS": "250"})
    entry = inst._mcp_entry("http://127.0.0.1:9", agent_id="a", profile_dir=tmp_path)
    assert entry["tool_costs"]["join_room"]["credits"] == [0, 250] and entry["spend_limit"] == 5000
    assert entry["command"] and "env" in entry
