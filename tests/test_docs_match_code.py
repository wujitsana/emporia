"""P174: what the docs tell people to run, and the names they show, match the code."""
import re
import shlex
from pathlib import Path

import pytest

from emporia import cli, networks

REPO = Path(__file__).resolve().parents[1]
DOCS = ["README.md", "docs/agent-security-setup.md", "docs/games.md", "docs/use-cases.md",
        "docs/manual-testing.md", "skills/emporia-setup/SKILL.md", "SECURITY.md"]
SUBS = {"NAME": "alpha", "ROLE": "marketplace", "VAR": "X", "K": "X", "PATH": "x"}


def _commands() -> list[tuple[str, str]]:
    """`emporia …` command lines in the docs: code spans and code-block lines."""
    out = []
    for rel in DOCS:
        text = (REPO / rel).read_text()
        spans = re.findall(r"`(emporia [^`]+)`", text)
        spans += [m.group(1) for m in re.finditer(r"^\s*(emporia [^\n#]+?)\s*(?:#.*)?$", text, re.M)]
        for span in spans:
            out.append((rel, span.strip()))
    return out


def _argvs(command: str) -> list[list[str]]:
    """Each command as arguments, placeholders filled in. Prose shorthand is
    understood: `a && b` (two commands), `start | stop` (alternatives)."""
    out = []
    for part in command.split("&&"):
        part = part.split(" | ")[0].strip()
        part = part.replace("\\|", "|").replace("0x…", "0x" + "ab" * 20).replace("…", "x")
        part = re.sub(r"\[([^\]]*)\]", lambda m: m.group(1).split("|")[0], part)   # [a|b] → a
        part = re.sub(r"\b([a-z-]+)\|[a-z|-]+", r"\1", part)                        # show|create → show
        try:
            argv = shlex.split(part)
        except ValueError:
            continue
        if argv and argv[0] == "emporia":
            out.append([SUBS.get(a, a) for a in argv[1:] if not a.startswith("(")])
    return out


def _parses(argv: list[str]) -> bool:
    for attempt in (argv, argv + ["x"]):        # an option shown without its value
        try:
            cli.build_parser().parse_args(attempt)
            return True
        except SystemExit:
            continue
    return False


@pytest.mark.parametrize("where,command", _commands())
def test_every_documented_emporia_command_exists(where, command):
    argvs = _argvs(command)
    if not argvs:
        pytest.skip("not a command")
    for argv in argvs:
        assert _parses(argv), f"{where}: `{command}` does not parse as an emporia command"


def test_the_dashboards_network_names_match_the_python_table():
    ts = (REPO / "dashboard" / "src" / "paymentFormat.ts").read_text()
    block = ts[ts.index("NETWORK_NAMES"):ts.index("};", ts.index("NETWORK_NAMES"))]
    dashboard = dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block))
    assert dashboard == {k: n.label for k, n in networks.NETWORKS.items()}
