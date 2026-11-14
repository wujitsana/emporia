// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { InboxEvent } from "../api";
import { NeedsAgent } from "./NeedsAgent";

const events: InboxEvent[] = [
  { inbox_id: "1", event_type: "challenge", payload: { from_agent: "beta", session_id: "s1" }, is_read: false, created_at: "2026-11-13T10:00:00Z" },
  { inbox_id: "2", event_type: "dm_received", payload: { thread_id: "t" }, is_read: false, created_at: "2026-11-13T10:01:00Z" },
  { inbox_id: "3", event_type: "room_invite", payload: { room_id: "r1", room_name: "lab" }, is_read: true, created_at: "2026-11-13T10:02:00Z" },
];
type Page = { events: InboxEvent[]; unread_total?: number; unread_dms?: number };
let current: Page = { events };
vi.mock("../hooks", () => ({ useInterval: () => [current, false, null] }));
afterEach(cleanup);

describe("NeedsAgent", () => {
  it("lists unread non-DM events, honestly labelled, and opens the game", () => {
    const navigate = vi.fn();
    render(<NeedsAgent viewer="alpha" navigate={navigate} />);
    expect(screen.getByText("2 for alpha to read")).toBeTruthy();   // one number: all unread, messages too
    fireEvent.click(screen.getByText("challenge"));
    expect(navigate).toHaveBeenCalledWith("session", { sessionId: "s1" });
    expect(screen.queryByText(/invited to/)).toBeNull();
  });
  it("draws nothing when nothing waits", () => {
    current = { events: [] };
    const { container } = render(<NeedsAgent viewer="alpha" navigate={vi.fn()} />);
    expect(container.textContent).toBe("");
    current = { events };
  });
  it("counts the whole inbox from the relay's total, not the capped page", () => {
    current = { events, unread_total: 301, unread_dms: 1 };
    render(<NeedsAgent viewer="alpha" navigate={vi.fn()} />);
    expect(screen.getByText("301 for alpha to read")).toBeTruthy();
    current = { events };
  });
  it("counts the unread events on the page on a relay without totals", () => {
    current = { events };
    render(<NeedsAgent viewer="alpha" navigate={vi.fn()} />);
    expect(screen.getByText("2 for alpha to read")).toBeTruthy();
    current = { events };
  });
});
