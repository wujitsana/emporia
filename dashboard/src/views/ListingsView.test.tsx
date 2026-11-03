// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ListingsView } from "./ListingsView";

// A bounty is a listing too, but a prize offered, not a price asked: it belongs on the bounty
// board and must not repeat in the Services table as a "$5.00" service.

const listings = [
  { listing_id: "l_service", listing_type: "session", title: "Logo design", description: "", module_type: "emporia:service:v1",
    payment_mode: "x402", price_usd: "12.00", agent_id: "alpha", expires_at: "2099-01-01T00:00:00Z" },
  { listing_id: "l_bounty", listing_type: "bounty", title: "Find the bug bounty", description: "", module_type: "emporia:service:v1",
    payment_mode: "x402", price_usd: "5.00", agent_id: "beta", expires_at: "2099-01-01T00:00:00Z" },
  { listing_id: "l_room", listing_type: "room", title: "Club room", description: "", payment_mode: "free",
    price_usd: "0", agent_id: "alpha", room_type: "public", gate_type: "open", member_count: 2 },
];

vi.mock("../api", () => ({
  api: {
    listings: vi.fn(() => Promise.resolve({ listings, count: listings.length })),
    openSessions: vi.fn(() => Promise.resolve({ open_sessions: [] })),
  },
}));
vi.mock("../ui/BountyBoard", () => ({ BountyBoard: () => <div data-testid="bounty-board">bounty board</div> }));

afterEach(cleanup);

describe("listings view", () => {
  it("lists services and rooms, and leaves bounties to the bounty board", async () => {
    render(<ListingsView refreshTrigger={0} navigate={vi.fn()} />);
    expect(await screen.findByText("Logo design")).toBeTruthy();
    expect(screen.getByTestId("bounty-board")).toBeTruthy();
    expect(screen.getByText("Club room")).toBeTruthy();
    expect(screen.queryByText("Find the bug bounty")).toBeNull();
    expect(screen.queryByText("$5.00")).toBeNull();
    expect(screen.getByText(/1 service · 1 room/)).toBeTruthy();
    const services = within(screen.getByText("Services").closest("section") ?? document.body);
    expect(services.getByText("Logo design")).toBeTruthy();
  });
});
