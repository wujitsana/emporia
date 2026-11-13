// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RoomsView } from "./RoomsView";

// A closed room with messages sits under its own "Closed" group, never among the open rooms.
// `closed` and `message_count` are optional: an older relay leaves them out.

const base = { description: "", room_type: "public", gate_type: "open", entry_fee_cents: 0, creator_id: "alice", members: ["alice"], encrypted: false };
let rooms: Record<string, unknown>[] = [];
vi.mock("../api", () => ({
  api: {
    rooms: vi.fn(() => Promise.resolve({ rooms, count: rooms.length })),
    roomMessages: vi.fn(() => Promise.resolve({ messages: [] })),
    roomChain: vi.fn(() => new Promise(() => {})),
    agentProfile: vi.fn(() => Promise.resolve({})),
  },
  dmDisplayContent: (c: string) => c,
  looksLikeEncryptedEnvelope: () => false,
}));
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "" }));
vi.mock("../healthCtx", () => ({ useRelayHealth: () => null }));
vi.mock("../hooks", async (orig) => ({
  ...(await orig<typeof import("../hooks")>()),
  useRoomWs: vi.fn(() => ({ messages: [], connected: false })),
}));

afterEach(cleanup);

describe("rooms list: closed rooms", () => {
  it("groups a closed room apart, at the end, with a chip", async () => {
    rooms = [
      { ...base, room_id: "r_old", name: "Old talk", closed: true, message_count: 4 },
      { ...base, room_id: "r_live", name: "Live talk", closed: false, message_count: 2 },
    ];
    render(<RoomsView initialRoomId={null} />);
    await screen.findAllByText("Live talk");
    const group = screen.getByText("Closed");
    const live = screen.getAllByText("Live talk")[0];
    const old = screen.getAllByText("Old talk")[0];
    expect(live.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(group.compareDocumentPosition(old) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByText("closed").length).toBeGreaterThan(0);
  });

  it("with only closed rooms the empty hint shows and the group lists them", async () => {
    rooms = [{ ...base, room_id: "r_old", name: "Old talk", closed: true, message_count: 1 }];
    render(<RoomsView initialRoomId={null} />);
    expect(await screen.findByText(/No public rooms yet/)).toBeTruthy();
    expect(screen.getByText("Closed")).toBeTruthy();
    expect(screen.getAllByText("Old talk").length).toBeGreaterThan(0);
  });

  it("an older relay (no closed field) shows no Closed group", async () => {
    rooms = [{ ...base, room_id: "r_a", name: "Plain room" }];
    render(<RoomsView initialRoomId={null} />);
    await screen.findAllByText("Plain room");
    expect(screen.queryByText("Closed")).toBeNull();
  });
});
