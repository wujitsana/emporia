/**
 * Messages — conversations on the left, each with its unread count (P121). The
 * rest of the agent's inbox (challenges, invites, sessions) is not here: Home
 * shows it as "Needs the agent".
 *
 * Visibility-only: the one write is marking events read.
 */
import AlertBanner from "@kit/AlertBanner";
import Indent from "@kit/Indent";
import SidebarLayout from "@kit/SidebarLayout";
import Text from "@kit/Text";
import RowSpaceBetween from "@kit/RowSpaceBetween";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { dmDisplayContent, dmThreadPreview, looksLikeEncryptedEnvelope, postDisplay, postParts } from "../api";
import type { DMMessage, DMThread, InboxEvent } from "../api";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { bareAgentName } from "../agentName";
import { FlatRailItem } from "../ui/FlatRailItem";
import { ReadOnlyChat } from "../ui/ReadOnlyChat";
import { ViewBody } from "../ui/layout";
import { VIEWER_ID } from "../relayEnv";
import type { AgentEvent } from "../hooks";
import type { Navigate } from "../navigation";
import { AgentLink } from "../ui/chips";
import { markReplayedNonces, verifyMessageSignature } from "../integrityCheck";
import { DmChainBadge } from "../ui/DmChainBadge";
import { inBox, splitInbox } from "../messagesModel";
import { VIA_CONSOLE, consoleUrl, httpFetch } from "../transport";
import { type SenderKey, senderKeyFrom } from "../keyLog";

/** P126: sealed DMs opened by the local console, with the viewer's own key, on
 *  this machine. Directly against a relay there is no key, and they stay 🔒. */
async function openSealedDms(viewer: string, threadId: string, messages: DMMessage[]) {
  const r = await httpFetch(consoleUrl(`/dm/${encodeURIComponent(viewer)}/open`), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ thread_id: threadId, messages }),
  });
  if (!r.ok) throw new Error(`console ${r.status}`);
  const d = (await r.json()) as { opened?: Record<string, string | null> };
  return Object.fromEntries(Object.entries(d.opened ?? {}).filter(
    (e): e is [string, string] => typeof e[1] === "string"));
}
import type { ThreadBox } from "../messagesModel";
import { SegmentTabs } from "../ui/SegmentTabs";

// ─── Inbox tab ────────────────────────────────────────────────────────────────

// ─── Conversation pane ──────────────────────────────────────────────────────

export function ThreadPane({
  thread, viewer, liveEvent, liveConnected,
}: {
  thread: DMThread;
  viewer: string;
  liveEvent: AgentEvent | null;
  liveConnected: boolean;
}) {
  const [messages, setMessages] = useState<DMMessage[]>([]);
  const [opened, setOpened] = useState<Record<string, string>>({});
  const [readerError, setReaderError] = useState<string | null>(null);
  const [pubkeys, setPubkeys] = useState<Record<string, SenderKey>>({});
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const loadMsgs = useCallback(() => {
    api.dmMessages(thread.thread_id, viewer).then((r) => {
      const msgs = r.messages;
      setMessages(msgs);
      // P125: sealed messages, opened on this machine by the local reader.
      if (VIA_CONSOLE && msgs.some((m) => looksLikeEncryptedEnvelope(m.content))) {
        openSealedDms(viewer, thread.thread_id, msgs)
          .then((o) => { setOpened(o); setReaderError(null); })
          .catch((e: unknown) => setReaderError(String(e)));
      }
    }).catch(() => { /* keep old */ });
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
  const senders = [...new Set(messages.map((m) => m.sender_id))].sort().join("|");
  useEffect(() => {
    const lifetime = new AbortController();
    // P185: in a thread with another relay's agent, both sides sign as their
    // address (`alpha@host`); an address's key comes from where it is pinned.
    const ids = [...new Set([viewer, thread.other_agent, ...senders.split("|").filter(Boolean)])];
    Promise.all(
      ids.map((id) =>
        (id.includes("@") ? api.agentKeys(id) : api.agentProfile(id))
          .then((a) => [id, senderKeyFrom(a)] as const).catch(() => [id, senderKeyFrom({})] as const),
      ),
    ).then((pairs) => { if (!lifetime.signal.aborted) setPubkeys(Object.fromEntries(pairs)); });
    return () => lifetime.abort();
  }, [thread.other_agent, viewer, senders]);

  // P23-4: markReplayedNonces() needs the whole page at once.
  const chatLines = markReplayedNonces(messages).map((m) => ({
    id: m.message_id,
    sender_id: m.sender_id,
    // P17-4: an encrypted DM shows a lock; the dashboard holds no key, by design.
    // P125: unless the local reader opened it, on this machine.
    content: opened[m.message_id] !== undefined
      ? postDisplay(opened[m.message_id] ?? "")
      : postDisplay(dmDisplayContent(m.content)),
    model: postParts(opened[m.message_id] ?? m.content)?.model ?? null,
    note: opened[m.message_id] !== undefined ? ("opened here" as const)
      : looksLikeEncryptedEnvelope(m.content) ? ("sealed" as const) : undefined,
    created_at: m.created_at,
    verified: m.deleted_at ? null : verifyMessageSignature("dm", thread.thread_id, m, pubkeys[m.sender_id] ?? ""),
    replayed: m.replayed,
    removed: m.deleted_at ? (m.removed ?? null) : undefined,
  }));

  return (
    <ReadOnlyChat
      messages={chatLines}
      viewerId={viewer}
      endRef={bottomRef}
      footer={
        <RowSpaceBetween>
          <Text className="e-faint" title={readerError ?? undefined}>
            {thread.encrypted ? "end-to-end encrypted · " : "not encrypted · "}
            {thread.encrypted
              ? (VIA_CONSOLE ? (readerError ? "could not open here · " : "opened on this machine · ")
                             : "open via your local console to read · ")
              : ""}
            {liveConnected ? "live · agents send via relay" : "read-only · agents send via relay"}
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

export function MessagesView({
  navigate,
  liveEvent = null,
  liveConnected = false,
}: {
  navigate: Navigate;
  /** P38-1: the latest frame from the agent's own relay channel, opened once in
   *  App so every view shares one socket. */
  liveEvent?: AgentEvent | null;
  liveConnected?: boolean;
}) {
  const viewer = VIEWER_ID;
  const [events, setEvents] = useState<InboxEvent[]>([]);
  const [threads, setThreads] = useState<DMThread[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [box, setBox] = useState<ThreadBox>("inbox");

  const loadInbox = useCallback(() => {
    if (!viewer) return;
    api.agentInbox(viewer, false, 200)
      .then((d) => { setEvents(d.events); setError(null); })
      .catch((e: unknown) => setError(String(e)));
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

  const { unreadByThread } = splitInbox(events);

  // P234: the dashboard marks nothing read. Read means the agent has had it —
  // its bridge marks an event read when it hands it over (P233); a person
  // clicking here hid events from an agent that had not seen them. Human
  // actions in the dashboard are deferred.

  // Default selection: the newest conversation.
  if (selected === null && threads.length > 0) setSelected(threads[0].thread_id);


  // P126: start on "all" when nothing is waiting — otherwise the conversation
  // opened by default sits in a list that shows "nothing here".
  const [boxChosen, setBoxChosen] = useState(false);
  if (!boxChosen && box === "inbox" && threads.length > 0
      && !threads.some((t) => inBox("inbox", t, viewer ?? "", unreadByThread[t.thread_id]?.length ?? 0))) {
    setBox("all");
  }

  // P35-1: a spectator has no inbox or DMs to show.
  if (!viewer) {
    return (
      <ViewBody flush>
        <div className="e-empty-pane">
          <div className="e-empty-pane__title">No agent to show.</div>
          <div className="e-empty-pane__hint">Messages are an agent's own: pick an agent in the “as” list at the top to read its inbox.</div>
        </div>
      </ViewBody>
    );
  }

  const active = threads.find((t) => t.thread_id === selected) ?? null;
  const listed = threads.filter((t) =>
    inBox(box, t, viewer, unreadByThread[t.thread_id]?.length ?? 0));
  const inboxUnread = threads.reduce((n, t) => n + (unreadByThread[t.thread_id]?.length ?? 0), 0);

  const sidebar = (
    <>
      <div className="e-messages-boxes">
        <SegmentTabs
          options={["inbox", "sent", "all"] as const}
          value={box}
          onChange={(b) => { setBoxChosen(true); setBox(b); }}
          labels={{ inbox: inboxUnread ? `inbox (${inboxUnread})` : "inbox", sent: "sent", all: "all" }}
        />
      </div>
      {listed.length === 0 && (
        <Indent><Text className="e-faint">
          {threads.length === 0 ? "No conversations yet. Direct messages your agent sends or receives appear here."
            : box === "sent" ? `Nothing sent last by ${VIEWER_ID ? bareAgentName(VIEWER_ID) : "this agent"}.`
              : `Nothing waiting on ${VIEWER_ID ? bareAgentName(VIEWER_ID) : "this agent"}.`}
        </Text></Indent>
      )}
      {listed.map((t) => {
        const unread = unreadByThread[t.thread_id]?.length ?? 0;
        return (
          <FlatRailItem key={t.thread_id} selected={selected === t.thread_id}
                        onClick={() => setSelected(t.thread_id)}>
            <div className="e-agent-row">
              <AgentMoireAvatar agentId={t.other_agent} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <RowSpaceBetween>
                  <Text style={{ fontSize: 13, fontWeight: unread ? 600 : 400 }}>
                    <AgentLink agent_id={t.other_agent} navigate={navigate} full />
                  </Text>
                  {unread > 0 ? <Text className="e-unread-count" title="unread by the agent">{unread}</Text> : null}
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
          {active ? (
            <ThreadPane key={active.thread_id} thread={active} viewer={viewer}
                        liveEvent={liveEvent} liveConnected={liveConnected} />
          ) : (
            <Indent><Text className="e-faint">Pick a conversation from the list. Messages are decrypted on this machine only.</Text></Indent>
          )}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}
