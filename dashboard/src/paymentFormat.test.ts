import { describe, expect, it } from "vitest";
import {
  bountyStateLabel,
  explorerTxUrl,
  railLabel,
  sessionStatusLabel,
  settlementHealth,
  usdcAtomicToUsd,
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
