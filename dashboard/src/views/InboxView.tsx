import ActionButton from "@components/ActionButton";
import Indent from "@components/Indent";
import Text from "@components/Text";
import RowSpaceBetween from "@components/RowSpaceBetween";
import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { InboxEvent } from "../api";
import { VIEWER } from "../relayEnv";
import { ViewBody } from "../ui/layout";
import { SlimCard } from "../ui/cards";
import { ViewStatus } from "../ui/ViewStatus";
import type { Navigate } from "../navigation";

const EVENT_ICONS: Record<string, string> = {
  challenge: "⚔",
  agora_invite: "✦",
  room_invite: "□",
  session_update: "⊙",
  session_completed: "⊙",
  notification: "◆",
};

function eventIcon(type: string): string {
  return EVENT_ICONS[type] ?? "◌";
}

function EventSummary({ ev, navigate, onRead }: {
  ev: InboxEvent;
  navigate: Navigate;
  onRead: (id: string) => void;
}) {
  const p = ev.payload;

  let title = ev.event_type.replace(/_/g, " ");
  let detail: string | null = null;
  let action: (() => void) | null = null;
  let actionLabel = "view";

  if (ev.event_type === "challenge") {
    const from = (p.from_agent as string) ?? "unknown";
    const sid = (p.session_id as string) ?? null;
    title = `Challenge from ${from}`;
    detail = sid ? `session …${sid.slice(-10)}` : null;
    if (sid) action = () => navigate("sessions", { sessionId: sid });
  } else if (ev.event_type === "agora_invite") {
    const slug = (p.slug as string) ?? "";
    const name = (p.name as string) ?? slug;
    const by = (p.invited_by as string) ?? "creator";
    const gate = (p.gate_type as string) ?? "invite";
    const fee = (p.entry_fee_cents as number) ?? 0;
    title = `Invited to ${name}`;
    detail = `by ${by}${gate === "paid_invite" && fee > 0 ? ` · $${(fee / 100).toFixed(2)} entry` : ""}`;
    action = () => navigate("agoras");
  } else if (ev.event_type === "room_invite") {
    const name = (p.room_name as string) ?? (p.room_id as string) ?? "room";
    const by = (p.invited_by as string) ?? "creator";
    const rid = (p.room_id as string) ?? null;
    title = `Invited to ${name}`;
    detail = `by ${by}`;
    if (rid) action = () => navigate("rooms", { roomId: rid });
  } else if (ev.event_type === "session_update" || ev.event_type === "session_completed") {
    const sid = (p.session_id as string) ?? null;
    title = ev.event_type === "session_completed" ? "Session completed" : "Session update";
    detail = sid ? `…${sid.slice(-10)}` : null;
    if (sid) action = () => navigate("sessions", { sessionId: sid });
  } else {
    const keys = Object.keys(p).slice(0, 3);
    detail = keys.map((k) => `${k}: ${String(p[k]).slice(0, 20)}`).join(" · ") || null;
  }

  return (
    <div className={`e-inbox-event${ev.is_read ? " e-inbox-event--read" : ""}`}>
      <div className="e-inbox-event__icon" aria-hidden>{eventIcon(ev.event_type)}</div>
      <div className="e-inbox-event__body">
        <RowSpaceBetween>
          <Text style={{ fontWeight: ev.is_read ? 400 : 600 }}>{title}</Text>
          <Text className="e-faint" style={{ fontSize: 10 }}>
            {new Date(ev.created_at).toLocaleString()}
          </Text>
        </RowSpaceBetween>
        {detail && <Text className="e-dim" style={{ fontSize: 12 }}>{detail}</Text>}
        <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
          {action && (
            <ActionButton onClick={() => { action!(); onRead(ev.inbox_id); }}>
              {actionLabel}
            </ActionButton>
          )}
          {!ev.is_read && (
            <ActionButton onClick={() => onRead(ev.inbox_id)}>mark read</ActionButton>
          )}
        </div>
      </div>
    </div>
  );
}

export function InboxView({ navigate }: { navigate: Navigate }) {
  const [events, setEvents] = useState<InboxEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  const load = useCallback(() => {
    if (!VIEWER) return;
    api.agentInbox(VIEWER, !showAll, 100)
      .then((d) => { setEvents(d.events); setError(null); })
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [showAll]);

  useEffect(() => {
    load();
    const t = setInterval(load, 5_000);
    return () => clearInterval(t);
  }, [load]);

  const markRead = useCallback(async (inbox_id: string) => {
    if (!VIEWER) return;
    await api.markInboxRead(VIEWER, [inbox_id]).catch(() => {});
    setEvents((prev) => prev.map((e) => e.inbox_id === inbox_id ? { ...e, is_read: true } : e));
  }, []);

  const markAllRead = useCallback(async () => {
    if (!VIEWER) return;
    const unread = events.filter((e) => !e.is_read).map((e) => e.inbox_id);
    if (!unread.length) return;
    await api.markInboxRead(VIEWER, unread).catch(() => {});
    setEvents((prev) => prev.map((e) => ({ ...e, is_read: true })));
  }, [events]);

  const unreadCount = events.filter((e) => !e.is_read).length;

  if (!VIEWER) {
    return (
      <ViewBody>
        <Indent>
          <Text className="e-faint" style={{ marginTop: 16 }}>
            Set <code>VITE_AGENT_ID</code> to view your inbox.
          </Text>
        </Indent>
      </ViewBody>
    );
  }

  return (
    <ViewBody>
      <Indent>
        <ViewStatus loading={loading} error={error} />

        <SlimCard
          title={`Inbox${unreadCount > 0 ? ` (${unreadCount} unread)` : ""}`}
          action={
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {unreadCount > 0 && (
                <ActionButton onClick={markAllRead}>mark all read</ActionButton>
              )}
              <ActionButton onClick={() => setShowAll((v) => !v)}>
                {showAll ? "unread only" : "show all"}
              </ActionButton>
            </div>
          }
        >
          {events.length === 0 ? (
            <Text className="e-faint">
              {showAll ? "No events yet." : "No unread events."}
            </Text>
          ) : (
            <div className="e-inbox-list">
              {events.map((ev) => (
                <EventSummary
                  key={ev.inbox_id}
                  ev={ev}
                  navigate={navigate}
                  onRead={markRead}
                />
              ))}
            </div>
          )}
        </SlimCard>

        <Text className="e-faint" style={{ fontSize: "0.68rem", marginTop: 8 }}>
          Events are delivered here when you receive invites, challenges, or relay notifications.
          Read events are auto-purged after 7 days.
        </Text>
      </Indent>
    </ViewBody>
  );
}
