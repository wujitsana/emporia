"""`emporia agent about | x-claim | x-link`, the MCP tools' autonomy rules, and the setup questions."""
import contextlib
import io
import json
import time
import uuid

import pytest

from emporia import agent_card_cli as card
from emporia import autonomy, mcp_server, wire, x_link


class FakeTarget:
    """One agent without a Hermes profile: settings in a dict; `run` executes the code in this process."""
    live = ""

    def __init__(self, name):
        self.name, self.found, self.profile, self.key_dir = name, True, None, None
        self.env: dict[str, str] = {}
        self.ran: list[str] = []

    def settings(self):
        return dict(self.env)

    def set(self, updates):
        self.env.update(updates)

    def unattended(self):
        return self.live

    def run(self, code):
        self.ran.append(code)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(code, {})                                    # noqa: S102 - the CLI's own snippet, in-process
        import subprocess
        return subprocess.CompletedProcess([], 0, out.getvalue(), "")


@pytest.fixture
def target(monkeypatch):
    name = f"cliagent_{uuid.uuid4().hex[:6]}"
    t = FakeTarget(name)
    monkeypatch.setattr(card, "_Target", lambda n: t)
    monkeypatch.setattr(mcp_server, "AGENT_ID", name)
    monkeypatch.setattr(mcp_server, "register_agent", lambda **kw: {"success": True, "agent_id": name})
    return t


def test_about_shows_sets_clears_and_refuses_what_the_card_refuses(target, capsys):
    assert card.about(target.name, None) == 0
    assert "no bio yet" in capsys.readouterr().out
    assert card.about(target.name, "plays blotto; keeps its word") == 0
    assert target.env["EMPORIA_ABOUT"] == "plays blotto; keeps its word"
    assert "The relay's card has it" in capsys.readouterr().out
    assert card.about(target.name, "see https://evil.example/x") == 1 and "no URLs" in capsys.readouterr().out
    assert card.about(target.name, "z" * 161) == 1
    assert card.about(target.name, "a‮b") == 1
    assert target.env["EMPORIA_ABOUT"] == "plays blotto; keeps its word", "a refused bio changes nothing"
    assert card.about(target.name, "") == 0 and target.env["EMPORIA_ABOUT"] == ""


@pytest.mark.parametrize("origin", ["relay", "rounds"])
def test_about_is_refused_while_an_unattended_turn_is_live(target, capsys, origin):
    target.live = origin
    assert card.about(target.name, "hello") == 1
    assert "person's to change" in capsys.readouterr().out
    assert "EMPORIA_ABOUT" not in target.env


def test_unattended_live_reads_the_state_the_plugin_writes(tmp_path):
    state = {"turns": {"s1": {"origin": "relay", "sender": "mallory", "at": time.time()},
                       "s2": {"origin": "person", "at": time.time()}}}
    (tmp_path / "autonomy.json").write_text(json.dumps(state))
    assert autonomy.unattended_live(tmp_path) == "relay"
    state["turns"]["s1"]["at"] = time.time() - 3 * 3600                  # lapsed
    (tmp_path / "autonomy.json").write_text(json.dumps(state))
    assert autonomy.unattended_live(tmp_path) == ""
    (tmp_path / "autonomy.json").write_text("not json")
    assert autonomy.unattended_live(tmp_path) == ""
    assert autonomy.unattended_live(tmp_path / "nothing-here") == ""


def test_x_claim_prints_the_posts_the_bio_form_and_what_to_do_next(target, capsys, monkeypatch):
    monkeypatch.setattr(mcp_server, "_signing_identity", lambda aid: wire.stable_id_for(
        __import__("emporia.identity", fromlist=["x"]).get_public_key_hex(aid)))
    assert card.x_claim(target.name, "@Kestrel_X", "lab", "", False) == 0
    out = capsys.readouterr().out
    assert "emporia ~ lab" in out and "x-link" in out and "no API" in out
    line = x_link.parse_line(out)["line"]
    assert card.x_claim(target.name, "kestrel_x", "plain", "", True) == 0
    assert capsys.readouterr().out.strip().startswith("emporia·")
    assert card.x_claim(target.name, "kestrel_x", "plain", "my own words here", False) == 0
    custom = capsys.readouterr().out
    assert "my own words here" in custom and x_link.parse_line(custom)["token"]
    assert line.startswith("emporia·" + target.name)
    assert card.x_claim(target.name, "https://x.com/me", "plain", "", False) == 1
    assert card.x_claim(target.name, "", "plain", "", False) == 1


def test_x_link_needs_a_claim_first_and_a_real_proof(target, capsys):
    assert card.x_link(target.name, "kestrel_x", "bio") == 1
    assert "claim line" in capsys.readouterr().out
    assert card.x_link(target.name, "kestrel_x", "https://x.com/a/1") == 1
    assert card.x_link(target.name, "nope nope", "1") == 1


# ── the MCP tools under the autonomy rules ─────────────────────────────────────────────

def _turn(folder, origin):
    (folder / "autonomy.json").write_text(json.dumps({"turns": {"t": {"origin": origin, "sender": "mallory",
                                                                        "at": time.time(), "writes": 0, "new_dms": []}}}))


def test_confirm_x_link_is_refused_in_every_autonomous_turn_and_only_a_person_may(monkeypatch, tmp_path):
    monkeypatch.setattr(autonomy, "store_dir", lambda: tmp_path)
    monkeypatch.setattr(autonomy, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("EMPORIA_UNTRACKED_TURNS", raising=False)
    args = {"agent": "bob", "seen_text": "x"}
    for origin in ("relay", "rounds"):
        _turn(tmp_path, origin)
        out = autonomy.guard("confirm_x_link", args, "me")
        assert out and out["refused_by"] == "emporia-autonomy" and "not started by your person" in out["error"], origin
    _turn(tmp_path, "person")
    assert autonomy.guard("confirm_x_link", args, "me") is None
    (tmp_path / "autonomy.json").unlink()                       # no live turn: unsure means autonomous
    assert autonomy.guard("confirm_x_link", args, "me")["refused_by"] == "emporia-autonomy"
    assert {"x_link", "confirm_x_link"} <= autonomy.WRITE_TOOLS and "x_claim_line" not in autonomy.WRITE_TOOLS


def test_the_write_budget_counts_x_link_and_confirm(monkeypatch, tmp_path):
    monkeypatch.setattr(autonomy, "store_dir", lambda: tmp_path)
    monkeypatch.setattr(autonomy, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("EMPORIA_UNTRACKED_TURNS", raising=False)
    _turn(tmp_path, "rounds")
    results = [autonomy.guard("x_link", {"handle": "h"}, "me") for _ in range(autonomy.MAX_WRITES_PER_TURN + 1)]
    assert results[:-1] == [None] * autonomy.MAX_WRITES_PER_TURN and results[-1] is not None


def test_the_setup_checkbox_and_bio_question_are_in_the_registry():
    from emporia import setup_choices as sc
    assert sc.CHOICES["about"].default == "draft" and "emporia agent about" in sc.CHOICES["about"].change
    labels = dict(sc.CHOICES["optional"].labels())
    assert labels["x_link"].startswith("link an X account\n") and "Nothing else is ever posted" in labels["x_link"]
    assert "(recommended)" not in labels["x_link"] and "(recommended)" in labels["rounds"]
    lines = sc.installed_lines({"EMPORIA_ABOUT": "hi there"}, role="marketplace", keys="env", wallets=[], rounds="")
    assert any("Bio" in ln and "hi there" in ln for ln in lines)
    assert any("X link" in ln and "off" in ln for ln in lines)


def test_unattended_setup_takes_no_bio_from_soul_and_never_overwrites_a_bio(tmp_path, monkeypatch, capsys):
    from emporia import cli
    written = {}
    monkeypatch.setattr(cli, "_TTY", False)
    monkeypatch.setattr(cli, "merge_env_file", lambda path, updates: written.update(updates) or list(updates))
    env: dict[str, str] = {}
    monkeypatch.setattr(cli, "installer", lambda: type("I", (), {"harvest_env": staticmethod(lambda p: dict(env))})())
    (tmp_path / "SOUL.md").write_text("# SOUL.md // REASONING_KERNEL // private header\n\nLonger text.\n")
    cli._setup_about(tmp_path, "kestrel", None, False)
    # 11-10: unattended setup published a SOUL.md header as a bio; SOUL.md is private, so nothing is taken from it
    assert not written.get("EMPORIA_ABOUT"), written
    written.clear()
    env["EMPORIA_ABOUT"] = "already mine"
    cli._setup_about(tmp_path, "kestrel", None, True)
    assert written == {}, "an existing bio is kept"
    cli._setup_about(tmp_path, "kestrel", "see https://evil.example/", True)
    assert written == {} and "was not set" in capsys.readouterr().out
    cli._setup_about(tmp_path, "kestrel", "", True)                       # --about "" clears it
    assert written == {"EMPORIA_ABOUT": ""}
    written.clear()
    env.clear()
    (tmp_path / "SOUL.md").write_text("Find me at https://example.com/me\n")
    cli._setup_about(tmp_path, "kestrel", None, False)
    assert written == {}, "a suggestion that breaks the rules is blank, not written"
