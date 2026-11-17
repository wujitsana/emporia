"""The remote endpoint's tool surface, guards, limits, hosts and audit log."""
import json

import pytest
from remote_support import AGENT, URL, Rig, stub_mcp

from emporia import autonomy, dm_access, features, identity_card, mcp_surface
from emporia import remote as R
from emporia.remote import server, surface
from emporia.remote.audit import AuditLog, keylike, summarize


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def _registered(mcp) -> set[str]:
    from fastmcp.tools import Tool
    return {c.name for c in mcp.local_provider._components.values() if isinstance(c, Tool)}


# ── which tools exist ─────────────────────────────────────────────────────────────────

async def test_the_exact_remote_surface_for_each_scope(tmp_path, monkeypatch):
    expect = {("emporia:read",): surface.READ_TOOLS, ("emporia:talk",): surface.TALK_TOOLS,
              ("emporia:read", "emporia:talk"): surface.READ_TOOLS | surface.TALK_TOOLS}
    for scopes, tools in expect.items():
        rig = Rig(tmp_path / "-".join(s.split(":")[1] for s in scopes), monkeypatch)
        async with rig.running() as c:
            tok = await c.tokens(scope=" ".join(scopes))
            assert await c.tool_names(tok["access_token"]) == set(tools), scopes
    # and nothing is in the process but the allowlist: the others are gone, not hidden
    assert _registered(rig.mcp) == set(surface.REMOTE_TOOLS)


def test_person_only_settings_and_money_tools_are_never_on_the_remote_surface():
    never = (autonomy.MONEY_TOOLS | autonomy.SETTINGS_TOOLS | autonomy.PERSON_ONLY_TOOLS | autonomy.RELAY_REFUSED_TOOLS
             | {"fund_session", "register_x402_wallet", "confirm_delivery", "dispute_delivery", "reclaim_escrow",
                "join_room", "subscribe_agora_topic", "join_session", "create_session", "challenge", "create_room",
                "create_agora_topic", "money_info", "move_home", "leave_relay", "visit_relay", "archive_now",
                "read_archive", "close_room", "kick_from_room", "kick_from_agora_topic", "delete_item", "x_claim_line",
                "x_link", "confirm_x_link", "github_claim_line", "set_cross_relay_dms", "send_dm", "get_dm_messages",
                "list_dm_threads", "block_dm_sender", "assay", "register_agent"})
    assert not (surface.REMOTE_TOOLS & never), sorted(surface.REMOTE_TOOLS & never)
    assert not (surface.TALK_TOOLS - autonomy.WRITE_TOOLS), "a talk tool the write window does not count"
    assert not (surface.READ_TOOLS & autonomy.WRITE_TOOLS), "a read tool that writes"


def test_the_allowlist_names_real_tools_and_only_read_tools_a_spectator_may_have():
    from emporia import mcp_server
    registered = _registered(mcp_server.mcp)
    assert registered >= surface.REMOTE_TOOLS, sorted(surface.REMOTE_TOOLS - registered)
    assert surface.READ_TOOLS <= features.READ_TOOLS
    assert surface.READ_TOOLS & features.hidden_by_features(features.OPTIONAL, read_only=True) == set()


async def test_a_new_tool_is_absent_remotely_until_it_is_added_on_purpose(tmp_path, monkeypatch):
    mcp = stub_mcp()

    @mcp.tool(name="brand_new_tool")
    def brand_new_tool() -> dict:
        return {"success": True, "ran": True}
    rig = Rig(tmp_path, monkeypatch, mcp=mcp)
    async with rig.running() as c:
        tok = await c.tokens()
        assert "brand_new_tool" not in await c.tool_names(tok["access_token"])
        out = await c.tool(tok["access_token"], "brand_new_tool")
        assert "not available" in out["error"]
    assert "brand_new_tool" not in _registered(mcp)


async def test_a_money_or_person_only_tool_called_by_name_is_refused(rig):
    async with rig.running() as c:
        tok = await c.tokens()
        for name in ("fund_session", "get_dm_messages", "send_dm", "set_cross_relay_dms", "delete_item", "assay",
                     "visit_relay", "money_info"):
            out = await c.tool(tok["access_token"], name)
            assert "not available through a remote connector" in out["error"], name


async def test_a_read_only_agent_can_only_be_granted_read(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, read_only=True)
    assert rig.info["ceiling"] == (R.SCOPE_READ,) and rig.info["notes"]
    async with rig.running() as c:
        r = await c.http.post("/register", json={"client_name": "x", "redirect_uris": ["http://127.0.0.1:1/c"],
                                                 "scope": "emporia:read emporia:talk"})
        assert r.status_code == 400
        assert (await c.http.get("/.well-known/oauth-authorization-server")).json()["scopes_supported"] == [
            "emporia:read"]
    assert surface.ceiling_for((R.SCOPE_READ, R.SCOPE_TALK), True)[0] == (R.SCOPE_READ,)


async def test_another_agent_id_is_refused_and_the_bound_one_is_fine(rig):
    async with rig.running() as c:
        tok = await c.tokens()
        t = tok["access_token"]
        for name, args in (("read_feed", {"agent_id": "someone-else"}), ("post", {"text": "x", "agent_id": "mallory"}),
                           ("my_quests", {"agent_id": "bob"}), ("get_inbox", {"agent_id": "bob"})):
            assert "one agent only" in (await c.tool(t, name, **args))["error"], name
        assert (await c.tool(t, "my_quests", agent_id=AGENT))["success"] is True
        assert (await c.tool(t, "my_quests", agent_id=AGENT.upper()))["success"] is True
        assert (await c.tool(t, "my_quests"))["success"] is True


async def test_arguments_that_make_a_read_tool_write_or_fetch_are_refused_and_limit_is_clamped(rig):
    async with rig.running() as c:
        t = (await c.tokens())["access_token"]
        assert "refused remotely" in (await c.tool(t, "chronicle", join=True))["error"]
        assert (await c.tool(t, "chronicle"))["joined"] is False
        assert "refused remotely" in (await c.tool(t, "scan_relays", relays=["http://169.254.169.254"]))["error"]
        assert (await c.tool(t, "read_feed", limit=9999))["limit"] == surface.MAX_LIMIT


def test_a_server_will_not_start_in_the_situations_the_design_names(monkeypatch):
    ok = dict(public_url="https://a.example", bind="127.0.0.1:8789", behind_proxy=False, agent_id="a-agent",
              has_key=True)
    monkeypatch.setenv("EMPORIA_UNTRACKED_TURNS", "")
    assert server.startup_problems(**ok) == []
    monkeypatch.setenv("EMPORIA_UNTRACKED_TURNS", "person")
    assert any("UNTRACKED_TURNS" in p for p in server.startup_problems(**ok))
    monkeypatch.setenv("EMPORIA_UNTRACKED_TURNS", "")
    assert any("no key" in p for p in server.startup_problems(**{**ok, "has_key": False}))
    assert any("no agent" in p for p in server.startup_problems(**{**ok, "agent_id": ""}))
    for url in ("http://a.example", "ftp://a.example", "https://a.example/mcp", "https://a.example?x=1", "a.example", ""):
        assert server.startup_problems(**{**ok, "public_url": url}), url
    assert server.startup_problems(**{**ok, "public_url": "http://127.0.0.1:8789"}) == []
    assert server.startup_problems(**{**ok, "public_url": "http://localhost:8789"}) == []
    assert any("not loopback" in p for p in server.startup_problems(**{**ok, "bind": "0.0.0.0:8789"}))
    assert any("not loopback" in p for p in server.startup_problems(**{**ok, "bind": "192.168.1.5:8789"}))
    assert server.startup_problems(**{**ok, "bind": "0.0.0.0:8789", "behind_proxy": True}) == []
    assert server.startup_problems(**{**ok, "bind": "[::1]:8789"}) == []


def test_main_refuses_to_start_with_untracked_turns_person(monkeypatch, capsys):
    monkeypatch.setenv("EMPORIA_AGENT_ID", "a-agent")
    monkeypatch.setenv("EMPORIA_UNTRACKED_TURNS", "person")
    monkeypatch.setattr(server, "agent_has_key", lambda a: True)
    assert server.main(["--public-url", "https://a.example"]) == 2
    assert "UNTRACKED_TURNS" in capsys.readouterr().err


def test_scopes_are_parsed_and_the_ones_not_offered_are_an_error():
    assert server.parse_scopes("read,talk") == (R.SCOPE_READ, R.SCOPE_TALK)
    assert server.parse_scopes("") == (R.SCOPE_READ,)
    for bad in ("private", "play", "read,admin"):
        with pytest.raises(ValueError):
            server.parse_scopes(bad)


# ── guards ────────────────────────────────────────────────────────────────────────────

async def test_the_write_window_is_shared_by_every_grant_of_the_agent(rig):
    async with rig.running() as c:
        a = (await c.tokens())["access_token"]
        b = (await c.tokens())["access_token"]
        assert a != b
        outcomes = []
        for i in range(6):
            tok = a if i % 2 == 0 else b
            outcomes.append("error" not in await c.tool(tok, "post", text=f"hello {i}"))
    assert outcomes == [True] * autonomy.UNTRACKED_WRITES + [False]


async def test_a_refused_write_says_why_and_is_in_the_audit_log(rig):
    async with rig.running() as c:
        t = (await c.tokens())["access_token"]
        for i in range(autonomy.UNTRACKED_WRITES):
            await c.tool(t, "post", text=f"w{i}")
        out = await c.tool(t, "post", text="one too many")
    assert out["success"] is False and out["refused_by"] == "emporia-autonomy" and "5 per 10 minutes" in out["error"]
    assert any(r.get("outcome") == "refused:emporia-autonomy" for r in rig.audit.rows())


def test_a_remote_call_gets_the_guards_of_a_turn_another_agents_message_started(monkeypatch):
    monkeypatch.setenv("EMPORIA_UNTRACKED_TURNS", "person")       # the remote mark wins over it
    token = autonomy.REMOTE_CALL.set("conn")
    try:
        assert autonomy.current().autonomous and autonomy.current().origin == "remote"
        for tool, args in (("kick_from_room", {"room_id": "r", "agent": "x"}), ("close_room", {"room_id": "r"}),
                           ("x_link", {}), ("x_claim_line", {}), ("github_claim_line", {}),
                           ("confirm_x_link", {}), ("set_cross_relay_dms", {"enabled": True}),
                           ("block_dm_sender", {"sender": "bob"}), ("block_dm_sender", {"sender": ""})):
            out = autonomy.guard(tool, args, AGENT)
            assert out and out["refused_by"] == "emporia-autonomy", tool
        assert autonomy.guard("block_dm_sender", {"list": True}, AGENT) is None
    finally:
        autonomy.REMOTE_CALL.reset(token)
    assert not autonomy.current().autonomous           # outside a remote call EMPORIA_UNTRACKED_TURNS=person applies again


def test_money_is_refused_for_a_remote_call_even_when_the_playbook_allows_spending(monkeypatch):
    monkeypatch.setenv("EMPORIA_PLAYBOOK_AUTONOMOUS_SPEND_CENTS_PER_DAY", "100000")
    token = autonomy.REMOTE_CALL.set("conn")
    try:
        assert autonomy.spend_allowed_cents() == 100000
        for tool, args in (("fund_session", {}), ("register_x402_wallet", {}), ("confirm_delivery", {}),
                           ("join_room", {"pay_with_x402": True}), ("challenge", {"prize_cents": 50}),
                           ("create_room", {"gate_type": "paid"})):
            out = autonomy.guard(tool, args, AGENT)
            assert out and out["refused_by"] == "emporia-autonomy" and "Money stays with the person" in out["error"], tool
        assert "never spent from a remote connector" in autonomy.gate_spend(10_000)
    finally:
        autonomy.REMOTE_CALL.reset(token)


async def test_the_outbound_secret_block_applies_to_remote_text(rig, monkeypatch):
    monkeypatch.setattr(identity_card, "operator_label", lambda: "op_supersecretlabel99")
    async with rig.running() as c:
        t = (await c.tokens())["access_token"]
        out = await c.tool(t, "post", text="my label is op_supersecretlabel99 ok")
        assert out["success"] is False and out["refused_by"] == "emporia-private"
        assert (await c.tool(t, "post", text="a plain post"))["success"] is True


async def test_reads_are_limited_per_grant_by_the_injected_clock(rig):
    async with rig.running() as c:
        t = (await c.tokens())["access_token"]
        for _ in range(60):
            assert "error" not in await c.tool(t, "whats_next")
        assert "read limit" in (await c.tool(t, "whats_next"))["error"]
        rig.clock.advance(61)
        assert "error" not in await c.tool(t, "whats_next")


def test_at_most_four_calls_at_once_and_a_burst_of_refusals_is_noticed():
    from remote_support import Clock
    clock = Clock()
    lim = surface.Limits(clock)
    assert [lim.start("g", False) for _ in range(4)] == [""] * 4
    assert "4 at most" in lim.start("g", False)
    lim.done("g")
    assert lim.start("g", False) == ""
    assert [lim.refused("g") for _ in range(10)] == [False] * 9 + [True]
    clock.advance(120)
    assert lim.refused("g") is False


async def test_a_burst_of_refused_calls_locks_the_grant_for_ten_minutes(rig):
    async with rig.running() as c:
        t = (await c.tokens(approve=["emporia:read"]))["access_token"]
        codes = [(await c.rpc(t, "tools/call", {"name": "post", "arguments": {"text": "x"}})).status_code
                 for _ in range(10)]
        assert codes == [403] * 10
        locked = await c.rpc(t, "tools/list")
        assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 500
        rig.clock.advance(R.LOCK_SECONDS + 1)
        assert (await c.rpc(t, "tools/list")).status_code == 200
    assert any(r["event"] == "grant_locked" for r in rig.audit.rows())


async def test_an_answer_is_cut_to_the_size_cap(rig):
    async with rig.running() as c:
        t = (await c.tokens())["access_token"]
        out = await c.tool(t, "list_rooms")
    assert out["truncated"] is True and len(json.dumps(out)) <= R.MAX_RESPONSE_BYTES + 400
    assert 0 < len(out["untrusted_content"]["rooms"]) < 300


def test_cap_cuts_the_longest_list_and_refuses_what_cannot_be_cut():
    small = {"success": True, "items": [1, 2, 3]}
    assert surface.cap(small) is small
    big = surface.cap({"success": True, "a": ["x" * 100] * 1000, "b": [1, 2]}, limit=5000)
    assert big["truncated"] and len(json.dumps(big)) <= 5000 + 300 and big["b"] == [1, 2]
    blob = surface.cap({"success": True, "text": "y" * 100_000}, limit=5000)
    assert blob["success"] is False and blob["refused_by"] == "emporia-remote"


def test_links_in_strangers_text_become_inert_words_that_name_the_domain():
    text = ("![logo](https://evil.example/p.png?x=SECRET) [click here](https://evil.example/a?b=1) "
            "bare https://evil.example/path?q=1 auto <https://evil.example/z> <img src=x onerror=1> end")
    out = surface.defang(text)
    assert "http" not in out and "![" not in out and "](" not in out and "<img" not in out and "SECRET" not in out
    assert out.count("evil.example") >= 4 and "click here" in out and out.endswith("end")
    assert surface.defang("no links at all: 3 < 5 and a(b)") == "no links at all: 3 < 5 and a(b)"


async def test_strangers_text_stays_wrapped_and_is_defanged_over_the_wire(rig):
    async with rig.running() as c:
        t = (await c.tokens())["access_token"]
        out = await c.tool(t, "read_feed")
    assert out["untrusted"] == mcp_surface.UNTRUSTED_NOTE and "untrusted_content" in out
    text = json.dumps(out)
    assert "https://" not in text and "![" not in text and "evil.example" in text


async def test_a_remote_connector_never_gets_private_message_text(rig):
    assert dm_access.mode() == "person"
    async with rig.running() as c:
        t = (await c.tokens())["access_token"]
        out = await c.tool(t, "get_inbox")
    assert "private words" not in json.dumps(out) and out["withheld_private_events"] == 1
    assert [e["event_type"] for e in out["untrusted_content"]["events"]] == ["challenge"]


# ── hosts and headers ─────────────────────────────────────────────────────────────────

async def test_the_wrong_host_is_421_and_the_right_one_is_served(rig):
    async with rig.running() as c:
        r = await c.http.get("/.well-known/oauth-authorization-server", headers={"host": "evil.example"})
        assert r.status_code == 421
        r = await c.http.post(R.MCP_PATH, json={}, headers={"host": "127.0.0.1:8789"})
        assert r.status_code == 421
        assert (await c.http.get("/.well-known/oauth-authorization-server")).status_code == 200
        assert (await c.http.get("/.well-known/oauth-authorization-server")).json()["issuer"] == URL, \
            "the issuer is the configured one, whatever Host said"


async def test_forwarded_proto_is_believed_only_from_a_loopback_peer(rig):
    bad = {"x-forwarded-proto": "http"}
    async with rig.running(peer="127.0.0.1", headers=bad) as c:
        assert (await c.http.get("/.well-known/oauth-authorization-server")).status_code == 400
    async with rig.running(peer="203.0.113.9", headers=bad) as c:
        assert (await c.http.get("/.well-known/oauth-authorization-server")).status_code == 200
    async with rig.running(peer="127.0.0.1", headers={"x-forwarded-proto": "https"}) as c:
        assert (await c.http.get("/.well-known/oauth-authorization-server")).status_code == 200


async def test_the_peer_in_the_log_is_the_proxys_report_only_from_loopback(rig):
    async with rig.running(peer="127.0.0.1", headers={"x-forwarded-for": "198.51.100.7, 10.0.0.1"}) as c:
        t = (await c.tokens())["access_token"]
        await c.tool(t, "whats_next")
    async with rig.running(peer="203.0.113.9", headers={"x-forwarded-for": "198.51.100.7"}) as c:
        await c.tool(t, "whats_next")
    peers = [r["peer"] for r in rig.audit.rows() if r["event"] == "call"]
    assert peers == ["198.51.100.7", "203.0.113.9"]


async def test_a_browser_origin_that_is_not_this_server_is_refused_at_mcp(rig):
    async with rig.running() as c:
        t = (await c.tokens())["access_token"]
        r = await c.rpc(t, "tools/list", Origin="https://evil.example")
        assert r.status_code == 403
        assert (await c.rpc(t, "tools/list", Origin=URL)).status_code == 200


async def test_a_body_over_1mb_is_refused_and_the_token_is_not_passed_inward(rig):
    async with rig.running() as c:
        t = (await c.tokens())["access_token"]
        r = await c.http.post(R.MCP_PATH, content=b"x" * (R.MAX_REQUEST_BYTES + 10),
                              headers={"Authorization": "Bearer " + t, "Content-Type": "application/json"})
        assert r.status_code == 413
    seen = {}

    async def inner(scope, receive, send):
        seen["headers"] = dict(scope["headers"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    rig.app.inner = inner
    import httpx
    transport = httpx.ASGITransport(app=rig.app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url=URL) as raw:
        await raw.post(R.MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                       headers={"Authorization": "Bearer " + t, "Cookie": "a=b"})
    assert b"authorization" not in seen["headers"] and b"cookie" not in seen["headers"]


# ── the audit log ─────────────────────────────────────────────────────────────────────

async def test_every_call_is_logged_the_chain_verifies_and_a_change_is_caught(rig):
    async with rig.running() as c:
        t = (await c.tokens(approve=["emporia:read"]))["access_token"]
        await c.tool(t, "whats_next")
        await c.tool(t, "fund_session")                               # refused
        await c.rpc(t, "tools/call", {"name": "post", "arguments": {"text": "x"}})   # 403 by scope
    calls = [r for r in rig.audit.rows() if r["event"] == "call"]
    assert [(r["tool"] if "tool" in r else "", r["outcome"]) for r in calls if "tool" in r] == [
        ("whats_next", "ok"), ("fund_session", "refused:remote")]
    assert calls[-1]["outcome"] == "refused:scope"
    events = [r["event"] for r in rig.audit.rows()]
    assert {"register", "authorize_pending", "approve", "token_issued"} <= set(events)
    ok, note = rig.audit.verify()
    assert ok, note
    path = rig.folder / "audit.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:2] + lines[3:]) + "\n")         # a line removed
    assert rig.audit.verify()[0] is False
    path.write_text("\n".join(lines) + "\n")
    assert rig.audit.verify()[0] is True
    path.write_text("\n".join([lines[0].replace("register", "regisTer")] + lines[1:]) + "\n")
    assert rig.audit.verify()[0] is False


async def test_the_log_never_holds_private_text_or_a_key_like_string(rig):
    secret_text = "a private thought the log must never keep"
    key = "0123456789abcdef0123456789abcdef0123456789abcdef"
    async with rig.running() as c:
        t = (await c.tokens())["access_token"]
        await c.tool(t, "post", text=secret_text, topic="ideas")
        await c.tool(t, "react", target=key, value="like")
        await c.tool(t, "follow", target="emp_at_" + "A" * 43)
        await c.tool(t, "get_inbox")
    raw = (rig.folder / "audit.jsonl").read_text()
    for forbidden in (secret_text, key, "emp_at_AAAA", "private words", t):
        assert forbidden not in raw
    assert '"topic":"ideas"' in raw and "<text 41 chars>" in raw or "<text" in raw


def test_the_argument_summary_keeps_ids_and_withholds_text_and_keys():
    out = summarize({"room_id": "room_12", "limit": 5, "text": "hello world", "to_agent_id": "bob",
                     "api_key": "short", "target": "0x" + "ab" * 20, "tags": [["t", "x"]], "flag": True,
                     "sentence": "this has spaces and is long enough to be words, not an id at all, really"})
    assert out["room_id"] == "room_12" and out["limit"] == 5 and out["to_agent_id"] == "bob" and out["flag"] is True
    assert out["text"] == "<text 11 chars>" and out["api_key"] == "<withheld>" and out["target"] == "<withheld>"
    assert out["sentence"].startswith("<text ")
    assert keylike("-----" + "BEGIN PRIVATE KEY-----") and keylike("a" * 64) and not keylike("hello world")


def test_the_chain_runs_across_a_rotated_file(tmp_path):
    from remote_support import Clock
    log = AuditLog(tmp_path, Clock())
    log.write("one")
    log.path.rename(tmp_path / "audit.1.jsonl")
    log.write("two")
    assert log.verify() == (True, "2 lines, chain intact")
    (tmp_path / "audit.1.jsonl").unlink()
    assert log.verify()[0] is False


def test_the_key_guard_treats_the_connector_state_like_keys():
    from importlib import util
    spec = util.spec_from_file_location("kg", "hermes_plugin/emporia/key_guard.py")
    kg = util.module_from_spec(spec)
    spec.loader.exec_module(kg)
    for text in ("cat ~/.hermes/keys/remote/connectors.db", "sqlite3 /x/agents/a/remote/connectors.db .dump",
                 "tail /x/agents/a/remote/audit.jsonl", "python -m emporia.remote.server",
                 "from emporia.remote import oauth", "emporia agent connector approve a ABCD-EFGH --yes",
                 "emporia agent serve-remote a --public-url https://x.example"):
        veto = kg.pre_tool_call(tool_name="terminal", args={"command": text})
        assert veto and veto["action"] == "block", text
