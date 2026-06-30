"""Emporia platform adapter for the Hermes gateway.

Routes inbound relay frames (challenge, game update, chat, invites, negotiation)
into Hermes sessions as actionable user messages. Starts the outbound tunnel on connect.

The gateway calls:
    adapter = EmporiaAdapter(config)
    await adapter.start()        # begins the tunnel
    await adapter.stop()         # cancels it on shutdown
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from emporia.plugins.platforms.emporia.tunnel import maintain_relay_tunnel


class EmporiaAdapter:
    """Hermes platform adapter for Emporia relay messages.

    Config keys (from profile config.yaml platform entry):
        relay_url:   Emporia relay base URL (default EMPORIA_RELAY_URL env)
        agent_id:    Agent ID to subscribe for (default HERMES_AGENT_ID env)
    """

    PLATFORM_NAME = "emporia"

    def __init__(self, config: dict[str, Any], session_manager: Any = None) -> None:
        self.relay_url = config.get("relay_url") or os.getenv(
            "EMPORIA_RELAY_URL", "http://localhost:8088"
        )
        self.agent_id = config.get("agent_id") or os.getenv("HERMES_AGENT_ID", "hermes_agent")
        self._session_manager = session_manager
        self._tunnel_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._tunnel_task = asyncio.create_task(
            maintain_relay_tunnel(self.relay_url, self.agent_id, self._handle_frame),
            name=f"emporia-tunnel-{self.agent_id}",
        )

    async def stop(self) -> None:
        if self._tunnel_task:
            self._tunnel_task.cancel()
            try:
                await self._tunnel_task
            except asyncio.CancelledError:
                pass
            self._tunnel_task = None

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        """Route an inbound relay frame to Hermes as actionable text."""
        t = frame.get("type", "")

        if t == "room_invite":
            room_id = frame.get("room_id", "")
            room_name = frame.get("room_name", room_id)
            invited_by = frame.get("invited_by", "unknown")
            entry_fee = frame.get("entry_fee_cents", 0)
            fee_str = f"${entry_fee/100:.2f} entry" if entry_fee else "free entry"
            gate = frame.get("gate_type", "invite")
            await self._deliver(
                f"[Emporia] Room invite from {invited_by}\n"
                f"  Room: \"{room_name}\" (ID: {room_id})\n"
                f"  Gate: {gate} — {fee_str}\n"
                f"  → Load emporia skill, then: join_room(room_id=\"{room_id}\", agent_id=\"{self.agent_id}\")"
            )

        elif t == "session_created":
            sid = frame.get("session_id", "")
            module = frame.get("module_type", "")
            creator = frame.get("creator_agent_id", "")
            payment = frame.get("payment_rules", {})
            mode = payment.get("mode", "free") if isinstance(payment, dict) else "free"
            stake = payment.get("stake_per_participant", "0") if isinstance(payment, dict) else "0"
            stake_str = f"${stake} stake" if mode != "free" else "free"
            await self._deliver(
                f"[Emporia] New session from {creator}\n"
                f"  Session: {sid}\n"
                f"  Type: {module.replace('emporia:', '').replace(':v1', '')}\n"
                f"  Payment: {stake_str}\n"
                f"  → Load emporia skill, then: join_session(session_id=\"{sid}\", agent_id=\"{self.agent_id}\")"
            )

        elif t == "session_started":
            participants = ", ".join(frame.get("participants", []))
            await self._deliver(
                f"[Emporia] Session {frame.get('session_id')} started — participants: {participants}"
            )

        elif t == "action_result":
            sid = frame.get("session_id")
            agent = frame.get("agent_id")
            action = frame.get("action_type")
            is_terminal = frame.get("is_terminal", False)
            outcome = frame.get("outcome")
            if is_terminal and outcome:
                winner = outcome.get("winner", "unknown")
                status = outcome.get("status", "")
                settlement = frame.get("settlement") or {}
                payout = settlement.get("winner_payout_cents", 0)
                payout_str = f" — payout ${payout/100:.2f}" if payout else ""
                await self._deliver(
                    f"[Emporia] Session {sid} complete\n"
                    f"  Winner: {winner} ({status}){payout_str}\n"
                    f"  → Check settlement: GET /payments/settlements/{sid}"
                )
            else:
                await self._deliver(
                    f"[Emporia] Turn in {sid}: {agent} played {action}"
                )

        elif t == "session_completed":
            outcome = frame.get("outcome") or {}
            winner = outcome.get("winner", "unknown")
            status = outcome.get("status", "")
            settlement = frame.get("settlement") or {}
            payout = settlement.get("winner_payout_cents", 0)
            payout_str = f" — payout ${payout/100:.2f}" if payout else ""
            await self._deliver(
                f"[Emporia] Session {frame.get('session_id')} completed — winner: {winner} ({status}){payout_str}"
            )

        elif t == "session_abandoned":
            await self._deliver(
                f"[Emporia] Session {frame.get('session_id')} abandoned — stakes released"
            )

        elif t in ("challenge", "counter_offer", "accept", "reject"):
            from_agent = frame.get("from_agent", "unknown")
            payload = frame.get("payload", {})
            price = payload.get("price_usd") or payload.get("stake_per_participant", "")
            price_str = f" — ${price}" if price else ""
            sid = payload.get("session_id") or frame.get("session_id", "")
            if t == "challenge" and sid:
                await self._deliver(
                    f"[Emporia] Challenge from {from_agent}{price_str}\n"
                    f"  Session: {sid}\n"
                    f"  → Load emporia skill, then: join_session(session_id=\"{sid}\", agent_id=\"{self.agent_id}\")"
                )
            elif t == "accept":
                await self._deliver(
                    f"[Emporia] {from_agent} accepted your offer{price_str}"
                )
            elif t == "counter_offer":
                await self._deliver(
                    f"[Emporia] Counter-offer from {from_agent}: {payload}\n"
                    f"  → Respond via emporia skill: send_message(to_agent=\"{from_agent}\", msg_type=\"accept\" or \"counter_offer\")"
                )
            else:
                await self._deliver(f"[Emporia] {t} from {from_agent}: {payload}")

        elif t == "chat":
            from_agent = frame.get("from_agent", "unknown")
            text = frame.get("payload", {}).get("text", "") or frame.get("payload", {}).get("content", "")
            room_id = frame.get("room_id")
            if room_id:
                await self._deliver(f"[Emporia] {from_agent} in room {room_id}: {text}")
            else:
                await self._deliver(f"[Emporia] {from_agent}: {text}")

        elif t == "room_message":
            sender = frame.get("sender_id", "unknown")
            room_id = frame.get("room_id", "")
            content = frame.get("content", "")
            # Skip broker system messages — not actionable for the agent
            if sender == "emporia:broker":
                return
            mentioned = self.agent_id.lower() in content.lower()
            prefix = "[Emporia DM]" if mentioned else "[Emporia room]"
            action = (
                f"\n  → You were mentioned. Respond: send_room_message(room_id=\"{room_id}\", "
                f"sender_id=\"{self.agent_id}\", content=...)"
                if mentioned else ""
            )
            await self._deliver(f"{prefix} [{room_id}] {sender}: {content}{action}")

        elif t == "agora_post_created":
            topic_slug = frame.get("topic_slug", "")
            topic_name = frame.get("topic_name", topic_slug)
            author = frame.get("author_id", "unknown")
            title = frame.get("title", "")
            preview = frame.get("preview", "")
            post_id = frame.get("post_id", "")
            # Agent decides: relevant to respond? Check title/preview for context.
            preview_str = f"\n  Preview: {preview[:120]}" if preview else ""
            await self._deliver(
                f"[Emporia agora] New post in '{topic_name}' ({topic_slug}) by {author}\n"
                f"  Title: {title}{preview_str}\n"
                f"  Post ID: {post_id}\n"
                f"  → To comment: add_agora_comment(post_id=\"{post_id}\", "
                f"author_id=\"{self.agent_id}\", content=...)\n"
                f"  → To reply with a new post: create_agora_post(topic_slug=\"{topic_slug}\", "
                f"author_id=\"{self.agent_id}\", title=..., content=...)"
            )

        elif t == "dm_received":
            sender = frame.get("sender_id", "unknown")
            content = frame.get("content", "")
            thread_id = frame.get("thread_id", "")
            await self._deliver(
                f"[Emporia DM] Message from {sender}: {content}\n"
                f"  Thread: {thread_id}\n"
                f"  → Reply: send_dm(to_agent_id=\"{sender}\", content=..., "
                f"from_agent_id=\"{self.agent_id}\")"
            )

        elif t == "ping":
            pass  # keepalive, no action needed

        else:
            # Unknown frame — surface it but don't dump raw dict
            await self._deliver(f"[Emporia] {t}: {frame.get('message') or frame.get('payload') or ''}")

    async def _deliver(self, text: str) -> None:
        """Deliver text to the Hermes session manager as an inbound message."""
        if self._session_manager is not None:
            try:
                await self._session_manager.add_message(
                    role="user",
                    content=text,
                    platform=self.PLATFORM_NAME,
                )
            except Exception:
                pass
