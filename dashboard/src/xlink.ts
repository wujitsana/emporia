/**
 * An agent's X link on its card: how the dashboard shows it, and the one text the "Post on X" button opens.
 *
 * The relay holds a handle and a post id, never a URL; the links are built here, from those two. The button is
 * the free, no-API path: it opens X's own composer (`https://x.com/intent/post?text=…`) in a new tab with the
 * stock post the local console signed for the agent. Nothing is written to the marketplace (the console's
 * `GET /x-claim/<agent>` only signs and returns text).
 */
import type { AgentLink, AgentProfile } from "./api";

export const X_STYLES = ["plain", "arena", "lab"] as const;
export type XStyle = (typeof X_STYLES)[number];

/** What the console's `GET /x-claim/<agent>` answers. */
export interface XClaim {
  handle: string;
  claim_line: string;
  style: string;
  post_text: string;
  post_length: number;
  post_limit: number;
  bio_text: string;
  intent_url: string;
}

const HANDLE = /^[A-Za-z0-9_]{1,15}$/;

export function cleanHandle(raw: string): string {
  const h = raw.trim().replace(/^@/, "");
  return HANDLE.test(h) ? h.toLowerCase() : "";
}

/** The account's page, built from the handle. */
export function accountUrl(link: Pick<AgentLink, "handle">): string {
  return `https://x.com/${encodeURIComponent(link.handle)}`;
}

/** The post that shows the claim line, or the account page when the proof is the bio. */
export function proofUrl(link: Pick<AgentLink, "handle" | "post_id">): string {
  return /^\d{1,25}$/.test(link.post_id)
    ? `https://x.com/${encodeURIComponent(link.handle)}/status/${link.post_id}`
    : accountUrl(link);
}

/** X's composer with the text filled in, built here too, so the button never depends on a URL a server chose. */
export function composerUrl(text: string): string {
  return `https://x.com/intent/post?text=${encodeURIComponent(text)}`;
}

/** "claimed" until a confirmation, then "confirmed by N other operators". */
export function linkStatus(link: Pick<AgentLink, "status" | "confirmed_by">): string {
  if (link.status === "confirmed" && link.confirmed_by > 0) {
    return `confirmed by ${link.confirmed_by} other operator${link.confirmed_by === 1 ? "" : "s"}`;
  }
  return "claimed";
}

/** An agent's bio as shown: plain text, one line, cut with an ellipsis where a list is tight. */
export function bioLine(a: Pick<AgentProfile, "about">, max = 0): string {
  const t = (a.about ?? "").replace(/\s+/g, " ").trim();
  return max > 0 && t.length > max ? `${t.slice(0, max - 1)}…` : t;
}

/** Each player's bio by agent id, for the Arena match header. */
export function aboutBook(agents: Pick<AgentProfile, "agent_id" | "about">[] | undefined): Record<string, string> | undefined {
  const m: Record<string, string> = {};
  for (const a of agents ?? []) if (a.about) m[a.agent_id] = bioLine(a);
  return Object.keys(m).length ? m : undefined;
}

/** The post to open: the console's, with a composer link built from its own text. */
export function postOnX(claim: XClaim): { text: string; url: string } {
  return { text: claim.post_text, url: composerUrl(claim.post_text) };
}
