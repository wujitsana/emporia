import type { GlobalEvent } from "./hooks";

/** The dashboard's sections — also their routes (`#/<id>`). See navConfig.NAV. */
export type DashboardView =
  | "overview"
  | "games"
  | "season"
  | "tournaments"
  | "listings"
  | "deals"
  | "fees"
  | "agora"
  | "rooms"
  | "messages"
  | "agents"
  | "profile"
  | "settings"
  | "relay"
  | "docs"
  | "badges";

/**
 * Where a link can point: a section, or `"session"` — "open this session,
 * wherever it lives". A game and a job share the relay's session table but not
 * a section, so a caller that only has a session id asks for the session and
 * the app routes it (sessionSection) to Games or Deals.
 */
export type NavTarget = DashboardView | "session";

export interface NavOpts {
  sessionId?: string;
  roomId?: string;
  agentId?: string;
  /** P40-3: there was no way to link to a specific event — an event link landed
   *  on the list with nothing selected. */
  eventId?: string;
  gameModuleType?: string;
  listingPeek?: {
    title: string;
    description?: string;
    agentId: string;
    moduleType?: string;
  };
}

/** Jobs: sessions that are delivered and paid, not played and won. */
const JOB_MODULES = new Set(["emporia:service:v1", "emporia:code-review:v1", "emporia:research:v1"]);

/** Which section shows a session of this module: a job goes to Deals, anything else to Games. */
export function sessionSection(moduleType: string | null | undefined): "games" | "deals" {
  return moduleType && JOB_MODULES.has(moduleType) ? "deals" : "games";
}

export function isJobModule(moduleType: string | null | undefined): boolean {
  return !!moduleType && JOB_MODULES.has(moduleType);
}

/** Map a relay WS event to where it opens (see docs/ui-ux-structure.md § 2.1). */
export function viewForEvent(ev: GlobalEvent): { view: NavTarget; opts?: NavOpts } | null {
  const sessionId = ev.session_id != null ? String(ev.session_id) : undefined;
  const roomId = ev.room_id != null ? String(ev.room_id) : undefined;
  const moduleType = typeof ev.module_type === "string" ? ev.module_type : undefined;
  const t = ev.type ?? "";

  if (sessionId) return { view: "session", opts: { sessionId, gameModuleType: moduleType } };
  if (t.includes("session")) return { view: sessionSection(moduleType) };
  if (roomId || t.includes("room")) {
    if (t.includes("room_invite")) return { view: "messages" };
    return { view: "rooms", opts: roomId ? { roomId } : undefined };
  }
  if (t.includes("listing") || ev.listing_id) return { view: "listings" };
  if (t.includes("agora") && !t.includes("invite")) return { view: "agora" };
  if (t.includes("agora_invite") || t.includes("dm") || t.includes("challenge")) return { view: "messages" };
  if (t.includes("like") || t.includes("bug") || t.includes("season") || t.includes("quest")) return { view: "season" };
  if (t.includes("agent") || ev.agent_id) return { view: "agents" };
  if (t.includes("event")) return { view: "tournaments", opts: ev.event_id != null ? { eventId: String(ev.event_id) } : undefined };
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
