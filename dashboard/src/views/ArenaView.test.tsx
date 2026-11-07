// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ArenaView } from "./ArenaView";
import { AUCTION, BARGAIN, BLOTTO, DICE, TWO_THIRDS } from "../arenaFixtures";
import type { Session, SessionAction } from "../api";

const FIXTURES = [BLOTTO, TWO_THIRDS, DICE, AUCTION, BARGAIN];
const byId = (id: string) => FIXTURES.find((f) => f.session.session_id === id)!;

vi.mock("../hooks", () => ({ useInterval: () => [null, false, null] }));
vi.mock("../api", () => ({
  api: {
    session: vi.fn((id: string) => Promise.resolve(byId(id).session as Session)),
    sessionActions: vi.fn((id: string) => Promise.resolve({ session_id: id, actions: byId(id).actions as SessionAction[] })),
    sessionModels: vi.fn((id: string) => Promise.resolve({
      session_id: id, ended: true,
      players: { alpha: { status: "revealed", model: "model-a", committed_steps: 1, revealed_steps: 1 }, beta: { status: "mismatch", model: null, committed_steps: 1, revealed_steps: 0 } },
    })),
    agents: vi.fn(() => Promise.resolve({ agents: [], count: 0 })),
  },
  dashboardToken: () => "",
  looksLikeEncryptedEnvelope: () => false,
}));
vi.mock("../transport", async (orig) => ({ ...(await orig<object>()), relaySocket: vi.fn(() => new Promise(() => {})) }));
vi.mock("../ui/AuditBadge", () => ({ AuditBadge: () => null }));
vi.mock("../ui/VerifyBadge", () => ({ VerifyBadge: () => <span>verified-badge</span> }));
vi.mock("../ui/GameChat", () => ({ GameChat: () => <p>table talk here</p> }));
vi.mock("../ui/Leaderboard", () => ({ Leaderboard: () => null }));
vi.mock("../ui/ModelResults", () => ({ ModelResults: () => null }));
vi.mock("./QuestArcs", () => ({ OpenChallenges: () => <p>OPEN CHALLENGES</p> }));

const view = (id: string | null, step: number | null = null) =>
  <ArenaView initialSessionId={id} initialStep={step} refreshTrigger={0} />;
const stepText = () => document.querySelector(".e-chess-replay-status [role=status]")?.textContent ?? "";

beforeEach(() => { vi.useRealTimers(); });
afterEach(cleanup);

describe("the Arena home", () => {
  it("names its sections", () => {
    render(view(null));
    for (const t of ["Live now", "Recent", "OPEN CHALLENGES", "Leaderboards", "Results by model"]) {
      expect(screen.getByText(new RegExp(`^${t}`))).toBeTruthy();
    }
  });
});

describe("a match opened by its address", () => {
  it("opens at the step the link names, with the table talk beside the moves", async () => {
    render(view(BLOTTO.session.session_id, 2));
    await waitFor(() => expect(stepText()).toContain("step 2 of 4"));
    expect(screen.getByText("table talk here")).toBeTruthy();
    expect(screen.getByText(/moves · click to seek/)).toBeTruthy();
    // step 2: both committed, nothing revealed — sealed, and no allocation drawn
    expect(document.querySelectorAll(".e-arena-slot.is-sealed")).toHaveLength(2);
    expect(document.querySelector(".e-arena-frame")?.textContent).not.toMatch(/30/);
  });

  it("follows live (the last step) with no step in the link, and clamps one past the end", async () => {
    const { unmount } = render(view(BLOTTO.session.session_id));
    await waitFor(() => expect(stepText()).toContain("step 4 of 4"));
    unmount();
    render(view(BLOTTO.session.session_id, 99));
    await waitFor(() => expect(stepText()).toContain("step 4 of 4"));
  });

  it("reports the step to the address as the person moves, and nothing for the live end", async () => {
    const shown = vi.fn();
    render(<ArenaView initialSessionId={BLOTTO.session.session_id} initialStep={null} refreshTrigger={0} onSessionShown={shown} />);
    await waitFor(() => expect(stepText()).toContain("step 4 of 4"));
    fireEvent.click(screen.getByRole("button", { name: "Previous move" }));
    await waitFor(() => expect(stepText()).toContain("step 3 of 4"));
    expect(shown).toHaveBeenLastCalledWith(BLOTTO.session.session_id, 3);
    fireEvent.change(screen.getByLabelText("Step"), { target: { value: "1" } });
    expect(shown).toHaveBeenLastCalledWith(BLOTTO.session.session_id, 1);
    fireEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
    expect(shown).toHaveBeenLastCalledWith(BLOTTO.session.session_id, null);
  });

  it("keeps a sealed move hidden until its reveal step, and shows it then", async () => {
    render(view(BLOTTO.session.session_id, 2));
    await waitFor(() => expect(stepText()).toContain("step 2 of 4"));
    fireEvent.click(screen.getByRole("button", { name: "Next move" }));
    await waitFor(() => expect(stepText()).toContain("step 3 of 4"));
    const frame = document.querySelector(".e-arena-frame")!.textContent!;
    expect(frame).toContain("30 · 30 · 30 · 5 · 5");        // alpha revealed
    expect(frame).toContain("sealed");                       // beta still sealed
    expect(frame).not.toContain("35");
  });

  it("plays through at a fixed speed and stops at the end", async () => {
    render(view(TWO_THIRDS.session.session_id, 0));
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
    render(view(fx.session.session_id, step));
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
