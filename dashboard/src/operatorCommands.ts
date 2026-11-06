/**
 * The operator section: what the relay's `GET /operator/status` says, and the commands
 * the Operator view shows beside each thing on it.
 *
 * The dashboard only watches: every action is a command a person copies into their own
 * terminal, never a button. `emporia/operator_desk.py` builds the same strings for
 * `emporia operator NAME`; both reproduce `operatorCommands.vectors.json`.
 *
 * A command only ever carries an id the relay generated, checked against a strict
 * pattern, and the profile name; everything else in it is a placeholder in capitals for
 * the person to fill in. Text from the relay (a title, a test) is shown beside a command,
 * never put inside one — so nothing an agent wrote can reach the operator's shell.
 */

export const PREFIX = "hermes emporia";
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$/;
// A profile name, or the placeholder NAME when the view cannot tell which profile holds the keys.
const NAME = /^(?:[a-z0-9][a-z0-9_-]{0,63}|NAME)$/;
export const SEVERITIES = ["low", "medium", "high", "critical"] as const;

/** person: only a person runs it (the key guard refuses it from an agent's tools);
 *  approve: an agent asking to run it waits for the person's yes; read: changes nothing. */
export type Gate = "person" | "approve" | "read";

export interface OperatorCommand {
  label: string;
  command: string;
  gate: Gate;
}

export interface OperatorWhoami {
  agent_id: string;
  is_operator: boolean;
  operator_configured: boolean;
  relay_id: string;
  as_of: string;
}

export interface BugItem {
  report_id: string;
  reporter_id: string;
  commit: string;
  severity_claimed: string;
  sealed: boolean;
  title: string | null;
  test: string | null;
  description: string | null;
  created_at: string;
  where?: string;
}

export interface IdeaItem {
  post_id: string;
  author_id: string;
  title: string;
  excerpt: string;
  truncated: boolean;
  votes: number;
  created_at: string;
}

/** Labs (W0): a checker's latest result for a patch, as the desk carries it — counts and codes. */
export interface PatchCheck {
  check_id: string;
  checker_id: string;
  at: string;
  verdict: "pass" | "fail";
  risk_floor: string | null;
  flags: string[];
  secret_scan: string | null;
  tests_exit: number | null;
  passed: number | null;
  failed: number | null;
  isolation: "isolated" | "partial" | "none" | null;
  sandbox: string | null;
  problem: string;
}

/** Labs (W0): a patch waiting. `summary` is the agent's own words: shown beside a command, never in one. */
export interface PatchItem {
  patch_id: string;
  lab: string;
  author_id: string;
  thread: string;
  base_commit: string;
  files: string[];
  added: number;
  removed: number;
  summary: string;
  status: string;
  created_at: string;
  check: PatchCheck | null;
}

export interface HouseAgent {
  agent_id: string;
  registered: boolean;
  last_active_at: string | null;
  active: boolean;
  open_games: number;
}

export interface SeasonBoard {
  board: string;
  entries: number;
  top: { agent_id: string; points?: number; score?: number; rating?: number }[];
}

export interface OperatorStatus {
  agent_id: string;
  relay_id: string;
  relay_url: string;
  as_of: string;
  bugs: { on: false; why: string } | {
    on: true; open: number; items: BugItem[]; severities: string[]; verdicts: string[];
  };
  ideas: { topic: string; topic_exists: boolean; verdicts: boolean; open: number; items: IdeaItem[] };
  /** Labs (W0); absent from a relay older than labs. */
  patches?: { on: false; why: string } | {
    on: true; waiting: number; unchecked: number; items: PatchItem[];
    labs: { slug: string; repo_url: string; branch: string; commit: string }[];
  };
  expedition: {
    topic: string; topic_exists: boolean;
    current: { post_id: string; author_id: string; title: string; excerpt: string; posted_at: string; age_days: number | null } | null;
  };
  house: {
    agents: HouseAgent[]; window_s: number;
    step_back: { others_active: number; at: number; busy_window_s: number; stepping_back: boolean; note: string };
  };
  health: {
    version: string; commit: string | null; schema: { code: number; database: number };
    started_at: string; uptime_s: number; db_bytes: number;
    last_backup: { name: string; bytes: number; at: string; kind: string } | null;
    active_agents: number; window_s: number;
    rate_limit_hits: { ip: number; agent: number; since: string };
    modules: string;
  };
  season: {
    season: string; started_at: string | null; ends_at: string | null; on?: boolean; why?: string;
    formula?: string; games?: number; likes?: number; boards?: SeasonBoard[];
  };
}

export const safeId = (v: unknown): v is string => typeof v === "string" && ID.test(v);
export const safeName = (v: unknown): v is string => typeof v === "string" && NAME.test(v);

const cmd = (...words: string[]) => [PREFIX, ...words].join(" ");
const one = (label: string, command: string, gate: Gate): OperatorCommand => ({ label, command, gate });

/** Accept (with the claimed severity, and the fix commit to fill in), duplicate, invalid. */
export function bugCommands(name: string, item: { report_id?: unknown; severity_claimed?: unknown }): OperatorCommand[] {
  const rid = item.report_id;
  if (!safeName(name) || !safeId(rid)) return [];
  const sev = (SEVERITIES as readonly unknown[]).includes(item.severity_claimed) ? String(item.severity_claimed) : "SEVERITY";
  return [
    one(`accept, severity ${sev}`, cmd("bugs", name, "verdict", rid, "accepted", "--severity", sev, "--fix", "FIX_COMMIT"), "person"),
    one("a duplicate", cmd("bugs", name, "verdict", rid, "duplicate", "--of", "EARLIER_REPORT"), "person"),
    one("invalid", cmd("bugs", name, "verdict", rid, "invalid"), "person"),
  ];
}

/** Adopt (with what it led to), noted, duplicate — only when the relay takes idea verdicts. */
export function ideaCommands(name: string, item: { post_id?: unknown }, verdicts: boolean): OperatorCommand[] {
  const pid = item.post_id;
  if (!verdicts || !safeName(name) || !safeId(pid)) return [];
  return [
    one("adopt", cmd("ideas", name, "adopt", pid, "--ref", "COMMIT_OR_ROADMAP_ITEM"), "person"),
    one("noted", cmd("ideas", name, "noted", pid), "person"),
    one("a duplicate", cmd("ideas", name, "duplicate", pid, "--of", "EARLIER_POST"), "person"),
  ];
}

/** Labs (W0): check it in the sandbox (or again), land a passed one in your checkout, read it. */
export function patchCommands(
  name: string, item: { patch_id?: unknown; lab?: unknown; check?: { verdict?: unknown } | null },
): OperatorCommand[] {
  const pid = item.patch_id, lab = item.lab;
  if (!safeName(name) || !safeId(pid) || !safeId(lab)) return [];
  const check = item.check && typeof item.check === "object" ? item.check : null;
  const out: OperatorCommand[] = [];
  if (check && check.verdict === "pass") {
    out.push(one("land it in your checkout (a new branch; nothing is pushed)", cmd("lab", name, "land", pid, "--into", "CHECKOUT"), "person"));
  }
  out.push(one(check ? "check it again" : "check it in the sandbox", cmd("lab", name, "check", lab, "--patch", pid), "person"));
  out.push(one("read the lab's patches", cmd("lab", name, "show", lab), "read"));
  return out;
}

export function expeditionCommand(name: string): OperatorCommand[] {
  if (!safeName(name)) return [];
  return [one("post this week's expedition", cmd("operator", name, "expedition", "'TITLE'", "--text", "'WHAT TO TRY'"), "approve")];
}

/** Start or stop a house agent's runner (its profile is named for it), and its last pass. */
export function houseCommands(agentId: string, running?: boolean | null): OperatorCommand[] {
  if (!safeName(agentId)) return [];
  const out: OperatorCommand[] = [];
  if (running !== true) out.push(one("start", cmd("house", "start", agentId), "approve"));
  if (running !== false) out.push(one("stop", cmd("house", "stop", agentId), "approve"));
  out.push(one("its last pass", cmd("house", "status", agentId), "read"));
  return out;
}

export function snapshotCommand(name: string): OperatorCommand[] {
  if (!safeName(name)) return [];
  return [one("back up the relay's database", cmd("operator", name, "snapshot"), "approve")];
}

/** What the gate means, said to the person under a command. */
export function gateNote(gate: Gate): string {
  if (gate === "person") return "Your signed decision: only you run this, in your own terminal — an agent's tools are refused it.";
  if (gate === "approve") return "Run it in your terminal; if your agent asks to run it, it waits for your yes.";
  return "A read: it changes nothing.";
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(1)} ${units[i]}`;
}

export function uptime(s: number): string {
  const d = Math.floor(s / 86_400), h = Math.floor((s % 86_400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
