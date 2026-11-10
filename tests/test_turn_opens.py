"""11-10 live test: a game dealt from a public draw (bargain, liar's dice) opens its first turn about a minute after
it starts. The first mover was told "you move first" at once, found nothing dealt, and nothing woke it again."""
import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from emporia import relay_server  # noqa: F401  (loads the routes in their order)
from emporia.plugins.platforms.emporia.frames import format_frame
from emporia.routes import sessions


def test_only_a_later_first_turn_is_scheduled():
    now = datetime.now(UTC)
    assert sessions._first_turn_opens_at({"turn_started_at": now.isoformat()}) is None
    later = now + timedelta(seconds=70)
    assert sessions._first_turn_opens_at({"turn_started_at": later.isoformat()}) == later
    assert sessions._first_turn_opens_at({}) is None
    assert sessions._first_turn_opens_at({"turn_started_at": "not a time"}) is None


def _run_tell(session):
    with patch.object(sessions.rs, "load_session", return_value=session), \
         patch.object(sessions.rs, "broadcast_to_agent", new=AsyncMock()) as bcast:
        asyncio.run(sessions._tell_when_open("sess_x", datetime.now(UTC) - timedelta(seconds=5)))
    return bcast


def test_the_first_mover_is_told_when_the_turn_opens():
    bcast = _run_tell({"status": "active", "step_number": 0, "module_type": "emporia:bargain:v2",
                       "current_agent": "alpha", "participants": ["alpha", "beta"], "state": {"data": {}}})
    bcast.assert_awaited_once()
    agent, frame = bcast.await_args.args
    assert agent == "alpha" and frame["type"] == "your_turn" and frame["session_id"] == "sess_x"


def test_nobody_is_told_once_someone_moved_or_the_game_ended():
    for s in ({"status": "active", "step_number": 1, "module_type": "emporia:bargain:v2", "current_agent": "beta"},
              {"status": "completed", "step_number": 0, "module_type": "emporia:bargain:v2", "current_agent": "alpha"},
              None):
        _run_tell(s).assert_not_awaited()


def test_the_started_notice_says_when_and_not_to_wait():
    at = "2026-11-10T00:36:24+00:00"
    frame = {"type": "session_started", "session_id": "sess_x", "module_type": "emporia:bargain:v2",
             "participants": ["alpha", "beta"], "current_agent": "alpha", "opens_at": at}
    first = format_frame(frame, "alpha").text
    assert "You move first, from 2026-11-10 00:36 UTC" in first and "your turn" in first
    assert "The first move is not yours" in format_frame(frame, "beta").text
    plain = format_frame({**frame, "opens_at": None}, "alpha").text
    assert "You move first." in plain


def test_negotiation_turn_notices_offer_a_line_at_the_table():
    """11-10: table talk had nearly vanished (6 lines all day); the move notices never mentioned `say`."""
    for module in ("emporia:bargain:v2", "emporia:liars-dice:v1"):
        n = format_frame({"type": "your_turn", "session_id": "sess_x", "module_type": module}, "alpha")
        assert 'say="..."' in n.text and "bluffing is fair play" in n.text, module


def _run_remind(session, agents, step, reveal=False):
    with patch.object(sessions.rs, "load_session", return_value=session), \
         patch.object(sessions.rs, "broadcast_to_agent", new=AsyncMock()) as bcast, \
         patch.object(sessions.asyncio, "sleep", new=AsyncMock()):
        asyncio.run(sessions._remind("sess_x", agents, step, 150.0, reveal))
    return bcast


def test_a_player_who_has_not_moved_is_reminded_once():
    """Live run 11-10: a provider error ended a player's turn before it moved, nothing woke it again, the game timed
    out. At half the clock the relay says it once more."""
    waiting = {"status": "active", "step_number": 1, "module_type": "emporia:bargain:v2", "current_agent": "beta",
               "state": {"data": {}}}
    bcast = _run_remind(waiting, ["beta"], 1)
    bcast.assert_awaited_once()
    agent, frame = bcast.await_args.args
    assert agent == "beta" and frame["reminder"] is True and frame["type"] == "your_turn"
    assert "Reminder — your last attempt may not have gone through." in format_frame(frame, "beta").text
    moved = {**waiting, "step_number": 2, "current_agent": "alpha"}
    _run_remind(moved, ["beta"], 1).assert_not_awaited()
    _run_remind({**waiting, "status": "completed"}, ["beta"], 1).assert_not_awaited()


def test_a_player_who_has_not_revealed_is_reminded_and_one_who_has_is_not():
    s = {"status": "active", "step_number": 3, "module_type": "emporia:bargain:v2", "current_agent": "alpha",
         "state": {"data": {"ended": {"how": "accepted"}, "rand": {"reveals": {"alpha": {}}}}}}
    bcast = _run_remind(s, ["alpha", "beta"], 3, reveal=True)
    assert [c.args[0] for c in bcast.await_args_list] == ["beta"]
    assert bcast.await_args.args[1]["reveal"] is True


def test_no_clock_no_reminder():
    with patch.object(sessions, "_schedule") as sched:
        sessions._remind_later("sess_x", ["beta"], 1, {})
        sessions._remind_later("sess_x", ["beta"], 1, {"move_time_limit_s": 0})
    sched.assert_not_called()
