// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { RoomChainBadge } from "./RoomChainBadge";

// P68: the three badges verify something, and each opened with
// `if (!result) return null` while discarding `useInterval`'s error flag. A
// check that could not run rendered as *absence*, beside rooms showing
// "✓ chain verified" — which reads as "not checked yet", not "could not
// check". Those are different claims about a hash chain.
//
// This is the rule the relay already enforces on itself
// (`x402_authorization_used()` is tri-state; `find_settlement_transfer()`
// returns `known`), applied to the one surface still collapsing it.

vi.mock("../integrityCheck", () => ({
  verifyRoomChainFromRelay: vi.fn(),
}));
vi.mock("../api", () => ({ api: { roomChain: vi.fn(), roomMessages: vi.fn() } }));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "viewer" }));

import { verifyRoomChainFromRelay } from "../integrityCheck";
import type { RoomChainVerifyResult } from "../integrityCheck";

const mockVerify = vi.mocked(verifyRoomChainFromRelay);

// Typed against the real interface rather than a loose object literal — the
// first version used `replay_scan_scope: "full"`, which is not one of the
// union's members, and `typecheck:src` caught it. A fixture that does not
// satisfy the type it stands in for is a test asserting on a shape the code
// never sees.
const verified: RoomChainVerifyResult = {
  room_id: "room_1",
  signed_chain_head: "abc123",
  message_count: 3,
  verified: true,
  reason: "ok",
  chain_verified_scope: "genesis",
  replay_scan_scope: "full_history",
  replayed_nonce_count: 0,
  replayed_nonce_sample: [],
};

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  // Not automatic here: vitest globals are off, so testing-library never
  // registers its own afterEach. Without this the first test's badge is still
  // mounted when the fourth asserts on absence.
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("integrity badges distinguish 'could not check' from 'nothing to check'", () => {
  it("says so when verification could not run", async () => {
    mockVerify.mockRejectedValue(new Error("relay unreachable"));
    render(<RoomChainBadge roomId="room_1" />);
    await vi.waitFor(() =>
      expect(screen.getByText(/chain unverifiable/i)).toBeTruthy(),
    );
    // The distinction that matters: it must not read as a clean result.
    expect(screen.queryByText(/chain verified/i)).toBeNull();
  });

  it("renders nothing only when there is genuinely nothing to verify", async () => {
    mockVerify.mockResolvedValue({ ...verified, message_count: 0 });
    const { container } = render(<RoomChainBadge roomId="room_2" />);
    await vi.waitFor(() => expect(mockVerify).toHaveBeenCalled());
    await vi.waitFor(() => expect(container.textContent).toBe(""));
  });

  it("still reports a clean chain", async () => {
    mockVerify.mockResolvedValue(verified);
    render(<RoomChainBadge roomId="room_3" />);
    await vi.waitFor(() =>
      expect(screen.getByText(/chain verified \(3\)/i)).toBeTruthy(),
    );
  });

  it("still reports a broken chain, and not as unverifiable", async () => {
    mockVerify.mockResolvedValue({ ...verified, verified: false, reason: "hash mismatch" });
    render(<RoomChainBadge roomId="room_4" />);
    await vi.waitFor(() =>
      expect(screen.getByText(/chain unverified/i)).toBeTruthy(),
    );
    expect(screen.queryByText(/unverifiable/i)).toBeNull();
  });
});
