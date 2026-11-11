// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgorasView } from "./AgorasView";
import { RoomsView } from "./RoomsView";

// P249: the dashboard shows where each room and topic is hosted, whether it is
// advertised to other relays, and what linked relays advertise. It only reads.

const rooms = [
  { room_id: "r_open", name: "Open room", description: "", room_type: "public", gate_type: "open", entry_fee_cents: 0,
    creator_id: "alice", members: ["alice"], encrypted: false, host: "home.example", advertised: true },
  { room_id: "r_paid", name: "Paid club", description: "weekly notes", room_type: "private", gate_type: "paid",
    entry_fee_cents: 700, currency: "USD", access_duration_days: 30, creator_id: "bob", member_count: 4,
    encrypted: false, host: "home.example", advertised: true, card_only: true },
  { room_id: "r_quiet", name: "Quiet room", description: "", room_type: "public", gate_type: "open", entry_fee_cents: 0,
    creator_id: "alice", members: ["alice"], encrypted: false, host: "home.example", advertised: false },
];
const topics = [
  { topic_id: "t1", slug: "talk", name: "Talk", description: "", visibility: "public", gate_type: "open",
    entry_fee_cents: 0, creator_id: "alice", created_at: "2026-10-01T00:00:00Z", post_count: 1, subscriber_count: 2,
    flair_options: [], host: "home.example", encrypted: true },
  { topic_id: "t2", slug: "quiet", name: "Quiet topic", description: "", visibility: "public", gate_type: "open",
    entry_fee_cents: 0, creator_id: "alice", created_at: "2026-10-01T00:00:00Z", post_count: 0, subscriber_count: 0,
    flair_options: [], host: "home.example" },
];
const calls: string[] = [];
vi.mock("../api", () => ({
  api: {
    rooms: vi.fn(() => Promise.resolve({ rooms, count: rooms.length })),
    roomMessages: vi.fn((id: string) => { calls.push(`messages:${id}`); return Promise.resolve({ messages: [] }); }),
    roomChain: vi.fn(() => new Promise(() => {})),
    agentProfile: vi.fn(() => Promise.resolve({})),
    agoraTopics: vi.fn(() => Promise.resolve({ topics, count: topics.length })),
    agoraPosts: vi.fn(() => Promise.resolve({ posts: [], count: 0 })),
    agoraFeed: vi.fn(() => Promise.resolve({ posts: [] })),
    agoraPost: vi.fn(),
  },
  dmDisplayContent: (c: string) => c,
  looksLikeEncryptedEnvelope: () => false,
}));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "" }));
vi.mock("../healthCtx", () => ({ useRelayHealth: () => null }));
vi.mock("../hooks", async (orig) => ({
  ...(await orig<typeof import("../hooks")>()),
  useRoomWs: vi.fn((_relay: string, roomId: string | null) => {
    if (roomId) calls.push(`ws:${roomId}`);
    return { messages: [], connected: false };
  }),
}));

afterEach(() => { cleanup(); calls.length = 0; });

describe("P249 rooms view", () => {
  it("shows each room's host and which are advertised", async () => {
    render(<RoomsView initialRoomId={null} />);
    // in the rail, and again in "Rooms you can join" (a public room is on offer)
    expect((await screen.findAllByText("Open room")).length).toBeGreaterThan(0);
    // two of the three rooms are advertised (the selected one repeats its badge in the toolbar)
    expect(screen.getAllByText("advertised").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("on home.example").length).toBeGreaterThanOrEqual(3);
  });

  it("an advertised paid room the viewer is not in shows its terms and asks for no messages", async () => {
    render(<RoomsView initialRoomId="r_paid" />);
    expect(await screen.findByText(/Paid room · \$7\.00 for 30 days · 4 members/)).toBeTruthy();
    expect(screen.getByText(/Joining and paying happen on home\.example/)).toBeTruthy();
    expect(calls.filter((c) => c.endsWith(":r_paid"))).toEqual([]);
  });
});

describe("agoras view", () => {
  it("shows each topic's host and which are encrypted, and no adverts", async () => {
    render(<AgorasView />);
    expect(await screen.findByText("Talk")).toBeTruthy();
    expect(screen.getAllByText("sealed")).toHaveLength(1);
    expect(screen.getAllByText("on home.example")).toHaveLength(2);
    expect(screen.queryByText(/On other relays/)).toBeNull();
  });
});
