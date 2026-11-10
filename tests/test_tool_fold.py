"""The tool fold (11-10): tools that were two became one. Each folded call goes to the same relay endpoint the
two tools went to; what changes is which one a model picks."""
from __future__ import annotations

from emporia import mcp_server as m


class R:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.content = status, body, b"{}"
        self.text = str(body)

    def json(self):
        return self._body


def _record(monkeypatch, answers):
    """Stand in for the relay: `answers` maps (method, path suffix) to a body; every call is kept."""
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url, kw))
        for (meth, suffix), body in answers.items():
            if meth == method and url.endswith(suffix):
                return R(body)
        return R({}, 404)
    monkeypatch.setattr(m, "_bearer_request", fake)
    monkeypatch.setattr(m, "AGENT_ID", "me")
    return calls


# ── leave_session: waiting leaves freely, started walks away ─────────────────────────────

def test_leaving_a_waiting_game_you_joined_is_the_signed_leave(monkeypatch):
    calls = _record(monkeypatch, {("get", "/sessions/s1"): {"status": "waiting", "participants": ["bob", "me"]},
                                  ("post", "/sessions/s1/leave"): {"status": "left"}})
    monkeypatch.setattr(m.signing, "sign", lambda *a, **k: "sig")
    out = m.leave_session("s1")
    assert out["success"] and out["status"] == "left"
    assert [c[0:2] for c in calls][-1][1].endswith("/leave") and "signature" in calls[-1][2]["json"]


def test_leaving_a_game_you_created_or_one_under_way_is_the_walk_away(monkeypatch):
    for body in ({"status": "waiting", "participants": ["me"]}, {"status": "active", "participants": ["bob", "me"]},
                 {}):
        calls = _record(monkeypatch, {("get", "/sessions/s2"): body,
                                      ("post", "/sessions/s2/abandon"): {"status": "abandoned"}})
        out = m.leave_session("s2")
        assert out["success"] and out["status"] == "abandoned", body
        assert calls[-1][1].endswith("/abandon") and calls[-1][2]["json"] == {"agent_id": "me"}


def test_the_one_leave_tool_keeps_both_old_tools_places_in_the_autonomy_sets():
    from emporia import autonomy
    assert "leave_session" in autonomy.WRITE_TOOLS and "leave_session" in autonomy.LOGGED_TOOLS
    assert "abandon_session" not in autonomy.WRITE_TOOLS


# ── whats_next carries the happening part ──────────────────────────────────────────────

def test_whats_next_carries_what_is_new_in_the_public_places(monkeypatch):
    calls = _record(monkeypatch, {
        ("get", "/agents/me/next"): {"as_of": "t", "inbox": {"unread_total": 0}},
        ("get", "/happening"): {"since": "a", "next_since": "b", "topics": [{"name": "club"}], "rooms": [],
                                "top_posts": [{"text": "long"}], "labs": [], "open_challenges": []}})
    out = m.whats_next(since="2026-01-01T00:00:00+00:00")
    got = out["happening"]
    assert got["next_since"] == "b" and got["topics"] == [{"name": "club"}]
    assert set(got) == {"since", "next_since", "topics", "rooms"}, "compact: hot topics and rooms only"
    happening = [c for c in calls if c[1].endswith("/happening")][0]
    assert happening[2]["params"] == {"since": "2026-01-01T00:00:00+00:00", "limit": 3}


def test_whats_next_stands_without_the_news(monkeypatch):
    _record(monkeypatch, {("get", "/agents/me/next"): {"as_of": "t", "inbox": {"unread_total": 2}}})
    out = m.whats_next()
    assert out["success"] and out["unread_total"] == 2 and "happening" not in out


# ── block_dm_sender lists with list=true ─────────────────────────────────────────────

def test_block_dm_sender_lists_and_blocks(monkeypatch):
    calls = _record(monkeypatch, {("get", "/agents/me/dm-blocks"): {"blocked": ["eve"]},
                                  ("post", "/agents/me/dm-blocks"): {"blocked": ["eve", "mal"]}})
    assert m.block_dm_sender(list=True)["blocked"] == ["eve"] and calls[-1][0] == "get"
    assert m.block_dm_sender("mal")["blocked"] == ["eve", "mal"] and calls[-1][2]["json"] == {"agent": "mal"}
    assert not m.block_dm_sender()["success"], "neither a sender nor list=true"


# ── agent_card brings the profile for a home agent ──────────────────────────────────────

def test_agent_card_adds_the_profile_of_an_agent_on_the_home_relay(monkeypatch):
    class Cards:
        def get(self, url, timeout=10):
            return R({"card": {"public_key_hex": "ab" * 32, "fingerprint": "x", "relay": "r"},
                      "signature": "s", "relay_public_key": "k"})
    monkeypatch.setattr(m, "fetch", Cards())
    monkeypatch.setattr(m, "_pin_relay_key_for", lambda relay, key: (key, False))
    monkeypatch.setattr(m, "_relay_list_ok", lambda card: True)
    _record(monkeypatch, {("get", "/agents/bob"): {"agent_id": "bob", "display_name": "Bob", "about": "hi"}})
    out = m.agent_card("bob")
    assert out["success"] and out["profile"]["agent_id"] == "bob" and "card" in out and "checks" in out
    assert m._agent_profile("nobody") == {}, "a missing profile adds nothing"


# ── money_info: the relay's rails and your wallet in one ───────────────────────────────────

def test_money_info_has_the_relay_part_and_the_wallet_part(monkeypatch):
    class Health:
        status_code = 200

        def json(self):
            return {"payment_rails": ["free", "x402"], "fee_bps": 100, "x402_networks": ["eip155:84532"]}
    monkeypatch.setattr(m.httpx, "get", lambda url, timeout=10: Health())
    relay = m._relay_payment_info()
    assert relay["payment_rails"] == ["free", "x402"] and relay["fee_bps"] == 100
    assert relay["x402_networks"] == ["eip155:84532"]
