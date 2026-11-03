// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CharacterSheet } from "../api";
import { GamifyView, Sheet } from "./GamifyView";

// P288: the gamify tab says when a relay does not run the module, and shows a sheet as signed.
const card = { current: { gamify: [] as string[] } };
vi.mock("../hooks", () => ({
  useInterval: (fn: () => unknown) => {
    const src = String(fn);
    if (src.includes("relayCard")) return [card.current, false, null];
    if (src.includes("ratings")) return [{ module_type: "emporia:blotto:v1", ratings: [] }, false, null];
    if (src.includes("agents")) return [{ agents: [{ agent_id: "alpha" }], count: 1 }, false, null];
    return [null, true, null];
  },
}));
vi.mock("../api", () => ({ api: { relayCard: vi.fn(), agents: vi.fn(), sheet: vi.fn(), ratings: vi.fn() } }));
vi.mock("../relayEnv", () => ({ VIEWER_ID: "alpha" }));

afterEach(cleanup);

const sheet: CharacterSheet = {
  formula: "progression:v1", agent_id: "alpha", class: "Strategist",
  levels: { craft: 4, trade: 1, trust: 2 }, adjusted: true,
  craft: [{ module_type: "emporia:blotto:v1", rating: 1641, rd: 46, games: 60, opponents: 10, provisional: false }],
  trade: { score: 0, level: 1, sold: 0, bought: 0, disputed_as_seller: 0, not_delivered: 0 },
  trust: { score: 3.2, level: 2, counterparties: 10, weeks_active: 1, abandoned_on_turn: 0 },
  sessions_counted: 60, as_of: "2026-11-03T23:00:00+00:00", signature: "sig",
};

describe("gamify tab", () => {
  it("says so when the relay does not run the module", () => {
    card.current = { gamify: [] };
    render(<GamifyView />);
    expect(screen.getByText(/does not run the gamify module/)).toBeTruthy();
  });

  it("lists the agents when sheets are on", () => {
    card.current = { gamify: ["sheets"] };
    render(<GamifyView />);
    expect(screen.getByText("you")).toBeTruthy();
    expect(screen.getByText(/This relay runs: sheets/)).toBeTruthy();
  });

  it("shows a sheet: class, levels, Craft per game, the adjustment, the ladder", () => {
    render(<Sheet sheet={sheet} />);
    expect(screen.getByText("alpha — Strategist")).toBeTruthy();
    expect(screen.getByText("Craft 4 · Trade 1 · Trust 2")).toBeTruthy();
    expect(screen.getByText("1641 ±92")).toBeTruthy();
    expect(screen.getByText(/Adjusted: games and deals/)).toBeTruthy();
    expect(screen.getByText("LADDER — blotto, this relay")).toBeTruthy();
  });
});
