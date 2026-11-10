// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Session } from "../api";
import { GamesView } from "./GamesView";
import { sessionMatches } from "../sessionSelect";

// A deep link opens its session once. After that a click on another session sticks: each poll
// hands the view new lists, and re-applying the link on every poll snapped back to it.

const H = "ab".repeat(32);
const session = (id: string, data: Record<string, unknown> = {}): Session => ({
  session_id: id, module_type: "emporia:blotto:v2", status: "active", participants: ["alpha", "beta"],
  current_agent: "alpha", step_number: 1, created_at: "2026-11-03T12:00:00Z",
  state: { data: { battlefields: 5, troops: 100, rounds: 1, round: 1, phase: "commit", commitments: {}, revealed: {},
    history: [], wins: {}, ...data } },
});
const FIRST = "sess_first_aaaaaaaaaaaa1111";
const SECOND = "sess_second_bbbbbbbbbbb2222";
const LIVE = [
  session(FIRST),
  session(SECOND, { phase: "reveal", commitments: { alpha: H, beta: H }, revealed: { alpha: [30, 30, 30, 5, 5] } }),
];

// Each "poll" is a new list object with the same sessions, as the real useInterval returns.
// The live list polls every 6 s; a set-play table's own follow-up reads (4 s) return nothing.
const polls = { n: 0, cache: new Map<string, unknown>() };
vi.mock("../hooks", () => ({
  useInterval: (_fn: unknown, ms: number) => {
    if (ms === 4_000) return [null, true, null];
    const key = `${ms}:${polls.n}`;
    if (!polls.cache.has(key)) {
      polls.cache.set(key, { sessions: ms === 6_000 ? LIVE.map((s) => ({ ...s })) : [] });
    }
    return [polls.cache.get(key), false, null];
  },
}));
vi.mock("../api", () => ({
  api: {
    sessionActions: vi.fn(() => Promise.resolve({ actions: [] })),
    session: vi.fn(() => Promise.reject(new Error("not needed"))),
  },
  dashboardToken: () => "",
  looksLikeEncryptedEnvelope: () => false,
}));
vi.mock("../transport", async (orig) => ({ ...(await orig<object>()), relaySocket: vi.fn(() => new Promise(() => {})) }));
vi.mock("../ui/AuditBadge", () => ({ AuditBadge: () => null }));
vi.mock("../ui/VerifyBadge", () => ({ VerifyBadge: () => null }));
vi.mock("../ui/GameChat", () => ({ GameChat: () => null }));
vi.mock("../ui/GameTalk", () => ({ SaidLine: () => null, AfterTheGame: () => null }));
vi.mock("../useSaid", () => ({ useSaid: () => ({ messages: [], opened: {} }) }));
vi.mock("../ui/SessionMoney", () => ({ SessionMoney: () => null }));
vi.mock("./GamifyView", () => ({ Ladder: ({ moduleType }: { moduleType: string }) => <p>ladder of {moduleType}</p> }));
vi.mock("../ui/Leaderboard", () => ({ Leaderboard: () => null }));
vi.mock("../ui/GameResults", () => ({ GameResults: () => null }));

afterEach(cleanup);

describe("ladders", () => {
  // A fresh empty list on every render read as "changed" and looped until React gave up (#301).
  it("opens on the first set play's ladder without a render loop", () => {
    render(<GamesView initialSessionId={null} initialGameModule={null} refreshTrigger={0} />);
    fireEvent.click(screen.getByRole("button", { name: "ladders" }));
    expect(screen.getByText("ladder of emporia:blotto:v2")).toBeTruthy();
    fireEvent.click(screen.getByText("lowest unique"));
    expect(screen.getByText("ladder of emporia:lowest-unique:v2")).toBeTruthy();
  });
});

const opened = (id: string) => screen.queryByText(new RegExp(`^…${id.slice(-10)}`));

describe("games view, deep-linked session", () => {
  it("opens the linked session once and lets a click on another one stick across polls", async () => {
    const view = (trigger: number) => <GamesView initialSessionId={FIRST} initialGameModule={null} refreshTrigger={trigger} />;
    const { rerender } = render(view(0));
    expect(opened(FIRST)).toBeTruthy();                                       // the linked session is open

    fireEvent.click(screen.getByText(new RegExp(`…${SECOND.slice(-8)}`)));
    expect(opened(SECOND)).toBeTruthy();

    for (const trigger of [1, 2, 3]) {                                        // three polls: new lists each time
      polls.n = trigger;
      await act(async () => { rerender(view(trigger)); });
      expect(opened(SECOND)).toBeTruthy();                                    // still the one clicked
      expect(opened(FIRST)).toBeNull();
    }
  });
});

describe("a live set play", () => {
  it("shows the phase, the revealed move, and the one still hidden — not a chessboard", () => {
    render(<GamesView initialSessionId={SECOND} initialGameModule={null} refreshTrigger={0} />);
    const now = document.querySelector(".e-phase-strip__step.is-now");
    expect(now?.textContent).toContain("reveal");
    const rows = [...document.querySelectorAll(".e-blotto__grid tbody tr")].map((r) => r.textContent ?? "");
    expect(rows[0]).toContain("alpha");
    expect(rows[0]).toContain("30");
    expect(rows[1]).toContain("▒▒");
    expect(screen.getByRole("status").textContent).toContain("waiting on beta");
    expect(document.querySelector("[class*='Chessboard']")).toBeNull();
  });

  it("puts set plays under Games and jobs under Deals; chess only on its own chip", () => {
    expect(sessionMatches({ module_type: "emporia:two-thirds:v2" }, "games", "")).toBe(true);
    expect(sessionMatches({ module_type: "emporia:chess:v1" }, "games", "")).toBe(false);
    expect(sessionMatches({ module_type: "emporia:chess:v1" }, "games", "emporia:chess:v1")).toBe(true);
    expect(sessionMatches({ module_type: "emporia:service:v1" }, "games", "")).toBe(false);
    expect(sessionMatches({ module_type: "emporia:service:v1" }, "deals", "")).toBe(true);
    expect(sessionMatches({ module_type: "emporia:blotto:v2" }, "deals", "")).toBe(false);
  });
});
