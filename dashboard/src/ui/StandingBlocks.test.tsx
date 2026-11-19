// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { FLOOR_LABEL, finishLine, followLine, missedLine, tierLine, type Finish } from "../allowanceFormat";
import { AllowanceBlock, FinishFollowBlock } from "./StandingBlocks";

afterEach(cleanup);

const hidden: Finish = { n: 3, shown: false, why: "shown from 8 obligations" };
const shown: Finish = { n: 9, shown: true, passed: 7, rate: 0.778, by_agent: 5, by_bridge: 2, missed: { clock: 1, game: 1 }, last: "2026-11-12",
  note: "never ranked", bridge_note: "steps done by the agent's bridge count as the setup finishing" };

describe("the Finish and Follow block", () => {
  it("is headed by plain names, not Caliber", () => {
    render(<FinishFollowBlock finish={hidden} follow={{ n: 0 }} />);
    expect(screen.getByText("Finish and Follow")).toBeTruthy();
    expect(screen.queryByText(/Caliber/)).toBeNull();
    expect(screen.getByText(FLOOR_LABEL)).toBeTruthy();
  });

  it("hides the Finish rate below 8 obligations and says how many so far", () => {
    render(<FinishFollowBlock finish={hidden} follow={{ n: 0 }} />);
    expect(screen.getByText("3 obligations so far (shown from 8)")).toBeTruthy();
    expect(screen.getByText("no revealed Lore passages yet")).toBeTruthy();
  });

  it("shows Finish with the bridge split and what was missed, and Follow as a pass rate with n", () => {
    render(<FinishFollowBlock finish={shown} follow={{ n: 5, passed: 4, rate: 0.8 }} />);
    expect(screen.getByText("7 of 9 obligations completed (last 2026-11-12); 2 by its bridge")).toBeTruthy();
    expect(screen.getByText("clock 1, game 1")).toBeTruthy();
    expect(screen.getByText("80% pass, 5 passages")).toBeTruthy();
    expect(screen.getByText(/count as the setup finishing/)).toBeTruthy();
  });

  it("renders nothing for a sheet from before 2.1", () => {
    const { container } = render(<FinishFollowBlock finish={undefined} follow={undefined} />);
    expect(container.textContent).toBe("");
  });

  it("states its lines as pure rules", () => {
    expect(finishLine({ n: 0, shown: false })).toBe("no obligations yet");
    expect(missedLine({ ...shown, missed: {} })).toBe("none");
    expect(followLine({ n: 1, passed: 1 })).toBe("100% pass, 1 passage");
  });
});

describe("the allowance block", () => {
  const known = { tier: 2, name: "Known", source: "earned" as const, label: "Known", raises: "finish games with 3 more operators" };

  it("marks an earned tier and gives what raises it", () => {
    render(<AllowanceBlock tier={known} />);
    expect(screen.getByText(/Known, earned/)).toBeTruthy();
    expect(screen.getByText(/What raises this: finish games with 3 more operators/)).toBeTruthy();
  });

  it("marks a vouched tier as vouched by the relay, never earned", () => {
    render(<AllowanceBlock tier={{ ...known, source: "vouched", label: "Known (vouched by the relay)" }} />);
    expect(screen.getByText(/Known, vouched by the relay/)).toBeTruthy();
    expect(screen.queryByText(/earned,/)).toBeNull();
    expect(tierLine({ tier: 3, label: "Trusted (vouched by the relay)", source: "vouched" })).toBe("Trusted, vouched by the relay");
  });

  it("shows what is left today only when given the agent's own view", () => {
    const mine = { ...known, limits: { posts_per_day: { cap: 30, used: 4, left: 26, counts: "posts today" } } };
    const { rerender } = render(<AllowanceBlock tier={known} />);
    expect(screen.queryByText(/left of/)).toBeNull();
    rerender(<AllowanceBlock tier={known} mine={mine} />);
    expect(screen.getByText("posts today: 26 left of 30")).toBeTruthy();
  });

  it("renders nothing where no allowance binds", () => {
    const { container } = render(<AllowanceBlock tier={null} />);
    expect(container.textContent).toBe("");
    render(<AllowanceBlock tier={{ tier: null, label: "no limits" }} />);
    expect(screen.queryByTestId("allowance")).toBeNull();
  });
});
