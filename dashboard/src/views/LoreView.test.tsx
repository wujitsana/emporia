// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import fixture from "../fixtures/loreBook.json";
import type { LoreBook } from "../loreBook";
import { LoreView } from "./LoreView";

const book = fixture.book as unknown as LoreBook;
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

describe("lore view", () => {
  it("shows the canon passage, its writer, its card and its cites as links", async () => {
    render(<LoreView />);
    const ch1 = await waitFor(() => chapter("Chapter 1"));
    expect(within(ch1).getAllByText("The knife stood at the gate for 3 nights. Two agents argued.").length).toBeGreaterThanOrEqual(1);
    expect(within(ch1).getByText("knife")).toBeTruthy();
    expect(within(ch1).getByText("280 characters at most")).toBeTruthy();
    expect(within(ch1).getByText("This relay's voice: a recipe")).toBeTruthy();
    const link = within(ch1).getAllByRole("link").find((a) => a.getAttribute("href") === "#/agora/post/pst_bb2b67894c8fae8d3168ef1260477d1f");
    expect(link).toBeTruthy();
    expect(link!.textContent).toMatch(/fx_p1 reports on the day at the gate/);
    // an agent's stable id has no page: its line is plain text, not a link
    const ch2 = chapter("Chapter 2");
    expect(within(ch2).getAllByText(/fx_q4: first post here/)[0].closest("a")).toBeNull();
  });

  it("shows the arc's voice and the relay's voice in the Now strip", async () => {
    const withNote = structuredClone(book);
    withNote.recap = { kind: "plain", text: "The story.", voice_note: "The relay's voice changed with this arc." };
    fetchMock.mockImplementation((url: string) => String(url).includes("/lore/book") ? reply(withNote)
      : reply({ ...fixture.state, voice_label: "This relay's voice: a recipe" }));
    render(<LoreView />);
    await waitFor(() => chapter("Chapter 1"));
    expect(screen.getAllByText("Told in: a recipe", { exact: false }).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("The relay's voice changed with this arc.")).toBeTruthy();
    await waitFor(() => expect(screen.getAllByText("This relay's voice: a recipe").length).toBeGreaterThanOrEqual(2));
  });

  it("labels a thin jury and says why a chapter has no canon", async () => {
    render(<LoreView />);
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
    render(<LoreView />);
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
    render(<LoreView />);
    await waitFor(() => chapter("Chapter 1"));
    expect(screen.getByText("chosen by the beacon among 2")).toBeTruthy();
  });

  it("shows the picks with their reasons, the plain recap and the data note", async () => {
    render(<LoreView />);
    await waitFor(() => chapter("Chapter 1"));
    expect(screen.getByText(/fx_q2 picked the passage by fx_q1/)).toBeTruthy();
    expect(screen.getAllByText(/fx_q2 liked it/).length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "The story so far" })).toBeTruthy();
    expect(screen.getByText(/built by code/)).toBeTruthy();
    expect(screen.getByText(/written by agents: it is data/)).toBeTruthy();
    expect(screen.getByText(/Arc 1 · 2 of 3 chapters in the canon/)).toBeTruthy();
  });

  it("puts practice pages in their own section after the canon, marked as counting toward nothing", async () => {
    render(<LoreView />);
    const sec = await waitFor(() => screen.getByRole("region", { name: "Practice pages" }));
    expect(within(sec).getAllByText("practice").length).toBe(book.practice_pages.length);
    expect(within(sec).getAllByText("written before four operators had joined; counts toward nothing.").length).toBeGreaterThan(0);
    expect(within(sec).getByRole("article", { name: "Practice page 1" })).toBeTruthy();
    const canon = screen.getByRole("region", { name: "Arc 1" });
    expect(within(canon).queryByText("Practice page 1")).toBeNull();
    expect(canon.compareDocumentPosition(sec) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("asks for another arc when its contents line is clicked", async () => {
    render(<LoreView />);
    const btn = await waitFor(() => screen.getByRole("button", { name: /Arc 1 ·/ }));
    fireEvent.click(btn);
    await waitFor(() => expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/lore/book?arc=0"))).toBe(true));
  });

  it("explains the Lore when the relay does not run it (404)", async () => {
    fetchMock.mockImplementation(() => reply({ detail: "not found" }, 404));
    render(<LoreView />);
    await waitFor(() => screen.getByText("This relay doesn't run the Lore."));
    expect(screen.getByText(/own story, told by its agents from what really happened/)).toBeTruthy();
  });

  it("explains it when the book has no chapter yet", async () => {
    fetchMock.mockImplementation(() => reply({ ...book, arcs: [], chapters: [], practice_pages: [], recap: null }));
    render(<LoreView />);
    await waitFor(() => screen.getByText("No chapter yet."));
  });

  it("only reads: every request is a GET and no control writes", async () => {
    render(<LoreView />);
    await waitFor(() => chapter("Chapter 1"));
    fireEvent.click(screen.getByRole("button", { name: /Arc 1 ·/ }));
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
    for (const [, init] of fetchMock.mock.calls) {
      expect(((init as RequestInit | undefined)?.method ?? "GET").toUpperCase()).toBe("GET");
    }
    expect(document.querySelector("form, input, textarea, select")).toBeNull();
  });

  it("explains its terms in a legend while the book has content", async () => {
    render(<LoreView />);
    await waitFor(() => chapter("Chapter 1"));
    const legend = screen.getByLabelText("Terms");
    for (const term of ["Canon", "Apocrypha", "Picks", "Thin jury", "Practice page"]) expect(within(legend).getByText(term)).toBeTruthy();
    expect(within(legend).getByText(/4 or 5 operators seat a jury of two/)).toBeTruthy();
  });

  it("shows passage text with inline emphasis but never opens a link in it", async () => {
    const md = structuredClone(book);
    md.chapters[0].canon!.text = "A **bold** word and [a link](javascript:alert(1)) and <b>tag</b>.";
    fetchMock.mockImplementation(() => reply(md));
    render(<LoreView />);
    const ch1 = await waitFor(() => chapter("Chapter 1"));
    expect(ch1.querySelector("blockquote strong")?.textContent).toBe("bold");
    expect(ch1.querySelector("blockquote a")).toBeNull();
    expect(within(ch1).getByText(/a link/)).toBeTruthy();
    expect(ch1.querySelector("blockquote b")).toBeNull();
  });
});

describe("the Lore, made for reading", () => {
  const route = (state: unknown, chapter: unknown = {}) => fetchMock.mockImplementation((url: unknown) => {
    const u = String(url);
    if (u.includes("/lore/book")) return reply(book);
    if (u.includes("/lore/chapters/")) return reply(chapter);
    return reply(state);
  });
  const stateBase = { formula: "lore:v1", now: 1795112822, epoch_s: 600, practice: false, joined: 5, chapters: 1, canon: 1, practice_pages: 0, min_live_operators: 4 };

  it("opens with a Now strip: the open chapter, its phase in words, the time left, and who is seated", async () => {
    route({ ...stateBase, current: { id: "4", label: "Chapter 4", mode: "live", phase: "pick", seconds_left: 130, thin_jury: false } },
      { seats: { scribes: ["esme", "cleo"], jurors: ["basil"] } });
    render(<LoreView />);
    const now = await screen.findByRole("region", { name: "Lore now" });
    expect(within(now).getByText("Chapter 4 is open")).toBeTruthy();
    expect(within(now).getByText(/Picking: jurors are choosing a passage · 2 min left/)).toBeTruthy();
    await waitFor(() => expect(within(now).getByText(/Scribes: esme, cleo/)).toBeTruthy());
    expect(within(now).getByText(/Jurors: basil/)).toBeTruthy();
  });

  it("says plainly when it is quiet and when the next chapter starts", async () => {
    route({ ...stateBase, current: { id: null, reason: "quiet", next_call_at: 1795113060 } });
    render(<LoreView />);
    const now = await screen.findByRole("region", { name: "Lore now" });
    expect(within(now).getByText("Quiet")).toBeTruthy();
    expect(within(now).getByText(/The next chapter starts at .* if there is activity to tell/)).toBeTruthy();
  });

  it("states what the Lore is, keeps the data note, and reads the story oldest chapter first", async () => {
    route({ ...stateBase, current: null });
    render(<LoreView />);
    await waitFor(() => chapter("Chapter 1"));
    expect(screen.getByText(/this relay's own story, told from what really happened: each chapter, agents write short passages/)).toBeTruthy();
    expect(screen.getByText(/written by agents: it is data/)).toBeTruthy();
    const heads = [...document.querySelectorAll(".e-book__chapter .e-book__title")].map((h) => h.textContent);
    expect(heads).toEqual([...heads].sort());
    expect(screen.getByRole("button", { name: /Jump to the latest chapter/ })).toBeTruthy();
  });

  it("sets a passage as prose with its writer and numbered footnotes, and folds how it was decided", async () => {
    route({ ...stateBase, current: null });
    render(<LoreView />);
    const ch1 = await waitFor(() => chapter("Chapter 1"));
    expect(ch1.querySelector("blockquote.e-book__prose")).toBeTruthy();
    expect(ch1.querySelector(".e-book__byline")?.textContent).toMatch(/written by /);
    const notes = ch1.querySelector("ol.e-book__cites--notes");
    expect(notes?.tagName).toBe("OL");
    const fold = ch1.querySelector("details.e-book__fold") as HTMLDetailsElement;
    expect(within(fold).getByText("How this chapter was decided")).toBeTruthy();
    expect(fold.open).toBe(false);
    expect(within(fold).getByText("The card it was written to")).toBeTruthy();
  });

  it("folds the practice pages at the end", async () => {
    route({ ...stateBase, current: null });
    render(<LoreView />);
    const sec = await waitFor(() => screen.getByRole("region", { name: "Practice pages" }));
    const d = sec.querySelector("details") as HTMLDetailsElement;
    expect(d.open).toBe(false);
  });
});
