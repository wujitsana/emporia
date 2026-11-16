import { describe, expect, it } from "vitest";
import type { Passport, Presence } from "./api";
import { passportLines, presenceHint, presenceLabel } from "./presence";

const visitor = (status: "confirmed" | "unconfirmed"): Presence => ({
  standing: "visitor", declared: true, home: { kind: "relay", relay_id: "rl_a", url: "https://a.example" },
  check: { status, reason: status === "confirmed" ? "listed by a.example" : "it did not answer", at: "" },
  label: status === "confirmed" ? "visiting from a.example" : "visiting from a.example (unconfirmed)" });

describe("presence", () => {
  it("says nothing of a member and names a visitor and a nomad", () => {
    expect(presenceLabel(undefined)).toBe("");
    expect(presenceLabel({ standing: "member", declared: true, home: { kind: "here", relay_id: "", url: "" } })).toBe("");
    expect(presenceLabel(visitor("confirmed"))).toBe("visiting from a.example");
    expect(presenceLabel(visitor("unconfirmed"))).toContain("unconfirmed");
    expect(presenceLabel({ standing: "nomad", declared: true, home: { kind: "none", relay_id: "", url: "" }, label: "nomad" })).toBe("nomad");
    expect(presenceHint(visitor("unconfirmed"))).toContain("not confirmed: it did not answer");
    expect(presenceHint(visitor("confirmed"))).toContain("lists it as its own");
  });
  it("lists each relay's statement and an unweighted reach", () => {
    const c = { kind: "emporia-passport", identity: { agent_id: "x", address: "x@r", fingerprint: "f" },
      statements: [
        { relay: "r", says: "r says", counts_here: true, checked: "", record: { games: { finished: 1 } } },
        { relay: "a", says: "a says", counts_here: false, checked: "", record: { games: { finished: 4 }, as_seller: { sales: 2 } } },
        { relay: "b", says: "b says", counts_here: false, checked: "", record: {} }],
      reach: { relays: 2, listed: ["r", "a"], weighted: false, text: "records on 2 relays" }, counts_here: false, note: "" } as Passport;
    expect(passportLines(c)).toEqual(["r says: 1 game finished", "a says: 4 games finished, 2 deals (evidence only)",
      "b says: no record yet (evidence only)", "Reach: records on 2 relays (unweighted)"]);
  });
});
