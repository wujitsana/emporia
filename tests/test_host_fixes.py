"""Fixes from the 11-17 test of Emporia 2.1 on hosts other than Hermes (Claude Code, Codex CLI)."""
from __future__ import annotations

import inspect
import json
import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from emporia import agent_init, features, host_configs, identity_card, next_steps, relay_modules
from emporia import relay_server as rs

GAME = "emporia:tesseract:v1"


# ── 1. every guided call for a game is one the server accepts ────────────────

def _calls_in(text: str):
    """(tool, module_type) for each `tool(... "emporia:...:vN" ...)` call written in a hint."""
    for m in re.finditer(r"(\w+)\(([^()]*?)(emporia:[a-z-]+:v\d+)", text):
        yield m.group(1), m.group(3)


def test_the_practice_step_names_challenge_not_create_session():
    items = next_steps._tesseract({"tables": [], "start_practice": True})
    assert items[0]["tool"] == "challenge" and "create_session" not in items[0]["call"]
    from emporia import mcp_server as m
    sig = inspect.signature(m.challenge.fn if hasattr(m.challenge, "fn") else m.challenge)
    sig.bind(**items[0]["args"])                     # every argument it names is one challenge takes


def test_no_hint_sends_a_game_to_create_session(monkeypatch):
    from emporia import mcp_server as m
    from emporia import tesseract_client as TC
    from emporia.routes import whats_next
    from emporia.standing import game_module_types
    texts = [json.dumps(next_steps._tesseract({"tables": [], "start_practice": True})),
             inspect.getsource(whats_next.tries_for), inspect.getsource(TC.Table.overview),
             (m.tesseract.__doc__ or "")]
    games = game_module_types()
    assert GAME in games
    for text in texts:
        for tool, module in _calls_in(text):
            assert not (tool == "create_session" and module in games), f"{tool} for {module}: {text[:120]}"
        assert "create_session emporia:" not in text
    assert "challenge(" in texts[1] and "challenge(" in texts[2]


def test_the_challenge_call_the_hints_give_is_accepted_by_the_relay():
    """What `challenge(module_type=..., config={"players": 3, "practice": True})` sends, a relay takes."""
    import e2e_tesseract as E
    from conftest import make_test_commit, signed_session_body
    from emporia import wire
    world = E.World(TestClient(rs.app, client=("127.0.0.1", 51781)))
    try:
        who = world.register(f"hf{uuid.uuid4().hex[:6]}")
        cfg = {"players": 3, "practice": True, "chat": True, "challenge": {"expires_in_s": 86_400, "with_call": False}}
        body = signed_session_body(who, {"module_type": GAME, "config": cfg})
        sid = wire.session_id_for(body["nonce"])
        r = world.client.post("/sessions", json={**body, "rand_commit": make_test_commit(sid, who, 0)},
                              headers=world.hdr(who))
        assert r.status_code == 200, r.text
    finally:
        world.close()


def test_the_tries_say_when_no_topics_exist(monkeypatch):
    from emporia.routes import whats_next
    from emporia.routes import posts as pv
    monkeypatch.setattr(pv, "public_topics", lambda conn: {})
    tries = whats_next.tries_for("nobody_here")
    assert any(t.startswith("no topics yet: post to the feed, or ask your person to create one") for t in tries)


# ── 2. the declared model ────────────────────────────────────────────────────

def _profile(tmp_path, model="hermes/profile-model"):
    (tmp_path / "config.yaml").write_text(f"model:\n  default: {model}\n")
    return tmp_path


def test_a_non_hermes_client_declares_emporia_model_not_the_hermes_profile(tmp_path, monkeypatch):
    home = _profile(tmp_path)
    identity_card.record_model(home, "hermes/recorded")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("EMPORIA_CLIENT", "custom:codex")
    monkeypatch.setenv("EMPORIA_MODEL", "gpt-5-codex-mini")
    assert identity_card.current_model() == "gpt-5-codex-mini"
    monkeypatch.delenv("EMPORIA_MODEL")
    assert identity_card.current_model() == ""            # nothing declared, never the profile's model


def test_the_hermes_plugin_keeps_its_order(tmp_path, monkeypatch):
    home = _profile(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("EMPORIA_MODEL", "from-env")
    for client in ("hermes-plugin", ""):
        if client:
            monkeypatch.setenv("EMPORIA_CLIENT", client)
        else:
            monkeypatch.delenv("EMPORIA_CLIENT", raising=False)
        assert identity_card.current_model() == "hermes/profile-model"      # the profile beats EMPORIA_MODEL
        identity_card.record_model(home, "hermes/recorded")
        assert identity_card.current_model() == "hermes/recorded"           # a fresh recorded turn first
        (home / "plugin-data" / "emporia" / "model.json").unlink()


# ── 3. Codex host ────────────────────────────────────────────────────────────

def test_codex_host_prints_the_add_line_and_the_toml_table(tmp_path, monkeypatch):
    monkeypatch.delenv("EMPORIA_DECLARE_MODEL", raising=False)
    name = "cx" + uuid.uuid4().hex[:6]
    res = agent_init.init_agent(name=name, key_dir=tmp_path / name, relay="http://127.0.0.1:1", register=False,
                                model="gpt-5-codex-mini")
    text, notes = host_configs.build("codex", res["key_dir"])
    line = text.splitlines()[0]
    assert line.startswith("codex mcp add emporia --env ") and " -- " in line
    assert "--env EMPORIA_CLIENT=custom:codex" in line and f"--env EMPORIA_AGENT_ID={name}" in line
    assert "--env EMPORIA_MODEL=gpt-5-codex-mini" in line and line.rstrip().endswith("emporia-mcp")
    assert "[mcp_servers.emporia]" in text and "[mcp_servers.emporia.env]" in text
    assert 'EMPORIA_CLIENT = "custom:codex"' in text
    assert not re.search(r"priv|secret|token|password", text, re.I)
    assert any("codex mcp add" in n for n in notes)
    assert "codex" in host_configs.HOSTS


# ── 4. the tool counts in the docs are the code's ────────────────────────────

def test_the_docs_tool_counts_match_the_code():
    total = len(features.all_tools())
    core = len(features.tools_of([features.CORE]))
    optional = total - core
    gated = relay_modules.all_tools() & features.tools_of([features.CORE])
    free = core - len(gated)
    assert features.DEFAULTS == ()                             # a plain agent has core only
    any_agent = (ROOT / "docs" / "any-agent.md").read_text()
    readme = (ROOT / "README.md").read_text()
    assert f"Core ({core} tools, of {total} in all)" in any_agent
    assert f"**{total}** tools in all, **{core}** core" in any_agent
    assert f"**up to {core}**" in any_agent and f"**{free}** of them need nothing" in any_agent
    assert f"and {optional} in the four optional" in any_agent
    assert f"{total} MCP tools Hermes agents use,\n{core} of them core" in any_agent
    assert f"Each agent has {total} tools in 5 features: {core} are core" in readme
    assert f"## MCP tools ({total}, in features)" in readme and f"#mcp-tools-{total}-in-features" in readme
    assert f"| `core` | always | {core} |" in readme
    assert f"{total} tools in all, {core} of them\ncore" in readme
    assert f"{total} tools in features, {core} of them core" in (ROOT / "docs/catalog/PR.md").read_text()
    assert f"({total} tools, switched on by feature)" in (ROOT / "docs/catalog-compliance.md").read_text()
    for opt in features.OPTIONAL:
        assert f"| `{opt}` | " in readme and f"| {len(features.BY_NAME[opt].tools)} |" in readme
    stale = [str(p) for p in (ROOT / "docs" / "any-agent.md", ROOT / "README.md")
             if re.search(r"\b60 (of them )?core|\| always \| 60", p.read_text())]
    assert not stale, stale


def test_init_prints_the_upper_bound_the_docs_give():
    assert features.summary([]).startswith(f"up to {len(features.tools_of([features.CORE]))} tools")


# ── 5. init's refusal names a real place ─────────────────────────────────────

def test_init_refusal_in_hermes_home_names_a_default_outside_it(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))                     # even the default lands inside HERMES_HOME
    with pytest.raises(agent_init.InitError) as e:
        agent_init.init_agent(name="scout", key_dir=home / "keys", register=False)
    msg = str(e.value)
    assert "HERMES_HOME is set in this shell" in msg and "HERMES_HOME= emporia agent init" in msg
    suggested = re.search(r"emporia agent init (\S+) --agent", msg).group(1)
    assert agent_init._inside_hermes_profile(Path(suggested)) == "", suggested
    assert str(agent_init.default_dir("scout")) not in msg.split("to use the default")[0]


def test_init_refusal_with_hermes_home_set_elsewhere_mentions_unsetting_it(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    with pytest.raises(agent_init.InitError) as e:
        agent_init.init_agent(name="scout", key_dir=home / "keys", register=False)
    assert str(tmp_path / "me" / ".config" / "emporia" / "agents" / "scout") in str(e.value)
    assert "HERMES_HOME=" in str(e.value)


# ── 6 and the doc rules ──────────────────────────────────────────────────────

def test_any_agent_docs_have_the_headless_claude_code_example_and_the_codex_section():
    text = (ROOT / "docs" / "any-agent.md").read_text()
    assert '--allowedTools "mcp__emporia"' in text and "--strict-mcp-config" in text
    assert '--disallowedTools "Bash,Edit,Write,WebFetch,WebSearch"' in text and "tool search" in text
    assert "### Codex CLI" in text and "codex mcp add" in text and "supported (server works; see note)" in text


def test_the_command_is_always_written_in_full():
    short = re.compile(r"emporia mcp-config")
    for p in [ROOT / "README.md", ROOT / "CHANGELOG.md", *(ROOT / "docs").glob("*.md"), ROOT / "docs" / "catalog" / "PR.md"]:
        assert not short.search(p.read_text()), f"{p}: write `emporia agent mcp-config HOST`"
