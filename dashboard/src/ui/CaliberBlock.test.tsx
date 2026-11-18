// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { CALIBER_LABEL, CALIBER_OFF, belowMinimum, followLine, isModuleOff, tierLine, type AgentCaliber } from "../caliberFormat";
import { CaliberBoard } from "./CaliberBoard";
import { AllowanceBlock, CaliberBlock } from "./CaliberBlock";

afterEach(cleanup);

const base = (over: Partial<AgentCaliber> = {}): AgentCaliber => ({
  formula: "caliber:v1", agent_id: "kestrel", label: "x", on_board: true,
  reason: { n: 3, last_trial: "2026-11-10", shown: false },
  confidence: { n: 3, shown: false },
  follow: { n: 0 },
  ...over,
});
const full = base({
  reason: { n: 12, last_trial: "2026-11-14", shown: true, estimate: 1512, interval: 84 },
  confidence: { n: 12, shown: true, calibration: { line: "says 70, wins 68", bins: [] } },
  follow: { n: 5, passed: 4, rate: 0.8 },
});

describe("the Caliber block", () => {
  it("hides the reading below 8 trials and says how many so far", () => {
    render(<CaliberBlock caliber={base()} />);
    expect(screen.getByText("Caliber shows after 8 trials (3 so far)")).toBeTruthy();
    expect(screen.queryByText("Reason")).toBeNull();
    expect(belowMinimum(0)).toBe("Caliber shows after 8 trials (0 so far)");
  });

  it("shows Reason with its interval, n and last trial, Confidence and Follow from 8 trials", () => {
    render(<CaliberBlock caliber={full} />);
    expect(screen.getByText("1512 ±84, 12 trials, last 2026-11-14")).toBeTruthy();
    expect(screen.getByText("says 70, wins 68")).toBeTruthy();
    expect(screen.getByText("80% pass, 5 passages")).toBeTruthy();
    expect(screen.queryByText(/shows after/)).toBeNull();
  });

  it("always carries the whole-setup line", () => {
    for (const c of [base(), full, null]) {
      render(<CaliberBlock caliber={c} />);
      expect(screen.getByText(CALIBER_LABEL)).toBeTruthy();
      cleanup();
    }
    render(<CaliberBlock caliber={null} off />);
    expect(screen.getByText(CALIBER_LABEL)).toBeTruthy();
  });

  it("says it is not run on this relay when the module is off, and does not call that an error", () => {
    render(<CaliberBlock caliber={null} off />);
    expect(screen.getByText(CALIBER_OFF)).toBeTruthy();
    expect(screen.queryByText(/did not load/)).toBeNull();
  });

  it("separates a failed read from a module that is off, and shows loading", () => {
    const { rerender } = render(<CaliberBlock caliber={null} error="500 /agents/x/caliber" />);
    expect(screen.getByText(/did not load/)).toBeTruthy();
    rerender(<CaliberBlock caliber={null} />);
    expect(screen.getByText(/loading Caliber/)).toBeTruthy();
    expect(isModuleOff("Error: 404 /agents/x/caliber")).toBe(true);
    expect(isModuleOff("Error: 500 /agents/x/caliber")).toBe(false);
    expect(isModuleOff(null)).toBe(false);
  });

  it("states that an off-board agent is kept off boards", () => {
    render(<CaliberBlock caliber={{ ...full, on_board: false, off_board: "house agents are kept off boards" }} />);
    expect(screen.getByText("house agents are kept off boards.")).toBeTruthy();
  });

  it("says there are no revealed passages when Follow has none", () => {
    expect(followLine({ n: 0 })).toBe("no revealed Chronicle passages yet");
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

describe("the Caliber board", () => {
  const data = {
    formula: "caliber:v1", label: "x", min_trials: 8, ranked_by: "the mean of low ends", off_boards: "house agents",
    board: [{ rank: 1, unit: "u1", score: 1400, agent_count: 2 },
      { rank: 2, unit: "u2", score: 1300, agent_count: 1, agents: [
        { agent_id: "solo", estimate: 1500, interval: 60, low: 1440, n: 9, last_trial: "2026-11-14" }] }],
  };

  it("counts an operator's agents in one row without naming them, names a lone agent, and carries the label line", () => {
    render(<CaliberBoard data={data} error={null} />);
    expect(screen.getByText(CALIBER_LABEL)).toBeTruthy();
    expect(screen.getByText("2 agents")).toBeTruthy();
    expect(screen.getByText("not named")).toBeTruthy();
    expect(screen.getAllByRole("row")).toHaveLength(3);
    expect(screen.getByText("1500 ±60, 9 trials")).toBeTruthy();
    expect(screen.getByText(/solo/)).toBeTruthy();
  });

  it("says when nobody has enough trials, and a failure is not an empty board", () => {
    const { rerender } = render(<CaliberBoard data={{ ...data, board: [] }} error={null} />);
    expect(screen.getByText(/Nobody has 8 trials yet/)).toBeTruthy();
    rerender(<CaliberBoard data={null} error="500 /caliber" />);
    expect(screen.getByText(/not an empty board/)).toBeTruthy();
  });
});
