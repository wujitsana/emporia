import { describe, expect, it } from "vitest";
import { sealedItems } from "./sealedSession";

const ENV = JSON.stringify({ v: 1, alg: "x25519-hkdf-aesgcm", epk: "00", n: "AA", ct: "AA", keys: { a: {} } });

describe("sealed session fields (P149)", () => {
  it("finds only the sealed fields, one item per field", () => {
    const items = sealedItems([
      { id: "act1", payload: { deliverable: ENV, note: "plain" } },
      { id: "act2", payload: { move: "e4" } },
      { id: "act3", payload: null },
    ]);
    expect(items.map((i) => [i.id, i.field])).toEqual([["act1:deliverable", "deliverable"]]);
  });
});
