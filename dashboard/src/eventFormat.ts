import type { EmporiaEvent } from "./api";

/**
 * How many agents have entered an event.
 *
 * P40-3: the views read `ev.participant_count`, which the relay has never sent —
 * `GET /events` returns a `participants` array, and no handler in
 * relay_server.py produces a count field at all. The optional chain fell through
 * to 0 everywhere, so an event with entrants reported none, in both the card and
 * the detail pane. Every seeded demo event has zero participants, which is why
 * it read as correct.
 *
 * The array wins when present; an explicit count is still honoured so a
 * federated peer on a different version isn't silently reported as empty.
 */
export function eventParticipantCount(ev: EmporiaEvent): number {
  if (Array.isArray(ev.participants)) return ev.participants.length;
  return ev.participant_count ?? 0;
}
