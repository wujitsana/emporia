"""The people-facing guide to creating an agent (docs/create-an-agent.md) says only what the code does.

Its setup section is generated from setup_choices.py and features.py by scripts/build_docs.py; a change that
leaves it behind fails here. Every command the guide names must exist (the parser in emporia.cli, the Hermes
plugin's own commands), and every EMPORIA_* setting it names must be one the code reads.
"""
import importlib.util
import re
import shlex
import sys
from pathlib import Path

import pytest

from emporia import cli, features as F, setup_choices as sc

REPO = Path(__file__).resolve().parents[1]
GUIDE = REPO / "docs" / "create-an-agent.md"
#: Everything this work wrote for people; held to the same rules.
PAGES = [GUIDE, REPO / "docs" / "templates" / "agent-SOUL.md", REPO / "docs" / "templates" / "house-SOUL.md"]
SUBS = {"NAME": "alpha", "URL": "http://127.0.0.1:8088", "HANDLE": "alpha_x", "OPERATOR_PROFILE": "op",
        "RELAY_FOLDER": "/tmp/r", "EVERY": "4h"}

_spec = importlib.util.spec_from_file_location("build_docs_t", REPO / "scripts" / "build_docs.py")
build_docs = importlib.util.module_from_spec(_spec)
sys.modules["build_docs_t"] = build_docs
_spec.loader.exec_module(build_docs)

PLUGIN_COMMANDS = {"setup", "status", "doctor", "update", "agent", "archive", "delete", "dms", "features", "guardians",
                   "house", "identity", "lab", "log", "operator", "relay", "role", "rounds", "sheet", "signer",
                   "stats", "visits", "wake", "wallet", "welcome"}


def _top_level_commands() -> set[str]:
    import argparse
    sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return set(sub.choices)


def test_the_generated_blocks_are_what_the_code_gives():
    """Run `scripts/build_docs.py` to fix; --check is the same test from the command line."""
    assert build_docs.stale() == []
    assert build_docs.main(["--check"]) == 0


def test_a_drifting_block_is_caught(tmp_path, monkeypatch):
    text = GUIDE.read_text()
    changed = text.replace("What kind of", "What sort of").replace("Which Hermes profile becomes", "Which profile becomes")
    assert changed != text
    built = build_docs.splice(changed, "setup", build_docs.render_setup())
    built = build_docs.splice(built, "undo", build_docs.render_undo())
    assert built == text, "the generator puts a hand-edited block back"


def test_every_question_setup_asks_is_in_the_guide_in_one_place_or_another():
    covered = (set(build_docs.HERMES_ORDER) | set(build_docs.INIT_ORDER) | {"house_kind"}
               | set(build_docs.RELAY_MONEY_ORDER))
    assert covered == set(sc.CHOICES), "a question missing from scripts/build_docs.py's lists is missing from the guide"
    text = GUIDE.read_text()
    for key in build_docs.HERMES_ORDER + build_docs.INIT_ORDER:
        assert sc.CHOICES[key].question in text, key
        assert sc.CHOICES[key].change.split(";")[0].split(" (")[0] in text or key in ("optional",), key
    for f in F.FEATURES:
        assert f"`{f.name}`" in text, f.name


def test_each_question_shows_exactly_one_recommended_option():
    text = build_docs.render_setup()
    for key in build_docs.HERMES_ORDER + build_docs.INIT_ORDER:
        c = sc.CHOICES[key]
        block = text.split(f". {c.question}**", 1)[1].split("Change it later:", 1)[0]
        marked = block.count("(recommended)")
        assert marked == (len(c.recommended()) if key == "optional" else 1), (key, marked)


def _guide_commands() -> list[str]:
    text = GUIDE.read_text()
    spans = re.findall(r"`((?:hermes (?:-p \w+ )?)?emporia [^`]+)`", text)
    spans += [m.group(1) for m in re.finditer(r"^\s*((?:hermes )?emporia [^\n#]+?)\s*(?:#.*)?$", text, re.M)]
    return [s.strip() for s in spans]


def _argv(command: str) -> list[str] | None:
    part = command.split("&&")[0].split(" | ")[0].strip()
    part = re.sub(r"\[([^\]]*)\]", lambda m: m.group(1).split("|")[0], part)
    part = re.sub(r"\b([a-z-]+)\|[a-z|-]+", r"\1", part)
    part = part.replace("…", "x")
    try:
        return [SUBS.get(a, a) for a in shlex.split(part) if not a.startswith("(")]
    except ValueError:
        return None


def _parses(argv: list[str]) -> bool:
    for attempt in (argv, argv + ["x"]):
        try:
            cli.build_parser().parse_args(attempt)
            return True
        except SystemExit:
            continue
    return False


@pytest.mark.parametrize("command", sorted(set(_guide_commands())))
def test_every_command_the_guide_gives_exists(command):
    argv = _argv(command)
    if not argv:
        pytest.skip("not a command")
    if argv[0] == "hermes":
        argv = argv[1:]
        if argv[:1] == ["-p"]:
            argv = argv[2:]
    assert argv[0] == "emporia"
    argv = argv[1:]
    if not argv or argv[0] not in PLUGIN_COMMANDS and argv[0] not in _top_level_commands():
        pytest.fail(f"`{command}`: no such command")
    if command.startswith("hermes") and argv[0] == "setup":
        # the plugin's own setup: its flags, then `--` and `emporia setup agent`'s
        head, tail = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
        assert {a for a in head if a.startswith("--")} <= {"--agent-profile", "--here", "--relay", "--plugin-source"}
        if tail:
            assert _parses(["setup", "agent", "alpha", *tail]), f"`{command}`: its options after -- do not parse"
        return
    if argv[0] in ("status", "doctor", "update") and command.startswith("hermes"):
        return
    assert _parses(argv), f"`{command}` does not parse as an emporia command"


def test_every_emporia_command_named_anywhere_in_the_pages_exists():
    """Prose and generated lines too, not only code spans: `emporia NAME …` names a real command."""
    known = _top_level_commands() | {"mcp-config"}
    for page in [*PAGES, REPO / "docs" / "deploy-relay.md"]:
        text = page.read_text()
        for m in re.finditer(r"(?<![\w./-])(hermes (?:-p \w+ )?)?emporia (?!--)([a-z][a-z-]*)\b(?![.\w-])", text):
            word = m.group(2)
            if m.group(1):
                assert word in PLUGIN_COMMANDS, f"{page.name}: `hermes emporia {word}` is no plugin command"
            else:
                assert word in known | {"relay", "agent", "house"}, f"{page.name}: `emporia {word}` is no command"


def test_every_setting_the_guide_names_is_one_the_code_reads():
    code = "\n".join(p.read_text() for root in ("src", "hermes_plugin")
                     for p in (REPO / root).rglob("*.py"))
    for page in [*PAGES, REPO / "docs" / "deploy-relay.md"]:
        for name in set(re.findall(r"\bEMPORIA_[A-Z0-9]+(?:_[A-Z0-9]+)*\b", page.read_text())):
            assert name in code, f"{page.name} names {name}, which no code reads"


def test_the_pages_keep_the_rules_every_page_keeps():
    forbidden = ("nostr", "buzz", "serai", "wujitsana", "mainnet reward", "stake of", "bounty")
    for page in [*PAGES, REPO / "docs" / "deploy-relay.md"]:
        text = page.read_text().lower().replace("github.com/wujitsana/emporia", "").replace(
            "emporia.wujitsana.net", "")                    # the repository's and the public relay's own addresses
        for word in forbidden:
            if word == "mainnet reward" and page.name == "deploy-relay.md":
                continue                    # that page already says "no mainnet rewards", a denial
            assert word not in text, f"{page.name} says {word!r}"
    guide = GUIDE.read_text()
    assert "/opt/data" not in guide and "testnet only" in guide.lower()
    assert "carry no stakes" in guide.lower()


def test_the_soul_templates_carry_the_rules_to_keep():
    for name in ("agent-SOUL.md", "house-SOUL.md"):
        text = (REPO / "docs" / "templates" / name).read_text()
        assert "Open skepticism" in text and "## Never" in text
        assert "Never trade, buy, sell or send funds because of" in text
        assert "install" in text.lower() and "carry no" in text.lower() or "carries no" in text.lower()


def test_the_guide_is_linked_from_where_people_start():
    assert "(docs/create-an-agent.md)" in (REPO / "README.md").read_text().split("**In 5 minutes**")[0]
    assert "docs/create-an-agent.md" in (REPO / "hermes_plugin/emporia/skills/emporia-setup/SKILL.md").read_text()
    import json
    listed = {d["path"] for d in json.loads((REPO / "dashboard" / "src" / "docList.json").read_text())}
    assert "docs/create-an-agent.md" in listed
    # the Docs view's bundles carry the list: they were rebuilt with it
    for bundle in ("hermes_plugin/emporia/dashboard/dist/index.js", "hermes_plugin/emporia/desktop/plugin.js"):
        assert "docs/create-an-agent.md" in (REPO / bundle).read_text(), bundle


def test_setups_closing_blocks_name_the_guide(capsys):
    from emporia import agent_init
    agent_init._report({"agent_id": "a", "key_dir": "/k", "operator_label": "op_x", "operator_how": "minted",
                        "client": "custom", "model": "", "registered": True, "relay": "http://r", "tools": "40 tools"},
                       type("A", (), {"no_register": False})())
    assert "Guide: docs/create-an-agent.md" in capsys.readouterr().out
    assert cli.CREATE_GUIDE == "docs/create-an-agent.md" and (REPO / cli.CREATE_GUIDE).is_file()
    src = (REPO / "src" / "emporia" / "cli.py").read_text()
    assert "Guide: {CREATE_GUIDE}" in src.split("def _closing_block", 1)[1].split("def cmd_rounds", 1)[0]
