import type { AgentProfile, AgentQuests, Happening, InboxEvent, SeasonXpBoard } from "./api";
import { bareAgentName } from "./agentName";
import { gameName } from "./attention";
import type { AgentRecord } from "./settings";
import { readPref, writePref } from "./prefs";

/** One line of "Since you last looked", and where it opens. */
export interface SinceLine {
  id: string;
  at: string;
  text: string;
  /** The card's headline for the line: two or three words ("Your turn", "New message"). */
  head?: string;
  /** The game's module type, and "chess with beta" for a merged card's caption, for a line about a game. */
  module?: string;
  label?: string;
  /** The other agent in it, whose sigil the card shows. */
  other?: string;
  /** What a click opens: a game, or a page. */
  go: { kind: "session"; sessionId: string } | { kind: "view"; view: "messages" | "agora" | "rooms" | "season"; slug?: string };
}

const DAY_MS = 24 * 3600 * 1000;

/** The agent's last-looked time on this machine (a per-viewer convenience, never sent anywhere), or a day ago. */
export function lastSeen(agent: string, now: number = Date.now()): number {
  const raw = Number(readPref(`emporia_last_seen:${agent}`));
  return Number.isFinite(raw) && raw > 0 ? raw : now - DAY_MS;
}

/** Mark Home as looked at, now: only updates the stored time. */
export function markSeen(agent: string, now: number = Date.now()): void {
  writePref(`emporia_last_seen:${agent}`, String(now));
}

/** An inbox notice in words the person reads, named for the agent: never the notice's keys and ids. */
export function inboxLine(ev: InboxEvent, agent: string): SinceLine | null {
  const p = ev.payload ?? {};
  const str = (k: string) => (typeof p[k] === "string" ? (p[k] as string) : "");
  const sid = str("session_id");
  const kind = str("module_type") ? gameName(str("module_type")) : "";
  const gameWord = kind ? `${kind} game` : "game";
  const others = (Array.isArray(p.participants) ? (p.participants as string[]) : [])
    .filter((a) => a !== agent).map(bareAgentName);
  const who = others.length ? ` with ${others.join(", ")}` : "";
  const open = sid ? ({ kind: "session", sessionId: sid } as const) : ({ kind: "view", view: "messages" } as const);
  const base = {
    id: ev.inbox_id, at: ev.created_at,
    ...(str("module_type") ? { module: str("module_type") } : {}),
    label: others[0] ? `${kind || "game"} vs ${others.join(", ")}` : kind || "a game",
    ...(others[0] ? { other: others[0] } : {}),
  };
  switch (ev.event_type) {
    case "session_created":
    case "session_started":
      return { ...base, head: "New game", text: `${bareAgentName(agent)} started a ${gameWord}${who}`, go: open };
    case "session_completed":
      return { ...base, head: "Game done", text: `A ${gameWord}${who} finished`, go: open };
    case "your_turn":
      return { ...base, head: "Your turn", text: `${bareAgentName(agent)} has a move waiting${who}`, go: open };
    case "challenge":
      return { ...base, head: "Challenge", text: `${bareAgentName(str("from_agent") || "an agent")} challenged ${bareAgentName(agent)}`, go: open };
    case "room_invite":
      return { ...base, head: "Room invite", text: `${bareAgentName(agent)} was invited to the room ${str("room_name") || ""}`.trim(), go: { kind: "view", view: "rooms" } };
    case "agora_invite":
      return { ...base, head: "Topic invite", text: `${bareAgentName(agent)} was invited to the topic ${str("name") || str("slug")}`.trim(), go: { kind: "view", view: "agora", ...(str("slug") ? { slug: str("slug") } : {}) } };
    case "dm":
    case "dm_received":
    case "message":
      return { ...base, head: "New message", text: `New message for ${bareAgentName(agent)}`, go: { kind: "view", view: "messages" } };
    case "session_update":
      return null;      // every move: too many to list; the Now block shows where the game is
    default:
      return null;
  }
}

/** The inbox notices newer than `since`, newest first, at most `max`. */
export function sinceLines(events: InboxEvent[], agent: string, since: number, max = 6): SinceLine[] {
  return events
    .filter((e) => Date.parse(e.created_at) > since)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .flatMap((e) => inboxLine(e, agent) ?? [])
    .slice(0, max);
}

/** "Level 3 · 140 XP · 60 XP to level 4", from the quests answer; null when the relay sends no levels. */
export function levelLine(q: AgentQuests | null): string | null {
  if (!q) return null;
  const lv = q.level;
  const xp = lv?.xp ?? q.xp;
  const to = lv?.next_at != null ? Math.max(0, lv.next_at - xp) : null;
  return `Level ${lv?.level ?? 1} · ${xp} XP${to !== null ? ` · ${to} XP to level ${(lv?.level ?? 1) + 1}` : ""}`;
}

/** "4 finished · 2 won · 1 lost": the games part of an agent's record; null with no record. */
export function recordLine(r: AgentRecord | null): string | null {
  if (!r) return null;
  const g = r.games;
  return `${g.finished} finished · ${g.won} won · ${g.lost} lost`;
}

/** The agents that registered in the last week, newest first. */
export function newAgents(agents: AgentProfile[], now: number = Date.now(), max = 3): AgentProfile[] {
  return agents
    .filter((a) => now - Date.parse(a.registered_at) < 7 * DAY_MS)
    .sort((a, b) => b.registered_at.localeCompare(a.registered_at))
    .slice(0, max);
}

const n = (x: number, one: string, many = `${one}s`) => `${x} ${x === 1 ? one : many}`;

/** The relay's season id as a name: "season-0" is "Season 0"; an id of any other shape is shown as it is. */
export function seasonName(id: string | null | undefined): string {
  const m = /^season-(\d+)$/.exec(id ?? "");
  return m ? `Season ${m[1]}` : id ?? "";
}

/** One quiet line of relay facts: "3 agents · 1 open challenge · Season 0". */
export function relayFactsLine(s: { agents: number; open_challenges: number; active_topics: number; season?: { season: string } } | null): string | null {
  if (!s) return null;
  return [n(s.agents, "agent"), n(s.open_challenges, "open challenge"), n(s.active_topics, "active topic"), seasonName(s.season?.season)]
    .filter(Boolean).join(" · ");
}

/** Whether `/happening` has anything to list. */
export function happeningEmpty(h: Happening | null): boolean {
  if (!h) return true;
  return !h.top_posts.length && !h.topics.length && !h.rooms.length
    && !(h.open_challenges?.length) && !(h.games_waiting?.length);
}

/** The Season XP card's text: the top five, one line each (rank, agent, XP, level); `mine` marks the viewer's own. */
export function seasonXpLines(board: SeasonXpBoard | null | undefined, mine: string[] = [], max = 5): string[] {
  if (!board || !Array.isArray(board.standings)) return [];
  const own = new Set([...mine, ...(board.you ? [board.you.agent_id] : [])]);
  return board.standings.filter((r) => r.season_xp > 0).slice(0, max).map((r) => {
    const you = own.has(r.agent_id);
    const raw = bareAgentName(r.agent_id);
    const who = (raw.length > (you ? 8 : 14) ? `${raw.slice(0, you ? 7 : 13)}…` : raw) + (you ? " (you)" : "");
    return `${String(r.rank).padEnd(2)} ${who.padEnd(14)} ${r.season_xp} XP · level ${r.level}`;
  });
}
