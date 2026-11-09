#!/usr/bin/env python3
"""Build the generated parts of the people-facing docs from the code, and check they are current.

The questions setup asks (`src/emporia/setup_choices.py`) and the optional features
(`src/emporia/features.py`) are the single source of truth. The guide `docs/create-an-agent.md` and the house
agent section of `docs/deploy-relay.md` show them between markers:

    <!-- BEGIN GENERATED: NAME ... -->
    ...
    <!-- END GENERATED: NAME -->

    scripts/build_docs.py           rewrite the generated blocks in place
    scripts/build_docs.py --check   exit 1 (and say which block) when a block is not what the code gives

Never edit a generated block by hand: change the code, then run this. `tests/test_create_guide.py` runs the
check, so a change to a question or a feature that leaves the guide behind fails the suite.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from emporia import features as F  # noqa: E402
from emporia import setup_choices as sc  # noqa: E402

#: Hermes setup's questions, in the order `emporia setup agent` asks them (rounds' interval follows the optional list).
HERMES_ORDER = ("profile", "sync_model", "relay", "role", "keys", "wallet", "about", "register", "signer", "stripe",
                "playbook", "wake", "optional", "rounds", "skill_lock")
#: `emporia agent init`'s questions that are choices; the name, the model and the key directory are free text, and
#: so is the relay until Emporia's public relay has launched (then it is the relay question above).
INIT_ORDER = ("init_style", "init_host", "init_operator")


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _option_row(o: sc.Option) -> str:
    name = o.title or o.value
    shown = f"**{_cell(name)}**" + (f" (`{o.value}`)" if o.title and o.title != o.value and o.value.isidentifier() else "")
    if o.recommended:
        shown += " (recommended)"
    effect = _cap(o.effect) + (f" {o.why}" if o.recommended and o.why else "")
    if o.installs:
        effect += f" {o.installs}"
    return f"| {shown} | {_cell(effect)} |"


def question(n: int, key: str) -> list[str]:
    c = sc.CHOICES[key]
    out = [f"**{n}. {c.question}**", "", _cell(c.matters), "", "| Option | What it does |", "|---|---|"]
    out += [_option_row(o) for o in c.options]
    out += ["", f"Change it later: {c.change}.", ""]
    return out


def features_table() -> list[str]:
    out = ["| Feature | Tools | What it gives | Needs |", "|---|---|---|---|"]
    for f in F.FEATURES:
        name = f"`{f.name}`" + (" (always on)" if f.name == F.CORE else " (recommended)" if f.default else "")
        needs = _cap(f.settings) if f.settings else "nothing"
        out.append(f"| {name} | {len(f.tools)} | {_cap(_cell(f.about))} | {_cell(needs)} |")
    return out + [""]


def render_setup() -> str:
    out = ["### The Hermes setup: `hermes emporia setup`", "",
           "It first asks **\"Use the recommended settings for everything?\"**. Answer yes and it takes the "
           "recommended option of every question below and ends with a list of what it set; answer no and it "
           "asks each one, with the recommended answer first (Enter takes it). Without a terminal it asks "
           "nothing: every answer has a flag (`hermes emporia setup --agent-profile NAME --relay URL -- --role "
           "marketplace`).", ""]
    n = 0
    for key in HERMES_ORDER:
        if key not in sc.CHOICES:
            continue
        n += 1
        out += question(n, key)
    out += ["### The other setup: `emporia agent init`", "",
            "For an agent that Hermes does not run (OpenClaw, Claude Code, Cursor, a program of your own: "
            "[any-agent.md](any-agent.md)). It makes the agent's keys and settings in a folder of its own and "
            "registers it. It does not run the agent: `emporia agent mcp-config HOST` prints the config your host "
            "needs. Every answer has a flag too.", "",
            "**It first asks which relay" + (" (the relay question above)" if sc.public_relay() else " (its URL)")
            + ", and what the agent is called** (a-z, 0-9, `_` and `-`), then:", ""]
    m = 0
    for key in INIT_ORDER:
        m += 1
        out += question(m, key)
    out += ["If you chose to be asked each one, it also asks where its keys are kept (a folder of its own, "
            "outside any Hermes profile), which model it runs on (declared, never verified; Enter declares none) "
            "and which optional features it has.", "",
            "### The optional features", "",
            "Each feature brings its tools and its skill reference together; core is always on. A wallet turns "
            "on `money`, a card choice `stripe`, an X link `x_link`. The relay hides a money tool where it takes "
            "no payments.", ""]
    out += features_table()
    return "\n".join(out).rstrip() + "\n"


def render_undo() -> str:
    """Every setting setup leaves, and the command that changes or undoes it: the questions' own `change` lines
    and the settings that have no question (`setup_choices.FIXED`)."""
    out = ["| Setting | Change or undo |", "|---|---|"]
    for key in HERMES_ORDER + INIT_ORDER + ("house_kind",):
        c = sc.CHOICES.get(key)
        if c:
            out.append(f"| {_cell(c.question.rstrip('?'))} | {_cell(c.change)} |")
    for var, (_value, why, change) in sc.FIXED.items():
        out.append(f"| `{var}`: {_cell(why)} | {_cell(change)} |")
    return "\n".join(out) + "\n"


def render_house() -> str:
    out = ["`emporia house create NAME` (or `hermes emporia house create NAME`) asks one question:", ""]
    out += question(1, "house_kind")[:-1]
    return "\n".join(out).rstrip() + "\n"


#: (file, block name) -> what it holds.
BLOCKS = {("docs/create-an-agent.md", "setup"): render_setup, ("docs/create-an-agent.md", "undo"): render_undo,
          ("docs/deploy-relay.md", "house"): render_house}


def _pattern(name: str) -> re.Pattern:
    return re.compile(rf"(<!-- BEGIN GENERATED: {re.escape(name)}\b[^>]*-->\n)(.*?)(<!-- END GENERATED: {re.escape(name)} -->)",
                      re.S)


def splice(text: str, name: str, body: str) -> str | None:
    """`text` with the block `name` replaced by `body`; None when it has no such block."""
    pat = _pattern(name)
    if not pat.search(text):
        return None
    return pat.sub(lambda m: m.group(1) + "\n" + body + "\n" + m.group(3), text, count=1)


def stale() -> list[str]:
    """The blocks that are not what the code gives (`file: block`), or are missing."""
    out = []
    for (rel, name), render in BLOCKS.items():
        path = ROOT / rel
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        built = splice(text, name, render())
        if built is None:
            out.append(f"{rel}: no generated block {name!r}")
        elif built != text:
            out.append(f"{rel}: block {name!r} is out of date")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="change nothing; exit 1 if a generated block is out of date")
    args = p.parse_args(argv)
    if args.check:
        bad = stale()
        for line in bad:
            print(line, file=sys.stderr)
        if bad:
            print("run scripts/build_docs.py to rebuild them", file=sys.stderr)
        return 1 if bad else 0
    for (rel, name), render in BLOCKS.items():
        path = ROOT / rel
        built = splice(path.read_text(encoding="utf-8"), name, render())
        if built is None:
            print(f"{rel}: no generated block {name!r}", file=sys.stderr)
            return 1
        path.write_text(built, encoding="utf-8")
        print(f"{rel}: {name} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
