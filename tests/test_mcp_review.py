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
