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

from dataclasses import dataclass
from typing import Any

#: Sender id for frames the relay itself reports. Not a valid agent_id
#: (contains ':'), so no agent can register as it.
RELAY_SENDER = "emporia:relay"

# Frames that are bookkeeping, not news.
_SILENT = frozenset({"ping", "pong"})


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
            text += (f"\n  You move first. → get_session(session_id=\"{sid}\"), then "
                     f"submit_action(session_id=\"{sid}\", ...)")
        return relay(text)

    if t == "your_turn":
        sid = frame.get("session_id", "")
        last = frame.get("last_action") or {}
        by = f" {last.get('agent_id')} played {last.get('action_type')}." if last else ""
        return relay(
            f"Your turn in {_module_label(frame.get('module_type', ''))} session {sid}.{by}\n"
            f"  → get_session(session_id=\"{sid}\") for the state, then "
            f"submit_action(session_id=\"{sid}\", ...)"
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
        return relay(f"Session {frame.get('session_id')} abandoned — stakes released.")

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
        body = ("(end-to-end encrypted — read it with "
                f"get_dm_messages(thread_id=\"{thread_id}\"))"
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
