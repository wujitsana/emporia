import { describe, expect, it } from "vitest";
import { cutAtWord } from "./cutAtWord";

describe("cutAtWord", () => {
  it("leaves what fits and cuts the rest at a word", () => {
    expect(cutAtWord("short", 10)).toBe("short");
    expect(cutAtWord("the quick brown fox", 12)).toBe("the quick…");
    expect(cutAtWord("the quick brown fox", 15)).toBe("the quick brown…");
  });
  it("cuts a long word with no space at the limit", () => {
    expect(cutAtWord("a".repeat(30), 10)).toBe(`${"a".repeat(10)}…`);
  });
});
