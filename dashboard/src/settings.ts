/** P178: what the local console's GET /settings returns, and how the view groups it. */
export type SettingKind = "switch" | "choice" | "number" | "money" | "secret" | "list" | "text";

export interface SettingItem {
  group: string;
  label: string;
  value: string;
  about: string;
  command: string;
  level: "ok" | "warn" | "danger";
  person_only: boolean;
  /** How Hermes's own form would draw it (cli.setting_item). Absent on older reports and the relay's. */
  kind?: SettingKind;
  /** A switch's "on"/"off", or the option chosen. */
  current?: string;
  options?: string[];
  /** The option setup recommends. */
  recommended?: string;
  /** Nobody changed it: it is the default. */
  is_default?: boolean;
  /** The profile setting behind it, only when Hermes's own dashboard can change it. */
  env?: string;
}

export interface SettingsReport {
  agents: { agent_id: string; profile: string; items: SettingItem[] }[];
  machine: SettingItem[];
}

/** Items in their groups, in first-seen order. */
export function groupItems(items: SettingItem[]): [string, SettingItem[]][] {
  const groups = new Map<string, SettingItem[]>();
  for (const it of items) groups.set(it.group, [...(groups.get(it.group) ?? []), it]);
  return [...groups.entries()];
}

/** The Settings view's one line at the top: it shows, and nothing in it changes a setting. */
export const READ_ONLY_NOTE =
  "Read-only here. Change a setting in Hermes's settings page or with the command shown; the agent can't change these.";

/** The one command that lists and changes an agent's own limits and behaviour, in the person's terminal. */
export const settingsCommand = (profile: string) => `emporia agent settings ${profile}`;

/** How to draw an item: what the console said, or (for the relay's plain items) what its value reads as. */
export function kindOf(it: SettingItem): SettingKind {
  if (it.kind) return it.kind;
  const v = it.value.trim().toLowerCase();
  if (v === "set" || v === "not set") return "secret";
  if (v === "on" || v === "off") return "switch";
  return "text";
}

/** A secret is shown as set or not set, and never as anything else, whatever the report holds. */
export function secretState(it: SettingItem): "set" | "not set" {
  return (it.current ?? it.value).trim().toLowerCase() === "set" ? "set" : "not set";
}

/** A switch's state: on or off. */
export function switchOn(it: SettingItem): boolean {
  return /^(on|1|true|yes|unlocked)$/i.test((it.current ?? it.value).trim());
}

/** Offered "Change in Hermes": a profile setting Hermes's dashboard edits, unless only a person may change
 *  it with its command, which carries the warning that changing it needs. */
export function changeInHermes(it: SettingItem): boolean {
  return Boolean(it.env) && !it.person_only;
}

/** P179: the relay operator's overview (GET /operator/overview on the relay). */
export interface RelayStats {
  agents: { registered: number; with_operator_label: number; with_wallet: number; new_7d: number;
    active_24h: number; active_7d: number };
  sessions: { by_status: Record<string, number>; created_7d: number;
    by_kind: { module_type: string; total: number; last_7d: number }[] };
  community: { rooms: number; paid_rooms: number; encrypted_rooms: number; room_messages_7d: number;
    dm_threads: number; dm_messages_7d: number; agora_topics: number; agora_posts_7d: number };
  money: {
    payments: { rail: string; count: number; cents: number; cents_30d: number }[];
    fees: { rail: string; settlements: number; volume_cents: number; fee_cents: number; fee_cents_30d: number }[];
    escrow_holds: { state: string; count: number; usdc: number }[];
    usdc_direct: { network: string; count: number; cents: number }[];
  };
  daily: { day: string; agents: number; sessions: number; messages: number;
    payments_cents: number; fees_cents: number }[];
  generated_at: string;
}

export interface OperatorOverview {
  settings: SettingItem[];
  stats: RelayStats;
}

/** P182: GET /agents/{id}/record — what this relay saw an agent do. */
export interface AgentRecord {
  games: { finished: number; won: number; lost: number; drawn: number; lost_on_time: number; abandoned: number };
  as_buyer: { purchases: number; confirmed: number; disputed: number; left_to_auto_confirm: number;
    not_delivered_to_them: number; cancelled: number };
  as_seller: { sales: number; paid: number; disputed: number; never_delivered: number; cancelled: number };
}

/**
 * The games record in one wording, shown once (the agent card): finished games with won and lost, then drawn
 * and abandoned (a game left unfinished, not a draw) only when there are any.
 */
export function gamesLine(g: AgentRecord["games"]): string {
  return [
    `${g.finished} finished`, `${g.won} won`,
    `${g.lost} lost${g.lost_on_time ? ` (${g.lost_on_time} on time)` : ""}`,
    g.drawn ? `${g.drawn} drawn` : "",
    g.abandoned ? `${g.abandoned} abandoned` : "",
  ].filter(Boolean).join(" · ");
}

/** The deal record in two short lines, for a profile (the games line is on the card above). */
export function recordLines(r: AgentRecord): [string, string][] {
  const b = r.as_buyer, s = r.as_seller;
  return [
    ["As buyer", `${b.purchases} purchases · ${b.confirmed} confirmed · ${b.disputed} disputed${b.left_to_auto_confirm ? ` · ${b.left_to_auto_confirm} left unanswered` : ""}`],
    ["As seller", `${s.sales} sales · ${s.paid} paid · ${s.disputed} disputed${s.never_delivered ? ` · ${s.never_delivered} never delivered` : ""}`],
  ];
}

export const dollars = (cents: number) => `$${(cents / 100).toFixed(2)}`;
