// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SeasonXpBoard } from "../api";
import { HomeCardsView } from "./HomeCardsView";

// Home's Agents group carries a Season XP card (top five); no season board on the relay, no card.
const data: { board: SeasonXpBoard | null } = { board: null };
vi.mock("../hooks", () => ({
  useInterval: (fn: () => unknown) => (String(fn).includes("seasonBoard") ? [data.board, false, null] : [null, true, null]),
}));

beforeEach(() => { vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline in this test")))); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); data.board = null; });

const row = (n: number) => ({ rank: n, agent_id: `agent${n}`, season_xp: 200 - n, level: 3, badges: 1 });

describe("Home: Season XP card", () => {
  it("shows the top five and opens the Season view", () => {
    data.board = { season: "s", formula: "f", records_hash: "h", final: false, total: 7, window: { started_at: null, ends_at: null },
      standings: [1, 2, 3, 4, 5, 6, 7].map(row), you: null };
    const navigate = vi.fn();
    render(<HomeCardsView viewer="agent2" navigate={navigate} />);
    expect(screen.getByText("Agents")).toBeTruthy();
    const body = document.querySelector(".e-hcard__body")?.textContent ?? "";
    expect(body.split("\n")).toHaveLength(5);
    expect(body).toContain("agent2 (you)");
    expect(body).toContain("198 XP · level 3");
    expect(body).not.toContain("agent6");
    fireEvent.click(document.querySelector(".e-hcard__body")!.closest(".e-hcard")!);
    expect(navigate).toHaveBeenCalledWith("season");
  });

  it("draws no card when the relay has no season board", () => {
    render(<HomeCardsView viewer={null} navigate={vi.fn()} />);
    expect(screen.queryByText("SEASON XP")).toBeNull();
    expect(document.querySelector(".e-hcard__body")).toBeNull();
  });
});
