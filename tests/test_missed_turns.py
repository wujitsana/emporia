"""A turn notice sent while a bridge was away is handed over when it reconnects: newest per game, last day."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from emporia.plugins.platforms.emporia.tunnel import missed_turn_frames

NOW = datetime(2026, 11, 11, 18, 30, tzinfo=UTC)


def ev(iid, sid, minutes_ago, *, read=False, kind="your_turn", step=1):
    return {"inbox_id": iid, "event_type": kind, "is_read": read,
            "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "payload": {"type": kind, "session_id": sid, "step_number": step}}


def test_newest_unread_turn_per_game_from_the_last_day():
    events = [ev("i5", "s1", 1, step=3), ev("i4", "s1", 5, step=2), ev("i3", "s2", 10),
              ev("i2", "s3", 60 * 30), ev("i1", "s4", 2, read=True), ev("i0", "s5", 3, kind="session_completed")]
    frames = missed_turn_frames(events, now=NOW)
    assert [f["inbox_id"] for f in frames] == ["i3", "i5"]
    assert frames[1]["session_id"] == "s1" and frames[1]["step_number"] == 3 and frames[1]["replayed"] is True


def test_nothing_missed_is_nothing_replayed():
    assert missed_turn_frames([], now=NOW) == []
    assert missed_turn_frames([{"event_type": "your_turn", "payload": "junk"}], now=NOW) == []


def test_an_older_notice_is_not_replayed_when_the_newest_was_read():
    """Fable pre-release: the newest notice for a game was read, so an older unread one is stale."""
    events = [ev("i2", "s1", 1, read=True, step=3), ev("i1", "s1", 5, step=2)]
    assert missed_turn_frames(events, now=NOW) == []
