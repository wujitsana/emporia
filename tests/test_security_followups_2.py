"""Security review pass 2, its proposals built (branch security-followups-2).

P1: idea verdicts are signed by the operator's verdict key, a person's key no agent
holds — the operator agent's own token or key no longer awards points. P2: `house stop` stops
only its own profile's runner. P6: the key guard matches paths as they resolve. P7: MetaMask's
Guard Mode check is remembered across requests. Each test fails without its fix."""
from tests.ids import stable_of  # noqa: E402
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from emporia import identity, operator_verdicts, signer, signing, wire
from emporia import relay_server as rs
from emporia import signer_backends as sb
from tests import verdict_key as vk
from tests.test_emporia import _register_keyed
from tests.test_ideas import idea
from tests.test_key_guard import guard
from tests.test_signer_backends import AGENT, _hold_like, mm, setup  # noqa: F401  (fixtures)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,ideas")
    monkeypatch.delenv("EMPORIA_IDEA_POINTS", raising=False)
    monkeypatch.delenv("EMPORIA_HOUSE_AGENTS", raising=False)
    monkeypatch.delenv(operator_verdicts.ENV, raising=False)
    return TestClient(rs.app, client=("127.0.0.1", 51331))


def as_(agent):
    return {"x-emporia-agent-id": agent}


def _setup(client, monkeypatch, *tags):
    agents = [f"sf2_{t}_{uuid.uuid4().hex[:6]}" for t in ("op", *tags)]
    for a in agents:
        _register_keyed(client, a)
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of(agents[0]))
    return agents


def _points(client, agent):
    return client.get(f"/agents/{agent}/points").json()["points"]


# ── P1: the operator's verdict key ───────────────────────────────────────────

def test_without_a_registered_verdict_key_the_relay_takes_no_verdict(client, monkeypatch):
    op, finder = _setup(client, monkeypatch, "finder")
    pid = idea(finder)
    r = client.post(f"/ideas/{pid}/verdict", headers=as_(op), json=vk.idea(pid, "adopted", "abc1234"))
    assert r.status_code == 403 and operator_verdicts.ENV in r.json()["detail"] and "verdict-key new" in r.json()["detail"]
    monkeypatch.setenv(operator_verdicts.ENV, "not-a-key")
    r = client.post(f"/ideas/{pid}/verdict", headers=as_(op), json=vk.idea(pid, "adopted", "abc1234"))
    assert r.status_code == 403 and "64 hex" in r.json()["detail"]
    assert _points(client, finder) == 0
    who = client.get("/operator/whoami", headers=as_(op)).json()
    assert who["verdict_key"] is None and "64 hex" in who["verdict_key_problem"]


def test_the_operator_agents_own_token_and_key_award_no_points(client, monkeypatch):
    """The takeover P1 is about: everything the operator agent's process holds — its bearer
    token, its identity key, its signer's kinds — and no points come of it."""
    vk.register(monkeypatch)
    op, finder = _setup(client, monkeypatch, "finder")
    pid = idea(finder)
    ref = uuid.uuid4().hex[:12]
    # its token alone (the old, unsigned verdict)
    r = client.post(f"/ideas/{pid}/verdict", headers=as_(op), json={"verdict": "adopted", "ref": ref})
    assert r.status_code == 403 and "unsigned" in r.json()["detail"]
    # its own identity key over the payload, naming itself as the verdict key or not
    at = datetime.now(UTC).isoformat()
    own = identity.generate_or_load_keypair(op)[0].public_key().public_bytes_raw().hex()
    for named, why in ((vk.PUB, "does not verify"), (own, "after a rotation")):
        p = wire.signed_idea_verdict_payload(pid, "adopted", ref, "", rs.relay_id(), named, at)
        r = client.post(f"/ideas/{pid}/verdict", headers=as_(op), json={
            "verdict": "adopted", "ref": ref, "at": at, "verdict_key": named, "signature": identity.sign(p, op)})
        assert r.status_code == 403 and why in r.json()["detail"], r.json()
    # an idea verdict as v1 had it: an agent kind no more
    with pytest.raises(signing.Refused, match="no rule"):
        signing.sign(op, "idea-verdict", pid, "adopted", "abc1234", "", op, at)
    p = wire.signed_idea_verdict_payload(pid, "adopted", "abc1234", "", rs.relay_id(), vk.PUB, at)
    r = client.post(f"/ideas/{pid}/verdict", headers=as_(op), json={
        "verdict": "adopted", "ref": "abc1234", "at": at, "signature": identity.sign(p, op)})
    assert r.status_code == 403
    assert _points(client, finder) == 0
    # the person's verdict key, with the operator's sign-in: awarded
    assert client.post(f"/ideas/{pid}/verdict", headers=as_(op), json=vk.idea(pid, "adopted", ref)).status_code == 200
    assert _points(client, finder) == 10


def test_the_verdict_key_alone_is_not_enough_either(client, monkeypatch):
    """The operator's sign-in still comes first: a verdict key signature sent by anyone else is 403."""
    vk.register(monkeypatch)
    op, finder = _setup(client, monkeypatch, "finder")
    pid = idea(finder)
    assert client.post(f"/ideas/{pid}/verdict", json=vk.idea(pid, "noted")).status_code == 401
    assert client.post(f"/ideas/{pid}/verdict", headers=as_(finder), json=vk.idea(pid, "noted")).status_code == 403


def test_a_verdict_is_bound_to_its_fields_its_relay_and_its_time(client, monkeypatch):
    vk.register(monkeypatch)
    op, finder = _setup(client, monkeypatch, "finder")
    pid, other = idea(finder), idea(finder)
    ref = uuid.uuid4().hex[:12]
    adopt = vk.idea(pid, "adopted", ref)
    r = client.post(f"/ideas/{pid}/verdict", headers=as_(op), json={**adopt, "ref": uuid.uuid4().hex[:12]})
    assert r.status_code == 403, "a signed ref is not another ref"
    assert client.post(f"/ideas/{other}/verdict", headers=as_(op), json=adopt).status_code == 403, "another post"
    r = client.post(f"/ideas/{pid}/verdict", headers=as_(op), json=vk.idea(pid, "adopted", ref, relay="another-relay"))
    assert r.status_code == 403 and rs.relay_id() in r.json()["detail"], "another relay's verdict"
    r = client.post(f"/ideas/{pid}/verdict", headers=as_(op),
                    json=vk.idea(pid, "noted", at="2020-01-01T00:00:00+00:00"))
    assert r.status_code == 400, "an old signature"
    assert client.post(f"/ideas/{pid}/verdict", headers=as_(op), json=adopt).status_code == 200


def test_rotation_the_old_key_stops_and_each_verdict_keeps_the_key_that_signed_it(client, monkeypatch):
    vk.register(monkeypatch)
    op, finder = _setup(client, monkeypatch, "finder")
    first, second = idea(finder), idea(finder)
    out1 = client.post(f"/ideas/{first}/verdict", headers=as_(op), json=vk.idea(first, "noted"))
    assert out1.status_code == 200
    new = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    new_pub = vk.register(monkeypatch, new.public_key().public_bytes_raw().hex())    # rotated, relay restarted
    r = client.post(f"/ideas/{second}/verdict", headers=as_(op), json=vk.idea(second, "noted"))
    assert r.status_code == 403 and "after a rotation" in r.json()["detail"]
    out2 = client.post(f"/ideas/{second}/verdict", headers=as_(op), json=vk.idea(second, "noted", key=new))
    assert out2.status_code == 200
    for pid, out, key in ((first, out1.json(), vk.PUB), (second, out2.json(), new_pub)):
        assert out["verdict_key"] == key
        p = wire.signed_idea_verdict_payload(pid, "noted", "", "", rs.relay_id(), key, out["decided_at"])
        assert identity.verify(p, out["signature"], key), "anyone rechecks it from what the relay serves"
    assert client.get("/ideas").json()["verdict_key"] == new_pub


def test_the_verdict_key_is_made_and_kept_on_the_persons_side(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "signer"))
    pub, retired = operator_verdicts.new()
    path = operator_verdicts.key_path()
    assert retired is None and path == tmp_path / "signer" / "operator" / "verdict.priv"
    assert oct(path.stat().st_mode & 0o777) == "0o600" and oct(path.parent.stat().st_mode & 0o777) == "0o700"
    with pytest.raises(operator_verdicts.VerdictKeyError, match="rotate"):
        operator_verdicts.new()
    newer, retired = operator_verdicts.new(rotate=True)
    assert newer != pub and retired.name.startswith("verdict.priv.retired-") and operator_verdicts.public_hex() == newer
    p = wire.signed_idea_verdict_payload("pst_1", "noted", "", "", "r", pub, "t")
    with pytest.raises(operator_verdicts.VerdictKeyError, match="another verdict key"):
        operator_verdicts.sign(p)
    path.parent.chmod(0o755)
    with pytest.raises(operator_verdicts.VerdictKeyError, match="open to other users"):
        operator_verdicts.public_hex()


def test_the_verdict_key_and_its_setting_are_a_persons(tmp_path, monkeypatch, capsys):
    from emporia import cli
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "signer"))
    monkeypatch.setattr(cli, "_agent", lambda n: (tmp_path / "op", "op", {"EMPORIA_AGENT_ID": "op"}))
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setenv("HERMES_SESSION_ID", "an-agent's-terminal")
    assert cli.main(["operator", "op", "verdict-key", "new"]) == 1
    assert "only a person" in capsys.readouterr().out and not operator_verdicts.key_path().exists()
    env = tmp_path / "relay" / ".env"
    env.parent.mkdir()
    env.write_text("EMPORIA_AGENT_ID=op\n")
    monkeypatch.setattr(cli, "_relay_env_path", lambda args: env)
    assert cli.main(["relay", "config", "set", operator_verdicts.ENV, "ab" * 32]) == 1
    assert "only a person" in capsys.readouterr().out and operator_verdicts.ENV not in env.read_text()
    for cmd in ("emporia operator op verdict-key new", "hermes emporia operator op verdict-key rotate",
                f"emporia relay config set {operator_verdicts.ENV} {'ab' * 32}",
                f"echo {operator_verdicts.ENV}={'ab' * 32} >> /srv/relay/.env",
                "cat ~/.emporia-signer/operator/verdict.priv", "cat /srv/s/operator/verdict.priv.retired-1",
                "python -c 'from emporia import operator_verdicts as o; print(o.public_hex())'",
                "python -c 'import emporia.operator_verdicts'"):
        r = guard.pre_tool_call("terminal", {"command": cmd})
        assert r and r["action"] == "block", cmd
    for ok in ("emporia operator op", "emporia operator op whoami", "cat src/emporia/operator_verdicts.py"):
        assert guard.pre_tool_call("terminal", {"command": ok}) is None, ok


# ── P2: `house stop` stops only its own profile's runner ─────────────────────

def _fake_runner(home: Path) -> subprocess.Popen:
    """A process whose command line reads like a house runner's, for `home`: it only sleeps."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "emporia.house",
                             "--home", str(home.resolve()), "--interval", "20"])


def test_house_stop_stops_its_own_profiles_runner_and_no_other(tmp_path, monkeypatch, capsys):
    from emporia import cli
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr(cli, "_agent", lambda n: (tmp_path / n, n, {"EMPORIA_AGENT_ID": n}))
    runner_b = _fake_runner(b)
    try:
        time.sleep(0.3)
        cli._house_files(a)[0].write_text(str(runner_b.pid))     # A's agent points A's PID file at B's runner
        assert cli.main(["house", "stop", "a"]) == 1
        assert "not a's house runner" in capsys.readouterr().out
        assert runner_b.poll() is None, "B's runner still runs"
        assert cli.main(["house", "status", "a"]) == 0 and "not running here" in capsys.readouterr().out
        cli._house_files(b)[0].write_text(str(runner_b.pid))
        assert cli._house_pid(b) == runner_b.pid
        assert cli.main(["house", "stop", "b"]) == 0 and "stopped" in capsys.readouterr().out
        runner_b.wait(timeout=10)
    finally:
        if runner_b.poll() is None:
            runner_b.kill()
            runner_b.wait()


def test_the_runner_names_its_profile_in_its_command_line(tmp_path, monkeypatch):
    from emporia import cli
    started = []

    class P:
        pid = 4242

        def poll(self):
            return None
    monkeypatch.setattr(cli, "_agent", lambda n: (tmp_path, n, {"EMPORIA_AGENT_ID": n}))
    monkeypatch.setattr(cli, "_mcp_env", lambda p: {})
    monkeypatch.setattr(cli, "installer", lambda: type("I", (), {"harvest_env": staticmethod(lambda p: {
        "EMPORIA_PLAYBOOK": "host"})})())
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: started.append(argv) or P())
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["house", "start", "h"]) == 0
    argv = started[0]
    assert argv[1:3] == ["-m", "emporia.house"] and argv[argv.index("--home") + 1] == str(tmp_path.resolve())


# ── P6: paths as they resolve ────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    {"command": "cat ~/.hermes//keys/alpha.json"},
    {"command": "ls ~/.hermes/./keys"},
    {"command": "tar czf /x/k.tgz ~/.hermes/x/../keys"},
    {"command": "cat ~/.hermes/a/b/../../keys/z"},
    {"command": "cd ~/.hermes && tar czf /x/k.tgz keys"},
    {"command": "cd ~/.hermes; ls keys"},
    {"command": "cd /opt/data/profiles && cat .signer/admin.token"},
    {"command": "cat /opt/data/profiles//.signer/policy.json"},
    {"command": "cd ~/plugin-data && ls emporia/console"},
    {"command": "ls keys", "workdir": "~/.hermes"},
    {"path": "/home/a/.hermes/./keys/b.json"},
])
def test_the_key_guard_matches_paths_as_they_resolve(args):
    r = guard.pre_tool_call("terminal", args)
    assert r and r["action"] == "block", args


@pytest.mark.parametrize("cmd", ["cd src && ls emporia", "cd docs && cat protocol.md", "echo a//b ./c ../d",
                                 "curl http://localhost:8088//health", "cd tests && ../.venv/bin/python -m pytest -q"])
def test_normalising_adds_no_false_positive(cmd):
    assert guard.pre_tool_call("terminal", {"command": cmd}) is None


# ── P7: MetaMask's Guard Mode check, remembered across requests ──────────────

def _mode_checks(m):
    return [c for c in m["state"]()["calls"] if c[:3] == ["wallet", "trading-mode", "get"]]


def test_guard_mode_is_checked_once_per_interval_across_requests(setup, mm, monkeypatch):  # noqa: F811
    ref = signer.get_backend("mm1").make(AGENT, "evm")
    msg = _hold_like(ref)
    for _ in range(3):                          # a new back end per request, as the signer builds them
        signer.get_backend("mm1").sign_typed_data(AGENT, msg, ref)
    assert len(_mode_checks(mm)) == 1
    clock = [time.monotonic() + sb.MetaMaskBackend.GUARD_TTL_S + 1]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    st = mm["state"]()
    st["wallets"][ref]["mode"] = "beast"        # switched after the last check
    mm["set"](wallets=st["wallets"])
    with pytest.raises(sb.BackendError, match="Beast Mode"):
        signer.get_backend("mm1").sign_typed_data(AGENT, msg, ref)
    with pytest.raises(sb.BackendError, match="Beast Mode"):
        signer.get_backend("mm1").sign_typed_data(AGENT, msg, ref)   # a refusal is never remembered
    assert len(_mode_checks(mm)) == 3
    st["wallets"][ref]["mode"] = "guard"
    mm["set"](wallets=st["wallets"])
    signer.get_backend("mm1").sign_typed_data(AGENT, msg, ref)
    b = signer.get_backend("mm1")
    signer.save_backend("mm1", "metamask", {**b.settings, "wait_seconds": "30"}, b.secrets)   # set up again
    signer.get_backend("mm1").sign_typed_data(AGENT, msg, ref)
    assert len(_mode_checks(mm)) == 5, "a back end set up again is asked afresh"


