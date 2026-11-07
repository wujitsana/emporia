import { destById, destinationOf, NAV, type View } from "./navConfig";
import { MODE } from "./transport";

const VIEW_IDS = new Set<View>(NAV.map((n) => n.id));
/** Sections whose route can name one session: `#/play/games/<session_id>`, `#/market/deals/<session_id>`. */
const SESSION_ROUTES = new Set<View>(["games", "deals"]);

export interface Route {
  view: View;
  /** The session a games or deals route names, if any. */
  sessionId: string | null;
}

/*
 * Where the route lives. In a browser it is the page's hash (`#/games/<id>`), so
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
 * Hash routes, v2 (docs/ui-nav-v2.md): `#/<destination>/<section>[/<session_id>]`, or
 * `#/<destination>` alone for its first section — `#/home`, `#/play/games/<id>`,
 * `#/market/deals/<id>`, `#/talk/rooms`, `#/season`, `#/more/docs`.
 *
 * The v1 routes still open what they opened (`#/overview`, `#/games/<id>`, `#/deals/<id>`,
 * `#/rooms`, …): a bookmark or a link in a doc keeps working. Anything else opens Home.
 */
export function routeFromHash(hash: string = currentHash()): Route {
  const parts = hash.replace(/^#\/?/, "").split("?")[0].trim().split("/");
  const first = (parts[0] ?? "").toLowerCase();
  let view: View | null = null;
  let rawId: string | undefined;
  const dest = destById(first);
  if (dest) {
    const second = (parts[1] ?? "").toLowerCase() as View;
    if (dest.views.includes(second)) {
      view = second;
      rawId = parts[2];
    } else {
      view = dest.views[0];
      rawId = undefined;
    }
  } else if (VIEW_IDS.has(first as View)) {
    view = first as View;            // a v1 route
    rawId = parts[1];
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

export function viewFromHash(hash: string = currentHash()): View {
  return routeFromHash(hash).view;
}

export function hashForView(view: View, sessionId?: string | null): string {
  const dest = destinationOf(view);
  const named = !!sessionId && SESSION_ROUTES.has(view);
  // a lone section is its destination's route, unless a session follows: then the section is named
  const base = dest.views.length === 1 && !named ? `#/${dest.id}` : `#/${dest.id}/${view}`;
  return named ? `${base}/${encodeURIComponent(sessionId)}` : base;
}

export function syncHash(view: View, sessionId?: string | null): void {
  replaceHash(hashForView(view, sessionId));
}
