"""P126: the local console — signs in to the relay as a local agent with that
agent's own key, opens its sealed DMs, and answers only its owner."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from emporia import crypto_box, identity
from emporia.console.agents import discover
from emporia.console.app import COOKIE, Console, build_app
from emporia.relay_server import app as relay_app
from tests.test_emporia import _enc_pair


@pytest.fixture
def client():
    return TestClient(relay_app, client=("127.0.0.1", 51217))


def _console(session_token=None):
    transport = httpx.ASGITransport(app=relay_app, client=("203.0.113.9", 4000))
    return Console([identity.KEY_DIR], "http://relay.test", session_token=session_token,
                   transport=transport)


def test_an_agents_own_profile_keys_win_over_a_stale_copy(tmp_path):
    for profile, agents in (("alpha", ["alpha"]), ("hub", ["alpha", "hub"])):
        d = tmp_path / profile / "home" / ".hermes" / "keys"
        d.mkdir(parents=True)
        for a in agents:
            (d / f"{a}.priv").write_bytes(b"x" * 32)
        (d / "_relay_identity.priv").write_bytes(b"x" * 32)
    found = discover(sorted(tmp_path.glob("*/home/.hermes/keys"), reverse=True))
    assert sorted(found) == ["alpha", "hub"]
    assert found["alpha"].key_dir.parts[-4] == "alpha"


def test_the_console_signs_in_as_the_agent_with_its_own_key(client):
    """From a non-local address and without any agent header: only the agent's
    signed session can make the relay answer as that agent."""
    _enc_pair(client, "p126_a", "p126_b")
    c = TestClient(build_app(_console()))
    r = c.get("/relay/p126_a/agents/p126_a/inbox?unread_only=true",
              headers={"x-emporia-agent-id": "p126_b"})       # ignored, never forwarded
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == "p126_a"
    assert r.headers["cache-control"] == "no-store"
    assert c.get("/relay/nobody_here/agents").status_code == 404
    assert "p126_a" in c.get("/agents").json()["agents"]


def test_the_console_opens_a_sealed_dm_for_its_recipient(client):
    _enc_pair(client, "p126_c", "p126_d")
    pubs = {a: identity.get_encryption_public_key_hex(a) for a in ("p126_c", "p126_d")}
    nonce = "a" * 32
    env = crypto_box.seal("for d only", pubs,
                          crypto_box.message_aad("dm", "dm_x", "p126_c", "chat", nonce))
    msgs = [{"message_id": "m1", "sender_id": "p126_c", "msg_type": "chat", "nonce": nonce,
             "content": json.dumps(env)}, {"message_id": "m2", "content": "plain"}]
    c = TestClient(build_app(_console()))
    r = c.post("/dm/p126_d/open", json={"thread_id": "dm_x", "messages": msgs})
    assert r.json() == {"opened": {"m1": "for d only"}}
    other = c.post("/dm/p126_d/open", json={"thread_id": "dm_other", "messages": msgs})
    assert other.json() == {"opened": {"m1": None}}


def test_standalone_answers_only_its_link_holder_on_localhost(client):
    _enc_pair(client, "p126_e", "p126_f")
    c = TestClient(build_app(_console(session_token="s" * 32)), base_url="http://127.0.0.1:8089")
    assert c.get("/agents").status_code == 401
    assert c.get("/session?key=wrong", follow_redirects=False).status_code == 401
    ok = c.get("/session?key=" + "s" * 32, follow_redirects=False)
    assert ok.status_code == 303 and COOKIE in ok.headers["set-cookie"]
    assert "httponly" in ok.headers["set-cookie"].lower()
    assert "samesite=strict" in ok.headers["set-cookie"].lower()
    assert c.get("/agents").status_code == 200
    rebound = TestClient(build_app(_console(session_token="s" * 32)),
                         base_url="http://attacker.example")
    rebound.cookies.set(COOKIE, "s" * 32)
    assert rebound.get("/agents").status_code == 401, "a non-local Host is refused"
    # P198: the link opens once
    assert c.get("/session?key=" + "s" * 32, follow_redirects=False).status_code == 401


def test_the_console_only_reads_as_the_agent(client):
    """P198: whatever the agent's keys could do, the dashboard only watches."""
    _enc_pair(client, "p198_a", "p198_b")
    c = TestClient(build_app(_console()))
    for method in ("post", "put", "patch", "delete"):
        r = getattr(c, method)("/relay/p198_a/listings") if method != "delete" else \
            c.delete("/relay/p198_a/admin/sessions/x")
        assert r.status_code == 405, (method, r.status_code)
    # P234: nor marking its own inbox read — read means the agent has had it
    assert c.post("/relay/p198_a/agents/p198_a/inbox/mark-read", json=[]).status_code == 405
    assert c.post("/relay/p198_a/agents/p198_a/inbox/mark-all-read").status_code == 405


def test_the_console_checks_a_game_itself_with_the_agents_own_receipts(client):
    """P146: the dashboard's verdict comes from this machine, not the relay."""
    import uuid

    from emporia import calls, commit_reveal
    from tests.test_emporia import _register_keyed, _signed_action_body
    a, b = f"cv_{uuid.uuid4().hex[:6]}", f"cw_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register_keyed(client, x)
    sid = client.post("/sessions", json={"module_type": "emporia:blotto:v2", "creator_agent_id": a},
                      headers={"x-emporia-agent-id": a}).json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers={"x-emporia-agent-id": b})
    moves, nonces = {a: [30, 30, 30, 5, 5], b: [20] * 5}, {a: commit_reveal.new_nonce(), b: commit_reveal.new_nonce()}
    rdir = identity.KEY_DIR / "receipts"
    rdir.mkdir(exist_ok=True)
    for action in ("commit", "reveal"):
        for x in (a, b):
            payload = ({"commitment": calls.commitment(moves[x], None, nonces[x])} if action == "commit"
                       else {"move": moves[x], "nonce": nonces[x]})
            r = client.post(f"/sessions/{sid}/action",
                            json=_signed_action_body(client, sid, x, action, payload, "Reading this opponent"))
            if x == a:
                with (rdir / f"{a}.{sid}.jsonl").open("a") as f:
                    f.write(json.dumps(r.json()["receipt"]) + "\n")
    c = TestClient(build_app(_console()))
    report = c.get(f"/verify/{a}/{sid}").json()
    assert report["verdict"] == "ok", [f for f in report["findings"] if f["status"] != "ok"]
    assert sum(f["check"] == "receipt" and f["status"] == "ok" for f in report["findings"]) == 2
    assert c.get(f"/verify/{a}/sess_missing").status_code == 404


def test_the_console_opens_a_private_sessions_sealed_text(client):
    """P149: a sealed deliverable, opened for its player on this machine only."""
    import uuid
    from tests.test_emporia import _enc_pair
    a, b = f"co_{uuid.uuid4().hex[:6]}", f"cp_{uuid.uuid4().hex[:6]}"
    _enc_pair(client, a, b)
    env = json.dumps(crypto_box.seal(
        "the report", {x: identity.get_encryption_public_key_hex(x) for x in (a, b)},
        crypto_box.session_aad("sess_q", "deliverable")))
    c = TestClient(build_app(_console()))
    r = c.post(f"/session/{a}/sess_q/open", json={"items": [
        {"id": "1", "field": "deliverable", "content": env},
        {"id": "2", "field": "deliverable", "content": "plain"},
        {"id": "3", "field": "chat", "content": env}]})
    assert r.json() == {"opened": {"1": "the report", "3": None}}, "bound to its field"


def test_settings_are_read_only_and_hold_no_secret(tmp_path, monkeypatch):
    """P178: each local agent's settings, with the command that changes each."""
    from unittest.mock import patch as _patch

    from emporia import cli
    profile = tmp_path / "profiles" / "lambda"
    keys = profile / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    (profile / "config.yaml").write_text("mcp_servers:\n  emporia:\n    env:\n      EMPORIA_AGENT_ID: lambda\n"
                                         f"      EMPORIA_KEYS_DIR: {keys}\n")
    secret = "11" * 32
    (profile / ".env").write_text("EMPORIA_X402_PAY_NETWORKS=eip155:84532\nEMPORIA_X402_EVM_PRIVATE_KEY=0x" + secret
                                  + "\nEMPORIA_X402_EVM_KEY_SOURCE=env:EMPORIA_X402_EVM_PRIVATE_KEY\n")
    with _patch.object(identity, "KEY_DIR", keys):
        identity.generate_or_load_keypair("lambda")
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    c = TestClient(build_app(Console([keys], "http://relay.test", session_token=None)))
    r = c.get("/settings")
    assert r.status_code == 200 and secret not in r.text
    [agent] = r.json()["agents"]
    items = {i["label"]: i for i in agent["items"]}
    assert items["Mainnet (real money)"]["value"].startswith("locked")
    assert items["Mainnet (real money)"]["person_only"] is True
    assert items["EVM wallet"]["value"].startswith("0x") and "its .env" in items["EVM wallet"]["about"]
    assert items["Asks you above"]["level"] == "warn"
    assert items["Where it may pay"]["command"] == "emporia wallet limits lambda --networks base-sepolia,solana-devnet"
    assert c.post("/settings").status_code == 405, "nothing is changed from the browser"


def test_settings_are_drawn_like_hermes_form_and_name_only_what_hermes_can_change(tmp_path, monkeypatch):
    """The Settings view groups like Hermes's config page and draws each value as its form would: a switch,
    the chosen option among its options, a number with its unit. `env` names a setting only when Hermes's
    own dashboard can change it (plugin.yaml optional_env, never a secret); a secret is only set or not set."""
    import yaml

    from emporia import cli
    profile = tmp_path / "profiles" / "mu"
    keys = profile / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    (profile / "config.yaml").write_text(f"mcp_servers:\n  emporia:\n    env:\n      EMPORIA_KEYS_DIR: {keys}\n")
    secret = "22" * 32
    (profile / ".env").write_text("EMPORIA_RELAY_URL=http://relay.test\nEMPORIA_X402_EVM_PRIVATE_KEY=0x" + secret
                                  + "\nEMPORIA_X402_EVM_KEY_SOURCE=env:EMPORIA_X402_EVM_PRIVATE_KEY\nEMPORIA_THREAD_LIMIT=0\n"
                                  "EMPORIA_WAKE=open\nEMPORIA_SIGNER_URL=http://127.0.0.1:1\n"
                                  "EMPORIA_SIGNER_TOKEN=tok-" + secret + "\nEMPORIA_DECLARE_MODEL=off\n")
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    report = cli.agent_settings(profile, "mu")
    assert secret not in json.dumps(report)
    groups = list(dict.fromkeys(i["group"] for i in report))
    assert groups == [g for g in cli.SETTINGS_GROUPS if g in groups], "sections come in the view's order"
    assert {"Relay & identity", "Who can reach the agent", "Features & tools", "Money & limits", "Keys & signer",
            "Sharing"} <= set(groups)
    items = {i["label"]: i for i in report}
    wake = items["Wake mode"]
    assert (wake["kind"], wake["current"], wake["options"], wake["recommended"], wake["is_default"]) == (
        "choice", "open", ["closed", "direct", "open"], "direct", False)
    assert wake["env"] == "EMPORIA_WAKE"
    limit = items["Messages per conversation"]
    assert (limit["kind"], limit["value"], limit["level"]) == ("number", "no limit", "warn")
    assert items["Turns other agents can start"]["value"] == "300 per 24 hours"
    assert items["Turns other agents can start"]["is_default"] is True
    assert items["Largest single payment"]["value"] == "$10.00 per payment"
    assert items["Declare its model"]["current"] == "off" and items["Declare its model"]["recommended"] == "on"
    assert items["Signer token"] == {**items["Signer token"], "kind": "secret", "value": "set", "current": "set"}
    assert "env" not in items["Signer token"], "a secret is never offered for editing"
    declared = {e["name"]: e for e in yaml.safe_load(
        (cli.REPO / "hermes_plugin" / "emporia" / "plugin.yaml").read_text())["optional_env"]}
    for i in report:
        assert i.get("kind") != "secret" or i["value"] in ("set", "not set"), i
        if "env" in i:
            assert declared[i["env"]]["password"] is False, i["env"]
        assert i["about"].endswith(".") and len(i["about"]) <= 120, i["about"]
    machine = cli.machine_settings("http://relay.test")
    assert {i["group"] for i in machine} == {"This machine"}


def test_the_console_reads_the_agents_own_log_check_and_never_runs_one(tmp_path, monkeypatch):
    """P281: the dashboard shows what the agent's own last check of its relay's transparency log found,
    from the state it keeps (log_states.json): numbers and words, no key, no signature. Nothing is fetched
    from the relay and nothing is written; a never-checked relay is `kept: null`."""
    from unittest.mock import patch

    from emporia import cli, logcheck
    keys = tmp_path / "profiles" / "nu" / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    with patch.object(identity, "KEY_DIR", keys):
        identity.generate_or_load_keypair("nu")
    c = TestClient(build_app(Console([keys], "http://relay.test", session_token=None)))
    assert c.get("/log-check/nu").json() == {"relay": "http://relay.test", "kept": None}
    signature = "ab" * 64
    state = {"type": "emporia:log-state:v1", "relay_public_key": "cd" * 32, "size": 1204, "root": "ef" * 32,
             "at": "2026-11-09T11:55:00+00:00", "epoch": 2, "signature": signature}
    (keys / logcheck.CLIENT_FILE).write_text(json.dumps({"relays": {"http://relay.test": {
        "state": state, "status": "fail", "detail": "the log shrank from 1300 to 1204 entries",
        "checked_at": "2026-11-09T11:58:00+00:00",
        "failures": [{"at": "x", "detail": "shrank", "evidence": {"states": [state]}}]}}}))
    before = (keys / logcheck.CLIENT_FILE).read_text()
    monkeypatch.setattr(logcheck, "check", lambda *a, **k: pytest.fail("the console ran a check"))
    monkeypatch.setattr(cli, "health", lambda *a, **k: pytest.fail("the console asked the relay"))
    r = c.get("/log-check/nu")
    assert r.status_code == 200
    assert r.json()["kept"] == {"status": "fail", "ok": False, "detail": "the log shrank from 1300 to 1204 entries",
                                "checked_at": "2026-11-09T11:58:00+00:00", "size": 1204, "epoch": 2,
                                "state_at": "2026-11-09T11:55:00+00:00", "failures": 1}
    assert signature not in r.text and "cd" * 32 not in r.text and "ef" * 32 not in r.text
    assert (keys / logcheck.CLIENT_FILE).read_text() == before, "nothing written"
    assert c.post("/log-check/nu").status_code == 405
    assert c.get("/log-check/nobody").status_code == 404


def test_the_console_opens_an_encrypted_rooms_messages_for_a_member(client):
    """P238: rooms showed 🔒 only on the dashboard; DMs opened on this machine."""
    _enc_pair(client, "p238_a", "p238_b")
    aad = crypto_box.message_aad("room", "room_x", "p238_b", "chat", "n1")
    env = crypto_box.seal("hello room", {"p238_a": identity.get_encryption_public_key_hex("p238_a")}, aad)
    c = TestClient(build_app(_console()))
    r = c.post("/room/p238_a/open", json={"room_id": "room_x", "messages": [
        {"message_id": "m1", "sender_id": "p238_b", "msg_type": "chat", "nonce": "n1",
         "content": json.dumps(env)}]})
    assert r.status_code == 200 and r.json()["opened"] == {"m1": "hello room"}
    assert c.post("/room/p238_a/open", json={}).status_code == 400


def test_the_dashboards_console_exits_when_the_dashboard_does(tmp_path):
    """P239: a console that outlived its dashboard served day-old code."""
    import subprocess
    import sys
    import time
    from pathlib import Path
    if not Path("/proc/self/status").exists():
        pytest.skip("needs /proc")
    repo = Path(__file__).resolve().parents[1]
    marker = tmp_path / "child.pid"
    parent = subprocess.Popen([sys.executable, "-c", f"""
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c",
    "import time\\nfrom emporia.console.__main__ import _exit_with_parent\\n_exit_with_parent(0.2)\\ntime.sleep(30)"],
    cwd={str(repo)!r}, start_new_session=True)
open({str(marker)!r}, "w").write(str(child.pid))
time.sleep(0.5)
"""], cwd=str(repo))
    parent.wait(timeout=20)
    pid = int(marker.read_text())
    status = Path(f"/proc/{pid}/status")
    for _ in range(50):
        # gone, or a zombie nobody has reaped yet (a container's init may not)
        if not status.exists() or "\nState:\tZ" in status.read_text():
            break
        time.sleep(0.1)
    else:
        raise AssertionError("the console kept running after its parent exited")


def test_the_console_serves_the_listed_docs_and_nothing_else():
    """P295: the Hermes tab's bundle carries no doc text (the catalog's install scan
    reads a bundled doc as one line); its console serves the dashboard's docs."""
    c = TestClient(build_app(_console()))
    r = c.get("/docs/docs/payments.md")
    assert r.status_code == 200 and r.text.startswith("#") and "markdown" in r.headers["content-type"]
    assert c.get("/docs/README.md").status_code == 200
    for path in ("docs/security-log.md", "pyproject.toml", "../README.md", ".env"):
        assert c.get(f"/docs/{path}").status_code == 404, path


def test_the_standalone_console_sends_a_strict_content_security_policy(tmp_path):
    """Security follow-up (11-05 audit): the standalone console serves the dashboard next to
    the agents' keys and sent no Content-Security-Policy: a script injected into the page
    (a relay-supplied string rendered unsafely, a compromised dependency) could load code
    from anywhere and send what the page reads anywhere. Every response now carries a CSP
    allowing this origin only — no inline script or style except by hash of what the
    served index.html holds — plus nosniff and no referrer."""
    import base64
    import hashlib
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    inline = "window.__boot = 1;"
    (ui / "index.html").write_text(
        '<!doctype html><html><head><script type="module" src="/assets/a.js"></script>'
        f"<script>{inline}</script><style>body{{margin:0}}</style></head><body></body></html>")
    (ui / "assets" / "a.js").write_text("console.log(1)")
    c = TestClient(build_app(_console(session_token="s" * 32), ui_dir=ui), base_url="http://127.0.0.1:8089")
    refused = c.get("/agents")
    c.get("/session?key=" + "s" * 32, follow_redirects=False)
    for r in (refused, c.get("/"), c.get("/assets/a.js"), c.get("/agents")):
        csp = r.headers.get("content-security-policy", "")
        rules = dict((part.split(" ", 1) + [""])[:2] for part in (p.strip() for p in csp.split(";")) if part)
        assert rules.get("default-src") == "'none'", (r.url, csp)
        assert rules.get("frame-ancestors") == "'none'" and rules.get("object-src") == "'none'"
        assert rules.get("base-uri") == "'none'"
        assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp and "*" not in csp
        assert rules["connect-src"] == "'self'"
        script_hash = "'sha256-" + base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode() + "'"
        style_hash = "'sha256-" + base64.b64encode(hashlib.sha256(b"body{margin:0}").digest()).decode() + "'"
        assert rules["script-src"] == f"'self' {script_hash}", csp
        assert rules["style-src"] == f"'self' {style_hash}", csp
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert r.headers.get("referrer-policy") == "no-referrer"
    # With no inline code in the page (the built bundle has none), only this origin.
    (ui / "index.html").write_text('<!doctype html><script type="module" src="/assets/a.js"></script>')
    plain = TestClient(build_app(_console(), ui_dir=ui)).get("/").headers["content-security-policy"]
    assert "script-src 'self';" in plain and "style-src 'self';" in plain


def test_the_console_serves_every_listed_doc_as_json_for_the_desktop_page():
    """The Desktop page reaches the console through ctx.rest, which carries JSON only."""
    from pathlib import Path
    doc_list = Path(__file__).resolve().parents[1] / "dashboard" / "src" / "docList.json"
    listed = {d["path"] for d in json.loads(doc_list.read_text())}
    texts = TestClient(build_app(_console())).get("/doc-texts").json()["texts"]
    assert set(texts) == listed
    assert texts["docs/payments.md"].startswith("#")
    assert "docs/security-log.md" not in texts


def test_the_dashboards_console_answers_only_with_its_secret():
    """Console scope (the 11-05 audit's proposal): on its socket the console took any request —
    the 0700 folder was the only gate, and an agent's terminal on the `local` backend runs as
    that folder's user. Now each start has a secret the plugin gives it on stdin and presents
    on every request and websocket; without it, 401 (or a closed socket)."""
    from starlette.websockets import WebSocketDisconnect
    secret = "k" * 43
    transport = httpx.ASGITransport(app=relay_app, client=("203.0.113.9", 4000))
    c = TestClient(build_app(Console([identity.KEY_DIR], "http://relay.test", session_token=None,
                                     socket_secret=secret, transport=transport)))
    assert c.get("/agents").status_code == 401
    assert c.get("/agents", headers={"x-emporia-console-secret": "k" * 42}).status_code == 401
    assert c.get("/agents", headers={"x-emporia-console-secret": secret}).status_code == 200
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/alpha/ws/agent/alpha") as ws:
            ws.receive_text()


def test_the_dashboards_console_serves_its_own_profile_only(tmp_path, monkeypatch):
    """Console scope: the plugin started one console for every profile under the Hermes root
    (`--hermes-root`), so one profile's tab — or anything that reached its socket — read every
    other profile's inbox and had their sealed DMs opened. On a socket the console now serves
    one profile (`--profile`), refuses `--hermes-root` and `--keys-dir`, and will not start
    without a secret on stdin."""
    import io

    import uvicorn

    from emporia.console import __main__ as console_main
    profile = tmp_path / "profiles" / "alpha"
    keys = profile / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    (keys / "alpha.priv").write_bytes(b"\0" * 32)
    other = tmp_path / "profiles" / "beta" / "home" / ".hermes" / "keys"
    other.mkdir(parents=True)
    (other / "beta.priv").write_bytes(b"\0" * 32)
    served = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))
    sock = str(tmp_path / "c" / "console.sock")

    def run(*argv, stdin="s3cret-from-the-plugin-at-this-start-0123\n"):
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        served.clear()
        return console_main.main(["--uds", sock, *argv])

    assert run("--profile", str(profile)) == 0 and served["uds"] == sock
    c = TestClient(served["app"])
    # The Operator section's `profiles` (each agent's profile name, for its commands) rides along.
    listed = c.get("/agents", headers={"x-emporia-console-secret": "s3cret-from-the-plugin-at-this-start-0123"}).json()
    assert listed["agents"] == ["alpha"] and "beta" not in listed.get("profiles", {}), "this profile's agent, not beta's"
    assert c.get("/agents").status_code == 401
    assert run("--profile", str(profile), stdin="") == 1 and not served, "no secret, no console"
    assert run() == 1 and not served, "a socket console names its profile"
    assert run("--profile", str(profile), "--hermes-root", str(tmp_path)) == 1 and not served
    assert run("--profile", str(profile), "--keys-dir", str(other)) == 1 and not served


def test_the_console_makes_the_text_for_the_post_on_x_button_and_sends_nothing(client):
    """The dashboard's button: a GET that signs the claim with the agent's own key and returns the stock post and
    X's composer link. It writes nothing to the relay, and a bad handle or style is refused."""
    from emporia import wire, x_link
    _enc_pair(client, "xc_a", "xc_b")
    c = TestClient(build_app(_console()))
    r = c.get("/x-claim/xc_a?handle=@Kestrel_X&style=arena")
    assert r.status_code == 200, r.text
    got = r.json()
    parsed = x_link.parse_line(got["post_text"])
    assert got["post_text"].endswith(parsed["line"]) and got["handle"] == "kestrel_x" and got["post_length"] <= 280
    assert got["intent_url"].startswith("https://x.com/intent/post?text=") and "EMP0R" in got["post_text"]
    profile = c.get("/relay/xc_a/agents/xc_a").json()
    assert x_link.verify_line(parsed, profile["stable_id"], "kestrel_x", profile["public_key_hex"])
    assert not x_link.verify_line(parsed, profile["stable_id"], "someone_else", profile["public_key_hex"])
    assert wire.clean_x_handle("https://x.com/me") == ""
    assert c.get("/x-claim/xc_a?handle=https://x.com/me").status_code == 400
    assert c.get("/x-claim/xc_a?handle=ok&style=nope").status_code == 400
    assert c.get("/x-claim/nobody_here?handle=ok").status_code == 404
    assert c.post("/x-claim/xc_a?handle=ok").status_code in (404, 405)


def test_a_relays_answer_is_served_as_data_never_as_a_page(client):
    """Client audit C1: a hostile relay chose the Content-Type served on the console's origin, and
    'self' in the CSP covered /relay/…, so HTML with a script from the relay ran as the console."""
    from emporia.console.relay import response_headers
    html = httpx.Response(200, headers={"content-type": "text/html", "location": "https://x.example",
                                        "set-cookie": "a=b", "content-security-policy": "script-src *"},
                          content=b"<script>alert(1)</script>")
    h = {k.lower(): v for k, v in response_headers(html).items()}
    assert h["content-type"].startswith("text/plain") and h["x-content-type-options"] == "nosniff"
    assert "location" not in h and "set-cookie" not in h and h["content-security-policy"].startswith("sandbox")
    ok = httpx.Response(200, headers={"content-type": "application/json", "x-next-since": "t"}, content=b"{}")
    assert {k.lower(): v for k, v in response_headers(ok).items()}["content-type"] == "application/json"
    _enc_pair(client, "c1_a", "c1_b")
    r = TestClient(build_app(_console())).get("/relay/c1_a/agents")
    assert r.status_code == 200 and r.headers["content-security-policy"].startswith("sandbox")
