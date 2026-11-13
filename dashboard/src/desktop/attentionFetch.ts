/** What the Desktop status chip polls (desktop/chip.tsx). */
import type { InboxEvent, Session } from "../api";
import { gamesToMove } from "../attention";
import { unreadDmCount } from "../messagesModel";
import { httpFetch, relayHttpBase } from "../transport";

export const CHIP_POLL_MS = 30_000;

export interface Attention {
  turns: Session[];
  unread: number;
}

/** The agent's games to move in and its unread direct messages (what the Messages badge counts), or null when it is no agent here. */
export async function fetchAttention(agent: string): Promise<Attention | null> {
  const base = relayHttpBase(agent);
  const id = encodeURIComponent(agent);
  const [inbox, sessions] = await Promise.all([
    httpFetch(`${base}/agents/${id}/inbox?unread_only=true&limit=200`),
    httpFetch(`${base}/agents/${id}/sessions?status=active`),
  ]);
  if (!inbox.ok || !sessions.ok) return null;
  const i = (await inbox.json()) as { events?: InboxEvent[]; unread_dms?: number };
  const s = (await sessions.json()) as { sessions?: Session[] };
  return { turns: gamesToMove(s.sessions, agent), unread: unreadDmCount(i) };
}
