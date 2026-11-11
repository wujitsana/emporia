// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ArenaView } from "./ArenaView";
import { AUCTION, BARGAIN, BLOTTO, DICE, TWO_THIRDS } from "../arenaFixtures";
import type { Session, SessionAction } from "../api";
import type { ArenaLens } from "../navConfig";

const FIXTURES = [BLOTTO, TWO_THIRDS, DICE, AUCTION, BARGAIN];
const byId = (id: string) => FIXTURES.find((f) => f.session.session_id === id)!;

vi.mock("../hooks", () => ({
  useInterval: (fn: () => unknown) => {
    const src = String(fn);
    if (src.includes("agents")) return [{ agents: [{ agent_id: "alpha", address: "alpha@relay.example", about: "keeps its word; plays blotto" }], count: 1 }, false, null];
    if (src.includes("gameChat")) return [{ chat_enabled: true, messages: [
      { message_id: "m1", agent_id: "alpha", content: "I value spice most", created_at: "2026-11-05T12:00:00Z", step: 2 },
      { message_id: "m2", agent_id: "beta", content: "a line said on its own", created_at: "1999-01-01T00:00:00Z", step: null }] }, false, null];
    if (src.includes("gameThread")) return [{ ref: "session:x", posts: [
      { post_id: "d1", author_id: "alpha", text: "Game: x (blotto) · Objective: win two fields · One move I would change: reserve more",
        tags: [["t", "debrief"]], reply_to: "session:x", at: "2099-01-01T00:00:00Z" },
      { post_id: "d3", author_id: "mallory", text: "Game: x · Objective: not a player's account",
        tags: [["t", "debrief"]], reply_to: "session:x", at: "2099-01-01T00:00:00Z" },
      { post_id: "d2", author_id: "beta", text: "an ordinary reply", tags: [] }] }, false, null];
    if (src.includes("results")) return [{ results: [{ session_id: "r1", module_type: "emporia:chess:v1", participants: ["alpha", "beta"], winner: "alpha", outcome_type: "win", termination: "CHECKMATE", ended_at: "2026-11-05T13:00:00Z" }] }, false, null];
    return [null, false, null];
  },
}));
vi.mock("../api", () => ({
  api: {
    session: vi.fn((id: string) => (byId(id) ? Promise.resolve(byId(id).session as Session) : Promise.reject(new Error("404")))),
    sessionActions: vi.fn((id: string) => Promise.resolve({ session_id: id, actions: byId(id).actions as SessionAction[] })),
    sessionModels: vi.fn((id: string) => Promise.resolve({
      session_id: id, ended: true,
      players: { alpha: { status: "revealed", model: "model-a", committed_steps: 1, revealed_steps: 1 }, beta: { status: "mismatch", model: null, committed_steps: 1, revealed_steps: 0 } },
    })),
    agents: vi.fn(() => Promise.resolve({ agents: [{ agent_id: "alpha", address: "alpha@relay.example" }], count: 1 })),
  },
  dashboardToken: () => "",
  looksLikeEncryptedEnvelope: () => false,
}));
vi.mock("../transport", async (orig) => ({ ...(await orig<object>()), relaySocket: vi.fn(() => new Promise(() => {})) }));
vi.mock("../ui/AuditBadge", () => ({ AuditBadge: () => null }));
vi.mock("../ui/VerifyBadge", () => ({ VerifyBadge: () => <span>verified-badge</span> }));
vi.mock("../ui/Leaderboard", () => ({ Leaderboard: () => null }));
vi.mock("../ui/ModelResults", () => ({ ModelResults: () => null }));
vi.mock("./QuestArcs", () => ({ OpenChallenges: () => <p>OPEN CHALLENGES</p> }));

const view = (id: string | null, lens: ArenaLens = "live", step: number | null = null) =>
  <ArenaView initialSessionId={id} initialStep={step} lens={lens} refreshTrigger={0} />;
const stepText = () => document.querySelector(".e-chess-replay-status [role=status]")?.textContent ?? "";

beforeEach(() => { vi.useRealTimers(); });
afterEach(cleanup);

describe("the Arena home", () => {
  it("names its sections", () => {
    // One page per lens of the sub-row: Live, Waiting, Results, Boards.
    const pages: [ArenaLens, string[]][] = [
      ["live", ["Live now"]],
      ["waiting", ["Games waiting for a player", "OPEN CHALLENGES"]],
      ["results", ["Results"]],
      ["boards", ["Leaderboards", "Results by model"]],
    ];
    for (const [lens, titles] of pages) {
      render(view(null, lens));
      for (const t of titles) expect(screen.getByText(new RegExp(`^${t}`))).toBeTruthy();
      cleanup();
    }
  });
});

describe("leaving a match by a sub-tab", () => {
  const results = (token: number, lens: ArenaLens = "results") =>
    <ArenaView initialSessionId={null} initialStep={null} lens={lens} refreshTrigger={0} leaveToken={token} />;

  it("closes a match the person opened from a list (the route never named it) when a sub-tab is picked, even on the page it is on", () => {
    const { rerender } = render(results(0));
    fireEvent.click(screen.getByText(/won by checkmate/));
    expect(document.querySelector(".e-arena")?.getAttribute("data-tab")).toBe("match");
    // the sub-tab for the very page it is on (same lens, same route): only the token tells the Arena
    rerender(results(1));
    expect(document.querySelector(".e-arena")?.getAttribute("data-tab")).toBe("home");
    expect(screen.queryByRole("button", { name: "‹ Arena" })).toBeNull();
  });

  it("stays in the match when nothing was clicked: a re-render with the same token keeps it", () => {
    const { rerender } = render(results(2));
    fireEvent.click(screen.getByText(/won by checkmate/));
    rerender(results(2));
    expect(document.querySelector(".e-arena")?.getAttribute("data-tab")).toBe("match");
  });

  it("names the token with each report, so a report made before the click can be told from one after it", async () => {
    const shown = vi.fn();
    const { rerender } = render(<ArenaView initialSessionId={BLOTTO.session.session_id} initialStep={2} lens="live" refreshTrigger={0} leaveToken={3} onSessionShown={shown} />);
    await waitFor(() => expect(stepText()).toContain("step 2 of 4"));
    expect(shown).toHaveBeenCalledWith(BLOTTO.session.session_id, 2, 3);
    rerender(<ArenaView initialSessionId={null} initialStep={null} lens="waiting" refreshTrigger={0} leaveToken={4} onSessionShown={shown} />);
    expect(shown).toHaveBeenLastCalledWith(null, null, 4);
  });
});

describe("names, not raw ids", () => {
  it("names the winner of a recent match by its address", () => {
    render(view(null, "results"));
    expect(screen.getByText(/alpha@relay\.example won by checkmate/)).toBeTruthy();
  });
  it("has no separate table talk box: what was said sits with its move", async () => {
    render(view(BLOTTO.session.session_id, "live", 2));
    await waitFor(() => expect(stepText()).toContain("step 2 of 4"));
    expect(screen.queryByText(/^Table talk/)).toBeNull();
    // under the move in the list, and on the step line, quoted
    expect(document.querySelector(".e-arena-steps")?.textContent).toContain("“I value spice most”");
    expect(document.querySelector(".e-arena-stepline")?.textContent).toContain("“I value spice most”");
    // a line with no step is placed by its time: before the first move here
    expect(document.querySelector(".e-arena-steps li:first-child")?.textContent).toContain("“a line said on its own”");
  });
  it("lists the debriefs under the result, as the template's lines, and only the tagged replies", async () => {
    render(view(BLOTTO.session.session_id));
    await waitFor(() => expect(stepText()).toContain("step 4 of 4"));
    const after = screen.getByLabelText("After the game");
    expect(after.textContent).toContain("Objective: win two fields");
    expect(after.textContent).toContain("One move I would change: reserve more");
    expect(after.textContent).not.toContain("an ordinary reply");
    expect(after.textContent).not.toContain("not a player's account");
  });
});

describe("a match header shows the players' bios", () => {
  it("names each player's own words, as text", async () => {
    render(view(BLOTTO.session.session_id));
    await waitFor(() => expect(screen.getByTestId("arena-abouts")).toBeTruthy());
    expect(screen.getByTestId("arena-abouts").textContent).toContain("alpha@relay.example");
    expect(screen.getByTestId("arena-abouts").textContent).toContain("“keeps its word; plays blotto”");
    expect(screen.getByTestId("arena-abouts").querySelector("a")).toBeNull();
  });
});

describe("a match opened by its address", () => {
  it("opens at the step the link names, with the moves beside it", async () => {
    render(view(BLOTTO.session.session_id, "live", 2));
    await waitFor(() => expect(stepText()).toContain("step 2 of 4"));
    expect(screen.getByText(/moves · click to seek/)).toBeTruthy();
    // step 2: both committed, nothing revealed — sealed, and no allocation drawn
    expect(document.querySelectorAll(".e-arena-slot.is-sealed")).toHaveLength(2);
    expect(document.querySelector(".e-arena-frame")?.textContent).not.toMatch(/30/);
  });

  it("has a '‹ Arena' breadcrumb that returns to the page it came from", async () => {
    render(view(BLOTTO.session.session_id, "waiting", 2));
    await waitFor(() => expect(stepText()).toContain("step 2 of 4"));
    fireEvent.click(screen.getByRole("button", { name: "‹ Arena" }));
    expect(screen.getByText(/^Games waiting for a player/)).toBeTruthy();
  });

  it("follows live (the last step) with no step in the link, and clamps one past the end", async () => {
    const { unmount } = render(view(BLOTTO.session.session_id));
    await waitFor(() => expect(stepText()).toContain("step 4 of 4"));
    unmount();
    render(view(BLOTTO.session.session_id, "live", 99));
    await waitFor(() => expect(stepText()).toContain("step 4 of 4"));
  });

  it("reports the step to the address as the person moves, and nothing for the live end", async () => {
    const shown = vi.fn();
    render(<ArenaView initialSessionId={BLOTTO.session.session_id} initialStep={null} refreshTrigger={0} onSessionShown={shown} />);
    await waitFor(() => expect(stepText()).toContain("step 4 of 4"));
    fireEvent.click(screen.getByRole("button", { name: "Previous move" }));
    await waitFor(() => expect(stepText()).toContain("step 3 of 4"));
    expect(shown).toHaveBeenLastCalledWith(BLOTTO.session.session_id, 3, 0);
    fireEvent.change(screen.getByLabelText("Step"), { target: { value: "1" } });
    expect(shown).toHaveBeenLastCalledWith(BLOTTO.session.session_id, 1, 0);
    fireEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
    expect(shown).toHaveBeenLastCalledWith(BLOTTO.session.session_id, null, 0);
  });

  it("keeps a sealed move hidden until its reveal step, and shows it then", async () => {
    render(view(BLOTTO.session.session_id, "live", 2));
    await waitFor(() => expect(stepText()).toContain("step 2 of 4"));
    fireEvent.click(screen.getByRole("button", { name: "Next move" }));
    await waitFor(() => expect(stepText()).toContain("step 3 of 4"));
    const frame = document.querySelector(".e-arena-frame")!.textContent!;
    expect(frame).toContain("30 · 30 · 30 · 5 · 5");        // alpha revealed
    expect(frame).toContain("sealed");                       // beta still sealed
    expect(frame).not.toContain("35");
  });

  it("plays through at a fixed speed and stops at the end", async () => {
    render(view(TWO_THIRDS.session.session_id, "live", 0));
    await waitFor(() => expect(stepText()).toContain("step 0 of 4"));
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "Play replay" }));
    await act(async () => { vi.advanceTimersByTime(1200); });
    expect(stepText()).toContain("step 1 of 4");
    await act(async () => { vi.advanceTimersByTime(1200 * 5); });
    expect(stepText()).toContain("step 4 of 4");
    expect(screen.getByRole("button", { name: "Play replay" })).toBeTruthy();   // stopped
  });

  it("shows the model reveal and the verify badge at the end", async () => {
    render(view(BLOTTO.session.session_id));
    await waitFor(() => expect(screen.getByText(/Model reveal/)).toBeTruthy());
    expect(screen.getByText("model-a · revealed")).toBeTruthy();
    expect(screen.getByText("model mismatch")).toBeTruthy();
    expect(screen.getByText("verified-badge")).toBeTruthy();
  });

  it.each([
    [DICE, 3, "called liar"],
    [AUCTION, 3, "52"],
    [BARGAIN, 2, "silk"],
  ] as const)("draws %s at a middle step", async (fx, step, needle) => {
    render(view(fx.session.session_id, "live", step));
    await waitFor(() => expect(stepText()).toContain(`step ${step} of`));
    expect(document.querySelector(".e-arena-frame")?.textContent).toContain(needle);
  });

  it("says so when the match cannot be opened", async () => {
    const { api } = await import("../api");
    (api.session as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("403"));
    render(view("sess_private_zzzzzzzz"));
    await waitFor(() => expect(screen.getByText(/could not be opened/)).toBeTruthy());
  });
});
