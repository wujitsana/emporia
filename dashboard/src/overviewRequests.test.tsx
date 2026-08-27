// @vitest-environment jsdom
import { render, act } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OverviewView } from "./views/OverviewView";

// P43-2: the Overview's request behaviour, measured by running it rather than
// read off the call sites. Before this pass it opened with three concurrent
// /health pollers -- App's, its own, and PaymentsFeesSection's, which is handed
// App's health as a `seed` prop and polled for it anyway -- and two fanouts over
// the same seven collections on 15s and 20s timers.

function bodyFor(path: string): unknown {
  if (path.startsWith("/health")) {
    return { status: "ok", version: "0.1.0", modules: [], payment_rails: [] };
  }
  if (path.startsWith("/payments/settlements")) return { settlements: [] };
  if (path.startsWith("/sessions")) return { sessions: [], count: 0 };
  if (path.startsWith("/listings")) return { listings: [], count: 0 };
  if (path.startsWith("/rooms")) return { rooms: [], count: 0 };
  if (path.startsWith("/events")) return { events: [], count: 0 };
  if (path.startsWith("/agents")) return { agents: [], count: 0 };
  if (path.startsWith("/agoras")) return { topics: [], count: 0 };
  if (path.startsWith("/safety")) return { stats: {} };
  if (path.startsWith("/federation")) return { peers: [] };
  return [];
}

/** Requests grouped by endpoint, ignoring query strings. */
function tally(calls: string[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const url of calls) {
    const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    out[path] = (out[path] ?? 0) + 1;
  }
  return out;
}

describe("Overview request behaviour", () => {
  let calls: string[];

  beforeEach(() => {
    calls = [];
    vi.useFakeTimers();
    vi.spyOn(globalThis, "fetch").mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      calls.push(url);
      const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => bodyFor(path),
      } as Response);
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  async function runFor(ms: number) {
    render(
      <OverviewView events={[]} seed={null} wsConnected={false} navigate={() => {}} />
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
    return tally(calls);
  }

  it("polls /health nowhere in the Overview subtree", async () => {
    const seen = await runFor(60_000);
    // App owns the one poll and provides it through RelayHealthCtx. Rendered
    // without that provider, this subtree must request /health zero times --
    // it measured 20 per minute before, from two pollers, one of which was
    // already being handed the value as a prop.
    expect(seen["/health"] ?? 0).toBe(0);
  });

  it("fetches each collection once per cycle, not twice", async () => {
    const seen = await runFor(60_000);
    // One 15s fanout: mount + 4 ticks. Nine each before, from two fanouts on
    // 15s and 20s timers over the same seven collections.
    for (const path of ["/listings", "/rooms", "/events", "/agoras/topics", "/agents"]) {
      expect(seen[path], `${path} was fetched ${seen[path]} times`).toBe(5);
    }
    // ...settlements included: PaymentsFeesSection polled it on its own 15s
    // timer while the counts fanout fetched the same rows.
    expect(seen["/payments/settlements"]).toBe(5);
  });
});
