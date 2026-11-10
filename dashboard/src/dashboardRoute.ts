import { destById, destinationOf, isArenaLens, NAV, type ArenaLens, type View } from "./navConfig";
import { MODE } from "./transport";

/** Sections whose route can name one session: `#/arena/games/<session_id>`, `#/agent/deals/<session_id>`. */
const SESSION_ROUTES = new Set<View>(["games", "deals"]);

/**
 * Destinations an earlier layout had, so a stale bookmark still opens something useful: where a bare
 * `#/play`, `#/market`, `#/talk`, `#/agents`, `#/you` or `#/more` goes now. (UI routing only; nothing in the
 * relay's protocol depends on it.)
 */
const LEGACY_DESTINATIONS: Record<string, View> = {
  play: "games", market: "rooms", season: "season",
  talk: "feed", agents: "agents", you: "profile", more: "docs",
};
/** Sections that no longer exist or were renamed, and where their content went. */
const LEGACY_SECTIONS: Record<string, string> = {
  offers: "rooms",      // its rooms are in Rooms
  topics: "agora",      // the Topics tab is the section `agora`
  money: "fees",        // the route's word for Money
  badges: "season",     // the catalogue sits at the foot of the Season
};
/** The route's word for a section, where it differs from the section's id. */
const ROUTE_NAME: Partial<Record<View, string>> = { agora: "topics", fees: "money" };

export interface Route {
  view: View;
  /** The session a games or deals route names, if any. */
  sessionId: string | null;
  /** The Arena's lens (`#/arena/waiting`), when the route names one. */
  lens: ArenaLens | null;
}

/*
 * Where the route lives. In a browser it is the page's hash (`#/arena/games/<id>`), so
 * a link opens a section. On the Hermes Desktop page the hash is the app's own
 * router (Desktop matches a plugin page by its exact path, `/emporia`), so the
 * same route strings live in memory instead: the page's palette commands set
 * them, and a profile switch resets them.
 */
const IN_MEMORY = MODE === "desktop";
const DEFAULT_ROUTE = "#/home";
let memoryHash = DEFAULT_ROUTE;
const listeners = new Set<() => void>();

/** The current route string, `#/<view>[/<id>][?…]`. */
export function currentHash(): string {
  return IN_MEMORY ? memoryHash : window.location.hash;
}

/** Record the route without announcing it (the app already shows it). */
export function replaceHash(next: string): void {
  if (IN_MEMORY) {
    memoryHash = next;
    return;
  }
  if (window.location.hash === next) return;
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${next}`);
}

/** Record the route as a new history entry, so Back returns to where the person was. */
export function pushHash(next: string): void {
  if (IN_MEMORY) {
    memoryHash = next;
    return;
  }
  if (window.location.hash === next) return;
  window.history.pushState(null, "", `${window.location.pathname}${window.location.search}${next}`);
}

/** Go to a route from outside the app (a Desktop palette command, a status chip). */
export function goToHash(next: string): void {
  if (!IN_MEMORY) {
    window.location.hash = next;
    return;
  }
  memoryHash = next;
  listeners.forEach((fn) => fn());
}

/** Back to the Overview, silently: a new profile starts there. */
export function resetRoute(): void {
  memoryHash = DEFAULT_ROUTE;
}

/** Called on every route change from outside the app; returns the unsubscribe. */
export function onRouteChange(fn: () => void): () => void {
  if (IN_MEMORY) {
    listeners.add(fn);
    return () => { listeners.delete(fn); };
  }
  window.addEventListener("hashchange", fn);
  return () => window.removeEventListener("hashchange", fn);
}

/**
 * Hash routes, v4: `#/<destination>/<section>[/<session_id>]`, or
 * `#/<destination>` alone for its first section: `#/home`, `#/arena/waiting`, `#/arena/games/<id>`,
 * `#/agent/deals/<id>`, `#/agora/rooms`, `#/agora/topics`, `#/labs/season`, `#/help`, `#/desk/relay`.
 * Routes from the earlier layouts (`#/talk/feed`, `#/talk/agora`, `#/talk/messages`, `#/agents/agents`,
 * `#/you/fees`, `#/you/deals`, `#/labs/badges`, `#/more/docs`, `#/play/games/<id>`, `#/market/stats`,
 * `#/season`) are read too and open where the section lives now.
 *
 * Anything else opens Home.
 */
export function routeFromHash(hash: string = currentHash()): Route {
  const parts = hash.replace(/^#\/?/, "").split("?")[0].trim().split("/");
  const first = (parts[0] ?? "").toLowerCase();
  let view: View | null = null;
  let rawId: string | undefined;
  let lens: ArenaLens | null = null;
  const dest = destById(first);
  const legacyView = LEGACY_DESTINATIONS[first];
  if (dest || legacyView) {
    const second = (parts[1] ?? "").toLowerCase();
    const named = LEGACY_SECTIONS[second] ?? second;
    if (first === "arena" && isArenaLens(second)) {
      view = "games";
      lens = second;
    } else if (NAV.some((n) => n.id === named)) {
      // A section the link names opens wherever it lives now, whichever destination the link said
      // (`#/market/stats`, `#/more/profile`): a bookmark from an earlier layout still lands.
      view = named as View;
      rawId = parts[2];
    } else {
      view = dest ? dest.views[0] : legacyView;
      rawId = undefined;
    }
    // The old You > Deals with no session named is Money now; a deal that names its session keeps its page.
    if (first === "you" && view === "deals" && !rawId) view = "fees";
  }
  if (!view) return { view: "overview", sessionId: null, lens: null };
  const id = rawId ? safeDecode(rawId) : "";
  return { view, sessionId: SESSION_ROUTES.has(view) && /^[A-Za-z0-9_.:-]{1,128}$/.test(id) ? id : null, lens };
}

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return "";
  }
}

/** The step a match link names: `#/arena/games/<id>?step=3` → 3; absent or not a whole number → null. */
export function stepFromHash(hash: string = currentHash()): number | null {
  const q = hash.split("?")[1];
  if (!q) return null;
  const raw = new URLSearchParams(q).get("step");
  if (raw === null || !/^\d{1,6}$/.test(raw)) return null;
  return Number(raw);
}

export function hashForView(view: View, sessionId?: string | null, step?: number | null, lens?: ArenaLens | null): string {
  const dest = destinationOf(view);
  const named = !!sessionId && SESSION_ROUTES.has(view);
  if (view === "games" && !named) return lens && lens !== "live" ? `#/arena/${lens}` : "#/arena";
  // a lone section is its destination's route, unless a session follows: then the section is named
  const base = dest.views.length === 1 && !named ? `#/${dest.id}` : `#/${dest.id}/${ROUTE_NAME[view] ?? view}`;
  const at = named && step !== null && step !== undefined ? `?step=${step}` : "";
  return named ? `${base}/${encodeURIComponent(sessionId)}${at}` : base;
}

/**
 * Record the route. A move to another section, lens or match is a new history entry (so Back goes
 * where the person was); scrubbing the steps of a match, or the first write of a fresh load, replaces.
 */
export function syncHash(view: View, sessionId?: string | null, step?: number | null, lens?: ArenaLens | null): void {
  const next = hashForView(view, sessionId, step, lens);
  const here = currentHash();
  if (!here) { replaceHash(next); return; }
  const before = routeFromHash(here);
  const same = before.view === view && (before.sessionId ?? null) === (sessionId ?? null)
    && (view !== "games" || sessionId || (before.lens ?? "live") === (lens ?? "live"));
  (same ? replaceHash : pushHash)(next);
}
