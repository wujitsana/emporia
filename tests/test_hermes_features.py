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
