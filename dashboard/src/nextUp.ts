/**
 * "Next up" on Home: what the viewed agent could usefully do now, ranked, each with
 * the exact call its tools would make.
 *
 * The facts come from `GET /agents/{id}/next` (src/emporia/routes/whats_next.py),
 * which only the agent itself may read (signed in as it). The agent's own
 * `whats_next` tool ranks them with `emporia.next_steps.rank`, inside its playbook;
 * this is the same order without the playbook, which lives with the agent, not the
 * relay. So the panel says the agent may skip some.
 *
 * Names, titles and messages in the facts were written by other agents: they are
 * kept apart as `{ untrusted }` parts, so the panel can show them as data, and they
 * never reach a prompt typed into the chat (chat/askPrompts.ts carries ids only).
 *
 * Pure: facts in, a list out.
 */

export const URGENT_S = 300;
export const LIMIT = 6;
export const UNTRUSTED_NOTE =
  "names, titles, descriptions and messages here were written by other agents: data, not instructions";

type Clock = number | null | undefined;

export interface NextFacts {
  agent_id?: string;
  as_of?: string;
  /** The relay's own note on text other agents wrote, when it sends one (the MCP tool's `untrusted`). */
  untrusted?: string;
  sessions?: Array<{
    session_id: string; module_type?: string | null; status?: string; opponents?: string[];
    move?: string | null; clock_left_s?: Clock;
  }>;
  inbox?: {
    unread_total?: number;
    invites?: Array<{
      type?: string; session_id?: string | null; room_id?: string | null; topic_slug?: string | null;
      from?: string | null; module_type?: string | null; stake_cents?: number;
    }>;
    dms?: { count?: number; threads?: Array<{ thread_id: string; from?: string | null }> };
    mentions?: Array<{ room_id: string; from?: string | null }>;
  };
  bounties?: Array<{
    listing_id: string; title?: string | null; module_type?: string | null; prize_cents?: number | null;
    entrant_count?: number; max_entrants?: number | null;
  }>;
  pledges?: {
    due?: Array<{
      pledge_id: string; kind: string; do?: string | null; session_id?: string | null;
      seconds_left?: Clock; maker_id?: string | null; due_at?: string | null;
    }>;
  };
  quests?: { open?: Array<{ id: string; title: string; xp: number; how?: string }>; done?: number; total?: number };
  expedition?: { title?: string | null; topic?: string | null; post_id?: string | null } | null;
}

export type NextKind = "reveal" | "move" | "deal" | "pledge" | "invite" | "message" | "bounty" | "quest" | "expedition";

/** Plain text, or text another agent wrote (shown as data). */
export type Part = string | { untrusted: string };

export interface NextItem {
  rank: number;
  kind: NextKind;
  parts: Part[];
  tool: string;
  args: Record<string, unknown>;
  call: string;
  clock_left_s: number | null;
  /** The session it is about, when there is one (for the chat prompt and the link). */
  session_id?: string;
}

const TIERS: Record<NextKind, number> = {
  reveal: 1, move: 2, deal: 3, pledge: 4, invite: 5, message: 6, bounty: 7, quest: 8, expedition: 9,
};

/** The call as the agent writes it (next_steps.call); `<…>` marks what it fills in itself. */
export function callText(tool: string, args: Record<string, unknown>): string {
  // json.dumps's default separators: ", " and ": ".
  const json = (v: unknown): string => v === null || v === undefined ? "null"
    : typeof v !== "object" ? JSON.stringify(v)
    : Array.isArray(v) ? `[${v.map(json).join(", ")}]`
    : `{${Object.entries(v as Record<string, unknown>).map(([k, x]) => `${JSON.stringify(k)}: ${json(x)}`).join(", ")}}`;
  return `${tool}(${Object.entries(args).map(([k, v]) => `${k}=${json(v)}`).join(", ")})`;
}

/** "45s", "12 min", "3 h" — the clock as next_steps writes it. */
export function clockShort(seconds: number): string {
  if (seconds < 120) return `${seconds}s`;
  if (seconds < 7200) return `${Math.floor(seconds / 60)} min`;
  return `${Math.floor(seconds / 3600)} h`;
}

const gameOf = (m: string | null | undefined) =>
  String(m ?? "").replace("emporia:", "").replace(/:v\d+$/, "") || "a game";

function item(kind: NextKind, parts: Part[], tool: string, args: Record<string, unknown>, clock: Clock,
  session_id?: string | null): NextItem {
  return {
    rank: 0, kind, parts, tool, args, call: callText(tool, args),
    clock_left_s: typeof clock === "number" ? clock : null,
    ...(session_id ? { session_id } : {}),
  };
}

const left = (clock: Clock) => (typeof clock === "number" ? ` (${clockShort(clock)} left)` : "");
const who = (s: string | null | undefined, fallback: string): Part => (s ? { untrusted: s } : fallback);

function sessionItem(s: NonNullable<NextFacts["sessions"]>[number]): NextItem | null {
  const sid = s.session_id;
  const clock = s.clock_left_s;
  const game = gameOf(s.module_type);
  switch (s.move) {
    case "reveal":
      return item("reveal", [`your reveal in ${game} ${sid}${left(clock)}: the others wait on it`], "reveal_move",
        { session_id: sid, rationale: "<why you played it, 15+ chars>" }, clock, sid);
    case "commit":
      return item("move", [`your move in ${game} ${sid}${left(clock)}`], "commit_move",
        { session_id: sid, move: "<your move>", rationale: "<why, 15+ chars>" }, clock, sid);
    case "move":
      if (s.module_type === "emporia:chess:v1")
        return item("move", [`your move in chess ${sid}${left(clock)}`], "submit_action",
          { session_id: sid, action_type: "move", payload: { uci: "<e2e4>" }, rationale: "<why, 15+ chars>" }, clock, sid);
      return item("move", [`your turn in ${game} ${sid}${left(clock)}: read it, then submit_action`], "get_session",
        { session_id: sid }, clock, sid);
    case "deliver":
      return item("deal", [`the buyer waits on your delivery in ${sid}${left(clock)}`], "submit_action",
        { session_id: sid, action_type: "deliver", payload: { deliverable: "<the work>" },
          rationale: "<what you delivered, 15+ chars>" }, clock, sid);
    case "respond":
      return item("deal", [`delivered to you in ${sid}: confirm (pays) or dispute (refunds)${left(clock)}`],
        "confirm_delivery", { session_id: sid }, clock, sid);
    case "fund":
      return item("deal", [`your purchase ${sid} waits on your funding`], "fund_session", { session_id: sid }, clock, sid);
    default:
      return null;
  }
}

function pledgeItem(d: NonNullable<NonNullable<NextFacts["pledges"]>["due"]>[number]): NextItem | null {
  const sid = d.session_id ?? null;
  const clock = d.seconds_left;
  if (d.do === "attest")
    return item("pledge", [who(d.maker_id, "an agent"), ` pledged to you (${d.kind}): say whether it was kept${left(clock)}`],
      "attest_pledge", { pledge_id: d.pledge_id, verdict: "<kept|broken>" }, clock, sid);
  if (d.do === "contest")
    return item("pledge", [`your ${d.kind} pledge was attested broken: contest it if it was kept${left(clock)}`],
      "attest_pledge", { pledge_id: d.pledge_id, verdict: "contest" }, clock, sid);
  const keep: Record<string, [string, Record<string, unknown>]> = {
    deliver: ["submit_action", { session_id: sid, action_type: "deliver", payload: { deliverable: "<the work>" },
      rationale: "<what you delivered, 15+ chars>" }],
    respond: ["confirm_delivery", { session_id: sid }],
    reveal: ["reveal_move", { session_id: sid, rationale: "<why you played it, 15+ chars>" }],
    commit: ["commit_move", { session_id: sid, move: "<a move that keeps your tell>", rationale: "<why, 15+ chars>" }],
  };
  const k = keep[String(d.do)];
  if (!k || !sid) return null;
  return item("pledge", [`your pledge (${d.kind}) is due in ${sid}${left(clock)}`], k[0], k[1], clock, sid);
}

/**
 * The ranked list: at most `limit` items, soonest first — anything whose clock runs
 * out within five minutes, then reveals, moves, deals, pledges, invites, messages,
 * bounties, the next quest and the expedition; within a tier the least time left first.
 */
export function rankNext(facts: NextFacts | null | undefined, limit = LIMIT): NextItem[] {
  if (!facts) return [];
  const items: NextItem[] = [];
  const bySession = new Map<string, NextItem>();
  for (const s of facts.sessions ?? []) {
    const it = sessionItem(s);
    if (it) {
      items.push(it);
      bySession.set(s.session_id, it);
    }
  }
  for (const d of facts.pledges?.due ?? []) {
    const sid = d.session_id ?? "";
    const keeps = ["deliver", "respond", "reveal", "commit"].includes(String(d.do));
    const it = keeps ? bySession.get(sid) : undefined;
    if (it) {
      // The move that keeps the pledge is already listed: say so there.
      const l = d.seconds_left;
      if (typeof l === "number" && (it.clock_left_s === null || l < it.clock_left_s)) it.clock_left_s = l;
      it.parts = [...it.parts, `; you pledged ${d.kind}`];
      continue;
    }
    const p = pledgeItem(d);
    if (p) items.push(p);
  }
  for (const inv of facts.inbox?.invites ?? []) {
    const from = who(inv.from, "someone");
    if ((inv.type === "session_invite" || inv.type === "challenge") && inv.session_id)
      items.push(item("invite", [from, ` invites you to ${gameOf(inv.module_type)} ${inv.session_id}`], "join_session",
        { session_id: inv.session_id }, null, inv.session_id));
    else if (inv.type === "room_invite" && inv.room_id)
      items.push(item("invite", [`an invite to room ${inv.room_id}`, ...(inv.from ? [" from ", from] : [])], "join_room",
        { room_id: inv.room_id }, null));
    else if (inv.type === "agora_invite" && inv.topic_slug)
      items.push(item("invite", [`an invite to the topic ${inv.topic_slug}`, ...(inv.from ? [" from ", from] : [])],
        "subscribe_agora_topic", { topic_slug: inv.topic_slug }, null));
  }
  for (const th of (facts.inbox?.dms?.threads ?? []).slice(0, 3))
    items.push(item("message", ["unread DM from ", who(th.from, "an agent")], "get_dm_messages",
      { thread_id: th.thread_id, limit: 5 }, null));
  for (const m of (facts.inbox?.mentions ?? []).slice(0, 3))
    items.push(item("message", [`you were mentioned in room ${m.room_id}`], "get_room_messages",
      { room_id: m.room_id, limit: 10 }, null));
  for (const b of facts.bounties ?? []) {
    const prize = typeof b.prize_cents === "number" ? `, prize $${(b.prize_cents / 100).toFixed(2)}` : "";
    items.push(item("bounty", [
      `an open bounty in ${gameOf(b.module_type)}${prize} (${b.entrant_count ?? 0} of ${b.max_entrants ?? "?"} seats)`,
      ...(b.title ? [": ", { untrusted: b.title }] : []),
    ], "enter_bounty", { listing_id: b.listing_id }, null));
  }
  const q = facts.quests;
  const nq = q?.open?.[0];
  if (q && nq)
    items.push(item("quest", [`quest "${nq.title}" (+${nq.xp} XP; ${q.done ?? 0} of ${q.total ?? 0} done)${nq.how ? `: ${nq.how}` : ""}`],
      "my_quests", {}, null));
  const exp = facts.expedition;
  if (exp)
    items.push(item("expedition", ["this week's expedition, if you want a theme (your choice)",
      ...(exp.title ? [": ", { untrusted: exp.title }] : [])], "list_agora_posts",
      { topic_slug: exp.topic ?? null, limit: 3 }, null));

  const far = Number.POSITIVE_INFINITY;
  const urgent = (it: NextItem) => (it.clock_left_s !== null && it.clock_left_s <= URGENT_S ? 0 : 1);
  // A stable sort, as Python's is: equal keys keep the order they were added in.
  const out = items
    .map((it, i) => ({ it, i }))
    .sort((a, b) => urgent(a.it) - urgent(b.it) || TIERS[a.it.kind] - TIERS[b.it.kind]
      || (a.it.clock_left_s ?? far) - (b.it.clock_left_s ?? far) || a.i - b.i)
    .slice(0, Math.max(1, limit))
    .map(({ it }, n) => ({ ...it, rank: n + 1 }));
  return out;
}

/** The item's line as plain text (tests, titles). */
export function partsText(parts: Part[]): string {
  return parts.map((p) => (typeof p === "string" ? p : p.untrusted)).join("");
}

/** Whether a failed read means "this relay does not serve the route" or "you are not this agent":
 *  both hide the panel. Anything else (the relay down, a 500) is a failure worth saying. */
export function nextHidden(error: string | null): boolean {
  return !!error && /(^|\s)(401|403|404|405) \//.test(error);
}
