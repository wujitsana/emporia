"""Emporia agent pictures: "Mix v2, restored: sliver edges scaled, all 135 marks". One pure function from the agent's
stable id (64 hex chars) to an SVG string:

  token_avatar(agent_id)   Rooms (3 agents in 8), Strata (3 in 8) or Hallmark (2 in 8), flat shapes: the profile picture
                           and the ERC-8004 registration image

Strata: a shaped edge that would leave a sliver is drawn smaller (same kind, a lower amplitude or radius), never flipped,
copied or flattened. Hallmark: v2's 24 plates and all 135 marks.
(The earlier line-screen "everyday" design is archived outside the repo: research/archive/avatars-line-screen.)
Integer math and literal tables only; avatars.ts produces the same bytes (see tests/fixtures/avatar_vectors.json).
"""

# ---- palette: the dashboard's six tints, with exactly the colors its themes show (themePalette.ts, badges/tokens.ts) ------
# ink = the dark theme's page background, paper = the light theme's page background, deep = the light theme's accent,
# bright = the dark theme's accent. avatars.test.ts holds these rows to the theme code, so they cannot drift.
#        tint       ink        paper      deep       bright    light tint?
TINT = [
    ["#39ff44", "#001b00", "#ebfaea", "#006c00", "#39ff44", 1],  # green
    ["#0047ff", "#051232", "#ecf6ff", "#1446c2", "#8bb6ff", 0],  # blue
    ["#b00331", "#250a0c", "#fff0f0", "#a50029", "#ff8b91", 0],  # crimson
    ["#d57c22", "#201004", "#fdf3ec", "#8c3900", "#fb9f4c", 1],  # burnt orange
    ["#7d3c98", "#1a0f20", "#f9f2fc", "#74338e", "#dc97fb", 0],  # purple
    ["#888888", "#141414", "#ececec", "#555555", "#b7b7b7", 1],  # charcoal
]
SVG_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'


def id_bytes(agent_id):
    return list(bytes.fromhex(agent_id[:64]))


def fill(color, d):
    return '<path fill="%s" d="%s"/>' % (color, d) if d else ""


def ground(color):
    return fill(color, "M0 0h32v32H0z")


def close(col):
    return ('<path fill="none" stroke="%s" stroke-opacity=".5" stroke-width=".5" d="M.25 .25h31.5v31.5h-31.5z"/>' % col[3] if len(col) > 4 and col[4] else "") + "</svg>"


def luma(h):
    """The color's luma, 2126 R + 7152 G + 722 B on the 0-255 values (integer: the TypeScript port does the same)."""
    return 2126 * int(h[1:3], 16) + 7152 * int(h[3:5], 16) + 722 * int(h[5:7], 16)


GUEST_GAP = 765000   # the accent's luma must differ from the ground by 30% of the full range (2,550,000)
GUEST_GAP2 = 382500  # and from every other color of the picture by 15%
MID = 1275000        # the ground's luma at the 50% midpoint: above it the ground is light
# The accent family, two tones per hue: [pastel for dark and mid grounds, deep partner for light grounds].
FAMILY = [["#69cdff", "#005e8b"], ["#72e072", "#006c00"], ["#ffad33", "#814400"], ["#ffbeff", "#743c76"], ["#888888", "#555555"]]  # blue green orange pink charcoal
# Per tint (green blue crimson orange purple charcoal): the family hue to skip (crimson and purple skip pink).
OWN = [1, 0, 3, 2, 3, 4]


def guest_color(b):
    """The accent. Ground light (luma over MID) -> the deep tone, else the pastel. Of the four hues but the picture's own
    (charcoal aside), those clear of the ground by GUEST_GAP and of every other color by GUEST_GAP2, picked by b[2]; else
    charcoal if clear; else a hue clear of the ground only; else the tint's own deep or bright shade clear of the ground;
    else the candidate with the largest smallest gap."""
    t, mode = b[0] % 6, b[1] % 8
    f = TINT[t]
    others = [[f[1], f[4], f[2]], [f[1], f[4], f[2]], [f[2], f[3], f[1]], [f[2], f[1], f[3]],
              [f[0], f[1], f[2]] if f[5] else [f[0], f[2], f[1]], [f[1], f[2], f[4]], [f[3], f[4], f[1]], [f[4], f[3], f[1]]][mode]
    tone = 1 if luma(others[0]) >= MID else 0

    def gap(c, r):
        return abs(luma(c) - luma(r))

    def clear(c):
        return gap(c, others[0]) >= GUEST_GAP and all(gap(c, r) >= GUEST_GAP2 for r in others[1:])

    hues = [FAMILY[i][tone] for i in range(4) if i != OWN[t]]
    ok = [c for c in hues if clear(c)]
    if ok:
        return ok[b[2] % len(ok)]
    gray = FAMILY[4][tone]
    if OWN[t] != 4 and clear(gray):
        return gray
    ok = [c for c in hues if gap(c, others[0]) >= GUEST_GAP]
    if ok:
        return ok[b[2] % len(ok)]
    ok = [c for c in (f[3], f[4]) if gap(c, others[0]) >= GUEST_GAP]
    if ok:
        return ok[b[2] % len(ok)]
    return max(hues + [gray, f[3], f[4]], key=lambda c: min(gap(c, r) for r in others))


def palette2(b):
    t = b[0] % 6
    f = TINT[t]
    guest = guest_color(b)
    mode = b[1] % 8
    if mode == 0:
        return [f[1], f[4], guest, f[2]]
    if mode == 1:
        return [f[1], guest, f[4], f[2]]
    if mode == 2:
        return [f[2], f[3], guest, f[1], 1]
    if mode == 3:
        return [f[2], f[1], f[3], guest, 1]
    if mode == 4:
        return [f[0], f[1], f[2], guest] if f[5] else [f[0], f[2], f[1], guest]
    if mode == 5:
        return [guest, f[1], f[2], f[4]]
    if mode == 6:
        return [f[3], f[4], guest, f[1]]
    return [f[4], f[3], guest, f[1]]


def num(v):
    return str(int(v)) if v == int(v) else str(v)


ROOM_WALLS = [[1, 0, 0, 0], [1, 1, 0, 0], [1, 0, 1, 0], [1, 2, 0, 0], [1, 0, 2, 0], [1, 2, 1, 2], [1, 1, 2, 2], [1, 1, 1, 0], [1, 2, 3, 0], [1, 3, 0, 0], [1, 0, 3, 0], [3, 1, 3, 1]]


def rooms2(i):
    b = id_bytes(i)
    col = palette2(b)
    h = [4, 6, 8][b[3] % 3]
    lo, hi = h + 2, 30 - h
    cx = [8, 12, 16, 20, 24][b[4] % 5]
    cy = [8, 12, 16, 20, 24][b[5] % 5]
    cx = lo if cx < lo else hi if cx > hi else cx
    cy = lo if cy < lo else hi if cy > hi else cy
    x0, y0, x1, y1 = cx - h, cy - h, cx + h, cy + h
    wall = [
        "M0 0L32 0L%d %dL%d %dz" % (x1, y0, x0, y0),
        "M32 0L32 32L%d %dL%d %dz" % (x1, y1, x1, y0),
        "M32 32L0 32L%d %dL%d %dz" % (x0, y1, x1, y1),
        "M0 32L0 0L%d %dL%d %dz" % (x0, y0, x0, y1),
    ]
    pat = ROOM_WALLS[b[6] % 12]
    turn = b[7] % 4
    d = ["", "", "", ""]
    for k in range(4):
        d[pat[(k + turn) % 4]] += wall[k]
    d[[2, 1, 3][b[8] % 3]] += "M%d %dH%dV%dH%dz" % (x0, y0, x1, y1, x0)
    m = b[9] % 4
    r = h // 2
    mark = ""
    if m == 1:
        mark = '<circle cx="%d" cy="%d" r="%d" fill="%s"/>' % (cx, cy, r, col[0])
    if m == 2:
        mark = fill(col[0], "M%d %dL%d %dL%d %dL%d %dz" % (cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy))
    if m == 3:
        mark = fill(col[0], "M%d %dh%dv%dh-%dz" % (cx - r, cy - r, h, h, h))
    return SVG_OPEN + ground(col[0]) + fill(col[1], d[1]) + fill(col[2], d[2]) + fill(col[3], d[3]) + mark + close(col)


STRATA_Y3 = [[7, 15, 23], [6, 13, 22], [10, 17, 24], [8, 18, 25]]
STRATA_Y2 = [[10, 21], [8, 18], [13, 23], [11, 19]]
STRATA_ORDER = [[1, 2, 3], [2, 1, 3], [1, 3, 2], [1, 2, 1]]
AMP = [None, [5, 4, 3, 2], [7, 5, 4, 2], [5, 4, 3, 2], [10, 8, 6, 4], [4, 3, 2, 1], [3, 2, 1], [8, 6, 4, 2]]


def isqrt(v):
    r = 0
    while (r + 1) * (r + 1) <= v:
        r += 1
    return r


def strata_at(y, kind, s, v, a, X2):
    if kind == 1:
        return 4096 * (y if X2 < 2 * [10, 16, 22][(v >> 1) % 3] else y + a * s)
    if kind == 2:
        return 4096 * y + 64 * a * s * X2
    if kind == 3:
        return 4096 * (y + a * s if 22 < X2 < 42 else y)
    if kind == 4:
        return 4096 * y + 2 * a * s * X2 * (64 - X2)
    if kind == 5:
        return 4096 * (y - a * s) + 256 * a * s * (X2 & 15) if (X2 >> 4) % 2 else 4096 * y - 256 * a * s * (X2 & 15)
    if kind == 6:
        return 4096 * (y + a * s * (X2 >> 4))
    if kind == 7:
        return 4096 * y - s * 2048 * isqrt(4 * a * a - (X2 - 32) * (X2 - 32)) if 32 - 2 * a < X2 < 32 + 2 * a else 4096 * y
    return 4096 * y


def height(c, X2):
    return strata_at(c["y"], c["kind"], c["s"], c["v"], c["a"], X2) if c["kind"] else 4096 * c["y"]


def fits(c, above):
    for X2 in range(1, 64, 2):
        p = height(c, X2)
        if p < 12288 or p > 118784:
            return False
        if above and p - height(above, X2) < 12288:
            return False
    return True


def edge(c):
    y, kind, s, v, a = c["y"], c["kind"], c["s"], c["v"], c["a"]
    if kind == 1:
        return "H%dV%dH32" % ([10, 16, 22][(v >> 1) % 3], y + a * s)
    if kind == 2:
        return "L32 %d" % (y + a * s)
    if kind == 3:
        return "H11v%dh10v%dH32" % (a * s, -a * s)
    if kind == 4:
        return "Q16 %d 32 %d" % (y + a * s, y)
    if kind == 5:
        return "l8 %dl8 %dl8 %dl8 %d" % (-a * s, a * s, -a * s, a * s)
    if kind == 6:
        return "h8v%dh8v%dh8v%dh8" % (a * s, a * s, a * s)
    if kind == 7:
        return "H%da%d %d 0 0 %d %d 0H32" % (16 - a, a, a, 1 if s > 0 else 0, 2 * a)
    return "H32"


def _opts(kind, y, v):
    s = -1 if v % 2 else 1
    if not kind:
        return [{"y": y, "kind": 0, "s": 1, "v": 0, "a": 0, "lv": 0}]
    return [{"y": y, "kind": kind, "s": s, "v": v, "a": a, "lv": lv} for lv, a in enumerate(AMP[kind])]


def plan(kinds, ys, vs):
    n = len(ys)

    def solve(i, above):
        if i == n:
            return []
        for c in _opts(kinds[i], ys[i], vs[i]):
            if not fits(c, above):
                continue
            rest = solve(i + 1, c)
            if rest is not None:
                return [c] + rest
        return None

    sol = solve(0, None)
    if sol is not None:
        return sol
    out, above = [], None
    for i in range(n):
        c = {"y": ys[i], "kind": 0, "s": 1, "v": 0, "a": 0, "lv": 0, "flat": bool(kinds[i])}
        if kinds[i]:
            for t in _opts(kinds[i], ys[i], vs[i]):
                if fits(t, above):
                    c = t
                    break
        out.append(c)
        above = c
    return out


def strata_draw(i, stat=None):
    b = id_bytes(i)
    col = palette2(b)
    n = 2 + (b[4] % 2)
    ys = (STRATA_Y3 if n == 3 else STRATA_Y2)[b[5] % 4][:n]
    order = STRATA_ORDER[b[6] % 4]
    kinds = [b[7 + k] % 8 for k in range(n)]
    vs = [b[10 + k] for k in range(n)]
    pl = plan(kinds, ys, vs)
    out = ""
    for k in range(n):
        c = pl[k]
        if stat is not None and kinds[k]:
            stat["asked"] += 1
            stat["flat" if c.get("flat") else "scaled" if c["lv"] else "kept"] += 1
        out += fill(col[order[k]], "M0 %d%sV32H0z" % (ys[k], edge(c)))
    if b[13] % 3 == 0:
        out += '<circle cx="%d" cy="%d" r="2" fill="%s"/>' % ([8, 16, 24][b[14] % 3], ys[0] - 5, col[order[1]])
    turn = (b[3] % 4) * 90
    return SVG_OPEN + ground(col[0]) + ('<g transform="rotate(%d 16 16)">%s</g>' % (turn, out) if turn else out) + close(col)


PLATES2 = [
    "M3 16a13 13 0 1 0 26 0a13 13 0 1 0-26 0z", "M4 4h24v24H4z", "M16 1L31 16L16 31L1 16z", "M10 3h12l7 7v12l-7 7H10l-7-7V10z",
    "M4 29V15a12 12 0 0 1 24 0V29z", "M4 4h24v13L16 29L4 17z", "M16 2l12 7v14l-12 7L4 23V9z", "M4 4h17l7 7v17H4z",
    "M4 28V15L16 3L28 15V28z", "M4 3h24v14a12 12 0 0 1-24 0z", "M4 4h24v24H11l-7-7z", "M0 6h32v20H0z", "M6 0h20v32H6z",
    "M-2 16a18 18 0 1 0 36 0a18 18 0 1 0-36 0z", "M0 0h32v19L19 32H0z",
    "M8 4h16a4 4 0 0 0 4 4v16a4 4 0 0 0-4 4H8a4 4 0 0 0-4-4V8a4 4 0 0 0 4-4z", "M11 4h17v24H4V11z", "M4 4h24v17l-7 7H4z",
    "M0 0h19L32 13v19H0z", "M13 0h19v32H0V13z", "M0 0h32v32H13L0 19z", "M4 4h13l12 12l-12 12H4z", "M28 4H15L3 16l12 12h13z",
    "M16 -4L36 32H-4z",
]
MARK2_PARTS = [
    "M10 10H22M10 22H22", "M10 10V22M22 10V22", "M16 10V22", "M10 16H22", "M10 10L22 22M22 10L10 22",
    "M16 10L22 16L16 22L10 16z", "M10 10L22 16L10 22M22 10L10 16L22 22", "M10 10L16 22L22 10M10 22L16 10L22 22",
    "M13 13h6v6h-6z", "M10 13V10h3M19 10h3v3M22 19v3h-3M13 22h-3v-3",
]
MARK2_BAD = [4, 5, 13, 2, 6, 10, 14, 3, 11, 7, 16, 32, 12, 64, 128, 18, 130, 17, 65, 36, 40, 66, 129, 8, 22, 26, 134, 138, 70, 74, 42, 37, 256, 260, 264, 258, 266, 261, 262, 272, 528, 514, 516, 518, 522, 517, 524, 257, 259, 273, 274, 288, 292, 296, 515, 521, 529, 530, 768, 769, 770, 772, 776]


def hallmark2(i):
    b = id_bytes(i)
    col = palette2(b)
    m = (1 << (b[5] % 10)) | (1 << (b[6] % 10))
    if b[4] % 2:
        m |= 1 << (b[7] % 4)
    if m in MARK2_BAD:
        m |= 48
    d, parts = "", 0
    for k in range(10):
        if m & (1 << k):
            d += MARK2_PARTS[k]
            parts += 1
    node, fr = b[14] % 9, b[17] % 3
    return (SVG_OPEN + ground(col[0]) + fill(col[1 if b[15] % 3 else 3], PLATES2[b[3] % 24])
            + '<path fill="none" stroke="%s" stroke-width="%d" stroke-linecap="round" stroke-linejoin="round" d="%s"/>' % (col[0], 2 if parts > 2 else 3, d)
            + '<circle cx="%d" cy="%d" r="%d" fill="%s"/>' % (10 + 6 * (node % 3), 10 + 6 * (node // 3), 3 + (b[16] % 2), col[2])
            + ('<path fill="none" stroke="%s" stroke-width="2" d="M1 1h30v30H1z"/></svg>' % col[fr + 1] if fr else close(col)))


# Of 8 agents, 3 draw Rooms, 3 Strata and 2 Hallmark.
def token_avatar(agent_id):
    f = id_bytes(agent_id)[31] % 8
    if f in (4, 7):
        return hallmark2(agent_id)
    if f in (1, 3, 6):
        return strata_draw(agent_id)
    return rooms2(agent_id)
