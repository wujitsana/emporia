// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";

import { AgentDetailPanel } from "./AgentsView";
import type { AgentProfile } from "../api";

// P70: the relay's `_payment_rails()` had two copies of one rule and they
// disagreed; this file is the third copy, in the client. It guessed rails from
// `has_stripe` whenever the relay sent none — but `has_stripe` describes this
// *agent's* Connect account, and "mpp"/"stripe_pi" describe the *relay's*
// configuration. So the dashboard could show an agent as payable by rails the
// relay does not offer. Absence must read as absence, the same rule the
// integrity badges follow (see ui/IntegrityState.tsx).

vi.mock("../api", () => ({
  api: {
    agentProfile: vi.fn(() => new Promise(() => {})),  // never resolves: keep the fallback row on screen
    agentSessions: vi.fn(), agentListings: vi.fn(), agentPosts: vi.fn(),
    agentRecord: vi.fn(() => new Promise(() => {})),     // P182: not what these tests are about
  },
}));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "" }));

const base: AgentProfile = {
  agent_id: "a1", display_name: "Agent One", registered_at: new Date().toISOString(),
  is_active: true, session_count: 0, win_count: 0, trust_level: "key",
} as AgentProfile;

afterEach(cleanup);

describe("an agent's payment rails are reported, never inferred", () => {
  it("says nothing was reported rather than guessing from has_stripe", async () => {
    render(<AgentDetailPanel agent={{ ...base, has_stripe: true } as AgentProfile} />);
    expect(screen.getByText(/not reported/i)).toBeTruthy();
    // The specific fabrication this replaces: a Connect account said nothing
    // about whether the relay has an MPP profile or a Stripe key.
    expect(screen.queryByText(/mpp/i)).toBeNull();
    expect(screen.queryByText(/stripe_pi/i)).toBeNull();
  });

  it("renders exactly what the relay did report", async () => {
    render(<AgentDetailPanel
      agent={{ ...base, has_stripe: false, payment_rails: ["free", "x402"] } as AgentProfile} />);
    expect(screen.getByText(/x402/i)).toBeTruthy();
    expect(screen.queryByText(/not reported/i)).toBeNull();
  });

  it("an empty list is a real answer, not a missing one", async () => {
    render(<AgentDetailPanel agent={{ ...base, payment_rails: [] } as AgentProfile} />);
    expect(screen.queryByText(/not reported/i)).toBeNull();
  });
});
