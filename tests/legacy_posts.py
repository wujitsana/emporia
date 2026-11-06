"""Tests written before rooms v2 send a room message or a DM as plain text with msg_type `chat` and sign
that. A message is a post body now, so this wraps such a request on its way to the relay: the plain text
becomes a note's body, and the message is signed again for the new content when its first signature was
good (a request signed wrongly stays signed wrongly, so the tests of a refusal still see one). What those
tests are about (signatures, nonces, chains, encryption, scans) is still exercised against the real routes.
A test of the new shape sends the new shape and is not touched. Port a test to bodies and it needs no help;
this file goes when the last one is."""
from __future__ import annotations

import re

import pytest

_ROUTE = re.compile(r"^/(?:rooms/(?P<room>[^/?]+)/message|dm/(?P<dm>[^/?]+)/send)$")


def _rewrite(url: str, body: dict, headers: dict) -> dict:
    from emporia import crypto_box, identity, room_post, wire
    from emporia import relay_server as rs
    m = _ROUTE.match(url.split("?")[0])
    content = body.get("content")
    if not m or not isinstance(content, str) or not content or "signature" not in body:
        return body
    msg_type = body.get("msg_type") or ""
    if (room_post.parse(content) is not None or crypto_box.looks_like_any_envelope(content)
            or msg_type not in ("", "chat", "collab", "note")):
        return body
    agent = next((v for k, v in headers.items() if k.lower() == "x-emporia-agent-id"), "")
    if not agent:
        auth = next((v for k, v in headers.items() if k.lower() in ("authorization", "x-emporia-authorization")), "")
        payload = rs._jwt_decode(auth[7:]) if auth.startswith("Bearer ") else None
        agent = (payload or {}).get("agent_id") or ""
    if not agent:
        return body
    scope, build = (("room_id", wire.signed_room_message_payload), m["room"]) if m["room"] else (
        ("thread_id", wire.signed_dm_thread_payload), m["dm"])
    nonce = body.get("nonce") or ""
    extra = (rs.AGENT_REGISTRY.get_stable_id(agent) or "",) if m["room"] else ()
    good = any(rs.AGENT_REGISTRY.verify_signature(agent, scope[1](build, content, t, nonce, *extra), body["signature"])
               for t in {msg_type, "chat", "note"} if t)
    new = room_post.canonical(room_post.body("note", content))
    out = {**body, "content": new, "msg_type": "note"}
    if good:
        out["signature"] = identity.sign(scope[1](build, new, "note", nonce, *extra), agent)
    return out


@pytest.fixture(autouse=True)
def _legacy_plain_posts(monkeypatch, request):
    if request.module.__name__.endswith("test_rooms_v2"):     # the new shape's own tests send what they mean
        yield
        return
    from starlette.testclient import TestClient
    real = TestClient.request

    def request(self, method, url, *args, **kwargs):
        if method == "POST" and isinstance(kwargs.get("json"), dict):
            kwargs["json"] = _rewrite(str(url), kwargs["json"], kwargs.get("headers") or {})
        return real(self, method, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "request", request)

    yield
