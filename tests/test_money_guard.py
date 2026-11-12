"""Plugin hook money_guard: on mainnet, a payment tool is refused at once in a turn another agent started."""
import ast
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("emporia_money_guard", REPO / "hermes_plugin" / "emporia" / "money_guard.py")
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)

# What reaches the wallet: a paid retry of a 402, or a signed escrow hold or reclaim.
PAYING_CALLS = {"_x402_paid_retry", "_tempo_paid_retry", "sign_escrow", "reclaim_escrow", "sign_exact", "sign_entry",
                "pay_tempo"}


def _setting(mainnet):
    return lambda name: mainnet if name == "EMPORIA_X402_MAINNET" else ""


def _relay_turn(session="s-relay"):
    mg.relay_turn._origin.clear()
    mg.relay_turn.pre_llm_call(platform="emporia", session_id=session, task_id=session)
    return {"session_id": session, "task_id": session}


def _person_turn(session="s-person"):
    mg.relay_turn.pre_llm_call(platform="cli", session_id=session, task_id=session)
    return {"session_id": session, "task_id": session}


def test_every_money_tool_is_refused_at_once_in_a_turn_another_agent_started_on_mainnet(monkeypatch):
    """Nobody can answer the wallet's approval there: it would wait out its timeout. Refused at once."""
    monkeypatch.setattr(mg, "_setting", _setting("1"))
    turn = _relay_turn()
    for tool in ("join_room", "subscribe_agora_topic", "fund_session", "reclaim_escrow"):
        out = mg.pre_tool_call(tool_name=f"mcp__emporia__{tool}", args={"room_id": "r1"}, **turn)
        assert out == {"action": "block", "message": mg.UNATTENDED}
    assert "cannot ask you" in mg.UNATTENDED and "Not paid" in mg.UNATTENDED


def test_a_persons_own_turn_is_left_to_the_wallets_approval_and_nothing_is_asked_twice(monkeypatch):
    monkeypatch.setattr(mg, "_setting", _setting("1"))
    turn = _person_turn()
    for tool in ("join_room", "fund_session"):
        assert mg.pre_tool_call(tool_name=f"mcp__emporia__{tool}", args={}, **turn) is None
    assert mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={}) is None, "a session never seen"


def _signer(monkeypatch, answer, status=200):
    """A signer answering /v1/wallet (the suite blocks real sockets); the profile names it and holds no mainnet flag."""
    seen = []

    class Conn:
        def __init__(self, netloc, timeout=0):
            self.netloc = netloc

        def request(self, method, path, headers=None):
            seen.append((self.netloc, path, (headers or {}).get("x-emporia-agent")))

        def getresponse(self):
            return type("R", (), {"status": status, "read": lambda self: json.dumps(answer).encode()})()

        def close(self):
            pass

    monkeypatch.setattr(mg.http.client, "HTTPConnection", Conn)
    values = {"EMPORIA_SIGNER_URL": "http://signer.test:8090", "EMPORIA_SIGNER_TOKEN": "t", "EMPORIA_AGENT_ID": "alpha"}
    monkeypatch.setattr(mg, "_setting", lambda name: values.get(name, ""))
    mg._signer_seen.clear()
    return seen


def test_mainnet_unlocked_in_the_signers_policy_also_goes_to_the_approval_prompt(monkeypatch):
    """B1: `emporia signer add` takes EMPORIA_X402_MAINNET out of the profile; the unlock lives in the signer.
    The hook asks the signer (which stays the authority); the answer only ever adds a prompt."""
    seen = _signer(monkeypatch, {"mainnet": True})
    turn = _relay_turn()
    out = mg.pre_tool_call(tool_name="mcp__emporia__fund_session", args={"session_id": "s1"}, **turn)
    assert out and out["action"] == "block" and seen == [("signer.test:8090", "/v1/wallet", "alpha")]
    assert mg.pre_tool_call(tool_name="mcp__emporia__send_dm", args={}, **turn) is None
    mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={}, **turn)
    assert len(seen) == 1, "remembered for a moment"


def test_a_signer_without_a_mainnet_key_is_an_older_one_and_counts_as_mainnet(monkeypatch):
    turn = _relay_turn()
    _signer(monkeypatch, {"pay_networks": []})
    assert mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={}, **turn)["action"] == "block"


def _make_runtime(base: Path, head: str, ref: bool = True) -> Path:
    runtime = base / "plugin-data" / "emporia" / "runtime"
    (runtime / "src" / "emporia").mkdir(parents=True, exist_ok=True)
    (runtime / "src" / "emporia" / "cli.py").write_text("")
    (runtime / "pyproject.toml").write_text("")
    git = runtime / ".git"
    (git / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    if ref:
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "refs" / "heads" / "main").write_text(head + "\n")
    else:
        (git / "HEAD").write_text(head + "\n")
    return runtime


def _runtime(tmp_path, monkeypatch, head, pinned="a" * 40, ref=True):
    home = tmp_path / "home"
    _make_runtime(home, head, ref)
    monkeypatch.setenv("HERMES_HOME", str(home))
    src = tmp_path / "plugin"
    src.mkdir(exist_ok=True)
    (src / "emporia_source.json").write_text(json.dumps({"sha": pinned}))
    monkeypatch.setattr(mg, "__file__", str(src / "money_guard.py"))


def test_a_shared_runtime_is_checked_too_and_a_development_checkout_never_is(monkeypatch, tmp_path):
    """F1: a profile made by setup from another shares that profile's runtime through EMPORIA_PYTHON (no folder of
    its own); a checkout is the runtime for a plugin that sits in one, whatever plugin-data holds."""
    monkeypatch.setattr(mg, "_setting", lambda name: {"EMPORIA_X402_MAINNET": "1", "EMPORIA_PYTHON": py}.get(name, ""))
    other = tmp_path / "other-home"
    shared = _make_runtime(other, head="b" * 40)
    py = str(shared / ".venv" / "bin" / "python")
    own = tmp_path / "own-home"
    own.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(own))                              # no runtime folder of its own
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "emporia_source.json").write_text(json.dumps({"sha": "a" * 40}))
    monkeypatch.setattr(mg, "__file__", str(plugin / "money_guard.py"))
    assert mg.runtime_is_stale()
    out = mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={}, **_person_turn())
    assert out["action"] == "block" and "hermes emporia update" in out["message"]
    (shared / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n")             # updated to the pin
    assert not mg.runtime_is_stale()
    # a runtime that is not under plugin-data is a checkout someone develops in: never stale
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "src" / "emporia").mkdir(parents=True)
    (elsewhere / "src" / "emporia" / "cli.py").write_text("")
    (elsewhere / "pyproject.toml").write_text("")
    (elsewhere / ".git").mkdir()
    (elsewhere / ".git" / "HEAD").write_text("c" * 40 + "\n")
    monkeypatch.setattr(mg, "_setting", lambda name: {"EMPORIA_PYTHON": str(elsewhere / ".venv" / "bin" / "python")}.get(name, ""))
    assert not mg.runtime_is_stale()
    # a plugin that sits in a checkout: the checkout is the runtime, whatever plugin-data holds
    repo = tmp_path / "repo"
    (repo / "src" / "emporia").mkdir(parents=True)
    (repo / "src" / "emporia" / "cli.py").write_text("")
    (repo / "pyproject.toml").write_text("")
    (repo / "hermes_plugin" / "emporia").mkdir(parents=True)
    (repo / "hermes_plugin" / "emporia" / "emporia_source.json").write_text(json.dumps({"sha": "a" * 40}))
    monkeypatch.setattr(mg, "__file__", str(repo / "hermes_plugin" / "emporia" / "money_guard.py"))
    _make_runtime(own, head="b" * 40)                                        # leftover plugin-data
    monkeypatch.setattr(mg, "_setting", lambda name: {"EMPORIA_X402_MAINNET": "1"}.get(name, ""))
    assert not mg.runtime_is_stale()
    assert mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={}, **_person_turn()) is None
    # a `.git` that is a file (a worktree) is not read
    (plugin / "x").mkdir()
    wt = tmp_path / "wt-home"
    rt = _make_runtime(wt, head="b" * 40)
    shutil.rmtree(rt / ".git")
    (rt / ".git").write_text("gitdir: /somewhere\n")
    monkeypatch.setenv("HERMES_HOME", str(wt))
    monkeypatch.setattr(mg, "__file__", str(plugin / "money_guard.py"))
    assert not mg.runtime_is_stale()


def test_a_runtime_that_is_not_the_pinned_one_blocks_the_payment_tools_in_any_turn_on_mainnet(monkeypatch, tmp_path):
    """S2: after a plugin update the runtime may be from before the release and not ask. Read from .git/HEAD."""
    monkeypatch.setattr(mg, "_setting", _setting("1"))
    _runtime(tmp_path, monkeypatch, head="b" * 40)
    for turn in (_person_turn(), _relay_turn()):
        out = mg.pre_tool_call(tool_name="mcp__emporia__fund_session", args={}, **turn)
        assert out["action"] == "block" and "hermes emporia update" in out["message"]
    assert "hermes emporia update" in mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={}, **_person_turn())["message"]
    assert mg.pre_tool_call(tool_name="mcp__emporia__send_dm", args={}, **_person_turn()) is None
    _runtime(tmp_path, monkeypatch, head="a" * 40)                       # at the pin: a person's turn is left alone
    assert mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={}, **_person_turn()) is None
    _runtime(tmp_path, monkeypatch, head="a" * 40, ref=False)            # a detached HEAD, as setup leaves it
    assert not mg.runtime_is_stale()
    _runtime(tmp_path, monkeypatch, head="b" * 40, ref=False)
    assert mg.runtime_is_stale()
    monkeypatch.setattr(mg, "_setting", _setting("0"))
    assert mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={}, **_person_turn()) is None, "testnet: nothing"


def test_a_signer_on_testnet_refuses_nothing_and_an_unreachable_or_odd_one_counts_as_mainnet(monkeypatch):
    turn = _relay_turn()
    _signer(monkeypatch, {"mainnet": False})
    assert mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={}, **turn) is None
    _signer(monkeypatch, {"error": "x"}, status=500)
    assert mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={}, **turn)["action"] == "block"
    _signer(monkeypatch, {})
    monkeypatch.setattr(mg.http.client, "HTTPConnection", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={}, **turn)["action"] == "block"


@pytest.mark.parametrize("tool", ["mcp__emporia__send_dm", "mcp__emporia__get_inbox", "mcp__emporia__money_info",
                                  "mcp__other__join_room", "join_room", "terminal"])
def test_other_tools_are_left_alone_on_mainnet(monkeypatch, tool):
    monkeypatch.setattr(mg, "_setting", _setting("1"))
    assert mg.pre_tool_call(tool_name=tool, args={}, **_relay_turn()) is None


@pytest.mark.parametrize("value", ["", "0", "false"])
def test_testnet_behaviour_is_unchanged(monkeypatch, value):
    monkeypatch.setattr(mg, "_setting", _setting(value))
    assert mg.pre_tool_call(tool_name="mcp__emporia__join_room", args={"room_id": "r1"}, **_relay_turn()) is None


def test_the_list_is_every_tool_that_reaches_the_wallet():
    """A tool added to the runtime that pays must be added to MONEY_TOOLS: found by reading the source."""
    tree = ast.parse((REPO / "src" / "emporia" / "mcp_server.py").read_text())
    paying = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and any("tool" in ast.unparse(d) for d in node.decorator_list):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | \
                    {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            if names & PAYING_CALLS:
                paying.add(node.name)
    assert paying == set(mg.MONEY_TOOLS), (paying, mg.MONEY_TOOLS)


def test_the_hook_is_registered():
    src = (REPO / "hermes_plugin" / "emporia" / "__init__.py").read_text()
    assert 'ctx.register_hook("pre_tool_call", money_guard.pre_tool_call)' in src
