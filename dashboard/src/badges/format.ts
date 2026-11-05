/**
 * The pixel badge format: a badge is a small grid written as one string per row.
 *
 *   "."  (or a space)  transparent: nothing is drawn, so the badge sits on any canvas
 *   "#"                ink: `currentColor`, the text colour where the badge is placed
 *   "+"                accent: `var(--e-badge-accent)`, derived from the tint (badges.css)
 *
 * Two roles at most, and no background: the badge follows light/dark and every tint by
 * itself. A grid renders to an inline SVG of `<rect>`s (one per horizontal run) with
 * `shape-rendering="crispEdges"`, at a whole-number scale (1×, 2×, 3×).
 *
 * Pure: no DOM, no React. The component (Badge.tsx), the gallery, the tests and the
 * sheet renderer all read the same grids.
 */

export type Role = "ink" | "accent";

/** 0 transparent, 1 ink, 2 accent. */
export type Cell = 0 | 1 | 2;

export interface Grid {
  readonly w: number;
  readonly h: number;
  /** Row-major, `w * h` cells. */
  readonly cells: readonly Cell[];
}

export interface Run {
  x: number;
  y: number;
  w: number;
  role: Role;
}

export const SCALES = [1, 2, 3] as const;
export type Scale = (typeof SCALES)[number];

/** The sizes a badge is drawn at: 16×16, or 12×12 for small inline marks. */
export const GRID_SIZES = [12, 16] as const;

const CHAR_CELL: Record<string, Cell> = { ".": 0, " ": 0, "#": 1, "+": 2 };

/** Parse rows into a grid. Throws on a ragged grid or an unknown character (a source bug). */
export function parseGrid(rows: readonly string[]): Grid {
  if (!rows.length) throw new Error("badge grid: no rows");
  const w = rows[0].length;
  if (!w) throw new Error("badge grid: empty row");
  const cells: Cell[] = [];
  rows.forEach((row, y) => {
    if (row.length !== w) throw new Error(`badge grid: row ${y} is ${row.length} wide, not ${w}`);
    for (const ch of row) {
      const c = CHAR_CELL[ch];
      if (c === undefined) throw new Error(`badge grid: row ${y} has "${ch}" (use . # +)`);
      cells.push(c);
    }
  });
  return { w, h: rows.length, cells };
}

/** The grid back as rows, for tests and for reading a composed badge. */
export function gridRows(g: Grid): string[] {
  const out: string[] = [];
  for (let y = 0; y < g.h; y++) {
    let s = "";
    for (let x = 0; x < g.w; x++) s += ".#+"[g.cells[y * g.w + x]];
    out.push(s);
  }
  return out;
}

export function cellAt(g: Grid, x: number, y: number): Cell {
  return x < 0 || y < 0 || x >= g.w || y >= g.h ? 0 : g.cells[y * g.w + x];
}

/**
 * Draw `top` onto `base` at (x, y). Opaque cells of `top` win; its transparent cells
 * leave the base as it is. `as` repaints every opaque cell of `top` in one role.
 * The result keeps the base's size (whatever falls outside is dropped).
 */
export function overlay(base: Grid, top: Grid, x: number, y: number, as?: Role): Grid {
  const cells = base.cells.slice();
  const paint: Cell | undefined = as === "ink" ? 1 : as === "accent" ? 2 : undefined;
  for (let ty = 0; ty < top.h; ty++) {
    for (let tx = 0; tx < top.w; tx++) {
      const c = top.cells[ty * top.w + tx];
      const bx = x + tx;
      const by = y + ty;
      if (!c || bx < 0 || by < 0 || bx >= base.w || by >= base.h) continue;
      cells[by * base.w + bx] = paint ?? c;
    }
  }
  return { w: base.w, h: base.h, cells };
}

/** Clear a rectangle (a cut-out, so an overlay reads apart from what is under it). */
export function clearRect(base: Grid, x: number, y: number, w: number, h: number): Grid {
  const cells = base.cells.slice();
  for (let yy = Math.max(0, y); yy < Math.min(base.h, y + h); yy++) {
    for (let xx = Math.max(0, x); xx < Math.min(base.w, x + w); xx++) cells[yy * base.w + xx] = 0;
  }
  return { w: base.w, h: base.h, cells };
}

// ── digits: a 3×5 pixel numeral face, for levels, ranks and the season ──────

const DIGITS: Record<string, readonly string[]> = {
  "0": ["###", "#.#", "#.#", "#.#", "###"],
  "1": [".#.", "##.", ".#.", ".#.", "###"],
  "2": ["###", "..#", "###", "#..", "###"],
  "3": ["###", "..#", ".##", "..#", "###"],
  "4": ["#.#", "#.#", "###", "..#", "..#"],
  "5": ["###", "#..", "###", "..#", "###"],
  "6": ["###", "#..", "###", "#.#", "###"],
  "7": ["###", "..#", "..#", ".#.", ".#."],
  "8": ["###", "#.#", "###", "#.#", "###"],
  "9": ["###", "#.#", "###", "..#", "###"],
  "+": ["...", ".#.", "###", ".#.", "..."],
};

/** A number in the 3×5 face, one column between digits. Above 99 it reads "9+", so it
 *  fits a 16-pixel badge. Negative or not a number: "0". */
export function numeralGrid(n: number): Grid {
  const v = Number.isFinite(n) ? Math.max(0, Math.floor(n)) : 0;
  const text = v > 99 ? "9+" : String(v);
  const rows = ["", "", "", "", ""];
  [...text].forEach((ch, i) => {
    const d = DIGITS[ch];
    for (let r = 0; r < 5; r++) rows[r] += (i ? "." : "") + d[r];
  });
  return parseGrid(rows);
}

/** Draw a number centred on column `cx` (the left of the two middle columns rounds left). */
export function withNumeral(base: Grid, n: number, cx: number, y: number, as: Role = "ink"): Grid {
  const num = numeralGrid(n);
  const x = Math.round(cx - (num.w - 1) / 2);
  return overlay(clearRect(base, x - 1, y - 1, num.w + 2, num.h + 2), num, x, y, as);
}

// ── rendering ────────────────────────────────────────────────────────────────

/** Horizontal runs of one role: one `<rect>` each. Ink first, then accent. */
export function toRuns(g: Grid): Run[] {
  const out: Run[] = [];
  for (const role of ["ink", "accent"] as const) {
    const want: Cell = role === "ink" ? 1 : 2;
    for (let y = 0; y < g.h; y++) {
      let x = 0;
      while (x < g.w) {
        if (g.cells[y * g.w + x] !== want) { x++; continue; }
        const start = x;
        while (x < g.w && g.cells[y * g.w + x] === want) x++;
        out.push({ x: start, y, w: x - start, role });
      }
    }
  }
  return out;
}

/** A whole-number scale of 1–3; anything else reads as 1. */
export function toScale(size: unknown): Scale {
  return size === 2 || size === 3 ? size : 1;
}

/** What fills each role. The defaults are the live ones; the sheet renderer passes colours. */
export interface Paint {
  ink?: string;
  accent?: string;
}

export const INK_FILL = "currentColor";
export const ACCENT_FILL = "var(--e-badge-accent, currentColor)";

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/**
 * The badge as SVG markup: two groups (ink, accent), no background. The live dashboard
 * renders the same structure as React elements (Badge.tsx); this string form is for the
 * tests and the gallery sheet.
 */
export function svgMarkup(g: Grid, scale: Scale = 1, paint: Paint = {}): string {
  const runs = toRuns(g);
  const rect = (r: Run) => `<rect x="${r.x}" y="${r.y}" width="${r.w}" height="1"/>`;
  const ink = runs.filter((r) => r.role === "ink").map(rect).join("");
  const acc = runs.filter((r) => r.role === "accent").map(rect).join("");
  const accentAttr = paint.accent ? `fill="${esc(paint.accent)}"` : `style="fill: ${ACCENT_FILL}"`;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${g.w * scale}" height="${g.h * scale}" `
    + `viewBox="0 0 ${g.w} ${g.h}" shape-rendering="crispEdges" aria-hidden="true" focusable="false">`
    + `<g fill="${esc(paint.ink ?? INK_FILL)}">${ink}</g>`
    + (acc ? `<g class="e-badge__accent" ${accentAttr}>${acc}</g>` : "")
    + `</svg>`;
}
