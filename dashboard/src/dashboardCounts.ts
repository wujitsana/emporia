import { api } from "./api";
import type { RelayHealth } from "./hooks";
import type { DashboardView } from "./eventNav";
import type { NavItem } from "./navConfig";

export type OverviewTabId = Exclude<DashboardView, "overview">;

export interface OverviewSectionRow {
  view: OverviewTabId;
  lines: string[];
  /** Optional headline count (e.g. recent games, not module types). */
  metric?: string;
}

/** Tabs omitted from Overview hub. */
export const OVERVIEW_SKIP_VIEWS: DashboardView[] = ["overview", "games", "fees", "profile"];

/** Grid slots per row (≥ card count) so few cards don't stretch to half-screen squares. */
export function overviewGridCols(cardCount: number): number {
  if (cardCount <= 1) return 1;
  if (cardCount === 2) return 5;
  if (cardCount === 3) return 5;
  return Math.min(5, cardCount);
}

const MAX_LINES = 3;

function clip(s: string, max = 52): string {
  const t = s.replace(/\s+/g, " ").trim();
  return t.length <= max ? t : `${t.slice(0, max - 1)}…`;
}

/** Live snippets from each dashboard tab (for Overview). */
export async function fetchOverviewSections(viewerId: string): Promise<OverviewSectionRow[]> {
  const settled = await Promise.allSettled([
    api.listings(),
    api.sessions(),
    api.rooms(viewerId),
    api.events(),
    api.agents(),
    api.agoraTopics(),
    api.dmThreads(viewerId),
    api.settlements(),
  ]);

  const val = <T>(i: number): T | null =>
    settled[i]?.status === "fulfilled" ? (settled[i] as PromiseFulfilledResult<T>).value : null;

  const listings = val<Awaited<ReturnType<typeof api.listings>>>(0);
  const sessions = val<Awaited<ReturnType<typeof api.sessions>>>(1);
  const rooms = val<Awaited<ReturnType<typeof api.rooms>>>(2);
  const events = val<Awaited<ReturnType<typeof api.events>>>(3);
  const agents = val<Awaited<ReturnType<typeof api.agents>>>(4);
  const topics = val<Awaited<ReturnType<typeof api.agoraTopics>>>(5);
  const dms = val<Awaited<ReturnType<typeof api.dmThreads>>>(6);
  const settlementsData = val<Awaited<ReturnType<typeof api.settlements>>>(7);

  const gameSessions = [...(sessions?.sessions ?? [])]
    .filter((s) => s.module_type.startsWith("emporia:"))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));

  const allSettlements = settlementsData?.settlements ?? [];

  const rows: OverviewSectionRow[] = [
    {
      view: "listings",
      lines:
        listings?.listings?.length
          ? listings.listings.slice(0, MAX_LINES).map((l) =>
              clip(`${l.title || l.listing_type} · ${l.payment_mode}${l.price_usd ? ` · $${l.price_usd}` : ""}`),
            )
          : ["No open listings"],
    },
    {
      view: "rooms",
      lines:
        rooms?.rooms?.length
          ? rooms.rooms.slice(0, MAX_LINES).map((r) =>
              clip(`${r.name} · ${r.gate_type} · ${r.members?.length ?? 0} members`),
            )
          : ["No rooms visible to viewer"],
    },
    {
      view: "events",
      lines:
        events?.events?.length
          ? events.events.slice(0, MAX_LINES).map((e) => clip(`${e.title} · ${e.status} · $${e.entry_fee_usd}`))
          : ["No scheduled events"],
    },
    {
      view: "games",
      metric: String(gameSessions.length),
      lines:
        gameSessions.length
          ? gameSessions.slice(0, MAX_LINES).map((s) => {
              const kind = s.module_type.split(":").pop() ?? "game";
              const vs = s.participants?.length ? s.participants.join(" vs ") : s.current_agent;
              return clip(`${kind} · ${s.status} · ${vs}`);
            })
          : ["No recent games"],
    },
    {
      view: "sessions",
      metric: String(gameSessions.length),
      lines:
        gameSessions.length
          ? gameSessions.slice(0, MAX_LINES).map((s) => {
              const kind = s.module_type.split(":")[1] ?? "game";
              const vs = s.participants?.length ? s.participants.join(" vs ") : s.current_agent;
              return clip(`${kind} · ${s.status} · ${vs}`);
            })
          : ["No recent sessions"],
    },
    {
      view: "agents",
      lines:
        agents?.agents?.length
          ? agents.agents.slice(0, MAX_LINES).map((a) =>
              clip(`${a.display_name || a.agent_id} · ${a.trust_level}${a.session_count ? ` · ${a.session_count} sessions` : ""}`),
            )
          : ["No registered agents"],
    },
    {
      view: "agoras",
      lines:
        topics?.topics?.length
          ? topics.topics.slice(0, MAX_LINES).map((t) =>
              clip(`${t.name} · ${t.visibility} · ${t.post_count ?? 0} posts`),
            )
          : ["No Agora topics"],
    },
    {
      view: "dms",
      lines:
        dms?.length
          ? dms.slice(0, MAX_LINES).map((d) =>
              clip(`${d.other_agent}: ${d.last_content || "(no preview)"}`),
            )
          : ["No DM threads"],
    },
  ];

  return rows;
}

export interface DashboardCounts {
  listings: number | null;
  sessions: number | null;
  rooms: number | null;
  events: number | null;
  agents: number | null;
  settlements: number | null;
  agoraTopics: number | null;
  dmThreads: number | null;
  modules: number | null;
  /** Emporia module sessions (chess, etc.) — used for Games, not raw /health modules. */
  gameSessions: number | null;
}

export async function fetchDashboardCounts(viewerId: string): Promise<DashboardCounts> {
  const [listingsR, sessionsR, roomsR, eventsR, agentsR, settlementsR, topicsR, dmsR] =
    await Promise.allSettled([
      api.listings(),
      api.sessions(),
      api.rooms(viewerId),
      api.events(),
      api.agents(),
      api.settlements(),
      api.agoraTopics(),
      api.dmThreads(viewerId),
    ]);

  const len = (r: PromiseSettledResult<unknown>): number | null => {
    if (r.status !== "fulfilled") return null;
    const v = r.value;
    if (Array.isArray(v)) return v.length;
    if (v && typeof v === "object") {
      const o = v as Record<string, unknown>;
      if (typeof o.count === "number") return o.count;
      if (Array.isArray(o.listings)) return o.listings.length;
      if (Array.isArray(o.sessions)) return o.sessions.length;
      if (Array.isArray(o.rooms)) return o.rooms.length;
      if (Array.isArray(o.events)) return o.events.length;
      if (Array.isArray(o.agents)) return o.agents.length;
      if (Array.isArray(o.settlements)) return o.settlements.length;
      if (Array.isArray(o.topics)) return o.topics.length;
    }
    return null;
  };

  const sessionsList =
    sessionsR.status === "fulfilled" ? sessionsR.value.sessions : null;
  const gameSessions = sessionsList
    ? sessionsList.filter((s) => s.module_type.startsWith("emporia:")).length
    : null;

  return {
    listings: len(listingsR),
    sessions: len(sessionsR),
    rooms: len(roomsR),
    events: len(eventsR),
    agents: len(agentsR),
    settlements: len(settlementsR),
    agoraTopics: len(topicsR),
    dmThreads: len(dmsR),
    modules: null,
    gameSessions,
  };
}

export function metricForNav(
  item: NavItem,
  health: RelayHealth | null,
  counts: DashboardCounts | null,
  _feedLen: number,
  online: boolean,
): string {
  switch (item.id) {
    case "overview":
      return online ? "● LIVE" : health ? "OFFLINE" : "…";
    case "listings":
      return String(counts?.listings ?? health?.listing_count ?? "—");
    case "sessions":
      return String(counts?.sessions ?? health?.session_count ?? "—");
    case "rooms":
      return String(counts?.rooms ?? "—");
    case "events":
      return String(counts?.events ?? "—");
    case "agents":
      return String(counts?.agents ?? "—");
    case "agoras":
      return String(counts?.agoraTopics ?? "—");
    case "dms":
      return String(counts?.dmThreads ?? "—");
    case "profile":
      return "◎";
    case "games":
      return String(counts?.gameSessions ?? "—");
    case "fees":
      return String(counts?.settlements ?? "—");
    default:
      return "—";
  }
}

export function subForNav(item: NavItem, feedLen: number, wsConnected: boolean): string {
  if (item.id === "overview") {
    return wsConnected ? `${feedLen} events on feed` : "WS reconnecting…";
  }
  return item.blurb;
}

/** Optional count caption for overview cards (words, not bare digits) */
export function countCaptionForNav(
  item: NavItem,
  health: RelayHealth | null,
  counts: DashboardCounts | null,
  online: boolean,
): string | null {
  const n = (v: number | null | undefined) => (v != null && !Number.isNaN(v) ? v : null);
  switch (item.id) {
    case "overview":
      return online ? "Relay connected" : health ? "Relay unreachable" : null;
    case "listings": {
      const c = n(counts?.listings) ?? n(health?.listing_count as number | undefined);
      return c != null ? `${c} listing${c === 1 ? "" : "s"}` : null;
    }
    case "sessions": {
      const c = n(counts?.sessions) ?? n(health?.session_count as number | undefined);
      return c != null ? `${c} session${c === 1 ? "" : "s"}` : null;
    }
    case "rooms": {
      const c = n(counts?.rooms);
      return c != null ? `${c} room${c === 1 ? "" : "s"}` : null;
    }
    case "events": {
      const c = n(counts?.events);
      return c != null ? `${c} event${c === 1 ? "" : "s"}` : null;
    }
    case "agents": {
      const c = n(counts?.agents);
      return c != null ? `${c} agent${c === 1 ? "" : "s"}` : null;
    }
    case "agoras": {
      const c = n(counts?.agoraTopics);
      return c != null ? `${c} topic${c === 1 ? "" : "s"}` : null;
    }
    case "dms": {
      const c = n(counts?.dmThreads);
      return c != null ? `${c} thread${c === 1 ? "" : "s"}` : null;
    }
    case "profile":
      return "Settings & relay";
    case "games": {
      const c = n(counts?.gameSessions);
      return c != null ? `${c} recent game${c === 1 ? "" : "s"}` : null;
    }
    case "fees": {
      const c = n(counts?.settlements);
      return c != null ? `${c} settlement${c === 1 ? "" : "s"}` : null;
    }
    default:
      return null;
  }
}