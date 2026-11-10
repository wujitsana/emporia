/**
 * The Stats view's figures (You › Stats): what the viewing agent earned, spent and is owed,
 * what it did, and — where Hermes's own records are readable — what its Emporia conversations
 * cost in model tokens, so one number says whether it pays for itself.
 *
 * The same figures as `emporia stats NAME` and the `my_stats` tool (src/emporia/analytics.py,
 * `agent_stats`), from the same relay routes, worked out here so the 7/30-day toggle needs no
 * new request. Pure: rows in, figures out. Amounts are micro-dollars (USDC atomic units), so an
 * escrow capture's exact split survives (paymentFormat.settlementPayoutMicros).
 */
import type { EscrowHold, Health, PaymentRecord, Settlement } from "./api";
import type { AgentRecord } from "./settings";
import { isTestnet, networkWithKind, settlementHealth, settlementPayoutMicros, settlementFeeMicros } from "./paymentFormat";

export const PERIODS = [7, 30] as const;
export type Period = (typeof PERIODS)[number];

const DAY_MS = 86_400_000;

/** A settlement as the relay's ?agent_id= read names it (relay_server._settlement_kinds). */
export type SettlementKind = "game" | "service" | "room" | "topic" | "access" | "session";
export type StatsSettlement = Settlement & { kind?: SettlementKind | string; payer_id?: string | null };

/** Where earnings come from, in the order the view lists them. */
export const SOURCES = ["services", "games", "rooms_topics", "other"] as const;
export type Source = (typeof SOURCES)[number];

export const SOURCE_LABELS: Record<Source, string> = {
  services: "Services sold",
  games: "Games won",
  rooms_topics: "Room and topic entry fees",
  other: "Other sessions",
};

/** A settlement's source. A relay that names no kind: a row with a payer is an access sale. */
export function earningSource(s: StatsSettlement): Source {
  const kind = s.kind ?? (s.payer_id ? "access" : "session");
  switch (kind) {
    case "service": return "services";
    case "game": return "games";
    case "room": case "topic": case "access": return "rooms_topics";
    default: return "other";
  }
}

/** What Hermes reports about the agent's Emporia conversations (plugin_api.py `/_hermes_stats`). */
export interface ModelCost {
  available: boolean;
  usd: number;
  tokens: number;
  sessions: number;
  by_kind: Record<string, { usd: number; sessions: number }>;
  /** Sessions a subscription pays for (Hermes `cost_status` "included"): in no dollar figure, but they count against its limits. */
  included_sessions?: number;
  included_tokens?: number;
  included_providers?: string[];
  days?: number;
  reason?: string;
}

/** "3 sessions included in your openai-codex subscription (count against its limits)", or "" when none. */
export function includedText(m: ModelCost): string {
  const n = m.included_sessions ?? 0;
  if (n <= 0) return "";
  const who = (m.included_providers ?? []).join(", ");
  const own = who && who !== "subscription" ? `your ${who} subscription` : "your subscription";
  return `${n} session${n === 1 ? "" : "s"} included in ${own} (count against its limits)`;
}

export interface HermesStats {
  model: ModelCost;
  turns_24h: number | null;
}

/** The relay's parts; null where the relay refused it or does not report it. */
export interface StatsInput {
  agentId: string;
  payments: PaymentRecord[] | null;
  settlements: StatsSettlement[] | null;
  holds: EscrowHold[] | null;
  record: AgentRecord | null;
  /** The relay had more settlements than it sent (`has_more`): older ones are not counted. */
  settlementsTruncated?: boolean;
}

export interface RecentPayment {
  key: string;
  at: string;
  direction: "in" | "out";
  micros: number;
  rail: string;
  what: string;
  /** settled · owed · failed (incoming), or the payment's own status (outgoing). */
  state: string;
}

export interface AgentStats {
  days: Period;
  earned: number;
  earnedByRail: Record<string, number>;
  earnedBySource: Record<Source, number>;
  /** How many room and topic entries the agent was paid for, in the period. */
  accessSales: number;
  owed: number;
  failed: number;
  fees: number;
  spent: number;
  spentByRail: Record<string, number>;
  spentOn: Record<string, number>;
  /** USDC the agent paid that escrow still holds, by network; and held for it, as the seller. */
  heldPaid: Record<string, number>;
  heldForIt: Record<string, number>;
  /** Earned − spent − model cost, when the model cost is known. */
  net: number | null;
  modelMicros: number | null;
  games: AgentRecord["games"] | null;
  winRate: number | null;
  asBuyer: AgentRecord["as_buyer"] | null;
  asSeller: AgentRecord["as_seller"] | null;
  /** Purchases confirmed (or left to auto-confirm) of those finished; sales paid of those made. */
  buyerSettleRate: number | null;
  sellerSettleRate: number | null;
  recent: RecentPayment[];
  /** Parts of the relay's record this view could not read. */
  missing: ("payments" | "settlements" | "holds" | "record")[];
  /** Nothing earned, spent, held or owed in the period. */
  quiet: boolean;
}

function when(at: string | undefined | null): number {
  const t = Date.parse(at ?? "");
  return Number.isFinite(t) ? t : 0;
}

const SPENT_STATES = new Set(["confirmed", "succeeded", "paid"]);
const HELD = new Set(["authorized", "held", "capture_pending"]);

function add(into: Record<string, number>, key: string, n: number) {
  into[key] = (into[key] ?? 0) + n;
}

function rate(num: number, den: number): number | null {
  return den > 0 ? Math.round((num / den) * 1000) / 1000 : null;
}

const PAYMENT_LABELS: Record<string, string> = {
  session_price: "purchase", room_entry: "room entry", agora_subscribe: "topic entry",
};

const KIND_LABELS: Record<string, string> = {
  game: "game won", service: "service sold", room: "room entry fee",
  topic: "topic entry fee", access: "entry fee", session: "session",
};

export function agentStats(input: StatsInput, days: Period, model: ModelCost | null, now = Date.now()): AgentStats {
  const since = now - days * DAY_MS;
  const { agentId } = input;
  const missing: AgentStats["missing"] = [];
  if (!input.payments) missing.push("payments");
  if (!input.settlements) missing.push("settlements");
  if (!input.holds) missing.push("holds");
  if (!input.record) missing.push("record");

  const spentByRail: Record<string, number> = {};
  const spentOn: Record<string, number> = {};
  const recent: RecentPayment[] = [];
  for (const p of input.payments ?? []) {
    if (p.agent_id !== agentId || when(p.created_at) < since) continue;
    if (!SPENT_STATES.has(p.status || "confirmed")) continue;
    const micros = (p.amount_cents || 0) * 10_000;
    add(spentByRail, p.rail || "stripe", micros);
    add(spentOn, p.payment_type || "other", micros);
    recent.push({ key: `p:${p.payment_id}`, at: p.created_at, direction: "out", micros, rail: p.rail || "stripe",
      what: PAYMENT_LABELS[p.payment_type] ?? p.payment_type.replace(/_/g, " "), state: p.status || "confirmed" });
  }

  const earnedByRail: Record<string, number> = {};
  const earnedBySource = Object.fromEntries(SOURCES.map((s) => [s, 0])) as Record<Source, number>;
  let owed = 0, failed = 0, fees = 0, accessSales = 0;
  for (const s of input.settlements ?? []) {
    if (s.winner_id !== agentId || when(s.created_at) < since) continue;
    const health = settlementHealth(s);
    if (health === "refunded") continue;
    const micros = settlementPayoutMicros(s);
    const source = earningSource(s);
    if (health === "failed") failed += micros;
    else if (health === "pending") owed += micros;
    else {
      add(earnedByRail, s.rail || "stripe", micros);
      earnedBySource[source] += micros;
    }
    if (source === "rooms_topics") accessSales += 1;
    fees += settlementFeeMicros(s);
    if (micros > 0 || health !== "settled") {
      recent.push({ key: `s:${s.settlement_id}`, at: s.created_at, direction: "in", micros, rail: s.rail || "stripe",
        what: KIND_LABELS[s.kind ?? ""] ?? (source === "rooms_topics" ? "entry fee" : "session"),
        state: health === "pending" ? "owed" : health });
    }
  }

  const heldPaid: Record<string, number> = {};
  const heldForIt: Record<string, number> = {};
  for (const h of input.holds ?? []) {
    if (!HELD.has(h.state)) continue;
    const n = Number(h.amount_atomic);
    if (!Number.isFinite(n)) continue;
    if (h.payer_agent_id === agentId) add(heldPaid, h.network, n);
    else if (h.receiver_agent_id === agentId) add(heldForIt, h.network, n);
  }

  const sum = (r: Record<string, number>) => Object.values(r).reduce((a, b) => a + b, 0);
  const earned = sum(earnedByRail), spent = sum(spentByRail);
  const modelMicros = model?.available ? Math.round(model.usd * 1e6) : null;
  const record = input.record;
  const g = record?.games ?? null, b = record?.as_buyer ?? null, sl = record?.as_seller ?? null;
  recent.sort((x, y) => when(y.at) - when(x.at));
  return {
    days, earned, earnedByRail, earnedBySource, accessSales, owed, failed, fees, spent, spentByRail, spentOn,
    heldPaid, heldForIt,
    net: modelMicros === null ? null : earned - spent - modelMicros,
    modelMicros,
    games: g,
    winRate: g ? rate(g.won, g.finished) : null,
    asBuyer: b,
    asSeller: sl,
    buyerSettleRate: b ? rate(b.confirmed + b.left_to_auto_confirm, b.purchases - b.cancelled) : null,
    sellerSettleRate: sl ? rate(sl.paid, sl.sales - sl.cancelled) : null,
    recent: recent.slice(0, 10),
    missing,
    quiet: earned === 0 && spent === 0 && owed === 0 && failed === 0 && sum(heldPaid) === 0 && sum(heldForIt) === 0,
  };
}

/** "x402 · USDC on Base Sepolia testnet", "card · Stripe test mode": a rail, and where its money is. */
export function railNetwork(rail: string, health: Health | null): string {
  if (rail === "x402") {
    const nets = [...new Set([...(health?.x402_networks ?? []), ...(health?.x402_escrow_network ? [health.x402_escrow_network] : [])])];
    return `USDC (x402) on ${nets.length ? nets.map(networkWithKind).join(", ") : "a testnet"}`;
  }
  if (rail === "stripe") return health?.stripe_mode === "live" ? "card · Stripe live mode" : "card · Stripe test mode";
  if (rail === "tempo") return `Tempo on ${health?.tempo_network ? networkWithKind(health.tempo_network) : "a testnet"}`;
  return rail;
}

/** Whether every rail this relay offers moves test money: testnets, and Stripe in test mode. */
export function allTestMoney(health: Health | null): boolean {
  const nets = [...(health?.x402_networks ?? []), health?.x402_escrow_network, health?.tempo_network]
    .filter((n): n is string => !!n);
  return nets.every((n) => isTestnet(n)) && health?.stripe_mode !== "live";
}

/** A share as "62 %", or "—". */
export function pct(r: number | null): string {
  return r === null ? "—" : `${Math.round(r * 100)} %`;
}
