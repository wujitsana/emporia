"""A public paid room is announced in a signed pointer post naming it, so agents (here and on other relays) can
find it and decide to pay and join; an open or private room is not, and announce=False skips it."""
import httpx

from emporia import mcp_server as m


def _run(monkeypatch, **kw):
    posts = []
    monkeypatch.setattr(m, "_signed_room_fields", lambda creator, body: {})
    monkeypatch.setattr(m, "_bearer_request", lambda *a, **k: httpx.Response(200, json={"room_id": "room_abc"}))
    monkeypatch.setattr(m, "post", lambda text, **k: posts.append((text, k)) or {"success": True, "post_id": "pst_1"})
    out = m.create_room(name="Tasting", creator_id="alpha", description="game openings", **kw)
    return out, posts


def test_a_public_paid_room_is_announced_with_a_pointer(monkeypatch):
    out, posts = _run(monkeypatch, room_type="public", gate_type="paid", entry_fee_cents=5)
    assert out["success"] and out["announced"] == "pst_1"
    text, k = posts[0]
    assert k["kind"] == "pointer" and k["tags"] == [["ref", "room", "room_abc"]] and k["agent_id"] == "alpha"
    assert "Tasting" in text and "0.05" in text and "game openings" in text


def test_open_private_or_unannounced_rooms_post_nothing(monkeypatch):
    for kw in ({"room_type": "public", "gate_type": "open"},
               {"room_type": "private", "gate_type": "paid", "entry_fee_cents": 5},
               {"room_type": "public", "gate_type": "paid", "entry_fee_cents": 5, "announce": False}):
        out, posts = _run(monkeypatch, **kw)
        assert out["success"] and posts == [] and "announced" not in out, kw
