// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ListingsView } from "./ListingsView";

// What is on offer comes from the routes that hold it: rooms from /rooms, games waiting from the lobby, and
// bounties from the bounty board (its own route). A private room is not an offer.

const rooms = [
  { room_id: "r_club", name: "Club room", description: "", room_type: "public", gate_type: "open",
    entry_fee_cents: 0, creator_id: "alpha", members: ["alpha", "beta"], encrypted: false },
  { room_id: "r_paid", name: "Paid studio", description: "", room_type: "private", gate_type: "paid",
    entry_fee_cents: 500, creator_id: "alpha", member_count: 3, encrypted: false },
  { room_id: "r_secret", name: "Secret room", description: "", room_type: "private", gate_type: "invite",
    entry_fee_cents: 0, creator_id: "alpha", members: ["alpha"], encrypted: false },
];

vi.mock("../api", () => ({
  api: {
    rooms: vi.fn(() => Promise.resolve({ rooms, count: rooms.length })),
    openSessions: vi.fn(() => Promise.resolve({ open_sessions: [] })),
  },
}));
vi.mock("../ui/BountyBoard", () => ({ BountyBoard: () => <div data-testid="bounty-board">bounty board</div> }));

afterEach(cleanup);

describe("offers view", () => {
  it("shows the bounty board and the rooms anyone may open, free or paid, never a private one", async () => {
    render(<ListingsView refreshTrigger={0} navigate={vi.fn()} />);
    expect(await screen.findByText("Club room")).toBeTruthy();
    expect(screen.getByTestId("bounty-board")).toBeTruthy();
    expect(screen.getByText("Paid studio")).toBeTruthy();
    expect(screen.getByText("$5.00")).toBeTruthy();
    expect(screen.queryByText("Secret room")).toBeNull();
  });
});
