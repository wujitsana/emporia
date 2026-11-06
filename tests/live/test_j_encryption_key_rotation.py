"""P271 (j): an agent replaces its encryption key; its peer on another relay follows it,
and what was sealed to the old key still opens."""
import json
import sqlite3

from tests.live.threads import thread_id_for


def _read(agent, other: str) -> list[tuple]:
    out = agent.tool("get_dm_messages", thread_id=thread_id_for(agent, other))
    assert out["success"], out
    return [(m["content"], m["decrypted"]) for m in out["messages"]]


def test_a_replaced_encryption_key_is_followed_across_relays(fleet):
    a, b = fleet.a, fleet.b
    alpha = fleet.registered("alpha", a)
    bob = fleet.registered("bob", b)
    for agent in (alpha, bob):
        assert agent.tool("set_dm_policy", other_relays=True)["success"]
    assert bob.tool("send_dm", to_agent_id=alpha.address, content="bob writes first")["success"]
    assert alpha.tool("send_dm", to_agent_id=bob.address, content="sealed to the old key")["success"]

    old, new, published = bob.call(
        "from emporia import identity\n"
        "old, new = identity.rotate_encryption_keypair(m.AGENT_ID)\n"
        "r = [old, new, m.register_agent(agent_id=m.AGENT_ID)['success']]")
    assert published and old != new

    # alpha replies in the thread bob began: its relay asks B again, and its client follows the newer binding
    assert alpha.tool("send_dm", to_agent_id=bob.address, content="sealed to the new key")["success"]
    db = sqlite3.connect(f"file:{b.root / 'relay.sqlite3'}?mode=ro", uri=True)
    last = json.loads(db.execute("SELECT content FROM dm_messages ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    assert last["alg"].startswith("x25519")
    db.close()
    seen = _read(bob, alpha.address)
    assert ("sealed to the old key", True) in seen and ("sealed to the new key", True) in seen
    # and the new message is sealed to the new key: bob's current key alone opens it
    opened_with_new_only = bob.call(
        "from emporia import crypto_box, identity\n"
        f"t = {thread_id_for(bob, alpha.address)!r}\n"
        "msgs = m._bearer_request('get', m._base() + f'/dm/{t}/messages', agent_id=m.AGENT_ID,"
        " params={'agent_id': m.AGENT_ID}, timeout=10).json()['messages']\n"
        "cur = identity.encryption_private_keys(m.AGENT_ID)[0]\n"
        "x = msgs[-1]\n"
        "env = json.loads(x['content'])\n"
        "label = next(k for k in env['keys'] if k.startswith(m.AGENT_ID))\n"
        "r = crypto_box.open_sealed(env, label, cur, crypto_box.message_aad('dm', t, x['sender_id'],"
        " x.get('msg_type', ''), x.get('nonce') or ''))")
    assert opened_with_new_only == "sealed to the new key"
