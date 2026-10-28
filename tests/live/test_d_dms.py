"""P248 (d): DMs between agents on two relays - sealed, signed, and a swapped key is never believed."""
import json
import sqlite3

import pytest

from emporia.federation_dm import thread_id_for


def _read(agent, other: str) -> dict:
    """`agent`'s side of its thread with `other` (an address), as its own client opens and checks it."""
    thread = thread_id_for(agent.address, other)
    out = agent.tool("get_dm_messages", thread_id=thread)
    assert out["success"], out
    return out


def _seen(out: dict) -> list[tuple]:
    return [(m["sender_id"], m["content"], m["verified"], m["decrypted"]) for m in out["messages"]]


def test_dms_cross_relays_sealed_and_signed_and_a_swapped_key_is_never_believed(fleet):
    a, b, rogue = fleet.a, fleet.b, fleet.rogue
    alpha = fleet.registered("alpha", a)
    bob = fleet.registered("bob", b)
    for agent in (alpha, bob):          # each takes DMs from other relays: the recipient's own choice
        assert agent.tool("set_dm_policy", other_relays=True)["success"]

    # both ways: each end opens what was sealed to it and checks the other's signature
    assert alpha.tool("send_dm", to_agent_id=bob.address, content="hello bob, it is alpha")["success"]
    got = _read(bob, alpha.address)
    assert _seen(got) == [(alpha.address, "hello bob, it is alpha", True, True)]
    assert bob.tool("send_dm", to_agent_id=alpha.address, content="hello alpha, bob here")["success"]
    back = _read(alpha, bob.address)
    assert _seen(back) == [(alpha.address, "hello bob, it is alpha", True, True),
                           (bob.address, "hello alpha, bob here", True, True)]
    assert "signing_key_changed" not in back and "signing_key_changed" not in _read(bob, alpha.address)

    # neither operator can read it: each relay's database holds sealed envelopes only
    for relay in (a, b):
        db = sqlite3.connect(f"file:{relay.root / 'relay.sqlite3'}?mode=ro", uri=True)
        rows = [c for (c,) in db.execute("SELECT content FROM dm_messages")]
        assert len(rows) == 2 and all(json.loads(c)["alg"].startswith("x25519") for c in rows), rows
        assert not any("hello" in c for c in rows)

    # dana and fay live on B too, but their clients reach B through a hostile operator's hop (they registered where
    # they live, so their address is B's). Honest at first: dana reads alpha's message, fay writes to her, so each
    # pins alpha's keys on first sight.
    dana, fay = fleet.registered("dana", b), fleet.registered("fay", b)
    for agent in (dana, fay):
        agent.restart(home=rogue.url)
        assert agent.tool("set_dm_policy", other_relays=True)["success"]
    assert alpha.tool("send_dm", to_agent_id=dana.address, content="hello dana")["success"]
    first = _read(dana, alpha.address)
    assert _seen(first) == [(alpha.address, "hello dana", True, True)] and "signing_key_changed" not in first
    assert fay.tool("send_dm", to_agent_id=alpha.address, content="hi alpha, fay here")["success"]

    # the hop starts serving a signing key of its own for alpha: dana is told. What she holds is checked
    # against the key she pinned, never the one served now.
    rogue.swap_signing_keys = True
    swapped = _read(dana, alpha.address)
    assert swapped["signing_key_changed"] == [alpha.address], swapped
    assert _seen(swapped) == [(alpha.address, "hello dana", True, True)]

    # a full impersonation (its own encryption key, bound to its own signing key) cannot get fay to seal to it
    rogue.swap_encryption_keys = True
    sent = fay.tool("send_dm", to_agent_id=alpha.address, content="is that you, alpha?")
    assert not sent["success"] and "does not match the one pinned" in sent["error"], sent
    assert [m["content"] for m in _read(alpha, fay.address)["messages"]] == ["hi alpha, fay here"]

    # eve first meets alpha through the same hop with the swap already on: she pins the rogue's key, so alpha's
    # genuine message is not verified for her. It is never reported as good.
    eve = fleet.registered("eve", b)
    eve.restart(home=rogue.url)
    assert eve.tool("set_dm_policy", other_relays=True)["success"]
    assert alpha.tool("send_dm", to_agent_id=eve.address, content="hello eve")["success"]
    unverified = _read(eve, alpha.address)
    assert [m["verified"] for m in unverified["messages"]] == [False], unverified


@pytest.mark.xfail(strict=True, reason=(
    "found by P248: a client that pinned alpha's signing key but has not sent her a DM yet pins no encryption key, "
    "and the binding of a served encryption key is checked against the signing key served now, not the pinned one - "
    "so a hop that swaps both gets the first message sealed to its own key. Rooms and private sessions take their "
    "keys from a bundle that carries no key log; one fix for every site is a design change, not a one-line one."))
def test_a_swapped_signing_key_is_refused_even_before_the_first_dm_is_sent(fleet):
    a, b, rogue = fleet.a, fleet.b, fleet.rogue
    alpha = fleet.registered("alpha", a)
    dana = fleet.registered("dana", b)
    dana.restart(home=rogue.url)
    assert dana.tool("set_dm_policy", other_relays=True)["success"]
    assert alpha.tool("send_dm", to_agent_id=dana.address, content="hello dana")["success"]
    assert _seen(_read(dana, alpha.address)) == [(alpha.address, "hello dana", True, True)]   # alpha's signing key pinned
    rogue.swap_signing_keys = rogue.swap_encryption_keys = True
    sent = dana.tool("send_dm", to_agent_id=alpha.address, content="is that you, alpha?")
    assert not sent["success"], sent
