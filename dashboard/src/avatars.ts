// Emporia agent pictures. One pure function from the agent's stable id (64 hex chars) to an SVG string:
//   tokenAvatar(id)   Rooms, Strata or Hallmark, flat shapes: the profile picture and the ERC-8004 registration image
// (the earlier line-screen "everyday" design is archived outside the repo: research/archive/avatars-line-screen)
// Integer math and literal tables only; avatars.py produces the same bytes (see tests/fixtures/avatar_vectors.json).

// ---- palette: the dashboard's colors -------------------------------------------------------------------------
// The six tints (the dashboard's) with the tones its themes derive from them (oklch from the tint, frozen as hex):
//            tint       ink        paper      deep       bright    light tint?
const TINT: (string | number)[][] = [
  ["#39ff44", "#001b01", "#ebfaea", "#01680c", "#6cf96c", 1], // green
  ["#0047ff", "#051232", "#f0f5ff", "#1446c2", "#93b7fe", 0], // blue
  ["#b00331", "#250a0c", "#fef2f2", "#a1022c", "#fe9498", 0], // crimson
  ["#d57c22", "#121212", "#eeeeee", "#d57c22", "#d57c22", 1], // burnt orange: only itself plus neutral grounds
  ["#7d3c98", "#1a0f20", "#f9f2fc", "#74338e", "#dc97fb", 0], // purple
  ["#cdbf1f", "#171501", "#f7f6ea", "#635700", "#cdbf1f", 1], // yellow
];
// The ten chat sender colors, hsl(h, 65%, 58%) for h = 210 0 145 280 235 175 300 195 255 100.
const CHAT = ["#4e94da", "#da4e4e", "#4eda88", "#ab4eda", "#4e5ada", "#4edace", "#da4eda", "#4eb7da", "#714eda", "#7dda4e"];
// Per tint: the chat colors at least 50 degrees of hue away (the "guest"); purple (hsl hue 286) also keeps #4e5ada at 51 degrees.
const GUEST = [[0, 1, 3, 4, 5, 6, 7, 8], [1, 2, 3, 6, 9], [0, 2, 3, 4, 5, 7, 8, 9], [0, 2, 3, 4, 5, 6, 7, 8, 9], [0, 1, 2, 4, 5, 7, 9], [0, 1, 2, 3, 4, 5, 6, 7, 8]];

export function idBytes(id: string): number[] {
  const b: number[] = [];
  for (let i = 0; i < 32; i++) b.push(parseInt(id.substr(i * 2, 2), 16));
  return b;
}
const SVG_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">';
export function fill(color: string, d: string): string { return d ? '<path fill="' + color + '" d="' + d + '"/>' : ""; }
export function ground(color: string): string { return fill(color, "M0 0h32v32H0z"); }

// ---- token picture -------------------------------------------------------------------------------------------
// -> [ground, lead, second, third]; eight ground-and-lead modes.
export function palette(b: number[]): string[] {
  const t = b[0] % 6, f = TINT[t] as string[], guest = CHAT[GUEST[t][b[2] % GUEST[t].length]];
  const mode = b[1] % 8, one = f[3] === f[4];              // one: the burnt orange has no deep or bright tone
  if (mode === 0) return [f[1], f[4], guest, f[2]];        // dark ground, tint leads
  if (mode === 1) return [f[1], guest, f[4], f[2]];        // dark ground, guest leads
  if (mode === 2) return [f[2], f[3], guest, f[1]];        // paper ground, tint leads
  if (mode === 3) return [f[2], f[1], f[3], guest];        // paper ground, ink leads
  if (mode === 4) return f[5] ? [f[0], f[1], f[2], guest] : [f[0], f[2], f[1], guest]; // tint ground
  if (mode === 5) return [guest, f[1], f[2], f[4]];        // guest ground, ink leads
  if (mode === 6) return one ? [f[1], f[2], f[0], guest] : [f[3], f[4], guest, f[1]];  // deep ground, bright leads
  return one ? [f[2], f[1], f[0], guest] : [f[4], f[3], guest, f[1]];                  // bright ground, deep leads
}
// A token picture without a frame ends with a faint neutral edge, so a light area at the border holds on a light page.
const EDGE = '<path fill="none" stroke="#121212" opacity=".35" d="M0 0h32v32H0z"/>';

// Rooms: a room seen from above, the far wall off-center, the four walls lit in a pattern.
const ROOM_WALLS = [[1, 0, 0, 0], [1, 1, 0, 0], [1, 0, 1, 0], [1, 2, 0, 0], [1, 0, 2, 0], [1, 2, 1, 2], [1, 1, 2, 2], [1, 1, 1, 0], [1, 2, 3, 0], [1, 3, 0, 0], [1, 0, 3, 0], [3, 1, 3, 1]];
export function rooms(b: number[], col: string[]): string {
  const h = [4, 6, 8][b[3] % 3], lo = h + 4, hi = 28 - h;   // every wall is at least 4 units deep
  const cx = lo + (((hi - lo) * (b[4] % 5)) >> 2), cy = lo + (((hi - lo) * (b[5] % 5)) >> 2); // five places across the room that is left
  const x0 = cx - h, y0 = cy - h, x1 = cx + h, y1 = cy + h;
  const wall = [
    "M0 0H32L" + x1 + " " + y0 + "H" + x0 + "z",
    "M32 0V32L" + x1 + " " + y1 + "V" + y0 + "z",
    "M32 32H0L" + x0 + " " + y1 + "H" + x1 + "z",
    "M0 32V0L" + x0 + " " + y0 + "V" + y1 + "z",
  ];
  const pat = ROOM_WALLS[b[6] % 12], turn = b[7] % 4;
  const d = ["", "", "", ""];
  for (let i = 0; i < 4; i++) d[pat[(i + turn) % 4]] += wall[i];
  let far = [2, 1, 3][b[8] % 3];                            // the far wall's color: not the one three walls already have
  if (Number(pat[0] === far) + Number(pat[1] === far) + Number(pat[2] === far) + Number(pat[3] === far) > 2) far = far % 3 + 1;
  d[far] += "M" + x0 + " " + y0 + "H" + x1 + "V" + y1 + "H" + x0 + "z";
  const m = b[9] % 4, r = h / 2;
  let mark = "";
  if (m === 1) mark = '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="' + col[0] + '"/>';
  if (m === 2) mark = fill(col[0], "M" + cx + " " + (cy - r) + "L" + (cx + r) + " " + cy + "L" + cx + " " + (cy + r) + "L" + (cx - r) + " " + cy + "z");
  if (m === 3) mark = fill(col[0], "M" + (cx - r) + " " + (cy - r) + "h" + h + "v" + h + "h-" + h + "z");
  return fill(col[1], d[1]) + fill(col[2], d[2]) + fill(col[3], d[3]) + mark;
}

// Strata: two or three layers poured across the tile, each with its own edge profile, turned or mirrored one of eight ways.
const STRATA_TURN = ["", "rotate(90 16 16)", "rotate(180 16 16)", "rotate(270 16 16)", "matrix(-1 0 0 1 32 0)", "matrix(0 1 1 0 0 0)", "matrix(1 0 0 -1 0 32)", "matrix(0 -1 -1 0 32 32)"];
const STRATA_Y3 = [[7, 15, 23], [6, 13, 22], [10, 17, 24], [8, 18, 25]];
const STRATA_Y2 = [[10, 21], [8, 18], [13, 23], [11, 19]];
const STRATA_ORDER = [[1, 2, 3], [2, 1, 3], [1, 3, 2], [1, 2, 1]];
export function isqrt(v: number): number { let r = 0; while ((r + 1) * (r + 1) <= v) r++; return r; }
// A profile's height at x = X2 / 2 (X2 odd, 1..63), times 4096, so that everything stays an integer.
export function strataAt(y: number, kind: number, s: number, v: number, X2: number): number {
  if (kind === 1) return 4096 * (X2 < 2 * [10, 16, 22][(v >> 1) % 3] ? y : y + 5 * s);
  if (kind === 2) return 4096 * y + 448 * s * X2;
  if (kind === 3) return 4096 * (X2 > 22 && X2 < 42 ? y + 5 * s : y);
  if (kind === 4) return 4096 * y + 20 * s * X2 * (64 - X2);
  if (kind === 5) return (X2 >> 4) % 2 ? 4096 * (y - 4 * s) + 1024 * s * (X2 & 15) : 4096 * y - 1024 * s * (X2 & 15);
  if (kind === 6) return 4096 * (y + 3 * s * (X2 >> 4));
  if (kind === 7) return X2 > 16 && X2 < 48 ? 4096 * y - s * 2048 * isqrt(256 - (X2 - 32) * (X2 - 32)) : 4096 * y;
  return 4096 * y;
}
// A profile fits when it stays 3 units inside the tile and 3 units below the layer above it, all the way across.
export function strataFits(y: number, kind: number, s: number, v: number, above: number[] | null): boolean {
  for (let X2 = 1; X2 < 64; X2 += 2) {
    const p = strataAt(y, kind, s, v, X2);
    if (p < 12288 || p > 118784) return false;
    if (above && p - strataAt(above[0], above[1], above[2], above[3], X2) < 12288) return false;
  }
  return true;
}
export function strataEdge(y: number, kind: number, s: number, v: number): string {
  if (kind === 1) return "H" + [10, 16, 22][(v >> 1) % 3] + "V" + (y + 5 * s) + "H32";       // one step
  if (kind === 2) return "L32 " + (y + 7 * s);                                               // slope
  if (kind === 3) return "H11v" + 5 * s + "h10v" + -5 * s + "H32";                           // notch or block
  if (kind === 4) return "Q16 " + (y + 10 * s) + " 32 " + y;                                 // sag or swell
  if (kind === 5) return "l8 " + -4 * s + "l8 " + 4 * s + "l8 " + -4 * s + "l8 " + 4 * s;    // zigzag
  if (kind === 6) return "h8v" + 3 * s + "h8v" + 3 * s + "h8v" + 3 * s + "h8";               // cascade
  if (kind === 7) return "H8a8 8 0 0 " + (s > 0 ? 1 : 0) + " 16 0H32";                       // weir crest or basin
  return "H32";                                                                              // level
}
export function strata(b: number[], col: string[]): string {
  const n = 2 + (b[4] % 2);
  const ys = (n === 3 ? STRATA_Y3 : STRATA_Y2)[b[5] % 4], order = STRATA_ORDER[b[6] % 4];
  let out = "", above: number[] | null = null, first: number[] = [0, 0, 1, 0];
  for (let i = 0; i < n; i++) {
    const y = ys[i], v = b[10 + i], s = v % 2 ? -1 : 1;     // s: which way the profile leans
    let kind = b[7 + i] % 8, c = [y, kind, s, v];
    // If the profile would leave a sliver, lean it the other way, else run parallel to the layer above, else lie level.
    if (!strataFits(y, kind, s, v, above)) {
      if (strataFits(y, kind, -s, v, above)) c = [y, kind, -s, v];
      else if (above && strataFits(y, above[1], above[2], above[3], above)) c = [y, above[1], above[2], above[3]];
      else c = [y, 0, 1, 0];
    }
    out += fill(col[order[i]], "M0 " + y + strataEdge(y, c[1], c[2], c[3]) + "V32H0z");
    above = c;
    if (i === 0) first = c;
  }
  const fx = [8, 16, 24][b[14] % 3];                        // the float: only where it clears the tile edge and the first layer
  if (b[13] % 3 === 0 && ys[0] > 7 && strataAt(first[0], first[1], first[2], first[3], 2 * fx + 1) >= 4096 * (ys[0] - 2))
    out += '<circle cx="' + fx + '" cy="' + (ys[0] - 5) + '" r="2" fill="' + col[order[1]] + '"/>';
  const turn = STRATA_TURN[b[3] % 8];
  return turn ? '<g transform="' + turn + '">' + out + "</g>" : out;
}

// Hallmark: a plate struck with a woven line mark on a 3x3 lattice, with one punched node, sometimes framed.
const PLATES = [
  "M3 16a13 13 0 1 0 26 0a13 13 0 1 0-26 0z",   // 0 disc
  "M4 4h24v24H4z",                              // 1 square
  "M16 1L31 16L16 31L1 16z",                    // 2 diamond
  "M10 3h12l7 7v12l-7 7H10l-7-7V10z",           // 3 octagon
  "M4 29V15a12 12 0 0 1 24 0V29z",              // 4 arch
  "M4 4h24v13L16 29L4 17z",                     // 5 shield
  "M16 2l12 7v14l-12 7L4 23V9z",                // 6 hexagon
  "M4 4h17l7 7v17H4z",                          // 7 tab
  "M4 28V15L16 3L28 15V28z",                    // 8 house
  "M4 3h24v14a12 12 0 0 1-24 0z",               // 9 cup
  "M4 4h24v24H11l-7-7z",                        // 10 tab, cut low
  "M0 6h32v20H0z",                              // 11 wide band, runs off both sides
  "M6 0h20v32H6z",                              // 12 tall band
  "M-2 16a18 18 0 1 0 36 0a18 18 0 1 0-36 0z",  // 13 big disc, runs off all four sides
  "M0 0h32v19L19 32H0z",                        // 14 whole tile with one corner cut
  "M8 4h16q0 4 4 4v16q-4 0-4 4H8q0-4-4-4V8q4 0 4-4z", // 15 ticket
  "M11 4h17v24H4V11z",                          // 16 tab, cut top left
  "M4 4h24v17l-7 7H4z",                         // 17 tab, cut low right
  "M0 0h19L32 13v19H0z",                        // 18 whole tile, top right corner cut
  "M13 0h19v32H0V13z",                          // 19 whole tile, top left corner cut
  "M0 0h32v32H13L0 19z",                        // 20 whole tile, low left corner cut
  "M4 4h13l12 12l-12 12H4z",                    // 21 tag pointing right
  "M28 4H15L3 16l12 12h13z",                    // 22 tag pointing left
  "M16 -4L36 32H-4z",                           // 23 big triangle, runs off three sides
];
const PLATE_TIGHT = [2, 4, 5, 8, 9, 21, 22, 23];            // plates that do not cover the whole lattice: the mark is drawn at 3/4 size
const MARK_PARTS = [                                        // ten line groups, each with both mirror axes
  "M10 10H22M10 22H22",                         // 1   rails
  "M10 10V22M22 10V22",                         // 2   posts
  "M16 10V22",                                  // 4   spine
  "M10 16H22",                                  // 8   bar
  "M10 10l12 12m0-12L10 22",                    // 16  X
  "M16 10l6 6-6 6-6-6z",                        // 32  diamond
  "M10 10l12 6-12 6m12-12-12 6 12 6",           // 64  side chevrons
  "M10 10l6 12 6-12m-12 12 6-12 6 12",          // 128 up and down chevrons
  "M13 13h6v6h-6z",                             // 256 small square
  "M10 13v-3h3m6 0h3v3m0 6v3h-3m-6 0h-3v-3",    // 512 corner ticks
];
// Unions of groups that are first widened with X + diamond (the early letter check of Mix v2).
const MARK_WIDEN = [4, 5, 13, 2, 6, 10, 14, 3, 11, 7, 16, 32, 12, 64, 128, 18, 130, 17, 65, 36, 40, 66, 129, 8, 22, 26, 134, 138, 70, 74, 42, 37, 256, 260, 264, 258, 266, 261, 262, 272, 528, 514, 516, 518, 522, 517, 524, 257, 259, 273, 274, 288, 292, 296, 515, 521, 529, 530, 768, 769, 770, 772, 776];
// The 59 marks in use: dense woven ones that read as ornaments. Any other union is swapped for one of these.
const MARK_KEEP = [35, 48, 49, 50, 51, 52, 53, 54, 55, 56, 58, 59, 60, 61, 62, 96, 100, 104, 112, 113, 114, 118, 122, 160, 164, 168, 176, 177, 178, 182, 186, 192, 196, 200, 304, 305, 306, 307, 308, 309, 310, 312, 314, 544, 560, 561, 562, 563, 564, 565, 566, 569, 570, 572, 816, 817, 818, 820, 824];
export function hallmark(b: number[], col: string[]): string {
  let m = (1 << (b[5] % 10)) | (1 << (b[6] % 10));
  if (b[4] % 2) m |= 1 << (b[7] % 4);
  if (MARK_WIDEN.indexOf(m) >= 0) m |= 48;
  if (MARK_KEEP.indexOf(m) < 0) m = MARK_KEEP[(b[5] + b[6]) % 59];
  let d = "", parts = 0;
  for (let i = 0; i < 10; i++) if (m & (1 << i)) { d += MARK_PARTS[i]; parts++; }
  const plate = b[3] % 24, node = b[14] % 9, fr = b[17] % 3;
  let fc = fr + 1;                                          // the frame takes the second or third color, never the paper one
  if (col[fc] === TINT[b[0] % 6][2]) fc = 5 - fc;
  const mark = '<path fill="none" stroke="' + col[0] + '" stroke-width="' + (parts > 2 ? 2 : 3) + '" stroke-linecap="round" stroke-linejoin="round" d="' + d + '"/>'
    + '<circle cx="' + (10 + 6 * (node % 3)) + '" cy="' + (10 + 6 * ((node / 3) | 0)) + '" r="' + (3 + (b[16] % 2)) + '" fill="' + col[2] + '"/>';
  return fill(col[b[15] % 3 ? 1 : 3], PLATES[plate])
    + (PLATE_TIGHT.indexOf(plate) < 0 ? mark : '<g transform="matrix(.75 0 0 .75 4 4)">' + mark + "</g>")
    + (fr ? '<path fill="none" stroke="' + col[fc] + '" stroke-width="2" d="M1 1h30v30H1z"/>' : "");
}

// Of 8 agents, 3 draw Rooms, 3 Strata and 2 Hallmark.
const FAMILY = [rooms, strata, rooms, strata, hallmark, rooms, strata, hallmark];
export function tokenAvatar(id: string): string {
  const b = idBytes(id), col = palette(b);
  const body = FAMILY[b[31] % 8](b, col);
  return SVG_OPEN + ground(col[0]) + body + (body.indexOf('d="M1 1h30') < 0 ? EDGE : "") + "</svg>";
}
