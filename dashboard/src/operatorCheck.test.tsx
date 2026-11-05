// @vitest-environment jsdom
import { renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useRelayCtx } from "./relayCtx";
import { RelayContextProvider } from "./relayContext";

// The page was opened as the relay's operator: a claim, until the relay says so.
vi.mock("./relayEnv", async (orig) => ({ ...(await orig<typeof import("./relayEnv")>()), VIEWER_ID: "op_agent" }));

function relayAnswers(whoami: { status: number; body?: unknown }) {
  const seen: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    seen.push(url);
    if (url.includes("/operator/whoami")) {
      return { ok: whoami.status === 200, status: whoami.status, json: async () => whoami.body } as Response;
    }
    return { ok: true, status: 200, json: async () => ({ owner_agent_id: "op_agent", relay_id: "R1" }) } as Response;
  });
  return seen;
}

const wrapper = ({ children }: { children: React.ReactNode }) => <RelayContextProvider>{children}</RelayContextProvider>;

describe("the operator's sections open on the relay's signed answer, not the claim", () => {
  afterEach(() => vi.restoreAllMocks());

  it("opens them when the relay says the signed-in viewer is its operator", async () => {
    const seen = relayAnswers({ status: 200, body: { agent_id: "op_agent", is_operator: true } });
    const { result } = renderHook(() => useRelayCtx(), { wrapper });
    await waitFor(() => expect(result.current.isRelayOperator).toBe(true));
    expect(seen.some((u) => u.includes("/operator/whoami"))).toBe(true);
  });

  it("keeps them shut when the relay says no, or refuses the sign-in", async () => {
    for (const answer of [{ status: 200, body: { agent_id: "op_agent", is_operator: false } }, { status: 401 }]) {
      const seen = relayAnswers(answer);
      const { result, unmount } = renderHook(() => useRelayCtx(), { wrapper });
      await waitFor(() => expect(result.current.relayOwner).toBe("op_agent"));
      await waitFor(() => expect(seen.some((u) => u.includes("/operator/whoami"))).toBe(true));
      expect(result.current.isRelayOperator).toBe(false);
      unmount();
      vi.restoreAllMocks();
    }
  });
});
