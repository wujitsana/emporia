// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WaitingGames } from "./WaitingGames";

// Games waiting for a player come from the lobby; a job waiting for a buyer is a Deal, not a game.

const open = [
  { session_id: "s_chess", module_type: "emporia:chess:v1", creator: "alpha", players: 1, max_players: 2,
    created_at: "2026-11-05T12:00:00Z", payment_rules: { mode: "free" } },
  { session_id: "s_job", module_type: "emporia:service:v1", creator: "beta", players: 1, max_players: 2,
    created_at: "2026-11-05T12:00:00Z", payment_rules: { mode: "paid", price: 4 } },
];

vi.mock("../api", () => ({
  api: { openSessions: vi.fn(() => Promise.resolve({ open_sessions: open })) },
}));

afterEach(cleanup);

describe("games waiting for a player", () => {
  it("lists games, not jobs, and opens one", async () => {
    const onOpen = vi.fn();
    render(<WaitingGames onOpen={onOpen} />);
    expect(await screen.findByText(/Games waiting for a player · 1/)).toBeTruthy();
    expect(screen.getByText("chess")).toBeTruthy();
    expect(screen.queryByText("service")).toBeNull();
    fireEvent.click(screen.getByText("chess"));
    expect(onOpen).toHaveBeenCalledWith("s_chess");
  });
});
