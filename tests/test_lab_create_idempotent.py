"""`emporia lab NAME create` makes no orphan room: an existing lab is reported, a refusal closes the room."""
import argparse
import json
import subprocess

import pytest

from emporia import cli, lab_proof

SLUG, REPO = "widgets", "https://github.com/acme/widgets"
STABLE = "a" * 64


class Fake:
    """The relay as `_lab_create` sees it, through `_operator_call` and `_as_agent`."""

    def __init__(self, labs=None, rooms=None, post_status=200, represents=True):
        self.labs, self.rooms, self.post_status, self.represents = labs or [], rooms or [], post_status, represents
        self.made, self.closed, self.posted = [], [], []

    def operator_call(self, profile, method, path, body=None, params=None):
        if method == "GET" and path == f"/labs/{SLUG}":
            hit = next((x for x in self.labs if x["slug"] == SLUG), None)
            return (200, hit) if hit else (404, {"detail": "no such lab"})
        if method == "GET" and path == "/agents/me":
            return 200, {"stable_id": STABLE}
        if method == "GET" and path == "/labs":
            return 200, {"labs": self.labs}
        if method == "GET" and path == "/rooms":
            return 200, {"rooms": self.rooms}
        if method == "POST" and path == "/labs":
            self.posted.append(body)
            return self.post_status, ({"slug": SLUG, "subject": body["subject"], "room_id": body["room_id"],
                                       "verified": True, "checked_at": "now"} if self.post_status == 200
                                      else {"detail": "refused"})
        return 404, {}

    def as_agent(self, profile, code, **env):
        if "create_room" in code:
            rid = f"room{len(self.made) + 1}"
            self.made.append(rid)
            return subprocess.CompletedProcess([], 0, json.dumps({"success": True, "room_id": rid}) + "\n", "")
        if "close_room" in code:
            self.closed.append(code.split("close_room(")[1].split(")")[0].strip("'\""))
            return subprocess.CompletedProcess([], 0, json.dumps({"success": True}) + "\n", "")
        return subprocess.CompletedProcess([], 1, "", "unexpected")


@pytest.fixture
def run(monkeypatch, tmp_path):
    def go(fake):
        monkeypatch.setattr(cli, "_operator_call", fake.operator_call)
        monkeypatch.setattr(cli, "_as_agent", fake.as_agent)
        monkeypatch.setattr(cli, "_lab_key_of", lambda profile, aid, at="": ("pk", STABLE))
        monkeypatch.setattr(cli, "_relay_id_of", lambda profile: "relay-1")
        monkeypatch.setattr(cli, "_lab_sign", lambda *a, **k: ("2026-01-01T00:00:00+00:00", "sig"))
        monkeypatch.setattr(lab_proof, "tip", lambda *a, **k: "c" * 40)
        monkeypatch.setattr(lab_proof, "check", lambda *a, **k: {"represents": fake.represents, "why": "no proof file",
                                                                 "tip": "c" * 40, "proof": None})
        args = argparse.Namespace(repo=REPO, subject=None, branch="main", commit=None, name="Widgets",
                                  maintainer=None, room_type="public", gate="open", entry_fee_cents=0,
                                  encrypted=False, licence="MIT")

        def call(method, path, body=None, params=None):
            status, d = fake.operator_call(tmp_path, method, path, body, params)
            if status != 200:
                raise RuntimeError(f"relay {status}: {d}")
            return d
        return cli._lab_create(tmp_path, "me", [SLUG], args, call)
    return go


def test_an_existing_lab_is_reported_and_no_room_is_made(run, capsys):
    fake = Fake(labs=[{"slug": SLUG, "subject": {"kind": "github_repo", "ref": "acme/widgets"}, "creator": STABLE,
                       "room_id": "r0"}])
    assert run(fake) == 0 and fake.made == [] and fake.posted == []
    assert "already exists" in capsys.readouterr().out


def test_a_slug_taken_by_another_lab_is_refused_without_a_room(run, capsys):
    fake = Fake(labs=[{"slug": SLUG, "subject": {"kind": "github_repo", "ref": "other/thing"}, "creator": STABLE,
                       "room_id": "r0"}])
    assert run(fake) == 1 and fake.made == []
    assert "taken" in capsys.readouterr().out


def test_a_missing_proof_stops_before_any_room_exists(run, capsys):
    fake = Fake(represents=False)
    assert run(fake) == 1 and fake.made == [] and fake.posted == []
    assert "no proof file" in capsys.readouterr().out


def test_a_refused_create_closes_the_room_it_made(run, capsys):
    fake = Fake(post_status=409)
    with pytest.raises(RuntimeError):
        run(fake)
    assert fake.made == ["room1"] and fake.closed == ["room1"]
    assert "closed" in capsys.readouterr().out


def test_an_unbound_open_room_of_this_agent_is_reused_not_a_second_one_made(run):
    mine = {"room_id": "old", "creator_id": "me", "name": "lab: Widgets", "closed": False, "room_type": "public",
            "gate_type": "open", "encrypted": False}
    fake = Fake(rooms=[mine, {**mine, "room_id": "bound"}, {**mine, "room_id": "theirs", "creator_id": "x"}],
                labs=[{"slug": "other", "room_id": "bound"}])
    assert run(fake) == 0
    assert fake.made == [] and fake.posted[0]["room_id"] == "old"
