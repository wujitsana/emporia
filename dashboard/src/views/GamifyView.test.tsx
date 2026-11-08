// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CharacterSheet } from "../api";
import { GamifyView, QuestList, Sheet } from "./GamifyView";

// P288: the gamify tab says when a relay does not run the module, and shows a sheet as signed.
const card = { current: cardOf([]) };

function cardOf(parts: string[]) {
  return { relay_modules: parts.length ? { gamify: { v: 1, parts } } : {} } as { relay_modules: Record<string, { v: number; parts: string[] }> };
}
vi.mock("../hooks", () => ({
  useInterval: (fn: () => unknown) => {
    const src = String(fn);
    if (src.includes("relayCard")) return [card.current, false, null];
    if (src.includes("ratings")) return [{ module_type: "emporia:blotto:v1", ratings: [] }, false, null];
    if (src.includes("api.pledges")) return [[{ pledges: [{ pledge_id: "pl_1", maker_id: "alpha", to_agent: "bob",
      kind: "deliver_by", public: true, made_at: "2026-11-05T10:00:00Z", status: "kept", resolved_by: "evidence",
      evidence: { delivered_at: "2026-11-05T11:00:00Z", by: "2026-11-05T12:00:00Z" } }] }, { pledges: [] }], false, null];
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

describe("gamify tab, ideas", () => {
  it("has no ideas page in the rail: adopted ideas are on the season board, from the labs' reports", () => {
    card.current = cardOf(["sheets", "quests"]);
    render(<GamifyView />);
    expect(screen.queryByText("Ideas")).toBeNull();
    expect(screen.getByText("Season")).toBeTruthy();
    expect(screen.queryByText("Bug reports")).toBeNull();
    expect(screen.queryByText("Security incidents")).toBeNull();
  });
});

describe("P297: the season and Renown", () => {
  it("lists the boards in the season's order, the main game first", async () => {
    const { Season } = await import("./GamifyView");
    render(<Season data={{
      season: "season-0", started_at: null, ends_at: null, formula: "progression:v1/season",
      order: ["ideas", "renown", "emporia:blotto:v1"], games: 3, likes: 2,
      boards: {
        ideas: [{ agent_id: "beta", points: 40, adopted: 4 }],
        renown: [{ agent_id: "alpha", score: 1.5, likes: 2 }],
        "emporia:blotto:v1": [{ agent_id: "gamma", rating: 1580, rd: 90, provisional: true, games: 3 }],
      },
    }} />);
    const text = document.body.textContent ?? "";
    expect(text.indexOf("IMPROVEMENT POINTS")).toBeLessThan(text.indexOf("RENOWN"));
    expect(text.indexOf("RENOWN")).toBeLessThan(text.indexOf("BLOTTO"));
    expect(screen.getByText("40 pts · 4 ideas")).toBeTruthy();
    expect(screen.getByText(/open — no end date/)).toBeTruthy();
  });

  it("shows the improvement board with adopted ideas", async () => {
    const { Season } = await import("./GamifyView");
    render(<Season data={{
      season: "season-0", started_at: null, ends_at: null, formula: "progression:v1/season",
      order: ["ideas"], games: 0, likes: 0,
      boards: { ideas: [{ agent_id: "beta", points: 10, adopted: 1 }] },
    }} />);
    expect(screen.getByText("IMPROVEMENT POINTS — adopted ideas, named in a lab report")).toBeTruthy();
    expect(screen.getByText("10 pts · 1 idea")).toBeTruthy();
  });

  it("shows Renown on a sheet when the relay computes it", () => {
    render(<Sheet sheet={{ ...sheet, levels: { ...sheet.levels, renown: 2 },
      renown: { score: 3.2, level: 2, likes: 5, from_operators: 3, by_kind: { post: 3.2 } } }} />);
    expect(screen.getByText("RENOWN · 3.2")).toBeTruthy();
    expect(screen.getByText(/Renown 2/)).toBeTruthy();
  });
});

describe("launch additions: ideas, feats, the expedition", () => {
  it("shows improvements on a sheet", () => {
    render(<Sheet sheet={{ ...sheet, improvements: { agent_id: "alpha", points: 10, adopted: 1, badges: ["Idea adopted"] } }} />);
    expect(screen.getByText("IMPROVEMENTS · 10 points")).toBeTruthy();
    expect(screen.getByText(/1 ideas adopted/)).toBeTruthy();
  });

  it("marks the optional quest, lists feats and shows the expedition", () => {
    render(<QuestList q={{
      agent_id: "alpha", xp: 10, badges: ["hello"], next: "",
      quests: [{ id: "hello", title: "Say hello", xp: 10, done: true, at: "t" },
        { id: "debrief", title: "Explain a game", xp: 15, done: false, at: "", optional: true, how: "post in debriefs" }],
      play: { formula: "play:v1", xp: 6, week: 4, today: 2, day_cap: 10, operator_today: 2, operator_cap: 20, pair_week_cap: 3 },
      feats: [{ id: "trader", title: "Trader", done: true, at: "t" },
        { id: "voice", title: "Voice", done: false, at: "", how: "a public post liked by agents of 3 different operators" }],
      expedition: { topic: "expeditions", post_id: "p_e", author_id: "op", title: "Week 1: blotto with 7 fields",
        content: "Try 7 battlefields and 13 troops.", truncated: false, posted_at: "2026-11-05" },
    }} />);
    expect(screen.getByText(/15 XP, optional/)).toBeTruthy();
    expect(screen.getByText(/XP from play this week: 4 · today 2 \/ 10/)).toBeTruthy();
    expect(screen.getByText("FEATS · badges for breadth")).toBeTruthy();
    expect(screen.getByText(/liked by agents of 3 different operators/)).toBeTruthy();
    expect(screen.getByText("Week 1: blotto with 7 fields")).toBeTruthy();
    expect(screen.getByText(/joining is the agent's choice/)).toBeTruthy();
    // ui-v2: each quest and feat row leads with its pixel badge, the earned ones in a strip.
    for (const id of ["hello", "debrief", "trader", "voice"]) {
      expect(document.querySelector(`.e-quest-row [data-badge="${id}"]`)).not.toBeNull();
    }
    expect(document.querySelector(`.e-badge-strip [data-badge="hello"]`)).not.toBeNull();
    expect(screen.getAllByRole("img", { name: "Say hello" }).length).toBeGreaterThan(0);
  });
});

describe("calls and pledges: Insight and the Word line", () => {
  it("puts the Insight board among the others, in the relay's order, with its rule", async () => {
    const { Season } = await import("./GamifyView");
    render(<Season data={{
      season: "season-0", started_at: null, ends_at: null, formula: "progression:v1/season",
      order: ["ideas", "renown", "insight", "emporia:blotto:v2"], games: 3, likes: 2, insight_min_calls: 10,
      boards: { ideas: [], renown: [], insight: [{ agent_id: "gamma", insight: 0.214, se: 0.05, total: 2.6, games: 12 }],
        "emporia:blotto:v2": [] },
    }} />);
    const text = document.body.textContent ?? "";
    expect(text.indexOf("RENOWN")).toBeLessThan(text.indexOf("INSIGHT"));
    expect(text.indexOf("INSIGHT")).toBeLessThan(text.indexOf("BLOTTO"));
    expect(screen.getByText("+0.21 ±0.10 per game · 12 games")).toBeTruthy();
    expect(screen.getByText(/from 10 calls made with a settled rating/)).toBeTruthy();
  });

  // Review pass 2: a craft row opened its ladder on a click only.
  it("opens a game's ladder from the keyboard too", () => {
    render(<Sheet sheet={sheet} />);
    const row = screen.getByRole("row", { name: /blotto: show this relay's ladder/ });
    expect(row.getAttribute("tabindex")).toBe("0");
    expect(row.getAttribute("aria-current")).toBe("true");
  });

  it("says what an empty Insight board waits for", async () => {
    const { Season } = await import("./GamifyView");
    render(<Season data={{ season: "season-0", started_at: null, ends_at: null, formula: "progression:v1/season",
      order: ["insight"], games: 0, likes: 0, boards: { insight: [] } }} />);
    expect(screen.getByText(/Nobody on the Insight board yet/)).toBeTruthy();
  });

  it("shows the sheet's Insight block and its Word line, not in Trust", () => {
    render(<Sheet sheet={{ ...sheet,
      insight: { formula: "progression:v1/insight", games: 2, insight: -0.31, se: 0.2, total: -0.62, settled_games: 1,
        forfeited: 1, why: "calls fell short of the rating's own forecast by 0.31 per game over 2 games",
        board: "on the season board from 10 calls made once the rating had settled: 1 so far",
        calibration: { bins: [], line: "near 70%: 1 of 1 won" },
        recent: [{ session_id: "s1", module_type: "emporia:blotto:v2", call: 0.7, forecast: 0.5, outcome: 1, edge: 0.37 },
          { session_id: "s2", module_type: "emporia:two-thirds:v2", call: null, forfeited: true, forecast: 0.5, outcome: 0, edge: -3.9 }] },
      pledges: { kept: 3, broken: 1, disputed: 0, kept_with_evidence: 2, open: 1, void: 0,
        last_30_days: { kept: 2, broken: 1, disputed: 0 }, line: "kept 3 of 4 resolved (1 broken, 0 disputed)",
        recent: [{ kind: "deliver_by", status: "kept", resolved_by: "evidence", private: true }],
        not_counted: 0, why: [], in_trust: false },
    }} />);
    expect(screen.getByText(/INSIGHT · −0.31 per game/)).toBeTruthy();
    expect(screen.getByText("unrevealed")).toBeTruthy();
    expect(screen.getByText("70 %")).toBeTruthy();
    expect(screen.getByText(/kept 3 of 4 resolved .* shown, not in Trust/)).toBeTruthy();
    expect(screen.getByText(/deliver_by — kept \(evidence\) · a private deal/)).toBeTruthy();
    // Under the Word line, the agent's pledges themselves (GET /pledges).
    const table = screen.getByRole("table", { name: "Pledges of alpha" });
    expect(table.textContent).toMatch(/deliver by.*to bob.*kept.*delivered/);
  });

  it("has no pledges list when the relay does not run pledges", () => {
    render(<Sheet sheet={sheet} />);
    expect(screen.queryByRole("table", { name: /Pledges of/ })).toBeNull();
  });
});

describe("P1: Season on a phone", () => {
  it("opens on the detail (the season's boards), the rail one back away", () => {
    card.current = cardOf(["sheets", "quests"]);
    const { container } = render(<GamifyView />);
    const layout = container.querySelector("[data-pane-shown]") as HTMLElement;
    expect(layout.dataset.paneShown).toBe("detail");
    expect(screen.getByRole("button", { name: /quests, sheets/ })).toBeTruthy();
  });
});

describe("season XP board", () => {
  it("shows the standings with level and badges, and marks a signed final one", async () => {
    const { SeasonXp } = await import("./GamifyView");
    render(<SeasonXp board={{
      season: "season-0", formula: "season:v1/xp", records_hash: "h", final: true, total: 3,
      window: { started_at: "2026-11-01T00:00:00+00:00", ends_at: "2026-11-30T00:00:00+00:00" },
      standings: [{ rank: 1, agent_id: "alpha", season_xp: 120, level: 3, badges: 7 }],
      you: { rank: 3, agent_id: "gamma", season_xp: 10, level: 1, badges: 1 },
    }} />);
    expect(screen.getByText("SEASON XP — final standing")).toBeTruthy();
    expect(screen.getByText("120 XP · level 3 · 7 badges")).toBeTruthy();
    expect(screen.getByText(/you: #3 · 10 XP · level 1/)).toBeTruthy();
  });
});
