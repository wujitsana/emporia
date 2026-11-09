"""P273 (k): a relay that writes a member into an encrypted invite room on its
own is refused by the sender's client; a member who leaves is sealed to no more."""
import json
import sqlite3


def _members(relay, room_id: str) -> list[str]:
    db = sqlite3.connect(f"file:{relay.root / 'relay.sqlite3'}?mode=ro", uri=True)
    try:
        return json.loads(db.execute("SELECT members_json FROM rooms WHERE room_id = ?", (room_id,)).fetchone()[0])
    finally:
        db.close()


def test_a_member_no_signed_act_put_there_is_never_sealed_to(fleet):
    a = fleet.a
    alpha = fleet.registered("alpha", a)
    bob = fleet.registered("bob", a)
    mole = fleet.registered("mole", a)
    made = alpha.tool("create_room", name="k sealed room", creator_id=alpha.name, room_type="private",
                      gate_type="invite", encrypted=True)
    assert made["success"], made
    room_id = made["room"]["room_id"]
    assert alpha.tool("invite_to_room", room_id=room_id, agent_id=bob.name, invited_by=alpha.name)["success"]
    assert bob.tool("join_room", room_id=room_id, agent_id=bob.name)["success"]
    sent = alpha.tool("send_room_message", room_id=room_id, sender_id=alpha.name, content="for bob only")
    assert sent["success"], sent
    assert alpha.verify("room", room_id)["unproven_members"] == []

    # the relay's operator writes the mole in, past the invite and with no join of its own
    db = sqlite3.connect(a.root / "relay.sqlite3", timeout=10)
    db.execute("UPDATE rooms SET members_json = ? WHERE room_id = ?",
               (json.dumps(_members(a, room_id) + [mole.name]), room_id))
    db.commit()
    db.close()
    refused = alpha.tool("send_room_message", room_id=room_id, sender_id=alpha.name, content="not for the mole")
    assert not refused["success"] and mole.name in refused["error"], refused
    assert alpha.verify("room", room_id)["unproven_members"] == [mole.name]

    # taken out again, sending works; bob leaves, and is no longer among the members
    db = sqlite3.connect(a.root / "relay.sqlite3", timeout=10)
    db.execute("UPDATE rooms SET members_json = ? WHERE room_id = ?",
               (json.dumps([m for m in _members(a, room_id) if m != mole.name]), room_id))
    db.commit()
    db.close()
    assert bob.tool("leave_room", room_id=room_id)["success"]
    assert _members(a, room_id) == [alpha.name]
    after = alpha.tool("send_room_message", room_id=room_id, sender_id=alpha.name, content="alone now")
    assert after["success"], after
