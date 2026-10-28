import { describe, expect, it } from "vitest";

import { feeLabel, roomRow, topicRow } from "./advertFormat";
import type { RoomElsewhere, TopicElsewhere } from "./api";

const room: RoomElsewhere = {
  room_id: "room_1", name: "Remote club", description: "tips", room_type: "private", gate_type: "paid",
  entry_fee_cents: 900, currency: "USD", access_duration_days: 30, max_members: null, encrypted: false,
  relay: "https://peer.example", host: "peer.example", owner_address: "seller@peer.example",
  owner_fingerprint: "3f9a·c21e", owner_stable_id: "abc", join_url: "https://peer.example/rooms/room_1/join",
  verified: true,
};
const topic: TopicElsewhere = {
  slug: "talk", name: "Talk", description: "", gate_type: "open", entry_fee_cents: 0, relay: "https://peer.example",
  host: "peer.example", owner_address: "seller@peer.example", owner_fingerprint: "3f9a·c21e", owner_stable_id: "abc",
  join_url: "https://peer.example/agoras/topics/talk", verified: true,
};

describe("P249 adverts from other relays", () => {
  it("writes a fee in words", () => {
    expect(feeLabel(0)).toBe("free");
    expect(feeLabel(undefined)).toBe("free");
    expect(feeLabel(500, "USD")).toBe("$5.00");
    expect(feeLabel(500, "eur")).toBe("$5.00 EUR");
  });

  it("a room row says where it is joined, who offers it and what it costs", () => {
    const r = roomRow(room);
    expect(r).toMatchObject({ name: "Remote club", host: "peer.example", fee: "$9.00", owner: "seller@peer.example",
      fingerprint: "3f9a·c21e" });
    expect(r.note).toContain("30 days of access per payment");
    expect(r.note).toContain("Joining happens on peer.example, not here.");
  });

  it("a topic row is free unless it is paid", () => {
    expect(topicRow(topic).fee).toBe("free");
    expect(topicRow({ ...topic, gate_type: "paid_invite", entry_fee_cents: 250 }).fee).toBe("$2.50");
    expect(topicRow(topic).note).toBe("Joining happens on peer.example, not here.");
  });

  it("falls back to the relay address when the host is missing", () => {
    expect(roomRow({ ...room, host: "" }).host).toBe("peer.example");
  });
});
