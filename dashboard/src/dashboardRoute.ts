import { NAV, type View } from "./navConfig";

const VIEW_IDS = new Set<View>(NAV.map((n) => n.id));

/** Hash route: #/fees, #/sessions, etc. */
export function viewFromHash(hash: string = window.location.hash): View {
  const raw = hash.replace(/^#\/?/, "").split("?")[0].trim().toLowerCase();
  if (raw && VIEW_IDS.has(raw as View)) return raw as View;
  return "overview";
}

export function hashForView(view: View): string {
  return view === "overview" ? "#/overview" : `#/${view}`;
}

export function syncHash(view: View): void {
  const next = hashForView(view);
  if (window.location.hash === next) return;
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${next}`);
}