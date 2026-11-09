// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JoinableRooms } from "./JoinableRooms";
import type { Room } from "../api";

// Rooms you can join: the rooms anyone may open, free or paid. A private room is not an offer.

const rooms = [
  { room_id: "r_club", name: "Club room", description: "", room_type: "public", gate_type: "open",
    entry_fee_cents: 0, creator_id: "alpha", members: ["alpha", "beta"], encrypted: false },
  { room_id: "r_paid", name: "Paid studio", description: "", room_type: "private", gate_type: "paid",
    entry_fee_cents: 500, creator_id: "alpha", member_count: 3, encrypted: false },
  { room_id: "r_secret", name: "Secret room", description: "", room_type: "private", gate_type: "invite",
    entry_fee_cents: 0, creator_id: "alpha", members: ["alpha"], encrypted: false },
] as unknown as Room[];

afterEach(cleanup);

describe("rooms you can join", () => {
  it("lists the rooms anyone may open, free or paid, never a private one", () => {
    render(<JoinableRooms rooms={rooms} onOpen={vi.fn()} />);
    expect(screen.getByText(/Rooms you can join · 2/)).toBeTruthy();
    expect(screen.getByText("Club room")).toBeTruthy();
    expect(screen.getByText("Paid studio")).toBeTruthy();
    expect(screen.getByText("$5.00")).toBeTruthy();
    expect(screen.queryByText("Secret room")).toBeNull();
  });

  it("opens the picked room in Rooms", () => {
    const open = vi.fn();
    render(<JoinableRooms rooms={rooms} onOpen={open} />);
    fireEvent.click(screen.getByText("Paid studio"));
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ room_id: "r_paid" }));
  });

  it("draws nothing when no room is on offer", () => {
    const { container } = render(<JoinableRooms rooms={[rooms[2]]} onOpen={vi.fn()} />);
    expect(container.textContent).toBe("");
  });
});
