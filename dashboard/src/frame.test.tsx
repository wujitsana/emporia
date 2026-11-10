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
    // The menu belongs to the phone layout, chosen from the frame's own width (data-nav), not the window's.
    const PHONE = '.e-app-shell[data-nav="phone"]';
    const drawer = find("dashboard.css", `${PHONE} .e-app-drawer`, "position");
    const backdrop = find("dashboard.css", `${PHONE}.is-nav-open .e-app-backdrop`, "position");
    const chat = find("chat/ChatDrawer.module.css", ".root", "position", /max-width/);
    expect([drawer[0]?.value, backdrop[0]?.value, chat[0]?.value]).toEqual(["absolute", "absolute", "absolute"]);
    const zOf = (f: string, s: string, m?: RegExp) => Number(find(f, s, "z-index", m)[0]?.value);
    expect(zOf("dashboard.css", `${PHONE} .e-app-drawer`)).toBeGreaterThan(zOf("chat/ChatDrawer.module.css", ".root", /max-width/));
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

  it("has a large wordmark and words without icons in the header row, wide and medium", () => {
    const brand = find("dashboard.css", '.e-app-shell[data-nav="wide"] .e-app-top .e-brand-title,\n.e-app-shell[data-nav="medium"] .e-app-top .e-brand-title', "font-size");
    expect(brand[0]?.value).toContain("1.5rem");
    expect(find("dashboard.css", ".e-section-tabs--horizontal .e-section-tab__icon", "display")[0]?.value).toBe("none");
    expect(find("dashboard.css", '.e-app-shell[data-nav="medium"] .e-section-tabs--horizontal .e-section-tab__icon', "display")).toHaveLength(0);
  });

  it("has a slim composer: one 42px line, the send button as tall, growing to six lines", () => {
    expect(find("chat/ChatDrawer.module.css", ".input", "height")[0]?.value).toBe("42px");
    expect(find("chat/ChatDrawer.module.css", ".input", "resize")[0]?.value).toBe("none");
    expect(find("chat/ChatDrawer.module.css", ".input", "max-height")[0]?.value).toMatch(/^calc\(6 \*/);
    expect(find("chat/ChatDrawer.module.css", ".send", "height")[0]?.value).toBe("42px");
  });

  // Review pass 2: iOS zooms the page when a field under 16px takes focus. The composer had
  // 16px on a phone; the agent's question box (0.86rem) and the menu's viewer select (12px) did not.
  it("keeps every field on a phone at 16px, so focusing one never zooms the page", () => {
    const phone = /max-width: 900px/;
    const sized = (file: string, cls: string) => ALL.some((d) => d.file === file && phone.test(d.media)
      && d.prop === "font-size" && d.value === "16px" && d.sel.split(",").map((s) => s.trim()).includes(cls));
    expect(sized("chat/ChatDrawer.module.css", ".input")).toBe(true);
    expect(sized("chat/ChatDrawer.module.css", ".answerInput")).toBe(true);
    expect(sized("dashboard.css", ".e-viewer-switch select")).toBe(true);
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

// The header keeps one row at every width, and the main navigation never scrolls: the layout is
// chosen from the dashboard's own width (ui/navLayout.ts), so a chat beside the view, or Hermes's
// sidebar, makes it drop a tier by itself.
describe("the header and the main navigation", () => {
  it("keeps one row: no wrap, the five places never scroll", () => {
    expect(find("dashboard.css", ".e-app-top__row", "flex-wrap").map((d) => d.value)).toContain("nowrap");
    expect(find("dashboard.css", '.e-app-shell[data-nav="wide"] .e-section-tabs--horizontal,\n.e-app-shell[data-nav="medium"] .e-section-tabs--horizontal', "flex-wrap")[0]?.value).toBe("nowrap");
    const scrolls = ALL.filter((d) => d.file === "dashboard.css" && /overflow(-x)?$/.test(d.prop) && /auto|scroll/.test(d.value)
      && /e-section-tab/.test(d.sel) && !/drawer/.test(d.sel)).map((d) => d.sel);
    expect(scrolls).toEqual([]);
    expect(find("dashboard.css", ".e-app-top__actions", "flex-shrink").at(-1)?.value).toBe("0");
  });

  it("drops the relay's version, never the relay and money chips", () => {
    const hidden = ALL.filter((d) => d.prop === "display" && d.value === "none" && d.sel.includes("data-nav")).map((d) => d.sel).join(",");
    expect(hidden).toMatch(/\.e-relay-status__meta/);
    expect(hidden).not.toMatch(/e-money-chip|status-chip__label--relay/);
  });

  it("gates no navigation on the window's width: only content padding and touch sizes use a media query", () => {
    const nav = ALL.filter((d) => d.file === "dashboard.css" && /max-width|min-width/.test(d.media)
      && /e-section-tab|e-bottom-bar|e-subnav|e-app-top|e-mobile-top|e-app-drawer|e-app-backdrop/.test(d.sel)).map((d) => d.sel);
    expect(nav).toEqual([]);
  });
});
