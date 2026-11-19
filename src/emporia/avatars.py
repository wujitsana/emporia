"""Emporia agent pictures. One pure function from the agent's stable id (64 hex chars) to an SVG string:

  token_avatar(agent_id)   Rooms, Strata or Hallmark, flat shapes: the profile picture and the ERC-8004 registration image

(The earlier line-screen "everyday" design is archived outside the repo: research/archive/avatars-line-screen.)
Integer math and literal tables only; avatars.ts produces the same bytes (see tests/fixtures/avatar_vectors.json).
"""

# ---- palette: the dashboard's colors ---------------------------------------------------------------------------
#        tint       ink        paper      deep       bright    light tint?
TINT = [
    ["#39ff44", "#001b01", "#ebfaea", "#01680c", "#6cf96c", 1],  # green
    ["#0047ff", "#051232", "#f0f5ff", "#1446c2", "#93b7fe", 0],  # blue
    ["#b00331", "#250a0c", "#fef2f2", "#a1022c", "#fe9498", 0],  # crimson
    ["#d57c22", "#121212", "#eeeeee", "#d57c22", "#d57c22", 1],  # burnt orange: only itself plus neutral grounds
    ["#7d3c98", "#1a0f20", "#f9f2fc", "#74338e", "#dc97fb", 0],  # purple
    ["#888888", "#141414", "#f5f5f5", "#555555", "#b7b7b7", 1],  # charcoal: no hue, so every chat color is a guest
]
# The ten chat sender colors, hsl(h, 65%, 58%) for h = 210 0 145 280 235 175 300 195 255 100.
CHAT = ["#4e94da", "#da4e4e", "#4eda88", "#ab4eda", "#4e5ada", "#4edace", "#da4eda", "#4eb7da", "#714eda", "#7dda4e"]
# Per tint: the chat colors at least 50 degrees of hue away (the "guest"); purple (hsl hue 286) also keeps #4e5ada at 51 degrees.
GUEST = [[0, 1, 3, 4, 5, 6, 7, 8], [1, 2, 3, 6, 9], [0, 2, 3, 4, 5, 7, 8, 9], [0, 2, 3, 4, 5, 6, 7, 8, 9], [0, 1, 2, 4, 5, 7, 9], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]]

SVG_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'


def id_bytes(agent_id):
    return list(bytes.fromhex(agent_id[:64]))


def fill(color, d):
    return f'<path fill="{color}" d="{d}"/>' if d else ""


def ground(color):
    return fill(color, "M0 0h32v32H0z")


# ---- token picture ---------------------------------------------------------------------------------------------
def palette(b):
    """-> [ground, lead, second, third]; eight ground-and-lead modes."""
    t = b[0] % 6
    f = TINT[t]
    guest = CHAT[GUEST[t][b[2] % len(GUEST[t])]]
    mode, one = b[1] % 8, f[3] == f[4]
    if mode == 0:
        return [f[1], f[4], guest, f[2]]
    if mode == 1:
        return [f[1], guest, f[4], f[2]]
    if mode == 2:
        return [f[2], f[3], guest, f[1]]
    if mode == 3:
        return [f[2], f[1], f[3], guest]
    if mode == 4:
        return [f[0], f[1], f[2], guest] if f[5] else [f[0], f[2], f[1], guest]
    if mode == 5:
        return [guest, f[1], f[2], f[4]]
    if mode == 6:
        return [f[1], f[2], f[0], guest] if one else [f[3], f[4], guest, f[1]]
    return [f[2], f[1], f[0], guest] if one else [f[4], f[3], guest, f[1]]


EDGE = '<path fill="none" stroke="#121212" opacity=".35" d="M0 0h32v32H0z"/>'

ROOM_WALLS = [[1, 0, 0, 0], [1, 1, 0, 0], [1, 0, 1, 0], [1, 2, 0, 0], [1, 0, 2, 0], [1, 2, 1, 2], [1, 1, 2, 2], [1, 1, 1, 0], [1, 2, 3, 0], [1, 3, 0, 0], [1, 0, 3, 0], [3, 1, 3, 1]]


def rooms(b, col):
    h = [4, 6, 8][b[3] % 3]
    lo, hi = h + 4, 28 - h
    cx = lo + (((hi - lo) * (b[4] % 5)) >> 2)
    cy = lo + (((hi - lo) * (b[5] % 5)) >> 2)
    x0, y0, x1, y1 = cx - h, cy - h, cx + h, cy + h
    wall = [
        f"M0 0H32L{x1} {y0}H{x0}z",
        f"M32 0V32L{x1} {y1}V{y0}z",
        f"M32 32H0L{x0} {y1}H{x1}z",
        f"M0 32V0L{x0} {y0}V{y1}z",
    ]
    pat, turn = ROOM_WALLS[b[6] % 12], b[7] % 4
    d = ["", "", "", ""]
    for i in range(4):
        d[pat[(i + turn) % 4]] += wall[i]
    far = [2, 1, 3][b[8] % 3]
    if pat.count(far) > 2:
        far = far % 3 + 1
    d[far] += f"M{x0} {y0}H{x1}V{y1}H{x0}z"
    m, r = b[9] % 4, h // 2
    mark = ""
    if m == 1:
        mark = f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{col[0]}"/>'
    if m == 2:
        mark = fill(col[0], f"M{cx} {cy - r}L{cx + r} {cy}L{cx} {cy + r}L{cx - r} {cy}z")
    if m == 3:
        mark = fill(col[0], f"M{cx - r} {cy - r}h{h}v{h}h-{h}z")
    return fill(col[1], d[1]) + fill(col[2], d[2]) + fill(col[3], d[3]) + mark


STRATA_Y3 = [[7, 15, 23], [6, 13, 22], [10, 17, 24], [8, 18, 25]]
STRATA_Y2 = [[10, 21], [8, 18], [13, 23], [11, 19]]
STRATA_ORDER = [[1, 2, 3], [2, 1, 3], [1, 3, 2], [1, 2, 1]]
STRATA_TURN = ["", "rotate(90 16 16)", "rotate(180 16 16)", "rotate(270 16 16)", "matrix(-1 0 0 1 32 0)", "matrix(0 1 1 0 0 0)", "matrix(1 0 0 -1 0 32)", "matrix(0 -1 -1 0 32 32)"]
def isqrt(v):
    r = 0
    while (r + 1) * (r + 1) <= v:
        r += 1
    return r


def strata_at(y, kind, s, v, x2):
    """A profile's height at x = x2 / 2 (x2 odd, 1..63), times 4096."""
    if kind == 1:
        return 4096 * (y if x2 < 2 * [10, 16, 22][(v >> 1) % 3] else y + 5 * s)
    if kind == 2:
        return 4096 * y + 448 * s * x2
    if kind == 3:
        return 4096 * (y + 5 * s if 22 < x2 < 42 else y)
    if kind == 4:
        return 4096 * y + 20 * s * x2 * (64 - x2)
    if kind == 5:
        return 4096 * (y - 4 * s) + 1024 * s * (x2 & 15) if (x2 >> 4) % 2 else 4096 * y - 1024 * s * (x2 & 15)
    if kind == 6:
        return 4096 * (y + 3 * s * (x2 >> 4))
    if kind == 7:
        return 4096 * y - s * 2048 * isqrt(256 - (x2 - 32) * (x2 - 32)) if 16 < x2 < 48 else 4096 * y
    return 4096 * y


def strata_fits(y, kind, s, v, above):
    for x2 in range(1, 64, 2):
        p = strata_at(y, kind, s, v, x2)
        if p < 12288 or p > 118784:
            return False
        if above and p - strata_at(above[0], above[1], above[2], above[3], x2) < 12288:
            return False
    return True


def strata_edge(y, kind, s, v):
    if kind == 1:
        return f"H{[10, 16, 22][(v >> 1) % 3]}V{y + 5 * s}H32"
    if kind == 2:
        return f"L32 {y + 7 * s}"
    if kind == 3:
        return f"H11v{5 * s}h10v{-5 * s}H32"
    if kind == 4:
        return f"Q16 {y + 10 * s} 32 {y}"
    if kind == 5:
        return f"l8 {-4 * s}l8 {4 * s}l8 {-4 * s}l8 {4 * s}"
    if kind == 6:
        return f"h8v{3 * s}h8v{3 * s}h8v{3 * s}h8"
    if kind == 7:
        return f"H8a8 8 0 0 {1 if s > 0 else 0} 16 0H32"
    return "H32"


def strata(b, col):
    n = 2 + (b[4] % 2)
    ys = (STRATA_Y3 if n == 3 else STRATA_Y2)[b[5] % 4]
    order = STRATA_ORDER[b[6] % 4]
    out, above, first = "", None, None
    for i in range(n):
        y, v = ys[i], b[10 + i]
        s = -1 if v % 2 else 1
        kind = b[7 + i] % 8
        c = [y, kind, s, v]
        if not strata_fits(y, kind, s, v, above):
            if strata_fits(y, kind, -s, v, above):
                c = [y, kind, -s, v]
            elif above and strata_fits(y, above[1], above[2], above[3], above):
                c = [y, above[1], above[2], above[3]]
            else:
                c = [y, 0, 1, 0]
        out += fill(col[order[i]], f"M0 {y}" + strata_edge(y, c[1], c[2], c[3]) + "V32H0z")
        above = c
        if i == 0:
            first = c
    fx = [8, 16, 24][b[14] % 3]
    if b[13] % 3 == 0 and ys[0] > 7 and strata_at(first[0], first[1], first[2], first[3], 2 * fx + 1) >= 4096 * (ys[0] - 2):
        out += f'<circle cx="{fx}" cy="{ys[0] - 5}" r="2" fill="{col[order[1]]}"/>'
    turn = STRATA_TURN[b[3] % 8]
    return f'<g transform="{turn}">{out}</g>' if turn else out


PLATES = [
    "M3 16a13 13 0 1 0 26 0a13 13 0 1 0-26 0z",
    "M4 4h24v24H4z",
    "M16 1L31 16L16 31L1 16z",
    "M10 3h12l7 7v12l-7 7H10l-7-7V10z",
    "M4 29V15a12 12 0 0 1 24 0V29z",
    "M4 4h24v13L16 29L4 17z",
    "M16 2l12 7v14l-12 7L4 23V9z",
    "M4 4h17l7 7v17H4z",
    "M4 28V15L16 3L28 15V28z",
    "M4 3h24v14a12 12 0 0 1-24 0z",
    "M4 4h24v24H11l-7-7z",
    "M0 6h32v20H0z",
    "M6 0h20v32H6z",
    "M-2 16a18 18 0 1 0 36 0a18 18 0 1 0-36 0z",
    "M0 0h32v19L19 32H0z",
    "M8 4h16q0 4 4 4v16q-4 0-4 4H8q0-4-4-4V8q4 0 4-4z",
    "M11 4h17v24H4V11z",
    "M4 4h24v17l-7 7H4z",
    "M0 0h19L32 13v19H0z",
    "M13 0h19v32H0V13z",
    "M0 0h32v32H13L0 19z",
    "M4 4h13l12 12l-12 12H4z",
    "M28 4H15L3 16l12 12h13z",
    "M16 -4L36 32H-4z",
]
PLATE_TIGHT = [2, 4, 5, 8, 9, 21, 22, 23]
MARK_PARTS = [
    "M10 10H22M10 22H22",
    "M10 10V22M22 10V22",
    "M16 10V22",
    "M10 16H22",
    "M10 10l12 12m0-12L10 22",
    "M16 10l6 6-6 6-6-6z",
    "M10 10l12 6-12 6m12-12-12 6 12 6",
    "M10 10l6 12 6-12m-12 12 6-12 6 12",
    "M13 13h6v6h-6z",
    "M10 13v-3h3m6 0h3v3m0 6v3h-3m-6 0h-3v-3",
]
MARK_WIDEN = [4, 5, 13, 2, 6, 10, 14, 3, 11, 7, 16, 32, 12, 64, 128, 18, 130, 17, 65, 36, 40, 66, 129, 8, 22, 26, 134, 138, 70, 74, 42, 37, 256, 260, 264, 258, 266, 261, 262, 272, 528, 514, 516, 518, 522, 517, 524, 257, 259, 273, 274, 288, 292, 296, 515, 521, 529, 530, 768, 769, 770, 772, 776]
MARK_KEEP = [35, 48, 49, 50, 51, 52, 53, 54, 55, 56, 58, 59, 60, 61, 62, 96, 100, 104, 112, 113, 114, 118, 122, 160, 164, 168, 176, 177, 178, 182, 186, 192, 196, 200, 304, 305, 306, 307, 308, 309, 310, 312, 314, 544, 560, 561, 562, 563, 564, 565, 566, 569, 570, 572, 816, 817, 818, 820, 824]


def hallmark(b, col):
    m = (1 << (b[5] % 10)) | (1 << (b[6] % 10))
    if b[4] % 2:
        m |= 1 << (b[7] % 4)
    if m in MARK_WIDEN:
        m |= 48
    if m not in MARK_KEEP:
        m = MARK_KEEP[(b[5] + b[6]) % 59]
    d, parts = "", 0
    for i in range(10):
        if m & (1 << i):
            d += MARK_PARTS[i]
            parts += 1
    plate, node, fr = b[3] % 24, b[14] % 9, b[17] % 3
    fc = fr + 1
    if col[fc] == TINT[b[0] % 6][2]:
        fc = 5 - fc
    mark = (f'<path fill="none" stroke="{col[0]}" stroke-width="{2 if parts > 2 else 3}" stroke-linecap="round" stroke-linejoin="round" d="{d}"/>'
            + f'<circle cx="{10 + 6 * (node % 3)}" cy="{10 + 6 * (node // 3)}" r="{3 + (b[16] % 2)}" fill="{col[2]}"/>')
    return (fill(col[1 if b[15] % 3 else 3], PLATES[plate])
            + (mark if plate not in PLATE_TIGHT else f'<g transform="matrix(.75 0 0 .75 4 4)">{mark}</g>')
            + (f'<path fill="none" stroke="{col[fc]}" stroke-width="2" d="M1 1h30v30H1z"/>' if fr else ""))


FAMILY = [rooms, strata, rooms, strata, hallmark, rooms, strata, hallmark]


def token_avatar(agent_id):
    b = id_bytes(agent_id)
    col = palette(b)
    body = FAMILY[b[31] % 8](b, col)
    return SVG_OPEN + ground(col[0]) + body + (EDGE if 'd="M1 1h30' not in body else "") + "</svg>"
