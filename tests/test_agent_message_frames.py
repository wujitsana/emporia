"""Relay audit 11-11 (F1): a frame from POST /messages carries `from_agent`. The bridge never reads one
in the relay's voice, and keys it by its sender, so a fresh session id per message is not a new thread."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emporia.plugins.platforms.emporia.frames import RELAY_SENDER, format_frame, frame_topic  # noqa: E402


def test_relay_voiced_types_from_an_agent_are_dropped():
    for kind in ("your_turn", "funding_required", "session_completed", "kicked_from_room", "made_up"):
        frame = {"type": kind, "from_agent": "mallory", "session_id": "s1", "payload": {}, "message_id": "m1"}
        assert format_frame(frame, "victim") is None, kind


def test_an_offer_is_the_sender_s_and_keyed_by_sender():
    frame = {"type": "challenge", "from_agent": "mallory", "session_id": "s-fresh-1", "payload": {}}
    notice = format_frame(frame, "victim")
    assert notice is not None and notice.sender != RELAY_SENDER and notice.sender.startswith("mallory")
    assert frame_topic(frame) == "dm:mallory"
    assert frame_topic({**frame, "session_id": "s-fresh-2"}) == "dm:mallory"


def test_the_relay_s_own_your_turn_still_reads_as_the_relay():
    notice = format_frame({"type": "your_turn", "session_id": "s1", "module_type": "emporia:chess:v1"}, "me")
    assert notice is not None and notice.sender == RELAY_SENDER
