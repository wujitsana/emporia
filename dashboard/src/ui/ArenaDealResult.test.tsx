// @vitest-environment jsdom
import { cleanup, render, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ArenaFrame } from "./ArenaFrames";

// 11-10 review: a Bargain both sides accepted read "Winner: alpha" in the game panel, while Results said "deal".
afterEach(cleanup);

describe("a finished Bargain's result line", () => {
  const base = { kind: "bargain", offers: [], ended: { how: "accepted", by: "beta", offer: 1 }, hands: {} } as const;

  it("names a deal and each side's points, not a winner", () => {
    const frame = { ...base, result: { winner: "alpha", termination: "deal", points: { alpha: 8, beta: 7 } } };
    const { container } = render(<ArenaFrame frame={frame as never} book={undefined} />);
    const verdict = within(container).getByRole("status");
    expect(verdict.textContent).toContain("Deal: alpha 8 · beta 7 points");
    expect(verdict.textContent).not.toContain("Winner");
  });

  it("still names the winner when there was no deal", () => {
    const frame = { ...base, ended: { how: "declined", by: "beta", offer: 4 }, result: { winner: "alpha", termination: "walk_away" } };
    const { container } = render(<ArenaFrame frame={frame as never} book={undefined} />);
    expect(within(container).getByRole("status").textContent).toContain("Winner: alpha");
  });
});
