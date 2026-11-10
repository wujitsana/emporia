// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Health } from "../api";
import { RelayHealthCtx } from "../healthCtx";

const h = vi.hoisted(() => ({
  inHermes: false,
  refuse: new Set<string>(),
  hermesAsked: [] as string[],
  settlements: [] as Record<string, unknown>[],
}));

const now = Date.now();
const ago = (d: number) => new Date(now - d * 86_400_000).toISOString();

vi.mock("../relayEnv", async (orig) => ({ ...(await orig<typeof import("../relayEnv")>()), VIEWER_ID: "beta" }));

vi.mock("../transport", async (orig) => {
  const real = await orig<typeof import("../transport")>();
  return {
    ...real,
    get IN_HERMES() { return h.inHermes; },
    VIA_CONSOLE: false,
    consoleUrl: (p: string) => `/api/plugins/emporia${p}`,
    httpFetch: vi.fn(async (url: string) => {
      h.hermesAsked.push(url);
      const days = Number(new URL(url, "http://x").searchParams.get("days"));
      return new Response(JSON.stringify({ agent_id: "beta", turns_24h: 7, model: {
        available: true, usd: days === 7 ? 0.1 : 0.43, tokens: 1234, sessions: 3, days,
        included_sessions: 2, included_tokens: 900, included_providers: ["openai-codex"],
        by_kind: { dm: { usd: 0.3, sessions: 2 }, session: { usd: 0.13, sessions: 1 } } } }), { status: 200 });
    }),
  };
});

const refusing = <T,>(part: string, value: T) => () =>
  (h.refuse.has(part) ? Promise.reject(new Error(`403 /${part}`)) : Promise.resolve(value));

vi.mock("../api", () => ({
  api: {
    myPayments: vi.fn(() => refusing("payments", { payments: [
      { payment_id: "p1", agent_id: "beta", amount_cents: 100, currency: "usd", payment_intent_id: "pi1",
        payment_type: "session_price", status: "confirmed", rail: "x402", created_at: ago(1) }] })()),
    agentSettlements: vi.fn(() => refusing("settlements", { settlements: h.settlements, count: 0, has_more: false })()),
    escrowHolds: vi.fn(() => refusing("holds", { holds: [] })()),
    agentRecord: vi.fn(() => refusing("record", {
      games: { finished: 4, won: 1, lost: 2, drawn: 1, lost_on_time: 0, abandoned: 0 },
      as_buyer: { purchases: 0, confirmed: 0, disputed: 0, left_to_auto_confirm: 0, not_delivered_to_them: 0, cancelled: 0 },
      as_seller: { sales: 2, paid: 2, disputed: 0, never_delivered: 0, cancelled: 0 },
    })()),
  },
}));

import { StatsView } from "./StatsView";

const health = { x402_networks: ["eip155:84532"], x402_escrow_network: "eip155:84532", stripe_mode: "test",
  payment_rails: ["stripe", "x402"] } as Health;

const view = () => render(<RelayHealthCtx.Provider value={health}><StatsView /></RelayHealthCtx.Provider>);

beforeEach(() => {
  h.inHermes = false;
  h.refuse = new Set();
  h.hermesAsked = [];
  h.settlements = [
    { settlement_id: "s1", session_id: "room_1", winner_id: "beta", kind: "room", payer_id: "[redacted]",
      total_price_cents: 150, platform_fee_cents: 0, winner_payout_cents: 150, platform_fee_bps: 0,
      status: "settled", transfer_status: "direct", rail: "x402", created_at: ago(2) },
    { settlement_id: "s2", session_id: "sess_1", winner_id: "beta", kind: "service",
      total_price_cents: 200, platform_fee_cents: 5, winner_payout_cents: 195, platform_fee_bps: 250,
      status: "settled", transfer_status: "destination", rail: "stripe", created_at: ago(20) },
  ];
});
afterEach(cleanup);

describe("Market › Stats", () => {
  it("shows the headline figures as test money, earnings by source with room entry fees, and the record", async () => {
    view();
    expect(await screen.findByText("Earned, by source · 30 days")).toBeTruthy();
    expect(screen.getByText("$3.45")).toBeTruthy();                       // earned: room $1.50 + service $1.95
    expect(screen.getByText(/All amounts are test money/).textContent).toContain("USDC (x402) on Base Sepolia testnet");
    expect(screen.getByText("Room and topic entry fees")).toBeTruthy();
    expect(screen.getByText("$1.50 · 1 entry")).toBeTruthy();
    expect(screen.getByText(/win rate 25 %/)).toBeTruthy();
    expect(screen.getByText(/settle rate 100 %/)).toBeTruthy();
    expect(screen.getByRole("table").textContent).toContain("room entry fee");
  });

  it("switches to 7 days without asking the relay again, and leaves out older rows", async () => {
    view();
    await screen.findByText("Earned, by source · 30 days");
    fireEvent.click(screen.getByRole("button", { name: "7 days" }));
    expect(await screen.findByText("Earned, by source · 7 days")).toBeTruthy();
    expect(screen.getAllByText("$1.50").length).toBeGreaterThan(0);
    expect(screen.queryByText("$3.45")).toBeNull();
  });

  it("outside Hermes, says where model cost comes from and gives the command", async () => {
    view();
    expect(await screen.findByText(/This page is not inside Hermes/)).toBeTruthy();
    expect(screen.getByText(/hermes emporia stats \S+ --days 30/)).toBeTruthy();
    expect(screen.getByText("model cost not known here")).toBeTruthy();
    expect(h.hermesAsked).toEqual([]);
  });

  it("inside Hermes, reads model cost and turns from the plugin for the period, and shows the net", async () => {
    h.inHermes = true;
    view();
    expect(await screen.findByText(/metered over 3 conversations/)).toBeTruthy();
    expect(screen.getByText("2 sessions included in your openai-codex subscription (count against its limits)"))
      .toBeTruthy();
    expect(screen.getByText("Direct messages")).toBeTruthy();
    expect(screen.getByText(/relay-started turns in the last 24 hours/)).toBeTruthy();
    expect(screen.getByText("earned − spent − model")).toBeTruthy();
    expect(screen.getByText("$2.02")).toBeTruthy();                       // 3.45 − 1.00 − 0.43
    fireEvent.click(screen.getByRole("button", { name: "7 days" }));
    await waitFor(() => expect(h.hermesAsked.some((u) => u.includes("days=7"))).toBe(true));
    expect(await screen.findByText("$0.10")).toBeTruthy();                // the 7-day model cost, not the 30-day one
    expect(h.hermesAsked[0]).toContain("/api/plugins/emporia/_hermes_stats?agent=beta&days=30");
  });

  it("names each part the relay does not report, and an error when it reports nothing", async () => {
    h.refuse = new Set(["payments", "record"]);
    view();
    expect(await screen.findByText(/predates `\?mine=1`/)).toBeTruthy();
    expect(screen.getByText(/so games and trades are not shown/)).toBeTruthy();
    cleanup();
    h.refuse = new Set(["payments", "settlements", "holds", "record"]);
    view();
    expect(await screen.findByText(/Could not read .+'s figures from the relay/)).toBeTruthy();
  });

  it("says so when nothing moved in the period, and has no button but the toggle and copy", async () => {
    h.settlements = [];
    h.refuse = new Set(["payments"]);
    view();
    expect(await screen.findByText("Nothing earned or spent in the last 30 days.")).toBeTruthy();
    const buttons = [...document.querySelectorAll("button")].map((b) => b.textContent);
    expect(new Set(buttons)).toEqual(new Set(["7 days", "30 days", "copy"]));
  });
});
