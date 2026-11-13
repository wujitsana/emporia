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
let current: InboxEvent[] = events;
vi.mock("../hooks", () => ({ useInterval: () => [current, false, null] }));
afterEach(cleanup);

describe("NeedsAgent", () => {
  it("lists unread non-DM events, honestly labelled, and opens the game", () => {
    const navigate = vi.fn();
    render(<NeedsAgent viewer="alpha" navigate={navigate} />);
    expect(screen.getByText("1 unread by the agent")).toBeTruthy();
    fireEvent.click(screen.getByText("challenge"));
    expect(navigate).toHaveBeenCalledWith("session", { sessionId: "s1" });
    expect(screen.queryByText(/invited to/)).toBeNull();
  });
  it("draws nothing when nothing waits", () => {
    current = [];
    const { container } = render(<NeedsAgent viewer="alpha" navigate={vi.fn()} />);
    expect(container.textContent).toBe("");
    current = events;
  });
});
