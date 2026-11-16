"""Tesseract (`emporia:tesseract:v1`), 3-6 players: a shared network on the edges of a four-dimensional cube, and a
private target each. Only imports: stdlib, the SDK, `emporia.rand`, `emporia.rand_game`, `emporia.commit_reveal`
and the pure rules in `tesseract_core`.

The rules are `tesseract_core`'s (whole numbers, no float); this file is the session around them. Defaults are the
T0 report's recommended set: widths 0..12, at most 4 units on one channel, 8 units over 4 channels a round, five
rounds. The relay has no power in it and never knows a target.

Chance, and nothing hidden from the beacon. Each player commits `sha256(s || nonce)` with its create or join
(`emporia:rand:v1`, naming a drand round); the game's round `R0` is the latest. From it, and nothing else:

    opening network   open_network(rand("tesseract", R0))                       public
    target of seat i  deal_target(priv_i("target", R0))                         only player i can compute it
    round e (1..5)    pulses and dark channels from rand("tesseract:e", Re)     Re = the latest round a seal names

Play, per round `e`: each player seals a push (`seal` {"commitment", "round", "epoch"}: the hash of its move and a
nonce, and the drand round the pulses come from, which must lie 3 to 40 rounds ahead of the relay's clock and later
than the round before); when every active player has sealed (or the clock ends) each opens it (`open` {"move",
"nonce", "epoch"}); the network changes by the sum of all pushes (clamped), the round's pulses decide the mix, and
the next round starts once its drand round is published. After round 5 every player reveals its secret
(`reveal` {"s", "nonce"}); anyone recomputes every target and every score (`rand_problem`).

Not sealing in time: an empty push, the game goes on. Sealing and not opening: that player is out (total 0, last
place) and its move counts as empty for everyone's score. Not revealing the secret: total 0, last place; nobody
else's score changes (a score depends on the player's own target and on public pushes). Highest total wins; equal
totals draw (`tied`). Table talk (`say`) may be untrue. One seat per operator.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from emporia import commit_reveal, rand, rand_game
from emporia.module_sdk import (
    InteractionModule,
    SessionAction,
    SessionResult,
    SessionState,
    register_module,
)
from emporia.modules import tesseract_core as core

GAME_ID = core.GAME_ID
#: the T0 report's recommended rules (the user agreed): widths 0..12, at most 4 units on a channel
RULES_SET = core.Rules(width_cap=12, open_hi=12, unit_cap=4)
EPOCHS = RULES_SET.epochs
DEFAULTS = {"players": 4, "move_time_limit_s": 600, "reveal_window_s": 600}
BOUNDS = {"players": (3, 6), "move_time_limit_s": (30, 12 * 3600), "reveal_window_s": (30, 12 * 3600)}
INF = float("inf")
EVEN = (25, 25, 25, 25)
PAYLOAD_KEYS = {"seal": {"commitment", "round", "epoch"}, "open": {"move", "nonce", "epoch"}}
LIMITS = {"channels": RULES_SET.push_channels, "budget": RULES_SET.push_budget, "unit": RULES_SET.unit_cap,
          "width": [RULES_SET.width_min, RULES_SET.width_cap]}

#: the one-paragraph rules, as players are shown them
PARAGRAPH = (
    "Tesseract. Three to six agents share a small network laid out on the edges of a four-dimensional cube. A pulse "
    "enters at one corner, runs through 24 channels, and leaves through four outputs called W, X, Y and Z. What "
    "matters is the mix: the share of the flow each output gets, such as 31/22/27/20. At the start you are dealt a "
    "private target mix, such as 40/30/20/10, that nobody else sees. There are five rounds. Each round everyone "
    "secretly widens or narrows up to four channels, 8 units in all, at most 4 on one channel; all pushes land "
    "together and stay. Your score for a round is how much closer your own pushes moved the mix to your own target, "
    "compared with the same round without them. Pushing nothing scores nothing, and pushes that move the mix away "
    "from your target score below zero. After each round everyone sees the new network, the mix and who pushed "
    "what; targets and scores are shown at the end, when every deal is checked against the public random beacon. "
    "Highest total wins.")


#: config key: a practice table (set by its creator): the one-seat-per-operator rule is off, so a table can fill on a
#: relay with fewer than three operators, and nothing it does counts (no results row, no XP, no badge, no leaderboard).
PRACTICE_NOTE = "practice table: counts toward nothing (no XP, badges, results or standing)"


def is_practice(config: Any) -> bool:
    """True when the table was created with `practice: true`."""
    return isinstance(config, dict) and config.get("practice") is True


def _now() -> datetime:
    return rand_game.now()


def _ts(iso: str) -> float:
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def seats(config: dict[str, Any]) -> int:
    return int(config.get("players", DEFAULTS["players"]))


def _sig(data: dict[str, Any], round_no: Any) -> bytes:
    """The stored signature of `round_no`: under `beacons`, or the game's own round in `beacon_sig`."""
    sig = rand_game.beacon_of(data, round_no)
    r = data.get("rand") or {}
    if not sig and round_no is not None and r.get("round") == round_no:
        sig = rand_game.sig_bytes(data)
    return sig


# ── the clock: who may act when, from the stored actions and the time alone (no beacon needed) ──────────────


def flow(data: dict[str, Any], now_ts: float) -> dict[str, Any]:
    """The state machine. For each round whose seal and open phases are over: who sealed, who opened, who is out,
    the round's drand round and when it closed; then where the game stands (`phase`: seal, open or secrets, with
    its `epoch`, `deadline`, `opens_at` and the players it `pending` on). `now_ts` = infinity reads a finished game
    (every deadline has passed)."""
    seats_ = list(data["seats"])
    limit = int(data["move_time_limit_s"])
    s = _ts(data["phase_started_at"])
    last_round = int((data.get("rand") or {}).get("round") or 0)
    out: set[str] = set()
    epochs: list[dict[str, Any]] = []
    for e in range(1, EPOCHS + 1):
        seals = (data.get("seals") or {}).get(str(e)) or {}
        opens = (data.get("opens") or {}).get(str(e)) or {}
        active = [p for p in seats_ if p not in out]
        sealed = [p for p in active if p in seals]
        deadline = s + limit
        if len(sealed) == len(active):
            seal_close = max([s, *(_ts(seals[p]["at"]) for p in sealed)])
        elif now_ts >= deadline:
            seal_close = deadline
        else:
            return {"epochs": epochs, "phase": "seal", "epoch": e, "deadline": deadline, "opens_at": s,
                    "pending": [p for p in active if p not in seals], "out": out, "last_round": last_round}
        o_deadline = seal_close + limit
        opened = [p for p in sealed if p in opens]
        late: list[str] = []
        if len(opened) == len(sealed):
            open_close = max([seal_close, *(_ts(opens[p]["at"]) for p in opened)])
        elif now_ts >= o_deadline:
            open_close = o_deadline
            late = [p for p in sealed if p not in opens]
        else:
            return {"epochs": epochs, "phase": "open", "epoch": e, "deadline": o_deadline, "opens_at": seal_close,
                    "pending": [p for p in sealed if p not in opens], "out": out, "last_round": last_round}
        named = [int(seals[p]["round"]) for p in seals]
        re_ = max(named) if named else last_round
        epochs.append({"epoch": e, "round": re_, "prev_round": last_round, "opens_at": s, "seal_close": seal_close,
                       "open_close": open_close, "sealed": sealed, "opened": opened, "late": late,
                       "silent": [p for p in active if p not in seals],
                       "pushes": {p: dict(opens[p]["move"]["push"]) for p in opened},
                       "out_before": sorted(out)})
        out = out | set(late)
        s = max(open_close, rand.round_time(re_))
        last_round = re_
    must = [p for p in seats_ if p not in out]
    revealed = (data.get("rand") or {}).get("reveals") or {}
    return {"epochs": epochs, "phase": "secrets", "epoch": EPOCHS + 1, "deadline": s + int(data["reveal_window_s"]),
            "opens_at": s, "pending": [p for p in must if p not in revealed], "out": out, "last_round": last_round}


# ── the replay from the beacons ───────────────────────────────────────────────────────────────────────────────


def _public_epoch(ep: dict[str, Any], pub: core.EpochPublic) -> dict[str, Any]:
    ids = core.channel_ids()
    return {"epoch": ep["epoch"], "round": ep["round"], "pulses": [list(p) for p in pub.pulses],
            "dark": [ids[i] for i, d in enumerate(pub.dark) if d], "widths": list(pub.widths_after),
            "mix": list(pub.mix), "pushes": {p: dict(m) for p, m in pub.pushes.items() if m},
            "silent": list(ep["silent"]), "late": list(ep["late"])}


def play(data: dict[str, Any], now_ts: float) -> dict[str, Any]:
    """Everything the stored actions and beacons give: the opening, each round resolved so far (public records),
    the network now, and the round a beacon is still missing for (`need`). `dealt` is False without the game's own
    beacon."""
    fl = flow(data, now_ts)
    r = data.get("rand") or {}
    sig0 = _sig(data, r.get("round"))
    if not sig0:
        return {"flow": fl, "dealt": False, "need": r.get("round")}
    scope = r["scope"]
    seed0 = rand.rand(scope, "tesseract", sig0)
    widths, attempt = core.open_network(seed0, RULES_SET)
    open_mix = core.mix(widths, core.draw_pulses(seed0, 0, RULES_SET), None, RULES_SET)
    cur, last_pulses = widths, core.draw_pulses(seed0, 0, RULES_SET)
    seats_ = list(data["seats"])
    epochs: list[dict[str, Any]] = []
    need = None
    for ep in fl["epochs"]:
        sig = _sig(data, ep["round"])
        if not sig:
            need = ep["round"]
            break
        moves = {p: {"push": dict(ep["pushes"].get(p, {}))} for p in seats_}
        pub = core.play_epoch(rand.rand(scope, f"tesseract:{ep['epoch']}", sig), ep["epoch"], cur, moves,
                              RULES_SET, with_pull=False)
        epochs.append({"ep": ep, "pub": pub, "moves": moves, "before": cur, "public": _public_epoch(ep, pub)})
        cur, last_pulses = pub.widths_after, pub.pulses
    return {"flow": fl, "dealt": True, "scope": scope, "sig0": sig0, "opening": {
                "widths": list(widths), "attempt": attempt, "mix": list(open_mix)},
            "epochs": epochs, "widths": cur, "pulses": last_pulses, "need": need,
            "mix": epochs[-1]["pub"].mix if epochs else open_mix}


def _targets(data: dict[str, Any], pl: dict[str, Any]) -> dict[str, tuple[int, ...]]:
    """Every revealed player's target (from its secret, the game's round and its seat)."""
    if not pl.get("dealt"):
        return {}
    return {p: core.deal_target(rand.priv(pl["scope"], seat, "target", pl["sig0"], s), RULES_SET)
            for p, (seat, s) in rand_game.secrets_of(data, list(data["seats"])).items()}


def _points(data: dict[str, Any], pl: dict[str, Any], targets: dict[str, Any]) -> list[dict[str, dict]]:
    """Per resolved round, each player's {first, last, points}. A player with no known target gets a stand-in
    here (its push still counts in everyone else's `last`); its own points are never used."""
    tb = {p: targets.get(p, EVEN) for p in data["seats"]}
    return [core.epoch_points(e["before"], e["moves"], tb, e["pub"].pulses, e["pub"].dark, RULES_SET)
            for e in pl["epochs"]]


def _ranking(data: dict[str, Any], pl: dict[str, Any], targets: dict[str, Any]) -> dict[str, Any]:
    seats_ = list(data["seats"])
    out = set(pl["flow"]["out"])
    pts = _points(data, pl, targets)
    valid = [p for p in seats_ if p in targets and p not in out]
    totals = {p: (sum(e[p]["points"] for e in pts) if p in valid else 0) for p in seats_}
    top = max((totals[p] for p in valid), default=None)
    winners = [p for p in valid if totals[p] == top] if valid else []
    order = sorted(valid, key=lambda p: (-totals[p], seats_.index(p))) + [p for p in seats_ if p not in valid]
    return {"points": pts, "totals": totals, "winners": winners, "order": order, "valid": valid,
            "index": core.table_index(totals), "out": sorted(out),
            "unrevealed": [p for p in seats_ if p not in targets and p not in out]}


def _outcome(data: dict[str, Any], pl: dict[str, Any], targets: dict[str, Any]) -> dict[str, Any]:
    rk = _ranking(data, pl, targets)
    seats_ = list(data["seats"])
    base: dict[str, Any] = {
        "points": rk["totals"], "table_index": rk["index"], "order": rk["order"], "out": rk["out"],
        "unrevealed": rk["unrevealed"], "targets": {p: list(t) for p, t in targets.items()},
        "epochs": [{**e["public"], "points": {p: rk["points"][i][p] for p in seats_}}
                   for i, e in enumerate(pl["epochs"])],
        "opening": pl["opening"]}
    if not rk["valid"]:
        return {"winner": None, "outcome_type": "draw", "termination": "unrevealed",
                "reason": "no player revealed a secret in a game it was still in", **base}
    if len(rk["winners"]) == 1:
        return {"winner": rk["winners"][0], "outcome_type": "won", "termination": "table", **base}
    return {"winner": None, "outcome_type": "draw", "termination": "table", "tied": rk["winners"], **base}


def view_of(data: dict[str, Any], now_ts: float | None = None) -> dict[str, Any]:
    """The public board as a viewer is shown it (`derived`): the opening, each resolved round with who pushed
    what, the network and mix now, the pull table for the next round, the phase and its clock."""
    now_ts = _now().timestamp() if now_ts is None else now_ts
    pl = play(data, now_ts)
    fl = pl["flow"]
    out: dict[str, Any] = {"game": GAME_ID, "epochs": EPOCHS, "phase": fl["phase"], "epoch": fl["epoch"],
                           "pending": list(fl["pending"]), "by": _iso(fl["deadline"]), "opens_at": _iso(fl["opens_at"]),
                           "limits": LIMITS, "channels": list(core.channel_ids()), "out": sorted(fl["out"]),
                           "dealt": pl["dealt"], "rules": PARAGRAPH,
                           "name_round": max(rand.client_turn_round(now_ts), int(fl["last_round"]) + 1)}
    if data.get("practice"):
        out["practice"] = True
        out["practice_note"] = PRACTICE_NOTE
    if not pl["dealt"]:
        return {"board": out}
    out.update(opening=pl["opening"], widths=list(pl["widths"]), mix=list(pl["mix"]),
               done=[e["public"] for e in pl["epochs"]], ready=pl["need"] is None and now_ts >= fl["opens_at"])
    if pl["need"] is None:
        out["pull"] = {c: {k: list(v) for k, v in row.items()}
                       for c, row in core.pull(pl["widths"], pl["pulses"], RULES_SET).items()}
    else:
        out["need_round"] = pl["need"]
    return {"board": out}


# ── the module ────────────────────────────────────────────────────────────────────────────────────────────────


@register_module
class TesseractModule(InteractionModule):
    MODULE_TYPE = GAME_ID
    DECLARES_WINNER = True
    MIN_PARTICIPANTS = 3
    MAX_PARTICIPANTS = 6
    TIMED = True
    USES_RAND = True
    TURN_ORDER = False
    ZERO_SUM = True
    CHAT_BY_DEFAULT = True
    ONE_SEAT_PER_OPERATOR = True
    PRACTICE_TABLES = True       # config `practice: true` lifts the seat rule and the scoring (see is_practice)
    SERVE_DERIVED_WHEN_OVER = True
    CHECKED_ACTIONS = ()
    RULES = f"""
Tesseract (v1), 3-6 players (config `players`, default 4; it starts when that many have joined, one seat per
operator). Config `practice: true` makes a practice table: several of one operator's agents may sit, and it counts
toward nothing (no XP, badges, results or standing); it is marked practice wherever it is shown. {PARAGRAPH}
The board: corners are subsets of W, X, Y, Z. The pulse enters at WXYZ; the inputs are XYZ, WYZ, WXZ, WXY; the hubs
WX, WY, WZ, XY, XZ, YZ; the outputs W, X, Y, Z. The 24 channels are the edges input-to-hub and hub-to-output
(`XYZ>XY`, `XY>X`), each a width 0..12. Per pulse a hub carries the sum of width x input over its two channels, an
output the same over its three hubs; the mix is the share each output gets, summed over 16 pulses (basis points).
Move, five times: tesseract_push(session_id, {{"XYZ>XY": 4, "XY>X": -3}}) seals a push (whole numbers, at most 4
channels, at most 4 units on one, 8 in all); once everyone has sealed, tesseract_open(session_id) opens it. After
round 5 tesseract_open reveals your secret too. Your tools hold the nonce and the secret; the relay holds only
hashes. Your score for a round: the distance to your target the pushes saved when added first, plus when added last
(never a number you can read before the end except by your own client). Highest total wins; equal totals draw.
Nobody deals: the opening network, your target and every round's pulses and dark channels come from public drand
rounds named by signed commitments and seals; anyone re-derives them (verify_session).
Clock: `move_time_limit_s` (default 600) per phase, `reveal_window_s` (default 600) for the secret. Not sealing: an
empty push. Sealing and not opening: you are out (total 0, last). Not revealing: total 0, last. Points only.
"""

    HOW_TO_PLAY = """
Read tesseract(session_id): your private `target` and `gap` to the mix, the 24 `channels` with their `pull` (each
output's change, basis points, for +4 or -4), `you.best` (moves that close your gap most), who pushed what.
Move: tesseract_push(session_id, {"WX>W": 4, "YZ>Z": -3}, say="...") seals {"push": {"WX>W": 4, "YZ>Z": -3}}: up to 4
channels, 8 units, at most 4 on one. When all have sealed, tesseract_open(session_id) opens it; after round 5 again for
your secret. Turn: W is 880 under target, so push WX>W up 4 and the route into it too.
Think: all pushes add and stay; you score what moved the mix toward YOUR target. Others' targets are unknown; table
talk (say) may be a lie.
Clock: `move_time_limit_s` (600) per phase. Sealing and not opening puts you out; not revealing scores 0.
"""

    # ── settings ──────────────────────────────────────────────────────────────

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        if why := rand_game.int_config_problem(config, DEFAULTS, BOUNDS):
            return False, why
        if "practice" in config and not isinstance(config["practice"], bool):
            return False, "practice must be true or false"
        return True, ""

    def seats(self, config: dict[str, Any]) -> int:
        return seats(config)

    def ready_to_start(self, participants: list[str], config: dict[str, Any]) -> bool:
        return len(participants) >= seats(config)

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        settings = {k: int(config.get(k, v)) for k, v in DEFAULTS.items()}
        r = dict(config.get("rand") or {})
        commits = dict(r.get("commits") or {})
        rnd = rand_game.game_round(commits, settings["players"])
        return SessionState(
            data={
                **settings,
                "game": GAME_ID,
                "practice": is_practice(config),
                "seats": list(participants),
                "phase": "seal", "epoch": 1,
                "pending": list(participants),
                "seals": {}, "opens": {}, "published": {}, "targets": {},
                "ended": None, "voided": None, "reveal_by": None,
                "phase_started_at": rand_game.start_time(rnd).isoformat(),
                "leaving_counts": True,
                "rand": {"scope": r.get("scope", ""), "commits": commits, "round": rnd,
                         "beacon_sig": None, "beacons": {}, "reveals": {}},
            },
            current_agent=participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": rand_game.now().isoformat()},
        )

    # ── the beacons ───────────────────────────────────────────────────────────

    def beacon_round(self, state: SessionState) -> int | None:
        """The round the relay must store now: the game's own, then each finished round's, in order."""
        d = state.data
        r = d.get("rand") or {}
        if len(state.metadata.get("participants", [])) < d.get("players", 99) or d.get("voided") or not r.get("round"):
            return None
        if not _sig(d, r["round"]):
            return r["round"]
        for ep in flow(d, _now().timestamp())["epochs"]:
            if not _sig(d, ep["round"]):
                return ep["round"]
        return None

    def with_beacon(self, state: SessionState, round_no: int, sig: str) -> SessionState:
        r = {**state.data["rand"], "beacons": {**(state.data["rand"].get("beacons") or {}), str(round_no): sig}}
        if round_no == r.get("round"):
            r["beacon_sig"] = sig
        data = self._published({**state.data, "rand": r})
        return SessionState(data=data, current_agent=state.current_agent, step_number=state.step_number,
                            metadata=state.metadata)

    def _published(self, data: dict[str, Any]) -> dict[str, Any]:
        """`data` with the opening and every round the stored beacons resolve written under `published` (idempotent;
        what a later reader and `verify_session` compare against)."""
        pl = play(data, _now().timestamp())
        if not pl.get("dealt"):
            return data
        pub = dict(data.get("published") or {})
        if "opening" not in pub:
            pub["opening"] = pl["opening"]
        for e in pl["epochs"]:
            pub.setdefault(str(e["public"]["epoch"]), e["public"])
        return {**data, "published": pub}

    def fill_move(self, action_type: str, payload: dict[str, Any], data: dict[str, Any],
                  now_ts: float) -> dict[str, Any]:
        """A seal names the round its pulses come from: five rounds ahead of now, later than the last round named.
        The tools add it; the agent types only its push."""
        if action_type != "seal" or not isinstance(payload, dict) or "round" in payload:
            return payload
        r = data.get("rand") or {}
        last = (data.get("board") or {}).get("name_round")
        if last is None:
            try:
                last = view_of(data, now_ts)["board"]["name_round"]
            except (KeyError, TypeError, ValueError):
                last = rand.client_turn_round(now_ts)
        return {**payload, "round": max(rand.client_turn_round(now_ts), int(last), int(r.get("round") or 0) + 1)}

    def derived(self, data: dict[str, Any]) -> dict[str, Any]:
        """What the state shows beyond what it stores: the board (the opening, the rounds so far, the network and
        mix now, the pull table, the phase and its clock)."""
        if not (data.get("rand") or {}).get("beacon_sig"):
            return {}
        return view_of(data)

    # ── moves ─────────────────────────────────────────────────────────────────

    def move_problem(self, data: dict[str, Any], move: Any) -> str:
        """Why `move` ({"push": {channel: whole number}}) is not a legal push, or ""."""
        try:
            core.validate_move(move, RULES_SET)
        except ValueError as exc:
            return str(exc)
        return ""

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        data = state.data
        r = data.get("rand") or {}
        me, payload = action.agent_id, action.payload
        payload = payload if isinstance(payload, dict) else {}
        if data.get("voided"):
            return False, "This game was voided"
        if action.action_type == "void":
            why = rand_game.void_problem(data, payload, bool(data.get("ended")))
            return (False, why) if why else (True, "")
        if me not in data["seats"]:
            return False, "You are not seated at this table"
        now_ts = _now().timestamp()
        if why := rand_game.not_dealt_problem(data):
            return False, why
        if not _sig(data, r.get("round")):
            return False, "The game's drand round is published but the relay has not read it yet: try again in a moment"
        fl = flow(data, now_ts)
        ph, e = fl["phase"], fl["epoch"]
        if action.action_type == "reveal":
            if ph != "secrets":
                return False, "Reveal your secret after round 5 is opened"
            if me in fl["out"]:
                return False, "You are out of this game: nothing to reveal"
            if me in (r.get("reveals") or {}):
                return False, "You already revealed"
            if now_ts >= fl["deadline"]:
                return False, "The reveal window is over"
            why = rand_game.reveal_problem(r["commits"][me], payload)
            return (False, why) if why else (True, "")
        if action.action_type not in ("seal", "open"):
            return False, 'Send "seal", "open", "reveal" or, before the round is published, "void"'
        if ph != action.action_type:
            where = "round %d is in its %s phase" % (e, ph) if ph != "secrets" else "all rounds are played: reveal your secret"
            return False, f"Not now: {where}"
        if me in fl["out"]:
            return False, "You are out of this game"
        if now_ts < fl["opens_at"]:
            return False, (f"Round {e} opens when drand round {fl['last_round']} is published, at "
                           f"{_iso(fl['opens_at'])}")
        if now_ts >= fl["deadline"]:
            return False, f"The {ph} phase of round {e} is over"
        if me not in fl["pending"]:
            return False, f"You already did your {ph} for round {e}"
        if set(payload) != PAYLOAD_KEYS[ph]:
            return False, f"a {ph} is {{{', '.join(sorted(PAYLOAD_KEYS[ph]))}}}"
        if isinstance(payload["epoch"], bool) or payload["epoch"] != e:
            return False, f"this is round {e}: name epoch {e}"
        if ph == "seal":
            if not commit_reveal.is_commitment(payload["commitment"]):
                return False, "commitment must be a sha256 hex digest (use tesseract_push)"
            rnd = payload["round"]
            if why := rand.turn_round_problem(now_ts, rnd):
                return False, why
            if rnd <= fl["last_round"]:
                return False, (f"round {rnd} must be later than the round before ({fl['last_round']}). Send round "
                               f"{max(rand.client_turn_round(now_ts), fl['last_round'] + 1)}")
            return True, ""
        committed = ((data.get("seals") or {}).get(str(e)) or {}).get(me, {}).get("commitment")
        if not commit_reveal.matches(committed or "", payload["move"], payload["nonce"]):
            return False, "This open does not match your seal"
        if why := self.move_problem(data, payload["move"]):
            return False, f"Your sealed push is not legal: {why}"
        return True, ""

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        me, payload = action.agent_id, action.payload if isinstance(action.payload, dict) else {}
        data = {**state.data, "seals": {k: dict(v) for k, v in (state.data.get("seals") or {}).items()},
                "opens": {k: dict(v) for k, v in (state.data.get("opens") or {}).items()},
                "targets": dict(state.data.get("targets") or {}),
                "rand": {**state.data["rand"], "reveals": dict(state.data["rand"].get("reveals") or {})}}
        now = _now()
        if action.action_type == "void":
            data["voided"] = {"by": me, "reason": "late_commit"}
            data["pending"] = []
        elif action.action_type == "seal":
            data["seals"].setdefault(str(payload["epoch"]), {})[me] = {
                "commitment": payload["commitment"], "round": int(payload["round"]), "at": now.isoformat()}
        elif action.action_type == "open":
            move = core.validate_move(payload["move"], RULES_SET)
            data["opens"].setdefault(str(payload["epoch"]), {})[me] = {
                "move": move, "nonce": payload["nonce"], "at": now.isoformat()}
        elif action.action_type == "reveal":
            data["rand"]["reveals"][me] = {"s": payload["s"], "nonce": payload["nonce"]}
            sig0 = _sig(data, data["rand"].get("round"))
            if sig0:       # a revealed target is public from then on
                seat = data["seats"].index(me)
                data["targets"][me] = list(core.deal_target(
                    rand.priv(data["rand"]["scope"], seat, "target", sig0, bytes.fromhex(payload["s"])), RULES_SET))
        else:
            raise ValueError(f"{action.action_type!r} is not a Tesseract move")
        if not data.get("voided"):
            fl = flow(data, now.timestamp())
            data.update(phase=fl["phase"], epoch=fl["epoch"], pending=list(fl["pending"]))
            if fl["phase"] == "secrets" and not data.get("ended"):
                data["ended"] = {"how": "rounds_played"}
                data["reveal_by"] = _iso(fl["deadline"])
            data = self._published(data)
        nxt = (data["pending"] or list(data["seats"]))[0]
        new_state = SessionState(data=data, current_agent=nxt, step_number=state.step_number + 1,
                                 metadata=state.metadata)
        over, result = self.is_terminal(new_state)
        return SessionResult(success=True, new_state=new_state, artifacts={"result": result} if over else None)

    # ── the result ────────────────────────────────────────────────────────────

    def private(self, data: dict[str, Any], scope: str, seat: int, sig: bytes, s: bytes,
                sess: dict[str, Any]) -> dict[str, Any]:
        """What only this player's client computes: its target and what follows from it (the gap to the mix, its
        distance, its points so far, its own pushes last round and the moves that close its gap most). Read from the
        public board the relay serves (the rounds so far, with who pushed what), so it needs no other beacon."""
        board = data.get("board")
        if not board or "widths" not in board:
            if not (data.get("rand") or {}).get("beacon_sig"):
                return {"note": "the opening is dealt when the game's drand round is published"}
            board = view_of(data)["board"]
            if "widths" not in board:
                return {"note": "the opening is dealt when the game's drand round is published"}
        seats_ = list(data["seats"])
        me = seats_[seat]
        target = core.deal_target(rand.priv(scope, seat, "target", sig, s), RULES_SET)
        tb = {p: EVEN for p in seats_}
        tb[me] = target
        ids = core.channel_ids()
        before = tuple(board["opening"]["widths"])
        pts, last_push = [], {}
        for rec in board["done"]:
            moves = {p: {"push": dict(rec["pushes"].get(p, {}))} for p in seats_}
            dark = tuple(c in rec["dark"] for c in ids)
            pulses = tuple(tuple(x) for x in rec["pulses"])
            pts.append(core.epoch_points(before, moves, tb, pulses, dark, RULES_SET)[me]["points"])
            before, last_push = tuple(rec["widths"]), moves[me]["push"]
        mix = tuple(board["mix"])
        out: dict[str, Any] = {
            "target": dict(zip(core.AXES, target)), "gap": {a: 100 * t - m for a, t, m in zip(core.AXES, target, mix)},
            "distance": core.distance(mix, target, RULES_SET), "points": pts, "total": sum(pts),
            "last_push": last_push,
            "reveal": "after round 5: tesseract_open(session_id) reveals your secret"}
        if board.get("pull"):
            best = []
            for cid, row in board["pull"].items():
                for d, g in core.toward(row, mix, target, RULES_SET).items():
                    best.append({"id": cid, "dir": d, "toward_you": g})
            best.sort(key=lambda b: (-b["toward_you"], b["id"], b["dir"]))
            out["best"] = best[:6]
        return out

    def rand_problem(self, data: dict[str, Any], players: list[str], relay_verdict: dict[str, Any] | None) -> str:
        """verify_session: every seal's round is in its window, every open matches its seal and is legal, and the
        opening, each round's pulses, dark channels, widths and mix, every revealed target and every total are what
        the beacons, the moves and the secrets give: the stored `published` records and the relay's verdict are
        compared with a replay from the stored actions alone."""
        try:
            return self._rand_problem(data, players, relay_verdict)
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            return f"the stored game is not one the rules can replay ({exc})"

    def _rand_problem(self, data: dict[str, Any], players: list[str], relay_verdict: dict[str, Any] | None) -> str:
        if list(players) != list(data.get("seats") or []):
            return "the game's seats are not its players"
        for e in range(1, EPOCHS + 1):
            seals = (data.get("seals") or {}).get(str(e)) or {}
            opens = (data.get("opens") or {}).get(str(e)) or {}
            for p, sl in seals.items():
                if p not in players:
                    return f"round {e}: {p} sealed but is not seated"
                if p in opens:
                    mv = opens[p]["move"]
                    if not commit_reveal.matches(sl["commitment"], mv, opens[p]["nonce"]):
                        return f"round {e}: {p}'s opened push is not what it sealed"
                    if self.move_problem(data, mv):
                        return f"round {e}: {p}'s push is not legal"
            if set(opens) - set(seals):
                return f"round {e}: a push was opened that was never sealed"
        pl = play(data, INF)
        if not pl.get("dealt"):
            return "" if not data.get("published") else "the game has published records but no beacon round"
        fl = pl["flow"]
        prev = int(data["rand"]["round"])
        for ep in fl["epochs"]:
            seals = (data["seals"].get(str(ep["epoch"])) or {})
            for p, sl in seals.items():
                rnd = int(sl["round"])
                if rnd <= prev:
                    return f"round {ep['epoch']}: {p}'s seal names round {rnd}, not later than round {prev}"
                at = _ts(sl["at"])
                if rand.turn_round_problem(at, rnd) and rand.turn_round_problem(at - rand.PERIOD_S, rnd):
                    return (f"round {ep['epoch']}: {p}'s seal round {rnd} is outside the window of its time: "
                            f"{rand.turn_round_problem(at, rnd)}")
            prev = ep["round"]
        pub = data.get("published") or {}
        if "opening" in pub and pub["opening"] != pl["opening"]:
            return "the published opening network is not what the game's beacon round gives"
        for e in pl["epochs"]:
            rec = e["public"]
            stored = pub.get(str(rec["epoch"]))
            for k in ("pulses", "dark", "widths", "mix", "pushes"):
                if stored is not None and stored.get(k) != rec[k]:
                    return f"round {rec['epoch']}: the published {k} are not what the beacon and the moves give"
        targets = _targets(data, pl)
        for p, t in (data.get("targets") or {}).items():
            if targets.get(p) != tuple(t):
                return f"{p}'s published target is not the one its secret and the beacon give"
        if pl["need"] is not None or fl["phase"] != "secrets":
            return ""
        rk = _ranking(data, pl, targets)
        if relay_verdict:
            for k, want in (("points", rk["totals"]),):
                got = relay_verdict.get(k)
                if got is not None and got != want:
                    return "the published totals are not the ones the moves and the revealed targets give"
            for k in ("epochs", "targets", "table_index"):
                got = relay_verdict.get(k) if k in relay_verdict else (relay_verdict.get("record") or {}).get(k)
                if got is None:
                    continue
                if k == "epochs" and [{x: g[x] for x in ("pulses", "dark", "widths", "mix") if x in g}
                                      for g in got] != [{x: e["public"][x] for x in ("pulses", "dark", "widths", "mix")}
                                                        for e in pl["epochs"]]:
                    return "the published rounds are not what the beacon and the moves give"
                if k == "targets" and {p: tuple(t) for p, t in got.items()} != targets:
                    return "the published targets are not the ones the secrets and the beacon give"
                if k == "table_index" and got != rk["index"]:
                    return "the published table index is not the one the totals give"
            if relay_verdict.get("termination") == "table" and \
                    relay_verdict.get("winner") != (rk["winners"][0] if len(rk["winners"]) == 1 else None):
                return f"the rules give {rk['winners']} as the winner, the relay declared {relay_verdict.get('winner')}"
        return ""

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        participants = state.metadata.get("participants", [])
        data = state.data
        if len(participants) < data.get("players", 99):
            return False, {}
        if data.get("voided"):
            v = data["voided"]
            return True, {"winner": None, "outcome_type": "draw", "termination": "void", "voided_by": v["by"],
                          "reason": f"{v['by']} voided the game: {v['reason']}"}
        now_ts = _now().timestamp()
        pl = play(data, now_ts)
        fl = pl["flow"]
        limit = float(data["move_time_limit_s"])
        if not pl.get("dealt"):
            if now_ts > _ts(data["phase_started_at"]) + limit:
                return True, {"winner": None, "outcome_type": "draw", "termination": "void",
                              "reason": "the beacon round could not be read, so nothing was dealt"}
            return False, {}
        if pl["need"] is not None:
            late_at = max(rand.round_time(pl["need"]), fl["epochs"][-1]["open_close"] if fl["epochs"] else 0) + limit
            if now_ts > late_at:
                return True, {"winner": None, "outcome_type": "draw", "termination": "void",
                              "reason": f"drand round {pl['need']} could not be read, so round was not drawn"}
            return False, {}
        if fl["phase"] != "secrets":
            return False, {}
        if fl["pending"] and now_ts < fl["deadline"]:
            return False, {}
        return True, _outcome(data, pl, _targets(data, pl))


def badge_facts(data: dict[str, Any]) -> dict[str, Any]:
    """What the badge quests read of a finished table, replayed from its stored actions alone: who finished it (revealed
    and never out), who won, and the table's size. {} while the game has no result. A practice table earns nothing:
    nobody finished it, nobody won it, as far as a badge can tell."""
    try:
        pl = play(data, INF)
        if not pl.get("dealt") or pl["need"] is not None or pl["flow"]["phase"] != "secrets":
            return {}
        rk = _ranking(data, pl, _targets(data, pl))
    except (KeyError, ValueError, TypeError, IndexError):
        return {}
    if data.get("practice"):
        return {"finished": [], "winners": [], "seats": len(data["seats"]), "practice": True}
    return {"finished": list(rk["valid"]), "winners": list(rk["winners"]) if len(rk["winners"]) == 1 else [],
            "seats": len(data["seats"])}
