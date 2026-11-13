// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { buildTimeline } from "../../arena";
import {
  BLOTTO, BLOTTO_DEALT, LOWEST_UNIQUE, LOWEST_UNIQUE_TIE, TWO_THIRDS, TWO_THIRDS_TIE,
} from "../../arenaFixtures";
import { setPlayView } from "../../setPlay";
import { ArenaFrame } from "../ArenaFrames";
import { BlottoBars } from "./BlottoBars";
import { PickLine } from "./PickLine";
import { UniqueRow } from "./UniqueRow";
import { SetPlayBoard } from "./SetPlayBoard";
import { modelFromView } from "./setPlayModel";

afterEach(cleanup);

const tableRows = (c: HTMLElement) =>
  [...c.querySelectorAll("table tbody tr")].map((r) => [...r.children].map((x) => x.textContent));

describe("BlottoBars", () => {
  const seats = (a: Partial<Parameters<typeof BlottoBars>[0]["seats"][0]>, b: Partial<Parameters<typeof BlottoBars>[0]["seats"][0]>) => [
    { id: "alpha", name: "alpha", state: "waiting" as const, ...a }, { id: "beta", name: "beta", state: "waiting" as const, ...b },
  ];

  it("sealed: hatched placeholders, the word sealed, and a value handed to a sealed seat never reaches the DOM", () => {
    const { container } = render(
      <BlottoBars fields={3} troops={90} seats={seats({ state: "sealed", alloc: [71, 72, 73] }, { state: "sealed", alloc: [74, 75, 76] })} />);
    expect(container.textContent).toContain("sealed");
    for (const n of ["71", "72", "73", "74", "75", "76"]) expect(container.textContent).not.toContain(n);
    expect(tableRows(container)[0]).toEqual(["Field 1", "sealed", "sealed", "not yet known"]);
  });

  it("partly revealed: one side's bars, the other's placeholder, no field taken yet", () => {
    const { container } = render(
      <BlottoBars fields={3} troops={90} seats={seats({ state: "revealed", alloc: [50, 30, 10] }, { state: "sealed" })} />);
    expect(tableRows(container)).toEqual([
      ["Field 1", "50", "sealed", "not yet known"], ["Field 2", "30", "sealed", "not yet known"], ["Field 3", "10", "sealed", "not yet known"],
    ]);
    expect(container.textContent).not.toContain("◆");
  });

  it("finished and dealt: weights, who took each field, points and a tied field", () => {
    const { session, actions } = BLOTTO_DEALT;
    const v = setPlayView(session, actions);
    const { container } = render(<SetPlayBoard model={modelFromView(v, () => null)} />);
    expect(tableRows(container)).toEqual([
      ["Field 1", "3", "20", "10", "alpha"], ["Field 2", "1", "10", "10", "tie"],
      ["Field 3", "5", "5", "20", "beta"], ["Field 4", "2", "5", "0", "alpha"],
    ]);
    expect(container.textContent).toContain("alpha 5 · beta 5 points this round");
    expect(container.textContent).toContain("★ alpha");
    expect(container.querySelectorAll("table").length).toBe(1);
  });
});

describe("PickLine", () => {
  it("sealed: a count of hidden picks, no target", () => {
    const { container } = render(<PickLine min={0} max={100} picks={[]} hidden={2} average={null} target={null} />);
    expect(container.textContent).toContain("2 picks still hidden");
    expect(tableRows(container).at(-1)).toEqual(["target", "not known yet"]);
  });

  it("partly revealed: only the revealed pick has a value", () => {
    const { container } = render(<PickLine min={0} max={100} picks={[{ id: "a", name: "alpha", value: 37 }]} hidden={1} average={null} target={null} />);
    expect(container.querySelector('[role="img"]')?.getAttribute("aria-label")).toContain("alpha 37");
    expect(container.textContent).toContain("1 pick still hidden");
  });

  it("finished: the label states picks, average and target; the winner is starred", () => {
    const { session, actions } = TWO_THIRDS;
    const { container } = render(<SetPlayBoard model={modelFromView(setPlayView(session, actions), () => null)} />);
    const label = container.querySelector('[role="img"]')!.getAttribute("aria-label")!;
    expect(label).toContain("alpha 20");
    expect(label).toContain("beta 60");
    expect(label).toContain("average 40.0");
    expect(label).toContain("target 26.7");
    expect(container.textContent).toContain("★ alpha 20");
  });

  it("a tie: both pins, no star, and it says draw", () => {
    const { session, actions } = TWO_THIRDS_TIE;
    const { container } = render(<SetPlayBoard model={modelFromView(setPlayView(session, actions), () => null)} />);
    expect(container.textContent).not.toContain("★");
    expect(container.textContent).toContain("a draw");
    expect(tableRows(container).slice(0, 2)).toEqual([["alpha", "40"], ["beta", "40"]]);
  });
});

describe("UniqueRow", () => {
  it("sealed: hidden count, no cell has a picker", () => {
    const { container } = render(<UniqueRow min={1} max={5} picks={[]} hidden={2} />);
    expect(container.textContent).toContain("2 picks still hidden");
    expect(container.textContent).not.toContain("once");
  });

  it("partly revealed: the one revealed pick is listed", () => {
    const { container } = render(<UniqueRow min={1} max={5} picks={[{ id: "a", name: "alpha", value: 4 }]} hidden={1} />);
    expect(tableRows(container)[0]).toEqual(["4", "alpha", "once"]);
  });

  it("finished: the lowest unique number wins, the other is once", () => {
    const { session, actions } = LOWEST_UNIQUE;
    const { container } = render(<SetPlayBoard model={modelFromView(setPlayView(session, actions), () => null)} />);
    expect(tableRows(container)).toEqual([["2", "alpha (winner)", "once"], ["5", "beta", "once"]]);
    expect(container.textContent).toContain("★ alpha");
  });

  it("a tie: a number picked twice is counted twice, nobody wins, a draw is said", () => {
    const { session, actions } = LOWEST_UNIQUE_TIE;
    const { container } = render(<SetPlayBoard model={modelFromView(setPlayView(session, actions), () => null)} />);
    expect(tableRows(container)).toEqual([["3", "alpha, beta", "2"]]);
    expect(container.textContent).toContain("a draw");
    expect(container.textContent).toContain("×2");
    expect(container.textContent).not.toContain("★");
  });
});

describe("the Arena step-through draws the same boards", () => {
  const frameAt = (fx: { session: Parameters<typeof buildTimeline>[0]; actions: Parameters<typeof buildTimeline>[1] }, i: number) => {
    const tl = buildTimeline(fx.session, fx.actions);
    return render(<ArenaFrame frame={tl.frameAt(i)} book={undefined} />).container;
  };

  it("blotto: sealed until each reveal step, and the sealed move is never in the DOM", () => {
    expect(frameAt(BLOTTO, 0).textContent).not.toContain("◆");
    const sealed = frameAt(BLOTTO, 2);   // both committed
    expect(sealed.textContent).toContain("sealed");
    for (const n of ["30", "35"]) expect(sealed.querySelector("table")?.textContent).not.toContain(n);
    const half = frameAt(BLOTTO, 3);     // alpha revealed
    expect(tableRows(half)[0]).toEqual(["Field 1", "30", "sealed", "not yet known"]);
    const end = frameAt(BLOTTO, 4);
    expect(tableRows(end)[3]).toEqual(["Field 4", "5", "35", "beta"]);
  });

  it("two-thirds: the picks and the target appear only when revealed", () => {
    const sealed = frameAt(TWO_THIRDS, 2);
    expect(sealed.querySelector('[role="img"]')!.getAttribute("aria-label")).toContain("none revealed");
    expect(sealed.querySelector('[role="img"]')!.getAttribute("aria-label")).not.toContain("target");
    const end = frameAt(TWO_THIRDS, 4);
    expect(end.querySelector('[role="img"]')!.getAttribute("aria-label")).toContain("target 26.7");
  });

  it("lowest unique: draws a row of numbers with the winner", () => {
    const sealed = frameAt(LOWEST_UNIQUE, 2);
    expect(tableRows(sealed)[0][0]).toBe("hidden");
    expect(sealed.textContent).toContain("2 picks still hidden");
    const end = frameAt(LOWEST_UNIQUE, 4);
    expect(tableRows(end)[0]).toEqual(["2", "alpha (winner)", "once"]);
  });
});
