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


def _all(lists):
    """A platform_toolsets mapping that saves a list for every platform the agent runs on."""
    return {p: list(lists) for p in ("cli", "cron", "emporia")}


CONFIGS = {
    "none": {"platform_toolsets": _all(["terminal", "file", "web"]), "browser": {"cdp_url": ""}},
    "computer_use": {"platform_toolsets": _all(["terminal", "computer_use"])},
    "browser_cdp_toolset": {"platform_toolsets": _all(["terminal", "browser-cdp"])},
    "real_profile": {"platform_toolsets": _all(["terminal", "browser"]), "browser": {"use_real_profile": True}},
    "cdp_url": {"platform_toolsets": _all(["browser"]), "browser": {"cdp_url": "http://127.0.0.1:9222"}},
    "composite_default_real": {"toolsets": ["hermes-cli"], "browser": {"use_real_profile": "true"}},
    "no_list_real": {"browser": {"use_real_profile": True}},
}


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    dg._marked.clear()
    dg._parent.clear()
    dg._children.clear()
    dg._cache.clear()
    dg._loaded_stamp = None
    monkeypatch.setattr(dg, "_live_override", lambda: False)
    for name in ("BU_CDP_WS", "BU_CDP_URL", "BROWSER_CDP_URL", "EMPORIA_DESKTOP_TOOLS"):
        monkeypatch.delenv(name, raising=False)
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
    found = dc.detect(CONFIGS[key], {}, cua=False)
    assert found["known"] and found["tools"] == tools


def test_detection_edge_cases():
    off = {"platform_toolsets": _all(["browser", "computer_use"]), "agent": {"disabled_toolsets": ["computer_use"]},
           "browser": {"use_real_profile": True}}
    assert "computer_use" not in dc.detect(off, {}, cua=True)["tools"]
    assert dc.detect({"platform_toolsets": _all(["terminal"])}, {"BROWSER_CDP_URL": "ws://x"}, cua=True)["tools"] == []
    assert dc.detect({"platform_toolsets": _all(["browser"])}, {"BROWSER_CDP_URL": "ws://x"})["real_browser"]
    for unknown in (None, "not a mapping", []):
        assert dc.detect(unknown) == {"known": False, "tools": [], "real_browser": False, "why": []}


@pytest.mark.parametrize("key", sorted(CONFIGS))
def test_the_plugin_and_the_runtime_read_a_config_the_same_way(key):
    for cua in (True, False):
        assert dg.detect(CONFIGS[key], {}, cua=cua) == dc.detect(CONFIGS[key], {}, cua=cua)
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
    profile = Path("named-profile")

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


# ── the review round: defaults, other platforms, environment, live overrides ──────────

def test_computer_use_in_the_default_set_counts_only_where_a_driver_is_present():
    composite = {"toolsets": ["hermes-cli"]}
    assert dc.detect(composite, {}, cua=True)["tools"] == ["computer_use"]
    assert dc.detect(composite, {}, cua=False)["tools"] == []
    assert dc.detect({"platform_toolsets": {"cli": ["hermes-cli"]}}, {}, cua=True)["tools"] == ["computer_use"]
    assert dc.detect({}, {}, cua=True)["tools"] == ["computer_use"], "no saved list at all: the default set"
    # a profile that saved a list for every platform it runs on, without computer_use, has none, driver or not
    assert dc.detect({"platform_toolsets": _all(["terminal"])}, {}, cua=True)["tools"] == []
    # another computer-use backend is its own check: counted
    assert dc.detect({"computer_use": {"backend": "other"}}, {}, cua=False)["tools"] == ["computer_use"]


def test_a_saved_list_for_one_platform_leaves_the_others_on_their_defaults():
    only_cli = {"platform_toolsets": {"cli": ["web", "terminal"]}}
    assert dc.detect(only_cli, {}, cua=True)["tools"] == ["computer_use"]
    assert dc.detect(only_cli, {}, cua=False)["tools"] == []
    assert dc.detect({"platform_toolsets": {"cli": ["web"], "cron": ["web"]}}, {}, cua=True)["tools"] == ["computer_use"]
    both = {"platform_toolsets": {**_all(["web"]), "telegram": ["web"]}}
    assert dc.detect(both, {}, cua=True)["tools"] == []
    real = {**only_cli, "browser": {"use_real_profile": True}}
    assert "browser_exec" in dc.detect(real, {}, cua=False)["tools"]


def test_list_literal_strings_are_read_as_hermes_reads_them():
    literal = {"platform_toolsets": {p: "['web', 'computer_use']" for p in ("cli", "cron", "emporia")}}
    assert dc.detect(literal, {}, cua=False)["tools"] == ["computer_use"]
    plain = {"platform_toolsets": {p: "['web']" for p in ("cli", "cron", "emporia")}}
    assert dc.detect(plain, {}, cua=True)["tools"] == []
    off = {"platform_toolsets": _all(["computer_use"]), "agent": {"disabled_toolsets": "['computer_use']"}}
    assert dc.detect(off, {}, cua=True)["tools"] == []
    bad = {"platform_toolsets": {p: "['web'" for p in ("cli", "cron", "emporia")}}      # unparseable: not a saved list
    assert dc.detect(bad, {}, cua=True)["tools"] == ["computer_use"]
    for evil in ("[__import__('os').system('x')]", "[" * 5000):
        assert dc.detect({"platform_toolsets": {"cli": evil}}, {}, cua=False)["known"]


@pytest.mark.parametrize("name", ["BROWSER_CDP_URL", "BU_CDP_WS", "BU_CDP_URL"])
def test_cdp_environment_names_make_the_browser_real(name):
    cfg = {"platform_toolsets": _all(["browser"])}
    assert dc.detect(cfg, {}, cua=False)["tools"] == []
    assert "browser_exec" in dc.detect(cfg, {name: "ws://127.0.0.1:9222"}, cua=False)["tools"]
    assert dg.detect(cfg, {name: "x"}, cua=False) == dc.detect(cfg, {name: "x"}, cua=False)


def test_the_runtime_asks_the_profiles_env_by_name_and_only_those(tmp_path, monkeypatch):
    """An MCP server's own environment is filtered, so the .env is asked: booleans for the CDP names, the one
    list setting by value, nothing else."""
    from emporia import profile_env as pe
    asked = {}

    def present(path, names):
        asked["present"] = list(names)
        return {n: n == "BU_CDP_WS" for n in names}

    def read_names(path, names):
        asked["read"] = list(names)
        return {"EMPORIA_DESKTOP_TOOLS": "mcp__mydriver__"}

    monkeypatch.setattr(pe, "present", present)
    monkeypatch.setattr(pe, "read_names", read_names)
    assert dc.read_profile_env_names(tmp_path) == (["BU_CDP_WS"], "mcp__mydriver__")
    assert asked == {"present": ["BROWSER_CDP_URL", "BU_CDP_WS", "BU_CDP_URL"], "read": ["EMPORIA_DESKTOP_TOOLS"]}
    monkeypatch.setattr(dc, "_env_names", dc.read_profile_env_names)
    monkeypatch.setattr(dc, "profile_home", lambda profile=None: tmp_path)
    env = dc.profile_env(tmp_path)
    assert env["BU_CDP_WS"] == "set" and env["EMPORIA_DESKTOP_TOOLS"].endswith("mcp__mydriver__")
    monkeypatch.setattr(pe, "present", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no hermes")))
    assert dc.read_profile_env_names(tmp_path) == ([], "")


def test_a_cdp_name_in_the_profile_env_flows_into_detection(tmp_path, monkeypatch):
    folder = _profile(tmp_path, {"platform_toolsets": _all(["browser"])}, "agent")
    monkeypatch.setattr(dc, "profile_home", lambda profile=None: folder)
    assert dc.refusal("x") == ""
    monkeypatch.setattr(dc, "_env_names", lambda home: (["BROWSER_CDP_URL"], ""))
    assert "browser_exec" in dc.refusal("x")


def test_the_runtime_finds_the_agents_profile_from_the_dotenv_it_was_handed(tmp_path, monkeypatch):
    spec_ = importlib.util.spec_from_file_location(                  # a copy without the conftest patches
        "emporia_dc_real", Path(__file__).resolve().parents[1] / "src" / "emporia" / "desktop_control.py")
    real = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(real)
    folder = _profile(tmp_path, CONFIGS["none"], "named")
    monkeypatch.setenv("EMPORIA_DOTENV", str(folder / ".env"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert real.profile_home() == folder
    assert real.profile_home(tmp_path / "x") == tmp_path / "x"


def test_person_only_and_8004_check_the_named_profile_not_the_one_they_run_in(tmp_path, monkeypatch, capsys):
    bad = _profile(tmp_path, CONFIGS["computer_use"], "bad")
    good = _profile(tmp_path, CONFIGS["none"], "good")
    monkeypatch.setattr(dc, "profile_home", lambda profile=None: Path(profile) if profile else good)
    with pytest.raises(chain_link.LinkError, match="does no crypto here"):
        chain_link.person_only("sending", bad)
    chain_link.person_only("sending", good)
    chain_link.person_only("sending")
    t = _T()
    t.profile = bad
    monkeypatch.setattr(agent_card_cli, "_link_target", lambda cmd, name: t)
    monkeypatch.setattr(agent_card_cli, "_agent_session", lambda: False)
    assert agent_card_cli.link_8004("a", "base-sepolia", "1", browser=True) == 1
    assert "does no crypto here" in capsys.readouterr().out
    t.profile = good
    assert agent_card_cli._refuse_with_desktop_control("link-8004", "x", t) is False
    t.profile = None                      # an agent Hermes does not run: nothing to read
    assert agent_card_cli._refuse_with_desktop_control("link-8004", "x", t) is False


def test_money_is_not_turned_on_by_a_template(tmp_path, monkeypatch):
    from emporia import cli, features as F

    folder = _on(tmp_path, monkeypatch)
    merged = []
    monkeypatch.setattr(cli, "merge_env_file", lambda path, values: merged.append(values))

    class Inst:
        def harvest_env(self, profile):
            return {"EMPORIA_FEATURES": "core"}

        def refresh_mcp_references(self, profile):
            return []

    monkeypatch.setattr(cli, "installer", lambda: Inst())
    monkeypatch.setattr("emporia.templates.features_for", lambda key, raw, record, profile=None: ("money,labs", None))
    try:
        cli._template_apply(folder, "plain", rounds=False, pname="p")
    except Exception:       # noqa: BLE001 — whatever follows the write is not this test's business
        pass
    written = [m["EMPORIA_FEATURES"] for m in merged if "EMPORIA_FEATURES" in m]
    assert written and "money" not in F.parse(written[0])


def test_features_on_money_for_a_key_dir_agent_follows_its_own_declared_tools(tmp_path, monkeypatch, capsys):
    from emporia import agent_init, cli

    monkeypatch.setattr(agent_init, "settings_of", lambda kd: {"EMPORIA_AGENT_ID": "a", "EMPORIA_FEATURES": "core",
                                                                "EMPORIA_DESKTOP_TOOLS": "mcp__mydriver__"})
    assert cli.cmd_features(_features_args(dir=str(tmp_path), agent="")) == 1
    assert "does no crypto here" in capsys.readouterr().out
    assert dc.declared_refusal("x", {}) == ""


def test_mcp_servers_named_like_drivers_and_the_persons_list_count():
    cfg = {"platform_toolsets": _all(["web"]), "mcp_servers": {"playwright": {}, "notes": {}, "chrome-devtools": {
        "enabled": False}, "Desktop_Commander": {}}}
    assert dc.detect(cfg, {}, cua=False)["tools"] == ["mcp__playwright__*", "mcp__Desktop_Commander__*"]
    assert dc.detect({"platform_toolsets": _all(["web"])}, {"EMPORIA_DESKTOP_TOOLS": "mcp__x__, my_tool"},
                     cua=False)["tools"] == ["mcp__x__", "my_tool"]
    assert dg.detect(cfg, {}, cua=False) == dc.detect(cfg, {}, cua=False)


def test_the_plugin_copy_of_the_shared_block_is_the_runtimes_verbatim():
    repo = Path(__file__).resolve().parents[1]
    rt_src = (repo / "src" / "emporia" / "desktop_control.py").read_text()
    pl_src = (PLUGIN / "desktop_guard.py").read_text()
    consts = rt_src[rt_src.index("COMPUTER_TOOLS = "):rt_src.index("REFUSED = (")].rstrip()
    funcs = rt_src[rt_src.index("def _list_literal"):rt_src.index("def profile_home")].rstrip()
    assert consts in pl_src and funcs in pl_src


# ── guard A, the review round ─────────────────────────────────────────────────────────

def test_the_mark_is_kept_on_the_conversation_across_a_rotated_session_id():
    dg.transform_tool_result(tool_name="mcp__emporia__read_feed", session_id="s1", task_id="t1", conversation_id="c1")
    out = dg.pre_tool_call(tool_name="computer_use", session_id="s2-after-compression", task_id="t9",
                           conversation_id="c1")
    assert out["action"] == "block"
    assert dg.pre_tool_call(tool_name="computer_use", session_id="s5", conversation_id="other") is None


def test_post_compaction_copies_the_mark_to_the_new_session():
    _read("old")
    dg.post_compaction(session_id="new", old_session_id="old")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="new", task_id="new")["action"] == "block"
    dg.post_compaction(session_id="n2", old_session_id="clean")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="n2", task_id="n2") is None


def test_delegation_does_not_launder_the_mark_upward():
    dg.subagent_start(parent_session_id="parent", child_session_id="child")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="parent", task_id="parent") is None
    _read("child")                                       # the child reads Emporia
    assert dg.pre_tool_call(tool_name="computer_use", session_id="parent", task_id="parent")["action"] == "block"
    # the same through the stop hook, when the child was marked before it was linked
    dg._marked.clear()
    dg._parent.clear()
    dg._children.clear()
    _read("kid")
    dg.subagent_stop(parent_session_id="p2", child_session_id="kid")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="p2", task_id="p2")["action"] == "block"
    # and through a delegate_task result when a recorded child is marked
    dg._marked.clear()
    dg._children["p3"] = {"kid3"}
    dg._marked["kid3"] = 1.0
    assert dg.transform_tool_result(tool_name="delegate_task", session_id="p3", task_id="p3", result="{}") is None
    assert dg.pre_tool_call(tool_name="computer_use", session_id="p3", task_id="p3")["action"] == "block"


def test_a_marked_parent_marks_its_children_through_pre_llm_call():
    _read("parent")
    dg.pre_llm_call(session_id="child", task_id="child", parent_session_id="parent")
    assert dg.pre_tool_call(tool_name="computer_use", session_id="child", task_id="child")["action"] == "block"


def test_marks_survive_a_restart_and_old_ones_are_pruned():
    import json
    import time
    _read("s1")
    assert (rt.data_dir() / dg.MARKS_FILE).exists()
    dg._marked.clear()
    dg._loaded_stamp = None                         # a new process
    assert dg.pre_tool_call(tool_name="computer_use", session_id="s1", task_id="s1")["action"] == "block"
    path = rt.data_dir() / dg.MARKS_FILE
    path.write_text(json.dumps({"ancient": time.time() - dg.MARK_TTL - 5, "fresh": time.time()}))
    dg._marked.clear()
    dg._loaded_stamp = None
    assert dg.is_marked("fresh") and not dg.is_marked("ancient")


DRIVER_CALLS = (("browser_vault", {}), ("cronjob_manage", {"action": "create", "prompt": "x"}),
                ("cronjob_manage", {"action": "update"}),
                ("terminal", {"command": "osascript -e 'tell app \"System Events\"'"}),
                ("terminal", {"command": "xdotool click 1"}), ("terminal", {"command": "ydotool key 28"}),
                ("terminal", {"command": "cua-driver mcp"}),
                ("terminal", {"command": "curl localhost:9222/json/version"}),
                ("terminal", {"command": "chrome --remote-debugging-port=9222"}),
                ("terminal", {"command": "wscat -c ws://x/devtools/browser/abc"}))


def test_the_vault_scheduled_jobs_and_driver_commands_are_refused_in_a_marked_session(tmp_path, monkeypatch):
    _use_profile(tmp_path, monkeypatch, CONFIGS["none"])        # a sandboxed browser: only the vault is special
    ids = {"session_id": "s1", "task_id": "s1"}
    for tool, args in DRIVER_CALLS:
        assert dg.pre_tool_call(tool_name=tool, args=args, **ids) is None, f"unmarked: {tool} {args}"
    _read()
    for tool, args in DRIVER_CALLS:
        out = dg.pre_tool_call(tool_name=tool, args=args, **ids)
        assert out and out["action"] == "block", f"{tool} {args}"
    for tool, args in (("cronjob_manage", {"action": "list"}), ("terminal", {"command": "ls -la && git status"}),
                       ("terminal", {"command": "echo xdotoolbox"})):
        assert dg.pre_tool_call(tool_name=tool, args=args, **ids) is None, f"{tool} {args}"


def test_live_connect_and_bu_environment_make_the_browser_real(tmp_path, monkeypatch):
    _read()
    _use_profile(tmp_path, monkeypatch, CONFIGS["none"])
    assert dg.pre_tool_call(tool_name="browser_exec", session_id="s1", task_id="s1") is None
    monkeypatch.setattr(dg, "_live_override", lambda: True)
    assert dg.pre_tool_call(tool_name="browser_exec", session_id="s1", task_id="s1")["action"] == "block"
    monkeypatch.setattr(dg, "_live_override", lambda: False)
    monkeypatch.setenv("BU_CDP_WS", "ws://127.0.0.1:9222")
    assert dg.pre_tool_call(tool_name="browser_navigate", session_id="s1", task_id="s1")["action"] == "block"
    monkeypatch.delenv("BU_CDP_WS")
    monkeypatch.setattr(rt, "_setting", lambda name: "http://x" if name == "BROWSER_CDP_URL" else "")
    assert dg.pre_tool_call(tool_name="browser_navigate", session_id="s1", task_id="s1")["action"] == "block"


def test_the_live_override_is_asked_of_the_module_hermes_loaded_never_imported(monkeypatch):
    import sys
    import types
    real_live = importlib.util.module_from_spec(importlib.util.spec_from_file_location(
        "emporia_dg_live", PLUGIN / "desktop_guard.py"))
    real_live.__spec__.loader.exec_module(real_live)          # a copy without the fixture's stub
    mod = types.ModuleType("tools.browser_session_cdp")
    mod.get_cdp_override_raw = lambda: "ws://live"
    monkeypatch.setitem(sys.modules, "tools.browser_session_cdp", mod)
    assert real_live._live_override() is True
    mod.get_cdp_override_raw = lambda: ""
    assert real_live._live_override() is False
    mod.get_cdp_override_raw = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    assert real_live._live_override() is False
    monkeypatch.delitem(sys.modules, "tools.browser_session_cdp")
    assert real_live._live_override() is False
    src = (PLUGIN / "desktop_guard.py").read_text()
    assert "import tools" not in src and "from tools" not in src


def test_mcp_driver_servers_and_the_persons_list_are_desktop_control_in_a_marked_session(monkeypatch):
    ids = {"session_id": "s1", "task_id": "s1"}
    drivers = ("mcp__playwright__browser_click", "mcp__Chrome_DevTools__evaluate", "mcp__desktop_commander__run",
               "mcp__computer__screenshot", "mcp__puppeteer__goto")
    for tool in drivers:
        assert dg.pre_tool_call(tool_name=tool, **ids) is None, "unmarked"
    _read()
    for tool in drivers:
        assert dg.pre_tool_call(tool_name=tool, **ids)["action"] == "block", tool
    assert dg.pre_tool_call(tool_name="mcp__notes__add", **ids) is None
    assert dg.pre_tool_call(tool_name="mcp__emporia__post", **ids) is None
    assert dg.pre_tool_call(tool_name="mcp__mydriver__click", **ids) is None
    monkeypatch.setattr(rt, "_setting", lambda name: "mcp__mydriver__, odd_tool" if name == "EMPORIA_DESKTOP_TOOLS" else "")
    assert dg.pre_tool_call(tool_name="mcp__mydriver__click", **ids)["action"] == "block"
    assert dg.pre_tool_call(tool_name="odd_tool", **ids)["action"] == "block"


def test_the_new_hooks_and_the_new_setting_are_declared_registered_and_a_persons():
    manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    init = (PLUGIN / "__init__.py").read_text()
    for hook in ("post_compaction", "subagent_start", "subagent_stop"):
        assert hook in manifest["provides_hooks"] and f'ctx.register_hook("{hook}", desktop_guard.{hook})' in init
    assert "EMPORIA_DESKTOP_TOOLS" in {e["name"] for e in manifest["optional_env"]}
    repo = Path(__file__).resolve().parents[1]
    assert "EMPORIA_DESKTOP_TOOLS" in (repo / "docs" / "environment.md").read_text()
    kg = importlib.util.spec_from_file_location("emporia_key_guard2", PLUGIN / "key_guard.py")
    mod = importlib.util.module_from_spec(kg)
    kg.loader.exec_module(mod)
    out = mod.pre_tool_call(tool_name="terminal", args={"command": "echo EMPORIA_DESKTOP_TOOLS=x >> .env"})
    assert out and out["action"] == "block"
