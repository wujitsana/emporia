"""A client may reach its relay by another name than the relay's own (a proxy, a container service name). The
relay labels the agent in a cross-relay thread by its own host, so the label is accepted when it carries the host
the agent's own home relay names itself by in /health. Everything else stays refused: a label naming someone
else, a lookup that fails, a relay URL that is not the agent's home, and a URL that came from anywhere else."""
import httpx
import pytest

from emporia import identity

CLIENT_URL = "http://relay-internal:8088"
RELAY_OWN = "http://relay.example:8088"
LABEL = "fay@relay.example:8088"
PEER = "alpha@other.example:8088"


def _thread(me: str = LABEL, peer: str = PEER) -> dict:
    return {"thread_id": "dmx_1", "my_label": me, "agent_a": min(me, peer), "agent_b": max(me, peer)}


class _Health:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(identity, "_ADVERTISED_URLS", {})
    calls: list[str] = []

    def get(url, **kw):
        calls.append(url)
        assert kw["timeout"] <= 3 and kw["follow_redirects"] is False
        return _Health({"status": "ok", "relay_url": RELAY_OWN})

    monkeypatch.setattr(httpx, "get", get)
    return calls


def _peer(thread=None, relay=CLIENT_URL, home=CLIENT_URL):
    return identity.dm_thread_peer(relay, "fay", thread or _thread(), home_url=home)


def test_the_label_with_the_relays_own_host_is_the_agent(_fresh):
    assert _peer() == PEER
    assert _fresh == [f"{CLIENT_URL}/health"]          # only the agent's own configured relay is asked


def test_the_answer_is_kept_for_the_process(_fresh):
    _peer()
    _peer()
    assert len(_fresh) == 1


def test_a_label_matching_the_clients_own_url_asks_nothing(_fresh):
    assert identity.dm_thread_peer(RELAY_OWN, "fay", _thread(), home_url=RELAY_OWN) == PEER
    assert _fresh == []


def test_a_label_naming_another_agent_is_refused_even_with_the_relays_host():
    with pytest.raises(identity.ThreadPeerChanged):
        _peer(_thread("alpha@relay.example:8088", PEER))


def test_a_label_on_the_peers_host_is_refused():
    with pytest.raises(identity.ThreadPeerChanged):
        _peer(_thread("fay@other.example:8088", "fay@third.example:8088"))


def test_an_unreachable_relay_leaves_the_label_refused(monkeypatch):
    def down(url, **kw):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "get", down)
    with pytest.raises(identity.ThreadPeerChanged):
        _peer()


def test_a_relay_that_says_something_else_or_nothing_leaves_it_refused(monkeypatch):
    for body in ({"status": "ok", "relay_url": "http://elsewhere.example"}, {"status": "ok"}, ["not", "a", "dict"]):
        monkeypatch.setattr(identity, "_ADVERTISED_URLS", {})
        monkeypatch.setattr(httpx, "get", lambda url, _b=body, **kw: _Health(_b))
        with pytest.raises(identity.ThreadPeerChanged):
            _peer()


def test_a_bad_status_leaves_it_refused(monkeypatch):
    class Bad(_Health):
        status_code = 502

    monkeypatch.setattr(httpx, "get", lambda url, **kw: Bad({"relay_url": RELAY_OWN}))
    with pytest.raises(identity.ThreadPeerChanged):
        _peer()


def test_a_failed_lookup_is_tried_again_later_but_not_at_once(monkeypatch):
    seen: list[str] = []

    def down(url, **kw):
        seen.append(url)
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "get", down)
    for _ in range(2):
        with pytest.raises(identity.ThreadPeerChanged):
            _peer()
    assert len(seen) == 1
    monkeypatch.setattr(identity, "_ADVERTISED_RETRY_SECONDS", 0)
    with pytest.raises(identity.ThreadPeerChanged):
        _peer()
    assert len(seen) == 2


def test_only_the_agents_own_home_is_ever_asked(_fresh):
    # talking to some other relay (a visited one), or no home given: no lookup, the label is refused
    with pytest.raises(identity.ThreadPeerChanged):
        _peer(relay="http://visited.example", home=CLIENT_URL)
    with pytest.raises(identity.ThreadPeerChanged):
        _peer(home="")
    assert _fresh == []


def test_a_url_that_is_not_http_is_never_fetched(_fresh):
    with pytest.raises(identity.ThreadPeerChanged):
        _peer(relay="file:///etc/passwd", home="file:///etc/passwd")
    assert _fresh == []
