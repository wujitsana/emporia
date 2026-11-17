"""`emporia agent serve-remote` and `emporia agent connector ...`: the person's side."""
import json
import subprocess
import time

import pytest
from remote_support import AGENT, Clock, Rig

from emporia import cli


@pytest.fixture
def keys(tmp_path):
    d = tmp_path / "keys"
    d.mkdir()
    (d / "emporia.env").write_text(f"EMPORIA_AGENT_ID={AGENT}\nEMPORIA_RELAY_URL=http://127.0.0.1:1\n"
                                   "EMPORIA_REMOTE_SCOPES=read,talk\n")
    return d


def run(*argv) -> int:
    return cli.main(["agent", *argv])


@pytest.fixture
def terminal(monkeypatch):
    """The person at a terminal who answers y."""
    import builtins

    from emporia.remote import cli as rcli
    monkeypatch.setattr(rcli, "at_a_terminal", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")


async def test_the_person_lists_approves_narrower_revokes_and_reads_the_log(tmp_path, monkeypatch, keys, capsys,
                                                                           terminal):
    rig = Rig(tmp_path, monkeypatch, clock=Clock(time.time()))
    async with rig.running() as c:
        reg, verifier, page = await c.start(scope="emporia:read emporia:talk")
        assert run("connector", "pending", str(keys)) == 0
        out = capsys.readouterr().out
        code = rig.auth.pending_rows()[0]["code"]
        assert code in out and "scripted client" in out and "claims to be" in out
        # --yes is refused: only a person at a terminal approves
        assert run("connector", "approve", str(keys), code, "--yes") == 2
        assert rig.store.one("SELECT COUNT(*) n FROM grants")["n"] == 0
        # a scope that was not asked for is refused
        assert run("connector", "approve", str(keys), code, "--scopes", "play") == 2
        # approve only `read`, with a name
        assert run("connector", "approve", str(keys), code.lower().replace("-", ""), "--scopes", "read",
                   "--connector-name", "my duet") == 0
        shown = capsys.readouterr().out
        assert "emporia:read" in shown and "narrower than asked" in shown and "AS the agent" in shown
        assert "no money" in shown.lower()
        r = await c.finish(reg, verifier, page)
        assert r.status_code == 200, r.text
        assert r.json()["scope"] == "emporia:read"
        assert run("connector", "list", str(keys)) == 0
        listing = capsys.readouterr().out
        assert "my duet" in listing and "read" in listing and "live" in listing
        grant = rig.store.one("SELECT grant_id FROM grants")["grant_id"]
        assert run("connector", "revoke", str(keys), grant) == 0
        assert (await c.rpc(r.json()["access_token"], "tools/list")).status_code == 401
        assert run("connector", "revoke", str(keys), grant) == 1          # already revoked
        assert run("connector", "revoke", str(keys), "--all") == 0
        assert run("connector", "log", str(keys)) == 0
        captured = capsys.readouterr()
        assert "approve" in captured.out and "revoke" in captured.out and "chain: OK" in captured.err
        assert r.json()["access_token"] not in captured.out


def test_allow_redirect_takes_one_https_callback_and_is_logged(tmp_path, monkeypatch, keys, capsys, terminal):
    rig = Rig(tmp_path, monkeypatch, clock=Clock(time.time()))
    assert run("connector", "allow-redirect", str(keys), "http://evil.example/cb") == 2
    assert run("connector", "allow-redirect", str(keys), "https://host.example/cb#x") == 2
    assert run("connector", "allow-redirect", str(keys), "https://host.example/cb") == 0
    assert "https://host.example/cb" in rig.store.allowed_redirects()
    assert any(r["event"] == "allow_redirect" for r in rig.audit.rows())


def test_a_bad_command_or_agent_says_so(tmp_path, keys, capsys):
    assert run("connector", "nothing", str(keys)) == 2
    assert run("connector", "list", str(tmp_path / "nowhere" / "x")) == 2
    assert "usage" in capsys.readouterr().err


def test_serve_remote_refuses_what_the_design_says_and_starts_the_child_with_the_agents_own_settings(
        tmp_path, monkeypatch, keys, capsys):
    monkeypatch.setenv("EMPORIA_UNTRACKED_TURNS", "person")
    assert run("serve-remote", str(keys), "--public-url", "https://a.example") == 2
    assert "UNTRACKED_TURNS" in capsys.readouterr().err
    monkeypatch.setenv("EMPORIA_UNTRACKED_TURNS", "")
    assert run("serve-remote", str(keys)) == 2                                  # no public URL
    assert run("serve-remote", str(keys), "--public-url", "http://a.example") == 2
    assert run("serve-remote", str(keys), "--public-url", "https://a.example", "--bind", "0.0.0.0:9") == 2
    assert run("serve-remote", str(keys), "--public-url", "https://a.example", "--scopes", "private") == 2
    seen = {}

    def fake_run(cmd, env=None, **kw):
        seen.update(cmd=cmd, env=env)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-reach-the-child")
    assert run("serve-remote", str(keys), "--public-url", "https://a.example") == 0
    env = seen["env"]
    assert env["EMPORIA_AGENT_ID"] == AGENT and env["EMPORIA_KEYS_DIR"] == str(keys) and env["EMPORIA_NO_DOTENV"] == "1"
    assert "OPENAI_API_KEY" not in env and "EMPORIA_UNTRACKED_TURNS" not in env
    assert seen["cmd"][-4:] == ["--scopes", "read,talk", "--bind", "127.0.0.1:8789"]
    info = json.loads((keys / "remote" / "server.json").read_text())
    assert info["public_url"] == "https://a.example" and info["ceiling"] == ["emporia:read", "emporia:talk"]
    assert (keys / "remote" / "server.json").stat().st_mode & 0o777 == 0o600
    assert (keys / "remote").stat().st_mode & 0o777 == 0o700


async def test_the_person_can_deny_a_request(tmp_path, monkeypatch, keys, capsys):
    rig = Rig(tmp_path, monkeypatch, clock=Clock(time.time()))
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        code = rig.auth.pending_rows()[0]["code"]
        assert run("connector", "deny", str(keys)) == 2
        assert run("connector", "deny", str(keys), code) == 0
        assert run("connector", "deny", str(keys), code) == 1
        st = (await c.http.get(page + "/status")).json()
        assert st["status"] == "denied" and "access_denied" in st["redirect"]
        assert rig.store.one("SELECT COUNT(*) n FROM grants")["n"] == 0
    assert any(r["event"] == "deny" for r in rig.audit.rows())
