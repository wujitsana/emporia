// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ago, relayLogView, type KeptCheck, type LogState } from "./relayLog";
import { RelayLogLine } from "./ui/RelayLog";

const NOW = Date.parse("2026-11-09T12:00:00Z");
const minutesAgo = (m: number) => new Date(NOW - m * 60_000).toISOString();
const state = (over: Partial<LogState> = {}): LogState => ({ size: 1204, root: "ab", at: minutesAgo(3), ...over });
const kept = (over: Partial<KeptCheck> = {}): KeptCheck => ({
  status: "ok", ok: true, detail: "the log grew from 1100 to 1204 entries and kept what it had",
  checked_at: minutesAgo(20), size: 1204, epoch: 1, state_at: minutesAgo(25), failures: 0, ...over,
});

afterEach(cleanup);

describe("P281 the relay log line", () => {
  it("says plainly when the relay keeps no log, and shows no check", () => {
    const v = relayLogView({ on: false, state: null }, { relay: "r", kept: null }, NOW);
    expect(v.facts).toEqual(["This relay keeps no transparency log"]);
    expect(v.check).toBeNull();
    render(<RelayLogLine relay={{ on: false, state: null }} check={{ relay: "r", kept: null }} />);
    expect(screen.getByText("This relay keeps no transparency log")).toBeTruthy();
    expect(screen.queryByText(/append-only/)).toBeNull();
  });

  it("shows the size, when it was signed and a later log's number; not checked yet", () => {
    const v = relayLogView({ on: true, state: state({ epoch: 2 }) }, { relay: "r", kept: null }, NOW);
    expect(v.facts).toEqual(["1,204 entries", "signed 3 min ago", "log #2"]);
    expect(v.check).toEqual({ tone: "none", text: "The agent has not checked it yet" });
    expect(relayLogView({ on: true, state: state() }, null, NOW).facts).toEqual(["1,204 entries", "signed 3 min ago"]);
  });

  it("says the agent's check found it consistent, with when", () => {
    const v = relayLogView({ on: true, state: state() }, { relay: "r", kept: kept() }, NOW);
    expect(v.check).toEqual({ tone: "ok", text: "The agent checked it: consistent ✓ · 20 min ago" });
  });

  it("warns when the agent's check caught a problem, now or before", () => {
    const fail = kept({ status: "fail", ok: false, detail: "the log shrank from 1204 to 900 entries", failures: 1 });
    const v = relayLogView({ on: true, state: state() }, { relay: "r", kept: fail }, NOW);
    expect(v.check?.tone).toBe("warn");
    expect(v.check?.text).toContain("the log shrank from 1204 to 900 entries");
    const noHandover = kept({ status: "fail", ok: false, failures: 1,
      detail: "the relay started a new log (number 2) without a signed hand-over" });
    expect(relayLogView({ on: true, state: state() }, { relay: "r", kept: noHandover }, NOW).check?.text)
      .toContain("without a signed hand-over");
    expect(relayLogView({ on: true, state: state() }, { relay: "r", kept: kept({ failures: 2 }) }, NOW).check?.tone)
      .toBe("warn");
    render(<RelayLogLine relay={{ on: true, state: state() }} check={{ relay: "r", kept: fail }} />);
    expect(screen.getByRole("alert").textContent).toContain("found a problem");
  });

  it("only ever describes: no button, no link, nothing to press", () => {
    const { container } = render(<RelayLogLine relay={{ on: true, state: state() }} check={{ relay: "r", kept: kept() }} />);
    expect(container.querySelectorAll("button, a, input").length).toBe(0);
    expect(screen.getByText(/append-only public log/)).toBeTruthy();
  });

  it("reads times as relative", () => {
    expect(ago(minutesAgo(0), NOW)).toBe("just now");
    expect(ago(minutesAgo(180), NOW)).toBe("3 h ago");
    expect(ago(minutesAgo(60 * 24 * 3), NOW)).toBe("3 days ago");
    expect(ago("not a time", NOW)).toBe("");
  });
});
