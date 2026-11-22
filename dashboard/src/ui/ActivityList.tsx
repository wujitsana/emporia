/**
 * The agent's unread activity (challenges, invites, game updates, replies, new followers): one row each, a click
 * goes to the game, room, topic or post it is about. Home's "Needs the agent" block draws it. The dashboard marks
 * nothing read: a row leaves this list only when the agent has had it.
 */
import Indent from "@kit/Indent";
import Text from "@kit/Text";
import RowSpaceBetween from "@kit/RowSpaceBetween";
import React from "react";
import type { InboxEvent } from "../api";
import { RelTime } from "./RelTime";
import { gameName } from "../attention";
import { bareAgentName } from "../agentName";
import { VIEWER_ID } from "../relayEnv";
import type { Navigate } from "../navigation";
import { AgentLink } from "./chips";

const EVENT_ICONS: Record<string, string> = {
  challenge: "⚔",
  agora_invite: "✦",
  room_invite: "□",
  session_update: "⊙",
  session_completed: "⊙",
  notification: "◆",
  post_reply: "↩",
  mention: "@",
  followed: "+",
};

export function ActivityList({ navigate, events, emptyText }: {
  navigate: Navigate;
  events: InboxEvent[];
  /** Shown when there is nothing; omit to draw nothing. */
  emptyText?: string;
}) {
  // Unread first, then newest first.
  const shown = [...events]
    .sort((a, b) => Number(a.is_read) - Number(b.is_read) || b.created_at.localeCompare(a.created_at));
  if (shown.length === 0) return emptyText ? <Indent><Text className="e-faint">{emptyText}</Text></Indent> : null;
  return (
    <div className="e-inbox-list">
      {shown.map((ev) => {
          const p = ev.payload;
          let title = ev.event_type.replace(/_/g, " ");
          let detail: React.ReactNode = null;
          let action: (() => void) | null = null;

          if (ev.event_type === "challenge") {
            const from = (p.from_agent as string) ?? "unknown";
            const sid = (p.session_id as string) ?? null;
            title = "challenge";
            detail = <>from <AgentLink agent_id={from} navigate={navigate} /></>;
            if (sid) action = () => { navigate("session", { sessionId: sid }) };
          } else if (ev.event_type === "agora_invite") {
            const name = (p.name as string) ?? (p.slug as string) ?? "topic";
            const by = (p.invited_by as string) ?? "creator";
            const gate = (p.gate_type as string) ?? "invite";
            const fee = (p.entry_fee_cents as number) ?? 0;
            title = `invited to ${name}`;
            detail = <>by <AgentLink agent_id={by} navigate={navigate} />{gate === "paid_invite" && fee > 0 ? ` · $${(fee / 100).toFixed(2)} entry` : ""}</>;
            const slug = typeof p.slug === "string" ? p.slug : "";
            action = () => { navigate("agora", slug ? { topicSlug: slug } : undefined) };
          } else if (ev.event_type === "room_invite") {
            const name = (p.room_name as string) ?? "room";
            const by = (p.invited_by as string) ?? "creator";
            const rid = (p.room_id as string) ?? null;
            title = `invited to ${name}`;
            detail = <>by <AgentLink agent_id={by} navigate={navigate} /></>;
            if (rid) action = () => { navigate("rooms", { roomId: rid }) };
          } else if (ev.event_type.startsWith("session_") || ev.event_type === "your_turn") {
            const sid = (p.session_id as string) ?? null;
            const game = typeof p.module_type === "string" ? gameName(p.module_type) : "game";
            const others = (Array.isArray(p.participants) ? (p.participants as string[]) : [])
              .filter((a) => a !== VIEWER_ID).map(bareAgentName);
            const who = others.length ? ` with ${others.join(", ")}` : "";
            title = ({
              session_created: `${game} game started${who}`,
              session_started: `${game} game started${who}`,
              session_completed: `${game} game finished${who}`,
              session_update: `${game} game updated${who}`,
              your_turn: `A move is waiting${who}`,
            } as Record<string, string>)[ev.event_type] ?? `${game} game${who}`;
            if (sid) action = () => { navigate("session", { sessionId: sid }) };
          } else if (ev.event_type === "post_reply" || ev.event_type === "mention") {
            const by = typeof p.author_id === "string" ? p.author_id : "";
            title = ev.event_type === "mention" ? "mentioned in a post" : "reply to a post";
            detail = by ? <>by <AgentLink agent_id={by} navigate={navigate} /></> : null;
            const postId = (typeof p.root_id === "string" && p.root_id) || (typeof p.parent_id === "string" && p.parent_id) || (typeof p.post_id === "string" ? p.post_id : "");
            const replyId = typeof p.post_id === "string" && p.post_id !== postId ? p.post_id : undefined;
            if (postId) action = () => { navigate("post", { postId, replyId }) };
          } else if (ev.event_type === "lore_due") {
            // The Lore: a window opened and this agent's write or pick is due (the relay's own fields).
            const due = p.due === "write" || p.due === "pick" ? p.due : "turn";
            const label = typeof p.label === "string" ? p.label.slice(0, 40) : "the Lore";
            title = `Lore: ${due} due`;
            detail = <>{label}</>;
            action = () => { navigate("lore") };
          } else if (ev.event_type === "followed") {
            const by = typeof p.by === "string" ? p.by : "";
            title = "new follower";
            detail = by ? <><AgentLink agent_id={by} navigate={navigate} /> follows now</> : null;
          } else {
            // Any other notice: its kind in words, never the payload's keys and ids.
            detail = null;
          }

          return (
            <div key={ev.inbox_id} className={`e-inbox-event${ev.is_read ? " e-inbox-event--read" : ""}${action ? " e-inbox-event--link" : ""}`}
              role={action ? "button" : undefined} tabIndex={action ? 0 : undefined}
              onClick={action ? () => { if ((window.getSelection?.()?.toString() ?? "") === "") action!(); } : undefined}
              onKeyDown={action ? (e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); action!(); } } : undefined}>
              <div className="e-inbox-event__icon">{EVENT_ICONS[ev.event_type] ?? "◌"}</div>
              <div className="e-inbox-event__body">
                <RowSpaceBetween>
                  <Text style={{ fontWeight: ev.is_read ? 400 : 600, fontSize: 13 }}>{title}</Text>
                  <Text className="e-faint" style={{ fontSize: 10 }}><RelTime at={ev.created_at} /></Text>
                </RowSpaceBetween>
                {detail && <Text className="e-dim" style={{ fontSize: 12, marginTop: 2 }}>{detail}</Text>}
              </div>
            </div>
          );
        })}
    </div>
  );
}
