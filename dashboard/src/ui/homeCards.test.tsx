// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { attentionCount, titleFor } from "../attentionTitle";
import { Card } from "./HomeCards";
import { mergeCards, type CardSpec } from "./homeCardsModel";

afterEach(cleanup);

const turn = (sid: string, label: string): CardSpec => ({
  key: sid, meta: "game", head: "Your turn", caption: "alpha has a move waiting", merge: "turn", sid, label,
});

describe("merging a row's cards", () => {
  it("makes the same game twice one card", () => {
    const merged = mergeCards([turn("s1", "chess with beta"), turn("s1", "chess with beta")]);
    expect(merged).toHaveLength(1);
    expect(merged[0].head).toBe("Your turn");
  });

  it("makes several games of one kind one card, 'x2', naming the games", () => {
    const merged = mergeCards([turn("s1", "chess with beta"), turn("s2", "bargain with gamma"), { key: "x", meta: "m", head: "Other", caption: "c" }]);
    expect(merged.map((c) => c.head)).toEqual(["Your turn ×2", "Other"]);
    expect(merged[0].caption).toBe("chess with beta · bargain with gamma");
  });

  it("never touches a card that does not ask to merge", () => {
    const plain = { key: "a", meta: "m", head: "46 new posts", caption: "Ideas" };
    expect(mergeCards([plain, { ...plain, key: "b" }])).toHaveLength(2);
  });
});

describe("a post by an agent", () => {
  const hostile = [
    "# A heading", "", "Some **bold** and `code`.", "",
    "<script>window.__pwned = 1</script>", "<img src=x onerror=\"window.__pwned = 2\">", "",
    "[click me](javascript:window.__pwned=3) and [fine](https://example.org/a)", "",
    "![tracker](https://example.org/pixel.png)", "", "```", "**not bold**", "```",
  ].join("\n");
  const card = { key: "p", meta: "post", head: "Idea", caption: "bob", body: hostile, agent: "bob", theirs: true } as CardSpec;

  it("shows the preview without Markdown marks", () => {
    render(<Card c={card} />);
    const t = document.querySelector(".e-hcard__body")!.textContent!;
    expect(t).toContain("A heading");
    expect(t).not.toContain("# A heading");
    expect(t).not.toContain("**bold**");
  });

  it("opens as Markdown that is inert: no script, no image, no javascript: link", () => {
    render(<Card c={{ ...card, full: true }} />);
    const body = document.querySelector(".e-hcard__body")!;
    expect(body.querySelector("h2")?.textContent).toBe("A heading");
    expect(body.querySelector("strong")?.textContent).toBe("bold");
    expect(body.querySelector("script, img, iframe, object")).toBeNull();
    expect(body.textContent).toContain("<script>");           // shown as text, never run
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    const hrefs = [...body.querySelectorAll("a")].map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(["https://example.org/a", "https://example.org/pixel.png"]);
    expect(body.querySelector('a[href^="javascript"]')).toBeNull();
    expect(body.querySelector("a")?.getAttribute("rel")).toContain("noopener");
    expect(body.querySelector("pre code")?.textContent).toBe("**not bold**");
  });
});

describe("a card", () => {
  it("shows a post's first lines, then 'more' that opens it in place", () => {
    const text = Array.from({ length: 14 }, (_, i) => `line ${i + 1}`).join("\n");
    render(<Card c={{ key: "p", meta: "post", head: "New idea", caption: "bob", body: text, agent: "bob" }} />);
    const body = document.querySelector(".e-hcard__body")!;
    expect(body.textContent).toBe(text);
    expect(body.className).not.toContain("is-open");
    fireEvent.click(screen.getByRole("button", { name: "more" }));
    expect(document.querySelector(".e-hcard__body")!.className).toContain("is-open");
    expect(document.querySelector(".e-moire-avatar")).toBeTruthy();
  });

  it("shows a short post without 'more', the time as 'ago' with the exact time on hover", () => {
    render(<Card c={{ key: "p", meta: "post", head: "New idea", caption: "bob", body: "short", at: new Date(Date.now() - 3 * 60_000).toISOString() }} />);
    expect(screen.queryByRole("button", { name: "more" })).toBeNull();
    const t = document.querySelector("time")!;
    expect(t.textContent).toBe("3m ago");
    expect(t.getAttribute("title")).toBeTruthy();
  });
});

describe("each fact once on a card", () => {
  it("drops a chip that only repeats the headline, and a kind line that only repeats the chip", () => {
    const { unmount } = render(<Card c={{ key: "a", meta: "topic", chip: "TOPIC", head: "Games", caption: "carol · 1 new" }} />);
    expect(document.querySelector(".e-hcard__hud span")?.textContent).toBe("");          // "topic" would say the chip again
    expect(document.querySelector(".e-hcard__chip")?.textContent).toBe("TOPIC");
    unmount();
    render(<Card c={{ key: "b", meta: "", chip: "NEW", head: "New agent", caption: "erin" }} />);
    expect(document.querySelector(".e-hcard__chip--none")).toBeTruthy();                    // NEW is in "New agent"
    expect(document.querySelector(".e-hcard__chip:not(.e-hcard__chip--none)")).toBeNull();
  });

  it("keeps a game's type on the top line when the headline is the event", () => {
    render(<Card c={{ key: "g", meta: "chess", chip: "YOUR MOVE", head: "Your turn", caption: "active" }} />);
    expect(document.querySelector(".e-hcard__hud span")?.textContent).toBe("chess");
  });
});

describe("the tab title", () => {
  it("counts turns, deals and unread, and is plain when nothing waits", () => {
    expect(attentionCount([{ kind: "move" }, { kind: "deal" }, { kind: "quest" }, { kind: "message" }], 1)).toBe(3);
    expect(titleFor(2)).toBe("(2) Emporia");
    expect(titleFor(0)).toBe("Emporia");
  });
});
