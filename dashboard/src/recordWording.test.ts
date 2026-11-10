import { describe, expect, it } from "vitest";
import { seasonName } from "./homeModel";
import { gamesLine } from "./settings";

describe("one wording for the games record", () => {
  it("shows drawn and abandoned only when there are any, as different things", () => {
    const g = { finished: 2, won: 0, lost: 1, drawn: 1, lost_on_time: 0, abandoned: 0 };
    expect(gamesLine(g)).toBe("2 finished · 0 won · 1 lost · 1 drawn");
    expect(gamesLine({ ...g, drawn: 0, abandoned: 1, lost_on_time: 1 })).toBe("2 finished · 0 won · 1 lost (1 on time) · 1 abandoned");
    expect(gamesLine({ ...g, drawn: 1, abandoned: 2 })).toBe("2 finished · 0 won · 1 lost · 1 drawn · 2 abandoned");
  });
});

describe("season names", () => {
  it("turns the relay's id into a name and leaves other ids alone", () => {
    expect(seasonName("season-0")).toBe("Season 0");
    expect(seasonName("autumn")).toBe("autumn");
    expect(seasonName(undefined)).toBe("");
  });
});
