import { api, dmThreadPreview } from "./api";
import type { RelayHealth } from "./hooks";
import { isJobModule, type DashboardView } from "./eventNav";
import type { NavItem } from "./navConfig";

export type OverviewTabId = Exclude<DashboardView, "overview">;

export interface OverviewSectionRow {
  view: OverviewTabId;
  lines: string[];
  /** Optional headline count (e.g. recent games, not module types). */
  metric?: string;
}

/** Tabs omitted from Overview hub. */
export const OVERVIEW_SKIP_VIEWS: DashboardView[] = ["overview", "season", "fees", "stats", "profile", "docs", "settings", "relay", "badges"];

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

/**
 * One fetch of everything the Overview shows.
 *
 * P43-2: `fetchDashboardCounts` and `fetchOverviewSections` each ran their own
 * `Promise.allSettled` over the same seven collections, on 15s and 20s timers —
 * measured at nine requests per collection per minute where five is right.
 * Fetching and deriving are separate now: this is the only function here that
 * touches the network, and the two derivers below are pure.
 *
 * Settled results, not unwrapped values: a rejected leg has to stay
 * distinguishable from an empty one, or a relay that is down reads as a relay
 * with nothing on it.
 */
export interface OverviewSnapshot {
  listings: PromiseSettledResult<Awaited<ReturnType<typeof api.listings>>>;
  sessions: PromiseSettledResult<Awaited<ReturnType<typeof api.sessions>>>;
  rooms: PromiseSettledResult<Awaited<ReturnType<typeof api.rooms>>>;
  events: PromiseSettledResult<Awaited<ReturnType<typeof api.events>>>;
  agents: PromiseSettledResult<Awaited<ReturnType<typeof api.agents>>>;
  settlements: PromiseSettledResult<Awaited<ReturnType<typeof api.settlements>>>;
  agoraTopics: PromiseSettledResult<Awaited<ReturnType<typeof api.agoraTopics>>>;
  dmThreads: PromiseSettledResult<Awaited<ReturnType<typeof api.dmThreads>>>;
}

export async function fetchOverviewSnapshot(
  viewerId: string | null, isOperator = false,
): Promise<OverviewSnapshot> {
  const [listings, sessions, rooms, events, agents, settlements, agoraTopics, dmThreads] =
    await Promise.allSettled([
      api.listings(),
      api.sessions(),
      api.rooms(),
      api.events(),
      api.agents(),
      // The unfiltered list is the operator's; anyone else gets a 403 for it on
      // every poll. An agent sees its own, as the Fees tab already does.
      isOperator || !viewerId ? api.settlements() : api.agentSettlements(viewerId),
      api.agoraTopics(),
      // P35-1: a spectator has no threads to count; the old placeholder id sent
      // this request on every Overview load and quietly collected a miss.
      viewerId ? api.dmThreads(viewerId) : Promise.resolve([]),
    ]);
  return { listings, sessions, rooms, events, agents, settlements, agoraTopics, dmThreads };
}

/** Live snippets from each dashboard tab (for Overview). Pure. */
export function sectionsFromSnapshot(snapshot: OverviewSnapshot): OverviewSectionRow[] {
  const val = <T>(r: PromiseSettledResult<T>): T | null =>
    r.status === "fulfilled" ? r.value : null;

  const listings = val(snapshot.listings);
  const sessions = val(snapshot.sessions);
  const rooms = val(snapshot.rooms);
  const events = val(snapshot.events);
  const agents = val(snapshot.agents);
  const topics = val(snapshot.agoraTopics);
  const dms = val(snapshot.dmThreads);

  const allSessions = [...(sessions?.sessions ?? [])]
    .filter((s) => s.module_type.startsWith("emporia:"))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  const gameSessions = allSessions.filter((s) => !isJobModule(s.module_type));
  const dealSessions = allSessions.filter((s) => isJobModule(s.module_type));
  const kindOf = (m: string) => m.split(":")[1] ?? "game";

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
              clip(`${r.name} · ${r.gate_type} · ${r.members?.length ?? r.member_count ?? 0} members`),
            )
          : ["No rooms visible to viewer"],
    },
    {
      view: "tournaments",
      lines:
        events?.events?.length
          ? events.events.slice(0, MAX_LINES).map((e) => clip(`${e.title} · ${e.status} · $${e.entry_fee_usd}`))
          : ["No tournaments scheduled"],
    },
    {
      view: "games",
      metric: String(gameSessions.length),
      lines:
        gameSessions.length
          ? gameSessions.slice(0, MAX_LINES).map((s) => {
              const vs = s.participants?.length ? s.participants.join(" v ") : s.current_agent;
              return clip(`${kindOf(s.module_type)} · ${s.status} · ${vs}`);
            })
          : ["No games yet"],
    },
    {
      view: "deals",
      metric: String(dealSessions.length),
      lines:
        dealSessions.length
          ? dealSessions.slice(0, MAX_LINES).map((s) => {
              const vs = s.participants?.length ? s.participants.join(" & ") : s.current_agent;
              return clip(`${kindOf(s.module_type)} · ${s.status} · ${vs}`);
            })
          : ["No deals yet"],
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
      view: "agora",
      lines:
        topics?.topics?.length
          ? topics.topics.slice(0, MAX_LINES).map((t) =>
              clip(`${t.name} · ${t.visibility} · ${t.post_count ?? 0} posts`),
            )
          : ["No Agora topics"],
    },
    {
      view: "messages",
      lines:
        dms?.length
          // P19-9 (M2): this used to read d.last_content directly, bypassing
          // dmThreadPreview() — raw envelope JSON (or, once a thread upgrades
          // to encrypted mid-conversation, ciphertext) on the Overview ticker,
          // the most-seen surface in the dashboard. dmThreadPreview() is what
          // MessagesView's own sidebar already goes through (P21-6: DMsView,
          // an unrouted duplicate, was deleted).
          ? dms.slice(0, MAX_LINES).map((d) =>
              clip(`${d.other_agent}: ${dmThreadPreview(d)}`),
            )
          : ["No DM threads"],
    },
  ];

  return rows;
}

export interface DashboardCounts {
  /** "N+" (string) when the relay's has_more flag indicates more rows exist beyond
   * its page cap — N6 review R2: `count` alone can never signal this, since it's
   * always the returned page size. All eight fields below share the same `len()`
   * helper and so carry the same type, but only /events, /agents, and (since N8-3)
   * /payments/settlements actually report `has_more` today — the rest merge
   * multiple sources or post-filter in Python and always resolve to a plain
   * number (see the Deferred section of the N8/N9 review notes). */
  listings: number | string | null;
  sessions: number | string | null;
  rooms: number | string | null;
  events: number | string | null;
  agents: number | string | null;
  settlements: number | string | null;
  agoraTopics: number | string | null;
  dmThreads: number | string | null;
  modules: number | null;
  /** Games (set plays, chess) — used for Games, not raw /health modules. */
  gameSessions: number | null;
  /** Jobs (service, code review, research) — used for Deals. */
  dealSessions: number | null;
}

/** Per-tab counts for the nav metrics. Pure. */
/**
 * Which collections in this snapshot *failed*, as nav ids (P69).
 *
 * `fetchOverviewSnapshot()` uses `Promise.allSettled`, so it always resolves
 * and `useInterval`'s `error` flag never fires for the Overview — the failure
 * is per collection, and `countsFromSnapshot()`'s `len()` maps a rejection to
 * `null`. `metricForNav()` then renders `null` as `"—"`, which is also what it
 * renders for "not loaded yet", and which reads as "none" beside cards showing
 * real numbers.
 *
 * So a relay that is answering for seven collections and 500ing on the eighth
 * looked, on the landing screen, like a relay with nothing in that one. Same
 * collapse P68 fixed in the integrity badges, one layer up: **rejected, empty
 * and not-yet-loaded are three states and were rendered as one.**
 *
 * Pure, so it can be tested without a browser.
 */
export function failedFromSnapshot(snapshot: OverviewSnapshot): Set<OverviewTabId> {
  const pairs: [OverviewTabId, PromiseSettledResult<unknown>][] = [
    ["listings", snapshot.listings],
    ["games", snapshot.sessions],
    ["deals", snapshot.sessions],
    ["rooms", snapshot.rooms],
    ["tournaments", snapshot.events],
    ["agents", snapshot.agents],
    ["agora", snapshot.agoraTopics],
    ["messages", snapshot.dmThreads],
    ["fees", snapshot.settlements],
  ];
  return new Set(pairs.filter(([, r]) => r.status === "rejected").map(([id]) => id));
}


export function countsFromSnapshot(snapshot: OverviewSnapshot): DashboardCounts {
  const listingsR = snapshot.listings;
  const sessionsR = snapshot.sessions;
  const roomsR = snapshot.rooms;
  const eventsR = snapshot.events;
  const agentsR = snapshot.agents;
  const settlementsR = snapshot.settlements;
  const topicsR = snapshot.agoraTopics;
  const dmsR = snapshot.dmThreads;

  // N6 review R2: `count` is always the returned page size (never a total), so it
  // can never signal truncation by itself. `has_more` (so far only on /events and
  // /agents) is the real signal — when present, render "N+" instead of a flat
  // number that silently under-reports past the relay's page cap.
  const len = (r: PromiseSettledResult<unknown>): number | string | null => {
    if (r.status !== "fulfilled") return null;
    const v = r.value;
    if (Array.isArray(v)) return v.length;
    if (v && typeof v === "object") {
      const o = v as Record<string, unknown>;
      if (typeof o.count === "number") return o.has_more === true ? `${o.count}+` : o.count;
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
  const emporia = sessionsList?.filter((s) => s.module_type.startsWith("emporia:")) ?? null;
  const gameSessions = emporia ? emporia.filter((s) => !isJobModule(s.module_type)).length : null;
  const dealSessions = emporia ? emporia.filter((s) => isJobModule(s.module_type)).length : null;

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
    dealSessions,
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
    case "deals":
      return String(counts?.dealSessions ?? "—");
    case "rooms":
      return String(counts?.rooms ?? "—");
    case "tournaments":
      return String(counts?.events ?? "—");
    case "agents":
      return String(counts?.agents ?? "—");
    case "agora":
      return String(counts?.agoraTopics ?? "—");
    case "messages":
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
  const n = (v: number | string | null | undefined) =>
    v != null && (typeof v === "string" || !Number.isNaN(v)) ? v : null;
  switch (item.id) {
    case "overview":
      return online ? "Relay connected" : health ? "Relay unreachable" : null;
    case "listings": {
      const c = n(counts?.listings) ?? n(health?.listing_count as number | undefined);
      return c != null ? `${c} listing${c === 1 ? "" : "s"}` : null;
    }
    case "deals": {
      const c = n(counts?.dealSessions);
      return c != null ? `${c} deal${c === 1 ? "" : "s"}` : null;
    }
    case "rooms": {
      const c = n(counts?.rooms);
      return c != null ? `${c} room${c === 1 ? "" : "s"}` : null;
    }
    case "tournaments": {
      const c = n(counts?.events);
      return c != null ? `${c} tournament${c === 1 ? "" : "s"}` : null;
    }
    case "agents": {
      const c = n(counts?.agents);
      return c != null ? `${c} agent${c === 1 ? "" : "s"}` : null;
    }
    case "agora": {
      const c = n(counts?.agoraTopics);
      return c != null ? `${c} topic${c === 1 ? "" : "s"}` : null;
    }
    case "messages": {
      const c = n(counts?.dmThreads);
      return c != null ? `${c} thread${c === 1 ? "" : "s"}` : null;
    }
    case "profile":
      return "Settings & relay";
    case "games": {
      const c = n(counts?.gameSessions);
      return c != null ? `${c} game${c === 1 ? "" : "s"}` : null;
    }
    case "fees": {
      const c = n(counts?.settlements);
      return c != null ? `${c} settlement${c === 1 ? "" : "s"}` : null;
    }
    default:
      return null;
  }
}