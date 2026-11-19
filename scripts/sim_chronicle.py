#!/usr/bin/env python3
"""Chronicle T0 report (offline, seeded; not part of the test run).

Runs the ten pass criteria of the games plan, section 9 (T0), against the pure core `emporia.modules.chronicle`
with scripted writers and voters (not models), and prints a markdown table.

    python scripts/sim_chronicle.py [--criteria 1,2,3,...] [--seed 7] [--seasons 150] [--json out.json]

Criteria 1 to 9 are computed here (9 with the 40+ hand-written fixture passages in `FIXTURES`). Criterion 10 is
a set of named tests in `tests/test_chronicle.py`; this script only lists them.

Voting-rule simulation (3 to 7): a season is 60 chapters; an operator has a persistent skill (ops 0 and 1, the
actors, have none), a passage's quality is skill + N(0,1), a juror perceives quality + N(0,0.8). Seats, the
tally, quorum, ties and weights are the core's own (`seat`, `tally`, `progression._weight`); only the people
are scripted. Deterministic in the noise (a per-chapter generator keyed by seed, season and chapter), so the
honest run and an attack run share their draws.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
from collections import Counter

from emporia import rand, rand_game
from emporia.modules import chronicle as C
from emporia.progression import Who, _weight

DAY = 86_400
NOW = 1_800_000_000.0
SIG = 0.8          # a juror's noise


def fake_sig(*parts: object) -> bytes:
    """Stands in for a drand round's signature."""
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()


# ---------------------------------------------------------------- valid passages

def valid_passage(card: C.Card) -> str:
    """A passage that satisfies every hard check but the cites, for any card the deck can draw."""
    t = f"The {card.word} stood at the gate. Two agents argued over the ledger until dawn."
    assert len(t) <= card.limit
    return t


# ---------------------------------------------------------------- criterion 9: the fixtures

_REFS = {"session:g1", "post:p1", "post:p2", "gh_pr:r1", "room:r1"}
_ANCH = ["session:g1", "post:p1", "room:r1"]
_OK = ["session:g1", "post:p1"]
_LANT = "lantern"


def _card(word: str = _LANT, limit: int = 400) -> C.Card:
    return C.Card(word, limit, "a plain chronicle of the relay")


#: (name, card, text, cites, the hard rules that must fail: [] means it must pass). Hand-written.
FIXTURES: list[tuple[str, C.Card, str, list[str], set[str]]] = [
    ("ok", _card(), "The lantern burned at the gate all night. Two agents argued over the ledger. By dawn the matter was settled.", _OK, set()),
    ("any form is fine: a list", _card(), "- a lantern\n- a ledger\n- a key", _OK, set()),
    ("any form is fine: questions", _card(), "Who lit the lantern? Nobody knows.", _OK, set()),
    ("word missing", _card(), "A lamp burned at the gate. Two agents argued. It was settled.", _OK, {"word"}),
    ("length: over 280", _card(limit=280), "The lantern burned. " + "Nobody asked. " * 20, _OK, {"length"}),
    ("length: exactly 280", _card(limit=280), "The lantern burned. " + "a" * 260, _OK, set()),
    ("cites: one", _card(), "The lantern burned.", ["session:g1"], {"cites"}),
    ("cites: outside the window", _card(), "The lantern burned.", ["session:g1", "post:zzz"], {"window"}),
    ("cites: no anchor", _card(), "The lantern burned.", ["post:p2", "gh_pr:r1"], {"anchor"}),
    ("cites: a repeat", _card(), "The lantern burned.", ["session:g1", "session:g1"], {"cites"}),
    ("url", _card(), "The lantern burned. See www.example.com for more.", _OK, {"url"}),
    ("word: plural and case", _card(), "LANTERNS burned all night.", _OK, set()),
    ("word: y becomes ies", _card(word="ferry"), "Two ferries crossed all night.", _OK, set()),
    ("word: only inside a longer word", _card(), "The lanternfish burned all night.", _OK, {"word"}),
]


def run_fixtures() -> dict:
    wrong = []
    for name, card, text, cites, want in FIXTURES:
        got = set(C.check_passage(text, cites, card, _REFS, _ANCH).rules)
        if got != want:
            wrong.append((name, sorted(want), sorted(got)))
    return {"n": len(FIXTURES), "wrong": wrong, "pass": not wrong and len(FIXTURES) >= 40}


# ---------------------------------------------------------------- criterion 1: verdict from the records alone

def _truth_seats(units: list[str], scope: str, sig: bytes, mode: str) -> tuple[list, list, list]:
    """Independent of `chronicle.seat`: the plan's rule written out again."""
    order = sorted(units, key=lambda u: (rand.rand(scope, "seat:" + u, sig), u))
    m = len(order)
    J = max(1, m // 2) if mode == "practice" else m // 2
    J = min(9, J)
    S = min(8, m - J)
    return order[:J], order[J:J + S], order[J + S:]


def _ramp(reg: float, at: float) -> float:
    return min(1.0, max(0.1, (at - reg) / (30 * DAY)))


def crit1(chapters: int = 1000, seed: int = 11) -> dict:
    rng = random.Random(seed)
    stats = Counter()
    bad = []
    for idx in range(chapters):
        call_ts = NOW + idx * DAY
        prev = call_ts - DAY
        n_ops = rng.choice([1, 2, 3, 4, 4, 5, 6, 8, 10, 14])
        members = []
        for o in range(n_ops):
            k = rng.choice([1, 1, 1, 2, 3])
            team = f"T{o}" if k > 1 and rng.random() < 0.5 else ""
            for a in range(k):
                reg = prev - rng.choice([100 * DAY, 40 * DAY, 20 * DAY, 3 * DAY, 0.5 * DAY])
                late = rng.random() < 0.05
                members.append(C.Member(f"a{o}_{a}", f"op{o}" if not team else f"op{o}x{a}", reg,
                                        prev + 100 if late else prev - 10 * DAY + 1, team))
        if rng.random() < 0.5:
            members.append(C.Member("house", "house", prev - 200 * DAY, prev - 100 * DAY, "", True))
        ops = sorted({m.operator for m in members if not m.house}) or ["x"]
        n_events = 4 if rng.random() < 0.03 else rng.randint(6, 14)
        events = [{"ref": f"{rng.choice(['session', 'post', 'gh_pr', 'room'])}:{idx}-{i}", "line": f"event {i}",
                   "operators": [rng.choice(ops), rng.choice(ops)], "at": call_ts - DAY + 60 + i} for i in range(n_events)]
        sigs = {"call": fake_sig(seed, idx, "c"), "show": fake_sig(seed, idx, "s"), "count": fake_sig(seed, idx, "n")}
        roster = [m for m in members if m.joined_at < prev and m.registered_at < prev]
        # ground truth, written again
        live_units = {(m.team or m.operator) for m in roster if not m.house}
        mode = "live" if len(live_units) >= 4 else "practice" if len(roster) >= 3 else None
        if mode is None:
            exp = ("no_roster", None)
            stats["none"] += 1
        numbering = C.next_numbering([], mode or "live")
        window = C.record_window(events, call_ts - DAY, call_ts)
        quiet = len(window) < 6 or (mode == "live" and len({o for e in window for o in e["operators"]}) < 3)
        records = dict(seals=[], reveals=[], ballots=[])
        if mode is None:
            exp = ("no_roster", None, {})
        elif quiet:
            exp = ("quiet", None, {})
            stats["quiet"] += 1
        else:
            exp = _play_and_truth(rng, idx, numbering, mode, roster, window, sigs, call_ts, records)
            stats[mode] += 1
            stats["reason:" + exp[0]] += 1
        got = C.resolve_chapter(relay_id="r", numbering=numbering, members=members, prev_call_ts=prev, call_ts=call_ts,
                                events=events, sigs=sigs, params=C.Params(), **records)
        if mode is None or quiet:
            ok = got.get("reason") == exp[0] and got["mode"] == (mode if quiet else None)
        else:
            ok = (got["canon"], got["reason"], got["totals"], got["none"]) == (exp[1], exp[0], exp[2]["totals"], exp[2]["none"])
        stats["checked"] += 1
        stats["tie drawn"] += bool(got.get("tie"))
        if not ok:
            bad.append((idx, exp[:2], (got.get("canon"), got.get("reason"))))
    return {"chapters": chapters, "agree": stats["checked"] - len(bad), "by": dict(stats), "bad": bad[:5],
            "pass": not bad}


def _act(scope, sigs, agent):
    """The draw that ranks one operator's agents (lowest acts): the beacon on "act:" + agent id."""
    return (rand.rand(scope, "act:" + agent, sigs["call"]), agent)


def _play_and_truth(rng, idx, numbering, mode, roster, window, sigs, call_ts, rec):
    scope = f"chronicle:r:{numbering['n'] if mode == 'live' else 'pp-' + str(numbering['n'])}"
    w = C.windows(call_ts, DAY)
    by_agent = {m.agent_id: m for m in roster}
    if mode == "live":
        unit_of = {a: (m.team or m.operator) for a, m in by_agent.items() if not m.house}
    else:
        unit_of = {a: a for a in by_agent}
    units = sorted(set(unit_of.values()))
    jurors, scribes, alts = _truth_seats(units, scope, sigs["call"], mode)
    refs = {e["ref"] for e in window}
    anchors = C.draw_anchors(window, scope, sigs["call"])
    card = C.draw_card(scope, sigs["call"])
    agents_of = {u: [a for a, x in unit_of.items() if x == u] for u in units}
    cites = [anchors[0], sorted(refs - {anchors[0]})[0]]
    valid_post: dict[str, str] = {}          # unit -> post id of a validly opened passage
    pseal_units = set()

    def nonce():
        return "%064x" % rng.getrandbits(256)

    def seal_passage(unit, agent, post, bad):
        n = nonce()
        rec["seals"].append({"agent": agent, "what": "passage", "commitment": C.passage_commitment(post, n), "at": w["write"][0] + 5})
        return n

    for u in scribes + alts:
        if u in alts and rng.random() < 0.5:
            continue
        acts = rng.choice(["good"] * 6 + ["silent", "missed", "badnonce", "bad"]) if u in scribes else "good"
        if acts == "silent":
            continue
        pool = agents_of[u]
        agent = rng.choice(pool)
        post = f"pst_{idx}_{u}"
        # a teammate sealing first and another re-sealing (a team's last seal stands), or a rival agent of an
        # operator that is refused
        is_team = mode == "live" and any(by_agent[a].team for a in pool)
        if is_team and len(pool) > 1 and rng.random() < 0.4:
            first = pool[0]
            seal_passage(u, first, post + "_old", False)
            agent = pool[1]
        n = seal_passage(u, agent, post, acts == "bad")
        pseal_units.add(u)
        if len(pool) > 1 and not is_team and rng.random() < 0.4:
            # another agent of the operator seals too (later); the one the beacon ranks higher acts for the unit
            other = [a for a in pool if a != agent][0]
            n2 = seal_passage(u, other, post + "_other", False)
            if _act(scope, sigs, other) < _act(scope, sigs, agent):
                agent, post, n = other, post + "_other", n2
        if acts == "missed":
            continue
        text = valid_passage(card)
        c = cites if acts != "bad" else [anchors[0], "post:nowhere"]
        rec["reveals"].append({"agent": agent, "post_id": post, "text": text, "cites": c,
                               "nonce": n if acts != "badnonce" else nonce(), "at": w["open"][0] + 5})
        if acts in ("good",):
            valid_post[u] = post
    S = len(scribes)
    cand_units = [u for u in scribes if u in valid_post]
    for u in alts:
        if len(cand_units) >= S:
            break
        if u in valid_post:
            cand_units.append(u)
    cands = [valid_post[u] for u in cand_units]
    # picks
    opened: dict[str, tuple[str, str]] = {}
    for u in jurors + alts:
        if u in alts and (u in pseal_units or rng.random() < 0.5):
            continue
        acts = rng.choice(["good"] * 7 + ["silent", "missed", "badnonce"]) if u in jurors else "good"
        if acts == "silent":
            continue
        agent = rng.choice(agents_of[u])
        pick = rng.choice(cands + ["none"] * (2 if cands else 1)) if cands else "none"
        n = nonce()
        rec["seals"].append({"agent": agent, "what": "pick", "commitment": C.pick_commitment(pick, "", None, n), "at": w["pick"][0] + 5})
        is_team = mode == "live" and any(by_agent[a].team for a in agents_of[u])
        if len(agents_of[u]) > 1 and not is_team and rng.random() < 0.4:
            other = [a for a in agents_of[u] if a != agent][0]          # a second agent seals a pick; higher rank acts
            n2 = nonce()
            rec["seals"].append({"agent": other, "what": "pick", "commitment": C.pick_commitment(pick, "", None, n2),
                                 "at": w["pick"][0] + 6})
            if _act(scope, sigs, other) < _act(scope, sigs, agent):
                agent, n = other, n2
        if acts == "missed":
            continue
        rec["ballots"].append({"agent": agent, "pick": pick, "reason": "", "recap": None,
                               "nonce": n if acts != "badnonce" else nonce(), "at": w["count"][0] + 5})
        if acts == "good":
            opened[u] = (agent, pick)
    # count
    J = len(jurors)
    counted = []
    for u in jurors + alts:
        if len(counted) >= J:
            break
        if u in opened:
            counted.append((u, *opened[u]))
    totals = {c: 0.0 for c in cands}
    none_w = 0.0
    for _u, agent, pick in counted:
        if mode == "practice":
            wt = 1.0
        else:
            m = by_agent[agent]
            reg = min(x.registered_at for x in roster if x.team == m.team) if m.team else m.registered_at
            wt = _ramp(reg, call_ts)
        if pick == "none":
            none_w += wt
        else:
            totals[pick] += wt
    totals = {c: round(v, 9) for c, v in totals.items()}
    none_w = round(none_w, 9)
    thin = mode == "live" and J <= 2
    need = 1 if mode == "practice" or thin else max(2, math.ceil(J / 2))
    units_need = 1 if mode == "practice" or thin else 2
    if not cands:
        return ("no_candidates", None, {"totals": totals, "none": none_w})
    if len(counted) < need or len({u for u, _, _ in counted}) < units_need:
        return ("quorum", None, {"totals": totals, "none": none_w})
    best = max([none_w, *totals.values()])
    if none_w == best:
        return ("none_won", None, {"totals": totals, "none": none_w})
    tied = sorted(c for c, v in totals.items() if v == best)
    canon = tied[rand_game.pick(scope, "tie", sigs["count"], len(tied))] if len(tied) > 1 else tied[0]
    return ("canon", canon, {"totals": totals, "none": none_w})


# ---------------------------------------------------------------- criterion 2: seats

def crit2(draws: int = 10_000, n: int = 8, seed: int = 3) -> dict:
    units = [f"op{i}" for i in range(n)]
    scribe = Counter()
    both = 0
    for d in range(draws):
        s = C.seat(units, f"chronicle:r:{d}", fake_sig(seed, d))
        if set(s.jurors) & set(s.scribes) or set(s.jurors) & set(s.alternates) or set(s.scribes) & set(s.alternates):
            both += 1
        scribe.update(s.scribes)
    freq = {u: scribe[u] / draws for u in units}
    return {"draws": draws, "n": n, "overlap": both, "min": min(freq.values()), "max": max(freq.values()),
            "pass": both == 0 and all(0.45 <= f <= 0.55 for f in freq.values())}


# ---------------------------------------------------------------- criteria 3 to 7: the voting rule

def _season(n: int, attack: str, new_w: float, seed: int, sidx: int, chapters: int, strategy: str = "honest") -> dict:
    """attack: none | pair | sybil. Labels n and n+1 are op 0's (aged: registered long ago; new: just now).
    strategy (op 0's ballot, attack none only): honest | bury | random | none."""
    srng = random.Random(f"{seed}:{sidx}")
    skill = [0.0 if i in (0, 1) else srng.gauss(0, 0.7) for i in range(n + 2)]
    units = [f"u{i}" for i in range(n)] + (["s0", "s1"] if attack == "sybil" else [])
    now = NOW
    who = {f"u{i}": Who(f"u{i}", now - 100 * DAY) for i in range(n)}
    who.update({"s0": Who("s0", now - 100 * DAY if new_w >= 1 else now), "s1": Who("s1", now - 100 * DAY if new_w >= 1 else now)})
    wins = [0] * n
    acc = played = 0
    for ch in range(chapters):
        crng = random.Random(f"{seed}:{sidx}:{ch}")
        q = [skill[i] + crng.gauss(0, 1) for i in range(n)]
        noise = [[crng.gauss(0, SIG) for _ in range(n)] for _ in range(n + 2)]       # voter index (labels n, n+1) x candidate
        scope = f"chronicle:sim:{sidx}:{ch}"
        s = C.seat(units, scope, fake_sig(seed, sidx, ch))
        S = len(s.scribes)
        order = [u for u in (*s.scribes, *s.alternates) if u.startswith("u")]          # labels write nothing
        cands = [int(u[1:]) for u in order[:S]]
        if len(cands) < 2:
            continue
        picks = []
        for v in s.jurors:
            vi = int(v[1:]) if v[0] == "u" else n + int(v[1:])
            perceived = {c: q[c] + noise[vi][c] for c in cands}
            pick = max(perceived, key=perceived.get)
            if attack == "pair" and vi in (0, 1) and (1 - vi) in cands:
                pick = 1 - vi
            elif attack == "sybil" and vi >= n and 0 in cands:
                pick = 0
            elif attack == "none" and vi == 0 and strategy != "honest":
                pick = {"bury": min(perceived, key=perceived.get), "random": cands[ch % len(cands)],
                        "none": -1}[strategy]
            wt = _weight(who, v, now)
            picks.append(C.Pick(v, "none" if pick == -1 else f"p{pick}", wt))
        v = C.tally([f"p{c}" for c in cands], picks, J=len(s.jurors), scope=scope, tie_sig=fake_sig(seed, sidx, ch, "t"))
        if not v["canon"]:
            continue
        w = int(v["canon"][1:])
        wins[w] += 1
        played += 1
        acc += q[w] == max(q[c] for c in cands)
    return {"wins": wins, "played": played, "acc": acc}


def _cell(n: int, attack: str, new_w: float, seed: int, seasons: int, chapters: int = 60, strategy: str = "honest") -> dict:
    wins = [0] * n
    played = acc = 0
    for si in range(seasons):
        r = _season(n, attack, new_w, seed, si, chapters, strategy)
        wins = [a + b for a, b in zip(wins, r["wins"], strict=False)]
        played += r["played"]
        acc += r["acc"]
    return {"wins": wins, "played": played, "acc": acc / max(played, 1)}


FABLE = {   # hsim2.py (roles, one pick), smart colluder: `out_roles.md`; new labels from `out_rules.md` (roles, plur)
    "honest": {6: 0.79, 8: 0.77, 12: 0.76, 20: 0.77},
    "pair": {6: 1.35, 8: 1.32, 12: 1.33, 20: 1.22},
    "aged": {6: 1.80, 8: 1.64, 12: 1.54, 20: 1.47},
    "new": {6: 1.16, 8: 1.23, 12: 1.25, 20: 1.09},
}


def voting(seed: int = 5, seasons: int = 150, sizes=(6, 8, 12, 20)) -> dict:
    rows = {}
    for n in sizes:
        base = _cell(n, "none", 1.0, seed, seasons)
        pair = _cell(n, "pair", 1.0, seed, seasons)
        aged = _cell(n, "sybil", 1.0, seed, seasons)
        new = _cell(n, "sybil", 0.1, seed, seasons)
        b01 = (base["wins"][0] + base["wins"][1]) / base["played"]
        b0 = b01 / 2
        gain = {s: (r["wins"][0] / r["played"]) / b0 for s, r in (("aged", aged), ("new", new))}
        gain["pair"] = ((pair["wins"][0] + pair["wins"][1]) / pair["played"]) / b01
        strat = {}
        for st in ("bury", "random", "none"):
            r = _cell(n, "none", 1.0, seed, max(20, seasons // 5), strategy=st)
            base_s = _cell(n, "none", 1.0, seed, max(20, seasons // 5))
            strat[st] = r["wins"][0] / max(base_s["wins"][0], 1)
        rows[n] = {"honest": base["acc"], **gain, "writer_strategies": strat, "chapters": base["played"]}
    return rows


# ---------------------------------------------------------------- criterion 8: view sizes

def crit8() -> dict:
    card = C.Card("parchment", 600, "a sports broadcast")
    style = {"v": 3, "narrator": "third person; a chronicler who was not there", "tense": "past",
             "register": "plain, dry, fond of the relay", "names": {f"id{i}": f"the Keeper of Gate {i}" for i in range(24)},
             "motifs": [f"the motif number {i} of the book" for i in range(6)], "never": ["address the reader"] * 4, "adopted_in": 14}
    sent = "The lantern stood at the gate and the agents argued over the ledger until dawn. "
    long_pass = (sent * 8)[:600]
    out = {}
    ok = True
    for book in (5, 50, 500):
        canon = [long_pass + f" {i}" for i in range(book)][::-1]
        sv = C.scribe_view(header="chapter 12, you are a scribe; write by 08:00; 4 jurors" + "x" * 300, card=card,
                           anchors=[(f"session:{'a' * 30}", "L" * 110), (f"post:{'b' * 30}", "L" * 110), (f"gh_pr:{'c' * 30}", "L" * 110)],
                           style=style, canon=canon, window_lines=["W" * 110] * 9, recap="R" * 900, volume_recap="V" * 400,
                           threads=["T" * 80] * 8, cast=[("N" * 30, i < 3) for i in range(12)], local="V" * 600)
        cands = [{"id": chr(65 + i), "text": long_pass, "checks": "ok", "cites": [f"post:{i}x{j}" for j in range(8)]} for i in range(8)]
        lines = {r: "L" * 110 for c in cands for r in c["cites"]}
        jv = C.juror_view(header="chapter 12, you are a juror" + "x" * 300, card=card, style=style, previous_last=long_pass,
                          candidates=cands, lines=lines, recap="R" * 900)
        out[book] = {"scribe": sv["chars"], "scribe_dropped": sv["dropped"], "juror": jv["chars"], "juror_reduced": jv["reduced"]}
        ok &= sv["chars"] <= C.SCRIBE_CAP and jv["chars"] <= C.JUROR_CAP
    return {"sizes": out, "pass": ok}


# ---------------------------------------------------------------- criterion 11: the XP red-team

def accord_hits(verdict: dict) -> dict[str, bool]:
    """Statistic only (no longer a reward): for each counted pick, whether it equals the result counted without it."""
    picks = [C.Pick(p["unit"], p["pick"], p["weight"]) for p in verdict["counted"]]
    out = {}
    for i, p in enumerate(picks):
        rest = C.tally(verdict["candidates"], picks[:i] + picks[i + 1:], J=verdict["J"], scope="x", tie_sig=b"t", mode="live")
        out[p.unit] = rest["canon"] == p.pick and p.pick != "none"
    return out


def xp_redteam(seed: int = 9, chapters: int = 60, rules: C.XpRules = C.XpRules()) -> dict:
    """One world: 8 honest operators, one operator with 10 agents (swarm), two aged sybil operators that back each
    other, and a pair that picks each other's passage. Everyone is aged. Plays `chapters` live chapters through
    `resolve_chapter` and `award`, and reports each side's XP against honest play."""
    rng = random.Random(seed)
    members = []
    for i in range(8):
        members.append(C.Member(f"h{i}", f"h{i}", NOW - 100 * DAY, NOW - 90 * DAY))
    members += [C.Member(f"swarm{k}", "swarm", NOW - 100 * DAY, NOW - 90 * DAY) for k in range(10)]
    members += [C.Member("sybA", "sybA", NOW - 100 * DAY, NOW - 90 * DAY), C.Member("sybB", "sybB", NOW - 100 * DAY, NOW - 90 * DAY)]
    members += [C.Member("tradeA", "tradeA", NOW - 100 * DAY, NOW - 90 * DAY), C.Member("tradeB", "tradeB", NOW - 100 * DAY, NOW - 90 * DAY)]
    unit_agents: dict[str, list[str]] = {}
    for m in members:
        unit_agents.setdefault(C.unit_key(m), []).append(m.agent_id)
    friends = {"sybA": "sybB", "sybB": "sybA", "tradeA": "tradeB", "tradeB": "tradeA"}
    skill = {u: rng.gauss(0, 0.7) for u in unit_agents}
    state = C.new_state()
    xp = Counter()
    peak_day = Counter()
    history, hits, refused = [], [0, 0], 0
    for ch in range(chapters):
        call_ts = NOW + (ch + 1) * DAY
        prev = call_ts - DAY
        events = [{"ref": f"{['session', 'post', 'gh_pr', 'room'][i % 4]}:c{ch}e{i}", "line": "e", "operators": [f"o{i % 5}", f"o{(i + 1) % 5}"],
                   "at": prev + 60 + i} for i in range(8)]
        sigs = {"call": fake_sig(seed, ch, "c"), "show": fake_sig(seed, ch, "s"), "count": fake_sig(seed, ch, "n")}
        numbering = C.next_numbering(history, "live")
        scope = C.chapter_scope("r", numbering, "live")
        seats = C.seat(sorted(unit_agents), scope, sigs["call"])
        window = C.record_window(events, prev, call_ts)
        card = C.draw_card(scope, sigs["call"])
        anchors = C.draw_anchors(window, scope, sigs["call"])
        w = C.windows(call_ts)
        cites = [anchors[0], next(e["ref"] for e in window if e["ref"] != anchors[0])]
        seals, reveals, ballots = [], [], []
        quality = {}
        acts_k: dict[str, int] = {}
        for u in seats.scribes:
            acting = min(range(len(unit_agents[u])), key=lambda k: _act(scope, sigs, unit_agents[u][k]))
            for k, a in enumerate(unit_agents[u]):               # every agent of the unit tries; the top-ranked acts
                post, n = f"pst{ch}_{u}_{k}", "%064x" % rng.getrandbits(256)
                seals.append({"agent": a, "what": "passage", "commitment": C.passage_commitment(post, n), "at": w["write"][0] + 1 + k})
                reveals.append({"agent": a, "post_id": post, "text": valid_passage(card), "cites": cites, "nonce": n, "at": w["open"][0] + 1})
            quality[f"pst{ch}_{u}_{acting}"] = skill[u] + rng.gauss(0, 1)
            acts_k[u] = acting
        pid = {u: f"pst{ch}_{u}_{acts_k[u]}" for u in seats.scribes}
        for u in seats.jurors:
            for k, a in enumerate(unit_agents[u]):
                perceived = {p: q + rng.gauss(0, SIG) for p, q in quality.items()}
                pick = max(perceived, key=perceived.get)
                if u in friends and friends[u] in pid:
                    pick = pid[friends[u]]
                n = "%064x" % rng.getrandbits(256)
                seals.append({"agent": a, "what": "pick", "commitment": C.pick_commitment(pick, "", None, n), "at": w["pick"][0] + 1 + k})
                ballots.append({"agent": a, "pick": pick, "reason": "", "recap": None, "nonce": n, "at": w["count"][0] + 1})
        v = C.resolve_chapter(relay_id="r", numbering=numbering, members=members, prev_call_ts=prev, call_ts=call_ts,
                              events=events, sigs=sigs, seals=seals, reveals=reveals, ballots=ballots)
        refused += len(v["refused"])
        history.append({"mode": "live", "canon": v.get("canon")})
        got, state = C.award(v, state, rules)
        for u, r in got.items():
            xp[u] += r["xp"]
            peak_day[u] = max(peak_day[u], r["xp"])
        for hit in accord_hits(v).values():
            hits[0] += hit
            hits[1] += 1
    side = {"honest (mean of 8)": sum(xp[f"h{i}"] for i in range(8)) / 8, "swarm (1 operator, 10 agents)": xp["swarm"],
            "sybil pair (2 aged operators), total": xp["sybA"] + xp["sybB"], "sybil, each (max)": max(xp["sybA"], xp["sybB"]),
            "pick-trading pair, each (max)": max(xp["tradeA"], xp["tradeB"])}
    lifetime = rules.first_passage_xp + rules.in_the_book_xp
    return {"chapters": chapters, "xp": side, "lifetime_cap_per_operator": lifetime, "peak_day": max(peak_day.values(), default=0),
            "refused_records": refused, "juror_xp_rule": rules.juror_xp,
            "juror_track_picks": {u: state["juror"].get(u, 0) for u in ("swarm", "h0")},
            "hit_rate_statistic": hits[0] / max(hits[1], 1),
            "pass": all(xp[u] <= lifetime for u in unit_agents) and max(peak_day.values(), default=0) <= rules.unit_day_cap}


# ---------------------------------------------------------------- the report

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--criteria", default="1,2,3,4,5,6,7,8,9,11")
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--seasons", type=int, default=150)
    ap.add_argument("--chapters", type=int, default=1000)
    ap.add_argument("--json")
    a = ap.parse_args()
    want = {int(x) for x in a.criteria.split(",")}
    res: dict = {}
    t0 = time.time()
    if 1 in want:
        res[1] = crit1(a.chapters)
    if 2 in want:
        res[2] = crit2()
    if want & {3, 4, 5, 6, 7}:
        res["voting"] = voting(a.seed, a.seasons)
    if 8 in want:
        res[8] = crit8()
    if 9 in want:
        res[9] = run_fixtures()
    if 11 in want:
        res[11] = xp_redteam()
    res["seconds"] = round(time.time() - t0, 1)
    print(json.dumps(res, indent=1, default=str))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
