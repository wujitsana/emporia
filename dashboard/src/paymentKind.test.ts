import { describe, expect, it } from "vitest";
import { outcomeLabel, paidFor } from "./paymentKind";
import type { Settlement } from "./api";

const s = (session_id: string, outcome_type?: string) => ({ session_id, outcome_type } as unknown as Settlement);

describe("what a payment was for", () => {
  it("names a room's entry by its id when the relay leaves the type out, and never calls it a game", () => {
    expect(paidFor(s("room_fead03c9722f"))).toBe("room_entry");
    expect(outcomeLabel(s("room_fead03c9722f")).label).toBe("room");
    expect(paidFor(s("tpc_35411fa9b79e"))).toBe("agora_subscribe");
    expect(outcomeLabel(s("sess_e6035bcbbfd0")).label).toBe("game");
    expect(paidFor(s("room_x", "refund"))).toBe("refund");
  });
});
