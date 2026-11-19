// Emporia agent pictures: "Mix v2, restored: sliver edges scaled, all 135 marks". One pure function from the agent's stable id
// (64 hex chars) to an SVG string:
//   tokenAvatar(id)   Rooms (3 agents in 8), Strata (3 in 8) or Hallmark (2 in 8), flat shapes: the profile picture and the
//                     ERC-8004 registration image
// Strata: a shaped edge that would leave a sliver is drawn smaller (same kind, a lower amplitude or radius), never flipped,
// copied or flattened. Hallmark: v2's 24 plates and all 135 marks.
// (the earlier line-screen "everyday" design is archived outside the repo: research/archive/avatars-line-screen)
// Integer math and literal tables only; avatars.py produces the same bytes (see tests/fixtures/avatar_vectors.json).

import { COMPANIONS } from "./palette";
import { TINTS } from "./themeConfig";

// ---- palette: the dashboard's palette (palette.ts), with exactly the colors its themes show ----------------------------------
// ink = the dark theme's page background, paper = the light theme's page background, deep = the light theme's accent (the
// tint's deep companion), bright = the dark theme's accent (its light companion; green is pinned to the video's #39ff44).
// avatars.test.ts holds these rows to the theme code, so they cannot drift.
//            tint       ink        paper      deep       bright    light tint?
export const TINT: (string | number)[][] = [
  ["#39ff44", "#001b00", "#ebfaea", "#006c00", "#39ff44", 1], // green
  ["#0047ff", "#051232", "#ecf6ff", "#005e8b", "#69cdff", 0], // blue
  ["#b00331", "#250a0c", "#fff0f0", "#7d0022", "#ffbeff", 0], // crimson
  ["#d57c22", "#201004", "#fdf3ec", "#5f3a22", "#ffad33", 1], // burnt orange
  ["#7d3c98", "#1a0f20", "#f9f2fc", "#431655", "#dc97fb", 0], // purple
  ["#888888", "#141414", "#ececec", "#555555", "#b7b7b7", 1], // charcoal
];
// The companions per tint, [light, deep], in the same order: the accents come from these.
export const FAMILY: string[][] = TINTS.map((t) => [COMPANIONS[t].light, COMPANIONS[t].deep]);

export function idBytes(id: string): number[] {
  const b: number[] = [];
  for (let i = 0; i < 32; i++) b.push(parseInt(id.substr(i * 2, 2), 16));
  return b;
}
const SVG_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">';
export function fill(color: string, d: string): string { return d ? '<path fill="' + color + '" d="' + d + '"/>' : ""; }
export function ground(color: string): string { return fill(color, "M0 0h32v32H0z"); }
// Closes the SVG. A tile on a near-white (paper) ground gets a thin ink outline, so it does not vanish on a light page.
function close(col: (string | number)[]): string {
  return (col[4] ? '<path fill="none" stroke="' + col[3] + '" stroke-opacity=".5" stroke-width=".5" d="M.25 .25h31.5v31.5h-31.5z"/>' : "") + "</svg>";
}

// The color's luma, 2126 R + 7152 G + 722 B on the 0-255 values (integer: avatars.py does the same).
function luma(h: string): number { return 2126 * parseInt(h.slice(1, 3), 16) + 7152 * parseInt(h.slice(3, 5), 16) + 722 * parseInt(h.slice(5, 7), 16); }
const GUEST_GAP = 765000;   // the accent's luma must differ from the ground by 30% of the full range (2,550,000)
const GUEST_GAP2 = 382500;  // and from every other color of the picture by 15%
const MID = 1275000;        // the ground's luma at the 50% midpoint: above it the ground is light
// The accent: another tint's companion. Ground light (luma over MID) -> the deep tone, else the light one. Of the other tints'
// companions (charcoal's aside), those clear of the ground by GUEST_GAP and of every other color by GUEST_GAP2, picked by b[2]; else charcoal if clear;
// else a hue clear of the ground only; else the tint's own deep or bright shade clear of the ground; else the candidate with the
// largest smallest gap.
function guestColor(b: number[]): string {
  const t = b[0] % 6, mode = b[1] % 8, f = TINT[t] as string[];
  const others = [[f[1], f[4], f[2]], [f[1], f[4], f[2]], [f[2], f[3], f[1]], [f[2], f[1], f[3]],
    f[5] ? [f[0], f[1], f[2]] : [f[0], f[2], f[1]], [f[1], f[2], f[4]], [f[3], f[4], f[1]], [f[4], f[3], f[1]]][mode];
  const tone = luma(others[0]) >= MID ? 1 : 0;
  const gap = (c: string, r: string) => Math.abs(luma(c) - luma(r));
  const clear = (c: string) => gap(c, others[0]) >= GUEST_GAP && others.slice(1).every((r) => gap(c, r) >= GUEST_GAP2);
  const hues: string[] = [];
  for (let i = 0; i < 5; i++) if (i !== t) hues.push(FAMILY[i][tone]);
  let ok = hues.filter(clear);
  if (ok.length) return ok[b[2] % ok.length];
  const gray = FAMILY[5][tone];
  if (t !== 5 && clear(gray)) return gray;
  ok = hues.filter((c) => gap(c, others[0]) >= GUEST_GAP);
  if (ok.length) return ok[b[2] % ok.length];
  ok = [f[3], f[4]].filter((c) => gap(c, others[0]) >= GUEST_GAP);
  if (ok.length) return ok[b[2] % ok.length];
  let best = "", bestGap = -1;   // the first of the largest smallest gap, as Python's max() takes it
  for (const c of hues.concat([gray, f[3], f[4]])) {
    const m = Math.min(...others.map((r) => gap(c, r)));
    if (m > bestGap) { best = c; bestGap = m; }
  }
  return best;
}
// -> [ground, lead, second, third, outline?]; eight ground-and-lead modes.
export function palette(b: number[]): (string | number)[] {
  const t = b[0] % 6, f = TINT[t] as string[], guest = guestColor(b);
  const mode = b[1] % 8;
  if (mode === 0) return [f[1], f[4], guest, f[2]];        // dark ground, tint leads
  if (mode === 1) return [f[1], guest, f[4], f[2]];        // dark ground, guest leads
  if (mode === 2) return [f[2], f[3], guest, f[1], 1];     // paper ground, tint leads (1 = outline it)
  if (mode === 3) return [f[2], f[1], f[3], guest, 1];     // paper ground, ink leads
  if (mode === 4) return f[5] ? [f[0], f[1], f[2], guest] : [f[0], f[2], f[1], guest]; // tint ground
  if (mode === 5) return [guest, f[1], f[2], f[4]];        // guest ground, ink leads
  if (mode === 6) return [f[3], f[4], guest, f[1]];        // deep ground, bright leads
  return [f[4], f[3], guest, f[1]];                        // bright ground, deep leads
}

// ---- Rooms: a room seen from above, the far wall off-center, the four walls lit in a pattern -----------------
const ROOM_WALLS = [[1, 0, 0, 0], [1, 1, 0, 0], [1, 0, 1, 0], [1, 2, 0, 0], [1, 0, 2, 0], [1, 2, 1, 2], [1, 1, 2, 2], [1, 1, 1, 0], [1, 2, 3, 0], [1, 3, 0, 0], [1, 0, 3, 0], [3, 1, 3, 1]];
export function rooms(id: string): string {
  const b = idBytes(id), col = palette(b) as string[];
  const h = [4, 6, 8][b[3] % 3], lo = h + 2, hi = 30 - h;
  let cx = [8, 12, 16, 20, 24][b[4] % 5], cy = [8, 12, 16, 20, 24][b[5] % 5];
  cx = cx < lo ? lo : cx > hi ? hi : cx;
  cy = cy < lo ? lo : cy > hi ? hi : cy;
  const x0 = cx - h, y0 = cy - h, x1 = cx + h, y1 = cy + h;
  const wall = [
    "M0 0L32 0L" + x1 + " " + y0 + "L" + x0 + " " + y0 + "z",
    "M32 0L32 32L" + x1 + " " + y1 + "L" + x1 + " " + y0 + "z",
    "M32 32L0 32L" + x0 + " " + y1 + "L" + x1 + " " + y1 + "z",
    "M0 32L0 0L" + x0 + " " + y0 + "L" + x0 + " " + y1 + "z",
  ];
  const pat = ROOM_WALLS[b[6] % 12], turn = b[7] % 4;
  const d = ["", "", "", ""];
  for (let i = 0; i < 4; i++) d[pat[(i + turn) % 4]] += wall[i];
  d[[2, 1, 3][b[8] % 3]] += "M" + x0 + " " + y0 + "H" + x1 + "V" + y1 + "H" + x0 + "z";
  const m = b[9] % 4, r = h / 2;
  let mark = "";
  if (m === 1) mark = '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="' + col[0] + '"/>';
  if (m === 2) mark = fill(col[0], "M" + cx + " " + (cy - r) + "L" + (cx + r) + " " + cy + "L" + cx + " " + (cy + r) + "L" + (cx - r) + " " + cy + "z");
  if (m === 3) mark = fill(col[0], "M" + (cx - r) + " " + (cy - r) + "h" + h + "v" + h + "h-" + h + "z");
  return SVG_OPEN + ground(col[0]) + fill(col[1], d[1]) + fill(col[2], d[2]) + fill(col[3], d[3]) + mark + close(palette(b));
}

// ---- Strata: two or three layers poured across the tile, each with its own edge profile, turned one of four ways --
const STRATA_Y3 = [[7, 15, 23], [6, 13, 22], [10, 17, 24], [8, 18, 25]];
const STRATA_Y2 = [[10, 21], [8, 18], [13, 23], [11, 19]];
const STRATA_ORDER = [[1, 2, 3], [2, 1, 3], [1, 3, 2], [1, 2, 1]];
// Per edge kind (1..7), the amplitudes from the full one down: step, slope rise, notch depth, sag control height, zigzag swing,
// cascade stair, weir radius.
const AMP: number[][] = [[], [5, 4, 3, 2], [7, 5, 4, 2], [5, 4, 3, 2], [10, 8, 6, 4], [4, 3, 2, 1], [3, 2, 1], [8, 6, 4, 2]];
export function isqrt(v: number): number { let r = 0; while ((r + 1) * (r + 1) <= v) r++; return r; }
interface Edge { y: number; kind: number; s: number; v: number; a: number; lv: number; flat?: boolean }
// A profile's height at x = X2 / 2 (X2 odd, 1..63), times 4096, so that everything stays an integer. a: amplitude; s: the lean.
function strataAt(c: Edge, X2: number): number {
  const { y, kind, s, v, a } = c;
  if (kind === 1) return 4096 * (X2 < 2 * [10, 16, 22][(v >> 1) % 3] ? y : y + a * s);
  if (kind === 2) return 4096 * y + 64 * a * s * X2;
  if (kind === 3) return 4096 * (X2 > 22 && X2 < 42 ? y + a * s : y);
  if (kind === 4) return 4096 * y + 2 * a * s * X2 * (64 - X2);
  if (kind === 5) return (X2 >> 4) % 2 ? 4096 * (y - a * s) + 256 * a * s * (X2 & 15) : 4096 * y - 256 * a * s * (X2 & 15);
  if (kind === 6) return 4096 * (y + a * s * (X2 >> 4));
  if (kind === 7) return X2 > 32 - 2 * a && X2 < 32 + 2 * a ? 4096 * y - s * 2048 * isqrt(4 * a * a - (X2 - 32) * (X2 - 32)) : 4096 * y;
  return 4096 * y;
}
// A profile fits when it stays 3 units inside the tile and 3 units below the layer above it, all the way across.
function strataFits(c: Edge, above: Edge | null): boolean {
  for (let X2 = 1; X2 < 64; X2 += 2) {
    const p = strataAt(c, X2);
    if (p < 12288 || p > 118784) return false;
    if (above && p - strataAt(above, X2) < 12288) return false;
  }
  return true;
}
function strataEdge(c: Edge): string {
  const { y, kind, s, v, a } = c;
  if (kind === 1) return "H" + [10, 16, 22][(v >> 1) % 3] + "V" + (y + a * s) + "H32";       // one step
  if (kind === 2) return "L32 " + (y + a * s);                                               // slope
  if (kind === 3) return "H11v" + a * s + "h10v" + -a * s + "H32";                           // notch or block
  if (kind === 4) return "Q16 " + (y + a * s) + " 32 " + y;                                  // sag or swell
  if (kind === 5) return "l8 " + -a * s + "l8 " + a * s + "l8 " + -a * s + "l8 " + a * s;    // zigzag
  if (kind === 6) return "h8v" + a * s + "h8v" + a * s + "h8v" + a * s + "h8";               // cascade
  if (kind === 7) return "H" + (16 - a) + "a" + a + " " + a + " 0 0 " + (s > 0 ? 1 : 0) + " " + 2 * a + " 0H32"; // weir crest or basin
  return "H32";                                                                              // level
}
// Layers are tried in order, each at full amplitude first. When a layer fits at no amplitude (the layers above left it no
// room), the layers above are tried one step smaller before anything else; only if no combination fits does a layer lie level.
function strataPlan(kinds: number[], ys: number[], vs: number[]): Edge[] {
  const n = ys.length;
  const options = (i: number): Edge[] => {
    const kind = kinds[i], y = ys[i], v = vs[i], s = v % 2 ? -1 : 1;
    return kind ? AMP[kind].map((a, lv) => ({ y, kind, s, v, a, lv })) : [{ y, kind: 0, s: 1, v: 0, a: 0, lv: 0 }];
  };
  const solve = (i: number, above: Edge | null): Edge[] | null => {
    if (i === n) return [];
    for (const c of options(i)) {
      if (!strataFits(c, above)) continue;
      const rest = solve(i + 1, c);
      if (rest) return [c].concat(rest);
    }
    return null;
  };
  const sol = solve(0, null);
  if (sol) return sol;
  const out: Edge[] = [];
  let above: Edge | null = null;
  for (let i = 0; i < n; i++) {
    let c: Edge = { y: ys[i], kind: 0, s: 1, v: 0, a: 0, lv: 0, flat: !!kinds[i] };
    if (kinds[i]) for (const t of options(i)) if (strataFits(t, above)) { c = t; break; }
    out.push(c);
    above = c;
  }
  return out;
}
export function strata(id: string): string {
  const b = idBytes(id), col = palette(b) as string[];
  const n = 2 + (b[4] % 2);
  const ys = (n === 3 ? STRATA_Y3 : STRATA_Y2)[b[5] % 4].slice(0, n), order = STRATA_ORDER[b[6] % 4];
  const kinds: number[] = [], vs: number[] = [];
  for (let i = 0; i < n; i++) { kinds.push(b[7 + i] % 8); vs.push(b[10 + i]); }
  const plan = strataPlan(kinds, ys, vs);
  let out = "";
  for (let i = 0; i < n; i++) out += fill(col[order[i]], "M0 " + ys[i] + strataEdge(plan[i]) + "V32H0z");
  if (b[13] % 3 === 0) out += '<circle cx="' + [8, 16, 24][b[14] % 3] + '" cy="' + (ys[0] - 5) + '" r="2" fill="' + col[order[1]] + '"/>';
  const turn = (b[3] % 4) * 90;
  return SVG_OPEN + ground(col[0]) + (turn ? '<g transform="rotate(' + turn + ' 16 16)">' + out + "</g>" : out) + close(palette(b));
}

// ---- Hallmark: a plate struck with a woven line mark on a 3x3 lattice, with one punched node, sometimes framed -----
const PLATES = [
  "M3 16a13 13 0 1 0 26 0a13 13 0 1 0-26 0z", "M4 4h24v24H4z", "M16 1L31 16L16 31L1 16z", "M10 3h12l7 7v12l-7 7H10l-7-7V10z",
  "M4 29V15a12 12 0 0 1 24 0V29z", "M4 4h24v13L16 29L4 17z", "M16 2l12 7v14l-12 7L4 23V9z", "M4 4h17l7 7v17H4z",
  "M4 28V15L16 3L28 15V28z", "M4 3h24v14a12 12 0 0 1-24 0z", "M4 4h24v24H11l-7-7z", "M0 6h32v20H0z", "M6 0h20v32H6z",
  "M-2 16a18 18 0 1 0 36 0a18 18 0 1 0-36 0z", "M0 0h32v19L19 32H0z",
  "M8 4h16a4 4 0 0 0 4 4v16a4 4 0 0 0-4 4H8a4 4 0 0 0-4-4V8a4 4 0 0 0 4-4z", "M11 4h17v24H4V11z", "M4 4h24v17l-7 7H4z",
  "M0 0h19L32 13v19H0z", "M13 0h19v32H0V13z", "M0 0h32v32H13L0 19z", "M4 4h13l12 12l-12 12H4z", "M28 4H15L3 16l12 12h13z",
  "M16 -4L36 32H-4z",
];
const MARK_PARTS = [                                        // ten line groups, each with both mirror axes
  "M10 10H22M10 22H22", "M10 10V22M22 10V22", "M16 10V22", "M10 16H22", "M10 10L22 22M22 10L10 22",
  "M16 10L22 16L16 22L10 16z", "M10 10L22 16L10 22M22 10L10 16L22 22", "M10 10L16 22L22 10M10 22L16 10L22 22",
  "M13 13h6v6h-6z", "M10 13V10h3M19 10h3v3M22 19v3h-3M13 22h-3v-3",
];
// Unions of groups that are first widened with X + diamond (the early check against a mark that reads as a letter).
const MARK_WIDEN = [4, 5, 13, 2, 6, 10, 14, 3, 11, 7, 16, 32, 12, 64, 128, 18, 130, 17, 65, 36, 40, 66, 129, 8, 22, 26, 134, 138, 70, 74, 42, 37, 256, 260, 264, 258, 266, 261, 262, 272, 528, 514, 516, 518, 522, 517, 524, 257, 259, 273, 274, 288, 292, 296, 515, 521, 529, 530, 768, 769, 770, 772, 776];
export function hallmark(id: string): string {
  const b = idBytes(id), col = palette(b) as string[];
  let m = (1 << (b[5] % 10)) | (1 << (b[6] % 10));
  if (b[4] % 2) m |= 1 << (b[7] % 4);
  if (MARK_WIDEN.indexOf(m) >= 0) m |= 48;
  let d = "", parts = 0;
  for (let i = 0; i < 10; i++) if (m & (1 << i)) { d += MARK_PARTS[i]; parts++; }
  const node = b[14] % 9, fr = b[17] % 3;
  return SVG_OPEN + ground(col[0]) + fill(col[b[15] % 3 ? 1 : 3], PLATES[b[3] % 24])
    + '<path fill="none" stroke="' + col[0] + '" stroke-width="' + (parts > 2 ? 2 : 3) + '" stroke-linecap="round" stroke-linejoin="round" d="' + d + '"/>'
    + '<circle cx="' + (10 + 6 * (node % 3)) + '" cy="' + (10 + 6 * ((node / 3) | 0)) + '" r="' + (3 + (b[16] % 2)) + '" fill="' + col[2] + '"/>'
    + (fr ? '<path fill="none" stroke="' + col[fr + 1] + '" stroke-width="2" d="M1 1h30v30H1z"/></svg>' : close(palette(b)));
}

// Of 8 agents, 3 draw Rooms, 3 Strata and 2 Hallmark.
const DRAWERS = [rooms, strata, rooms, strata, hallmark, rooms, strata, hallmark];
export function tokenAvatar(id: string): string {
  return DRAWERS[idBytes(id)[31] % 8](id);
}
