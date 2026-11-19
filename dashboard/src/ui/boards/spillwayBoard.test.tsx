// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { buildTimeline } from "../../arena";
import { SPILLWAY, SPILLWAY_LEVEL, SPILLWAY_PRE, SPILLWAY_THREES } from "../../arenaFixtures";
import { spillwayView } from "../../spillway";
import { ArenaFrame } from "../ArenaFrames";
import { SpillwayBoard } from "./SpillwayBoard";
import { spillwayFromFrame, spillwayFromView } from "./spillwayModel";

afterEach(cleanup);

const view = (fx: typeof SPILLWAY) => spillwayView(fx.session, fx.actions)!;
const tables = (c: HTMLElement) => [...c.querySelectorAll("table")].map((t) => [...t.querySelectorAll("tbody tr")].map((r) => [...r.children].map((x) => x.textContent)));
const frameOf = (fx: typeof SPILLWAY, i: number) => buildTimeline(fx.session, fx.actions).frameAt(i);

describe("SpillwayBoard", () => {
  it("draws the cells as a table: words, never color alone", () => {
    const { container } = render(<SpillwayBoard {...spillwayFromFrame(frameOf(SPILLWAY, 0) as never, (s) => s)} />);
    const [grid] = tables(container);
    expect(grid[4]).toEqual(["1", "silt", "alpha", "beta", "alpha", "beta", "silt"]);
    expect(grid[0][1]).toBe("empty");
    expect(container.querySelector("table caption")?.textContent).toContain("6 channels by 5 rows");
  });

  it("marks the winning four in the table and in the text", () => {
    const { container } = render(<SpillwayBoard {...spillwayFromView(view(SPILLWAY))} />);
    const [grid] = tables(container);
    expect(grid[4][4]).toBe("alpha (in the winning line)");
    expect(grid[1][4]).toBe("alpha (in the winning line)");
    expect(container.textContent).toContain("Four in a line: channel 4 row 1, channel 4 row 2, channel 4 row 3, channel 4 row 4.");
    expect(container.textContent).toContain("alpha wins with four in a line.");
  });

  it("an earlier step: the gate of that drop, no line, no verdict", () => {
    const props = spillwayFromFrame(frameOf(SPILLWAY, 4) as never, (s) => s);
    const { container } = render(<SpillwayBoard {...props} />);
    expect(container.textContent).toContain("Channel 4 was shut for the last drop.");
    expect(container.textContent).toContain("Last drop: beta, channel 1, row 2 (move 4).");
    expect(container.textContent).not.toContain("Four in a line");
    expect(container.querySelector("[class*=gateWord]")?.textContent).toBe("shut");
    expect(tables(container)[1]).toHaveLength(4);
  });

  it("the opening as dealt, before any drop", () => {
    const { container } = render(<SpillwayBoard {...spillwayFromFrame(frameOf(SPILLWAY, 0) as never, (s) => s)} />);
    expect(container.textContent).toContain("No drop yet");
    expect(container.querySelector("[class*=gateWord]")).toBeNull();
  });

  it("the cap: live threes and the tiebreak in words", () => {
    const a = render(<SpillwayBoard {...spillwayFromView(view(SPILLWAY_THREES))} />);
    expect(a.container.textContent).toContain("Live threes at the cap: alpha 2, beta 1.");
    expect(a.container.textContent).toContain("alpha wins on live threes (alpha 2, beta 1).");
    cleanup();
    const b = render(<SpillwayBoard {...spillwayFromView(view(SPILLWAY_LEVEL))} />);
    expect(b.container.textContent).toContain("Level on live threes (1 each): beta moved second and wins.");
  });

  it("before the deal: only the round that deals the opening", () => {
    const { container } = render(<SpillwayBoard {...spillwayFromView(view(SPILLWAY_PRE))} />);
    expect(container.textContent).toContain("The opening is dealt when drand round 4242 is published.");
    expect(container.querySelector("table")).toBeNull();
  });

  it("visibility only: no button, no link, no form control", () => {
    const { container } = render(<SpillwayBoard {...spillwayFromView(view(SPILLWAY))} />);
    expect(container.querySelector("button, a, input, select, textarea")).toBeNull();
  });

  it("the drawing is hidden from a screen reader; the tables carry the facts", () => {
    const { container } = render(<SpillwayBoard {...spillwayFromView(view(SPILLWAY))} />);
    expect(container.querySelector("[class*=grid]")?.getAttribute("aria-hidden")).toBe("true");
  });
});

describe("Spillway in the Arena frame", () => {
  it("renders the board for a step and the verdict at the end", () => {
    const t = buildTimeline(SPILLWAY.session, SPILLWAY.actions);
    const mid = render(<ArenaFrame frame={t.frameAt(2)} book={undefined} />);
    expect(mid.container.querySelector('[data-kind="spillway"]')).not.toBeNull();
    expect(mid.container.textContent).not.toContain("wins with");
    cleanup();
    const end = render(<ArenaFrame frame={t.frameAt(5)} book={undefined} />);
    expect(end.container.textContent).toContain("alpha wins with four in a line");
  });
});
