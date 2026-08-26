import type { Session } from "./api";

export type SessionTab = "live" | "history";

export interface DeepLinkedSession {
  session: Session;
  tab: SessionTab;
}

/**
 * Resolve a deep-linked session id against both session lists, reporting which
 * tab it lives on.
 *
 * P36-3: GamesView used to search only `shown` — the currently selected tab's
 * list, already narrowed by the game-type rail. Since the tab defaults to
 * "live", following a link to a finished game selected nothing and reported
 * nothing; the view just stayed where it was. Searching both lists is most of
 * the fix, and returning the tab is the rest: finding the session is useless if
 * the caller keeps rendering the other list.
 *
 * `null` means "not in either list" — which is still reachable, since the rail
 * filters both and either can exceed a page. That is a signal to fetch the
 * session by id, not a reason to do nothing.
 */
export function resolveDeepLinkedSession(
  sessionId: string | null,
  live: Session[],
  history: Session[],
): DeepLinkedSession | null {
  if (!sessionId) return null;
  const inLive = live.find((s) => s.session_id === sessionId);
  if (inLive) return { session: inLive, tab: "live" };
  const inHistory = history.find((s) => s.session_id === sessionId);
  if (inHistory) return { session: inHistory, tab: "history" };
  return null;
}
