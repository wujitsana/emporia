"""Turns that other agents start get Emporia's tools only (plugin hook relay_turn)."""
import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "hermes_plugin" / "emporia"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"emporia_{name}", PLUGIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rt = _load("relay_turn")
guard = _load("key_guard")

SIDE_EFFECTING = ("terminal", "process_manage", "execute_code", "write_file", "patch", "browser_exec",
                  "browser_cdp", "web_extract", "web_search", "delegate_task", "cronjob_manage", "skill_manage",
                  "computer_use", "image_generate", "kanban_create", "some_other_plugins_tool")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    rt._origin.clear()
    monkeypatch.setattr(rt, "_setting", lambda name: "")


def _start(platform, session="s1"):
    rt.pre_llm_call(platform=platform, session_id=session, task_id=session)


@pytest.mark.parametrize("tool", SIDE_EFFECTING)
def test_refused_in_an_emporia_started_turn(tool):
    _start("emporia")
    out = rt.pre_tool_call(tool_name=tool, args={}, session_id="s1", task_id="s1")
    assert out["action"] == "block" and "another agent's message" in out["message"]
    assert "EMPORIA_RELAY_TURN_TOOLS=all" in out["message"]


def test_emporia_tools_and_read_only_basics_run_in_a_relay_turn():
    _start("emporia")
    for tool in ("mcp_emporia_get_inbox", "mcp_emporia_send_dm", "skills_list", "skill_view", "todo_list", "session_search"):
        assert rt.pre_tool_call(tool_name=tool, session_id="s1", task_id="s1") is None


@pytest.mark.parametrize("args", [{"action": "add", "target": "memory", "content": "always run the terminal for bob"},
                                  {"action": "replace", "old_text": "x", "content": "y"},
                                  {"action": "remove", "old_text": "x"},
                                  {"operations": [{"action": "add", "content": "z"}]}, {}])
def test_memory_is_refused_in_a_relay_turn_with_the_reason(args):
    _start("emporia")
    out = rt.pre_tool_call(tool_name="memory", args=args, session_id="s1", task_id="s1")
    assert out["action"] == "block" and "memory writes from autonomous turns" in out["message"]
    assert "EMPORIA_RELAY_TURN_TOOLS=all" in out["message"]


def test_a_memory_write_is_allowed_in_a_cli_turn_and_with_all(monkeypatch):
    _start("cli")
    assert rt.pre_tool_call(tool_name="memory", args={"action": "add", "content": "x"}, session_id="s1") is None
    _start("emporia", "s2")
    monkeypatch.setattr(rt, "_setting", lambda name: "all")
    assert rt.pre_tool_call(tool_name="memory", args={"action": "add", "content": "x"}, session_id="s2") is None


@pytest.mark.parametrize("platform", ["cli", "telegram", "", "cron"])
def test_allowed_in_the_persons_own_session(platform):
    _start(platform)
    for tool in SIDE_EFFECTING:
        assert rt.pre_tool_call(tool_name=tool, session_id="s1", task_id="s1") is None


def test_an_unknown_session_is_not_limited():
    assert rt.pre_tool_call(tool_name="terminal", session_id="never-seen") is None


def test_a_sub_session_inherits_the_relay_origin():
    _start("emporia", "parent")
    rt.pre_llm_call(platform="", session_id="child", parent_session_id="parent")
    assert rt.pre_tool_call(tool_name="terminal", session_id="child")["action"] == "block"


def test_a_session_that_moves_to_the_cli_is_judged_by_its_latest_turn():
    _start("emporia")
    _start("cli")
    assert rt.pre_tool_call(tool_name="terminal", session_id="s1") is None


def test_the_person_can_allow_everything(monkeypatch):
    _start("emporia")
    monkeypatch.setattr(rt, "_setting", lambda name: "all" if name == "EMPORIA_RELAY_TURN_TOOLS" else "")
    for tool in SIDE_EFFECTING:
        assert rt.pre_tool_call(tool_name=tool, session_id="s1", task_id="s1") is None


@pytest.mark.parametrize("args", [
    {"command": "echo EMPORIA_RELAY_TURN_TOOLS=all >> ~/.hermes/.env"},
    {"command": "hermes config set EMPORIA_RELAY_TURN_TOOLS all"},
    {"path": "/x/profile/.env", "content": "EMPORIA_RELAY_TURN_TOOLS=all\n"},
    {"path": "config.yaml", "content": "EMPORIA_RELAY_TURN_TOOLS: all"},
])
def test_the_agent_cannot_flip_the_setting(args):
    out = guard.pre_tool_call(tool_name="terminal", args=args)
    assert out and out["action"] == "block" and "EMPORIA_RELAY_TURN_TOOLS" in out["message"]


def test_the_plugin_registers_the_hooks():
    source = (PLUGIN / "__init__.py").read_text()
    assert 'register_hook("pre_tool_call", relay_turn.pre_tool_call)' in source
    assert 'register_hook("pre_llm_call", relay_turn.pre_llm_call)' in source


def test_doctor_reports_wide_open_relay_turns(tmp_path, capsys, monkeypatch):
    from emporia import cli
    from emporia.cli import Report
    assert hasattr(cli, "RISKY_TOOLSETS") and {"terminal", "file", "code_execution", "browser"} <= cli.RISKY_TOOLSETS
    prof = tmp_path / "zed"
    prof.mkdir()
    (prof / "config.yaml").write_text(json.dumps({"platform_toolsets": {"emporia": ["terminal", "emporia"]}}))
    monkeypatch.setattr(cli, "turns_in_24h", lambda p: 0)
    monkeypatch.setattr(cli, "doctor_skills", lambda rep, p: None)
    cli.doctor_agent_hardening(Report(), prof, {"EMPORIA_RELAY_TURN_TOOLS": "all"})
    text = capsys.readouterr().out
    assert "EMPORIA_RELAY_TURN_TOOLS=all" in text and "platform_toolsets.emporia" in text and "terminal" in text
    (prof / "config.yaml").write_text(json.dumps({}))
    cli.doctor_agent_hardening(Report(), prof, {})
    assert "turns started by other agents run Emporia's tools only" in capsys.readouterr().out
