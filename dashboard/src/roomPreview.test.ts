import { describe, expect, it } from "vitest";

import { roomMessagePreview } from "./roomPreview";

const body = (o: Record<string, unknown>) => JSON.stringify({ kind: "note", label: "", quote_of: null, reply_to: null, tags: [], ...o });

describe("roomMessagePreview", () => {
  const sealed = JSON.stringify({ v: 2, alg: "x25519", keys: { a: "b" }, ct: "AAAA" });
  const cases: [string, string, { removed?: boolean } | undefined, string][] = [
    ["note with text", body({ text: "Scout: first note in the lab room." }), undefined, "Scout: first note in the lab room."],
    ["reply", body({ text: "agreed", reply_to: "m_1" }), undefined, "agreed"],
    ["quote", body({ text: "as said", quote_of: "m_2" }), undefined, "as said"],
    ["multi-line text is one line, trimmed", body({ text: "  one\n two\t three  " }), undefined, "one two three"],
    ["non-text kind with text", body({ kind: "idea", text: "rank cards" }), undefined, "[idea] rank cards"],
    ["non-text kind without text", body({ kind: "tool", text: "" }), undefined, "[tool]"],
    ["other kind uses its label", body({ kind: "other", label: "poll", text: "" }), undefined, "[poll]"],
    ["legacy plain text", "  just plain\ntext ", undefined, "just plain text"],
    ["malformed JSON", '{"kind":"note","text":', undefined, "message"],
    ["cut-short body keeps its readable words", '{"kind":"note","label":"","text":"hello there","tags":[', undefined, "hello there"],
    ["JSON without text or kind", '{"a":1}', undefined, "message"],
    ["removed", body({ text: "gone" }), { removed: true }, "removed"],
    ["encrypted", sealed, undefined, "sealed"],
    ["empty", "", undefined, ""],
  ];
  it.each(cases)("%s", (_n, content, opts, want) => {
    expect(roomMessagePreview(content, opts)).toBe(want);
  });
  it("never returns JSON", () => {
    for (const [, c, o] of cases) expect(roomMessagePreview(c, o)).not.toMatch(/^\s*\{/);
  });
});
