// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Session } from "../api";
import { GamesView } from "./GamesView";

// A deep link opens its session once. After that a click on another session sticks: each poll
// hands the view new lists, and re-applying the link on every poll snapped back to it.

const session = (id: string): Session => ({
  session_id: id, module_type: "emporia:blotto:v1", status: "active", participants: ["alpha", "beta"],
  current_agent: "alpha", step_number: 1, created_at: "2026-11-03T12:00:00Z",
});
const FIRST = "sess_first_aaaaaaaaaaaa1111";
const SECOND = "sess_second_bbbbbbbbbbb2222";

// Each "poll" is a new list object with the same sessions, as the real useInterval returns.
const polls = { n: 0, cache: new Map<string, unknown>() };
vi.mock("../hooks", () => ({
  useInterval: (_fn: unknown, ms: number) => {
    const key = `${ms}:${polls.n}`;
    if (!polls.cache.has(key)) {
      polls.cache.set(key, { sessions: ms === 6_000 ? [session(FIRST), session(SECOND)] : [] });
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

afterEach(cleanup);

describe("games view, deep-linked session", () => {
  it("opens the linked session once and lets a click on another one stick across polls", async () => {
    const view = (trigger: number) => <GamesView initialSessionId={FIRST} initialGameModule={null} listingPeek={null} refreshTrigger={trigger} />;
    const { rerender } = render(view(0));
    expect(await screen.findByText(`…${FIRST.slice(-16)}`)).toBeTruthy();       // the linked session is open

    fireEvent.click(screen.getByText(new RegExp(`…${SECOND.slice(-8)}`)));
    expect(await screen.findByText(`…${SECOND.slice(-16)}`)).toBeTruthy();

    for (const trigger of [1, 2, 3]) {                                        // three polls: new lists each time
      polls.n = trigger;
      await act(async () => { rerender(view(trigger)); });
      expect(screen.getByText(`…${SECOND.slice(-16)}`)).toBeTruthy();         // still the one clicked
      expect(screen.queryByText(`…${FIRST.slice(-16)}`)).toBeNull();
    }
  });
});
