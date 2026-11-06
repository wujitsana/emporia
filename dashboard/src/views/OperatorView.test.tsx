// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OperatorView } from "./OperatorView";
import type { OperatorStatus } from "../operatorCommands";

const status: OperatorStatus = {
  agent_id: "opdesk", relay_id: "r1", relay_url: "http://relay.test", as_of: "2026-11-05T12:00:00+00:00",
  bugs: {
    on: true, open: 2, severities: ["low", "medium", "high", "critical"], verdicts: ["accepted", "duplicate", "invalid"],
    items: [
      { report_id: "bug_0123456789abcdef", reporter_id: "finder", commit: "a1b2c3d", severity_claimed: "high",
        sealed: false, title: "parse crashes", test: "def test_x():\n    assert parse('') == []",
        description: null, created_at: "2026-11-05T10:00:00+00:00" },
      { report_id: "bug_fedcba9876543210", reporter_id: "quiet", commit: "a1b2c3d", severity_claimed: "low",
        sealed: true, title: null, test: null, description: null, created_at: "2026-11-05T11:00:00+00:00",
        where: "sealed: the report and its test are in the encrypted DM from quiet" },
    ],
  },
  ideas: { topic: "ideas", topic_exists: true, verdicts: false, open: 1,
    items: [{ post_id: "pst_0123456789ab", author_id: "thinker", title: "Name the commit", excerpt: "so a report can",
      truncated: false, votes: 2, created_at: "2026-11-04T00:00:00+00:00" }] },
  expedition: { topic: "expeditions", topic_exists: false, current: null },
  house: { agents: [{ agent_id: "house-1", registered: true, last_active_at: "2026-11-05T11:30:00+00:00", active: true,
    open_games: 1 }], window_s: 3600,
    step_back: { others_active: 5, at: 4, busy_window_s: 3600, stepping_back: true, note: "at the defaults" } },
  health: { version: "0.9.0", commit: "abcdef1234567890", schema: { code: 31, database: 31 },
    started_at: "2026-11-05T00:00:00+00:00", uptime_s: 7260, db_bytes: 4096, last_backup: null, active_agents: 6,
    window_s: 3600, rate_limit_hits: { ip: 3, agent: 1, since: "2026-11-05T00:00:00+00:00" }, modules: "gamify (bugs, sheets)" },
  season: { season: "season-0", started_at: null, ends_at: null, on: true, games: 4, likes: 2,
    boards: [{ board: "bugs", entries: 1, top: [{ agent_id: "finder", points: 40 }] }] },
};

vi.mock("../api", () => ({ api: { operatorStatus: vi.fn(() => Promise.resolve(status)) } }));

afterEach(cleanup);

describe("the operator's desk", () => {
  it("shows each queue item with its command, and the test beside it, never inside it", async () => {
    render(<OperatorView />);
    expect(await screen.findByText("Bug reports waiting for a verdict")).toBeTruthy();
    // No console in a test: the NAME is the agent id it was opened as, or the placeholder.
    const name = (await screen.findAllByText(/hermes emporia bugs \S+ verdict bug_0123456789abcdef accepted/))[0]
      .textContent!.split(" ")[3];
    expect(screen.getByText(`hermes emporia bugs ${name} verdict bug_0123456789abcdef accepted --severity high --fix FIX_COMMIT`)).toBeTruthy();
    expect(screen.getByText(`hermes emporia bugs ${name} verdict bug_fedcba9876543210 invalid`)).toBeTruthy();
    expect(screen.getByLabelText("The failing test").textContent).toContain("assert parse('') == []");
    expect(screen.getByText(/encrypted DM from quiet/)).toBeTruthy();
    for (const code of document.querySelectorAll("code")) {
      expect(code.textContent).not.toContain("parse('')");
    }
  });

  it("offers no idea verdict where the relay takes none, and the relay controls with their gate", async () => {
    render(<OperatorView />);
    expect(await screen.findByText("Name the commit")).toBeTruthy();
    expect(screen.getByText(/takes no idea verdicts yet/)).toBeTruthy();
    expect(screen.queryByText(/hermes emporia ideas/)).toBeNull();
    expect(screen.getByText(/hermes emporia operator \S+ expedition 'TITLE' --text 'WHAT TO TRY'/)).toBeTruthy();
    expect(screen.getByText("hermes emporia house start house-1")).toBeTruthy();
    expect(screen.getByText(/hermes emporia operator \S+ snapshot/)).toBeTruthy();
    expect(screen.getByText(/it is stepping back now/)).toBeTruthy();
    expect(screen.getAllByText(/only you run this/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/waits for your yes/).length).toBeGreaterThan(0);
  });

  it("has no button but copy: it only reads", async () => {
    render(<OperatorView />);
    await screen.findByText("Relay health");
    const buttons = [...document.querySelectorAll("button")].map((b) => b.textContent);
    expect(buttons.length).toBeGreaterThan(0);
    expect(new Set(buttons)).toEqual(new Set(["copy"]));
  });
});
