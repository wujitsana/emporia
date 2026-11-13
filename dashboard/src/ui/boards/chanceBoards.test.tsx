// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { buildTimeline } from "../../arena";
import { AUCTION, BARGAIN, DICE } from "../../arenaFixtures";
import { auctionFromView, bargainFromView, diceFromView } from "./chanceModel";
import { bargainView } from "../../bargain";
import { chanceView, type AuctionView, type DiceView } from "../../chance";
import { ArenaFrame } from "../ArenaFrames";
import { AuctionLine } from "./AuctionLine";
import { BargainSplit, type BargainSplitOffer } from "./BargainSplit";
import { DiceTable } from "./DiceTable";

afterEach(cleanup);

const rows = (c: HTMLElement) => [...c.querySelectorAll("table tbody tr")].map((r) => [...r.children].map((x) => x.textContent));
const frameOf = (fx: { session: Parameters<typeof buildTimeline>[0]; actions: Parameters<typeof buildTimeline>[1] }, i?: number) => {
  const t = buildTimeline(fx.session, fx.actions);
  return t.frameAt(i ?? t.steps.length - 1);
};

describe("DiceTable", () => {
  const bids = [{ n: 1, name: "alpha", quantity: 2, face: 4 }, { n: 2, name: "beta", quantity: 4, face: 4 }];
  const cups = [{ id: "alpha", name: "alpha", values: [1, 4, 6] }, { id: "beta", name: "beta", values: [2, 2, 5] }];

  it("in play: bids on the ladder, no die in the DOM even when handed the dice", () => {
    const { container } = render(<DiceTable bids={bids} call={null} over={false} cups={cups} />);
    expect(container.textContent).toContain("#2 beta");
    expect(rows(container)).toContainEqual(["alpha's cup", "", "closed", ""]);
    expect(container.textContent).not.toContain("held");
    expect(container.querySelector("[class*=die]")).toBeNull();
  });

  it("called, not yet revealed: the called bid is marked and the cups stay closed", () => {
    const { container } = render(<DiceTable bids={bids} call={{ bid: 2, byName: "alpha" }} over={false} cups={cups} />);
    expect(container.textContent).toContain("alpha called liar on bid 2 (4 × 4 by beta)");
    expect(rows(container)[1][3]).toBe("called liar by alpha");
    expect(container.textContent).not.toContain("needed");
  });

  it("finished: every cup, the dice that count, needed against showed", () => {
    const { container } = render(<DiceTable bids={bids} call={{ bid: 2, byName: "alpha" }} over cups={cups} />);
    expect(container.textContent).toContain("needed 4, showed 1 — the bid failed");
    expect(container.querySelectorAll("[class*=counts]").length).toBe(1);
    expect(rows(container)).toContainEqual(["alpha's cup", "", "held 1 4 6", ""]);
  });

  it("ones wild: ones count for the called face; the relay's count wins when given", () => {
    const { container } = render(<DiceTable bids={bids} call={{ bid: 2, byName: "alpha" }} over onesWild cups={cups} />);
    expect(container.querySelectorAll("[class*=counts]").length).toBe(2);
    const r = render(<DiceTable bids={bids} call={{ bid: 2, byName: "alpha" }} over cups={cups} showed={4} held />);
    expect(r.container.textContent).toContain("needed 4, showed 4 — the bid held");
  });

  it("a cup nobody revealed says so", () => {
    const { container } = render(<DiceTable bids={bids} call={{ bid: 2, byName: "alpha" }} over cups={[cups[0], { ...cups[1], values: null }]} />);
    expect(rows(container)).toContainEqual(["beta's cup", "", "never revealed", ""]);
  });

  it("the model drops dice until the result is in", () => {
    const live = chanceView(DICE.session, null) as DiceView;
    const p = diceFromView({ ...live, result: null }, ["alpha", "beta"]);
    expect(p.over).toBe(false);
    expect(p.cups.every((c) => c.values === null)).toBe(true);
    const done = diceFromView({ ...live, result: { winner: "alpha", count: 1, termination: "bid_failed" } }, ["alpha", "beta"]);
    expect(done.cups[0].values).toEqual([1, 4, 6]);
    expect(done.showed).toBe(1);
  });

  it("Arena: no dice before the last step, no secret or nonce ever", () => {
    const mid = render(<ArenaFrame frame={frameOf(DICE, 4)} book={undefined} />);
    expect(rows(mid.container).some((r) => String(r[2]).startsWith("held"))).toBe(false);
    expect(mid.container.textContent).toContain("dice stay hidden");
    cleanup();
    const end = render(<ArenaFrame frame={frameOf(DICE)} book={undefined} />);
    expect(rows(end.container)).toContainEqual(["alpha's cup", "", "held 1 4 6", ""]);
    expect(end.container.textContent).toContain("needed 4, showed 1");
    for (const secret of ["ss", "nn", "tt", "mm"]) expect(end.container.textContent).not.toMatch(new RegExp(`\\b${secret}\\b`));
  });
});

describe("AuctionLine", () => {
  const winner = { id: "beta", name: "beta", bid: 61, profit: -6 };
  const bids = [{ id: "alpha", name: "alpha", value: 52 }, { id: "beta", name: "beta", value: 61 }];
  const signals = [{ id: "alpha", name: "alpha", value: 47 }, { id: "beta", name: "beta", value: 53 }];

  it("sealed: a count of bids, no position, no signal or V even when handed them", () => {
    const { container } = render(<AuctionLine bids={[]} sealed={2} base={null} over={false} signals={signals} value={55} winner={winner} />);
    expect(container.textContent).toContain("2 bids still sealed");
    expect(container.textContent).not.toContain("55");
    expect(container.textContent).not.toContain("47");
    expect(container.querySelector("[class*=pin]")).toBeNull();
    expect(rows(container)).toContainEqual(["sealed", "2 bids not revealed", ""]);
  });

  it("partly revealed: the revealed bid is a pin, the other stays a count", () => {
    const { container } = render(<AuctionLine bids={[bids[0]]} sealed={1} base={40} over={false} signals={null} value={null} winner={null} />);
    expect(rows(container)[0]).toEqual(["alpha", "52", "private"]);
    expect(rows(container)).toContainEqual(["base value", "40", ""]);
    expect(rows(container)).toContainEqual(["pot worth V", "not known yet", ""]);
  });

  it("finished, the winner's curse: the segment is the relay's loss", () => {
    const { container } = render(<AuctionLine bids={bids} base={40} over signals={signals} value={55} winner={winner} />);
    expect(container.textContent).toContain("beta paid 6 more than the pot was worth: the winner's curse");
    expect(container.textContent).toContain("-6 (curse)");
    expect(rows(container)).toContainEqual(["winner's result", "-6", "beta paid 6 more than the pot was worth: the winner's curse"]);
  });

  it("finished, a gain; and break-even", () => {
    const gain = render(<AuctionLine bids={bids} base={40} over signals={signals} value={70} winner={{ ...winner, profit: null }} />);
    expect(gain.container.textContent).toContain("beta gained 9");
    cleanup();
    const even = render(<AuctionLine bids={bids} base={40} over signals={signals} value={61} winner={{ ...winner, profit: 0 }} />);
    expect(even.container.textContent).toContain("beta broke even");
  });

  it("the fixture's board agrees with the relay's result", () => {
    const view = chanceView(AUCTION.session, AUCTION.actions) as AuctionView;
    const p = auctionFromView(view);
    expect(p.winner).toEqual({ id: "beta", name: "beta", bid: 61, profit: -6 });
    expect(p.value).toBe(55);
    expect(p.winner!.profit).toBe(p.value! - p.winner!.bid);
    const { container } = render(<AuctionLine {...p} />);
    expect(container.textContent).toContain("-6 (curse)");
  });

  it("Arena: bids are public as revealed, V and signals only on the last step; secrets never", () => {
    const mid = render(<ArenaFrame frame={frameOf(AUCTION, 4)} book={undefined} />);
    expect(mid.container.textContent).toContain("52");
    expect(mid.container.textContent).not.toMatch(/signal 47|V 55/);
    expect(mid.container.textContent).toContain("V stays unknown");
    cleanup();
    const end = render(<ArenaFrame frame={frameOf(AUCTION)} book={undefined} />);
    expect(end.container.textContent).toContain("signal 47");
    expect(end.container.textContent).toContain("V 55");
    for (const secret of ["ss", "nn", "tt", "mm"]) expect(end.container.textContent).not.toMatch(new RegExp(`\\b${secret}\\b`));
  });
});

describe("BargainSplit", () => {
  const sides = [{ id: "alpha", name: "alpha" }, { id: "beta", name: "beta" }];
  const pool = { silk: 4, spice: 3, amber: 3 };
  const offers: BargainSplitOffer[] = [
    { n: 1, byName: "alpha", give: { alpha: { silk: 4, spice: 1, amber: 0 }, beta: { silk: 0, spice: 2, amber: 3 } } },
    { n: 2, byName: "beta", give: { beta: { silk: 1, spice: 2, amber: 3 } }, claim: { silk: 0, spice: 4, amber: 6 } },
  ];
  const hands = { alpha: { silk: 5, spice: 1, amber: 0 }, beta: { silk: 0, spice: 3, amber: 4 } };

  it("running: splits only; handed hands and points never reach the DOM", () => {
    const { container } = render(<BargainSplit sides={sides} pool={pool} offers={offers} accepted={null} over={false}
      hands={hands} points={{ alpha: 15, beta: 14 }} walkAways={{ alpha: 2, beta: 3 }} claims={null} how={null} />);
    expect(rows(container)[0].slice(0, 4)).toEqual(["1", "alpha", "4 silk, 1 spice, 0 amber", "0 silk, 2 spice, 3 amber"]);
    expect(rows(container)[1][2]).toBe("3 silk, 1 spice, 0 amber");   // the rest of the pool
    expect(rows(container)[1][4]).toBe("0 silk, 4 spice, 6 amber (stated, unchecked)");
    expect(container.textContent).not.toContain("held");
    expect(container.textContent).not.toContain("15 points");
    expect(container.textContent).not.toContain("walk-away");
    expect(rows(container).slice(-2).map((r) => r[2])).toEqual(["private until the end", "private until the end"]);
  });

  it("no offer yet", () => {
    const { container } = render(<BargainSplit sides={sides} pool={pool} offers={[]} accepted={null} over={false}
      hands={null} points={null} walkAways={null} claims={null} how={null} />);
    expect(container.textContent).toContain("No offer yet.");
  });

  it("finished with a deal: the accepted offer, hands against claims, points against walk-aways", () => {
    const claims = { beta: { claimed: { silk: 0, spice: 4, amber: 6 }, actual: hands.beta, matches: false } };
    const { container } = render(<BargainSplit sides={sides} pool={pool} offers={offers} accepted={2} over
      hands={hands} points={{ alpha: 15, beta: 14 }} walkAways={{ alpha: 2, beta: 3 }} claims={claims} how="accepted" />);
    expect(rows(container)[1][5]).toBe("accepted");
    expect(container.textContent).toContain("deal: offer 2 accepted");
    expect(rows(container)).toContainEqual(["beta", "end", "held 0 silk, 3 spice, 4 amber", "said 0 silk, 4 spice, 6 amber: differed", "14 points, walk-away 3"]);
  });

  it("declined: no deal", () => {
    const { container } = render(<BargainSplit sides={sides} pool={pool} offers={offers} accepted={null} over
      hands={null} points={{ alpha: 2, beta: 3 }} walkAways={null} claims={null} how="declined" />);
    expect(container.textContent).toContain("no deal: the last offer was declined");
    expect(rows(container).at(-1)![2]).toBe("hand not published");
  });

  it("the model drops hands and points while the game runs", () => {
    const v = bargainView(BARGAIN.session, BARGAIN.actions);
    const live = bargainFromView(v, ["alpha", "beta"], false);
    expect(live.hands).toBeNull();
    expect(live.points).toBeNull();
    expect(live.claims).toBeNull();
    const done = bargainFromView(v, ["alpha", "beta"], true);
    expect(done.accepted).toBe(2);
    expect(done.walkAways).toEqual({ alpha: 2, beta: 3 });
  });

  it("Arena: hands only on the last step", () => {
    const mid = render(<ArenaFrame frame={frameOf(BARGAIN, 2)} book={undefined} />);
    expect(mid.container.textContent).not.toContain("held");
    expect(mid.container.textContent).toContain("private until the end");
    cleanup();
    const end = render(<ArenaFrame frame={frameOf(BARGAIN)} book={undefined} />);
    expect(end.container.textContent).toContain("held 5 silk, 1 spice, 0 amber");
    expect(end.container.textContent).toContain("15 points, walk-away 2");
  });
});
