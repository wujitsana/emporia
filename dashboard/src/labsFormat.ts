import type { LabBoardRow, LabReport } from "./api";

/** A stable id or agent name, short enough for a line: names as they are, long ids cut. */
export function shortWho(id: string | null | undefined): string {
  const s = (id ?? "").trim();
  if (!s) return "unknown agent";
  return s.length > 20 ? `${s.slice(0, 12)}…` : s;
}

/** Who a report credits: the author, each reviewer, and how many ideas it adopted, as one line. */
export function creditLine(r: LabReport): string {
  const author = shortWho(r.author_id || r.author_stable_id);
  const reviewers = (r.reviewer_ids.length ? r.reviewer_ids : r.reviewer_stable_ids).map(shortWho);
  const parts = [`author ${author}`];
  if (reviewers.length) parts.push(`${reviewers.length === 1 ? "reviewer" : "reviewers"} ${reviewers.join(", ")}`);
  if (r.ideas.length) parts.push(`${r.ideas.length} idea${r.ideas.length === 1 ? "" : "s"} adopted`);
  return parts.join(" · ");
}

/** `gh_pr:owner/repo#12` → `PR #12`; an issue likewise; anything else as it is. */
export function refText(ref: string): string {
  const m = /^gh_(pr|issue):.+#(\d+)$/.exec(ref);
  if (!m) return ref;
  return `${m[1] === "pr" ? "PR" : "Issue"} #${m[2]}`;
}

/** The repo link only when it is a plain https address; anything else is shown as text. */
export function safeRepoHref(url: string): string | null {
  return /^https:\/\/[^\s]+$/.test(url) ? url : null;
}

/** The lab's room, in words: who can read its threads. */
export function roomLine(room: { type: string; gate: string; encrypted: boolean }): string {
  const open = room.type === "public" && room.gate === "open";
  return `${open ? "open room" : "private room"}${room.encrypted ? ", encrypted" : ""}`;
}

/** A board row in a line: its score, then what it is made of (fixes landed, bugs fixed, ideas adopted...). */
export function boardLine(r: LabBoardRow): string {
  const parts: string[] = [];
  const add = (n: number | undefined, one: string, many = one) => { if (n) parts.push(`${n} ${n === 1 ? one : many}`); };
  add(r.fix_landed, "fix landed", "fixes landed");
  add(r.fix_reviewed, "fix reviewed", "fixes reviewed");
  add(r.bug_fixed, "bug fixed", "bugs fixed");
  add(r.bug_confirmed, "bug confirmed", "bugs confirmed");
  add(r.idea_adopted, "idea adopted", "ideas adopted");
  return `${r.score} pts${parts.length ? ` · ${parts.join(", ")}` : ""}`;
}
