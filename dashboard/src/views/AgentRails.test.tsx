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
    agentSessions: vi.fn(), agentPosts: vi.fn(),
    agentRecord: vi.fn(() => new Promise(() => {})),     // P182: not what these tests are about
    follows: vi.fn(() => new Promise(() => {})),         // nor who follows whom
  },
}));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "" }));

const base: AgentProfile = {
  agent_id: "a1", display_name: "Agent One", registered_at: new Date().toISOString(),
  is_active: true, session_count: 0, win_count: 0, 
} as AgentProfile;

afterEach(cleanup);

describe("an agent card shows the agent's own payout rail, never the relay's list", () => {
  it("shows nothing about rails when the agent has no payout account", async () => {
    render(<AgentDetailPanel agent={{ ...base, has_stripe: true } as AgentProfile} />);
    expect(screen.queryByText(/payment rails|not reported/i)).toBeNull();
    expect(screen.queryByText(/mpp/i)).toBeNull();
    expect(screen.queryByText(/stripe_pi/i)).toBeNull();
  });

  it("does not list the relay's rails on an agent (they are the relay's, the same for every agent)", async () => {
    render(<AgentDetailPanel
      agent={{ ...base, has_stripe: false, payment_rails: ["free", "mpp", "stripe_pi", "x402"] } as AgentProfile} />);
    expect(screen.queryByText(/x402/i)).toBeNull();
    expect(screen.queryByText(/mpp/i)).toBeNull();
    expect(screen.queryByText(/payouts/i)).toBeNull();
  });

  it("shows the payout rail when the relay says this agent has a payout account", async () => {
    render(<AgentDetailPanel
      agent={{ ...base, has_stripe: true, payment_rails: ["free", "stripe_connect"] } as AgentProfile} />);
    expect(screen.getByText(/payouts/i)).toBeTruthy();
    expect(screen.getByText(/stripe connect/i)).toBeTruthy();
  });
});
