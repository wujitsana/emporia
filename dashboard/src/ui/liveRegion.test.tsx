// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Session } from "../api";
import { BLOTTO, turnLine } from "../setPlay";
import { BargainPanel } from "./BargainPanel";
import { SetPlayPanel } from "./SetPlayPanel";

// P9: a table's clock line re-renders on every 4 s poll. It is not a live region: a screen
// reader hears only a change of phase or of turn, never the clock ticking down.
vi.mock("../hooks", () => ({ useInterval: () => [null, false, null] }));

beforeEach(() => { vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline in this test")))); });
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const started = new Date(Date.now() - 60_000).toISOString();
function session(module_type: string, data: Record<string, unknown>): Session {
  return {
    session_id: "s-live", module_type, status: "active", participants: ["alpha", "beta"], current_agent: "beta",
    step_number: 1, created_at: started, state: { data, current_agent: "beta", step_number: 1, metadata: {} },
  } as Session;
}

function statusTexts(): string[] {
  return [...document.querySelectorAll('[role="status"]')].map((e) => e.textContent ?? "");
}

describe("the table's live region", () => {
  it("set play: the clock line is plain text; the status says the phase and whom it waits on", () => {
    render(<SetPlayPanel session={session(BLOTTO, {
      battlefields: 5, troops: 100, rounds: 1, round: 1, phase: "commit", commitments: { alpha: "ab".repeat(32) },
      revealed: {}, history: [], wins: { alpha: 0, beta: 0 }, move_time_limit_s: 600, phase_started_at: started,
    })} />);
    const wait = document.querySelector(".e-setplay__wait")!;
    expect(wait.textContent).toMatch(/min left/);
    expect(wait.getAttribute("role")).toBeNull();
    expect(statusTexts()).toContain("Commit phase, waiting on beta.");
    expect(statusTexts().some((t) => /left|\d+ s\b|min/.test(t))).toBe(false);
  });

  it("bargain: only a change of turn is announced", () => {
    render(<BargainPanel session={session("emporia:bargain:v2", {
      pool: { silk: 3 }, total: 10, max_offers: 6, walk_away: 0, move_time_limit_s: 600, turn_started_at: started,
      offers: [], ended: null, deal: { commitment: "ab", hands: null },
    })} />);
    const wait = document.querySelector(".e-setplay__wait")!;
    expect(wait.textContent).toMatch(/beta's turn · \d+ min left · 6 offers left/);
    expect(wait.getAttribute("role")).toBeNull();
    expect(statusTexts()).toContain("beta's turn.");
  });

  it("turnLine never carries the clock", () => {
    expect(turnLine({ phase: "reveal", pending: ["alpha", "beta"] })).toBe("Reveal phase, waiting on alpha, beta.");
    expect(turnLine({ phase: "waiting", pending: [] })).toBe("Waiting for players.");
  });
});
