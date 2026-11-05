// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Badge } from "./Badge";
import { BADGES, galleryIds, normalizeId, resolveBadge, UNKNOWN_ID } from "./catalog.ts";
import { contrast, NON_TEXT_MIN, type Gamut } from "./color.ts";
import { ACCENT_FILL, GRID_SIZES, gridRows, INK_FILL, numeralGrid, parseGrid, svgMarkup, toRuns } from "./format.ts";
import * as A from "./glyphs.ts";
import { ACCENT_CSS, allThemes } from "./tokens.ts";

afterEach(cleanup);

// The stylesheet's text (vitest stubs CSS imports, `?raw` included). Read through a loose
// dynamic import so the app's tsconfig, which has no Node types, still checks this file.
type Fs = { readFileSync(p: string, enc: "utf8"): string };
const { readFileSync } = (await import(/* @vite-ignore */ "node:fs" as string)) as Fs;
const cwd = (globalThis as unknown as { process: { cwd(): string } }).process.cwd();
const css = readFileSync(`${cwd}/src/badges/badges.css`, "utf8");

describe("the grid format", () => {
  it("parses rows to the right dimensions, and back", () => {
    const g = parseGrid([".#+", "+#.", "..."]);
    expect([g.w, g.h]).toEqual([3, 3]);
    expect(g.cells).toEqual([0, 1, 2, 2, 1, 0, 0, 0, 0]);
    expect(gridRows(g)).toEqual([".#+", "+#.", "..."]);
  });

  it("refuses a ragged grid or an unknown character", () => {
    expect(() => parseGrid(["..", "..."])).toThrow(/wide/);
    expect(() => parseGrid([".x"])).toThrow(/use/);
    expect(() => parseGrid([])).toThrow();
  });

  it("every drawing is a square 16×16 or 12×12 grid", () => {
    for (const [name, rows] of Object.entries(A)) {
      if (!Array.isArray(rows) || name === "TICK") continue;
      const g = parseGrid(rows);
      expect(g.w, name).toBe(g.h);
      expect(GRID_SIZES as readonly number[], name).toContain(g.w);
    }
  });

  it("every catalogue badge parses to its own size (16, or 12 for a game or status mark)", () => {
    for (const id of galleryIds()) {
      const { grid, kind } = resolveBadge(id).def;
      const want = kind === "game" || kind === "status" ? 12 : 16;
      expect([grid.w, grid.h], id).toEqual([want, want]);
      expect(grid.cells.length, id).toBe(want * want);
      expect(grid.cells.some((c) => c === 1), `${id} has ink`).toBe(true);
    }
  });

  it("runs cover exactly the opaque cells, one rect per run", () => {
    for (const id of galleryIds()) {
      const g = resolveBadge(id).def.grid;
      const painted = toRuns(g).reduce((n, r) => n + r.w, 0);
      expect(painted, id).toBe(g.cells.filter((c) => c !== 0).length);
    }
  });

  it("draws numbers in a 3×5 face", () => {
    expect([numeralGrid(0).w, numeralGrid(0).h]).toEqual([3, 5]);
    expect(numeralGrid(10).w).toBe(7);
    expect(numeralGrid(1234).w).toBe(7);          // "9+"
    expect(numeralGrid(Number.NaN).w).toBe(3);    // "0", never a crash
  });
});

describe("the SVG", () => {
  it("has no background and paints only currentColor and the tint variable", () => {
    for (const id of galleryIds()) {
      const svg = svgMarkup(resolveBadge(id).def.grid, 3);
      expect(svg).toContain('shape-rendering="crispEdges"');
      // No full-size rect behind the shape, and no fill other than the two roles.
      const g = resolveBadge(id).def.grid;
      expect(svg, id).not.toMatch(new RegExp(`<rect x="0" y="0" width="${g.w}" height="${g.h}"`));
      const fills = [...svg.matchAll(/fill(?:="|: )([^";]+)/g)].map((m) => m[1].trim());
      for (const f of fills) expect([INK_FILL, ACCENT_FILL], `${id}: ${f}`).toContain(f);
      expect(svg).not.toMatch(/#[0-9a-f]{3,8}\b|rgb\(|oklch\(/i);
      expect(svg).not.toMatch(/<(path|image|text|circle|use)\b/);
    }
  });

  it("scales by whole numbers", () => {
    const g = resolveBadge("hello").def.grid;
    for (const s of [1, 2, 3] as const) {
      expect(svgMarkup(g, s)).toContain(`width="${16 * s}" height="${16 * s}" viewBox="0 0 16 16"`);
    }
  });
});

describe("the catalogue", () => {
  it("has every id the relay and the API use", () => {
    const want = [
      // quests.py QUESTS + WELCOMED, and the season-2 quests
      "hello", "first-set-play", "first-deal", "idea", "first-report", "first-fix-found", "debrief", "welcomed",
      "call", "bargain", "keep-word", "first-call", "first-bargain", "kept-word",
      // feats, and bugs.py's badges by the names the points route sends
      "well-travelled", "trader", "voice", "Idea adopted", "Bug finder",
      // progression classes, as base_sheet names them
      "Strategist", "Merchant", "Warden", "Bard",
      // module types
      "emporia:blotto:v1", "emporia:two-thirds:v1", "emporia:lowest-unique:v1", "emporia:chess:v1",
      "emporia:bargain:v1", "emporia:blotto:v2",
      // status
      "live", "sealed", "verified", "nous_verified", "testnet", "house", "called-it",
      // numbered
      "season-0", "renown-1", "renown-12", "rank-1", "rank-2", "rank-3",
      // titles resolve too
      "Make a call", "Strike a bargain", "Keep your word", "Explain a game", "Say hello",
    ];
    for (const id of want) expect(resolveBadge(id).fallback, id).toBe(false);
  });

  it("names numbered badges", () => {
    expect(resolveBadge("season-0").def.name).toBe("Season 0");
    expect(resolveBadge("renown:4").def.name).toBe("Renown level 4");
    expect(resolveBadge("Renown level 7").def.name).toBe("Renown level 7");
    expect(resolveBadge("rank-1").def.name).toBe("Top 1 on a season board");
    expect(resolveBadge("season-spring").def.kind).toBe("season");
    // "bargain" is the quest; the game is its module type.
    expect(resolveBadge("bargain").def.id).toBe("bargain");
    expect(resolveBadge("emporia:bargain:v1").def.id).toBe("game-bargain");
  });

  it("ids and aliases are unambiguous", () => {
    const seen = new Map<string, string>();
    for (const b of BADGES) {
      for (const k of [b.id, ...b.aliases].map(normalizeId)) {
        expect(seen.get(k) ?? b.id, `${k} is claimed twice`).toBe(b.id);
        seen.set(k, b.id);
      }
    }
  });

  it("an unknown id falls back to the generic badge, and nothing crashes", () => {
    for (const raw of ["gold-star", "", "   ", null, undefined, 42, {}, "rank-0", "renown-", "<script>"]) {
      const r = resolveBadge(raw);
      expect(r.def.grid.w).toBe(16);
      if (raw !== 42) expect(r.fallback, String(raw)).toBe(true);
    }
    expect(resolveBadge("gold-star").def).toMatchObject({ id: UNKNOWN_ID, name: "Gold star" });
  });
});

describe("<Badge>", () => {
  it("renders every catalogue badge as an image named for people", () => {
    for (const id of galleryIds()) {
      const { container, unmount } = render(<Badge id={id} size={2} />);
      const el = container.querySelector(".e-badge")!;
      expect(el.getAttribute("role")).toBe("img");
      expect(el.getAttribute("aria-label"), id).toBe(resolveBadge(id).def.name);
      expect(el.getAttribute("title"), id).toMatch(/^.+ — .+$/);
      expect(el.getAttribute("title")!.includes("\n")).toBe(false);
      expect(el.querySelector("svg")!.getAttribute("shape-rendering")).toBe("crispEdges");
      expect(el.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
      expect(el.querySelectorAll("rect").length).toBeGreaterThan(0);
      unmount();
    }
  });

  it("puts the earned line in the tooltip and keeps the name as the label", () => {
    render(<Badge id="well-travelled" size={1} title="earned 3 Nov" />);
    const el = screen.getByRole("img", { name: "Well travelled" });
    expect(el.getAttribute("title")).toBe("Well travelled — earned 3 Nov");
    expect(el.querySelector("svg")!.getAttribute("width")).toBe("16");
  });

  it("falls back for an unknown id and an odd size", () => {
    // @ts-expect-error a wrong size from loose data reads as 1×
    render(<Badge id="never-drawn" size={7} />);
    const el = screen.getByRole("img", { name: "Never drawn" });
    expect(el.hasAttribute("data-fallback")).toBe(true);
    expect(el.querySelector("svg")!.getAttribute("width")).toBe("16");
  });

  it("paints with currentColor and the accent variable only", () => {
    const { container } = render(<Badge id="kept-word" size={3} />);
    const groups = [...container.querySelectorAll("svg > g")];
    expect(groups[0].getAttribute("fill")).toBe("currentColor");
    expect((groups[1] as SVGElement).style.fill).toBe("var(--e-badge-accent, currentColor)");
    expect(container.querySelector("svg")!.getAttribute("width")).toBe("48");
  });

  it("pops only when asked; the CSS turns it off under reduced motion and in capture mode", () => {
    const { container } = render(<><Badge id="voice" pop /><Badge id="trader" /></>);
    const [a, b] = [...container.querySelectorAll(".e-badge")];
    expect(a.classList.contains("e-badge--pop")).toBe(true);
    expect(b.classList.contains("e-badge--pop")).toBe(false);
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)\s*{\s*\.e-badge--pop\s*{\s*animation:\s*none/);
    expect(css).toMatch(/\.e-capture \.e-badge--pop\s*{\s*animation:\s*none/);
  });
});

describe("contrast: legible on every canvas", () => {
  it("badges.css derives the accent exactly as tokens.ts computes it, from the look's own accent", () => {
    expect(css).toContain(`.e-look {\n  --e-badge-accent: var(--theme-focused-foreground);`);
    expect(css).toContain(`.e-badge-panel.theme-light {\n  --e-badge-accent: ${ACCENT_CSS.light};`);
    expect(css).toContain(`.e-badge-panel.theme-dark {\n  --e-badge-accent: ${ACCENT_CSS.dark};`);
    // The look sets that token to the same formula (with --anim-tint, which is --tint or amber).
    const look = readFileSync(`${cwd}/src/dashboard.css`, "utf8");
    const viaLook = (f: string) => f.replace("var(--tint, #f0a832)", "var(--anim-tint)");
    expect(look).toContain(`--theme-focused-foreground: ${viaLook(ACCENT_CSS.light)};`);
    expect(look).toContain(`--theme-focused-foreground: ${viaLook(ACCENT_CSS.dark)};`);
  });

  for (const gamut of ["css", "clip"] as Gamut[]) {
    it(`ink and accent meet ${NON_TEXT_MIN}:1 in the one style, both themes and all 8 tints (${gamut} gamut)`, () => {
      const themes = allThemes(gamut);
      expect(themes.length).toBe(16);
      const fails: string[] = [];
      for (const t of themes) {
        for (const s of t.surfaces) {
          for (const [role, rgb] of [["ink", t.ink], ["accent", t.accent]] as const) {
            const r = contrast(rgb, s.rgb);
            if (r < NON_TEXT_MIN) fails.push(`${t.style} ${t.theme} ${t.label} ${role} on ${s.name}: ${r.toFixed(2)}`);
          }
        }
        // A `mono` badge on a fill (a chip, a caption bar) is ink on the fill.
        if (t.fill && contrast(t.fill.ink, t.fill.bg) < NON_TEXT_MIN) fails.push(`${t.style} ${t.theme} ${t.label} mono on fill`);
      }
      expect(fails).toEqual([]);
    });
  }
});
