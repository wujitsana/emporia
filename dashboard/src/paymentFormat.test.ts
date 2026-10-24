import { describe, expect, it } from "vitest";
import {
  bountyStateLabel,
  holdNeedsAttention,
  explorerTxUrl,
  railLabel,
  sessionStatusLabel,
  settlementHealth,
  usdcAtomicToUsd,
  acceptsLabel,
  paidByLabel,
  formatMicros,
  settlementFeeMicros,
  settlementPayoutMicros,
} from "./paymentFormat";

describe("settlementHealth", () => {
  it("a failed capture is a failure, even though the row says settled", () => {
    expect(settlementHealth({ status: "settled", transfer_status: "capture_failed" })).toBe("failed");
    expect(settlementHealth({ status: "settled", transfer_status: "transfer_failed" })).toBe("failed");
  });
  it("money that reached the payee directly is settled", () => {
    for (const t of ["direct", "escrow_captured", "destination", "transferred"])
      expect(settlementHealth({ status: "settled", transfer_status: t })).toBe("settled");
  });
  it("owed but not paid out is pending", () => {
    expect(settlementHealth({ status: "settled", transfer_status: "pending_connect_not_active" })).toBe("pending");
  });
  it("a refund is a refund", () => {
    expect(settlementHealth({ status: "refunded", transfer_status: "refunded" })).toBe("refunded");
  });
});

describe("labels", () => {
  it("names the rail", () => {
    expect(railLabel("x402", "escrow_captured")).toBe("x402 escrow");
    expect(railLabel("x402", "direct")).toBe("x402");
    expect(railLabel("stripe", "destination")).toBe("card (direct)");
    expect(railLabel(undefined)).toBe("—");
  });
  it("links Base transactions only", () => {
    expect(explorerTxUrl("eip155:84532", "0xab")).toBe("https://sepolia.basescan.org/tx/0xab");
    expect(explorerTxUrl("eip155:1", "0xab")).toBeNull();
    expect(explorerTxUrl("eip155:8453", null)).toBeNull();
  });
  it("formats USDC", () => {
    expect(usdcAtomicToUsd("975000")).toBe("$0.975");
    expect(usdcAtomicToUsd("1000000")).toBe("$1.00");
  });
  it("knows every bounty state the relay writes", () => {
    for (const s of ["open", "running", "awaiting_payment", "defaulted", "unfunded", "settled",
                     "refunded", "cancelling", "cancelled"])
      expect(bountyStateLabel(s, false).title).not.toBe(`This bounty is ${s}.`);
    expect(bountyStateLabel("open", true).label).toBe("ready");
    expect(bountyStateLabel("unfunded", false).failed).toBe(true);
  });
  it("spells out awaiting_funding", () => {
    expect(sessionStatusLabel("awaiting_funding")).toBe("awaiting funding");
  });
});

describe("escrow hold states (P111)", () => {
  it("flags a hold the seller may not be paid from", () => {
    expect(holdNeedsAttention("capture_pending")).toBe(true);
    expect(holdNeedsAttention("expired")).toBe(true);
    expect(holdNeedsAttention("captured")).toBe(false);
    expect(holdNeedsAttention("void_pending")).toBe(false);
  });
});

describe("settlement split (P129)", () => {
  const cents = { platform_fee_cents: 2, winner_payout_cents: 98 };
  it("an escrow capture reads the contract's exact split, not the rounded cents", () => {
    const s = { ...cents, platform_fee_atomic: "25000", winner_payout_atomic: "975000" };
    expect(formatMicros(settlementFeeMicros(s))).toBe("$0.025");
    expect(formatMicros(settlementPayoutMicros(s))).toBe("$0.975");
  });
  it("every other rail keeps its cents", () => {
    expect(formatMicros(settlementFeeMicros({ ...cents, platform_fee_atomic: null }))).toBe("$0.02");
    expect(formatMicros(settlementPayoutMicros(cents))).toBe("$0.98");
  });
  it("totals summed in micro-dollars agree with the chain", () => {
    const rows = [
      { ...cents, platform_fee_atomic: "25000", winner_payout_atomic: "975000" },
      { ...cents, platform_fee_atomic: "25000", winner_payout_atomic: "975000" },
    ];
    expect(formatMicros(rows.reduce((n, r) => n + settlementFeeMicros(r), 0))).toBe("$0.05");
  });
});

describe("what pays for what (P153)", () => {
  const both = { stripe_enabled: true, x402_enabled: true, x402_escrow_network: "eip155:84532" };
  it("says before paying what is accepted", () => {
    expect(acceptsLabel("entry", both)).toBe("accepts card · USDC");
    expect(acceptsLabel("purchase", both)).toBe("accepts card · USDC escrow");
    expect(acceptsLabel("purchase", { ...both, x402_escrow_network: null })).toBe("accepts card");
    expect(acceptsLabel("bounty_promised", both)).toBe("accepts USDC, paid after the result");
    expect(acceptsLabel("entry", {})).toBe("no payment rail on this relay");
    expect(acceptsLabel("wager", both, "emporia:blotto:v1")).toBe("accepts card · USDC escrow");
    expect(acceptsLabel("wager", both, "emporia:two-thirds:v1")).toBe("accepts card");
  });
  it("says after paying how", () => {
    expect(paidByLabel(["stripe"])).toBe("paid by card");
    expect(paidByLabel(["x402"])).toBe("paid by USDC escrow");
    expect(paidByLabel([])).toBeNull();
  });
});
