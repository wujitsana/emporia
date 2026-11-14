"""Tesseract core: the pure rules of the network game (prototype, not registered).

Whole numbers everywhere a verdict is made: no float, no true division, no ``Fraction``.
All chance comes from a seed (bytes; in the real game a drand-derived value) through a
SHA-256 stream, so any party replays the same network, pulses and dark channels.

The board is the edge set of a four-dimensional cube. A corner is a subset of the letters
W, X, Y, Z. The pulse enters at WXYZ, runs to the four inputs (three letters), the six
hubs (two letters) and leaves by the four outputs (one letter). The 24 channels are the
edges between inputs and hubs and between hubs and outputs; channel ids name both ends
(``XYZ>XY``, ``XY>X``). Order is fixed: input-to-hub channels first (inputs in the order
XYZ, WYZ, WXZ, WXY; hubs WX, WY, WZ, XY, XZ, YZ), then hub-to-output channels (hubs in
order, outputs W, X, Y, Z).

Each player holds a private target mix. A player's points for an epoch say how much its
own pushes moved the mix toward its target, measured from both ends (added first and
added last). See ``epoch_points``.

(History: this replaces the earlier layered prototype; nothing of that design is kept.)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations

GAME_ID = "emporia:tesseract:v1"
AXES = "WXYZ"
BP = 10000

_INPUTS = tuple("".join(a for a in AXES if a != x) for x in AXES)  # XYZ, WYZ, WXZ, WXY
_HUBS = tuple("".join(c) for c in combinations(AXES, 2))  # WX, WY, WZ, XY, XZ, YZ
_OUTPUTS = tuple(AXES)


@dataclass(frozen=True)
class Rules:
    samples: int = 16  # pulses per epoch; BP // samples is the per-pulse unit (625)
    pulse_max: int = 9  # each input carries a whole number 0..pulse_max per pulse
    width_min: int = 0  # must stay >= 0: the forward pass has no cut
    width_cap: int = 20
    open_lo: int = 1  # opening widths uniform in open_lo..open_hi
    open_hi: int = 20
    band_lo: int = 1500  # opening mix: every output inside [band_lo, band_hi] bp
    band_hi: int = 3500
    open_attempts: int = 256
    push_budget: int = 8
    push_channels: int = 4
    dark_den: int = 24  # a channel is dark with chance 1 in dark_den
    epochs: int = 5
    target_l1: int = 40  # percent: every target is this far (L1) from 25/25/25/25
    target_min: int = 10
    target_max: int = 40
    # Depth experiments (defaults reproduce the plan's rules):
    score_power: int = 1  # 1: sum of gaps; 2: sum of squared gaps
    unit_cap: int = 0  # at most this many units on one channel in a move (0: no limit)
    score_mode: str = "first_last"  # "first_last" | "last" | "min" (experiments only)


DEFAULT = Rules()


# -- board ---------------------------------------------------------------------


@lru_cache(maxsize=None)
def topology() -> tuple[tuple[str, ...], tuple[tuple[int, int, int, str], ...]]:
    """(node ids, channels as (layer, from_index, to_index, id)) in the fixed order.

    Node indices are per layer: inputs 0-3, hubs 0-5, outputs 0-3. ``node ids`` lists the
    corner names in the order inputs, hubs, outputs.
    """
    ch = []
    for i, inp in enumerate(_INPUTS):
        for j, hub in enumerate(_HUBS):
            if set(hub) <= set(inp):
                ch.append((0, i, j, f"{inp}>{hub}"))
    for i, hub in enumerate(_HUBS):
        for j, out in enumerate(_OUTPUTS):
            if out in hub:
                ch.append((1, i, j, f"{hub}>{out}"))
    return _INPUTS + _HUBS + _OUTPUTS, tuple(ch)


@lru_cache(maxsize=None)
def channel_ids() -> tuple[str, ...]:
    return tuple(c[3] for c in topology()[1])


@lru_cache(maxsize=None)
def channel_index() -> dict[str, int]:
    return {cid: n for n, cid in enumerate(channel_ids())}


def n_channels() -> int:
    return len(channel_ids())


@lru_cache(maxsize=None)
def _layers():
    out = ([], [])
    for n, (layer, i, j, _) in enumerate(topology()[1]):
        out[layer].append((n, i, j))
    return tuple(out[0]), tuple(out[1])


# -- seeded stream ---------------------------------------------------------------


class Stream:
    """Deterministic bytes from a seed and a label; unbiased draws by rejection."""

    def __init__(self, seed: bytes, label: str):
        self._seed = bytes(seed)
        self._label = label.encode()
        self._counter = 0
        self._buf = b""
        self._pos = 0

    def _byte(self) -> int:
        if self._pos >= len(self._buf):
            self._buf = hashlib.sha256(
                self._seed + b"|" + self._label + b"|" + str(self._counter).encode()
            ).digest()
            self._counter += 1
            self._pos = 0
        b = self._buf[self._pos]
        self._pos += 1
        return b

    def below(self, n: int) -> int:
        """Uniform integer in [0, n) for 1 <= n <= 256."""
        if not 1 <= n <= 256:
            raise ValueError("n out of range")
        limit = 256 - (256 % n)
        while True:
            b = self._byte()
            if b < limit:
                return b % n


def draw_pulses(seed: bytes, epoch: int, rules: Rules = DEFAULT) -> tuple[tuple[int, ...], ...]:
    """``samples`` pulses, each a whole number 0..pulse_max on each of the four inputs.
    Epoch 0 is the opening pulses."""
    st = Stream(seed, f"pulse/{epoch}")
    return tuple(
        tuple(st.below(rules.pulse_max + 1) for _ in range(4)) for _ in range(rules.samples)
    )


def draw_dark(seed: bytes, epoch: int, rules: Rules = DEFAULT) -> tuple[bool, ...]:
    """Which channels are dark this epoch, each independently with chance 1 in dark_den."""
    n = n_channels()
    if rules.dark_den <= 0:
        return (False,) * n
    st = Stream(seed, f"dark/{epoch}")
    return tuple(st.below(rules.dark_den) == 0 for _ in range(n))


def open_network(seed: bytes, rules: Rules = DEFAULT) -> tuple[tuple[int, ...], int]:
    """(widths, attempt). The first attempt whose opening mix (the epoch-0 pulses, nothing
    dark) has every output inside the band is the opening; after ``open_attempts`` refusals
    the last draw stands and the attempt is -1."""
    span = rules.open_hi - rules.open_lo + 1
    n = n_channels()
    pulses0 = draw_pulses(seed, 0, rules)
    w: tuple[int, ...] = ()
    for a in range(rules.open_attempts):
        st = Stream(seed, f"open/{a}")
        w = tuple(st.below(span) + rules.open_lo for _ in range(n))
        if all(rules.band_lo <= x <= rules.band_hi for x in mix(w, pulses0, None, rules)):
            return w, a
    return w, -1


# -- targets ---------------------------------------------------------------------


@lru_cache(maxsize=None)
def targets(rules: Rules = DEFAULT) -> tuple[tuple[int, int, int, int], ...]:
    """Every whole-percent mix summing to 100, each part target_min..target_max, at exactly
    target_l1 from 25/25/25/25. Sorted, so an index names one (726 by default)."""
    out = []
    rng = range(rules.target_min, rules.target_max + 1)
    for a in rng:
        for b in rng:
            for c in rng:
                d = 100 - a - b - c
                if d < rules.target_min or d > rules.target_max:
                    continue
                if abs(a - 25) + abs(b - 25) + abs(c - 25) + abs(d - 25) == rules.target_l1:
                    out.append((a, b, c, d))
    return tuple(sorted(out))


def deal_target(draw: bytes, rules: Rules = DEFAULT) -> tuple[int, int, int, int]:
    """A seat's target from its private draw (a digest): the big-endian integer modulo the
    number of targets."""
    t = targets(rules)
    return t[int.from_bytes(draw, "big") % len(t)]


# -- moves -----------------------------------------------------------------------


def validate_move(move: dict, rules: Rules = DEFAULT) -> dict:
    """A move is ``{"push": {channel: whole number}}``. At most ``push_channels`` channels,
    ``sum(|push|) <= push_budget``, at most ``unit_cap`` on one channel when set. Zero entries
    are dropped. Returns the cleaned move; raises ValueError."""
    if not isinstance(move, dict) or set(move) != {"push"} or not isinstance(move["push"], dict):
        raise ValueError('a move is {"push": {...}}')
    ids = channel_index()
    clean: dict[str, int] = {}
    total = 0
    for cid, v in move["push"].items():
        if cid not in ids:
            raise ValueError(f"unknown channel {cid!r}")
        if type(v) is not int:
            raise ValueError("pushes are whole numbers")
        if rules.unit_cap and abs(v) > rules.unit_cap:
            raise ValueError("over the per-channel limit")
        if v:
            clean[cid] = v
            total += abs(v)
    if len(clean) > rules.push_channels:
        raise ValueError("too many channels")
    if total > rules.push_budget:
        raise ValueError("over budget")
    return {"push": clean}


def apply_pushes(
    widths: tuple[int, ...], moves: list[dict], rules: Rules = DEFAULT
) -> tuple[int, ...]:
    """New width = old width + the sum of all pushes on that channel, clamped to
    width_min..width_cap."""
    idx = channel_index()
    w = list(widths)
    for mv in moves:
        for cid, v in mv["push"].items():
            w[idx[cid]] += v
    return tuple(max(rules.width_min, min(rules.width_cap, x)) for x in w)


# -- forward pass ----------------------------------------------------------------


def forward(
    widths: tuple[int, ...],
    pulse: tuple[int, ...],
    dark: tuple[bool, ...] | None,
    rules: Rules = DEFAULT,
) -> list[int]:
    """Amounts reaching the four outputs for one pulse. A hub's amount is the sum of
    width * input over its channels; an output's is the same over the hubs. A dark channel
    counts as width 0 for this epoch only."""
    l0, l1 = _layers()
    hubs = [0] * 6
    for n, i, j in l0:
        if not (dark and dark[n]):
            hubs[j] += widths[n] * pulse[i]
    outs = [0] * 4
    for n, i, j in l1:
        if not (dark and dark[n]):
            outs[j] += widths[n] * hubs[i]
    return outs


def mix(
    widths: tuple[int, ...],
    pulses: tuple[tuple[int, ...], ...],
    dark: tuple[bool, ...] | None,
    rules: Rules = DEFAULT,
) -> tuple[int, int, int, int]:
    """The epoch's mix in basis points: for each pulse ``unit * out_k // sum(out)`` (156 each
    when all four are 0), summed over the pulses. Adds up to slightly under 10000 (floors)."""
    unit = BP // len(pulses)
    acc = [0, 0, 0, 0]
    for p in pulses:
        amt = forward(widths, p, dark, rules)
        tot = sum(amt)
        if tot == 0:
            for k in range(4):
                acc[k] += unit // 4
        else:
            for k in range(4):
                acc[k] += unit * amt[k] // tot
    return (acc[0], acc[1], acc[2], acc[3])


def distance(mix_bp, target_pct, rules: Rules = DEFAULT) -> int:
    """Sum over the four outputs of the gap (basis points) between a mix and a target given
    in percent; each gap squared when ``score_power`` is 2."""
    total = 0
    for m, t in zip(mix_bp, target_pct):
        g = abs(m - 100 * t)
        total += g * g if rules.score_power == 2 else g
    return total


# -- scoring ---------------------------------------------------------------------


def epoch_points(
    widths_before: tuple[int, ...],
    moves: dict[str, dict],
    targets_by_player: dict[str, tuple],
    pulses,
    dark,
    rules: Rules = DEFAULT,
) -> dict[str, dict]:
    """``{player: {"first": f, "last": l, "points": p}}``; the same pulses and dark
    channels in every pass.

    - first = D(mix(W)) - D(mix(W + only this player's pushes)): added first.
    - last  = D(mix(W + everyone else)) - D(mix(W + everyone)): added last (leave-one-out).
    - points = first + last. An empty push scores 0. Points can be negative.
    ``score_mode`` "last" or "min" (experiments) uses last, or 2 * min(first, last).
    """
    base = mix(widths_before, pulses, dark, rules)
    full = mix(apply_pushes(widths_before, list(moves.values()), rules), pulses, dark, rules)
    out = {}
    for p, mv in moves.items():
        if not mv["push"]:
            out[p] = {"first": 0, "last": 0, "points": 0}
            continue
        t = targets_by_player[p]
        alone = mix(apply_pushes(widths_before, [mv], rules), pulses, dark, rules)
        rest = [m for q, m in moves.items() if q != p]
        without = mix(apply_pushes(widths_before, rest, rules), pulses, dark, rules)
        first = distance(base, t, rules) - distance(alone, t, rules)
        last = distance(without, t, rules) - distance(full, t, rules)
        if rules.score_mode == "last":
            pts = last
        elif rules.score_mode == "min":
            pts = 2 * min(first, last)
        else:
            pts = first + last
        out[p] = {"first": first, "last": last, "points": pts}
    return out


# -- what a player is shown ------------------------------------------------------


def pull(
    widths: tuple[int, ...], pulses, rules: Rules = DEFAULT, step: int = 4
) -> dict[str, dict]:
    """``{channel: {"up": (dW, dX, dY, dZ), "down": (...)}}``: the change of each output's
    share (basis points) for +step and -step on that channel alone, nothing dark. A push the
    clamp would cut is reported as the clamped effect."""
    base = mix(widths, pulses, None, rules)
    out = {}
    for n, cid in enumerate(channel_ids()):
        row = {}
        for name, sign in (("up", 1), ("down", -1)):
            w = list(widths)
            w[n] = max(rules.width_min, min(rules.width_cap, w[n] + sign * step))
            m = mix(tuple(w), pulses, None, rules)
            row[name] = tuple(a - b for a, b in zip(m, base))
        out[cid] = row
    return out


def toward(pull_row: dict, mix_bp, target_pct, rules: Rules = DEFAULT) -> dict[str, int]:
    """Client-side: the drop in distance to the target, per direction, if the mix moved by
    that row's change. ``{"up": gain, "down": gain}``."""
    here = distance(mix_bp, target_pct, rules)
    return {
        k: here - distance(tuple(a + b for a, b in zip(mix_bp, pull_row[k])), target_pct, rules)
        for k in ("up", "down")
    }


# -- an epoch and a game ---------------------------------------------------------


@dataclass
class EpochPublic:
    """Everything published after an epoch resolves, who pushed what included (at once).
    Not here: targets, scores."""

    epoch: int
    pulses: tuple
    dark: tuple
    widths_before: tuple
    widths_after: tuple
    mix: tuple
    pushes: dict  # {player: {channel: amount}}
    pull: dict  # for the NEXT epoch, on this epoch's pulses, nothing dark ({} if not asked)


def play_epoch(
    seed: bytes,
    epoch: int,
    widths: tuple[int, ...],
    moves: dict[str, dict],
    rules: Rules = DEFAULT,
    with_pull: bool = True,
) -> EpochPublic:
    pulses = draw_pulses(seed, epoch, rules)
    dark = draw_dark(seed, epoch, rules)
    after = apply_pushes(widths, list(moves.values()), rules)
    return EpochPublic(
        epoch=epoch,
        pulses=pulses,
        dark=dark,
        widths_before=tuple(widths),
        widths_after=after,
        mix=mix(after, pulses, dark, rules),
        pushes={p: dict(m["push"]) for p, m in moves.items()},
        pull=pull(after, pulses, rules) if with_pull else {},
    )


def play_game(
    seed0: bytes,
    epoch_seeds: list[bytes],
    epoch_moves: list[dict[str, dict]],
    targets_by_player: dict[str, tuple],
    rules: Rules = DEFAULT,
) -> tuple[list[EpochPublic], list[dict], dict[str, int]]:
    """Replay a whole game: (public record, per-epoch points, totals). Moves are validated."""
    widths, _ = open_network(seed0, rules)
    publics, points = [], []
    totals = {p: 0 for p in targets_by_player}
    for e, (seed, moves) in enumerate(zip(epoch_seeds, epoch_moves), start=1):
        moves = {p: validate_move(m, rules) for p, m in moves.items()}
        pub = play_epoch(seed, e, widths, moves, rules)
        pts = epoch_points(widths, moves, targets_by_player, pub.pulses, pub.dark, rules)
        for p, d in pts.items():
            totals[p] += d["points"]
        publics.append(pub)
        points.append(pts)
        widths = pub.widths_after
    return publics, points, totals


def winners(totals: dict[str, int]) -> list[str]:
    """Highest total wins; equal totals tie."""
    top = max(totals.values())
    return sorted(p for p, t in totals.items() if t == top)


def table_index(totals: dict[str, int]) -> dict[str, int]:
    """``1000 * n * max(total, 0) // sum of max(total, 0)``; 0 for everyone if none is
    positive. An average seat is 1000 at any table size."""
    pos = sum(max(t, 0) for t in totals.values())
    n = len(totals)
    return {p: (1000 * n * max(t, 0) // pos if pos else 0) for p, t in totals.items()}
