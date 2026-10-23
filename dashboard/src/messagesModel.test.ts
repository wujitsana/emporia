import { describe, expect, it } from "vitest";
import type { InboxEvent } from "./api";
import { splitInbox, unreadActivity } from "./messagesModel";

const ev = (id: string, type: string, payload: Record<string, unknown>, is_read = false) =>
  ({ inbox_id: id, event_type: type, payload, is_read, created_at: "2026-10-23T20:00:00Z" }) as InboxEvent;

describe("splitInbox (P121)", () => {
  it("files DMs under their conversation and lists everything else as activity", () => {
    const { activity, unreadByThread } = splitInbox([
      ev("i1", "dm_received", { thread_id: "dm_a" }),
      ev("i2", "dm_received", { thread_id: "dm_a" }),
      ev("i3", "dm_received", { thread_id: "dm_b" }, true),
      ev("i4", "challenge", { from_agent: "beta" }),
      ev("i5", "room_invite", { room_id: "r1" }, true),
    ]);
    expect(unreadByThread).toEqual({ dm_a: ["i1", "i2"] });
    expect(activity.map((e) => e.inbox_id)).toEqual(["i4", "i5"]);
    expect(unreadActivity(activity)).toBe(1);
  });
  it("keeps a DM event without a thread visible as activity rather than losing it", () => {
    const { activity } = splitInbox([ev("i1", "dm_received", {})]);
    expect(activity).toHaveLength(1);
  });
});

describe("inBox (P124)", () => {
  it("lists a conversation by who spoke last, unread always in the inbox", async () => {
    const { inBox } = await import("./messagesModel");
    const theirs = { last_sender: "beta" }, mine = { last_sender: "alpha" };
    expect(inBox("inbox", theirs, "alpha", 0)).toBe(true);
    expect(inBox("sent", theirs, "alpha", 0)).toBe(false);
    expect(inBox("sent", mine, "alpha", 0)).toBe(true);
    expect(inBox("inbox", mine, "alpha", 0)).toBe(false);
    expect(inBox("inbox", mine, "alpha", 2)).toBe(true);
    expect(inBox("all", mine, "alpha", 0)).toBe(true);
  });
});
