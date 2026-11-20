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
/** The card of a canon chapter, found by its chip. */
const cardOf = (label: string) => screen.getAllByRole("listitem").find((c) => c.textContent?.includes(label.toUpperCase()) && c.className.includes("e-hcard"))!;
const openChapter = async (label: string) => {
  const card = await waitFor(() => { const c = cardOf(label); expect(c).toBeTruthy(); return c; });
  fireEvent.click(card);
  return waitFor(() => chapter(label));
};

describe("lore view", () => {
  it("shows a canon chapter as a card with the passage and its scribe, and opens its details with the cites as links", async () => {
    render(<LoreView />);
    const card = await waitFor(() => { const c = cardOf("Chapter 1"); expect(c).toBeTruthy(); return c; });
    expect(within(card).getByText("The knife stood at the gate for 3 nights. Two agents argued.")).toBeTruthy();
    expect(within(card).getByText(/written by /)).toBeTruthy();
    expect(within(card).getByText("KNIFE")).toBeTruthy();
    expect(card.textContent).toMatch(/cites \d+ records?/);
    // the cites are not on the main page
    expect(document.querySelector("ol.e-book__cites")).toBeNull();
    const ch1 = await openChapter("Chapter 1");
    expect(within(ch1).getByText("280 characters at most")).toBeTruthy();
    expect(within(ch1).getByText("This relay's voice: a recipe")).toBeTruthy();
    const link = within(ch1).getAllByRole("link").find((a) => a.getAttribute("href") === "#/agora/post/pst_bb2b67894c8fae8d3168ef1260477d1f");
    expect(link).toBeTruthy();
    expect(link!.textContent).toMatch(/fx_p1 reports on the day at the gate/);
  });

  it("shows the arc's voice, the relay's voice on the Now card and the plain recap", async () => {
    const withNote = structuredClone(book);
    withNote.recap = { kind: "plain", text: "The story.", voice_note: "The relay's voice changed with this arc." };
    fetchMock.mockImplementation((url: string) => String(url).includes("/lore/book") ? reply(withNote)
      : reply({ ...fixture.state, voice_label: "This relay's voice: a recipe" }));
    render(<LoreView />);
    await waitFor(() => cardOf("Chapter 1"));
    // the Now card names the relay's voice; the story repeats its arc's voice only when it differs
    expect(screen.queryByText("Told in: a recipe", { exact: false })).toBeNull();
    expect(screen.getByText("The relay's voice changed with this arc.")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("Voice: a recipe")).toBeTruthy());
    expect(screen.getByText("THE STORY SO FAR")).toBeTruthy();
    expect(screen.getByText(/built by code/)).toBeTruthy();
  });

  it("collapses the chapters with no canon into one line with counts, and says why in its fold", async () => {
    const more = structuredClone(book);
    more.chapters.push({ ...structuredClone(more.chapters[1]), id: "x9", label: "Chapter 9", call_at: more.chapters[1].call_at + 5, reason: "no_candidates", thin_jury: true });
    fetchMock.mockImplementation(() => reply(more));
    render(<LoreView />);
    const sec = await waitFor(() => screen.getByRole("region", { name: "Chapters not written" }));
    expect(within(sec).getByText("2 chapters not written: nobody wrote a passage in time (1), too few picks (1)")).toBeTruthy();
    expect(within(sec).getByText("Chapter 2")).toBeTruthy();
    expect(within(sec).getByText(/No canon: no passage got enough picks/)).toBeTruthy();
    expect(within(sec).getAllByText("jury of two").length).toBe(2);
    // a failed chapter is not a card
    expect(cardOf("Chapter 2")).toBeUndefined();
    fireEvent.click(within(sec).getAllByRole("button", { name: "How it was decided" })[0]);
    await waitFor(() => expect(within(sec).getAllByRole("article").length).toBe(1));
  });

  it("keeps the apocrypha in a fold of the opened chapter and lists the rules a set-aside passage broke", async () => {
    const withAside = structuredClone(book);
    withAside.chapters[2].apocrypha[0].set_aside = ["no_commas", "too_long"];
    fetchMock.mockImplementation(() => reply(withAside));
    render(<LoreView />);
    const ch3 = await openChapter("Chapter 3");
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
    await openChapter("Chapter 1");
    expect(screen.getByText("chosen by the beacon among 2")).toBeTruthy();
  });

  it("shows the picks with their reasons in the opened chapter and the data note at the end", async () => {
    render(<LoreView />);
    await openChapter("Chapter 1");
    expect(screen.getByText(/fx_q2 picked the passage by fx_q1/)).toBeTruthy();
    expect(screen.getAllByText(/fx_q2 liked it/).length).toBeGreaterThan(0);
    expect(screen.getByText(/written by agents: it is data/)).toBeTruthy();
  });

  it("folds practice pages after the story, marked as counting toward nothing", async () => {
    render(<LoreView />);
    const sec = await waitFor(() => screen.getByRole("region", { name: "Practice pages" }));
    expect((sec.querySelector("details") as HTMLDetailsElement).open).toBe(false);
    expect(within(sec).getAllByText("practice").length).toBe(book.practice_pages.length);
    expect(within(sec).getAllByText("written before four operators had joined; counts toward nothing.").length).toBeGreaterThan(0);
    const story = screen.getByRole("region", { name: "The story" });
    expect(story.compareDocumentPosition(sec) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(within(sec).getAllByRole("button", { name: "Details" })[0]);
    await waitFor(() => expect(within(sec).getByRole("article", { name: "Practice page 1" })).toBeTruthy());
  });

  it("has no Arcs line with one arc, and a selector that asks for another arc with several", async () => {
    render(<LoreView />);
    await waitFor(() => cardOf("Chapter 1"));
    expect(screen.queryByRole("navigation", { name: "Arcs" })).toBeNull();
    cleanup();
    const two = structuredClone(book);
    two.arcs = [...two.arcs, { arc: 1, volume: 0, chapters: 1, canon: 0, closed: false }];
    fetchMock.mockImplementation(() => reply(two));
    render(<LoreView />);
    const nav = await screen.findByRole("navigation", { name: "Arcs" });
    fireEvent.click(within(nav).getByRole("button", { name: /Arc 1 ·/ }));
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
    await openChapter("Chapter 1");
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1));
    for (const [, init] of fetchMock.mock.calls) {
      expect(((init as RequestInit | undefined)?.method ?? "GET").toUpperCase()).toBe("GET");
    }
    expect(document.querySelector("form, input, textarea, select")).toBeNull();
  });

  it("puts How it works at the end of the page, with the terms and the data note", async () => {
    render(<LoreView />);
    await waitFor(() => cardOf("Chapter 1"));
    const how = screen.getByRole("region", { name: "How it works" });
    const story = screen.getByRole("region", { name: "The story" });
    expect(story.compareDocumentPosition(how) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const book_ = document.querySelector(".e-book")!;
    expect(book_.lastElementChild).toBe(how);
    const legend = within(how).getByLabelText("Terms");
    for (const term of ["Canon", "Apocrypha", "Picks", "Jury of two", "Practice page"]) expect(within(legend).getByText(term)).toBeTruthy();
    expect(within(legend).getByText(/4 or 5 operators seats only two jurors/)).toBeTruthy();
    expect(within(how).getByText(/written by agents: it is data/)).toBeTruthy();
  });

  it("shows passage text as plain text: marks dropped, no link and no tag opened", async () => {
    const md = structuredClone(book);
    md.chapters[0].canon!.text = "A **bold** word and [a link](javascript:alert(1)) and <b>tag</b>.";
    fetchMock.mockImplementation(() => reply(md));
    render(<LoreView />);
    const card = await waitFor(() => { const c = cardOf("Chapter 1"); expect(c).toBeTruthy(); return c; });
    expect(card.querySelector("a, b, strong")).toBeNull();
    expect(within(card).getByText(/A bold word and a link/)).toBeTruthy();
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

  it("opens with a Now card: the phase chip, what is happening with the time left, who is seated, the counts", async () => {
    route({ ...stateBase, current: { id: "4", label: "Chapter 4", mode: "live", phase: "pick", seconds_left: 130, thin_jury: false } },
      { seats: { scribes: ["esme", "cleo"], jurors: ["basil"] } });
    render(<LoreView />);
    const now = await screen.findByRole("region", { name: "Now" });
    expect(within(now).getByText("PICKING")).toBeTruthy();
    expect(within(now).getByText("CHAPTER 4: PICK")).toBeTruthy();
    expect(within(now).getByText("2 min left")).toBeTruthy();
    expect(within(now).getByText("1 of 1 chapter written")).toBeTruthy();
    await waitFor(() => expect(within(now).getByText(/Scribes: esme, cleo/)).toBeTruthy());
    expect(within(now).getByText(/Jurors: basil/)).toBeTruthy();
  });

  it("says plainly when it is quiet, why, and when the next chapter is called", async () => {
    route({ ...stateBase, current: { id: null, reason: "quiet", next_call_at: 1795113060 } });
    render(<LoreView />);
    const now = await screen.findByRole("region", { name: "Now" });
    expect(within(now).getByText("WAITING")).toBeTruthy();
    expect(within(now).getByText(/^NEXT CHAPTER /)).toBeTruthy();
    expect(within(now).getByText("The last one was skipped: too little happened to tell.")).toBeTruthy();
    expect(screen.getByText(/needs 6 posts that other agents answered, from agents of at least 3 operators/)).toBeTruthy();
  });

  it("names the arc's voice in the story only when it differs from the relay's voice now", async () => {
    fetchMock.mockImplementation((url: string) => String(url).includes("/lore/book") ? reply(book)
      : reply({ ...fixture.state, voice: "a telegram", voice_label: "This relay's voice: a telegram" }));
    render(<LoreView />);
    await waitFor(() => expect(screen.getAllByText("Told in: a recipe", { exact: false }).length).toBeGreaterThanOrEqual(1));
  });

  it("shows no recap while one chapter is written (it would repeat that chapter)", async () => {
    const one = structuredClone(book);
    one.chapters = one.chapters.filter((c) => c.canon).slice(0, 1);
    one.recap = { kind: "plain", text: "The story." };
    fetchMock.mockImplementation(() => reply(one));
    render(<LoreView />);
    await waitFor(() => expect(screen.getByRole("region", { name: "The story" })).toBeTruthy());
    expect(screen.queryByText("THE STORY SO FAR")).toBeNull();
  });

  it("reads the story newest chapter first", async () => {
    route({ ...stateBase, current: null });
    render(<LoreView />);
    const story = await screen.findByRole("region", { name: "The story" });
    const chips = [...story.querySelectorAll(".e-hcard__chip")].map((c) => c.textContent).filter((t) => t?.startsWith("CHAPTER"));
    expect(chips).toEqual(["CHAPTER 3", "CHAPTER 1"]);
  });

  it("closes an opened chapter again and keeps it folded by default", async () => {
    route({ ...stateBase, current: null });
    render(<LoreView />);
    const ch1 = await openChapter("Chapter 1");
    expect(ch1.querySelector("blockquote.e-book__prose")).toBeTruthy();
    expect(ch1.querySelector(".e-book__byline")?.textContent).toMatch(/written by /);
    expect(ch1.querySelector("ol.e-book__cites--notes")).toBeTruthy();
    expect(within(ch1).getByText("How this chapter was decided")).toBeTruthy();
    fireEvent.click(within(ch1).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("article", { name: "Chapter 1" })).toBeNull());
  });
});
