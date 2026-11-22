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


def lore(iid, minutes_ago, *, chapter="3", due="write", left=3600, read=False):
    return {"inbox_id": iid, "event_type": "lore_due", "is_read": read,
            "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "payload": {"type": "lore_due", "chapter": chapter, "due": due, "seconds_left": left}}


def test_an_unread_lore_notice_inside_its_window_is_replayed_with_the_time_left():
    [f] = missed_turn_frames([lore("l1", 10)], now=NOW)
    assert f["inbox_id"] == "l1" and f["replayed"] is True and f["seconds_left"] == 3600 - 600


def test_a_lore_notice_whose_window_has_passed_or_has_no_number_is_not_replayed():
    assert missed_turn_frames([lore("l1", 61)], now=NOW) == []
    assert missed_turn_frames([lore("l1", 60)], now=NOW) == []
    for bad in (None, "soon", True, float("nan"), 1e999):
        assert missed_turn_frames([lore("l1", 1, left=bad)], now=NOW) == [], bad


def test_a_read_lore_notice_is_not_replayed_and_only_the_newest_per_chapter_and_window_counts():
    assert missed_turn_frames([lore("l1", 5, read=True)], now=NOW) == []
    assert missed_turn_frames([lore("l2", 1, read=True), lore("l1", 5)], now=NOW) == []
    frames = missed_turn_frames([lore("l3", 1, due="pick"), lore("l2", 2), lore("l1", 5),
                                 lore("l0", 6, chapter="2")], now=NOW)
    assert sorted(f["inbox_id"] for f in frames) == ["l0", "l2", "l3"]


def test_turn_and_lore_notices_replay_together_oldest_first():
    frames = missed_turn_frames([ev("t1", "s1", 1), lore("l1", 5)], now=NOW)
    assert [f["inbox_id"] for f in frames] == ["l1", "t1"]
