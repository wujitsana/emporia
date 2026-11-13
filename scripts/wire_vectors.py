"""The wire vectors dashboard/src/integrityCheck.test.ts checks against, made by the Python side so every
client-side check in TypeScript is held to what `wire.py`, `rooms.py` and `identity.py` really produce.

Deterministic: Ed25519 keys come from fixed seeds (sha256 of "emporia test vector <n>"), nonces and times are
fixed, and only public keys and signatures are written. Running it twice gives the same bytes, so a test can
require the committed file to equal the output (tests/test_wire_vectors.py).

    .venv/bin/python scripts/wire_vectors.py > dashboard/src/wire.vectors.json

Every payload is built by calling the real functions; `python_verified` is what the real verifier answers.
"""
import hashlib
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from emporia import identity, rooms, wire

RELAY_URL = "http://vector-relay.example"
ROOM_ID = "room_vector_1"
THREAD_ID = "dm_vector_1"
# Non-ASCII on purpose: an em-dash, CJK, an astral emoji, a quote and a backslash are where a canonical-JSON
# port is most likely to diverge from Python's ensure_ascii default.
UNICODE_TEXT = 'Hello — 世界 🎉 "quoted" and a\\backslash'


def _key(n: int):
    seed = hashlib.sha256(f"emporia test vector {n}".encode()).digest()
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return priv, pub


def _tamper(s: str) -> str:
    return s + " tampered"


def _room_case(name, priv, pub, stable_id, content, nonce, created_at, *, msg_type="chat", served=None,
               check_room=ROOM_ID, check_key=None):
    """One room message as the relay serves it. `served` overrides served fields after signing; `check_room` is
    the room the reader checks it against; `check_key` the sender key the reader holds (default: the signer's)."""
    sig = identity.sign_payload(priv, wire.signed_room_message_payload(ROOM_ID, content, msg_type, nonce, stable_id))
    message = {"content": content, "msg_type": msg_type, "nonce": nonce, "signature": sig,
               "sender_stable_id": stable_id, "created_at": created_at}
    message.update(served or {})
    key = check_key or pub
    return {"name": name, "room_id": check_room, "sender_public_key": key, "message": message,
            "python_verified": identity.verify_room_message(check_room, message, key)}


def _dm_case(name, priv, pub, content, nonce, created_at, served=None):
    sig = identity.sign_payload(priv, wire.signed_dm_thread_payload(THREAD_ID, content, "text", nonce))
    message = {"content": content, "msg_type": "text", "nonce": nonce, "signature": sig, "created_at": created_at}
    message.update(served or {})
    return {"name": name, "thread_id": THREAD_ID, "sender_public_key": pub, "message": message,
            "python_verified": identity.verify_dm_message(THREAD_ID, message, pub)}


def _chain(rows):
    """[(sender_id, content, created_at)] -> messages with their stored chain_hash, as the relay writes them."""
    out, prev = [], None
    for sender_id, content, created_at in rows:
        h = rooms.compute_chain_hash(prev, sender_id, content, created_at)
        out.append({"sender_id": sender_id, "content": content, "created_at": created_at, "chain_hash": h})
        prev = h
    return out


def _head(relay_priv, relay_pub, key, scope_key, scope_id, messages, name, **change):
    """A relay-signed chain head, over exactly the bytes routes/rooms.py and routes/dm.py sign."""
    head = messages[-1]["chain_hash"] if messages else ""
    item = {scope_key: scope_id, "chain_head": head, "message_count": len(messages)}
    sig = relay_priv.sign(identity.federation_signable_bytes(RELAY_URL, key, [item])).hex()
    served = {"chain_head": head or None, "message_count": len(messages), "origin_relay": RELAY_URL,
              "origin_public_key_hex": relay_pub, "signature": sig}
    served.update(change)
    signed_item = {scope_key: scope_id, "chain_head": served["chain_head"] or "", "message_count": served["message_count"]}
    ok = identity.verify_raw(identity.federation_signable_bytes(served["origin_relay"], key, [signed_item]),
                             served["signature"], served["origin_public_key_hex"])
    return {"name": name, "relay_url": RELAY_URL, scope_key: scope_id, "served": served, "messages": messages,
            "python_verified": ok}


def build() -> dict:
    alice, ALICE = _key(1)
    bob, BOB = _key(2)
    relay, RELAY = _key(3)
    a_sid, b_sid = wire.stable_id_for(ALICE), wire.stable_id_for(BOB)
    T = ["2026-11-01T10:00:00.000001+00:00", "2026-11-01T10:00:05+00:00", "2026-11-01T10:00:10.250000+00:00"]

    room_message = [
        _room_case("v2 with stable id", alice, ALICE, a_sid, "hello room", "n-room-1", T[0]),
        _room_case("v2 non-ASCII content", alice, ALICE, a_sid, UNICODE_TEXT, "n-room-2", T[1]),
        # the relay's own lab note is signed with an empty stable id
        _room_case("v2 with an empty stable id", alice, ALICE, "", "a note with no signer id", "n-room-3", T[2]),
        _room_case("tampered content", alice, ALICE, a_sid, "hello room", "n-room-1", T[0],
                   served={"content": _tamper("hello room")}),
        _room_case("tampered stable id", alice, ALICE, a_sid, "hello room", "n-room-1", T[0],
                   served={"sender_stable_id": b_sid}),
        _room_case("stable id missing from the served row", alice, ALICE, a_sid, "hello room", "n-room-1", T[0],
                   served={"sender_stable_id": None}),
        _room_case("another room", alice, ALICE, a_sid, "hello room", "n-room-1", T[0],
                   check_room="room_other"),
        _room_case("signed by someone else", bob, BOB, b_sid, "hello room", "n-room-4", T[0], check_key=ALICE),
    ]
    # a v1 payload (no type, no stable id) signed by the right key: v2 verification must refuse it
    v1_sig = identity.sign_payload(alice, {"room_id": ROOM_ID, "content": "hello room", "msg_type": "chat",
                                           "nonce": "n-room-5"})
    v1_msg = {"content": "hello room", "msg_type": "chat", "nonce": "n-room-5", "signature": v1_sig,
              "sender_stable_id": a_sid, "created_at": T[0]}
    room_message.append({"name": "v1 shaped signature", "room_id": ROOM_ID, "sender_public_key": ALICE,
                         "message": v1_msg, "python_verified": identity.verify_room_message(ROOM_ID, v1_msg, ALICE)})

    dm_message = [
        _dm_case("dm", alice, ALICE, "hello thread", "n-dm-1", T[0]),
        _dm_case("dm non-ASCII content", alice, ALICE, UNICODE_TEXT, "n-dm-2", T[1]),
        _dm_case("dm tampered content", alice, ALICE, "hello thread", "n-dm-1", T[0],
                 served={"content": _tamper("hello thread")}),
    ]

    ids = []
    for name, payload in (
        ("room message", wire.signed_room_message_payload(ROOM_ID, "hello room", "chat", "n-room-1", a_sid)),
        ("room message non-ASCII", wire.signed_room_message_payload(ROOM_ID, UNICODE_TEXT, "chat", "n-room-2", a_sid)),
    ):
        ids.append({"name": name, "payload": payload, "message_id": wire.message_id_for(payload)})
    ids.append({"name": "one changed byte is another id", "payload": {**ids[0]["payload"], "content": "hello room."},
                "message_id": wire.message_id_for({**ids[0]["payload"], "content": "hello room."})})

    rows = [("agent_a", "first", T[0]), ("agent_b", "second — 日本語", T[1]), ("agent_a", UNICODE_TEXT, T[2])]
    chained = _chain(rows)
    chain_hash = [
        {"name": "genesis", "prev_hash": None, **{k: chained[0][k] for k in ("sender_id", "content", "created_at", "chain_hash")}},
        {"name": "chained", "prev_hash": chained[0]["chain_hash"],
         **{k: chained[1][k] for k in ("sender_id", "content", "created_at", "chain_hash")}},
        {"name": "chained non-ASCII", "prev_hash": chained[1]["chain_hash"],
         **{k: chained[2][k] for k in ("sender_id", "content", "created_at", "chain_hash")}},
        {"name": "tampered content", "prev_hash": chained[0]["chain_hash"], "sender_id": "agent_b",
         "content": _tamper(rows[1][1]), "created_at": T[1], "chain_hash": chained[1]["chain_hash"]},
    ]
    for c in chain_hash:
        c["python_ok"] = rooms.link_ok(c["prev_hash"], c)
    # a removed message: its text is gone, so its own link is taken as stored (rooms.link_ok) and the next
    # message must still chain onto its stored hash
    removed = dict(chained[1], content="", deleted_at="2026-11-02T09:00:00+00:00")
    after = {**chained[2], "chain_hash": rooms.compute_chain_hash(removed["chain_hash"], "agent_a", rows[2][1], T[2])}
    removed_chain = {
        "name": "removed message taken as stored",
        "messages": [chained[0], removed, after],
        "python_link_ok": [rooms.link_ok(None, chained[0]), rooms.link_ok(chained[0]["chain_hash"], removed),
                           rooms.link_ok(removed["chain_hash"], after)],
    }
    assert all(removed_chain["python_link_ok"])

    # the full walk over a chain with a removed message, Python's verdict for each: the loop of
    # rooms.verify_chain (link_ok on every row, the next prev is the row's stored hash) and
    # rooms.verify_chain_page over the whole chain from the signed head
    def _walk(msgs):
        prev = None
        for m in msgs:
            if not rooms.link_ok(prev, m):
                return False
            prev = m["chain_hash"]
        return True

    def _page(msgs):
        ok, _, _ = rooms.verify_chain_page(msgs, msgs[-1]["chain_hash"], True)
        return ok

    gone = {"content": "", "deleted_at": "2026-11-02T09:00:00+00:00"}
    walk_cases = [
        ("removed message in the middle", [chained[0], removed, after]),
        ("removed first message", [dict(chained[0], **gone), {**chained[1]}, {**chained[2]}]),
        ("tampered stored hash after a removed message", [chained[0], removed, dict(after, chain_hash="0" * 64)]),
        ("removed message with a tampered stored hash", [chained[0], dict(removed, chain_hash="1" * 64), after]),
        ("removed message with no stored hash", [chained[0], dict(removed, chain_hash=None), after]),
        ("tampered content beside a removed message", [chained[0], removed, dict(after, content=_tamper(rows[2][1]))]),
    ]
    removed_chain_walk = [
        {"name": n, "messages": ms, "python_walk_ok": _walk(ms), "python_page_ok": _page(ms)}
        for n, ms in walk_cases
    ]
    assert removed_chain_walk[0]["python_walk_ok"] and removed_chain_walk[1]["python_walk_ok"]

    room_heads = [
        _head(relay, RELAY, "room_chain", "room_id", ROOM_ID, chained, "room head"),
        _head(relay, RELAY, "room_chain", "room_id", ROOM_ID, chained, "room head, message count changed",
              message_count=len(chained) + 1),
        _head(relay, RELAY, "room_chain", "room_id", ROOM_ID, chained, "room head, another relay's key",
              origin_public_key_hex=_key(4)[1]),
        _head(relay, RELAY, "room_chain", "room_id", ROOM_ID, [], "empty room head"),
    ]
    dm_heads = [
        _head(relay, RELAY, "dm_chain", "thread_id", THREAD_ID, chained, "dm head"),
        _head(relay, RELAY, "dm_chain", "thread_id", THREAD_ID, chained, "dm head, head changed",
              chain_head=chained[0]["chain_hash"]),
    ]
    # the DM walk needs a nonce and seq on every row
    for h in dm_heads:
        h["messages"] = [dict(m, nonce=f"n-chain-{i}", seq=i + 1) for i, m in enumerate(chained)]

    return {
        "about": "Made by scripts/wire_vectors.py from wire.py, rooms.py and identity.py; do not edit by hand.",
        "relay_url": RELAY_URL,
        "room_message": room_message,
        "dm_message": dm_message,
        "message_id": ids,
        "chain_hash": chain_hash,
        "removed_chain": removed_chain,
        "removed_chain_walk": removed_chain_walk,
        "room_chain_head": room_heads,
        "dm_chain_head": dm_heads,
    }


def main() -> None:
    print(json.dumps(build(), indent=1, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
