// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Leaderboard } from "./Leaderboard";

// P288: the Craft column appears only when the relay sends ratings (gamify sheets on).
const rows = { current: [] as unknown[] };
vi.mock("../hooks", () => ({
  useInterval: () => [{ scope: "this_relay", relay_url: "", module_type: "emporia:blotto:v1",
    count: rows.current.length, standings: rows.current }, false, null],
}));
vi.mock("../api", () => ({ api: { leaderboard: vi.fn() } }));

afterEach(cleanup);

const row = (agent_id: string, extra = {}) =>
  ({ agent_id, wins: 1, entries: 2, win_rate: 0.5, modules: ["emporia:blotto:v1"], ...extra });

describe("leaderboard Craft column", () => {
  it("is absent when the relay does not rate", () => {
    rows.current = [row("alpha")];
    render(<Leaderboard moduleType="emporia:blotto:v1" />);
    expect(screen.queryByText("CRAFT")).toBeNull();
  });

  it("shows each rating, marking a provisional one", () => {
    rows.current = [row("alpha", { rating: { rating: 1612, rd: 80, provisional: false } }),
                    row("beta", { rating: { rating: 1450, rd: 290, provisional: true } }),
                    row("gamma", { rating: null })];
    render(<Leaderboard moduleType="emporia:blotto:v1" />);
    expect(screen.getByText("CRAFT")).toBeTruthy();
    expect(screen.getByText("1612 ±160")).toBeTruthy();
    expect(screen.getByText("1450 ±580 (provisional)")).toBeTruthy();
    expect(screen.getByText("—")).toBeTruthy();
  });
});
