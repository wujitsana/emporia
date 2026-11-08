// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OperatorView } from "./OperatorView";
import type { OperatorStatus } from "../operatorCommands";

const status: OperatorStatus = {
  agent_id: "opdesk", relay_id: "r1", relay_url: "http://relay.test", as_of: "2026-11-05T12:00:00+00:00",
  labs: { on: true, reports: 2,
    labs: [{ slug: "emporia", subject: "OWNER/emporia", verified: true, maintainers: 1 }],
    recent: [{ lab: "emporia", ref: "gh_pr:OWNER/emporia#5", event: "merged", maintainer_id: "opdesk", at: "2026-11-05T10:40:00+00:00" }],
    open_threads: [{ lab: "emporia", ref: "gh_issue:OWNER/emporia#9", messages: 2, last_at: "2026-11-05T11:30:00+00:00" }] },
  expedition: { topic: "expeditions", topic_exists: false, current: null },
  house: { agents: [{ agent_id: "house-1", registered: true, last_active_at: "2026-11-05T11:30:00+00:00", active: true,
    open_games: 1 }], window_s: 3600,
    step_back: { others_active: 5, at: 4, busy_window_s: 3600, stepping_back: true, note: "at the defaults" } },
  health: { version: "0.9.0", commit: "abcdef1234567890", schema: { code: 31, database: 31 },
    started_at: "2026-11-05T00:00:00+00:00", uptime_s: 7260, db_bytes: 4096, last_backup: null, active_agents: 6,
    window_s: 3600, rate_limit_hits: { ip: 3, agent: 1, since: "2026-11-05T00:00:00+00:00" }, modules: "gamify (quests, sheets)" },
  season: { season: "season-0", started_at: null, ends_at: null, on: true, games: 4, likes: 2,
    boards: [{ board: "ideas", entries: 1, top: [{ agent_id: "finder", points: 40 }] }] },
};

vi.mock("../api", () => ({ api: { operatorStatus: vi.fn(() => Promise.resolve(status)) } }));

afterEach(cleanup);

describe("the operator's desk", () => {
  it("has no idea queue (ideas are adopted by lab reports), and the relay controls with their gate", async () => {
    render(<OperatorView />);
    expect((await screen.findAllByText("Lab activity")).length).toBe(1);
    expect(screen.queryByText(/not yet judged/)).toBeNull();
    expect(screen.queryByText(/hermes emporia ideas/)).toBeNull();
    expect(screen.getByText(/hermes emporia operator \S+ expedition 'TITLE' --text 'WHAT TO TRY'/)).toBeTruthy();
    expect(screen.getByText("hermes emporia house start house-1")).toBeTruthy();
    expect(screen.getByText(/hermes emporia operator \S+ snapshot/)).toBeTruthy();
    expect(screen.getByText(/it is stepping back now/)).toBeTruthy();
    expect(screen.getAllByText(/waits for your yes/).length).toBeGreaterThan(0);
  });

  it("shows the labs' recent reports and open threads, with the command to read a lab", async () => {
    render(<OperatorView />);
    expect((await screen.findAllByText("Lab activity")).length).toBe(1);
    expect(screen.getByText(/gh_pr:OWNER\/emporia#5/)).toBeTruthy();
    expect(screen.getByText(/gh_issue:OWNER\/emporia#9/)).toBeTruthy();
    expect(screen.getByText(/hermes emporia lab \S+ show emporia/)).toBeTruthy();
    expect(screen.queryByText(/Patches waiting/)).toBeNull();
  });

  it("has no button but copy: it only reads", async () => {
    render(<OperatorView />);
    await screen.findByText("Relay health");
    const buttons = [...document.querySelectorAll("button")].map((b) => b.textContent);
    expect(buttons.length).toBeGreaterThan(0);
    expect(new Set(buttons)).toEqual(new Set(["copy"]));
  });
});
