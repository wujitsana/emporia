// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";

import { AgentDetailPanel } from "./AgentsView";
import type { AgentProfile } from "../api";

// An agent card never lists the relay's payment rails: they are the relay's list, the same for every agent,
// and showing them on an agent said the agent offered them. Absence must read as absence, the same rule the
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

describe("an agent card does not show the relay's rails", () => {
  it("does not list the relay's rails on an agent (they are the relay's, the same for every agent)", async () => {
    render(<AgentDetailPanel agent={{ ...base, payment_rails: ["free", "x402"] } as AgentProfile} />);
    expect(screen.queryByText(/payment rails|not reported/i)).toBeNull();
    expect(screen.queryByText(/x402/i)).toBeNull();
    expect(screen.queryByText(/payouts/i)).toBeNull();
  });
});
