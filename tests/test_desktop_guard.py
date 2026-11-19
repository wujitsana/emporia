"""Desktop control and crypto are kept apart.

Guard A (plugin hook desktop_guard): a session that read an Emporia tool result loses computer_use, the browser-cdp
tools and, with a real browser configured, the other browser tools. Guard B (runtime emporia.desktop_control): a
profile with those tools gets no crypto of Emporia's own, with no override.
"""
import importlib.util
from pathlib import Path

import pytest
import yaml

from emporia import agent_card_cli, agent_wallet, chain_link, desktop_control as dc

PLUGIN = Path(__file__).resolve().parents[1] / "hermes_plugin" / "emporia"
spec = importlib.util.spec_from_file_location("emporia_desktop_guard", PLUGIN / "desktop_guard.py")
dg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dg)
rt = dg.relay_turn

CONFIGS = {
    "none": {"platform_toolsets": {"cli": ["terminal", "file", "web"]}, "browser": {"cdp_url": ""}},
    "computer_use": {"platform_toolsets": {"cli": ["terminal", "computer_use"]}},
    "browser_cdp_toolset": {"platform_toolsets": {"cli": ["terminal", "browser-cdp"]}},
    "real_profile": {"platform_toolsets": {"cli": ["terminal", "browser"]}, "browser": {"use_real_profile": True}},
    "cdp_url": {"platform_toolsets": {"cli": ["browser"]}, "browser": {"cdp_url": "http://127.0.0.1:9222"}},
    "composite_default_real": {"toolsets": ["hermes-cli"], "browser": {"use_real_profile": "true"}},
    "no_list_real": {"browser": {"use_real_profile": True}},
}


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    dg._marked.clear()
    dg._cache.clear()
    rt._origin.clear()
    rt._kind.clear()
    monkeypatch.setattr(rt, "_home", lambda: tmp_path / "home")
    monkeypatch.setattr(rt, "_setting", lambda name: "")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    for k in [k for k in __import__("os").environ if k.startswith("HERMES_SESSION_")]:
        monkeypatch.delenv(k, raising=False)


def _profile(tmp_path, config, name="p"):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    (folder / "config.yaml").write_text(yaml.safe_dump(config))
    return folder


def _use_profile(tmp_path, monkeypatch, config):
    """Point the plugin's home and the runtime's profile at one config."""
    folder = _profile(tmp_path, config, "home")
    monkeypatch.setattr(rt, "_home", lambda: folder)
    dg._cache.clear()
    return folder


def _read(session="s1", tool="mcp__emporia__read_feed"):
    dg.transform_tool_result(tool_name=tool, session_id=session, task_id=session, result="{}")


# ── guard A ───────────────────────────────────────────────────────────────────────────

def test_a_session_that_read_emporia_loses_computer_use_for_the_rest_of_it(tmp_path):
    ids = {"session_id": "s1", "task_id": "s1"}
    assert dg.pre_tool_call(tool_name="computer_use", **ids) is None, "unmarked: allowed"
    _read()
    out = dg.pre_tool_call(tool_name="computer_use", **ids)
    assert out["action"] == "block" and "read other agents' words on Emporia" in out["message"]
    assert "start a new session" in out["message"].lower() and "EMPORIA_DESKTOP_AFTER_RELAY=allow" in out["message"]
    assert dg.pre_tool_call(tool_name="computer_use", **ids)["action"] == "block", "for the rest of the session"
    assert dg.pre_tool_call(tool_name="computer_use", session_id="other", task_id="other") is None, "another session"
    log = (rt.data_dir() / rt.LOG_FILE).read_text()
    assert '"tool":"computer_use"' in log and '"outcome":"refused"' in log


@pytest.mark.parametrize("tool", ["browser_cdp", "browser_dialog"])
def test_the_browser_cdp_tools_are_refused_by_name_in_a_marked_session(tool):
    assert dg.pre_tool_call(tool_name=tool, session_id="s1", task_id="s1") is None
    _read()
    assert dg.pre_tool_call(tool_name=tool, session_id="s1", task_id="s1")["action"] == "block"


def test_a_non_emporia_result_does_not_mark_and_emporia_results_never_change(tmp_path):
    assert dg.transform_tool_result(tool_name="read_file", session_id="s1", task_id="s1", result="x") is None
    assert dg.transform_tool_result(tool_name="mcp__emporia__read_feed", session_id="s1", task_id="s1",
                                    result="x") is None
    assert dg.pre_tool_call(tool_name="computer_use", session_id="s1", task_id="s1")["action"] == "block"
    assert dg.pre_tool_call(tool_name="computer_use", session_id="s2", task_id="s2") is None
    dg.transform_tool_result(tool_name="mcp__emporia_x__run", session_id="s3", task_id="s3")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="s3", task_id="s3") is None, "look-alike server"


def test_other_tools_of_a_marked_session_are_not_touched(tmp_path):
    _read()
    for tool in ("terminal", "read_file", "web_search", "mcp__emporia__post"):
        assert dg.pre_tool_call(tool_name=tool, session_id="s1", task_id="s1") is None


def test_the_plain_browser_is_not_desktop_control_but_a_real_one_is(tmp_path, monkeypatch):
    _read()
    _use_profile(tmp_path, monkeypatch, CONFIGS["none"])
    assert dg.pre_tool_call(tool_name="browser_exec", session_id="s1", task_id="s1") is None
    _use_profile(tmp_path, monkeypatch, CONFIGS["real_profile"])
    for tool in ("browser_exec", "browser_vault", "browser_navigate"):
        assert dg.pre_tool_call(tool_name=tool, session_id="s1", task_id="s1")["action"] == "block", tool
    _use_profile(tmp_path, monkeypatch, CONFIGS["cdp_url"])
    assert dg.pre_tool_call(tool_name="browser_exec", session_id="s1", task_id="s1")["action"] == "block"
    assert dg.pre_tool_call(tool_name="browser_exec", session_id="unmarked", task_id="unmarked") is None


def test_an_unreadable_config_does_not_block_the_generic_browser_tools(tmp_path, monkeypatch):
    _read()
    monkeypatch.setattr(rt, "_home", lambda: tmp_path / "nowhere")
    assert dg.pre_tool_call(tool_name="browser_exec", session_id="s1", task_id="s1") is None
    assert dg.pre_tool_call(tool_name="computer_use", session_id="s1", task_id="s1")["action"] == "block"


def test_the_opt_out_allows_it(monkeypatch):
    _read()
    monkeypatch.setattr(rt, "_setting", lambda name: "allow" if name == dg.SETTING else "")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="s1", task_id="s1") is None
    monkeypatch.setattr(rt, "_setting", lambda name: "block" if name == dg.SETTING else "")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="s1", task_id="s1")["action"] == "block"


def test_a_sub_session_under_a_marked_parent_is_marked(tmp_path):
    _read("parent")
    dg.pre_llm_call(session_id="child", task_id="child", parent_session_id="parent")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="child", task_id="child")["action"] == "block"
    dg.pre_llm_call(session_id="c2", task_id="c2", parent_session_id="clean")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="c2", task_id="c2") is None


def test_relay_started_and_rounds_turns_are_covered_and_unchanged(tmp_path, monkeypatch):
    rt.pre_llm_call(platform="emporia", session_id="r1", task_id="r1")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="r1", task_id="r1")["action"] == "block"
    # relay_turn's own answer for the same turn is unchanged (its message, its setting)
    out = rt.pre_tool_call(tool_name="computer_use", args={}, session_id="r1", task_id="r1")
    assert out["action"] == "block" and "EMPORIA_RELAY_TURN_TOOLS=all" in out["message"]
    # with every tool allowed for relay turns, the desktop is still refused (it read relay content)
    monkeypatch.setattr(rt, "_setting", lambda name: "all" if name == rt.SETTING else "")
    assert rt.pre_tool_call(tool_name="computer_use", args={}, session_id="r1", task_id="r1") is None
    assert dg.pre_tool_call(tool_name="computer_use", session_id="r1", task_id="r1")["action"] == "block"
    rt.pre_llm_call(platform="cli", session_id="p1", task_id="p1", user_message="hello")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="p1", task_id="p1") is None, "a person's turn"


def test_the_hooks_are_registered():
    src = (PLUGIN / "__init__.py").read_text()
    for hook in ("desktop_guard.transform_tool_result", "desktop_guard.pre_llm_call", "desktop_guard.pre_tool_call"):
        assert hook in src


def test_the_setting_is_a_persons_not_an_agents():
    kg = importlib.util.spec_from_file_location("emporia_key_guard", PLUGIN / "key_guard.py")
    mod = importlib.util.module_from_spec(kg)
    kg.loader.exec_module(mod)
    out = mod.pre_tool_call(tool_name="terminal", args={"command": "echo EMPORIA_DESKTOP_AFTER_RELAY=allow >> .env"})
    assert out and out["action"] == "block"


# ── detection ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key, tools", [
    ("none", []),
    ("computer_use", ["computer_use"]),
    ("browser_cdp_toolset", ["browser_cdp", "browser_dialog"]),
    ("real_profile", ["browser_cdp", "browser_dialog", "browser_exec", "browser_vault"]),
    ("cdp_url", ["browser_cdp", "browser_dialog", "browser_exec", "browser_vault"]),
    ("composite_default_real", ["browser_cdp", "browser_dialog", "browser_exec", "browser_vault"]),
    ("no_list_real", ["browser_cdp", "browser_dialog", "browser_exec", "browser_vault"]),
])
def test_detection_from_config_variants(key, tools):
    found = dc.detect(CONFIGS[key], {})
    assert found["known"] and found["tools"] == tools


def test_detection_edge_cases():
    off = {"platform_toolsets": {"cli": ["browser", "computer_use"]}, "agent": {"disabled_toolsets": ["computer_use"]},
           "browser": {"use_real_profile": True}}
    assert "computer_use" not in dc.detect(off, {})["tools"]
    assert dc.detect({"platform_toolsets": {"cli": ["terminal"]}}, {"BROWSER_CDP_URL": "ws://x"})["tools"] == []
    assert dc.detect({"platform_toolsets": {"cli": ["browser"]}}, {"BROWSER_CDP_URL": "ws://x"})["real_browser"]
    for unknown in (None, "not a mapping", []):
        assert dc.detect(unknown) == {"known": False, "tools": [], "real_browser": False, "why": []}


@pytest.mark.parametrize("key", sorted(CONFIGS))
def test_the_plugin_and_the_runtime_read_a_config_the_same_way(key):
    assert dg.detect(CONFIGS[key], {}) == dc.detect(CONFIGS[key], {})
    assert dg.detect(None) == dc.detect(None)


def test_a_missing_or_broken_config_is_unknown_and_blocks_nothing(tmp_path):
    assert dc.detection(tmp_path / "missing")["known"] is False
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "config.yaml").write_text("platform_toolsets: [unclosed")
    assert dc.detection(bad)["known"] is False
    assert dc.refusal("x", bad) == "" and dc.refusal("x", tmp_path / "missing") == ""


# ── guard B ───────────────────────────────────────────────────────────────────────────

def _on(tmp_path, monkeypatch, key="computer_use"):
    folder = _profile(tmp_path, CONFIGS[key], "agent")
    monkeypatch.setattr(dc, "profile_home", lambda profile=None: folder)
    return folder


def test_the_refusal_names_the_reason_and_the_way_out(tmp_path, monkeypatch):
    _on(tmp_path, monkeypatch)
    why = dc.refusal("the money feature")
    assert "does no crypto here" in why and "--signature" in why and "computer_use" in why
    assert "control the desktop or a real browser" in why


@pytest.mark.parametrize("call", [
    lambda: agent_wallet.sign_exact("a", {}),
    lambda: agent_wallet.sign_entry("a", {}),
    lambda: agent_wallet.pay_tempo("a", {}),
    lambda: agent_wallet.sign_escrow("a", {}, expected_amount=1),
    lambda: agent_wallet.reclaim_escrow("a", {}),
])
def test_every_payment_path_refuses_at_use_time(tmp_path, monkeypatch, call):
    _on(tmp_path, monkeypatch, "real_profile")
    with pytest.raises(agent_wallet.AgentWalletError, match="does no crypto here"):
        call()


def test_a_payment_in_a_profile_without_desktop_control_is_not_refused_by_this_guard(tmp_path, monkeypatch):
    folder = _profile(tmp_path, CONFIGS["none"], "agent")
    monkeypatch.setattr(dc, "profile_home", lambda profile=None: folder)
    agent_wallet.no_desktop_control("x")               # no raise
    monkeypatch.setattr(dc, "profile_home", lambda profile=None: None)      # unknown
    agent_wallet.no_desktop_control("x")


def test_the_signers_scoped_calls_are_not_asked(tmp_path, monkeypatch):
    _on(tmp_path, monkeypatch)
    with agent_wallet.scope(agent_wallet.Scope(env={}, key_dir=tmp_path)):
        agent_wallet.no_desktop_control("x")


@pytest.mark.parametrize("call", [
    lambda: chain_link.person_only("sending from the agent's own wallet"),
    lambda: chain_link.person_only("signing with the agent's own wallet"),
])
def test_signs_and_sends_from_the_agents_own_wallet_refuse(tmp_path, monkeypatch, call):
    _on(tmp_path, monkeypatch)
    with pytest.raises(chain_link.LinkError, match="does no crypto here"):
        call()
    folder = _profile(tmp_path, CONFIGS["none"], "clean")
    monkeypatch.setattr(dc, "profile_home", lambda profile=None: folder)
    call()                                                       # no desktop control: a person's terminal is fine


class _T:
    found = True

    def unattended(self):
        return ""


def test_link_and_register_8004_refuse_browser_and_agent_wallet_but_not_signature(tmp_path, monkeypatch, capsys):
    _on(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_card_cli, "_link_target", lambda cmd, name: _T())
    monkeypatch.setattr(agent_card_cli, "_agent_session", lambda: False)
    assert agent_card_cli.link_8004("a", "base-sepolia", "1", browser=True) == 1
    assert "does no crypto here" in capsys.readouterr().out
    assert agent_card_cli.link_8004("a", "base-sepolia", "1", from_agent_wallet=True) == 1
    assert "does no crypto here" in capsys.readouterr().out
    assert agent_card_cli.register_8004("a", "base-sepolia", browser=True) == 1
    assert "does no crypto here" in capsys.readouterr().out
    assert agent_card_cli.register_8004("a", "base-sepolia", from_agent_wallet=True, max_gas=5) == 1
    assert "does no crypto here" in capsys.readouterr().out
    # signed elsewhere stays: it goes on to the next check (the signer and time are asked for)
    assert agent_card_cli.link_8004("a", "base-sepolia", "1", signature="0xabc") == 1
    out = capsys.readouterr().out
    assert "with --signature give --signer" in out and "does no crypto here" not in out


def _features_args(**kw):
    import argparse
    return argparse.Namespace(**{"dir": "", "agent": "p", "action": "on", "names": ["money"], **kw})


def test_features_on_money_is_refused_with_desktop_control_and_off_still_works(tmp_path, monkeypatch, capsys):
    from emporia import cli

    folder = _on(tmp_path, monkeypatch)

    class Inst:
        def harvest_env(self, profile):
            return {"EMPORIA_FEATURES": "core"}

    monkeypatch.setattr(cli, "installer", lambda: Inst())
    monkeypatch.setattr(cli, "_own_profile", lambda args: (folder, "p"))
    assert cli.cmd_features(_features_args()) == 1
    assert "does no crypto here" in capsys.readouterr().out
    assert cli.cmd_features(_features_args(action="")) == 0, "listing is read-only"


def test_a_clean_profile_may_turn_money_on(tmp_path, monkeypatch):
    from emporia import cli

    folder = _profile(tmp_path, CONFIGS["none"], "clean")
    monkeypatch.setattr(dc, "profile_home", lambda profile=None: Path(profile) if profile else folder)
    saved = []

    class Inst:
        def harvest_env(self, profile):
            return {"EMPORIA_FEATURES": "core"}

        def refresh_mcp_references(self, profile):
            return []

        def sync_mcp_costs(self, profile):
            return None

    monkeypatch.setattr(cli, "installer", lambda: Inst())
    monkeypatch.setattr(cli, "_own_profile", lambda args: (folder, "p"))
    monkeypatch.setattr(cli, "merge_env_file", lambda path, values: saved.append(values))
    assert cli.cmd_features(_features_args()) == 0
    assert saved and "money" in saved[0]["EMPORIA_FEATURES"]


# ── doctor and setup's closing block ──────────────────────────────────────────────────

class _Rep:
    def __init__(self):
        self.lines = []

    def ok(self, msg):
        self.lines.append(("ok", msg))

    def warn(self, msg, fix=""):
        self.lines.append(("warn", msg))

    def fail(self, msg, fix=""):
        self.lines.append(("fail", msg))


def test_doctor_names_the_tools_the_refusals_and_the_wallet_rule(tmp_path):
    from emporia import cli

    rep = _Rep()
    cli.doctor_desktop_control(rep, tmp_path / "p", {}, CONFIGS["none"])
    assert rep.lines[0][0] == "ok" and "real funds should never sit in a browser or desktop" in rep.lines[0][1]

    rep = _Rep()
    cli.doctor_desktop_control(rep, tmp_path / "p", {"EMPORIA_FEATURES": "core"}, CONFIGS["real_profile"])
    level, text = rep.lines[0]
    assert level == "warn" and "browser_exec" in text and "Refused now" in text and "real funds" in text

    rep = _Rep()                                   # money already on: danger
    cli.doctor_desktop_control(rep, tmp_path / "p", {"EMPORIA_FEATURES": "core,money"}, CONFIGS["computer_use"])
    assert rep.lines[0][0] == "fail" and rep.lines[0][1].startswith("DANGER") and "computer_use" in rep.lines[0][1]

    rep = _Rep()                                   # unknown is said, not blocked
    cli.doctor_desktop_control(rep, tmp_path / "nowhere", {}, None)
    assert rep.lines[0][0] == "warn" and "unknown" in rep.lines[0][1]

    rep = _Rep()
    cli.doctor_desktop_control(rep, tmp_path / "p", {"EMPORIA_DESKTOP_AFTER_RELAY": "allow"}, CONFIGS["none"])
    assert any(level == "warn" and "EMPORIA_DESKTOP_AFTER_RELAY=allow" in text for level, text in rep.lines)


def test_the_closing_block_line_comes_from_the_same_reading(tmp_path):
    level, text = dc.doctor_line(dc.detect(CONFIGS["computer_use"]))
    assert level == "danger" and "computer_use" in text and "ERC-8004" in text
    assert dc.doctor_line(dc.detect(CONFIGS["none"]))[0] == "ok"
    assert dc.doctor_line(dc.detect(None))[0] == "warn"
    assert "doctor_line" in (Path(__file__).resolve().parents[1] / "src" / "emporia" / "cli.py").read_text()


def test_the_setting_is_documented_and_declared():
    repo = Path(__file__).resolve().parents[1]
    assert "EMPORIA_DESKTOP_AFTER_RELAY" in (repo / "docs" / "environment.md").read_text()
    manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    assert "EMPORIA_DESKTOP_AFTER_RELAY" in {e["name"] for e in manifest["optional_env"]}
