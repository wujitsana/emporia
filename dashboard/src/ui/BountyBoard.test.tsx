// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";

import { BountyBoard } from "./BountyBoard";
import type { Bounty } from "../api";

// P72-8: the board showed `entrant_count` with no denominator and no state, so
// a full contest and a lightly-attended one rendered identically, and a running
// or cancelled bounty looked open for entry. Both omissions produced the
// *reassuring* reading — the same failure the integrity badges (P68) and the
// overview cards (P69) were fixed for.

vi.mock("../api", () => ({ api: { bounties: vi.fn() } }));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "viewer" }));

import { api } from "../api";
const mockBounties = vi.mocked(api.bounties);

const base: Bounty = {
  listing_id: "bty_1", title: "Beat me at chess", description: "",
  module_type: "emporia:chess:v1", funder_id: "funder",
  prize_cents: 5000, entry_deadline: new Date(Date.now() + 864e5).toISOString(),
  state: "open", entrant_count: 0, min_entrants: 2, max_entrants: 2,
  session_id: null, escrow_strength: "held", origin_relay: "http://relay.test",
};

afterEach(() => { cleanup(); vi.clearAllMocks(); });

const show = (b: Partial<Bounty>) => {
  mockBounties.mockResolvedValue({ count: 1, bounties: [{ ...base, ...b }] });
  return render(<BountyBoard />);
};

describe("the board says whether you can actually enter", () => {
  it("says seats are unknown rather than inventing a denominator", async () => {
    // A federated bounty from a peer running a module this relay does not
    // have. "3/3" would be a fact nobody established.
    show({ entrant_count: 3, max_entrants: null, min_entrants: null });
    expect(await screen.findByText("3/?")).toBeTruthy();
  });

  it("shows seats as a fraction, not a bare count", async () => {
    show({ entrant_count: 2, max_entrants: 2 });
    expect(await screen.findByText("2/2")).toBeTruthy();
    // The bug: "2" alone reads as interest, not as a closed door.
    expect(screen.queryByText(/^2$/)).toBeNull();
  });

  it("a full-but-unstarted contest reads as ready, not open", async () => {
    show({ entrant_count: 2, max_entrants: 2, state: "open" });
    expect(await screen.findByText(/ready/i)).toBeTruthy();
  });

  it("an open contest with seats left says open", async () => {
    show({ entrant_count: 1, max_entrants: 2, state: "open" });
    expect(await screen.findByText("1/2")).toBeTruthy();
    expect(screen.getByText(/^open$/i)).toBeTruthy();
  });

  it("a running contest is not shown as enterable", async () => {
    show({ state: "running", session_id: "ses_abc", entrant_count: 2 });
    expect(await screen.findByText(/running/i)).toBeTruthy();
    expect(screen.queryByText(/^open$/i)).toBeNull();
  });

  it("a cancelled bounty says so rather than looking open", async () => {
    show({ state: "cancelled" });
    expect(await screen.findByText(/cancelled/i)).toBeTruthy();
  });

  it("a promised prize is not shown as a held one", async () => {
    // P79: an x402 bounty sets nothing aside — the funder pays after the
    // result and is flagged if they do not. An agent competing there is taking
    // a credit risk, and rendering that identically to reserved money would be
    // the most consequential version of the collapse this board keeps being
    // fixed for.
    show({ escrow_strength: "promised", prize_cents: 5000 });
    expect(await screen.findByText(/\$50\.00 ⚠/)).toBeTruthy();
  });

  it("a held prize carries no warning", async () => {
    show({ escrow_strength: "held", prize_cents: 5000 });
    expect(await screen.findByText("$50.00")).toBeTruthy();
    expect(screen.queryByText(/⚠/)).toBeNull();
  });

  it("an unreachable relay is not an empty board", async () => {
    mockBounties.mockRejectedValue(new Error("relay down"));
    render(<BountyBoard />);
    expect(await screen.findByText(/unavailable/i)).toBeTruthy();
    expect(screen.queryByText(/No open bounties/i)).toBeNull();
  });
});
