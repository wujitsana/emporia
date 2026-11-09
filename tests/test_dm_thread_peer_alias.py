"""A client may reach its relay by another name than the relay's own (a proxy, a container service name). The
relay labels the agent in a cross-relay thread by its own host, so the label must be accepted when it carries
the host the relay names itself by; a label naming someone else is refused whatever host it carries."""
import pytest

from emporia import identity

CLIENT_URL = "http://relay-internal:8088"
RELAY_OWN = "http://relay.example:8088"


def _thread(me: str, peer: str) -> dict:
    return {"thread_id": "dmx_1", "my_label": me, "agent_a": min(me, peer), "agent_b": max(me, peer)}


@pytest.fixture(autouse=True)
def _advertised(monkeypatch):
    monkeypatch.setattr(identity, "_ADVERTISED_URLS", {CLIENT_URL: RELAY_OWN})


def test_the_label_with_the_relays_own_host_is_the_agent():
    thread = _thread("fay@relay.example:8088", "alpha@other.example:8088")
    assert identity.dm_thread_peer(CLIENT_URL, "fay", thread) == "alpha@other.example:8088"


def test_a_label_naming_another_agent_is_refused_even_with_the_relays_host():
    thread = _thread("alpha@relay.example:8088", "alpha@other.example:8088")
    with pytest.raises(identity.ThreadPeerChanged):
        identity.dm_thread_peer(CLIENT_URL, "fay", thread)


def test_a_label_on_the_peers_host_is_refused():
    thread = _thread("fay@other.example:8088", "fay@third.example:8088")
    with pytest.raises(identity.ThreadPeerChanged):
        identity.dm_thread_peer(CLIENT_URL, "fay", thread)


def test_without_an_advertised_url_an_alias_label_is_refused(monkeypatch):
    monkeypatch.setattr(identity, "_ADVERTISED_URLS", {CLIENT_URL: ""})
    with pytest.raises(identity.ThreadPeerChanged):
        identity.dm_thread_peer(CLIENT_URL, "fay", _thread("fay@relay.example:8088", "alpha@other.example:8088"))
