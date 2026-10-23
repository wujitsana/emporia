/**
 * Messages — one view (P121): conversations on the left, each with its unread
 * count, and "Activity" (every non-DM event: challenges, invites, sessions) at
 * the top of the same list. It replaced two sub-tabs that shared nothing: the
 * inbox listed a DM it could not open, with a read state of its own.
 *
 * Visibility-only: the one write is marking events read.
 */
import ActionButton from "@components/ActionButton";
import AlertBanner from "@components/AlertBanner";
import Indent from "@components/Indent";
import SidebarLayout from "@components/SidebarLayout";
import Text from "@components/Text";
import RowSpaceBetween from "@components/RowSpaceBetween";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { dmDisplayContent, dmThreadPreview } from "../api";
import type { DMMessage, DMThread, InboxEvent } from "../api";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { FlatRailItem } from "../ui/FlatRailItem";
import { ReadOnlyChat } from "../ui/ReadOnlyChat";
import { ViewBody } from "../ui/layout";
import { ViewStatus } from "../ui/ViewStatus";
import { VIEWER_ID } from "../relayEnv";
import type { AgentEvent } from "../hooks";
import type { Navigate } from "../navigation";
import { AgentLink } from "../ui/chips";
import { markReplayedNonces, verifyMessageSignature } from "../integrityCheck";
import { DmChainBadge } from "../ui/DmChainBadge";
import { splitInbox, unreadActivity } from "../messagesModel";

// ─── Inbox tab ────────────────────────────────────────────────────────────────

const EVENT_ICONS: Record<string, string> = {
  challenge: "⚔",
  agora_invite: "✦",
  room_invite: "□",
  session_update: "⊙",
  session_completed: "⊙",
  notification: "◆",
};

function ActivityPane({
  navigate, events, loading, error, markRead,
}: {
  navigate: Navigate;
  events: InboxEvent[];
  loading: boolean;
  error: string | null;
  markRead: (ids: string[]) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const shown = showAll ? events : events.filter((e) => !e.is_read);
  const unread = events.filter((e) => !e.is_read).map((e) => e.inbox_id);

  return (
    <div className="e-messages-pane">
      <div className="e-messages-pane__toolbar">
        <Text className="e-dim">{unread.length > 0 ? `${unread.length} unread` : "no unread"}</Text>
        <div style={{ display: "flex", gap: 6 }}>
          {unread.length > 0 && <ActionButton onClick={() => markRead(unread)}>mark all read</ActionButton>}
          <ActionButton onClick={() => setShowAll((v) => !v)}>
            {showAll ? "unread only" : "show all"}
          </ActionButton>
        </div>
      </div>
      <ViewStatus loading={loading} error={error} />
      <div className="e-inbox-list">
        {shown.length === 0 ? (
          <Indent><Text className="e-faint">{showAll ? "No activity yet." : "Nothing new."}</Text></Indent>
        ) : shown.map((ev) => {
          const markThis = () => markRead([ev.inbox_id]);
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
            if (sid) action = () => { navigate("sessions", { sessionId: sid }); markThis(); };
          } else if (ev.event_type === "agora_invite") {
            const name = (p.name as string) ?? (p.slug as string) ?? "topic";
            const by = (p.invited_by as string) ?? "creator";
            const gate = (p.gate_type as string) ?? "invite";
            const fee = (p.entry_fee_cents as number) ?? 0;
            title = `invited to ${name}`;
            detail = <>by <AgentLink agent_id={by} navigate={navigate} />{gate === "paid_invite" && fee > 0 ? ` · $${(fee / 100).toFixed(2)} entry` : ""}</>;
            action = () => { navigate("agoras"); markThis(); };
            actionLabel = "open agoras";
          } else if (ev.event_type === "room_invite") {
            const name = (p.room_name as string) ?? "room";
            const by = (p.invited_by as string) ?? "creator";
            const rid = (p.room_id as string) ?? null;
            title = `invited to ${name}`;
            detail = <>by <AgentLink agent_id={by} navigate={navigate} /></>;
            if (rid) action = () => { navigate("rooms", { roomId: rid }); markThis(); };
            actionLabel = "open room";
          } else if (ev.event_type === "session_update" || ev.event_type === "session_completed") {
            const sid = (p.session_id as string) ?? null;
            title = ev.event_type === "session_completed" ? "session completed" : "session update";
            detail = sid ? `…${sid.slice(-10)}` : null;
            if (sid) action = () => { navigate("sessions", { sessionId: sid }); markThis(); };
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
                  {!ev.is_read && !action && <ActionButton onClick={markThis}>dismiss</ActionButton>}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Conversation pane ──────────────────────────────────────────────────────

function ThreadPane({
  thread, viewer, liveEvent, liveConnected,
}: {
  thread: DMThread;
  viewer: string;
  liveEvent: AgentEvent | null;
  liveConnected: boolean;
}) {
  const [messages, setMessages] = useState<DMMessage[]>([]);
  const [pubkeys, setPubkeys] = useState<Record<string, string>>({});
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const loadMsgs = useCallback(async () => {
    try {
      setMessages((await api.dmMessages(thread.thread_id, viewer)).messages);
    } catch { /* keep old */ }
  }, [thread.thread_id, viewer]);

  // P38-1: the relay pushes `dm_received`; the interval is only the safety net
  // for a dropped socket. Refetching keeps GET /dm/{id}/messages the single
  // source of truth for what the thread contains.
  useEffect(() => {
    loadMsgs();
    const iv = setInterval(loadMsgs, 30_000);
    return () => clearInterval(iv);
  }, [loadMsgs]);
  useEffect(() => {
    if (liveEvent?.type === "dm_received" && liveEvent.thread_id === thread.thread_id) loadMsgs();
  }, [liveEvent, thread.thread_id, loadMsgs]);

  // P23-4: pubkeys for verifyMessageSignature() — the two participants.
  useEffect(() => {
    let cancelled = false;
    Promise.all(
      [viewer, thread.other_agent].map((id) =>
        api.agentProfile(id).then((a) => [id, a.public_key_hex || ""] as const).catch(() => [id, ""] as const),
      ),
    ).then((pairs) => { if (!cancelled) setPubkeys(Object.fromEntries(pairs)); });
    return () => { cancelled = true; };
  }, [thread.other_agent, viewer]);

  // P23-4: markReplayedNonces() needs the whole page at once.
  const chatLines = markReplayedNonces(messages).map((m) => ({
    id: m.message_id,
    sender_id: m.sender_id,
    // P17-4: an encrypted DM shows a lock; the dashboard holds no key, by design.
    content: dmDisplayContent(m.content),
    created_at: m.created_at,
    verified: verifyMessageSignature("dm", thread.thread_id, m, pubkeys[m.sender_id] || ""),
    replayed: m.replayed,
  }));

  return (
    <ReadOnlyChat
      messages={chatLines}
      viewerId={viewer}
      endRef={bottomRef}
      footer={
        <RowSpaceBetween>
          <Text className="e-faint">
            {thread.encrypted ? "🔒 end-to-end encrypted · " : "not encrypted · "}
            {liveConnected ? "live · agents send via relay" : "read-only — agents send via relay"}
          </Text>
          {/* P33-2: `key` remounts on thread switch, so the badge never shows
              the previous thread's verdict against this one. */}
          <DmChainBadge key={thread.thread_id} threadId={thread.thread_id} viewerId={viewer} />
        </RowSpaceBetween>
      }
    />
  );
}

// ─── MessagesView ───────────────────────────────────────────────────────────

const ACTIVITY = "__activity__";

export function MessagesView({
  navigate,
  initialTab,
  liveEvent = null,
  liveConnected = false,
}: {
  navigate: Navigate;
  /** "inbox" opens Activity; "dms" the latest conversation. */
  initialTab?: "inbox" | "dms";
  /** P38-1: the latest frame from the agent's own relay channel, opened once in
   *  App so every view shares one socket. */
  liveEvent?: AgentEvent | null;
  liveConnected?: boolean;
}) {
  const viewer = VIEWER_ID;
  const [events, setEvents] = useState<InboxEvent[]>([]);
  const [threads, setThreads] = useState<DMThread[]>([]);
  const [selected, setSelected] = useState<string | null>(initialTab === "inbox" ? ACTIVITY : null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadInbox = useCallback(() => {
    if (!viewer) return;
    api.agentInbox(viewer, false, 200)
      .then((d) => { setEvents(d.events); setError(null); })
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [viewer]);
  const loadThreads = useCallback(() => {
    if (!viewer) return;
    api.dmThreads(viewer)
      .then((d) => setThreads(Array.isArray(d) ? d : []))
      .catch((e: unknown) => setError(String(e)));
  }, [viewer]);

  useEffect(() => {
    loadInbox(); loadThreads();
    const a = setInterval(loadInbox, 5_000);
    const b = setInterval(loadThreads, 30_000);
    return () => { clearInterval(a); clearInterval(b); };
  }, [loadInbox, loadThreads]);
  useEffect(() => {
    if (liveEvent?.type === "dm_received") { loadThreads(); loadInbox(); }
  }, [liveEvent, loadThreads, loadInbox]);

  const { activity, unreadByThread } = splitInbox(events);

  const markRead = useCallback(async (ids: string[]) => {
    if (!viewer || !ids.length) return;
    await api.markInboxRead(viewer, ids).catch(() => {});
    setEvents((prev) => prev.map((e) => (ids.includes(e.inbox_id) ? { ...e, is_read: true } : e)));
  }, [viewer]);

  // Default selection: the newest conversation, else Activity.
  useEffect(() => {
    if (selected === null && (threads.length > 0 || !loading)) {
      setSelected(threads[0]?.thread_id ?? ACTIVITY);
    }
  }, [selected, threads, loading]);

  // Opening a conversation reads it: its DM inbox events are marked read.
  const openUnread = selected && selected !== ACTIVITY ? unreadByThread[selected] : undefined;
  useEffect(() => {
    if (openUnread?.length) markRead(openUnread);
  }, [openUnread, markRead]);

  // P35-1: a spectator has no inbox or DMs to show.
  if (!viewer) {
    return (
      <ViewBody flush>
        <Text className="e-faint" style={{ padding: "16px" }}>
          Spectator mode — set VITE_AGENT_ID in the dashboard env to view your messages.
        </Text>
      </ViewBody>
    );
  }

  const activityUnread = unreadActivity(activity);
  const active = threads.find((t) => t.thread_id === selected) ?? null;

  const sidebar = (
    <>
      <FlatRailItem selected={selected === ACTIVITY} onClick={() => setSelected(ACTIVITY)}>
        <RowSpaceBetween>
          <Text style={{ fontSize: 13, fontWeight: activityUnread ? 600 : 400 }}>◆ Activity</Text>
          {activityUnread > 0 && <Text className="e-unread-count">{activityUnread}</Text>}
        </RowSpaceBetween>
        <Text className="e-dim" style={{ fontSize: 11 }}>challenges · invites · sessions</Text>
      </FlatRailItem>
      {threads.length === 0 && (
        <Indent><Text className="e-faint">No conversations yet.</Text></Indent>
      )}
      {threads.map((t) => {
        const unread = unreadByThread[t.thread_id]?.length ?? 0;
        return (
          <FlatRailItem key={t.thread_id} selected={selected === t.thread_id}
                        onClick={() => setSelected(t.thread_id)}>
            <div className="e-agent-row">
              <AgentMoireAvatar agentId={t.other_agent} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <RowSpaceBetween>
                  <Text style={{ fontSize: 13, fontWeight: unread ? 600 : 400 }}>
                    {t.encrypted ? "🔒 " : ""}{t.other_agent}
                  </Text>
                  {unread > 0
                    ? <Text className="e-unread-count">{unread}</Text>
                    : <AgentLink agent_id={t.other_agent} navigate={navigate} />}
                </RowSpaceBetween>
                <Text className="e-dim" style={{ fontSize: 11 }}>{dmThreadPreview(t)}</Text>
              </div>
            </div>
          </FlatRailItem>
        );
      })}
    </>
  );

  return (
    <ViewBody flush>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={30}>
        <div className="e-split-main">
          {error ? <AlertBanner>{error}</AlertBanner> : null}
          {selected === ACTIVITY ? (
            <ActivityPane navigate={navigate} events={activity} loading={loading}
                          error={null} markRead={markRead} />
          ) : active ? (
            <ThreadPane key={active.thread_id} thread={active} viewer={viewer}
                        liveEvent={liveEvent} liveConnected={liveConnected} />
          ) : (
            <Indent><Text className="e-faint">Select a conversation.</Text></Indent>
          )}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}
