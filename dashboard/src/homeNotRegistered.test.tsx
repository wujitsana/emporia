// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RelayCtx } from "./relayCtx";
import type { RelayCtxValue } from "./relayCtx";
import { setViewer } from "./relayEnv";
import { OverviewView } from "./views/OverviewView";

const ARC = { title: "Welcome", badge: "w", xp: 10, steps: [], done: 0, total: 3, complete: false, at: "",
  next: { id: "hello", title: "Say hello", xp: 10, how: "post" } };

function body(path: string): unknown {
  if (path.startsWith("/agents/alpha/quests")) {
    return { agent_id: "alpha", xp: 0, badges: [], next: "", quests: [], arcs: [{ id: "welcome", ...ARC }],
      level: { formula: "level:v1", level: 1, xp: 0, next_at: 40, crossed: false, table: [0, 40] } };
  }
  if (path.startsWith("/health")) return { status: "ok", version: "0.1.0", modules: [], payment_rails: [] };
  return null;     // anything else: not found
}

describe("Home on a relay the agent has not joined", () => {
  beforeEach(() => {
    setViewer("alpha");
    vi.spyOn(globalThis, "fetch").mockImplementation((input: RequestInfo | URL) => {
      const path = String(input).replace(/^https?:\/\/[^/]+/, "").split("?")[0];
      const b = body(path);
      return Promise.resolve({ ok: b !== null, status: b !== null ? 200 : 404, json: async () => b } as Response);
    });
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); setViewer(null); });

  const home = (notRegistered: boolean) => render(
    <RelayCtx.Provider value={{ viewerId: "alpha", notRegistered, isSpectator: false, relayUrl: "http://r.test" } as RelayCtxValue}>
      <OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />
    </RelayCtx.Provider>,
  );

  it("shows the first-steps card for a registered agent", async () => {
    home(false);
    expect(await screen.findByText("First steps")).toBeTruthy();
  });

  it("hides the first-steps card (and its Level 1 line) when the agent is not registered", async () => {
    home(true);
    await new Promise((r) => setTimeout(r, 100));
    expect(screen.queryByText("First steps")).toBeNull();
    expect(screen.queryByText(/Level 1 · 0 XP/)).toBeNull();
  });
});
