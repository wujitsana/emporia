import { describe, expect, it } from "vitest";
import fixture from "./fixtures/loreBook.json";
import {
  apocryphaOf, arcLabel, arcVoiceLine, authorsByPost, cardBadges, citeTarget, isOff, latestArc, noCanonReason, pickLine, picksText, tieLine,
  type LoreBook,
} from "./loreBook";

const book = fixture.book as unknown as LoreBook;

describe("lore book model", () => {
  it("picks the latest arc and labels it", () => {
    expect(latestArc(book.arcs)).toBe(0);
    expect(latestArc([])).toBeNull();
    expect(arcLabel(book.arcs[0])).toBe("Arc 1 · 2 of 3 chapters in the canon");
  });
  it("makes three badges from the card's plain text, the voice already labeled", () => {
    const b = cardBadges(book.chapters[0].card);
    expect(b.map((x) => x.key)).toEqual(["word", "limit", "voice"]);
    expect(b[2].text).toBe("This relay's voice: a recipe");
    expect(b[2].label).toBe("");
    expect(b[0].text).toBe("knife");
    expect(b[1].text).toBe("280 characters at most");
  });
  it("says the voice an arc was told in, and the voice it changed from", () => {
    expect(arcVoiceLine(book.arcs[0])).toBe("Told in: a recipe");
    expect(arcVoiceLine({ ...book.arcs[0], voice_changed_from: "a telegram" })).toBe("Told in: a recipe (changed from: a telegram)");
    expect(arcVoiceLine({ ...book.arcs[0], voice: null })).toBe("");
    expect(arcVoiceLine(undefined)).toBe("");
  });
  it("words the tie and the lack of a canon", () => {
    expect(tieLine(null)).toBe("");
    expect(tieLine(["a", "b"])).toBe("chosen by the beacon among 2");
    expect(tieLine({ among: ["a", "b", "c"] })).toBe("chosen by the beacon among 3");
    expect(tieLine(true)).toMatch(/beacon/);
    expect(noCanonReason("none_won")).toMatch(/no passage got/);
    expect(noCanonReason("quorum")).toMatch(/too few/);
    expect(noCanonReason("no_candidates")).toMatch(/nobody wrote a passage/);
  });
  it("splits the apocrypha into counted and set aside", () => {
    const ch = { ...book.chapters[2], apocrypha: [
      { ...book.chapters[2].apocrypha[0], set_aside: ["no_commas"], post_id: "x" },
      ...book.chapters[2].apocrypha,
    ] };
    const { counted, setAside } = apocryphaOf(ch);
    expect(setAside).toHaveLength(1);
    expect(counted[0].picks).toBeGreaterThanOrEqual(counted[counted.length - 1].picks);
  });
  it("names who a pick chose", () => {
    const ch = book.chapters[0];
    const authors = authorsByPost(ch);
    expect(pickLine(ch.picks[0], authors)).toBe("fx_q2 picked the passage by fx_q1");
    expect(pickLine({ agent: "z", pick: "none", reason: "" }, authors)).toBe("z picked none");
    expect(picksText(1)).toBe("1 pick");
    expect(picksText(2)).toBe("2 picks");
  });
  it("routes cites: pages where the dashboard has one, plain text where not", () => {
    expect(citeTarget("post:pst_1")).toEqual({ kind: "post", id: "pst_1" });
    expect(citeTarget("session:s1")).toEqual({ kind: "session", id: "s1" });
    expect(citeTarget("room:r1")).toEqual({ kind: "room", id: "r1" });
    expect(citeTarget("topic:ideas")).toEqual({ kind: "topic", id: "ideas" });
    expect(citeTarget("agent:abcdef")).toEqual({ kind: "plain" });
    expect(citeTarget("post:<script>")).toEqual({ kind: "plain" });
    expect(citeTarget("gh_pr:acme/emporia#12")).toEqual({ kind: "github", href: "https://github.com/acme/emporia/pull/12", text: "PR #12" });
    expect(citeTarget("nonsense")).toEqual({ kind: "plain" });
  });
  it("reads a 404 as the module being off", () => {
    expect(isOff("Error: 404 /lore/book")).toBe(true);
    expect(isOff("Error: 500 /lore/book")).toBe(false);
    expect(isOff(null)).toBe(false);
  });
});
