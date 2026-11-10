// @vitest-environment jsdom
import { render, act } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OverviewView } from "./views/OverviewView";

// P43-2: the Home's request behaviour, measured by running it rather than read off the call sites.
// App owns the one /health poll; Home asks for what it shows (live games, what is happening, agents,
// the relay's facts) and no collection twice in one cycle.

function bodyFor(path: string): unknown {
  if (path.startsWith("/health")) {
    return { status: "ok", version: "0.1.0", modules: [], payment_rails: [] };
  }
  if (path.startsWith("/happening")) return { since: "", next_since: "", topics: [], rooms: [], top_posts: [], labs: [], untrusted: "" };
  if (path.startsWith("/sessions/open")) return { open_sessions: [] };
  if (path.startsWith("/sessions")) return { sessions: [], count: 0 };
  if (path.startsWith("/challenges")) return { challenges: [] };
  if (path.startsWith("/relay/summary")) return { agents: 1, operators: 1, open_challenges: 0, active_topics: 0, labs: 0 };
  if (path.startsWith("/agents")) return { agents: [{ agent_id: "x" }], count: 1 };
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

describe("Home request behaviour", () => {
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

  it("polls /health nowhere in the Home subtree", async () => {
    const seen = await runFor(60_000);
    // App owns the one poll and provides it through RelayHealthCtx. Rendered
    // without that provider, this subtree must request /health zero times.
    expect(seen["/health"] ?? 0).toBe(0);
  });

  it("reads what is happening on the relay about twice a minute, and nothing it does not draw", async () => {
    const seen = await runFor(60_000);
    // 30 s timer over 60 s: mount and two ticks.
    expect(seen["/happening"], `/happening was fetched ${seen["/happening"]} times`).toBe(3);
    // Nothing from the old hub: no per-collection fanout.
    for (const path of ["/rooms", "/agoras/topics", "/payments/settlements", "/safety/stats", "/federation/peers"]) {
      expect(seen[path] ?? 0, path).toBe(0);
    }
  });
});
