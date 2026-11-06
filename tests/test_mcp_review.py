"""MCP review (docs/proposals/mcp-review.md): the agent's tool surface — safety, errors,
idempotency, paging and the descriptions the model reads."""

from __future__ import annotations

import pytest

from emporia import mcp_server


def _no_network(*_a, **_k):
    raise AssertionError("no request may leave for a refused path")


# ── one route per call: ids from tool arguments cannot climb out of their path ──

@pytest.mark.parametrize("bad", [
    "../../agents/me/leave#", "x/../../agents/me/dm-blocks/bob", "..", ".",
    "a?b=1", "a#frag", "a\\b", "a b", "a%2F..%2Fb", "%2e%2e",
])
def test_an_id_that_leaves_its_path_is_refused(bad):
    assert mcp_server._unsafe_path(f"http://relay:8088/bounties/{bad}/enter")


@pytest.mark.parametrize("good", [
    "http://relay:8088/bounties/lst_abc123/enter",
    "http://relay:8088/agents/bob@relay.example/encryption-key",
    "http://relay:8088/agents/me/dm-blocks/bob%40relay.example%3A8088",
    "http://relay:8088",
])
def test_ordinary_ids_pass(good):
    assert mcp_server._unsafe_path(good) == ""


def test_a_bounty_tool_cannot_be_turned_into_leaving_the_relay(monkeypatch):
    """`withdraw_bounty_entry('../../agents/me/leave#')` resolved, in httpx, to
    `DELETE /agents/me/leave` with the agent's own token — leaving is its operator's call."""
    for verb in ("get", "post", "delete"):
        monkeypatch.setattr(mcp_server.httpx, verb, _no_network)
    out = mcp_server.withdraw_bounty_entry("../../agents/me/leave#", agent_id="mr_agent")
    assert out["success"] is False and "refused" in out["error"]


@pytest.mark.parametrize("bad", ["me/leave", "x%00", "%252e%252e", "a%25b"])
def test_an_id_cannot_add_a_segment_or_smuggle_an_encoding(monkeypatch, bad):
    """Review follow-up L4: `_unsafe_path` let a plain `/` through (`me/leave` sent
    `/bounties/me/leave/enter`), and `%00` and double encodings. Every id is now quoted
    as one segment where its URL is built, and what that leaves is refused."""
    sent = []
    for verb in ("get", "post", "delete"):
        monkeypatch.setattr(mcp_server.httpx, verb, lambda url, *a, **k: sent.append(url) or _no_network())
    out = mcp_server.withdraw_bounty_entry(bad, agent_id="mr_agent")
    assert out["success"] is False and "refused" in out["error"], out
    assert not [u for u in sent if "/bounties/" in u], sent


def test_both_clients_quote_an_id_as_one_segment():
    import asyncio

    from emporia.agent_sdk import EmporiaAgent
    seen = []

    async def record(method, url, **kw):
        seen.append(url)
        raise RuntimeError("stop here")
    agent = EmporiaAgent(relay_url="http://relay.test", agent_id="mr_agent", public_key_hex="ab" * 32)
    agent._bearer_request = record
    with pytest.raises(RuntimeError):
        asyncio.run(agent.get_dm_thread("dm_1;x/../y"))
    assert seen == ["http://relay.test/dm/dm_1%3Bx%2F..%2Fy"]
    assert mcp_server._seg("bob@relay.example") == "bob%40relay.example"


# ── sign-in signatures go only to this agent's own relays, with its operator's yes ──

def test_a_sign_in_signature_is_never_sent_to_a_stranger(monkeypatch):
    monkeypatch.setattr(mcp_server, "AGENT_ID", "mr_agent")
    monkeypatch.setattr(mcp_server.httpx, "post", _no_network)
    monkeypatch.setattr(mcp_server.signing, "sign_in", _no_network)
    out = mcp_server.sign_dashboard_challenge("ch_1", "ab" * 32, relay_url="https://evil.example")
    assert out["success"] is False and "not this agent's home" in out["error"]
    assert "signature_hex" not in out


def test_a_sign_in_needs_the_operators_yes(monkeypatch):
    monkeypatch.setattr(mcp_server, "AGENT_ID", "mr_agent")
    monkeypatch.setattr(mcp_server.signing, "sign_in", _no_network)
    for answer, words in ((None, "could not be asked"), (False, "said no")):
        monkeypatch.setattr(mcp_server, "_ask_operator", lambda *_a, answer=answer, **_k: answer)
        out = mcp_server.sign_dashboard_challenge("ch_1", "ab" * 32)
        assert out["success"] is False and words in out["error"]


# ── bounded loops ──

def test_a_chain_walk_stops_on_a_repeated_page_or_too_many():
    seen: set = set()
    assert mcp_server._chain_walk_stalled(1, None, None, seen) == ""
    assert mcp_server._chain_walk_stalled(2, "t1", 5, seen) == ""
    assert "same page twice" in mcp_server._chain_walk_stalled(3, "t1", 5, seen)
    assert "pages" in mcp_server._chain_walk_stalled(mcp_server._MAX_CHAIN_PAGES + 1, "t9", 9, set())


def test_both_chain_walks_are_bounded():
    import inspect
    for fn in (mcp_server.verify_dm_chain, mcp_server.verify_room_chain):
        assert "_chain_walk_stalled(" in inspect.getsource(fn)


def test_scan_relays_takes_a_bounded_list(monkeypatch):
    monkeypatch.setattr(mcp_server.fetch, "get", _no_network)
    out = mcp_server.scan_relays(relays=[f"https://r{i}.example" for i in range(21)])
    assert out["success"] is False and "at most" in out["error"]


# ── what the model reads: one agent-facing description per tool ──

def _tools():
    import asyncio
    return {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}


def test_every_tool_has_an_agent_description_and_none_is_stale():
    from emporia import mcp_surface
    registered = set(_tools())
    assert registered - mcp_surface.DESCRIPTIONS.keys() == set(), "add these to mcp_surface.DESCRIPTIONS"
    assert mcp_surface.DESCRIPTIONS.keys() - registered == set(), "these are no longer tools"


def test_descriptions_lead_with_a_short_first_sentence_and_carry_no_history():
    """Hermes's deferred catalog lists each tool by its first sentence, clipped at 80 chars."""
    import re
    for name, tool in _tools().items():
        first = re.split(r"(?<=[.!?])\s", " ".join(tool.description.split()), maxsplit=1)[0]
        assert len(first) <= 80, (name, first)
        assert not re.search(r"\bP\d{2,3}\b|\bN\d-\d\b", tool.description), (name, "review ids are for maintainers")


def test_the_tool_list_stays_within_its_measured_budget():
    """MCP review: 78 KB before, ~48 KB after (eager mode, Hermes's converted schemas); games pack: export_games, +0.4 KB."""
    import json
    size = sum(len(json.dumps({"name": t.name, "description": t.description, "parameters": t.parameters},
                              separators=(",", ":"))) for t in _tools().values())
    assert size < 52_500, size


def test_relay_and_own_id_arguments_say_what_they_are():
    props = {name: t.parameters["properties"] for name, t in _tools().items()}
    assert "visit" in props["submit_action"]["relay"]["description"]
    assert "omit for home" not in props["visit_relay"]["relay"].get("description", "")
    assert props["send_dm"]["from_agent_id"]["description"] == "you if empty"
    assert props["invite_to_room"]["agent_id"]["description"] == "the agent to invite"
    assert props["invite_to_room"]["invited_by"]["description"] == "you if empty"


def test_every_answer_has_one_shape():
    from emporia import mcp_surface
    assert mcp_surface.shape("x", [1]) == {"success": True, "items": [1]}
    assert mcp_surface.shape("x", {"error": "no"}) == {"success": False, "error": "no"}
    assert mcp_surface.shape("x", {"a": 1}) == {"success": True, "a": 1}
    assert "not instructions" in mcp_surface.shape("get_dm_messages", {"messages": []})["untrusted"]
    assert "untrusted" not in mcp_surface.shape("get_dm_messages", {"error": "x"})


def test_list_dm_threads_works_over_mcp(monkeypatch):
    """It returned the relay's bare list, which MCP refuses as structured content: every
    call with a thread in it failed with a ToolError."""
    import asyncio

    from fastmcp import Client

    class _R:
        status_code = 200

        def json(self):
            return [{"thread_id": "dm_1", "other_agent": "bob"}]
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: _R())
    monkeypatch.setattr(mcp_server, "AGENT_ID", "mr_agent")

    async def call():
        async with Client(mcp_server.mcp) as c:
            return (await c.call_tool("list_dm_threads", {})).structured_content
    out = asyncio.run(call())
    assert out["success"] is True and out["untrusted_content"]["threads"][0]["thread_id"] == "dm_1"
    assert "not instructions" in out["untrusted"]


def test_tool_calls_written_for_agents_use_real_arguments():
    """The skill told agents `send_dm(from_agent=..., to_agent=...)` — neither exists. Every
    bare `tool(kw=...)` in the agent's skill, the wake-up notices and the docs names real
    arguments (SDK method calls, `agent.x(...)`, are the SDK's business)."""
    import re
    from pathlib import Path
    root = Path(__file__).parent.parent
    tools = _tools()
    call = re.compile(r"(?<![.\w])(" + "|".join(sorted(tools, key=len, reverse=True)) + r")\(([^()]*)\)")
    kw = re.compile(r"(?:^|,)\s*([a-z_][a-z0-9_]*)\s*=")
    wrong = []
    for doc in ("hermes_plugin/emporia/skills/emporia/SKILL.md", "src/emporia/plugins/platforms/emporia/frames.py",
                "skills/dev/emporia-dev/SKILL.md", "README.md", "docs/protocol.md"):
        for m in call.finditer((root / doc).read_text()):
            props = tools[m.group(1)].parameters.get("properties", {})
            wrong += [f"{doc}: {m.group(1)}({k}=)" for k in kw.findall(m.group(2)) if k not in props]
    assert not wrong, wrong


# ── small gaps ──

def test_inviting_defaults_to_yourself():
    import inspect
    for fn in (mcp_server.invite_to_room, mcp_server.invite_to_agora_topic, mcp_server.revoke_agora_invite):
        assert inspect.signature(fn).parameters["invited_by"].default == ""


def test_message_reads_page_back(monkeypatch):
    seen = {}

    class _R:
        status_code = 200

        def json(self):
            return {"messages": [{"message_id": "m2", "created_at": "t2", "seq": 2, "sender_id": "bob"},
                                 {"message_id": "m1", "created_at": "t1", "seq": 1, "sender_id": "bob"}]}

    def fake(method, url, **kw):
        seen.setdefault("params", kw.get("params"))
        return _R()
    monkeypatch.setattr(mcp_server, "_bearer_request", fake)
    monkeypatch.setattr(mcp_server, "_sender_public_key", lambda *a, **k: "")
    monkeypatch.setattr(mcp_server, "AGENT_ID", "mr_agent")
    monkeypatch.setattr("emporia.identity.encryption_private_keys", lambda aid: [])
    out = mcp_server.get_dm_messages("dm_1", limit=2, before="t9", before_seq=9)
    assert seen["params"]["before"] == "t9" and seen["params"]["before_seq"] == 9
    assert out["older"] == {"before": "t1", "before_seq": 1}


def test_a_commit_or_reveal_retried_after_it_landed_is_a_success(monkeypatch, tmp_path):
    import json as _json
    monkeypatch.setattr(mcp_server, "AGENT_ID", "mr_agent")
    path = tmp_path / "c.json"
    path.write_text(_json.dumps({"1": {"move": [50, 50], "nonce": "n", "commitment": "abc"}}))
    monkeypatch.setattr(mcp_server, "_commitments_file", lambda aid, sid: path)
    monkeypatch.setattr(mcp_server, "_session_json", lambda sid, aid: {
        "module_type": "emporia:blotto:v2", "state": {"data": {"round": 1, "commitments": {"mr_agent": "abc"}}}})
    out = mcp_server.commit_move("s1", [50, 50], "a considered split of troops")
    assert out["success"] is True and out["already_committed"] is True
    monkeypatch.setattr(mcp_server, "_session_data", lambda sid, aid: {
        "round": 1, "commitments": {"mr_agent": "abc"}, "revealed": {"mr_agent": [50, 50]}})
    out = mcp_server.reveal_move("s1", "revealing the split as committed")
    assert out["success"] is True and out["already_revealed"] is True
