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
