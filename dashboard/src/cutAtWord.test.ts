import { describe, expect, it } from "vitest";
import { cutAtWord } from "./cutAtWord";

describe("cutAtWord", () => {
  it("leaves what fits and cuts the rest at a word", () => {
    expect(cutAtWord("short", 10)).toBe("short");
    expect(cutAtWord("the quick brown fox", 12)).toBe("the quick…");
    expect(cutAtWord("the quick brown fox", 16)).toBe("the quick brown…");
  });
  it("cuts a long word with no space at the limit", () => {
    expect(cutAtWord("a".repeat(30), 10)).toBe(`${"a".repeat(9)}…`);
  });
  it("never returns more than max characters, ellipsis included", () => {
    for (const text of ["the quick brown fox jumps", "a".repeat(50), "ab cd ef gh ij"]) {
      for (let max = 1; max < 20; max++) expect(cutAtWord(text, max).length).toBeLessThanOrEqual(max);
    }
  });
});
