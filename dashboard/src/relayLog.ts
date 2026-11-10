/**
 * P256/P281: the relay's transparency log, as the dashboard shows it — read only.
 *
 * Two sources, never mixed up:
 * - the relay's own signed state (its public GET /log/state): whether it keeps a log, its size, when it
 *   was last signed, which log (epoch) it is;
 * - this agent's own check of that log, kept on this machine beside its keys (logcheck.py,
 *   log_states.json), read through the local console (GET /log-check/<agent>). Where no console is in
 *   front, only the relay's side shows. The dashboard never starts a check: the agent does.
 */
import { consoleUrl, httpFetch, VIA_CONSOLE } from "./transport";

export interface LogState {
  size: number;
  root: string;
  at: string;
  epoch?: number;
}

/** logcheck.kept_summary: numbers, a status and its words — no key, no signature. */
export interface KeptCheck {
  status: string;
  ok: boolean;
  detail: string;
  checked_at: string;
  size: number | null;
  epoch: number | null;
  state_at: string;
  failures: number;
}

export interface LogCheckReport {
  relay: string;
  kept: KeptCheck | null;
}

export const RELAY_LOG_ABOUT =
  "An append-only public log of what this relay recorded, signed every few minutes. The agent checks that " +
  "it only ever grows, so the relay can't rewrite history or show different people different versions.";

/** The agent's own last check, from the console; null where no console is in front (or it did not answer). */
export async function fetchLogCheck(agentId: string | null): Promise<LogCheckReport | null> {
  if (!VIA_CONSOLE || !agentId) return null;
  const r = await httpFetch(consoleUrl(`/log-check/${encodeURIComponent(agentId)}`));
  if (!r.ok) return null;
  return r.json();
}

/** "just now", "4 min ago", "3 h ago", "2 days ago"; "" for a time that does not read. */
export function ago(iso: string, now: number = Date.now()): string {
  const t = Date.parse(iso);
  if (!iso || Number.isNaN(t)) return "";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 48 * 3600) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86_400)} days ago`;
}

export type CheckTone = "ok" | "none" | "info" | "warn";

export interface RelayLogView {
  /** Whether the relay keeps a log. */
  on: boolean;
  /** The relay's side, in short facts ("1,204 entries", "signed 3 min ago", "log #2"). */
  facts: string[];
  /** This agent's own check; null where it cannot be read (no console). */
  check: { tone: CheckTone; text: string } | null;
}

/** Statuses of a check that caught the relay out (logcheck.check), or that can no longer be checked. */
const PROBLEM = new Set(["fail", "other_log", "stopped"]);

export function relayLogView(
  relay: { on: boolean; state: LogState | null },
  check: LogCheckReport | null,
  now: number = Date.now(),
): RelayLogView {
  const facts: string[] = [];
  if (!relay.on) {
    facts.push("This relay keeps no transparency log");
  } else if (!relay.state || typeof relay.state.size !== "number") {
    facts.push("Log on · no state signed yet");
  } else {
    const s = relay.state;
    facts.push(`${s.size.toLocaleString("en-US")} ${s.size === 1 ? "entry" : "entries"}`);
    const when = ago(s.at, now);
    if (when) facts.push(`signed ${when}`);
    if ((s.epoch ?? 1) > 1) facts.push(`log #${s.epoch}`);
  }
  return { on: relay.on, facts, check: check ? checkLine(check.kept, relay.on, now) : null };
}

function checkLine(kept: KeptCheck | null, on: boolean, now: number): RelayLogView["check"] {
  if (!kept) return on ? { tone: "none", text: "The agent has not checked it yet" } : null;
  const when = ago(kept.checked_at, now);
  const at = when ? ` · ${when}` : "";
  if (PROBLEM.has(kept.status)) return { tone: "warn", text: `The agent's check found a problem: ${kept.detail}${at}` };
  if (kept.failures > 0) {
    return { tone: "warn", text: `The agent recorded ${kept.failures === 1 ? "a problem" : `${kept.failures} problems`} `
      + `with this log before (emporia log verify shows them)${at}` };
  }
  if (kept.status === "new_log") return { tone: "info", text: `The agent checked it: a new log, with a signed hand-over${at}` };
  if (kept.status === "ok") return { tone: "ok", text: `The agent checked it: consistent ✓${at}` };
  if (kept.status === "first") return { tone: "ok", text: `The agent checked it: first look, kept to compare next time${at}` };
  if (kept.status === "unreachable") return { tone: "none", text: `The agent's last check could not reach the log${at}` };
  if (kept.status === "off") return on ? { tone: "none", text: "The agent has not checked it since it was turned on" } : null;
  return { tone: "none", text: `The agent's last check: ${kept.detail || kept.status}${at}` };
}
