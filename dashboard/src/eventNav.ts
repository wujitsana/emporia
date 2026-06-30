import type { GlobalEvent } from "./hooks";

export type DashboardView =
  | "overview"
  | "listings"
  | "sessions"
  | "rooms"
  | "events"
  | "agents"
  | "fees"
  | "agoras"
  | "games"
  | "messages"
  | "profile";

export interface NavOpts {
  sessionId?: string;
  roomId?: string;
  agentId?: string;
  gameModuleType?: string;
  listingPeek?: {
    title: string;
    description?: string;
    agentId: string;
    moduleType?: string;
  };
}

/** Map a relay WS event to a dashboard view + selection (when applicable). */
export function viewForEvent(ev: GlobalEvent): { view: DashboardView; opts?: NavOpts } | null {
  const sessionId = ev.session_id != null ? String(ev.session_id) : undefined;
  const roomId = ev.room_id != null ? String(ev.room_id) : undefined;
  const t = ev.type ?? "";

  if (sessionId || t.includes("session")) {
    return { view: "sessions", opts: sessionId ? { sessionId } : undefined };
  }
  if (roomId || t.includes("room")) {
    return { view: "rooms", opts: roomId ? { roomId } : undefined };
  }
  if (t.includes("listing") || ev.listing_id) return { view: "listings" };
  if (t.includes("agora") && !t.includes("invite")) return { view: "agoras" };
  if (t.includes("agora_invite") || t.includes("room_invite") || t.includes("dm") || t.includes("challenge")) return { view: "messages" };
  if (t.includes("agent") || ev.agent_id) return { view: "agents" };
  if (t.includes("event") && !t.includes("session")) return { view: "events" };
  if (t.includes("payment") || t.includes("settlement") || t.includes("fee")) return { view: "overview" };
  return null;
}

export function eventDetail(ev: GlobalEvent): string {
  if (ev.session_id) return `…${String(ev.session_id).slice(-10)}`;
  if (ev.listing_id) return `…${String(ev.listing_id).slice(-10)}`;
  if (ev.room_id) return `…${String(ev.room_id).slice(-10)}`;
  if (ev.agent_id) return `…${String(ev.agent_id).slice(-10)}`;
  return "—";
}