import { NAV, type View } from "./navConfig";
import { MODE } from "./transport";

const VIEW_IDS = new Set<View>(NAV.map((n) => n.id));
/** Sections whose route can name one session: `#/games/<session_id>`, `#/deals/<session_id>`. */
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
const DEFAULT_ROUTE = "#/overview";
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

/** Hash route: `#/games`, `#/games/<session_id>`, `#/season`, … Anything else opens the Overview. */
export function routeFromHash(hash: string = currentHash()): Route {
  const [rawView, rawId] = hash.replace(/^#\/?/, "").split("?")[0].trim().split("/");
  const view = (rawView ?? "").toLowerCase() as View;
  if (!rawView || !VIEW_IDS.has(view)) return { view: "overview", sessionId: null };
  const id = rawId ? decodeURIComponent(rawId) : "";
  return { view, sessionId: SESSION_ROUTES.has(view) && /^[A-Za-z0-9_.:-]{1,128}$/.test(id) ? id : null };
}

export function viewFromHash(hash: string = currentHash()): View {
  return routeFromHash(hash).view;
}

export function hashForView(view: View, sessionId?: string | null): string {
  return sessionId && SESSION_ROUTES.has(view) ? `#/${view}/${encodeURIComponent(sessionId)}` : `#/${view}`;
}

export function syncHash(view: View, sessionId?: string | null): void {
  replaceHash(hashForView(view, sessionId));
}
