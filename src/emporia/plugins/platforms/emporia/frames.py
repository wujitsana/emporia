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
#: The sender of agent-attributed text whose author the relay named with no valid id
#: (security follow-up to the 11-05 audit): never the relay, never on the allowlist.
UNKNOWN_SENDER = "emporia:unknown-sender"
#: What a notice shows in place of a relay-supplied id that breaks the id rules.
INVALID_ID = "(invalid id)"
#: The idle sweep's reason for ending a game (`routes.sessions.sweep_idle_sessions`).
_IDLE_REASON = re.compile(r"no action for [0-9]{1,5}(?:\.[0-9]{1,3})?h")

# Frames that are bookkeeping, not news.
_SILENT = frozenset({"ping", "pong"})
#: Games played by commit-reveal (P132, P136).
_COMMIT_REVEAL_GAMES = frozenset({"emporia:blotto:v2", "emporia:lowest-unique:v2", "emporia:two-thirds:v2",
                                  "emporia:blotto:v1", "emporia:lowest-unique:v1", "emporia:two-thirds:v1"})
#: Played by offers in turn, with hidden priorities (gamify-bargain).
_BARGAIN = "emporia:bargain:v2"


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
                f"commit_move(session_id=\"{sid}\", move=..., rationale=...) or "
                f"reveal_move(session_id=\"{sid}\", rationale=...). Rules: supported_games()")
    if module_type == _BARGAIN:
        return (f"  → get_session(session_id=\"{sid}\") for the offers and your own values (`private`), "
                f"get_game_chat(session_id=\"{sid}\") for the talk, then submit_action(session_id=\"{sid}\", "
                f"action_type=\"offer\", payload={{\"keep\": {{...}}}}) — or \"accept\", or \"decline\" "
                f"the last offer. Rules: supported_games()")
    return (f"  → get_session(session_id=\"{sid}\") for the state, then "
            f"submit_action(session_id=\"{sid}\", ...)")


def format_frame(frame: dict[str, Any], agent_id: str, relay: str = "") -> Notice | None:
    """The notice for one relay frame, or None when there is nothing to say.

    `relay`: the host of a visited relay, "" for home. A visited relay is not the home
    relay (security follow-up to the 11-05 audit): its own reports come from
    `emporia:relay@<host>` and its agents from `<agent_id>@<host>`, neither on the
    operator's allowlist unless added. Every relay-supplied id put in the text meets
    `wire.plain_id` / `wire.agent_ref`, or is shown as a placeholder."""
    from emporia.wire import agent_ref, plain_host, plain_id

    t = str(frame.get("type") or "")
    if t in _SILENT:
        return None
    host = (plain_host(relay) or "invalid-host") if relay else ""
    relay_sender = f"{RELAY_SENDER}@{host}" if host else RELAY_SENDER

    def relay_notice(text: str) -> Notice:
        return Notice(f"[Emporia] {text}", relay_sender, t)

    def who(raw: Any) -> str:
        """An agent named by the relay, as the notice names it — and attributes it."""
        if not raw:
            return "unknown"
        ref = agent_ref(raw, bare_only=bool(host))
        if not ref:
            return INVALID_ID
        return f"{ref}@{host}" if host else ref

    def agent(raw: Any, text: str) -> Notice:
        name = who(raw)
        sender = name if name not in ("unknown", INVALID_ID) else (
            f"{UNKNOWN_SENDER}@{host}" if host else UNKNOWN_SENDER)
        return Notice(f"[Emporia] {text}", sender, t)

    def oid(raw: Any) -> str:
        return plain_id(raw) or (INVALID_ID if raw else "")

    if t == "room_invite":
        room_id = oid(frame.get("room_id"))
        fee = frame.get("entry_fee_cents") or 0
        return agent(frame.get("invited_by"), (
            f"Room invite from {who(frame.get('invited_by'))}\n"
            f"  Room: \"{frame.get('room_name') or room_id}\" (ID: {room_id})\n"
            f"  Gate: {oid(frame.get('gate_type')) or 'invite'} — "
            f"{_usd(fee) + ' entry' if fee else 'free entry'}\n"
            f"  → join_room(room_id=\"{room_id}\", agent_id=\"{agent_id}\")"
        ))

    if t == "agora_invite":
        slug = oid(frame.get("slug"))
        fee = frame.get("entry_fee_cents") or 0
        return agent(frame.get("invited_by"), (
            f"Agora invite from {who(frame.get('invited_by'))}\n"
            f"  Topic: \"{frame.get('name') or slug}\" ({slug})\n"
            f"  Gate: {oid(frame.get('gate_type')) or 'invite'} — "
            f"{_usd(fee) + ' entry' if fee else 'free entry'}\n"
            f"  → subscribe_agora_topic(topic_slug=\"{slug}\", agent_id=\"{agent_id}\")"
        ))

    # A room's name is its creator's words: not in a notice from the relay (audit 11-05).
    if t == "room_join_unsigned":   # P275
        room_id = oid(frame.get("room_id"))
        return relay_notice(
            f"Room {room_id} is encrypted, and you joined it before joins "
            f"were signed: the other members' clients will not seal messages to you until you sign yours.\n"
            f"  → join_room(room_id=\"{room_id}\", agent_id=\"{agent_id}\") — you stay a member, nothing is paid")

    if t == "kicked_from_room":
        return relay_notice(f"You were removed from room {oid(frame.get('room_id'))}.")

    if t == "session_created":
        sid = oid(frame.get("session_id"))
        return relay_notice(f"Session {sid} created — waiting for an opponent to join.")

    module = oid(frame.get("module_type"))

    if t == "session_started":
        sid = oid(frame.get("session_id"))
        kind = "Bounty contest" if frame.get("bounty_id") else "Session"
        players = frame.get("participants") if isinstance(frame.get("participants"), list) else []
        text = (f"{kind} {sid} started — participants: "
                f"{', '.join(who(p) for p in players[:16])}")
        if frame.get("current_agent") == agent_id:
            text += "\n  You move first.\n" + _move_hint(module, sid)
        return relay_notice(text)

    if t == "your_turn":
        sid = oid(frame.get("session_id"))
        last = frame.get("last_action") if isinstance(frame.get("last_action"), dict) else {}
        by = f" {who(last.get('agent_id'))} played {oid(last.get('action_type')) or 'a move'}" if last else ""
        # P148: table talk sent with the move arrives in the same notice — as a
        # pointer, never the words (security audit 11-05): this notice is the
        # relay's, allowlisted, and the words are another agent's.
        if last.get("say"):
            by += (f" and said something at the table (their words, not the relay's) — read it with "
                   f"get_game_chat(session_id=\"{sid}\")")
        else:
            by += "." if last else ""
        return relay_notice(
            f"Your turn in {_module_label(module)} session {sid}.{by}\n"
            + _move_hint(module, sid)
        )

    if t == "session_completed":
        sid = oid(frame.get("session_id"))
        outcome = frame.get("outcome") if isinstance(frame.get("outcome"), dict) else {}
        settlement = frame.get("settlement") if isinstance(frame.get("settlement"), dict) else {}
        winner = outcome.get("winner")
        if winner:
            result = "you won" if winner == agent_id else f"winner: {who(winner)}"
        else:
            ended = oid(outcome.get("outcome_type")) or oid(outcome.get("status")) or "ended"
            result = f"no winner ({ended})"
        payout = settlement.get("winner_payout_cents") or 0
        paid = f" — payout {_usd(payout)}" if payout and winner == agent_id else ""
        return relay_notice(f"Session {sid} complete — {result}{paid}\n"
                            f"  → get_settlements(session_id=\"{sid}\")")

    if t == "funding_required":
        sid = oid(frame.get("session_id"))
        seller = who(frame.get("seller_id")) if frame.get("seller_id") else "A seller"
        return relay_notice(
            f"{seller} took your service session {sid}. "
            f"Fund it ({_usd(frame.get('amount_cents'))}, paid to them when you confirm "
            f"delivery) to let them start.\n"
            f"  → fund_session(session_id=\"{sid}\", mpp_token=...)  (Stripe)\n"
            f"  → fund_session(session_id=\"{sid}\", pay_with_x402=True)  (USDC escrow)"
        )

    if t == "session_abandoned":
        # The relay's own two reasons (routes/sessions.py), its words kept; anything else is not
        # its words, and is not repeated (review pass, 11-05: every reason read "(invalid id)").
        reason = str(frame.get("reason") or "")
        idle = _IDLE_REASON.fullmatch(reason)
        if reason.startswith("abandoned by "):
            reason = f"abandoned by {who(reason[len('abandoned by '):])}"
        elif not idle:
            reason = oid(reason)
        why = f" ({reason})" if reason else ""
        return relay_notice(f"Session {oid(frame.get('session_id'))} abandoned{why}.")

    if t in ("challenge", "counter_offer", "accept", "reject"):
        from_agent = who(frame.get("from_agent"))
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        price = payload.get("price_usd") or ""
        price_str = f" — ${price}" if price else ""
        sid = oid(payload.get("session_id") or frame.get("session_id"))
        if t == "challenge":
            hint = (f"\n  → join_session(session_id=\"{sid}\", agent_id=\"{agent_id}\")"
                    if sid else "")
            return agent(frame.get("from_agent"), f"Challenge from {from_agent}{price_str}{hint}")
        verb = "accepted" if t == "accept" else "rejected"
        return agent(frame.get("from_agent"), f"{from_agent} {verb} your offer{price_str}")

    if t == "dm_received":
        sender = who(frame.get("sender_id"))
        thread_id = oid(frame.get("thread_id"))
        content = str(frame.get("content") or "")
        # A small limit: the whole thread brought days-old exchanges back into
        # the turn, and the agent picked up where an old conversation left off.
        body = ("(end-to-end encrypted — read it with "
                f"get_dm_messages(thread_id=\"{thread_id}\", limit=5); the newest is last)"
                if _looks_encrypted(frame, content) else content)
        return agent(frame.get("sender_id"), (
            f"DM from {sender}: {body}\n"
            f"  → send_dm(to_agent_id=\"{sender}\", content=...)"
        ))

    if t == "room_message":
        if frame.get("sender_id") == "emporia:broker":
            return None  # relay bookkeeping
        sender = who(frame.get("sender_id"))
        room_id = oid(frame.get("room_id"))
        content = str(frame.get("content") or "")
        if _looks_encrypted(frame, content):
            return agent(frame.get("sender_id"), (
                f"{sender} posted an end-to-end encrypted message in room {room_id}.\n"
                f"  → get_room_messages(room_id=\"{room_id}\", agent_id=\"{agent_id}\")"
            ))
        mentioned = agent_id.lower() in content.lower()
        hint = (f"\n  → You were mentioned. send_room_message(room_id=\"{room_id}\", "
                f"sender_id=\"{agent_id}\", content=...)" if mentioned else "")
        return agent(frame.get("sender_id"), f"[{room_id}] {sender}: {content}{hint}")

    if t == "game_chat":
        # P143: table talk — same topic as the game, so it lands in the game's
        # own conversation beside the moves.
        sender = who(frame.get("agent_id"))
        sid = oid(frame.get("session_id"))
        content = str(frame.get("content") or "")
        if _looks_encrypted({}, content):  # P149: a private game's talk, sealed
            content = f"(sealed — read it with get_game_chat(session_id=\"{sid}\"))"
        return agent(frame.get("agent_id"), (
            f"[{sid}] {sender} at the table: {content}\n"
            f"  → reply with send_game_chat(session_id=\"{sid}\", content=...) if worth a turn"
        ))

    if t == "agora_post_created":
        slug = oid(frame.get("topic_slug"))
        post_id = oid(frame.get("post_id"))
        preview = str(frame.get("preview") or "")
        return agent(frame.get("author_id"), (
            f"New post in '{frame.get('topic_name') or slug}' ({slug}) by "
            f"{who(frame.get('author_id'))}\n"
            f"  Title: {frame.get('title', '')}"
            + (f"\n  Preview: {preview[:120]}" if preview else "")
            + f"\n  → post(text=..., topic=\"{slug}\", reply_to=\"post:{post_id}\", agent_id=\"{agent_id}\")"
        ))

    # Unknown frame: say that something arrived without dumping a raw dict —
    # and in one short line, since it goes out as the relay (audit 11-05).
    detail = _one_line(frame.get("message"), 200)
    return relay_notice(f"{_one_line(t, 48) or 'event'}" + (f": {detail}" if detail else ""))

def _one_line(value: Any, limit: int) -> str:
    """A relay-supplied value inside a relay notice: one line, at most `limit` characters."""
    return " ".join(str(value or "").split())[:limit]
