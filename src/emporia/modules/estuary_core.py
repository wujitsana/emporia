"""Estuary core: the pure rules of the network game (prototype, not registered).

Whole numbers everywhere a verdict is made. Shares are ``Fraction``; no float, no
exponent. All chance comes from a seed (bytes, in the real game a beacon round) through
a SHA-256 stream, so any party replays the same network, rain and silt.

Layout: four layers (4 sources, two layers of 5 pools, ``mouths`` mouths); every node
feeds every node of the next layer. Widths are in a fixed channel order: layer by layer,
from-node major.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from fractions import Fraction
from functools import lru_cache

LAYER_LETTERS = "abcd"


@dataclass(frozen=True)
class Rules:
    mouths: int = 3
    sources: int = 4
    pools: int = 5
    samples: int = 16
    rain_max: int = 9
    width_cap: int = 20
    push_budget: int = 12
    push_channels: int = 6
    silt_den: int = 8  # each channel is blocked with probability 1/silt_den; 0 = no silt
    crowd_pow: int = 1  # a mouth's score is divided by (number of backers) ** crowd_pow

    def sizes(self) -> tuple[int, int, int, int]:
        return (self.sources, self.pools, self.pools, self.mouths)


DEFAULT = Rules()


# -- topology ----------------------------------------------------------------


@lru_cache(maxsize=None)
def channel_ids(sizes: tuple[int, ...]) -> tuple[str, ...]:
    out = []
    for layer in range(len(sizes) - 1):
        for i in range(sizes[layer]):
            for j in range(sizes[layer + 1]):
                out.append(f"{LAYER_LETTERS[layer]}{i + 1}>{LAYER_LETTERS[layer + 1]}{j + 1}")
    return tuple(out)


def channel_index(rules: Rules) -> dict[str, int]:
    return {cid: n for n, cid in enumerate(channel_ids(rules.sizes()))}


def n_channels(rules: Rules = DEFAULT) -> int:
    s = rules.sizes()
    return sum(s[i] * s[i + 1] for i in range(3))


# -- seeded stream -----------------------------------------------------------


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


def open_widths(seed: bytes, rules: Rules = DEFAULT) -> tuple[int, ...]:
    """Opening widths, each uniform in -cap..cap, drawn from the game seed."""
    st = Stream(seed, "widths")
    span = 2 * rules.width_cap + 1
    return tuple(st.below(span) - rules.width_cap for _ in range(n_channels(rules)))


def draw_rain(seed: bytes, epoch: int, rules: Rules = DEFAULT) -> tuple[tuple[int, ...], ...]:
    st = Stream(seed, f"rain/{epoch}")
    return tuple(
        tuple(st.below(rules.rain_max + 1) for _ in range(rules.sources))
        for _ in range(rules.samples)
    )


def draw_silt(seed: bytes, epoch: int, rules: Rules = DEFAULT) -> tuple[bool, ...]:
    n = n_channels(rules)
    if rules.silt_den <= 0:
        return (False,) * n
    st = Stream(seed, f"silt/{epoch}")
    return tuple(st.below(rules.silt_den) == 0 for _ in range(n))


# -- moves -------------------------------------------------------------------


def validate_move(move: dict, rules: Rules = DEFAULT) -> dict:
    """Return a normalised move ({'back': int, 'push': {id: int}}) or raise ValueError."""
    if not isinstance(move, dict) or set(move) - {"back", "push"}:
        raise ValueError("move must be {back, push}")
    back = move.get("back")
    if type(back) is not int or not 1 <= back <= rules.mouths:
        raise ValueError("back must be a mouth number")
    push = move.get("push", {})
    if not isinstance(push, dict):
        raise ValueError("push must be an object")
    idx = channel_index(rules)
    total = 0
    clean: dict[str, int] = {}
    for cid, v in push.items():
        if cid not in idx:
            raise ValueError(f"unknown channel {cid}")
        if type(v) is not int:
            raise ValueError("push values are whole numbers")
        if v == 0:
            continue
        clean[cid] = v
        total += abs(v)
    if len(clean) > rules.push_channels:
        raise ValueError("too many channels")
    if total > rules.push_budget:
        raise ValueError("over budget")
    return {"back": back, "push": clean}


def apply_pushes(
    widths: tuple[int, ...], moves: list[dict], rules: Rules = DEFAULT
) -> tuple[int, ...]:
    idx = channel_index(rules)
    w = list(widths)
    for mv in moves:
        for cid, v in mv["push"].items():
            w[idx[cid]] += v
    cap = rules.width_cap
    return tuple(max(-cap, min(cap, x)) for x in w)


# -- forward pass ------------------------------------------------------------


def forward(
    widths: tuple[int, ...],
    sample: tuple[int, ...],
    blocked: tuple[bool, ...] | None,
    rules: Rules = DEFAULT,
) -> list[int]:
    """Mouth amounts (whole numbers) for one rain sample."""
    sizes = rules.sizes()
    vals = list(sample)
    off = 0
    for layer in range(3):
        n, m = sizes[layer], sizes[layer + 1]
        nxt = [0] * m
        for i in range(n):
            v = vals[i]
            if v:
                base = off + i * m
                if blocked is None:
                    for j in range(m):
                        nxt[j] += widths[base + j] * v
                else:
                    for j in range(m):
                        if not blocked[base + j]:
                            nxt[j] += widths[base + j] * v
        vals = [x if x > 0 else 0 for x in nxt]
        off += n * m
    return vals


def shares(amounts: list[int]) -> tuple[Fraction, ...]:
    tot = sum(amounts)
    m = len(amounts)
    if tot == 0:
        return (Fraction(1, m),) * m
    return tuple(Fraction(a, tot) for a in amounts)


def avg_share(
    widths: tuple[int, ...],
    samples: tuple[tuple[int, ...], ...],
    blocked: tuple[bool, ...] | None,
    rules: Rules = DEFAULT,
) -> tuple[Fraction, ...]:
    acc = [Fraction(0)] * rules.mouths
    for s in samples:
        sh = shares(forward(widths, s, blocked, rules))
        acc = [a + b for a, b in zip(acc, sh)]
    n = len(samples)
    return tuple(a / n for a in acc)


# -- scoring, credit, pull ----------------------------------------------------


def score(
    backs: dict[str, int], avg: tuple[Fraction, ...], crowd_pow: int = 1
) -> dict[str, Fraction]:
    """Average share of the backed mouth divided by the number of players who backed it
    (to the power ``crowd_pow``)."""
    count: dict[int, int] = {}
    for m in backs.values():
        count[m] = count.get(m, 0) + 1
    return {p: avg[m - 1] / count[m] ** crowd_pow for p, m in backs.items()}


def credit(
    widths_before: tuple[int, ...],
    moves: dict[str, dict],
    samples,
    blocked,
    rules: Rules = DEFAULT,
) -> dict[str, tuple[Fraction, ...]]:
    """Leave-one-out: per player, each mouth's average share with minus without its pushes."""
    full = avg_share(
        apply_pushes(widths_before, list(moves.values()), rules), samples, blocked, rules
    )
    out = {}
    for p in moves:
        rest = [mv for q, mv in moves.items() if q != p]
        without = avg_share(apply_pushes(widths_before, rest, rules), samples, blocked, rules)
        out[p] = tuple(a - b for a, b in zip(full, without))
    return out


def pull(
    widths: tuple[int, ...], samples, blocked, rules: Rules = DEFAULT
) -> dict[str, tuple[Fraction, ...]]:
    """Per channel, how each mouth's average share changes for +1 on that channel."""
    base = avg_share(widths, samples, blocked, rules)
    ids = channel_ids(rules.sizes())
    cap = rules.width_cap
    out = {}
    for n, cid in enumerate(ids):
        if widths[n] >= cap:
            out[cid] = (Fraction(0),) * rules.mouths
            continue
        w = list(widths)
        w[n] += 1
        out[cid] = tuple(a - b for a, b in zip(avg_share(tuple(w), samples, blocked, rules), base))
    return out


# -- one epoch / one game -----------------------------------------------------


@dataclass
class EpochResult:
    epoch: int
    samples: tuple
    blocked: tuple
    widths_before: tuple
    widths_after: tuple
    avg: tuple
    scores: dict
    credit: dict = field(default_factory=dict)
    pull: dict = field(default_factory=dict)  # computed on this epoch's samples, silt ignored
    backs: dict = field(default_factory=dict)


def play_epoch(
    seed: bytes,
    epoch: int,
    widths: tuple[int, ...],
    moves: dict[str, dict],
    rules: Rules = DEFAULT,
    *,
    with_credit: bool = True,
    with_pull: bool = True,
) -> EpochResult:
    """Resolve one epoch from the revealed (already validated) moves."""
    samples = draw_rain(seed, epoch, rules)
    blocked = draw_silt(seed, epoch, rules)
    after = apply_pushes(widths, list(moves.values()), rules)
    avg = avg_share(after, samples, blocked, rules)
    backs = {p: mv["back"] for p, mv in moves.items()}
    res = EpochResult(
        epoch, samples, blocked, widths, after, avg, score(backs, avg, rules.crowd_pow), backs=backs
    )
    if with_credit:
        res.credit = credit(widths, moves, samples, blocked, rules)
    if with_pull:
        res.pull = pull(after, samples, None, rules)
    return res


def play_game(
    seed: bytes, epoch_moves: list[dict[str, dict]], rules: Rules = DEFAULT
) -> tuple[list[EpochResult], dict[str, Fraction]]:
    """Replay a whole game from the seed and every epoch's revealed moves."""
    widths = open_widths(seed, rules)
    results = []
    totals: dict[str, Fraction] = {}
    for e, mv in enumerate(epoch_moves, start=1):
        clean = {p: validate_move(m, rules) for p, m in mv.items()}
        r = play_epoch(seed, e, widths, clean, rules)
        widths = r.widths_after
        results.append(r)
        for p, s in r.scores.items():
            totals[p] = totals.get(p, Fraction(0)) + s
    return results, totals


def winners(totals: dict[str, Fraction]) -> list[str]:
    if not totals:
        return []
    top = max(totals.values())
    return sorted(p for p, t in totals.items() if t == top)
