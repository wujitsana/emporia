// @vitest-environment jsdom
/**
 * The whole app, offline (every request refused, no socket ever opens), at a
 * phone's width: what a route to a hidden section shows, and the phone menu's
 * focus (review pass 2, docs/review-pass-2026-11-06-ui.md).
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
  vi.resetModules();
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
  const { default: App } = await import("./App");
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
    await renderAt("#/talk/messages");
    expect(screen.getByText("This section is your agent's own.")).toBeTruthy();
  });

  it("shows a section the viewer may see as before", async () => {
    narrowWindow(false);
    await renderAt("#/season");
    expect(screen.queryByText(/This section is/)).toBeNull();
    expect(await screen.findByText(/the relay did not answer|asking the relay/)).toBeTruthy();
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
