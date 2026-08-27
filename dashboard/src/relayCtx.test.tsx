// @vitest-environment jsdom
import { renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useRelayCtx } from "./relayCtx";
import { RelayContextProvider } from "./relayContext";

// P42: `useRelayCtx` and `RelayContextProvider` were split into two modules so
// the provider's file exports only a component (Fast Refresh needs that). The
// split is the kind of change typecheck cannot validate — the context object
// has to be the SAME object on both sides, and importing a second copy would
// typecheck perfectly while silently handing every consumer the DEFAULT value.

describe("relay context wiring", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({
        owner_agent_id: "operator_agent",
        relay_id: "R1",
        relay_url: "http://relay.test",
        require_nous: false,
        write_requires_nous: true,
        require_challenge: true,
        header_auth_trusted: true,
        agent_count: 7,
        active_session_count: 2,
        version: "0.1.0",
      }),
    } as Response);
  });
  afterEach(() => vi.restoreAllMocks());

  it("delivers the provider's value to the hook, not the default", async () => {
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <RelayContextProvider>{children}</RelayContextProvider>
    );
    const { result } = renderHook(() => useRelayCtx(), { wrapper });

    await waitFor(() => expect(result.current.relayId).toBe("R1"));
    expect(result.current.agentCount).toBe(7);
    expect(result.current.requireChallenge).toBe(true);
    expect(result.current.headerAuthTrusted).toBe(true);
    // The DEFAULT has relayId null and agentCount 0 — reaching it would mean the
    // two modules are holding different context objects.
    expect(result.current.relayId).not.toBeNull();
  });

  it("falls back to the default outside a provider", () => {
    const { result } = renderHook(() => useRelayCtx());
    expect(result.current.relayId).toBeNull();
    expect(result.current.isSpectator).toBe(true);
  });
});
