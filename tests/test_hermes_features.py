"""The 2.1 "Hermes features" batch: redaction pattern, slim relay-turn tools, rounds gate, busy input, session rotation."""
import importlib.util
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "hermes_plugin" / "emporia"


def _plugin():
    spec = importlib.util.spec_from_file_location("emporia_plugin_hf", PLUGIN / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["emporia_plugin_hf"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 7: recovery key redaction ───────────────────────────────────────────────────────────────────

def test_the_recovery_key_pattern_matches_a_real_recovery_key_and_nothing_else_of_ours():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from emporia import key_recovery
    mod = _plugin()
    assert [p.split("[")[0] for p in mod.REDACTION_PATTERNS] == [key_recovery.RECOVERY_KEY_PREFIX]
    key = key_recovery.encode_recovery_key(ed25519.Ed25519PrivateKey.generate())
    pat = re.compile(mod.REDACTION_PATTERNS[0])
    assert pat.fullmatch(key)
    assert not pat.search("emporia-recovery-v1: see the docs")


def test_register_adds_the_pattern_and_no_hook_tool_or_capability():
    import types
    mod = _plugin()
    stub = types.ModuleType("emporia_plugin_hf.adapter")      # the real adapter needs Hermes's gateway package
    stub.register_platform = lambda ctx: None
    sys.modules["emporia_plugin_hf.adapter"] = stub
    mod.adapter = stub
    got = {"hooks": [], "patterns": []}

    class Ctx:
        def register_hook(self, name, fn): got["hooks"].append(name)
        def register_platform(self, **kw): pass
        def register_skill(self, *a, **kw): pass
        def register_cli_command(self, **kw): pass
        def register_command(self, *a, **kw): pass
        def register_redaction_patterns(self, patterns): got["patterns"] += list(patterns)
        def __getattr__(self, name):
            raise AssertionError(f"register() used ctx.{name}")

    mod.register(Ctx())
    assert got["patterns"] == list(mod.REDACTION_PATTERNS)
    manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())
    assert sorted(set(got["hooks"])) == sorted(manifest["provides_hooks"])
    assert "capabilities" not in manifest


# ── 6: skill conditions ─────────────────────────────────────────────────────────────────────────

def _front(skill):
    text = (PLUGIN / "skills" / skill / "SKILL.md").read_text()
    return yaml.safe_load(text.split("---", 2)[1])


def test_the_setup_skill_is_offered_only_while_emporias_tools_are_missing():
    hermes = _front("emporia-setup")["metadata"]["hermes"]
    assert hermes["fallback_for_tools"] == ["mcp__emporia__whats_next"]
    assert "whats_next" in (REPO / "src" / "emporia" / "mcp_server.py").read_text()


def test_the_main_skill_has_no_requires_condition_because_tool_search_defers_mcp_tools():
    """With Hermes's Tool Search active the MCP tools leave the session's tool list, so a `requires_*` condition
    would hide the main skill exactly then (agent/system_prompt.py builds the index from the visible tools)."""
    hermes = _front("emporia")["metadata"]["hermes"]
    assert not any(k.startswith("requires_") for k in hermes)


# ── 5: daily session rotation for non-game chats ────────────────────────────────────────────────

HERMES_PYTHON = Path("/opt/hermes/.venv/bin/python")


def test_non_game_chats_carry_the_utc_date_and_a_game_keeps_one_id(tmp_path):
    import json
    import subprocess

    import pytest
    if not HERMES_PYTHON.exists():
        pytest.skip("Hermes is not installed here")
    harness = tmp_path / "harness.py"
    harness.write_text(f'''
import datetime, importlib.util, json, sys
from pathlib import Path
d = Path({str(PLUGIN)!r})
spec = importlib.util.spec_from_file_location("emporia_platform", d / "__init__.py", submodule_search_locations=[str(d)])
mod = importlib.util.module_from_spec(spec); sys.modules["emporia_platform"] = mod
spec.loader.exec_module(mod)
from emporia_platform.adapter import EmporiaAdapter
a = EmporiaAdapter.__new__(EmporiaAdapter)
a.agent_id = "alice"
utc = datetime.timezone.utc
late = datetime.datetime(2026, 11, 14, 23, 59, tzinfo=utc)
early = datetime.datetime(2026, 11, 15, 0, 1, tzinfo=utc)
est = datetime.datetime(2026, 11, 14, 22, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-5)))   # 03:00 UTC next day
out = {{}}
for label, topic in (("none", None), ("dm", "dm:bob"), ("room", "room:r1"), ("agora", "agora:chess"),
                     ("post", "post:p1"), ("game", "session:sess_1"), ("bad", "session:a b")):
    n = {{"topic": topic}} if topic else {{}}
    out[label] = [a.chat_for(n, late)[0], a.chat_for(n, early)[0]]
out["tz"] = a.chat_for({{"topic": "dm:bob"}}, est)[0]
out["name"] = a.chat_for({{"topic": "dm:bob"}}, late)[1]
print(json.dumps(out))
''')
    r = subprocess.run([str(HERMES_PYTHON), str(harness)], cwd="/opt/hermes", capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "HERMES_HOME": str(tmp_path / "home"), "PYTHONPATH": "/opt/hermes"})
    assert r.returncode == 0, r.stderr[-1500:]
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["none"] == ["emporia:alice:2026-11-14", "emporia:alice:2026-11-15"]
    assert got["dm"] == ["emporia:alice:dm:bob:2026-11-14", "emporia:alice:dm:bob:2026-11-15"]
    assert got["room"][0] == "emporia:alice:room:r1:2026-11-14" and got["agora"][1] == "emporia:alice:agora:chess:2026-11-15"
    assert got["post"][0] == "emporia:alice:post:p1:2026-11-14"
    assert got["game"] == ["emporia:alice:session:sess_1"] * 2, "a game is one conversation for its whole life"
    assert got["bad"][0] == "emporia:alice:2026-11-14", "a malformed topic falls to the day's general chat"
    assert got["tz"] == "emporia:alice:dm:bob:2026-11-15", "the date is the UTC date"
    assert "2026-11-14" in got["name"]


def test_the_cost_by_kind_views_read_a_dated_chat_id_as_the_same_kind(tmp_path):
    from emporia import analytics
    assert analytics._DAY_SUFFIX.sub("", "dm:bob:2026-11-14") == "dm:bob"
    assert analytics._DAY_SUFFIX.sub("", ":2026-11-14").lstrip(":") == ""
    assert analytics._DAY_SUFFIX.sub("", "session:s1") == "session:s1"
