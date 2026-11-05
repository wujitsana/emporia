import { NAV, type View } from "./navConfig";

const VIEW_IDS = new Set<View>(NAV.map((n) => n.id));
/** Sections whose route can name one session: `#/games/<session_id>`, `#/deals/<session_id>`. */
const SESSION_ROUTES = new Set<View>(["games", "deals"]);

export interface Route {
  view: View;
  /** The session a games or deals route names, if any. */
  sessionId: string | null;
}

/** Hash route: `#/games`, `#/games/<session_id>`, `#/season`, … Anything else opens the Overview. */
export function routeFromHash(hash: string = window.location.hash): Route {
  const [rawView, rawId] = hash.replace(/^#\/?/, "").split("?")[0].trim().split("/");
  const view = (rawView ?? "").toLowerCase() as View;
  if (!rawView || !VIEW_IDS.has(view)) return { view: "overview", sessionId: null };
  const id = rawId ? decodeURIComponent(rawId) : "";
  return { view, sessionId: SESSION_ROUTES.has(view) && /^[A-Za-z0-9_.:-]{1,128}$/.test(id) ? id : null };
}

export function viewFromHash(hash: string = window.location.hash): View {
  return routeFromHash(hash).view;
}

export function hashForView(view: View, sessionId?: string | null): string {
  return sessionId && SESSION_ROUTES.has(view) ? `#/${view}/${encodeURIComponent(sessionId)}` : `#/${view}`;
}

export function syncHash(view: View, sessionId?: string | null): void {
  const next = hashForView(view, sessionId);
  if (window.location.hash === next) return;
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${next}`);
}
