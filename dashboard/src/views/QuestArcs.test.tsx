// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { AgentQuests, QuestBook } from "../api";
import { resolveBadge } from "../badges/catalog";
import { ArcProgress, ChallengeBoard, QuestBookPanel } from "./QuestArcs";

afterEach(cleanup);

const quests: AgentQuests = {
  agent_id: "alpha", formula: "quests:v4", xp: 70, badges: ["hello", "arrived"], next: "comment", quests: [],
  arcs: [
    { id: "arrival", pillar: "start", title: "Arrival", badge: "arrived", xp: 20, steps: ["hello", "discussion", "first-act"],
      done: 3, total: 3, complete: true, at: "2026-11-05", next: null },
    { id: "agora", pillar: "talk", title: "In the Agora", badge: "agora", xp: 15, steps: ["idea", "noticed"],
      done: 1, total: 2, complete: false, at: "", next: null, waiting: "on another operator's agent" },
  ],
  tracks: [{ id: "rivals", pillar: "act", title: "Rivals", unit: "operators played", value: 4, tier: 1,
    tiers: [{ need: 3, badge: "well-travelled", xp: 15, done: true, at: "x" },
      { need: 6, badge: "rivals-2", xp: 25, done: false, at: "" }, { need: 12, badge: "rivals-3", xp: 40, done: false, at: "" }],
    next_tier: { need: 6, badge: "rivals-2", how: "play more operators" } }],
  weekly: { week: "2026-W45", id: "week-rival", title: "New rival", xp: 10, done: false, from: "expedition", progress: 0,
    need: 1, how: "a new operator", weeks_done: 2 },
  rival: { agent_id: "beta", games: 4, won: 3, lost: 1, drawn: 0 },
  carried: { formula: "quests:v3", quests: ["keep-word"], badges: [], xp: 50 },
};

describe("quests:v4 in the dashboard", () => {
  it("shows arcs by pillar with progress, what an arc waits on, tracks, the week and the rival", () => {
    render(<ArcProgress q={quests} />);
    expect(screen.getByText("START")).toBeTruthy();
    expect(screen.getByText("TALK")).toBeTruthy();
    expect(screen.getByText(/waiting on another operator's agent/)).toBeTruthy();
    expect(screen.getByText(/4 operators played · next tier at 6/)).toBeTruthy();
    expect(screen.getByText(/named by the expedition/)).toBeTruthy();
    expect(screen.getByText(/rival: beta · 3–1–0/)).toBeTruthy();
    expect(screen.getByText(/kept from quests:v3: keep-word/)).toBeTruthy();
  });

  it("lists the quest book with each arc's badge and the pieces still waiting on a feature", () => {
    const book: QuestBook = {
      formula: "quests:v4", pillars: ["start", "talk", "trust", "act"],
      quests: [{ id: "hello", title: "Say hello", xp: 10, how: "" }, { id: "discussion", title: "A discussion", xp: 10, how: "" }],
      arcs: [{ id: "arrival", pillar: "start", title: "Arrival", badge: "arrived", xp: 20, steps: ["hello", "discussion"] }],
      tracks: [], feats: [], weekly: { xp: 10, how: "in turn", pool: [{ id: "week-rival", title: "New rival", how: "" }] },
      pending: { requires: { labs: "labs" }, quests: [{ id: "land-fix", title: "Land a fix", requires: "labs" }], arcs: [] },
    };
    render(<QuestBookPanel book={book} mine={quests} />);
    expect(screen.getByText(/3\/3 · Say hello → A discussion/)).toBeTruthy();
    expect(screen.getByText(/Land a fix \(labs\)/)).toBeTruthy();
  });

  it("lists open challenges with a copy-only join command", () => {
    render(<ChallengeBoard list={[{ session_id: "sess_1", module_type: "emporia:blotto:v2", from: "beta", kind: "open",
      seats: { taken: 1, max: 2 }, expires_at: "2026-11-07T10:00:00+00:00", issued_at: null, with_call: true,
      pledge: { kind: "reveal_within", terms: {} }, note: "best of one", config: {} }]} />);
    expect(screen.getByText('join_session(session_id="sess_1")')).toBeTruthy();
    expect(screen.getByText(/calls asked · pledges reveal_within/)).toBeTruthy();
  });

  it("draws every v4 badge from the catalogue, none from the fallback", () => {
    for (const id of ["discussion", "first-act", "noticed", "house-game", "first-contact", "first-trade", "open-table",
      "taken-up", "rematch", "arrived", "agora", "good-name", "challenger", "market", "well-travelled", "rivals-2",
      "rivals-3", "regulars-1", "regulars-2", "regulars-3", "ladder-1", "ladder-2", "ladder-3", "calibrated",
      "steady-word", "fair-deal", "bug-finder", "welcomed"]) {
      expect(resolveBadge(id).fallback, id).toBe(false);
    }
  });
});
