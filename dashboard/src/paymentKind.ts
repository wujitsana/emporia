import type { Settlement } from "./api";

/** What a settlement paid for: the relay's outcome type, else what its id names (a room's entry is
 * filed under the room's id, a topic's under the topic's), so a room payment is never shown as a game. */
export function paidFor(s: Settlement): string {
  const t = s.outcome_type ?? "";
  if (t) return t;
  if (s.session_id.startsWith("room_")) return "room_entry";
  if (s.session_id.startsWith("tpc_")) return "agora_subscribe";
  return "";
}

export function outcomeLabel(s: Settlement): { label: string; cls: string } {
  const t = paidFor(s);
  if (t === "refund" || s.status === "refunded") return { label: "refund", cls: "e-outcome-pill--refund" };
  if (t === "room_entry") return { label: "room", cls: "e-outcome-pill--room" };
  if (t === "agora_subscribe") return { label: "agora", cls: "e-outcome-pill--agora" };
  return { label: "game", cls: "e-outcome-pill--game" };
}
