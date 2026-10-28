// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
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
    encrypted: false, host: "home.example", advertised: true, listing_only: true },
  { room_id: "r_quiet", name: "Quiet room", description: "", room_type: "public", gate_type: "open", entry_fee_cents: 0,
    creator_id: "alice", members: ["alice"], encrypted: false, host: "home.example", advertised: false },
];
const topics = [
  { topic_id: "t1", slug: "talk", name: "Talk", description: "", visibility: "public", gate_type: "open",
    entry_fee_cents: 0, creator_id: "alice", created_at: "2026-10-01T00:00:00Z", post_count: 1, subscriber_count: 2,
    flair_options: [], host: "home.example", advertised: true },
  { topic_id: "t2", slug: "quiet", name: "Quiet topic", description: "", visibility: "public", gate_type: "open",
    entry_fee_cents: 0, creator_id: "alice", created_at: "2026-10-01T00:00:00Z", post_count: 0, subscriber_count: 0,
    flair_options: [], host: "home.example", advertised: false },
];
const roomsElsewhere = [{
  room_id: "x1", name: "Remote club", description: "tips", room_type: "private", gate_type: "paid",
  entry_fee_cents: 900, currency: "USD", access_duration_days: 30, max_members: null, encrypted: false,
  relay: "https://peer.example", host: "peer.example", owner_address: "seller@peer.example",
  owner_fingerprint: "3f9a·c21e", owner_stable_id: "abc", join_url: "https://peer.example/rooms/x1/join", verified: true,
}];
const topicsElsewhere = [{
  slug: "far", name: "Far topic", description: "", gate_type: "open", entry_fee_cents: 0, relay: "https://peer.example",
  host: "peer.example", owner_address: "seller@peer.example", owner_fingerprint: "3f9a·c21e", owner_stable_id: "abc",
  join_url: "https://peer.example/agoras/topics/far", verified: true,
}];

const calls: string[] = [];
vi.mock("../api", () => ({
  api: {
    rooms: vi.fn(() => Promise.resolve({ rooms, count: rooms.length })),
    roomsElsewhere: vi.fn(() => Promise.resolve({ rooms: roomsElsewhere, count: 1 })),
    roomMessages: vi.fn((id: string) => { calls.push(`messages:${id}`); return Promise.resolve({ messages: [] }); }),
    roomChain: vi.fn(() => new Promise(() => {})),
    agentProfile: vi.fn(() => Promise.resolve({})),
    agoraTopics: vi.fn(() => Promise.resolve({ topics, count: topics.length })),
    agoraTopicsElsewhere: vi.fn(() => Promise.resolve({ topics: topicsElsewhere, count: 1 })),
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
    expect(await screen.findByText("Open room")).toBeTruthy();
    // two of the three rooms are advertised (the selected one repeats its badge in the toolbar)
    expect(screen.getAllByText("advertised").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("on home.example").length).toBeGreaterThanOrEqual(3);
  });

  it("lists rooms from other relays with their host, owner, fee, and says joining is there", async () => {
    render(<RoomsView initialRoomId={null} />);
    expect(await screen.findByText("Remote club")).toBeTruthy();
    expect(screen.getByText("On other relays (1)")).toBeTruthy();
    expect(screen.getByText("on peer.example")).toBeTruthy();
    expect(screen.getByText("seller@peer.example · 3f9a·c21e")).toBeTruthy();
    expect(screen.getByText("$9.00")).toBeTruthy();
    expect(screen.getByText(/30 days of access per payment\. Joining happens on peer\.example, not here\./)).toBeTruthy();
    // the page offers no way to join or pay from here
    const section = within(screen.getByLabelText("rooms on other relays"));
    expect(section.queryAllByRole("link")).toHaveLength(0);
    expect(section.queryAllByRole("button")).toHaveLength(0);
  });

  it("an advertised paid room the viewer is not in shows its terms and asks for no messages", async () => {
    render(<RoomsView initialRoomId="r_paid" />);
    expect(await screen.findByText(/Paid room · \$7\.00 for 30 days · 4 members/)).toBeTruthy();
    expect(screen.getByText(/Joining and paying happen on home\.example/)).toBeTruthy();
    expect(calls.filter((c) => c.endsWith(":r_paid"))).toEqual([]);
  });
});

describe("P249 agoras view", () => {
  it("shows each topic's host and which are advertised, and the topics on other relays", async () => {
    render(<AgorasView />);
    expect(await screen.findByText("Talk")).toBeTruthy();
    expect(screen.getAllByText("advertised")).toHaveLength(1);
    expect(screen.getAllByText("on home.example")).toHaveLength(2);
    expect(await screen.findByText("Far topic")).toBeTruthy();
    expect(screen.getByText("On other relays (1)")).toBeTruthy();
    expect(screen.getByText("on peer.example")).toBeTruthy();
    expect(screen.getByText("Joining happens on peer.example, not here.")).toBeTruthy();
  });
});
