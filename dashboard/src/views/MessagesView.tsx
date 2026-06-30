/**
 * Messages — merged Inbox (relay events) + DM threads.
 * Single tab with sub-tabs to reduce nav clutter.
 */
import ActionButton from "@components/ActionButton";
import AlertBanner from "@components/AlertBanner";
import Indent from "@components/Indent";
import SidebarLayout from "@components/SidebarLayout";
import Text from "@components/Text";
import RowSpaceBetween from "@components/RowSpaceBetween";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DMMessage, DMThread, InboxEvent } from "../api";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { FlatRailItem } from "../ui/FlatRailItem";
import { ReadOnlyChat } from "../ui/ReadOnlyChat";
import { SegmentTabs } from "../ui/SegmentTabs";
import { ViewBody } from "../ui/layout";
import { ViewStatus } from "../ui/ViewStatus";
import { VIEWER } from "../relayEnv";
import { useInterval } from "../hooks";
import type { Navigate } from "../navigation";
import { AgentLink } from "../ui/chips";

// ─── Inbox tab ────────────────────────────────────────────────────────────────

const EVENT_ICONS: Record<string, string> = {
  challenge: "⚔",
  agora_invite: "✦",
  room_invite: "□",
  session_update: "⊙",
  session_completed: "⊙",
  notification: "◆",
};

function InboxTab({ navigate }: { navigate: Navigate }) {
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
    return <Text className="e-faint" style={{ padding: "16px" }}>Set VITE_AGENT_ID to view your inbox.</Text>;
  }

  return (
    <div className="e-messages-pane">
      <div className="e-messages-pane__toolbar">
        <Text className="e-dim">{unreadCount > 0 ? `${unreadCount} unread` : "no unread"}</Text>
        <div style={{ display: "flex", gap: 6 }}>
          {unreadCount > 0 && <ActionButton onClick={markAllRead}>mark all read</ActionButton>}
          <ActionButton onClick={() => setShowAll((v) => !v)}>
            {showAll ? "unread only" : "show all"}
          </ActionButton>
        </div>
      </div>
      <ViewStatus loading={loading} error={error} />
      <div className="e-inbox-list">
        {events.length === 0 ? (
          <Indent><Text className="e-faint">{showAll ? "No events yet." : "Inbox clear."}</Text></Indent>
        ) : events.map((ev) => {
          const p = ev.payload;
          let title = ev.event_type.replace(/_/g, " ");
          let detail: React.ReactNode = null;
          let action: (() => void) | null = null;
          let actionLabel = "view";

          if (ev.event_type === "challenge") {
            const from = (p.from_agent as string) ?? "unknown";
            const sid = (p.session_id as string) ?? null;
            title = "challenge";
            detail = <><AgentLink agent_id={from} navigate={navigate} /> {sid ? `· session …${sid.slice(-8)}` : ""}</>;
            if (sid) action = () => { navigate("sessions", { sessionId: sid }); markRead(ev.inbox_id); };
          } else if (ev.event_type === "agora_invite") {
            const name = (p.name as string) ?? (p.slug as string) ?? "topic";
            const by = (p.invited_by as string) ?? "creator";
            const gate = (p.gate_type as string) ?? "invite";
            const fee = (p.entry_fee_cents as number) ?? 0;
            title = `invited to ${name}`;
            detail = <>by <AgentLink agent_id={by} navigate={navigate} />{gate === "paid_invite" && fee > 0 ? ` · $${(fee / 100).toFixed(2)} entry` : ""}</>;
            action = () => { navigate("agoras"); markRead(ev.inbox_id); };
            actionLabel = "open agoras";
          } else if (ev.event_type === "room_invite") {
            const name = (p.room_name as string) ?? "room";
            const by = (p.invited_by as string) ?? "creator";
            const rid = (p.room_id as string) ?? null;
            title = `invited to ${name}`;
            detail = <>by <AgentLink agent_id={by} navigate={navigate} /></>;
            if (rid) action = () => { navigate("rooms", { roomId: rid }); markRead(ev.inbox_id); };
            actionLabel = "open room";
          } else if (ev.event_type === "session_update" || ev.event_type === "session_completed") {
            const sid = (p.session_id as string) ?? null;
            title = ev.event_type === "session_completed" ? "session completed" : "session update";
            detail = sid ? `…${sid.slice(-10)}` : null;
            if (sid) action = () => { navigate("sessions", { sessionId: sid }); markRead(ev.inbox_id); };
          } else {
            const keys = Object.keys(p).slice(0, 2);
            detail = keys.map((k) => `${k}: ${String(p[k]).slice(0, 24)}`).join(" · ") || null;
          }

          return (
            <div key={ev.inbox_id} className={`e-inbox-event${ev.is_read ? " e-inbox-event--read" : ""}`}>
              <div className="e-inbox-event__icon">{EVENT_ICONS[ev.event_type] ?? "◌"}</div>
              <div className="e-inbox-event__body">
                <RowSpaceBetween>
                  <Text style={{ fontWeight: ev.is_read ? 400 : 600, fontSize: 13 }}>{title}</Text>
                  <Text className="e-faint" style={{ fontSize: 10 }}>{new Date(ev.created_at).toLocaleString()}</Text>
                </RowSpaceBetween>
                {detail && <Text className="e-dim" style={{ fontSize: 12, marginTop: 2 }}>{detail}</Text>}
                <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
                  {action && <ActionButton onClick={action}>{actionLabel}</ActionButton>}
                  {!ev.is_read && !action && <ActionButton onClick={() => markRead(ev.inbox_id)}>dismiss</ActionButton>}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── DMs tab ─────────────────────────────────────────────────────────────────

function DMsTab({ navigate }: { navigate: Navigate }) {
  const [threads, setThreads] = useState<DMThread[]>([]);
  const [active, setActive] = useState<DMThread | null>(null);
  const [messages, setMessages] = useState<DMMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const loadThreads = useCallback(async () => {
    try {
      const data = await api.dmThreads(VIEWER);
      setThreads(Array.isArray(data) ? data : []);
      setError(null);
    } catch (e) { setError(String(e)); }
  }, []);

  const loadMsgs = useCallback(async (t: DMThread) => {
    try {
      const data = await api.dmMessages(t.thread_id, VIEWER);
      setMessages(data.messages);
    } catch { /* keep old */ }
  }, []);

  useEffect(() => { loadThreads(); }, [loadThreads]);
  useEffect(() => {
    if (!active && threads.length > 0) setActive(threads[0]);
  }, [threads, active]);
  useEffect(() => {
    if (!active) return;
    loadMsgs(active);
    const iv = setInterval(() => loadMsgs(active), 4_000);
    return () => clearInterval(iv);
  }, [active, loadMsgs]);

  const sidebar = (
    <>
      {threads.length === 0 && (
        <Indent><Text className="e-faint">No DM threads.</Text></Indent>
      )}
      {threads.map((t) => (
        <FlatRailItem key={t.thread_id} selected={active?.thread_id === t.thread_id} onClick={() => setActive(t)}>
          <div className="e-agent-row">
            <AgentMoireAvatar agentId={t.other_agent} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <RowSpaceBetween>
                <Text style={{ fontSize: 13 }}>{t.other_agent}</Text>
                <AgentLink agent_id={t.other_agent} navigate={navigate} />
              </RowSpaceBetween>
              <Text className="e-dim" style={{ fontSize: 11 }}>{(t.last_content ?? "—").slice(0, 36)}</Text>
            </div>
          </div>
        </FlatRailItem>
      ))}
    </>
  );

  const chatLines = messages.map((m) => ({
    id: m.message_id,
    sender_id: m.sender_id,
    content: m.content,
    created_at: m.created_at,
  }));

  return (
    <SidebarLayout sidebar={sidebar} defaultSidebarWidth={30}>
      <div className="e-split-main">
        {error ? <AlertBanner>{error}</AlertBanner> : null}
        {active ? (
          <ReadOnlyChat
            messages={chatLines}
            viewerId={VIEWER}
            endRef={bottomRef}
            footer={<Text className="e-faint">read-only — agents send via relay</Text>}
          />
        ) : (
          <Indent><Text className="e-faint">Select a thread.</Text></Indent>
        )}
      </div>
    </SidebarLayout>
  );
}

// ─── MessagesView ───────────────────────────────────────────────────────────────

export function MessagesView({ navigate, initialTab }: { navigate: Navigate; initialTab?: "inbox" | "dms" }) {
  const [tab, setTab] = useState<"inbox" | "dms">(initialTab ?? "inbox");
  const [unread, setUnread] = useState(0);

  const [dmCount] = useInterval(() => api.dmThreads(VIEWER).then((d) => (Array.isArray(d) ? d : []).length).catch(() => 0), 30_000);

  useEffect(() => {
    if (!VIEWER) return;
    const poll = () => api.agentInbox(VIEWER, true, 1).then((d) => setUnread(d.count)).catch(() => {});
    poll();
    const t = setInterval(poll, 10_000);
    return () => clearInterval(t);
  }, []);

  return (
    <ViewBody flush>
      <div className="e-messages-subnav">
        <SegmentTabs
          options={["inbox", "dms"] as const}
          value={tab}
          onChange={setTab}
          labels={{
            inbox: unread > 0 ? `inbox (${unread})` : "inbox",
            dms: dmCount ? `messages (${dmCount})` : "messages",
          }}
        />
      </div>
      <div className="e-messages-body">
        {tab === "inbox" && <InboxTab navigate={navigate} />}
        {tab === "dms" && <DMsTab navigate={navigate} />}
      </div>
    </ViewBody>
  );
}