// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LabsView } from "./LabsView";
import { api } from "../api";

// Labs are read from the relay's public /labs routes: the lab, its signed reports (public in every lab), its threads.

const lab = {
  slug: "emporia-core", name: "Emporia core", room_id: "r_lab", subject: { kind: "github_repo", ref: "acme/emporia" },
  repo_url: "https://github.com/acme/emporia", branch: "main", creator_id: "alpha", maintainers: ["stable-maintainer-one-xxxxxxxxxxxxxxxx"],
  licence: "MIT", verified: true, room: { type: "public", gate: "open", encrypted: false, entry_fee_cents: 0 },
  created_at: "2026-11-01T00:00:00Z",
};
const report = {
  report_id: "lrp_1", lab: "emporia-core", ref: "gh_pr:acme/emporia#12", event: "merged", maintainer_id: "alpha",
  author_id: "beta", author_stable_id: "s-beta", reviewer_ids: ["gamma"], reviewer_stable_ids: ["s-gamma"],
  ideas: ["post:p1", "post:p2"], note: "", at: "2026-11-05T12:00:00Z",
};

vi.mock("../api", () => ({
  api: { labs: vi.fn(), labReports: vi.fn(), labThreads: vi.fn(), labBoard: vi.fn() },
}));

const mocked = api as unknown as Record<"labs" | "labReports" | "labThreads" | "labBoard", ReturnType<typeof vi.fn>>;

beforeEach(() => {
  mocked.labs.mockReset();
  mocked.labReports.mockReset();
  mocked.labThreads.mockReset();
  mocked.labBoard.mockReset();
  mocked.labBoard.mockResolvedValue({ lab: "emporia-core", board: [] });
});
afterEach(cleanup);

describe("labs view", () => {
  it("draws the lab, its signed reports and its threads as the page's cards", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [report] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [
      { ref: "gh_pr:acme/emporia#12", state: "merged", messages: 3, last_at: "2026-11-05T12:00:00Z", reports: ["merged"] },
      { ref: "gh_issue:acme/emporia#7", state: "open", messages: 1, last_at: null, reports: [] },
    ] });
    render(<LabsView />);
    await screen.findByText(/^Emporia core$/i);
    await screen.findByText(/author beta/);
    expect(document.querySelectorAll(".e-card-grid .e-hcard").length).toBe(4);   // the lab, 1 report, 2 threads
    expect(screen.getByText("REPO VERIFIED")).toBeTruthy();
    expect(screen.getByText(/^Issue #7$/i)).toBeTruthy();
    expect(screen.getByText(/3 messages/)).toBeTruthy();
    expect(screen.getByText("open room")).toBeTruthy();
  });

  it("says a repo is not verified, and why", async () => {
    mocked.labs.mockResolvedValue({ labs: [{ ...lab, verified: false, unverified_note: "the repo does not carry the lab's proof file" }] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    render(<LabsView />);
    expect(await screen.findByText("NOT VERIFIED")).toBeTruthy();
    expect(screen.getByText("the repo does not carry the lab's proof file")).toBeTruthy();
    expect(await screen.findByText(/No report yet/)).toBeTruthy();
    expect(screen.getByText(/No thread yet/)).toBeTruthy();
  });

  it("shows who the lab's reports credit, as a board", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [report] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    mocked.labBoard.mockResolvedValue({ lab: "emporia-core", rule: "the maintainer's own operator never counts", board: [
      { agent_id: "beta", stable_id: "s-beta", fix_landed: 1, idea_adopted: 2, score: 7 },
      { agent_id: "gamma", stable_id: "s-gamma", fix_reviewed: 1, score: 1 },
    ] });
    render(<LabsView />);
    const board = await screen.findByRole("list", { name: "Credits" });
    expect(board.textContent).toContain("beta");
    expect(board.textContent).toContain("7 pts · 1 fix landed, 2 ideas adopted");
    expect(board.textContent).toContain("1 pts · 1 fix reviewed");
    expect(mocked.labBoard).toHaveBeenCalledWith("emporia-core");
  });

  it("has no button that acts: the page only reads", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [report] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    render(<LabsView />);
    await screen.findByText(/author beta/);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("shows each lab's repo, maintainers, reports with what they credited, and threads", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [report] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [
      { ref: "gh_pr:acme/emporia#12", state: "merged", messages: 3, last_at: "2026-11-05T12:00:00Z", reports: ["merged"] },
    ] });
    render(<LabsView />);
    expect(await screen.findByText(/^Emporia core$/i)).toBeTruthy();
    const repo = screen.getByRole("link", { name: "acme/emporia" }) as HTMLAnchorElement;
    expect(repo.href).toBe("https://github.com/acme/emporia");
    expect(repo.rel).toContain("noopener");
    expect(screen.getByText(/Maintainers · 1/)).toBeTruthy();
    // the report names what it credited: the author, the reviewer, the ideas it adopted
    expect(await screen.findByText(/author beta · reviewer gamma · 2 ideas adopted/)).toBeTruthy();
    expect(screen.getAllByText("PR #12").length).toBe(2);
    // the report's event and the thread's state are each a card's chip
    expect([...document.querySelectorAll(".e-hcard__chip")].filter((c) => c.textContent === "MERGED")).toHaveLength(2);
    expect(mocked.labReports).toHaveBeenCalledWith("emporia-core");
  });

  it("says a private lab's threads are its members', and still shows its public reports", async () => {
    mocked.labs.mockResolvedValue({ labs: [{ ...lab, room: { type: "private", gate: "invite", encrypted: true, entry_fee_cents: 0 } }] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [report] });
    mocked.labThreads.mockRejectedValue(new Error("403 /labs/emporia-core/threads"));
    render(<LabsView />);
    expect(await screen.findByText(/threads are for its members/)).toBeTruthy();
    expect(await screen.findByText(/author beta/)).toBeTruthy();
  });

  it("says how a lab starts when there is none, with a command to copy and nothing that acts", async () => {
    mocked.labs.mockResolvedValue({ labs: [] });
    render(<LabsView />);
    expect(await screen.findByText("No lab on this relay yet.")).toBeTruthy();
    expect(screen.getAllByText(/emporia lab NAME create/).length).toBeGreaterThan(0);
    // the only button is the copy button of the command
    const buttons = screen.getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["copy"]);
  });

  it("says so when the relay does not run labs (the routes answer 404)", async () => {
    mocked.labs.mockRejectedValue(new Error("404 /labs"));
    render(<LabsView />);
    expect(await screen.findByText("This relay doesn't run labs.")).toBeTruthy();
  });
});
