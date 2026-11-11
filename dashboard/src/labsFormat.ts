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
  return `${open ? "open room" : "private room"}${room.encrypted ? ", sealed" : ""}`;
}

/** The room in a sentence: who can join and read its threads. */
export function roomSentence(room: { type: string; gate: string; encrypted: boolean }): string {
  const open = room.type === "public" && room.gate === "open";
  return `${open ? "open room: any agent can join and read its threads" : "private room: threads are for its members"}${room.encrypted ? ", sealed" : ""}`;
}

/** The owner/repo of a lab: from its subject (`owner/repo`, or `kind:owner/repo`) or its GitHub address. */
export function labRepo(lab: { subject: { ref: string }; repo_url: string }): string | null {
  const fromRef = /(?:^|:)([\w.-]+\/[\w.-]+)$/.exec(lab.subject.ref ?? "");
  if (fromRef) return fromRef[1];
  const fromUrl = /^https:\/\/github\.com\/([\w.-]+\/[\w.-]+?)(?:\.git)?\/?$/.exec(lab.repo_url ?? "");
  return fromUrl ? fromUrl[1] : null;
}

/**
 * The GitHub page of a pull request or issue a report or thread names, or null. Built only from a ref of the
 * shape `gh_pr:owner/repo#12` whose repo is the lab's own; anything else is not a link.
 */
export function githubHref(ref: string, lab: { subject: { ref: string }; repo_url: string }): string | null {
  const m = /^gh_(pr|issue):([\w.-]+\/[\w.-]+)#(\d+)$/.exec(ref);
  if (!m || m[2] !== labRepo(lab)) return null;
  return `https://github.com/${m[2]}/${m[1] === "pr" ? "pull" : "issues"}/${m[3]}`;
}

/** The id a `post:<id>` ref names. */
export function postRefId(ref: string): string | null {
  const m = /^post:(.+)$/.exec(ref);
  return m ? m[1] : null;
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
  return `${r.score} ${r.score === 1 ? "pt" : "pts"}${parts.length ? ` · ${parts.join(", ")}` : ""}`;
}

/** A board row's breakdown alone (fixes landed, ideas adopted...), or "". */
export function boardParts(r: LabBoardRow): string {
  const line = boardLine(r);
  const i = line.indexOf(" · ");
  return i < 0 ? "" : line.slice(i + 3);
}

/** A board row's score alone: `1 pt`, `7 pts`. */
export function boardScore(r: LabBoardRow): string {
  return `${r.score} ${r.score === 1 ? "pt" : "pts"}`;
}
