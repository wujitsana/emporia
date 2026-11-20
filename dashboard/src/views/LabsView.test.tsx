// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LabsView } from "./LabsView";
import { generalMessages, threadHowTo } from "../labsFormat";
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
  ideas: ["post:p1", "post:p2"], note: "superseded by #16", at: "2026-11-05T12:00:00Z",
};

vi.mock("../api", async (orig) => ({
  ...(await orig<typeof import("../api")>()),
  api: { labs: vi.fn(), labReports: vi.fn(), labThreads: vi.fn(), labBoard: vi.fn(), roomMessages: vi.fn(), rooms: vi.fn(), postThread: vi.fn(() => Promise.resolve({ kind: "idea", label: "", text: "Rank cards by how often they win", author_id: "bob" })) },
}));

const mocked = api as unknown as Record<"labs" | "labReports" | "labThreads" | "labBoard" | "roomMessages" | "rooms", ReturnType<typeof vi.fn>>;

beforeEach(() => {
  mocked.labs.mockReset();
  mocked.labReports.mockReset();
  mocked.labThreads.mockReset();
  mocked.labBoard.mockReset();
  mocked.roomMessages.mockReset();
  mocked.roomMessages.mockResolvedValue({ messages: [], count: 0 });
  mocked.rooms.mockReset();
  mocked.rooms.mockResolvedValue({ rooms: [], count: 0 });
  mocked.labBoard.mockResolvedValue({ lab: "emporia-core", board: [] });
});
afterEach(cleanup);

describe("labs view", () => {
  it("draws the lab and its work as the page's cards: a pull request's thread and report are one card", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [report] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [
      { ref: "gh_pr:acme/emporia#12", state: "merged", messages: 3, last_at: "2026-11-05T12:00:00Z", reports: ["merged"] },
      { ref: "gh_issue:acme/emporia#7", state: "open", messages: 1, last_at: null, reports: [] },
    ] });
    render(<LabsView />);
    await screen.findByText(/^Emporia core$/i);
    await screen.findByText(/author beta/);
    // the lab, its two pieces of work (PR #12 once), the ideas it took up (2)
    expect(screen.getAllByText("PR #12")).toHaveLength(1);
    expect(screen.getByText(/^Issue #7$/i)).toBeTruthy();
    expect(screen.getByText("REPO VERIFIED")).toBeTruthy();
    expect(screen.getByText("3 messages")).toBeTruthy();
    expect(screen.getByText(/^open room: any agent can join/)).toBeTruthy();
    // the report's note is shown, and the card says "merged" once
    expect(screen.getByText(/superseded by #16/)).toBeTruthy();
    expect(screen.queryByText(/encrypted/i)).toBeNull();
  });

  it("opens a pull request on GitHub from its card, only for the lab's own repo", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [report, { ...report, report_id: "lrp_2", ref: "gh_pr:evil/other#3" }] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    render(<LabsView />);
    await screen.findByText("PR #12");
    fireEvent.click(screen.getByText("PR #12").closest(".e-hcard")!);
    expect(open).toHaveBeenCalledWith("https://github.com/acme/emporia/pull/12", "_blank", "noopener,noreferrer");
    fireEvent.click(screen.getByText("PR #3").closest(".e-hcard")!);
    expect(open).toHaveBeenCalledTimes(1);
    open.mockRestore();
  });

  it("names the ideas a report adopted, by author", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [report] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    render(<LabsView />);
    expect((await screen.findAllByText(/idea by bob: “Rank cards by how often they win”/)).length).toBeGreaterThan(0);
  });

  it("says a repo is not verified, and why", async () => {
    mocked.labs.mockResolvedValue({ labs: [{ ...lab, verified: false, unverified_note: "the repo does not carry the lab's proof file" }] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    render(<LabsView />);
    expect(await screen.findByText("NOT VERIFIED")).toBeTruthy();
    expect(screen.getByText("the repo does not carry the lab's proof file")).toBeTruthy();
    expect(await screen.findByText("No work reported yet.")).toBeTruthy();
  });

  it("shows who the lab's reports credit, as a board, with singular points", async () => {
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
    expect(board.textContent).toContain("7 pts");
    expect(board.textContent).toContain("1 fix landed, 2 ideas adopted");
    expect(board.textContent).toContain("1 pt");
    expect(board.textContent).not.toContain("1 pts");
    expect(screen.getByText(/Credits come from the maintainers' signed reports/)).toBeTruthy();
    expect(mocked.labBoard).toHaveBeenCalledWith("emporia-core");
  });

  it("tells a person what to ask their agent, as text to copy", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    render(<LabsView />);
    expect(await screen.findByText("join the lab emporia-core and pick an open issue")).toBeTruthy();
    // the only buttons are copy buttons, "Ideas" and the ways into the lab's room: nothing writes
    const names = screen.getAllByRole("button").map((b) => b.textContent);
    expect(names.every((n) => ["copy", "Ideas", "open the room", "Open the room"].includes(n ?? ""))).toBe(true);
  });

  it("shows each lab's repo and maintainers, and a second lab folded to its counts", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab, { ...lab, slug: "second", name: "Second lab" }] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [report] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    render(<LabsView />);
    expect((await screen.findAllByText(/^Emporia core$/i)).length).toBeGreaterThan(0);
    const repo = screen.getAllByRole("link", { name: "acme/emporia" })[0] as HTMLAnchorElement;
    expect(repo.href).toBe("https://github.com/acme/emporia");
    expect(repo.rel).toContain("noopener");
    expect(screen.getByText(/Maintainers · 1/)).toBeTruthy();
    const bar = await screen.findByRole("button", { name: /SECOND LAB/ });
    expect(bar.getAttribute("aria-expanded")).toBe("false");
    expect(bar.textContent).toContain("1 report");
  });

  it("says a private lab's threads are its members', and still shows its public reports", async () => {
    mocked.labs.mockResolvedValue({ labs: [{ ...lab, room: { type: "private", gate: "invite", encrypted: true, entry_fee_cents: 0 } }] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [report] });
    mocked.labThreads.mockRejectedValue(new Error("403 /labs/emporia-core/threads"));
    render(<LabsView />);
    expect((await screen.findAllByText(/threads are for its members/)).length).toBeGreaterThan(0);
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

  it("opens the lab's room from the lab card, and from the room block", async () => {
    const navigate = vi.fn();
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    render(<LabsView navigate={navigate} />);
    const head = (await screen.findByText("REPO VERIFIED")).closest(".e-hcard")!;
    fireEvent.click(head);
    expect(navigate).toHaveBeenCalledWith("rooms", { roomId: "r_lab" });
    fireEvent.click(await screen.findByRole("button", { name: "open the room" }));
    expect(navigate).toHaveBeenCalledTimes(2);
    // the repo link inside the card does not open the room
    fireEvent.click(screen.getAllByRole("link", { name: "acme/emporia" })[0]);
    expect(navigate).toHaveBeenCalledTimes(2);
  });

  it("shows the room's message count and its latest general messages, not the PR threads", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    mocked.roomMessages.mockResolvedValue({ count: 3, messages: [
      { message_id: "m1", sender_id: "beta", content: "welcome all", created_at: "2026-11-01T00:00:00Z" },
      { message_id: "m2", sender_id: "gamma", content: "thread talk", created_at: "2026-11-01T00:01:00Z", reply_to: "gh_pr:acme/emporia#12" },
      { message_id: "m3", sender_id: "alpha", content: "who takes the parser?", created_at: "2026-11-01T00:02:00Z" },
    ] });
    mocked.rooms.mockResolvedValue({ count: 2, rooms: [
      { room_id: "r_other", name: "x", room_type: "public", message_count: 9 },
      { room_id: "r_lab", name: "lab", room_type: "public", message_count: 412 },
    ] });
    render(<LabsView />);
    expect(await screen.findByText(/who takes the parser\?/)).toBeTruthy();
    // the room's own count, not the three messages on the page
    expect(await screen.findByText(/^412 messages$/)).toBeTruthy();
    expect(screen.getByText(/welcome all/)).toBeTruthy();
    expect(screen.queryByText(/thread talk/)).toBeNull();
    expect(mocked.roomMessages).toHaveBeenCalledWith("r_lab");
  });

  it("previews room messages as their words: a v2 body is text, a sealed one is 'sealed', never JSON", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    const env = (o: object) => JSON.stringify({ kind: "note", label: "", quote_of: null, reply_to: null, tags: [], ...o });
    mocked.roomMessages.mockResolvedValue({ count: 3, messages: [
      { message_id: "m1", sender_id: "beta", content: env({ text: "Scout: first note in the lab room." }), created_at: "2026-11-01T00:00:00Z" },
      { message_id: "m2", sender_id: "gamma", content: JSON.stringify({ v: 2, alg: "x", keys: { a: "b" }, ct: "AA" }), created_at: "2026-11-01T00:01:00Z" },
    ] });
    render(<LabsView />);
    expect(await screen.findByText(/Scout: first note in the lab room\./)).toBeTruthy();
    expect(screen.getByText(/: sealed$/)).toBeTruthy();
    expect(document.querySelector(".e-lab__room-msgs")?.textContent).not.toContain('"kind"');
  });

  it("says 'latest N' when the relay gives no count for the room", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    mocked.roomMessages.mockResolvedValue({ count: 3, messages: [
      { message_id: "m1", sender_id: "beta", content: "welcome all", created_at: "2026-11-01T00:00:00Z" },
    ] });
    mocked.rooms.mockResolvedValue({ count: 1, rooms: [{ room_id: "r_lab", name: "lab", room_type: "public" }] });
    render(<LabsView />);
    expect(await screen.findByText(/^latest 1 message$/)).toBeTruthy();
    expect(screen.queryByText(/^1 message$/)).toBeNull();
  });

  it("does not read an encrypted lab room", async () => {
    mocked.labs.mockResolvedValue({ labs: [{ ...lab, room: { ...lab.room, encrypted: true } }] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    render(<LabsView />);
    expect(await screen.findByText(/This lab's room is encrypted/)).toBeTruthy();
    expect(mocked.roomMessages).not.toHaveBeenCalled();
  });

  it("shows an empty room with no messages and a way in", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    render(<LabsView />);
    expect(await screen.findByText("No general messages yet.")).toBeTruthy();
    expect(screen.getByText(/^latest 0 messages$/)).toBeTruthy();
  });

  it("does not read a private lab's room, and keeps its private-room line", async () => {
    mocked.labs.mockResolvedValue({ labs: [{ ...lab, room: { type: "private", gate: "invite", encrypted: true, entry_fee_cents: 0 } }] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [report] });
    mocked.labThreads.mockRejectedValue(new Error("403 /labs/emporia-core/threads"));
    render(<LabsView />);
    expect(await screen.findByText(/This lab's room is private/)).toBeTruthy();
    expect(mocked.roomMessages).not.toHaveBeenCalled();
  });

  it("the empty WORK state names the lab's real repo and links to the room", async () => {
    mocked.labs.mockResolvedValue({ labs: [lab] });
    mocked.labReports.mockResolvedValue({ lab: "emporia-core", reports: [] });
    mocked.labThreads.mockResolvedValue({ lab: "emporia-core", threads: [] });
    render(<LabsView />);
    expect(await screen.findByText(/gh_pr:acme\/emporia#N or gh_issue:acme\/emporia#N/)).toBeTruthy();
    expect(screen.queryByText(/OWNER\/REPO/)).toBeNull();
    expect(screen.getByRole("button", { name: "Open the room" })).toBeTruthy();
  });

  it("labsFormat: general messages skip threads and removed ones; the how-to uses the repo", () => {
    const m = (id: string, extra = {}) => ({ message_id: id, sender_id: "a", content: id, created_at: "", ...extra });
    expect(generalMessages([m("a"), m("b", { root: "gh_issue:x/y#1" }), m("c", { deleted_at: "t" }), m("d")], 5).map((x) => x.message_id)).toEqual(["a", "d"]);
    expect(threadHowTo({ subject: { ref: "x/y" }, repo_url: "" })).toContain("gh_pr:x/y#N");
  });
});
