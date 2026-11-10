import { describe, expect, it } from "vitest";
import type { Health } from "./api";
import { agentStats, allTestMoney, earningSource, includedText, pct, railNetwork, type StatsInput } from "./statsModel";

const NOW = Date.parse("2026-11-06T12:00:00Z");
const ago = (days: number) => new Date(NOW - days * 86_400_000).toISOString();

const settlement = (over: Record<string, unknown>) => ({
  settlement_id: `stl_${Math.random()}`, session_id: "x", winner_id: "beta", total_price_cents: 0,
  platform_fee_cents: 0, winner_payout_cents: 0, platform_fee_bps: 0, status: "settled", created_at: ago(1), ...over,
});

const input = (): StatsInput => ({
  agentId: "beta",
  payments: [
    { payment_id: "p1", agent_id: "beta", amount_cents: 100, currency: "usd", payment_intent_id: "pi1",
      payment_type: "session_price", status: "confirmed", rail: "x402", created_at: ago(1) },
    { payment_id: "p2", agent_id: "beta", amount_cents: 25, currency: "usd", payment_intent_id: "pi2",
      payment_type: "room_entry", status: "confirmed", rail: "stripe", created_at: ago(10) },
    { payment_id: "p3", agent_id: "beta", amount_cents: 50, currency: "usd", payment_intent_id: "pi3",
      payment_type: "room_entry", status: "refunded", rail: "stripe", created_at: ago(1) },
  ],
  settlements: [
    settlement({ kind: "game", winner_payout_cents: 195, platform_fee_cents: 5, rail: "x402", transfer_status: "escrow_captured",
      winner_payout_atomic: "1950000", platform_fee_atomic: "50000" }),
    settlement({ kind: "service", winner_payout_cents: 98, platform_fee_cents: 2, rail: "stripe", transfer_status: "pending_connect" }),
    // A room's entry fee: the creator's earnings (relay_server's ?agent_id= third arm).
    settlement({ kind: "room", payer_id: "[redacted]", winner_payout_cents: 150, rail: "x402", transfer_status: "direct" }),
    settlement({ kind: "topic", payer_id: "[redacted]", winner_payout_cents: 75, rail: "stripe", transfer_status: "transferred",
      created_at: ago(12) }),
    settlement({ kind: "service", winner_payout_cents: 60, rail: "stripe", transfer_status: "transfer_failed: declined" }),
    settlement({ winner_id: "alpha", winner_payout_cents: 500, transfer_status: "direct" }),
    settlement({ kind: "game", status: "refunded", transfer_status: "refunded" }),
  ],
  holds: [
    { hold_hash: "h1", session_id: "s", payer_agent_id: "beta", payer_address: "", receiver_agent_id: "gamma",
      receiver_address: "", network: "eip155:84532", amount_atomic: "500000", state: "authorized",
      authorize_tx: null, capture_tx: null, void_tx: null, created_at: ago(1), updated_at: ago(1) },
    { hold_hash: "h2", session_id: "s", payer_agent_id: "gamma", payer_address: "", receiver_agent_id: "beta",
      receiver_address: "", network: "eip155:84532", amount_atomic: "250000", state: "authorized",
      authorize_tx: null, capture_tx: null, void_tx: null, created_at: ago(1), updated_at: ago(1) },
    { hold_hash: "h3", session_id: "s", payer_agent_id: "beta", payer_address: "", receiver_agent_id: "gamma",
      receiver_address: "", network: "eip155:84532", amount_atomic: "900000", state: "captured",
      authorize_tx: null, capture_tx: null, void_tx: null, created_at: ago(1), updated_at: ago(1) },
  ],
  record: {
    games: { finished: 4, won: 1, lost: 2, drawn: 1, lost_on_time: 0, abandoned: 2 },
    as_buyer: { purchases: 3, confirmed: 1, disputed: 1, left_to_auto_confirm: 1, not_delivered_to_them: 0, cancelled: 0 },
    as_seller: { sales: 5, paid: 3, disputed: 0, never_delivered: 0, cancelled: 1 },
  },
});

describe("the Stats view's figures", () => {
  it("count room and topic entry fees as the creator's earnings, by source and by rail", () => {
    const st = agentStats(input(), 30, null, NOW);
    expect(st.earned).toBe(1_950_000 + 1_500_000 + 750_000);           // the escrow's exact split, in micros
    expect(st.earnedBySource).toEqual({ services: 0, games: 1_950_000, rooms_topics: 2_250_000, other: 0 });
    expect(st.accessSales).toBe(2);
    expect(st.earnedByRail).toEqual({ x402: 3_450_000, stripe: 750_000 });
    expect(st.owed).toBe(980_000);                                       // pending_connect: earned, not paid out
    expect(st.failed).toBe(600_000);                                     // transfer_failed: <reason>
    expect(st.fees).toBe(50_000 + 20_000);
  });

  it("narrow to the period: the 7-day view leaves out older rows", () => {
    const st = agentStats(input(), 7, null, NOW);
    expect(st.earnedBySource.rooms_topics).toBe(1_500_000);
    expect(st.spent).toBe(1_000_000);
    expect(agentStats(input(), 30, null, NOW).spent).toBe(1_250_000);  // the refunded payment never counts
  });

  it("hold escrow apart: what it paid and is still held, and what is held for it", () => {
    const st = agentStats(input(), 30, null, NOW);
    expect(st.heldPaid).toEqual({ "eip155:84532": 500_000 });
    expect(st.heldForIt).toEqual({ "eip155:84532": 250_000 });
  });

  it("give a net only when the model cost is known", () => {
    expect(agentStats(input(), 30, null, NOW).net).toBeNull();
    const model = { available: true, usd: 0.43, tokens: 1, sessions: 3, by_kind: {} };
    const st = agentStats(input(), 30, model, NOW);
    expect(st.modelMicros).toBe(430_000);
    expect(st.net).toBe(st.earned - st.spent - 430_000);
    expect(agentStats(input(), 30, { ...model, available: false }, NOW).net).toBeNull();
  });

  it("say what a subscription covers apart from what was metered", () => {
    const m = { available: true, usd: 0.43, tokens: 1, sessions: 5, by_kind: {} };
    expect(includedText(m)).toBe("");
    expect(includedText({ ...m, included_sessions: 1, included_providers: ["openai-codex"] }))
      .toBe("1 session included in your openai-codex subscription (count against its limits)");
    expect(includedText({ ...m, included_sessions: 3, included_providers: [] }))
      .toBe("3 sessions included in your subscription (count against its limits)");
  });

  it("rates from the record: win rate, and settle rates of what was not cancelled", () => {
    const st = agentStats(input(), 30, null, NOW);
    expect(st.winRate).toBe(0.25);
    expect(st.buyerSettleRate).toBe(0.667);
    expect(st.sellerSettleRate).toBe(0.75);
    expect(pct(st.winRate)).toBe("25 %");
    expect(pct(null)).toBe("—");
  });

  it("list the newest payments in and out, and name what the relay did not report", () => {
    const st = agentStats(input(), 30, null, NOW);
    expect(st.recent.length).toBeLessThanOrEqual(10);
    expect(st.recent.map((p) => p.direction)).toContain("out");
    expect(st.recent.find((p) => p.what === "room entry fee")?.direction).toBe("in");
    expect(st.recent.filter((p) => p.what === "service sold").map((p) => p.state)).toContain("failed");
    const times = st.recent.map((p) => Date.parse(p.at));
    expect([...times].sort((a, b) => b - a)).toEqual(times);
    const bare = agentStats({ agentId: "beta", payments: null, settlements: [], holds: null, record: null }, 30, null, NOW);
    expect(bare.missing).toEqual(["payments", "holds", "record"]);
    expect(bare.quiet).toBe(true);
    expect(bare.games).toBeNull();
  });

  it("read a settlement's source from its kind, or from its payer on a relay that names none", () => {
    expect(earningSource(settlement({ kind: "service" }))).toBe("services");
    expect(earningSource(settlement({ payer_id: "[redacted]" }))).toBe("rooms_topics");
    expect(earningSource(settlement({}))).toBe("other");
  });
});

describe("rails, networks and test money", () => {
  const health = { x402_networks: ["eip155:84532"], x402_escrow_network: "eip155:84532", stripe_mode: "test",
    tempo_network: "eip155:42431" } as Health;

  it("say each rail's network, and that it is a test one", () => {
    expect(railNetwork("x402", health)).toBe("USDC (x402) on Base Sepolia testnet");
    expect(railNetwork("stripe", health)).toBe("card · Stripe test mode");
    expect(railNetwork("tempo", health)).toBe("Tempo on Tempo Moderato testnet");
    expect(railNetwork("x402", null)).toBe("USDC (x402) on a testnet");
    expect(allTestMoney(health)).toBe(true);
    expect(allTestMoney({ ...health, x402_networks: ["eip155:8453"] } as Health)).toBe(false);
    expect(allTestMoney({ ...health, stripe_mode: "live" } as Health)).toBe(false);
  });
});
