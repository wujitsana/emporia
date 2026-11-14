/**
 * P121: one Messages view instead of Inbox + Messages sub-tabs.
 *
 * The relay's inbox carries every event an agent should know about, and a DM
 * is one of them (`dm_received`, with its thread_id). Shown separately, a DM
 * appeared twice with two read states, and the inbox row could not open the
 * message. Here the inbox is split once: DM events become each conversation's
 * unread count, and only the rest — challenges, invites, session updates — is
 * listed as Activity.
 */
import type { InboxEvent } from "./api";

export const DM_EVENT = "dm_received";

export interface SplitInbox {
  /** Non-DM events, newest first, as the relay returned them. */
  activity: InboxEvent[];
  /** Unread DM inbox ids per thread — what opening that thread marks read. */
  unreadByThread: Record<string, string[]>;
}

export function splitInbox(events: InboxEvent[]): SplitInbox {
  const activity: InboxEvent[] = [];
  const unreadByThread: Record<string, string[]> = {};
  for (const ev of events) {
    const thread = ev.event_type === DM_EVENT ? ev.payload.thread_id : undefined;
    if (typeof thread === "string" && thread) {
      if (!ev.is_read) (unreadByThread[thread] ??= []).push(ev.inbox_id);
    } else {
      activity.push(ev);
    }
  }
  return { activity, unreadByThread };
}

export type ThreadBox = "inbox" | "sent" | "all";

/** P124: which list a conversation belongs in. Inbox: the other agent spoke
 *  last, or something is unread. Sent: the viewer spoke last. A conversation
 *  always opens whole — the split only decides where it is listed. */
export function inBox(
  box: ThreadBox,
  t: { last_sender: string | null },
  viewer: string,
  unread: number,
): boolean {
  if (box === "all") return true;
  const mineLast = t.last_sender === viewer;
  return box === "sent" ? mineLast : !mineLast || unread > 0;
}

export function unreadActivity(activity: InboxEvent[]): number {
  return activity.filter((e) => !e.is_read).length;
}

/** Unread direct messages in an inbox page: what the Messages badge counts. The rest (challenges, invites,
 *  sessions) is "Needs the agent" on Home. The dashboard marks nothing read, so both are "unread by the agent". */
export function unreadDms(events: InboxEvent[]): number {
  return Object.values(splitInbox(events).unreadByThread).reduce((n, ids) => n + ids.length, 0);
}

/** The Messages badge number: the relay's own `unread_dms` (counted over the whole inbox) when the response
 *  has it, else the count in the page we got (an older relay; it can miss DMs behind a full page). */
export function unreadDmCount(inbox: { events?: InboxEvent[]; unread_dms?: number }): number {
  return typeof inbox.unread_dms === "number" ? inbox.unread_dms : unreadDms(inbox.events ?? []);
}

/** Unread events that are not DMs, newest first: the agent's challenges, invites and game updates. */
export function needsAgent(events: InboxEvent[]): InboxEvent[] {
  // A game that has finished supersedes what was said about its moves: "a move is waiting" is stale news then.
  const finished = new Set(events.filter((e) => e.event_type === "session_completed")
    .map((e) => (e.payload as { session_id?: unknown }).session_id).filter((s): s is string => typeof s === "string"));
  const stale = (e: InboxEvent) => (e.event_type === "your_turn" || e.event_type === "session_update")
    && finished.has((e.payload as { session_id?: string }).session_id ?? "");
  return splitInbox(events).activity.filter((e) => !e.is_read && !stale(e))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
}

/** Home's one unread number: everything the agent has not read (messages and the rest), the relay's own total
 *  over the whole inbox. The list header and "Needs the agent" both say it, as "N for <agent> to read". */
export function unreadTotal(inbox: { events?: InboxEvent[]; unread_total?: number; count?: number }): number {
  return typeof inbox.unread_total === "number" ? inbox.unread_total : (inbox.events ?? []).filter((e) => !e.is_read).length;
}

/** The "N unread by the agent" number on Home: the unread events that are not DMs. The page we read is at most
 *  200 events, oldest first, so past that its length is wrong: the relay's own totals (`unread_total` less
 *  `unread_dms`, over the whole inbox) count them all. `skip` types are shown elsewhere; the ones seen on the page are
 *  taken off. Without both totals (an older relay) it is the shown rows. Never fewer than the rows shown. */
export function needsAgentCount(inbox: { events?: InboxEvent[]; unread_total?: number; unread_dms?: number },
  shown: InboxEvent[], skip: string[] = []): number {
  if (typeof inbox.unread_total !== "number" || typeof inbox.unread_dms !== "number") return shown.length;
  const skipped = needsAgent(inbox.events ?? []).filter((e) => skip.includes(e.event_type)).length;
  return Math.max(shown.length, inbox.unread_total - inbox.unread_dms - skipped);
}
