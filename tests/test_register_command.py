"""`emporia register NAME`, the not-registered words, and what setup's end says about registration."""
import argparse
from types import SimpleNamespace


from emporia import cli


def _run(returns):
    """A stand-in for cli._as_agent that answers from `returns` (stdout lines) and records the code run."""
    seen = []
    answers = iter(returns)

    def fake(profile, code, **env):
        seen.append(code)
        return SimpleNamespace(returncode=0, stdout=next(answers), stderr="")
    return fake, seen


def test_register_runs_register_agent_as_the_agent_and_says_so(monkeypatch, tmp_path):
    fake, seen = _run(['RESULT {"ok": true, "who": "shop@relay.example", "error": ""}\n'])
    monkeypatch.setattr(cli, "_as_agent", fake)
    ok, said = cli._register_now(tmp_path, "shop", ["evm"])
    assert ok and "registered as shop@relay.example" in said
    assert "register_run.run('shop', ['evm'])" in seen[0]


def _run_register(monkeypatch, capsys, families):
    """register_run.run with a stand-in MCP module; returns what it printed."""
    import sys
    import types
    from emporia import register_run
    calls = []
    fake = types.SimpleNamespace(
        register_agent=lambda agent_id: {"success": True, "address": "shop@r.example"},
        register_x402_wallet=lambda agent_id, network_family: calls.append(network_family) or {"success": True})
    monkeypatch.setitem(sys.modules, "emporia.mcp_server", fake)
    monkeypatch.setattr("emporia.mcp_server", fake, raising=False)
    register_run.run("shop", families)
    return capsys.readouterr().out, calls


def test_register_code_runs_with_no_wallet_one_wallet_and_both(monkeypatch, capsys):
    for fams in ([], ["evm"], ["evm", "svm"]):
        out, calls = _run_register(monkeypatch, capsys, fams)
        assert out.startswith("RESULT ") and '"ok": true' in out
        assert calls == fams and out.count(" wallet registered") == len(fams)


def test_the_generated_code_is_valid_python_for_any_families(monkeypatch):
    import ast
    for fams in ([], ["evm"], ["evm", "svm"]):
        fake, seen = _run(['RESULT {"ok": true, "who": "a", "error": ""}\n'])
        monkeypatch.setattr(cli, "_as_agent", fake)
        cli._register_now(None, "shop", fams)
        ast.parse(seen[0])


def test_the_retry_hint_names_the_profile_not_the_agent_id(monkeypatch, tmp_path):
    fake, _ = _run(['RESULT {"ok": false, "who": "", "error": "nope"}\n'])
    monkeypatch.setattr(cli, "_as_agent", fake)
    ok, said = cli._register_now(tmp_path, "e2eagent", name="e2e21")
    assert not ok and "register e2e21" in said and "register e2eagent" not in said


def test_a_busy_relay_is_retried_with_a_jittered_wait_and_a_refusal_is_not(monkeypatch, tmp_path):
    busy = 'RESULT {"ok": false, "who": "", "error": "Rate limit exceeded — 30 requests per 60s per IP"}\n'
    fake, seen = _run([busy, busy, 'RESULT {"ok": true, "who": "a", "error": ""}\n'])
    monkeypatch.setattr(cli, "_as_agent", fake)
    waits = []
    ok, _ = cli._register_now(tmp_path, "shop", sleep=waits.append)
    assert ok and len(seen) == 3 and len(waits) == 2 and all(w > 0 for w in waits)
    fake, seen = _run(['RESULT {"ok": false, "who": "", "error": "name is reserved"}\n'])
    monkeypatch.setattr(cli, "_as_agent", fake)
    ok, said = cli._register_now(tmp_path, "shop", sleep=waits.append)
    assert not ok and len(seen) == 1 and "name is reserved" in said and "register shop" in said


def test_the_command_checks_the_profile_and_the_relay_and_returns_the_result(monkeypatch, tmp_path, capsys):
    prof = tmp_path / "shop"
    monkeypatch.setattr(cli, "profile_dir", lambda name: prof)
    args = argparse.Namespace(name="shop")
    assert cli.cmd_register(args) == 1 and "no agent profile" in capsys.readouterr().out
    prof.mkdir()
    (prof / "config.yaml").write_text("{}")
    monkeypatch.setattr(cli, "_mcp_env", lambda p: {"EMPORIA_AGENT_ID": "shop", "EMPORIA_RELAY_URL": "http://r.example"})
    monkeypatch.setattr(cli, "health", lambda **kw: None)
    assert cli.cmd_register(args) == 1 and "not answering" in capsys.readouterr().out
    monkeypatch.setattr(cli, "health", lambda **kw: {"ok": True})
    monkeypatch.setattr(cli, "wallet_presence", lambda p, a: {"evm": False, "svm": False})
    monkeypatch.setattr(cli, "_register_now", lambda *a, **k: (True, "registered as shop@r.example"))
    assert cli.cmd_register(args) == 0 and "registered as" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_register_now", lambda *a, **k: (False, "not registered: nope"))
    assert cli.cmd_register(args) == 1


def test_the_command_is_in_the_parser_and_the_plugin():
    p = cli.build_parser()
    assert p.parse_args(["register", "shop"]).func is cli.cmd_register
    from hermes_plugin.emporia import setup_cli
    assert "register" in setup_cli.PASSTHROUGH


def test_setups_closing_block_and_summary_say_whether_it_registered():
    yes = "\n".join(cli.welcome_lines("shop", "shop", "https://r.example", registered=True))
    no = "\n".join(cli.welcome_lines("shop", "shop", "https://r.example", registered=False))
    untried = "\n".join(cli.welcome_lines("shop", "shop", "https://r.example"))
    assert "registered on the relay" in yes and "NOT registered" not in yes
    assert "NOT registered yet" in no and "emporia register shop" in no
    assert "emporia register shop" in untried
