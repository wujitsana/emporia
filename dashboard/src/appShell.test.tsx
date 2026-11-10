// @vitest-environment jsdom
/**
 * The whole app, offline (every request refused, no socket ever opens), at a
 * phone's width: what a route to a hidden section shows, and the phone menu's
 * focus (review pass 2, docs/review-pass-2026-11-06-ui.md).
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentType } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

// The whole app is a large import: once, not per test (under a full run it took over 5 s).
let App: ComponentType;
beforeAll(async () => {
  App = (await import("./App")).default;
}, 60_000);

class NoSocket {
  static OPEN = 1;
  readyState = 3;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: (() => void) | null = null;
  constructor() {
    setTimeout(() => this.onclose?.(), 0);
  }
  send() {}
  close() {}
}

function narrowWindow(narrow: boolean) {
  const listeners = new Set<() => void>();
  const mq = {
    get matches() { return narrow; },
    media: "(max-width: 900px)",
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  window.matchMedia = ((q: string) => (q.includes("900px") ? mq : { matches: false, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
  return (next: boolean) => {
    narrow = next;
    listeners.forEach((fn) => fn());
  };
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline in this test"))));
  vi.stubGlobal("WebSocket", NoSocket);
  Element.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.location.hash = "";
});

async function renderAt(hash: string) {
  window.location.hash = hash;
  render(<App />);
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

describe("a route to a section this viewer may not see", () => {
  it("says the operator's sections are the operator's, not a heading over nothing", async () => {
    narrowWindow(false);
    await renderAt("#/more/operator");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toMatch(/Operator|[A-Z#!0-9]/);
    expect(screen.getByText("This section is for the relay's operator.")).toBeTruthy();
  });

  it("says a spectator's Fees and Messages are an agent's own", async () => {
    narrowWindow(false);
    await renderAt("#/talk/messages");   // the earlier route: it opens Agent › Messages
    expect(screen.getByText("This section is your agent's own.")).toBeTruthy();
  });

  it("shows a section the viewer may see as before", async () => {
    narrowWindow(false);
    await renderAt("#/labs/season");
    expect(screen.queryByText(/This section is/)).toBeNull();
    expect(await screen.findByText(/the relay did not answer|asking the relay/)).toBeTruthy();
  });

  it("says in one line why Deals is not here when the relay cannot sell services, and draws it nowhere", async () => {
    narrowWindow(false);
    await renderAt("#/agent/deals");
    expect(screen.getByText("Deals are not switched on for this relay.")).toBeTruthy();
    // The row is never left with nothing selected: the hidden page is drawn, dimmed, as the active tab.
    const here = screen.getAllByRole("button", { name: /^Deals/ }).filter((b) => b.classList.contains("e-subnav__tab"));
    expect(here).toHaveLength(1);
    expect(here[0].classList.contains("is-off")).toBe(true);
    expect(here[0].getAttribute("aria-current")).toBe("page");
  });

  it("keeps the sub-row the same on every place, so the page never jumps under it", async () => {
    narrowWindow(false);
    for (const h of ["#/home", "#/arena", "#/agora/feed", "#/labs/season", "#/help"]) {
      await renderAt(h);
      expect(document.querySelectorAll(".e-subnav")).toHaveLength(1);
      expect(document.querySelector(".e-subnav__dest")).toBeNull();
      cleanup();
    }
  });

  it("opens a bookmark from the earlier layout where its content lives now", async () => {
    narrowWindow(false);
    await renderAt("#/market/offers");
    expect(document.querySelector(".e-page-head__title")).toBeTruthy();
    expect(screen.queryByText(/This section is/)).toBeNull();
    // Rooms now: it names the place it sits in, Agora, in the strip.
    expect(screen.getByRole("navigation", { name: "Agora: sections" })).toBeTruthy();
  });

  it("opens Labs with its empty state when the relay answers nothing", async () => {
    narrowWindow(false);
    await renderAt("#/labs");
    expect(screen.getByRole("navigation", { name: "Labs & Season: sections" })).toBeTruthy();
  });

  it("is not offered on Home: a spectator's Home has no Operator or Messages card", async () => {
    narrowWindow(false);
    await renderAt("#/home");
    const cards = [...document.querySelectorAll(".e-overview-card__title")].map((e) => e.textContent);
    expect(cards).toContain("Arena");
    expect(cards).not.toContain("Operator");
    expect(cards).not.toContain("Messages");
  });
});

describe("the phone menu", () => {
  it("takes the focus, makes the view behind it inert, and gives the focus back on Escape", async () => {
    narrowWindow(true);
    await renderAt("#/home");
    const opener = screen.getByRole("button", { name: "Open menu" });
    opener.focus();
    fireEvent.click(opener);
    const menu = document.querySelector(".e-app-drawer") as HTMLElement;
    const main = document.querySelector(".e-app-main") as HTMLElement;
    expect(menu.hasAttribute("inert")).toBe(false);
    expect(menu.contains(document.activeElement)).toBe(true);
    expect(main.hasAttribute("inert")).toBe(true);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(menu.hasAttribute("inert")).toBe(true);
    expect(main.hasAttribute("inert")).toBe(false);
    expect(document.activeElement).toBe(opener);
  });

  it("closes when the window widens past the header-nav width, so the view is never left inert", async () => {
    const setNarrow = narrowWindow(true);
    await renderAt("#/home");
    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));
    expect((document.querySelector(".e-app-main") as HTMLElement).hasAttribute("inert")).toBe(true);
    act(() => setNarrow(false));
    expect((document.querySelector(".e-app-main") as HTMLElement).hasAttribute("inert")).toBe(false);
    expect((document.querySelector(".e-app-drawer") as HTMLElement).hasAttribute("inert")).toBe(true);
  });
});

describe("the header when the chat opens beside the view (P5)", () => {
  it("marks the frame while the chat is beside the view, not when it covers a phone's frame", async () => {
    const { setChatOpen } = await import("./chat/chatAccess");
    const setNarrow = narrowWindow(false);
    await renderAt("#/home");
    const shell = () => document.querySelector(".e-app-shell") as HTMLElement;
    expect(shell().classList.contains("has-chat")).toBe(false);
    act(() => setChatOpen(true));
    expect(shell().classList.contains("has-chat")).toBe(true);
    act(() => setNarrow(true));
    expect(shell().classList.contains("has-chat")).toBe(false);
    act(() => setChatOpen(false));
  });
});
