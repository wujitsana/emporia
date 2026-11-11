"""The console never forwards the caller's Accept-Encoding to the relay: behind a CDN that answers in br, a client
without a br decoder passed compressed bytes to the tab, which read them as broken JSON."""
from __future__ import annotations

from emporia.console import relay as console_relay


def test_accept_encoding_is_not_forwarded():
    assert "accept-encoding" in console_relay._DROP
    sent = {k: v for k, v in {"Accept-Encoding": "gzip, deflate, br", "Accept": "application/json"}.items()
            if k.lower() not in console_relay._DROP}
    assert sent == {"Accept": "application/json"}


def test_a_403_is_an_answer_not_a_reason_to_sign_in_again():
    import asyncio

    import httpx
    signed = []
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(403, json={"detail": "not yours to see"})
    client = console_relay.RelayClient("https://relay.example", transport=httpx.MockTransport(handler))

    async def token(agent, *, fresh=False):
        signed.append(fresh)
        return "tok"
    client.token = token
    r = asyncio.run(client.forward(object(), "GET", "/operator/status", "", {}, b""))
    assert r.status_code == 403 and signed == [False] and calls == ["/operator/status"]


def test_session_refused_tells_an_expired_session_from_a_refusal():
    import httpx
    from emporia.console.relay import session_refused
    assert session_refused(httpx.Response(401, json={"detail": "Authentication required"}))
    assert session_refused(httpx.Response(403, json={"detail": "Not authorized to act as 'alpha'"}))
    assert not session_refused(httpx.Response(403, json={"detail": "topics need level 2"}))
    assert not session_refused(httpx.Response(403, text="<html>blocked</html>"))
    assert not session_refused(httpx.Response(200, json={}))


def test_a_403_whose_body_is_not_an_object_is_an_answer():
    import httpx
    from emporia.console.relay import session_refused
    assert session_refused(httpx.Response(403, json=["Not authorized to act as x"])) is False
    assert session_refused(httpx.Response(403, json="Not authorized to act as x")) is False
    assert session_refused(httpx.Response(403, json={"detail": "Not authorized to act as x"})) is True
