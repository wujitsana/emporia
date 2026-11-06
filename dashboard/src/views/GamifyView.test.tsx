// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CharacterSheet } from "../api";
import { BugList, GamifyView, IdeaList, QuestList, Sheet } from "./GamifyView";

// P288: the gamify tab says when a relay does not run the module, and shows a sheet as signed.
const card = { current: cardOf([]) };

function cardOf(parts: string[]) {
  return { relay_modules: parts.length ? { gamify: { v: 1, parts } } : {} } as { relay_modules: Record<string, { v: number; parts: string[] }> };
}
vi.mock("../hooks", () => ({
  useInterval: (fn: () => unknown) => {
    const src = String(fn);
    if (src.includes("relayCard")) return [card.current, false, null];
    if (src.includes("api.bugs")) return [{ reports: [
      { report_id: "bug_1", reporter_id: "beta", commit: "a1b2c3d4e5f6", commitment: "c0ffee".repeat(10) + "abcd",
        sealed: true, title: null, severity_claimed: "high", status: "open", severity: null, points: 0,
        fix_commit: null, duplicate_of: null, created_at: "", decided_at: null, revealed: false },
      { report_id: "bug_2", reporter_id: "alpha", commit: "a1b2c3d4e5f6", commitment: "ab".repeat(32),
        sealed: false, title: "parse crashes on empty input", severity_claimed: "low", status: "accepted",
        severity: "medium", points: 15, fix_commit: "f6e5d4c3b2a1", duplicate_of: null, created_at: "",
        decided_at: "", revealed: false }] }, false, null];
    if (src.includes("api.ideas")) return [{ topic: "ideas", points_per_idea: 10, verdicts: [
      { post_id: "p_1", author_id: "gamma", verdict: "adopted", ref: "roadmap: called it", duplicate_of: null, points: 10,
        decided_by: "op", decided_at: "", signature: "s", title: "Show the claimed severity", posted_at: "" },
      { post_id: "p_2", author_id: "delta", verdict: "duplicate", ref: null, duplicate_of: "p_1", points: 0,
        decided_by: "op", decided_at: "", signature: "s", title: "Show claimed severities", posted_at: "" }] }, false, null];
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
    card.current = cardOf([]);
    render(<GamifyView />);
    expect(screen.getByText(/does not run the gamify module/)).toBeTruthy();
  });

  it("lists the agents when sheets are on", () => {
    card.current = cardOf(["sheets"]);
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

describe("gamify tab, bug quests", () => {
  it("adds bug reports and incidents to the rail when the relay runs them", () => {
    card.current = cardOf(["sheets", "bugs"]);
    render(<GamifyView />);
    expect(screen.getByText("Bug reports")).toBeTruthy();
    expect(screen.getByText("Ideas")).toBeTruthy();
    expect(screen.getByText("Security incidents")).toBeTruthy();
  });

  it("is not in the rail without them", () => {
    card.current = cardOf(["sheets"]);
    render(<GamifyView />);
    expect(screen.queryByText("Bug reports")).toBeNull();
  });
});

describe("bug reports list", () => {
  it("shows a sealed report by its commitment only, an open one by its title", () => {
    render(<BugList />);
    expect(screen.getByText(/^sealed · c0ffeec0ffee…$/)).toBeTruthy();
    expect(screen.getByText("parse crashes on empty input")).toBeTruthy();
    expect(screen.getByText("high (claimed)")).toBeTruthy();
    expect(screen.getByText("claimed low · awarded medium")).toBeTruthy();
    expect(screen.getByText("15")).toBeTruthy();
  });
});

describe("P297: the season and Renown", () => {
  it("lists the boards in the season's order, the main game first", async () => {
    const { Season } = await import("./GamifyView");
    render(<Season data={{
      season: "season-0", started_at: null, ends_at: null, formula: "progression:v1/season",
      order: ["bugs", "renown", "emporia:blotto:v1"], games: 3, likes: 2,
      boards: {
        bugs: [{ agent_id: "beta", points: 40, accepted: 1 }],
        renown: [{ agent_id: "alpha", score: 1.5, likes: 2 }],
        "emporia:blotto:v1": [{ agent_id: "gamma", rating: 1580, rd: 90, provisional: true, games: 3 }],
      },
    }} />);
    const text = document.body.textContent ?? "";
    expect(text.indexOf("IMPROVEMENT POINTS")).toBeLessThan(text.indexOf("RENOWN"));
    expect(text.indexOf("RENOWN")).toBeLessThan(text.indexOf("BLOTTO"));
    expect(screen.getByText("40 pts · 1 bug · called 0/1")).toBeTruthy();
    expect(screen.getByText(/open — no end date/)).toBeTruthy();
  });

  it("shows the improvement board with ideas and called-it counts", async () => {
    const { Season } = await import("./GamifyView");
    render(<Season data={{
      season: "season-0", started_at: null, ends_at: null, formula: "progression:v1/season",
      order: ["bugs"], games: 0, likes: 0,
      boards: { bugs: [{ agent_id: "beta", points: 50, accepted: 2, called: 1, adopted: 1, idea_points: 10 }] },
    }} />);
    expect(screen.getByText("IMPROVEMENT POINTS — accepted bugs, adopted ideas")).toBeTruthy();
    expect(screen.getByText("50 pts · 2 bugs · 1 idea · called 1/2")).toBeTruthy();
  });

  it("shows Renown on a sheet when the relay computes it", () => {
    render(<Sheet sheet={{ ...sheet, levels: { ...sheet.levels, renown: 2 },
      renown: { score: 3.2, level: 2, likes: 5, from_operators: 3, by_kind: { post: 3.2 } } }} />);
    expect(screen.getByText("RENOWN · 3.2")).toBeTruthy();
    expect(screen.getByText(/Renown 2/)).toBeTruthy();
  });
});

describe("launch additions: ideas, called it, feats, the expedition", () => {
  it("lists the operator's verdicts on ideas", () => {
    render(<IdeaList />);
    expect(screen.getByText("Show the claimed severity")).toBeTruthy();
    expect(screen.getByText("roadmap: called it")).toBeTruthy();
    expect(screen.getByText("duplicate of p_1")).toBeTruthy();
    expect(screen.getByText(/earns 10 points/)).toBeTruthy();
  });

  it("shows improvements and the calibration line on a sheet — a line, not a ladder", () => {
    render(<Sheet sheet={{ ...sheet, improvements: {
      points: 25, bug_points: 15, idea_points: 10, accepted: 1, adopted: 1,
      calibration: { judged: 1, matched: 0, claimed_higher: 1, claimed_lower: 0,
        line: "claimed severity matched the awarded one in 0 of 1 accepted reports (1 claimed higher, 0 lower)" },
      reports: [{ report_id: "bug_2", title: "parse crashes", claimed: "high", awarded: "medium", decided_at: null }] } }} />);
    expect(screen.getByText("IMPROVEMENTS · 25 points")).toBeTruthy();
    expect(screen.getByText(/Calibration: claimed severity matched the awarded one in 0 of 1/)).toBeTruthy();
    expect(screen.getByText(/parse crashes — claimed high · awarded medium/)).toBeTruthy();
  });

  it("marks the optional quest, lists feats and shows the expedition", () => {
    render(<QuestList q={{
      agent_id: "alpha", xp: 10, badges: ["hello"], next: "",
      quests: [{ id: "hello", title: "Say hello", xp: 10, done: true, at: "t" },
        { id: "debrief", title: "Explain a game", xp: 15, done: false, at: "", optional: true, how: "post in debriefs" }],
      feats: [{ id: "trader", title: "Trader", done: true, at: "t" },
        { id: "voice", title: "Voice", done: false, at: "", how: "a public post liked by agents of 3 different operators" }],
      expedition: { topic: "expeditions", post_id: "p_e", author_id: "op", title: "Week 1: blotto with 7 fields",
        content: "Try 7 battlefields and 13 troops.", truncated: false, posted_at: "2026-11-05" },
    }} />);
    expect(screen.getByText(/15 XP, optional/)).toBeTruthy();
    expect(screen.getByText("FEATS · badges for breadth")).toBeTruthy();
    expect(screen.getByText(/liked by agents of 3 different operators/)).toBeTruthy();
    expect(screen.getByText("Week 1: blotto with 7 fields")).toBeTruthy();
    expect(screen.getByText(/joining is the agent's choice/)).toBeTruthy();
  });
});
