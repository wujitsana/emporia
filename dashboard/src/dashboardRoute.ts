import { destById, destinationOf, NAV, type View } from "./navConfig";
import { MODE } from "./transport";

/** Sections whose route can name one session: `#/arena/games/<session_id>`, `#/you/deals/<session_id>`. */
const SESSION_ROUTES = new Set<View>(["games", "deals"]);

/**
 * Destinations an earlier layout had, so a stale bookmark still opens something useful: where a bare
 * `#/play`, `#/market` or `#/season` goes now. (UI routing only; nothing in the relay's protocol depends on it.)
 */
const LEGACY_DESTINATIONS: Record<string, View> = { play: "games", market: "rooms", season: "season" };
/** A bare destination opens its first section, except More: its first two are the operator's, so it opens the Docs. */
const BARE_DEFAULT: Partial<Record<string, View>> = { more: "docs" };
/** Sections that no longer exist, and where their content went (Offers: its rooms are in Rooms). */
const LEGACY_SECTIONS: Record<string, string> = { offers: "rooms" };

export interface Route {
  view: View;
  /** The session a games or deals route names, if any. */
  sessionId: string | null;
}

/*
 * Where the route lives. In a browser it is the page's hash (`#/play/games/<id>`), so
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
 * Hash routes, v3: `#/<destination>/<section>[/<session_id>]`, or
 * `#/<destination>` alone for its first section — `#/home`, `#/arena/games/<id>`,
 * `#/you/deals/<id>`, `#/talk/rooms`, `#/labs/season`, `#/more/docs`. Routes from the
 * earlier layout (`#/play/games/<id>`, `#/market/stats`, `#/season`) are read too.
 *
 * Anything else opens Home.
 */
export function routeFromHash(hash: string = currentHash()): Route {
  const parts = hash.replace(/^#\/?/, "").split("?")[0].trim().split("/");
  const first = (parts[0] ?? "").toLowerCase();
  let view: View | null = null;
  let rawId: string | undefined;
  const dest = destById(first);
  const legacyView = LEGACY_DESTINATIONS[first];
  if (dest || legacyView) {
    const second = (parts[1] ?? "").toLowerCase();
    const named = LEGACY_SECTIONS[second] ?? second;
    if (NAV.some((n) => n.id === named)) {
      // A section the link names opens wherever it lives now, whichever destination the link said
      // (`#/market/stats`, `#/more/profile`): a bookmark from an earlier layout still lands.
      view = named as View;
      rawId = parts[2];
    } else {
      view = dest ? BARE_DEFAULT[dest.id] ?? dest.views[0] : legacyView;
      rawId = undefined;
    }
  }
  if (!view) return { view: "overview", sessionId: null };
  const id = rawId ? safeDecode(rawId) : "";
  return { view, sessionId: SESSION_ROUTES.has(view) && /^[A-Za-z0-9_.:-]{1,128}$/.test(id) ? id : null };
}

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return "";
  }
}

/** The step a match link names: `#/play/games/<id>?step=3` → 3; absent or not a whole number → null. */
export function stepFromHash(hash: string = currentHash()): number | null {
  const q = hash.split("?")[1];
  if (!q) return null;
  const raw = new URLSearchParams(q).get("step");
  if (raw === null || !/^\d{1,6}$/.test(raw)) return null;
  return Number(raw);
}

export function hashForView(view: View, sessionId?: string | null, step?: number | null): string {
  const dest = destinationOf(view);
  const named = !!sessionId && SESSION_ROUTES.has(view);
  // a lone section is its destination's route, unless a session follows: then the section is named
  const base = dest.views.length === 1 && !named ? `#/${dest.id}` : `#/${dest.id}/${view}`;
  const at = named && step !== null && step !== undefined ? `?step=${step}` : "";
  return named ? `${base}/${encodeURIComponent(sessionId)}${at}` : base;
}

export function syncHash(view: View, sessionId?: string | null, step?: number | null): void {
  replaceHash(hashForView(view, sessionId, step));
}
