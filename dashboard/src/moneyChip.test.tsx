// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Health } from "./api";
import { moneyChip } from "./moneyChip";
import { RelayStrip } from "./RelayStrip";

// P6: the header says what kind of money the amounts are, from /health.
const base: Health = { status: "ok", service: "emporia", version: "0.2.0", modules: [], session_count: 0 };

afterEach(cleanup);

describe("the money chip", () => {
  it("says testnet on a relay whose money is on test networks", () => {
    const c = moneyChip({ ...base, x402_enabled: true, x402_networks: ["eip155:84532"], x402_escrow_network: "eip155:84532",
      live_money: [] });
    expect(c?.kind).toBe("test");
    expect(c?.label).toBe("testnet");
    expect(c?.title).toBe("Test money only: amounts here are on Base Sepolia testnet, with no real value.");
  });

  it("counts Stripe's test mode and Tempo's testnet as test money", () => {
    const c = moneyChip({ ...base, stripe_enabled: true, stripe_mode: "test", tempo_network: "eip155:42431" });
    expect(c?.label).toBe("testnet");
    expect(c?.title).toMatch(/Tempo Moderato testnet, Stripe test mode/);
  });

  it("says 'real money', plainly, when the relay reports it moves real money", () => {
    for (const h of [
      { ...base, live_money: ["escrow on a mainnet"] },
      { ...base, x402_escrow_network: "eip155:8453" },
      { ...base, x402_enabled: true, x402_networks: ["eip155:84532", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"] },
      { ...base, stripe_enabled: true, stripe_mode: "live" },
      { ...base, x402_escrow_network: "eip155:999999" },          // unknown: not a known testnet
    ]) {
      const c = moneyChip(h);
      expect(c?.kind).toBe("real");
      expect(c?.label).toBe("real money");
      expect(c?.title).toMatch(/^This relay moves real money \(.+\)\. Amounts shown here are real\.$/);
    }
  });

  it("promises nothing: no reward, prize or earning in its words", () => {
    const words = [moneyChip({ ...base, x402_escrow_network: "eip155:84532" }), moneyChip({ ...base, live_money: ["a live Stripe key"] })]
      .map((c) => `${c?.label} ${c?.title}`).join(" ");
    expect(words).not.toMatch(/reward|prize|earn|win/i);
  });

  it("is not shown when the relay names no money rail, is down, or has not answered", () => {
    expect(moneyChip({ ...base })).toBeNull();
    expect(moneyChip({ ...base, x402_enabled: false, x402_networks: ["eip155:8453"] })).toBeNull();
    expect(moneyChip({ ...base, status: "error", x402_escrow_network: "eip155:84532" })).toBeNull();
    expect(moneyChip(null)).toBeNull();
  });

  it("sits beside the relay chip in the header, on a phone too", () => {
    const h = { ...base, x402_escrow_network: "eip155:84532" };
    for (const compact of [false, true]) {
      const { container } = render(<RelayStrip relayUrl="http://127.0.0.1:1" wsConnected={false} health={h} compact={compact} />);
      const chip = container.querySelector(".e-relay-status__chips .e-money-chip--test");
      expect(chip?.firstChild?.textContent).toBe("testnet");
      expect(chip?.getAttribute("title")).toMatch(/^Test money only/);
      cleanup();
    }
  });
});
