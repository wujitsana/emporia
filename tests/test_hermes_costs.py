"""Hermes 0.23 price declarations on the MCP entry: the paying tools, priced by the agent's own limits."""
import re
from pathlib import Path

import yaml

from emporia.cli import installer

MCP_SERVER = Path(__file__).resolve().parents[1] / "src" / "emporia" / "mcp_server.py"


def test_every_priced_tool_is_a_tool_the_server_offers():
    src = MCP_SERVER.read_text()
    offered = set(re.findall(r"@mcp\.tool\(\)\n(?:@\w+.*\n)*def (\w+)\(", src))
    assert set(installer().MONEY_TOOLS) <= offered


def test_the_declaration_is_the_cap_as_a_range_and_the_daily_limit_per_chat():
    d = installer().mcp_cost_policy({"EMPORIA_X402_MAX_PAYMENT_CENTS": "700", "EMPORIA_X402_MAX_DAILY_CENTS": "2500",
                                     "EMPORIA_FEATURES": "money"})
    assert d["spend_limit"] == 2500 and d["cost_unit"] == "cents"
    assert set(d["tool_costs"]) == set(installer().MONEY_TOOLS)
    for cost in d["tool_costs"].values():
        assert cost["credits"] == [0, 700] and "700" in cost["note"]


def test_the_defaults_are_the_wallets_defaults():
    from emporia import agent_wallet
    d = installer().mcp_cost_policy({"EMPORIA_FEATURES": "money"})
    assert d["spend_limit"] == agent_wallet.DEFAULT_MAX_DAILY_CENTS
    assert d["tool_costs"]["fund_session"]["credits"][1] == agent_wallet.DEFAULT_MAX_PAYMENT_CENTS


def test_a_zero_or_unreadable_limit_declares_nothing_hermes_could_not_count():
    assert installer().mcp_cost_policy({"EMPORIA_X402_MAX_PAYMENT_CENTS": "0"}) == {}
    assert installer().mcp_cost_policy({"EMPORIA_X402_MAX_DAILY_CENTS": "lots"}) == {}


def test_apply_adds_updates_and_removes_it_without_touching_the_rest():
    inst = installer()
    entry = {"command": "py", "args": ["-m", "emporia.mcp_server"], "env": {"A": "1"}, "tool_costs": {"old": 1}}
    env = {"EMPORIA_X402_MAX_PAYMENT_CENTS": "300", "EMPORIA_FEATURES": "money"}
    assert inst.apply_mcp_costs(entry, env) is True
    assert set(entry["tool_costs"]) == set(inst.MONEY_TOOLS) and entry["env"] == {"A": "1"}
    assert inst.apply_mcp_costs(entry, env) is False     # idempotent
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


def test_the_prices_name_only_tools_the_features_leave_on():
    """Hermes warns about a priced tool the server does not offer: `fund_session` belongs to the money feature."""
    inst = installer()
    from emporia import features as F
    for env in ({}, {"EMPORIA_FEATURES": "none"}, {"EMPORIA_FEATURES": "federation"}):
        d = inst.mcp_cost_policy(env)
        assert set(d["tool_costs"]) == {"join_room", "subscribe_agora_topic"}, env
        assert not set(d["tool_costs"]) & F.hidden_by_features(F.enabled_from(env)), env
    assert set(inst.mcp_cost_policy({"EMPORIA_FEATURES": "money"})["tool_costs"]) == set(inst.MONEY_TOOLS)
    # a wallet in the agent's settings implies money, as it does for the server
    assert "fund_session" in inst.mcp_cost_policy({"EMPORIA_X402_EVM_PRIVATE_KEY": "x"})["tool_costs"]
    # read-only: the paying tools are all off
    assert inst.mcp_cost_policy({"EMPORIA_FEATURES": "money,readonly"}) == {}


def test_a_features_change_resyncs_the_prices(tmp_path, monkeypatch):
    inst = installer()
    profile = tmp_path / "shop"
    profile.mkdir()
    cfg = profile / "config.yaml"
    cfg.write_text(yaml.safe_dump({"mcp_servers": {"emporia": {"command": "py", "env": {}}}}))
    env = {"EMPORIA_FEATURES": "none"}
    monkeypatch.setattr(inst, "harvest_env", lambda p: env)
    inst.sync_mcp_costs(profile)
    assert "fund_session" not in yaml.safe_load(cfg.read_text())["mcp_servers"]["emporia"]["tool_costs"]
    env["EMPORIA_FEATURES"] = "money"
    assert inst.sync_mcp_costs(profile) is True
    assert "fund_session" in yaml.safe_load(cfg.read_text())["mcp_servers"]["emporia"]["tool_costs"]


def test_the_features_from_the_entrys_argument_is_an_info_line_not_a_warning(monkeypatch, caplog):
    """The normal path for a default install: the entry's --features= argument stands in for an unset setting."""
    import logging
    import sys

    from emporia import features, mcp_server
    monkeypatch.setattr(sys, "argv", ["x", "--features=none"])
    monkeypatch.delenv("EMPORIA_FEATURES", raising=False)
    monkeypatch.setattr(mcp_server, "_check_features_arg", lambda: None)
    monkeypatch.setattr(mcp_server, "_fit_tools_to_relays", lambda **k: set())
    monkeypatch.setattr(mcp_server, "_start_in_background", lambda *a, **k: None)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **k: None)
    monkeypatch.setenv("EMPORIA_MCP_TRANSPORT", "stdio")
    with caplog.at_level(logging.DEBUG):
        mcp_server.main()
    monkeypatch.delenv("EMPORIA_FEATURES", raising=False)
    rec = [r for r in caplog.records if r.getMessage() == "features_from_argument"]
    assert len(rec) == 1 and rec[0].levelno == logging.INFO
