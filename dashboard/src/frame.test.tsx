// @vitest-environment jsdom
/**
 * ui-v2: the app is one frame, and everything it overlays stays inside it.
 *
 * The user's report: in the Hermes tab the chat had no input unless full page
 * (the drawer was 100vh tall, below Hermes's own header), full-page chat had no
 * way back or to the menu, and the phone menu covered the whole Hermes app
 * (position: fixed against the browser window). The frame is now sized to its
 * container, contains its overlays (`contain: layout paint`), and the chat's
 * composer is a slim line pinned at the bottom.
 */
import postcss, { type Declaration, type Rule } from "postcss";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BRAND, BRAND_OPEN_MS, BRAND_SCRAMBLED, BrandTitle } from "./ui/LookChrome";

// Vitest blanks CSS imports and this tsconfig has no node types: Node's own fs, typed narrowly
// (as cssModuleName.test.ts does).
type Fs = { readFileSync(p: string, enc: "utf8"): string };
const proc = (globalThis as unknown as { process: { cwd(): string; getBuiltinModule(m: string): unknown } }).process;
const fs = proc.getBuiltinModule("node:fs") as Fs;
const read = (f: string) => fs.readFileSync(`${proc.cwd()}/src/${f}`, "utf8");
const SHEETS = {
  "dashboard.css": read("dashboard.css"),
  "chat/ChatDrawer.module.css": read("chat/ChatDrawer.module.css"),
  "chat/AgentChat.module.css": read("chat/AgentChat.module.css"),
  "chat/AskAgent.module.css": read("chat/AskAgent.module.css"),
};

type Found = { file: string; sel: string; media: string; prop: string; value: string };

function decls(file: keyof typeof SHEETS): Found[] {
  const out: Found[] = [];
  postcss.parse(SHEETS[file]).walkDecls((d: Declaration) => {
    const rule = d.parent as Rule;
    if (rule?.type !== "rule") return;
    const at = rule.parent as { type?: string; params?: string };
    out.push({ file, sel: rule.selector, media: at?.type === "atrule" ? at.params ?? "" : "", prop: d.prop, value: d.value });
  });
  return out;
}

const ALL = (Object.keys(SHEETS) as (keyof typeof SHEETS)[]).flatMap(decls);
const find = (file: string, sel: string, prop: string, media?: RegExp) =>
  ALL.filter((d) => d.file === file && d.sel === sel && d.prop === prop && (!media || media.test(d.media)));

describe("the frame contains everything it overlays", () => {
  it("is the containing block of its overlays, sized to its container", () => {
    expect(find("dashboard.css", ".e-app-shell", "contain").map((d) => d.value)).toEqual(["layout paint"]);
    expect(find("dashboard.css", ".e-app-shell", "height")[0]?.value).toBe("var(--e-frame-h, 100dvh)");
    expect(find("dashboard.css", ".e-app-shell", "overflow")[0]?.value).toBe("hidden");
  });

  it("puts the phone menu, its backdrop and full-page chat inside the frame, the menu above the chat", () => {
    const drawer = find("dashboard.css", ".e-app-drawer", "position", /max-width/);
    const backdrop = find("dashboard.css", ".e-app-shell.is-nav-open .e-app-backdrop", "position", /max-width/);
    const chat = find("chat/ChatDrawer.module.css", ".root", "position", /max-width/);
    expect([drawer[0]?.value, backdrop[0]?.value, chat[0]?.value]).toEqual(["absolute", "absolute", "absolute"]);
    const z = (f: string, s: string) => Number(find(f, s, "z-index", /max-width/)[0]?.value);
    expect(z("dashboard.css", ".e-app-drawer")).toBeGreaterThan(z("chat/ChatDrawer.module.css", ".root"));
  });

  it("uses position: fixed nowhere but the theme popover's click-catcher, which the frame contains", () => {
    const fixed = ALL.filter((d) => d.prop === "position" && d.value === "fixed").map((d) => d.sel);
    expect(fixed).toEqual([".e-theme-compact__backdrop"]);
  });

  it("sizes no full-height part to the browser window but the standalone frame's own fallback", () => {
    // Caps on a panel's height (a feed at min(70vh, 520px)) are fine; a full window's height is not.
    const vh = ALL.filter((d) => (d.prop === "height" || d.prop === "min-height") && /\b100d?vh\b/.test(d.value)
      && !(d.sel === ".e-app-shell" || d.sel.startsWith(".e-capture")));
    expect(vh.map((d) => `${d.file} ${d.sel} ${d.prop}: ${d.value}`)).toEqual([]);
  });
});

describe("the chat drawer", () => {
  it("is as tall as the frame (not the window), with the transcript scrolling and the composer pinned", () => {
    expect(find("chat/ChatDrawer.module.css", ".root", "height").map((d) => d.value)).toContain("100%");
    expect(find("chat/ChatDrawer.module.css", ".transcript", "overflow-y")[0]?.value).toBe("auto");
    expect(find("chat/ChatDrawer.module.css", ".composer", "flex")[0]?.value).toBe("none");
  });

  it("has a slim composer: one 42px line, the send button as tall, growing to about four lines", () => {
    expect(find("chat/ChatDrawer.module.css", ".input", "height")[0]?.value).toBe("42px");
    expect(find("chat/ChatDrawer.module.css", ".input", "resize")[0]?.value).toBe("none");
    expect(find("chat/ChatDrawer.module.css", ".input", "max-height")[0]?.value).toMatch(/^calc\(4 \*/);
    expect(find("chat/ChatDrawer.module.css", ".send", "height")[0]?.value).toBe("42px");
  });
});

describe("the brand", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("opens on the title card's E#P0R!A and decodes to EMPORIA; screen readers get the word", async () => {
    // jsdom paints no frames: a frame every 16 ms stands in.
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => setTimeout(() => cb(performance.now()), 16));
    vi.stubGlobal("cancelAnimationFrame", (h: number) => clearTimeout(h));
    render(<BrandTitle />);
    const shown = () => document.querySelector(".e-brand > [aria-hidden]")!.textContent;
    expect(shown()).toBe(BRAND_SCRAMBLED);
    expect(screen.getByText("Emporia").className).toBe("e-sr");
    await act(() => new Promise((r) => setTimeout(r, BRAND_OPEN_MS + 250)));
    expect(shown()).toBe(BRAND);
  });

  it("is still under prefers-reduced-motion", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce"), media: q }));
    render(<BrandTitle replay="games" />);
    expect(document.querySelector(".e-brand > [aria-hidden]")!.textContent).toBe(BRAND);
  });
});
