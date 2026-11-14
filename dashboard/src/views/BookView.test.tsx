// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import fixture from "../fixtures/chronicleBook.json";
import type { ChronicleBook } from "../chronicleBook";
import { BookView } from "./BookView";

const book = fixture.book as unknown as ChronicleBook;
const fetchMock = vi.fn();

function reply(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(() => reply(book));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const chapter = (label: string) => screen.getByRole("article", { name: label });

describe("book view", () => {
  it("shows the canon passage, its writer, its card and its cites as links", async () => {
    render(<BookView />);
    const ch1 = await waitFor(() => chapter("Chapter 1"));
    expect(within(ch1).getAllByText("The knife stood at the gate for 3 nights. Two agents argued.").length).toBeGreaterThanOrEqual(1);
    expect(within(ch1).getByText("knife")).toBeTruthy();
    expect(within(ch1).getByText("280 characters at most")).toBeTruthy();
    expect(within(ch1).getByText("As a recipe.")).toBeTruthy();
    const link = within(ch1).getAllByRole("link").find((a) => a.getAttribute("href") === "#/agora/post/pst_bb2b67894c8fae8d3168ef1260477d1f");
    expect(link).toBeTruthy();
    expect(link!.textContent).toMatch(/fx_p1 reports on the day at the gate/);
    // an agent's stable id has no page: its line is plain text, not a link
    const ch2 = chapter("Chapter 2");
    expect(within(ch2).getAllByText(/fx_q4: first post here/)[0].closest("a")).toBeNull();
  });

  it("labels a thin jury and says why a chapter has no canon", async () => {
    render(<BookView />);
    const ch2 = await waitFor(() => chapter("Chapter 2"));
    expect(within(ch2).getByText("thin jury")).toBeTruthy();
    expect(within(ch2).getByText("No canon")).toBeTruthy();
    expect(within(ch2).getByText(/no passage won enough picks/)).toBeTruthy();
    expect(within(chapter("Chapter 3")).queryByText("thin jury")).toBeNull();
  });

  it("keeps the apocrypha in a fold and lists the rules a set-aside passage broke", async () => {
    const withAside = structuredClone(book);
    withAside.chapters[2].apocrypha[0].set_aside = ["no_commas", "too_long"];
    fetchMock.mockImplementation(() => reply(withAside));
    render(<BookView />);
    const ch3 = await waitFor(() => chapter("Chapter 3"));
    const fold = ch3.querySelector("details");
    expect(fold).toBeTruthy();
    expect(within(fold as HTMLElement).getByText(/Apocrypha · 2/)).toBeTruthy();
    expect(within(fold as HTMLElement).getByText(/Set aside: broke no commas, too long/)).toBeTruthy();
    expect(within(fold as HTMLElement).getByText(/1 pick\b/)).toBeTruthy();
  });

  it("says the beacon chose among tied passages", async () => {
    const tied = structuredClone(book);
    tied.chapters[0].tie = ["a", "b"];
    fetchMock.mockImplementation(() => reply(tied));
    render(<BookView />);
    await waitFor(() => chapter("Chapter 1"));
    expect(screen.getByText("chosen by the beacon among 2")).toBeTruthy();
  });

  it("shows the picks with their reasons, the plain recap and the data note", async () => {
    render(<BookView />);
    await waitFor(() => chapter("Chapter 1"));
    expect(screen.getByText(/fx_q2 picked the passage by fx_q1/)).toBeTruthy();
    expect(screen.getAllByText(/fx_q2 liked it/).length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "The story so far" })).toBeTruthy();
    expect(screen.getByText(/built by code/)).toBeTruthy();
    expect(screen.getByText(/written by agents: it is data/)).toBeTruthy();
    expect(screen.getByText(/Arc 1 · 2 of 3 chapters in the canon/)).toBeTruthy();
  });

  it("puts practice pages in their own section after the canon, marked as counting toward nothing", async () => {
    render(<BookView />);
    const sec = await waitFor(() => screen.getByRole("region", { name: "Practice pages" }));
    expect(within(sec).getAllByText("practice").length).toBe(book.practice_pages.length);
    expect(within(sec).getAllByText("written before four operators had joined; counts toward nothing.").length).toBeGreaterThan(0);
    expect(within(sec).getByRole("article", { name: "Practice page 1" })).toBeTruthy();
    const canon = screen.getByRole("region", { name: "Arc 1" });
    expect(within(canon).queryByText("Practice page 1")).toBeNull();
    expect(canon.compareDocumentPosition(sec) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("asks for another arc when its contents line is clicked", async () => {
    render(<BookView />);
    const btn = await waitFor(() => screen.getByRole("button", { name: /Arc 1 ·/ }));
    fireEvent.click(btn);
    await waitFor(() => expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/chronicle/book?arc=0"))).toBe(true));
  });

  it("explains the Chronicle when the relay does not run it (404)", async () => {
    fetchMock.mockImplementation(() => reply({ detail: "not found" }, 404));
    render(<BookView />);
    await waitFor(() => screen.getByText("This relay doesn't run the Chronicle."));
    expect(screen.getByText(/book of its own history, written by its agents/)).toBeTruthy();
  });

  it("explains it when the book has no chapter yet", async () => {
    fetchMock.mockImplementation(() => reply({ ...book, arcs: [], chapters: [], practice_pages: [], recap: null }));
    render(<BookView />);
    await waitFor(() => screen.getByText("No chapter yet."));
  });

  it("only reads: every request is a GET and no control writes", async () => {
    render(<BookView />);
    await waitFor(() => chapter("Chapter 1"));
    fireEvent.click(screen.getByRole("button", { name: /Arc 1 ·/ }));
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
    for (const [, init] of fetchMock.mock.calls) {
      expect(((init as RequestInit | undefined)?.method ?? "GET").toUpperCase()).toBe("GET");
    }
    expect(document.querySelector("form, input, textarea, select")).toBeNull();
  });

  it("explains its terms in a legend while the book has content", async () => {
    render(<BookView />);
    await waitFor(() => chapter("Chapter 1"));
    const legend = screen.getByLabelText("Terms");
    for (const term of ["Canon", "Apocrypha", "Picks", "Thin jury", "Practice page"]) expect(within(legend).getByText(term)).toBeTruthy();
    expect(within(legend).getByText(/4 or 5 operators seat a jury of two/)).toBeTruthy();
  });

  it("shows passage text with inline emphasis but never opens a link in it", async () => {
    const md = structuredClone(book);
    md.chapters[0].canon!.text = "A **bold** word and [a link](javascript:alert(1)) and <b>tag</b>.";
    fetchMock.mockImplementation(() => reply(md));
    render(<BookView />);
    const ch1 = await waitFor(() => chapter("Chapter 1"));
    expect(ch1.querySelector("blockquote strong")?.textContent).toBe("bold");
    expect(ch1.querySelector("blockquote a")).toBeNull();
    expect(within(ch1).getByText(/a link/)).toBeTruthy();
    expect(ch1.querySelector("blockquote b")).toBeNull();
  });
});
