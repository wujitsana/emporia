"""Who may read a room: one answer, asked by both the thread routes (`_readable_room`) and the post routes
(`_object_problem`). A private room is for its members, an encrypted room is for its members, a public open
room is for anyone, and a lab's room follows the same rule."""
import uuid

import pytest

from emporia import relay_server as rs
from emporia import rooms as rooms_db
from emporia.routes import posts as post_routes
from emporia.routes import room_threads

MEMBER, STRANGER = "rd_member", "rd_stranger"


def _room(**kw) -> str:
    room = rooms_db.create_room(name=f"rd {uuid.uuid4().hex[:6]}", creator_id=MEMBER, db_path=rs.DATABASE_PATH, **kw)
    return room.room_id


CASES = {
    "public open": dict(room_type="public", gate_type="open"),
    "public open, encrypted": dict(room_type="public", gate_type="open", encrypted=True),
    "private invite": dict(room_type="private", gate_type="invite"),
    "private paid": dict(room_type="private", gate_type="paid", entry_fee_cents=300),
    "private open": dict(room_type="private", gate_type="open"),
    "private, encrypted": dict(room_type="private", gate_type="invite", encrypted=True),
}
#: who may read each: (the member, a stranger, nobody signed in)
EXPECTED = {
    "public open": (True, True, True),
    "public open, encrypted": (True, False, False),
    "private invite": (True, False, False),
    "private paid": (True, False, False),
    "private open": (True, False, False),
    "private, encrypted": (True, False, False),
}


@pytest.mark.parametrize("name", list(CASES))
def test_both_readers_agree_on_who_may_read_a_room(name):
    room_id = _room(**CASES[name])
    for caller, expected in zip((MEMBER, STRANGER, ""), EXPECTED[name]):
        with rs.DB_LOCK, rs.get_db() as conn:
            threads = room_threads._readable_room(conn, caller, room_id) is not None
            problem = post_routes._object_problem(conn, caller, {"kind": "room", "id": room_id}, {})
            row = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
        assert threads == expected, f"{name}: thread routes, caller {caller or 'nobody'}"
        assert (problem is None) == expected, f"{name}: post routes, caller {caller or 'nobody'}"
        assert rooms_db.may_read(row, caller) == expected, f"{name}: the shared rule, caller {caller or 'nobody'}"


def test_a_labs_room_follows_the_same_rule():
    room_id = _room(room_type="private", gate_type="invite")
    repo = f"owner/repo-{uuid.uuid4().hex[:6]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        values = {"slug": uuid.uuid4().hex[:8], "name": "rd lab", "room_id": room_id, "subject_kind": "github_repo",
                  "subject_ref": repo, "branch": "main", "proof_commit": "0" * 40, "creator_id": MEMBER,
                  "creator_stable": "st", "signature": "sig", "signed_at": "2026-11-01T00:00:00+00:00",
                  "created_at": "2026-11-01T00:00:00+00:00"}
        conn.execute(f"INSERT INTO labs ({','.join(values)}) VALUES ({','.join('?' * len(values))})", list(values.values()))
        ref = {"kind": "gh_issue", "id": f"{repo}#7"}
        assert post_routes._object_problem(conn, MEMBER, ref, {}) is None
        assert post_routes._object_problem(conn, STRANGER, ref, {})[0] == 403
