import { describe, expect, it } from "vitest";
import { eventParticipantCount } from "./eventFormat";
import type { EmporiaEvent } from "./api";

// P40-3: EventsView read `ev.participant_count` in two places — a field the
// relay has never sent. `GET /events` returns a `participants` ARRAY (and
// `participants_json`); there is no `participant_count` anywhere in
// relay_server.py. So the optional-chain fell through to 0 on every event, in
// both the card and the detail pane, and an event with entrants reported none.
// The seeded demo events all happen to have zero participants, which is why it
// looked right.

const ev = (extra: Partial<EmporiaEvent>): EmporiaEvent => ({
  event_id: "evt_1",
  title: "Ladder",
  module_type: "emporia:chess:v1",
  organizer_id: "org",
  status: "open",
  entry_fee_usd: "0",
  created_at: "2026-01-01T00:00:00Z",
  ...extra,
});

describe("eventParticipantCount", () => {
  it("counts the participants the relay actually sends", () => {
    expect(eventParticipantCount(ev({ participants: ["a", "b", "c"] }))).toBe(3);
  });

  it("reports zero for an event nobody has entered", () => {
    expect(eventParticipantCount(ev({ participants: [] }))).toBe(0);
    expect(eventParticipantCount(ev({}))).toBe(0);
  });

  it("still honours an explicit count if a relay ever sends one", () => {
    // Federated peers may not be on the same version; prefer the array, but
    // don't ignore a count that is genuinely present.
    expect(eventParticipantCount(ev({ participant_count: 7 }))).toBe(7);
    expect(eventParticipantCount(ev({ participants: ["a"], participant_count: 7 }))).toBe(1);
  });
})
