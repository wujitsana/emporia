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


# ---------------------------------------------------------------- valid passages, for every shape

def valid_passage(card: C.Card) -> str:
    """A passage that satisfies every hard check but the cites, for any card the decks can draw."""
    W = card.word
    t = {
        "only_questions": f"Who carried the {W} to the gate? Did anyone ask why?",
        "three_sentences": f"The {W} stood at the gate. Two agents argued over the ledger. By dawn it was settled.",
        "short_sentences": f"The {W} stood at the gate. Two agents argued. Dawn settled it.",
        "one_quote": f'The {W} stood at the gate. "Settle it," said one agent. The other agreed.',
        "distinct_initials": f"The {W} stood at the gate. Agents argued. Dawn settled it. Some left.",
        "list_of_five": f"- the {W}\n- the ledger\n- a gate\n- two agents\n- one dawn",
        "second_person": f"You carried the {W} to the gate. Your ledger stayed shut.",
        "echo_end": f"{W.capitalize()} light fell on the gate and the ledger. Two agents argued until dawn, then settled it by {W}",
        "two_paragraphs": f"The {W} stood at the gate.\n\nTwo agents argued until dawn.",
        "no_commas": f"The {W} stood at the gate. Two agents argued until dawn.",
        "one_numeral": f"The {W} stood at the gate for 3 nights. Two agents argued.",
        "the_start": f"The {W} stood at the gate. The agents argued. The matter was settled.",
        "ends_with_question": f"The {W} stood at the gate. Who settled it?",
        "five_sentences": f"The {W} stood at the gate. Agents argued. Night fell. Dawn came. It was settled.",
        "no_long_words": f"The {W} stood at the gate. Two agents argued until dawn.",
        "one_semicolon": f"The {W} stood at the gate; two agents argued until dawn.",
    }[card.shape]
    assert len(t) <= card.limit
    return t


# ---------------------------------------------------------------- criterion 9: the fixtures

_REFS = {"session:g1", "post:p1", "post:p2", "gh_pr:r1", "room:r1"}
_ANCH = ["session:g1", "post:p1", "room:r1"]
_OK = ["session:g1", "post:p1"]
_LANT = "lantern"


def _card(shape: str, word: str = _LANT, limit: int = 400) -> C.Card:
    return C.Card(word, limit, shape, "myth")


#: (name, card, text, cites, the hard rules that must fail: [] means it must pass). Hand-written.
FIXTURES: list[tuple[str, C.Card, str, list[str], set[str]]] = [
    ("three ok", _card("three_sentences"), "The lantern burned at the gate all night. Two agents argued over the ledger. By dawn the matter was settled.", _OK, set()),
    ("three: four sentences", _card("three_sentences"), "The lantern burned. Two agents argued. Dawn came. It ended.", _OK, {"shape"}),
    ("three: word missing", _card("three_sentences"), "A lamp burned at the gate. Two agents argued. It was settled.", _OK, {"word"}),
    ("three: word missing and two sentences", _card("three_sentences"), "A lamp burned at the gate. Two agents argued.", _OK, {"word", "shape"}),
    ("questions ok", _card("only_questions"), "Who lit the lantern at the gate? Did anyone ask why? Was it the ledger?", _OK, set()),
    ("questions: one statement", _card("only_questions"), "Who lit the lantern? Nobody knows.", _OK, {"shape"}),
    ("short ok", _card("short_sentences"), "The lantern swung. Two agents met at the gate. They traded one story each.", _OK, set()),
    ("short: a 14-word sentence", _card("short_sentences"), "The lantern swung above the gate while two agents argued about the ledger until morning came.", _OK, {"shape"}),
    ("quote ok", _card("one_quote"), 'The lantern guttered. "Sell it," said the first agent. The second shook its head.', _OK, set()),
    ("quote: none", _card("one_quote"), "The lantern guttered. The agents left.", _OK, {"shape"}),
    ("quote: two lines of speech", _card("one_quote"), '"Sell," said one. "No," said the lantern keeper.', _OK, {"shape"}),
    ("initials ok", _card("distinct_initials"), "The lantern burned. Agents argued. Morning came. Some left.", _OK, set()),
    ("initials: two T", _card("distinct_initials"), "The lantern burned. Two agents argued.", _OK, {"shape"}),
    ("list ok", _card("list_of_five", limit=280), "- a lantern\n- a ledger\n- a key\n- a bell\n- a rope", _OK, set()),
    ("list: four items", _card("list_of_five", limit=280), "- a lantern\n- a ledger\n- a key\n- a bell", _OK, {"shape"}),
    ("list: no hyphens", _card("list_of_five", limit=280), "a lantern\na ledger\na key\na bell\na rope", _OK, {"shape"}),
    ("second person ok", _card("second_person"), "You carried the lantern to the gate. You told nobody. Your ledger stayed shut.", _OK, set()),
    ("second person: a third-person sentence", _card("second_person"), "You carried the lantern. The gate stayed shut.", _OK, {"shape"}),
    ("echo ok", _card("echo_end"), "Lantern light spread over the gate and the ledger and the sleeping market, and all of it was lantern", _OK, set()),
    ("echo: ends elsewhere", _card("echo_end"), "Lantern light fell on the gate.", _OK, {"shape"}),
    ("paragraphs ok", _card("two_paragraphs"), "The lantern burned.\n\nMorning came.", _OK, set()),
    ("paragraphs: one", _card("two_paragraphs"), "The lantern burned. Morning came.", _OK, {"shape"}),
    ("no commas ok", _card("no_commas"), "The lantern burned all night. Nobody asked why.", _OK, set()),
    ("no commas: one comma", _card("no_commas"), "The lantern burned, and nobody asked.", _OK, {"shape"}),
    ("numeral ok", _card("one_numeral"), "The lantern burned for 3 nights. Nobody asked why.", _OK, set()),
    ("numeral: two", _card("one_numeral"), "3 agents lit 2 lanterns at the gate.", _OK, {"shape"}),
    ("the-start ok", _card("the_start"), "The lantern burned. The agents argued. The ledger closed.", _OK, set()),
    ("the-start: one other", _card("the_start"), "The lantern burned. Agents argued.", _OK, {"shape"}),
    ("ends with question ok", _card("ends_with_question"), "The lantern burned. Who lit it?", _OK, set()),
    ("ends with question: no", _card("ends_with_question"), "Who lit the lantern? Nobody.", _OK, {"shape"}),
    ("five sentences ok", _card("five_sentences"), "The lantern burned. Agents argued. Night fell. Dawn came. It ended.", _OK, set()),
    ("short words ok", _card("no_long_words"), "The lantern burned at the gate. Nobody asked why.", _OK, set()),
    ("short words: eleven letters", _card("no_long_words"), "The lantern illuminated the courtyard.", _OK, {"shape"}),
    ("semicolon ok", _card("one_semicolon"), "The lantern burned; nobody asked why.", _OK, set()),
    ("semicolon: two", _card("one_semicolon"), "The lantern burned; nobody asked; nobody cared.", _OK, {"shape"}),
    ("length: over 280", _card("no_commas", limit=280), "The lantern burned. " + "Nobody asked. " * 20, _OK, {"length"}),
    ("length: exactly 280", _card("no_commas", limit=280), "The lantern burned. " + "a" * 260, _OK, set()),
    ("cites: one", _card("no_commas"), "The lantern burned.", ["session:g1"], {"cites"}),
    ("cites: outside the window", _card("no_commas"), "The lantern burned.", ["session:g1", "post:zzz"], {"window"}),
    ("cites: no anchor", _card("no_commas"), "The lantern burned.", ["post:p2", "gh_pr:r1"], {"anchor"}),
    ("cites: a repeat", _card("no_commas"), "The lantern burned.", ["session:g1", "session:g1"], {"cites"}),
    ("url", _card("no_commas"), "The lantern burned. See www.example.com for more.", _OK, {"url"}),
    ("word: plural and case", _card("no_commas"), "LANTERNS burned all night.", _OK, set()),
    ("word: y becomes ies", _card("no_commas", word="ferry"), "Two ferries crossed all night.", _OK, set()),
    ("word: only inside a longer word", _card("no_commas"), "The lanternfish burned all night.", _OK, {"word"}),
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
        roster = [m for m in members if m.enrolled_at < prev and m.registered_at < prev]
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
            seal_passage(u, [a for a in pool if a != agent][0], post + "_other", False)    # refused: another agent first
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
    for u, agent, pick in counted:
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
    need = 1 if mode == "practice" else max(2, math.ceil(J / 2))
    units_need = 1 if mode == "practice" else 2
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
        wins = [a + b for a, b in zip(wins, r["wins"])]
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
    card = C.Card("parchment", 600, "echo_end", "myth")
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


# ---------------------------------------------------------------- the report

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--criteria", default="1,2,3,4,5,6,7,8,9")
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
    res["seconds"] = round(time.time() - t0, 1)
    print(json.dumps(res, indent=1, default=str))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
