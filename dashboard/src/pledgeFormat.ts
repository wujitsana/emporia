/**
 * Pledges (gamify `pledges`, src/emporia/routes/pledges.py): an agent's signed promise to a
 * counterparty, and its status as the relay computes it from the record each time it is read
 * (`pledges.resolve`). The sheet shows the agent's own list under its Word line. Read-only.
 *
 * Visibility follows the subject: a pledge on a public game is public; one on a
 * private deal is shown to its two parties only, and the dashboard names neither the
 * counterparty nor the deal for it ("a private deal"), as the sheet's Word line does.
 */
import { whenText } from "./relayFormat";

export interface Pledge {
  pledge_id: string;
  maker_id: string;
  to_agent: string;
  kind: string;
  subject_type?: string;
  subject_id?: string;
  terms?: Record<string, unknown>;
  made_at?: string;
  public?: boolean;
  status: string;
  resolved_by?: string | null;
  resolved_at?: string | null;
  evidence?: Record<string, unknown> | null;
  due_at?: string | null;
  attest_by?: string | null;
  contest_by?: string | null;
  waiting_on?: string | null;
}

const KIND_LABEL: Record<string, string> = {
  deliver_by: "deliver by", respond_within: "respond within", reveal_within: "reveal within", tell: "a tell",
  refund: "refund if unhappy",
};

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind.replace(/_/g, " ");
}

/** "✓ kept", "× broken", … — a glyph that is not the only carrier of the state (the word follows). */
export function statusMark(status: string): string {
  return status === "kept" ? "✓" : status === "broken" ? "×" : status === "disputed" ? "!" : status === "void" ? "–" : "·";
}

/** Who the pledge is with, from the agent's side: "to bob", "from carol", or "a private deal". */
export function counterparty(p: Pledge, agentId: string): string {
  if (p.public === false) return "a private deal";
  return p.maker_id === agentId ? `to ${p.to_agent}` : `from ${p.maker_id}`;
}

const secs = (v: unknown) => (typeof v === "number" ? (v >= 120 ? `${Math.round(v / 60)} min` : `${v} s`) : "");
const at = (v: unknown) => (typeof v === "string" ? whenText(v) || v : "");

/** The evidence the relay gave for the status, in a line: what the record shows. "" when none. */
export function evidenceLine(p: Pledge): string {
  const e = p.evidence ?? {};
  if (typeof e.why === "string") return e.why;
  const out: string[] = [];
  if (typeof e.attested === "string") out.push(`attested ${e.attested} by the counterparty`);
  if (typeof e.contested_at === "string") out.push(`contested ${at(e.contested_at)}`);
  if ("delivered_at" in e) out.push(e.delivered_at ? `delivered ${at(e.delivered_at)}` : "not delivered");
  if (typeof e.by === "string") out.push(`due by ${at(e.by)}`);
  if ("answered_at" in e) out.push(e.answered_at ? `answered ${at(e.answered_at)}` : "no answer");
  if (typeof e.answer === "string") out.push(e.answer);
  if (typeof e.turn_opened_at === "string") out.push(`turn opened ${at(e.turn_opened_at)}`);
  if ("revealed_at" in e && typeof e.reveals !== "number") out.push(e.revealed_at ? `revealed ${at(e.revealed_at)}` : "not revealed");
  if (typeof e.reveals === "number") out.push(`${e.reveals} reveal${e.reveals === 1 ? "" : "s"} on time`);
  if (typeof e.within_s === "number") out.push(`within ${secs(e.within_s)}`);
  if (e.tell && typeof e.tell === "object") out.push(`tell ${String((e.tell as Record<string, unknown>).tell ?? "")}`.trim());
  if ("revealed" in e && e.revealed !== undefined && typeof e.reveals !== "number") out.push("checked against the revealed move");
  if (!out.length && p.resolved_by) out.push(`by ${p.resolved_by}`);
  return out.join(" · ");
}

/** When it is due (open), else when it was settled. "" when the relay gave neither. */
export function whenLine(p: Pledge): string {
  if (p.status === "open") {
    const due = p.due_at ?? p.attest_by;
    if (due) return `due ${at(due)}`;
    return p.waiting_on ? `waiting on ${p.waiting_on === "maker" ? "the maker" : p.waiting_on}` : "open";
  }
  if (p.contest_by) return `${p.resolved_at ? `${at(p.resolved_at)} · ` : ""}contest by ${at(p.contest_by)}`;
  return p.resolved_at ? at(p.resolved_at) : "";
}

/** The agent's pledges, made and received, once each, newest first. */
export function mergePledges(...lists: Array<Pledge[] | null | undefined>): Pledge[] {
  const by = new Map<string, Pledge>();
  for (const list of lists) for (const p of list ?? []) by.set(p.pledge_id, p);
  return [...by.values()].sort((a, b) => String(b.made_at ?? "").localeCompare(String(a.made_at ?? "")));
}
