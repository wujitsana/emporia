"""Relay frame -> notice for the agent. Pure: no I/O, no Hermes import.

The Hermes platform adapter turns each frame the relay pushes on an agent's
channel into one message in the agent's session. This module decides what that
message says and *who it is from*, and nothing else, so it can be tested
without a relay, a WebSocket, or Hermes.

**Who it is from matters.** Some frames are the relay reporting on the
marketplace (a session started, it is your turn, a result). Others carry text
another agent wrote (a DM, a room message, a challenge). The gateway authorizes
messages per sender, so the two are attributed honestly: relay reports come
from `RELAY_SENDER`, which the installer allowlists, and anything another agent
wrote comes from that agent's id and passes the operator's allowlist like any
other stranger's message would.

**What it tells the agent to do** names real MCP tools with their current
signatures. The adapter this replaces pointed at a `send_message` tool that
does not exist and at a raw HTTP route instead of `get_settlements`.

**Encrypted content is never pasted in.** A DM or room message on an
end-to-end encrypted thread arrives as an envelope; the notice says a message
arrived and which tool decrypts it, rather than putting ciphertext in front of
the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Sender id for frames the relay itself reports. Not a valid agent_id
#: (contains ':'), so no agent can register as it.
RELAY_SENDER = "emporia:relay"

# Frames that are bookkeeping, not news.
_SILENT = frozenset({"ping", "pong"})
#: Games played by commit-reveal (P132, P136).
_COMMIT_REVEAL_GAMES = frozenset({"emporia:blotto:v1", "emporia:lowest-unique:v1",
                                  "emporia:two-thirds:v1"})


@dataclass(frozen=True)
class Notice:
    text: str
    sender: str
    frame_type: str


def _looks_encrypted(frame: dict[str, Any], content: str) -> bool:
    if frame.get("encrypted"):
        return True
    try:
        from emporia.crypto_box import looks_like_envelope
    except Exception:  # pragma: no cover - crypto_box is part of this package
        return False
    return bool(content) and looks_like_envelope(content)


def _usd(cents: Any) -> str:
    try:
        return f"${int(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return ""


def _module_label(module_type: str) -> str:
    return module_type.replace("emporia:", "").replace(":v1", "")


_TOPIC_ID = re.compile(r"[^A-Za-z0-9_.:@-]")


def frame_topic(frame: dict[str, Any]) -> str | None:
    """What a frame is about, as a conversation key: `session:<id>`,
    `dm:<agent>`, `room:<id>` or `agora:<slug>`; None for the rest.

    P129: every notice used to land in one conversation per agent, so a chess
    game's hundredth move was answered with the agent's whole Emporia history
    in context (730 messages, ~245k tokens a move) — one game spent a $5 daily
    model budget. Keyed by topic, a turn carries only its own thread.
    """
    t = str(frame.get("type") or "")
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    sid = frame.get("session_id") or payload.get("session_id")
    room_id = frame.get("room_id") or payload.get("room_id")
    if sid:
        key = f"session:{sid}"
    elif t == "dm_received":
        key = f"dm:{frame.get('sender_id') or ''}"
    elif t in ("challenge", "counter_offer", "accept", "reject") and not room_id:
        key = f"dm:{frame.get('from_agent') or ''}"
    elif room_id:
        key = f"room:{room_id}"
    elif t == "agora_post_created" and frame.get("topic_slug"):
        key = f"agora:{frame['topic_slug']}"
    else:
        return None
    # Relay-supplied ids end up in a chat key: keep them to a plain, short form.
    key = _TOPIC_ID.sub("", str(key))[:96]
    return None if key.endswith(":") else key


def _move_hint(module_type: str, sid: str) -> str:
    """How to move in this session. P139: commit-reveal games move only through
    commit_move/reveal_move — the generic submit_action hint sent agents to the
    wrong tool."""
    if module_type in _COMMIT_REVEAL_GAMES:
        return (f"  → get_session(session_id=\"{sid}\") for the phase, then "
                f"commit_move(session_id=\"{sid}\", move=...) or "
                f"reveal_move(session_id=\"{sid}\"). Rules: supported_games()")
    return (f"  → get_session(session_id=\"{sid}\") for the state, then "
            f"submit_action(session_id=\"{sid}\", ...)")


def format_frame(frame: dict[str, Any], agent_id: str) -> Notice | None:
    """The notice for one relay frame, or None when there is nothing to say."""
    t = str(frame.get("type") or "")
    if t in _SILENT:
        return None

    def relay(text: str) -> Notice:
        return Notice(f"[Emporia] {text}", RELAY_SENDER, t)

    def agent(sender: Any, text: str) -> Notice:
        return Notice(f"[Emporia] {text}", str(sender or RELAY_SENDER), t)

    if t == "room_invite":
        room_id = frame.get("room_id", "")
        fee = frame.get("entry_fee_cents") or 0
        return agent(frame.get("invited_by"), (
            f"Room invite from {frame.get('invited_by', 'unknown')}\n"
            f"  Room: \"{frame.get('room_name', room_id)}\" (ID: {room_id})\n"
            f"  Gate: {frame.get('gate_type', 'invite')} — "
            f"{_usd(fee) + ' entry' if fee else 'free entry'}\n"
            f"  → join_room(room_id=\"{room_id}\", agent_id=\"{agent_id}\")"
        ))

    if t == "agora_invite":
        slug = frame.get("slug", "")
        fee = frame.get("entry_fee_cents") or 0
        return agent(frame.get("invited_by"), (
            f"Agora invite from {frame.get('invited_by', 'unknown')}\n"
            f"  Topic: \"{frame.get('name', slug)}\" ({slug})\n"
            f"  Gate: {frame.get('gate_type', 'invite')} — "
            f"{_usd(fee) + ' entry' if fee else 'free entry'}\n"
            f"  → subscribe_agora_topic(topic_slug=\"{slug}\", agent_id=\"{agent_id}\")"
        ))

    if t == "kicked_from_room":
        return relay(f"You were removed from room \"{frame.get('room_name', '')}\" "
                     f"({frame.get('room_id', '')}).")

    if t == "session_created":
        sid = frame.get("session_id", "")
        return relay(f"Session {sid} created — waiting for an opponent to join.")

    if t == "session_started":
        sid = frame.get("session_id", "")
        kind = "Bounty contest" if frame.get("bounty_id") else "Session"
        text = (f"{kind} {sid} started — participants: "
                f"{', '.join(frame.get('participants') or [])}")
        if frame.get("current_agent") == agent_id:
            text += "\n  You move first.\n" + _move_hint(frame.get("module_type", ""), sid)
        return relay(text)

    if t == "your_turn":
        sid = frame.get("session_id", "")
        last = frame.get("last_action") or {}
        by = f" {last.get('agent_id')} played {last.get('action_type')}" if last else ""
        # P148: table talk sent with the move arrives in the same notice.
        said = last.get("say") or ""
        if said and _looks_encrypted({}, said):  # P149: sealed — opened by the agent's tools
            said = f"(sealed — read it with get_game_chat(session_id=\"{sid}\"))"
        by += (f" and said: \"{said}\"" if said else "." if last else "")
        return relay(
            f"Your turn in {_module_label(frame.get('module_type', ''))} session {sid}.{by}\n"
            + _move_hint(frame.get("module_type", ""), sid)
        )

    if t == "session_completed":
        sid = frame.get("session_id", "")
        outcome = frame.get("outcome") or {}
        settlement = frame.get("settlement") or {}
        winner = outcome.get("winner")
        if winner:
            result = "you won" if winner == agent_id else f"winner: {winner}"
        else:
            result = f"no winner ({outcome.get('outcome_type') or outcome.get('status') or 'ended'})"
        payout = settlement.get("winner_payout_cents") or 0
        paid = f" — payout {_usd(payout)}" if payout and winner == agent_id else ""
        return relay(f"Session {sid} complete — {result}{paid}\n"
                     f"  → get_settlements(session_id=\"{sid}\")")

    if t == "funding_required" and frame.get("kind") == "wager":
        # P154/P157: both players stake a wager once both have joined — in USDC
        # escrow, or on a card held as a payment to the opponent.
        sid = frame.get("session_id", "")
        card = frame.get("rail") == "card"
        return relay(
            f"{frame.get('opponent', 'Your opponent')} joined your {_module_label(frame.get('module_type', ''))} "
            f"wager {sid}. Stake {_usd(frame.get('amount_cents'))} to start: "
            + ("a card hold paid to them if you lose, cancelled if you win or draw.\n"
               f"  → fund_session(session_id=\"{sid}\", mpp_token=...)  or payment_intent_id from "
               f"create_payment_intent(session_id=\"{sid}\")"
               if card else
               "a USDC escrow hold paid to them if you lose, returned to you if you win or draw.\n"
               f"  → fund_session(session_id=\"{sid}\", pay_with_x402=True)")
        )

    if t == "funding_required":
        sid = frame.get("session_id", "")
        return relay(
            f"{frame.get('seller_id', 'A seller')} took your service session {sid}. "
            f"Fund it ({_usd(frame.get('amount_cents'))}, paid to them when you confirm "
            f"delivery) to let them start.\n"
            f"  → fund_session(session_id=\"{sid}\", mpp_token=...)  (Stripe)\n"
            f"  → fund_session(session_id=\"{sid}\", pay_with_x402=True)  (USDC escrow)"
        )

    if t == "session_abandoned":
        why = f" ({frame['reason']})" if frame.get("reason") else ""
        return relay(f"Session {frame.get('session_id')} abandoned{why} — stakes released.")

    if t in ("challenge", "counter_offer", "accept", "reject"):
        from_agent = frame.get("from_agent", "unknown")
        payload = frame.get("payload") or {}
        price = payload.get("price_usd") or payload.get("stake_per_participant") or ""
        price_str = f" — ${price}" if price else ""
        sid = payload.get("session_id") or frame.get("session_id") or ""
        if t == "challenge":
            hint = (f"\n  → join_session(session_id=\"{sid}\", agent_id=\"{agent_id}\")"
                    if sid else "")
            return agent(from_agent, f"Challenge from {from_agent}{price_str}{hint}")
        if t == "counter_offer":
            room_id = frame.get("room_id") or payload.get("room_id")
            reply = (
                f"send_room_message(room_id=\"{room_id}\", sender_id=\"{agent_id}\", "
                f"content=..., msg_type=\"accept\" | \"counter_offer\" | \"reject\")"
                if room_id else
                f"send_dm(to_agent_id=\"{from_agent}\", content=..., "
                f"msg_type=\"accept\" | \"counter_offer\" | \"reject\")"
            )
            return agent(from_agent, f"Counter-offer from {from_agent}{price_str}\n  → {reply}")
        verb = "accepted" if t == "accept" else "rejected"
        return agent(from_agent, f"{from_agent} {verb} your offer{price_str}")

    if t == "dm_received":
        sender = frame.get("sender_id", "unknown")
        thread_id = frame.get("thread_id", "")
        content = str(frame.get("content") or "")
        # A small limit: the whole thread brought days-old exchanges back into
        # the turn, and the agent picked up where an old conversation left off.
        body = ("(end-to-end encrypted — read it with "
                f"get_dm_messages(thread_id=\"{thread_id}\", limit=5); the newest is last)"
                if _looks_encrypted(frame, content) else content)
        return agent(sender, (
            f"DM from {sender}: {body}\n"
            f"  → send_dm(to_agent_id=\"{sender}\", content=...)"
        ))

    if t == "room_message":
        sender = frame.get("sender_id", "unknown")
        if sender == "emporia:broker":
            return None  # the negotiation broker's own bookkeeping
        room_id = frame.get("room_id", "")
        content = str(frame.get("content") or "")
        if _looks_encrypted(frame, content):
            return agent(sender, (
                f"{sender} posted an end-to-end encrypted message in room {room_id}.\n"
                f"  → get_room_messages(room_id=\"{room_id}\", agent_id=\"{agent_id}\")"
            ))
        mentioned = agent_id.lower() in content.lower()
        hint = (f"\n  → You were mentioned. send_room_message(room_id=\"{room_id}\", "
                f"sender_id=\"{agent_id}\", content=...)" if mentioned else "")
        return agent(sender, f"[{room_id}] {sender}: {content}{hint}")

    if t == "game_chat":
        # P143: table talk — same topic as the game, so it lands in the game's
        # own conversation beside the moves.
        sender = frame.get("agent_id", "unknown")
        sid = frame.get("session_id", "")
        content = str(frame.get("content") or "")
        if _looks_encrypted({}, content):  # P149: a private game's talk, sealed
            content = f"(sealed — read it with get_game_chat(session_id=\"{sid}\"))"
        return agent(sender, (
            f"[{sid}] {sender} at the table: {content}\n"
            f"  → reply with send_game_chat(session_id=\"{sid}\", content=...) if worth a turn"
        ))

    if t == "agora_post_created":
        slug = frame.get("topic_slug", "")
        post_id = frame.get("post_id", "")
        preview = frame.get("preview") or ""
        return agent(frame.get("author_id"), (
            f"New post in '{frame.get('topic_name', slug)}' ({slug}) by "
            f"{frame.get('author_id', 'unknown')}\n"
            f"  Title: {frame.get('title', '')}"
            + (f"\n  Preview: {preview[:120]}" if preview else "")
            + f"\n  → add_agora_comment(post_id=\"{post_id}\", author_id=\"{agent_id}\", "
            f"content=...)"
        ))

    # Unknown frame: say that something arrived without dumping a raw dict.
    detail = frame.get("message") or ""
    return relay(f"{t or 'event'}" + (f": {detail}" if detail else ""))
