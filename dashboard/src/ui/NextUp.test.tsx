// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RelayCtx, type RelayCtxValue } from "../relayCtx";
import { NextUp } from "./NextUp";

// Home's "Next up": GET /agents/{id}/next, readable only by that agent. The panel hides when
// the relay does not serve the route or the viewer is not the agent, and never acts.
const state: { data: unknown; error: string | null } = { data: null, error: null };
vi.mock("../hooks", () => ({ useInterval: () => [state.data, false, state.error] }));
vi.mock("../api", () => ({ api: { agentNext: vi.fn() } }));

afterEach(() => {
  cleanup();
  state.data = null;
  state.error = null;
});

function withViewer(node: ReactNode, isSpectator = false) {
  return <RelayCtx.Provider value={{ isSpectator } as RelayCtxValue}>{node}</RelayCtx.Provider>;
}

const facts = {
  agent_id: "alpha",
  sessions: [{ session_id: "s-1", module_type: "emporia:blotto:v2", status: "active", move: "commit", clock_left_s: 200 }],
  inbox: { invites: [{ type: "challenge", session_id: "s-2", from: "mallory", module_type: "emporia:chess:v1" }] },
};

describe("Next up", () => {
  it("draws the ranked items as cards with their clocks, urgent first", () => {
    state.data = facts;
    const { container } = render(withViewer(<NextUp agentId="alpha" />));
    expect(screen.getByRole("region", { name: "Next steps" })).toBeTruthy();
    expect(screen.getByText("3 min left")).toBeTruthy();
    expect(container.querySelectorAll(".e-hcard")).toHaveLength(2);
    expect(container.querySelector(".e-now__row")).toBeNull();        // no list rows any more
    expect(screen.getByText("2 to do")).toBeTruthy();
  });

  it("shows another agent's words as plain text in the caption", () => {
    state.data = facts;
    render(withViewer(<NextUp agentId="alpha" />));
    expect(screen.getByText(/mallory/)).toBeTruthy();
  });

  it("says when nothing waits on the agent", () => {
    state.data = { agent_id: "alpha", sessions: [], inbox: {} };
    render(withViewer(<NextUp agentId="alpha" />));
    expect(screen.getByText(/Nothing waits on alpha right now/)).toBeTruthy();
    expect(screen.getByText("all clear")).toBeTruthy();
  });

  it("is not drawn on a relay without the route (404)", () => {
    state.error = "Error: 404 /agents/alpha/next";
    const { container } = render(withViewer(<NextUp agentId="alpha" />));
    expect(container.innerHTML).toBe("");
  });

  it("is not drawn for a viewer the relay does not take for the agent (403), even over old data", () => {
    state.data = facts;
    state.error = "Error: 403 /agents/alpha/next";
    const { container } = render(withViewer(<NextUp agentId="alpha" />));
    expect(container.innerHTML).toBe("");
  });

  it("is not drawn for a spectator, or with no agent", () => {
    state.data = facts;
    expect(render(withViewer(<NextUp agentId="alpha" />, true)).container.innerHTML).toBe("");
    cleanup();
    expect(render(withViewer(<NextUp agentId={null} />)).container.innerHTML).toBe("");
  });

  it("says a failed read failed, rather than showing an empty list", () => {
    state.error = "Error: 500 /agents/alpha/next";
    render(withViewer(<NextUp agentId="alpha" />));
    expect(screen.getByText(/did not load. This is not an empty list/)).toBeTruthy();
  });

  it("draws nothing while loading", () => {
    const { container } = render(withViewer(<NextUp agentId="alpha" />));
    expect(container.innerHTML).toBe("");
  });
});
