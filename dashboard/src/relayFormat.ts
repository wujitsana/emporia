/**
 * Display helpers for relay URLs.
 *
 * P42: there were two `relayHost` functions — one in RelayStrip.tsx (used by
 * FederationSection) and one added in P40 in discoveryFormat.ts (used for a
 * lobby challenge's `origin_relay`). Same job, different fallbacks: one stripped
 * the protocol and truncated at 48, the other truncated at 24 and returned
 * "local" for an empty value. Two names for one fact drifting apart is the exact
 * shape this codebase has spent several passes removing, and P40 introduced this
 * one while doing that removal elsewhere.
 *
 * One function now. The "local" default belongs to the caller that has a notion
 * of local, not to URL formatting.
 */
const MAX_HOST_CHARS = 48;

export function relayHost(url?: string | null): string {
  if (!url) return "";
  let host: string;
  try {
    host = new URL(url).host || url;
  } catch {
    // Not an absolute URL. This value can arrive from a federated peer, i.e.
    // from outside this relay's control, and a throw during render takes down
    // the whole view rather than one cell.
    host = url.replace(/^https?:\/\//, "");
  }
  // Capped on BOTH paths. `https://` + 300 characters is a perfectly valid URL,
  // so the parsed branch can return an unbounded host straight into a table
  // cell — only the malformed branch used to be truncated, which is the wrong
  // half to guard.
  return host.slice(0, MAX_HOST_CHARS);
}

/** P228: when something happened, readable at a glance — the time alone for
 *  today, the date as well for anything older (chat lines showed a bare clock
 *  time, so yesterday's 21:04 read like today's). "" for a missing or bad value. */
export function whenText(iso?: string | null, now: Date = new Date()): string {
  if (!iso) return "";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "";
  const time = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (t.toDateString() === now.toDateString()) return time;
  const date = t.toLocaleDateString([], {
    month: "short", day: "numeric",
    ...(t.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  });
  return `${date}, ${time}`;
}
