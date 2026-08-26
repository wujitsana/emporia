import { describe, expect, it } from "vitest";
import { resolveDeepLinkedSession } from "./sessionSelect";
import type { Session } from "./api";

// P36-3: GamesView resolved a deep-linked session by searching `shown` — the
// list for the *currently selected tab*, already filtered by the game-type rail.
// The tab defaults to "live", so following a link to a finished game (from the
// Overview feed, an event bracket, an agent's profile) selected nothing at all
// and said nothing about it: the view just sat on whatever was already there.
// RoomsView never had this because it searches one unfiltered list.

const s = (session_id: string, status: string, module_type = "emporia:chess:v1"): Session => ({
  session_id,
  module_type,
  status,
  participants: ["a", "b"],
  current_agent: "a",
  step_number: 4,
  created_at: "2026-01-01T00:00:00+00:00",
});

const live = [s("live-1", "active"), s("live-2", "active")];
const history = [s("done-1", "completed"), s("done-2", "completed")];

describe("resolveDeepLinkedSession", () => {
  it("finds a live session and stays on the live tab", () => {
    expect(resolveDeepLinkedSession("live-2", live, history)).toEqual({
      session: live[1],
      tab: "live",
    });
  });

  it("finds a completed session and switches the tab to history -- the actual bug", () => {
    // Before the fix this returned nothing, because only the live list was searched.
    expect(resolveDeepLinkedSession("done-1", live, history)).toEqual({
      session: history[0],
      tab: "history",
    });
  });

  it("returns null when the id is in neither list, so the caller can go fetch it", () => {
    // Still reachable: the game-type rail filters both lists, and either can be
    // longer than one page. Null is the signal to fall back to GET /sessions/{id},
    // not a reason to leave the view silently unchanged.
    expect(resolveDeepLinkedSession("elsewhere", live, history)).toBeNull();
  });

  it("returns null for no id at all", () => {
    expect(resolveDeepLinkedSession(null, live, history)).toBeNull();
  });

  it("prefers the live list when an id somehow appears in both", () => {
    const dup = s("dup", "active");
    const dupDone = s("dup", "completed");
    expect(resolveDeepLinkedSession("dup", [dup], [dupDone])?.tab).toBe("live");
  });
});
